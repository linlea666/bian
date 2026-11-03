"""网页后台：标准库 ThreadingHTTPServer + JSON API + 静态页面。

安全：登录会话（HttpOnly + SameSite=Strict）、修改类请求校验 X-CSRF-Token、登录失败限速、
接口永远不返回密钥明文、请求体大小限制、安全响应头。
"""
from __future__ import annotations

import json
import mimetypes
import os
import time
import traceback
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..curl_parse import CurlParseError, parse_credentials
from ..lifecycle import episodes
from ..notify import SmtpConfig, describe_smtp_error, smtp_send
from ..settings import SettingsError, public_view
from . import auth, views

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "bian_session"
MAX_BODY = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def make_handler(app):
    limiter = auth.RateLimiter()

    class Handler(BaseHTTPRequestHandler):
        server_version = "bian"
        sys_version = ""

        # ------------------------------------------------------------ 工具
        def log_message(self, fmt, *args):  # 不把请求（可能含敏感参数）写进日志
            pass

        def _ip(self) -> str:
            return self.client_address[0]

        def _cookie_token(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c[COOKIE].value if COOKIE in c else None

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200, extra: dict | None = None) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store", **(extra or {})})

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "内容太长")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError(400, "请求格式错误") from None
            if not isinstance(data, dict):
                raise ApiError(400, "请求格式错误")
            return data

        def _session(self, write: bool) -> dict:
            s = auth.get_session(app.store, self._cookie_token())
            if not s:
                raise ApiError(401, "请先登录")
            if write and self.headers.get("X-CSRF-Token") != s["csrf"]:
                raise ApiError(403, "页面已过期，请刷新后重试")
            return s

        # ------------------------------------------------------------ 分发
        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def _dispatch(self, method: str) -> None:
            url = urlparse(self.path)
            try:
                if url.path.startswith("/api/"):
                    route = ROUTES.get((method, url.path))
                    if not route:
                        raise ApiError(404, "接口不存在")
                    fn, need_login = route
                    q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                    sess = self._session(method != "GET") if need_login else None
                    body = self._body() if method in ("POST", "PUT") else {}
                    result = fn(self, q, body, sess)
                    if result is not None:
                        self._json(result)
                elif method == "GET":
                    self._static(url.path)
                else:
                    raise ApiError(405, "不支持")
            except ApiError as e:
                self._json({"error": e.message}, e.status)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                app.store.log_event("web.error", f"后台接口异常：{type(e).__name__}", "warn", path=url.path)
                self._json({"error": "服务器内部错误，详情见日志"}, 500)

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.realpath(os.path.join(STATIC_DIR, name))
            if not full.startswith(os.path.realpath(STATIC_DIR) + os.sep) or not os.path.isfile(full):
                full = os.path.join(STATIC_DIR, "index.html")  # 单页应用：未知路径都回到首页
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                body = f.read()
            cache = "no-cache" if full.endswith(".html") else "max-age=300"
            self._send(200, body, ctype, {"Cache-Control": cache})

    # ================================================================ 接口
    def health(h, q, b, s):
        poll, _ = app.store.get_runtime("poll", {})
        age = time.time() - poll["ts"] if poll and poll.get("ts") else None
        return {"ok": True, "version": __version__, "poll_age": age, "uptime": time.time() - app.started}

    def login(h, q, b, s):
        ip = h._ip()
        wait = limiter.blocked(ip)
        if wait:
            raise ApiError(429, f"登录失败次数过多，请 {int(wait / 60) + 1} 分钟后再试")
        user, pw = str(b.get("username") or "admin"), str(b.get("password") or "")
        if not auth.check_login(app.store, user, pw):
            limiter.fail(ip)
            app.store.log_event("auth.login_failed", "后台登录失败", "warn", ip=ip, username=user)
            raise ApiError(401, "用户名或密码错误")
        limiter.reset(ip)
        token, csrf = auth.create_session(app.store, user)
        app.store.log_event("auth.login", "后台登录", ip=ip, username=user)
        cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={auth.SESSION_TTL}"
        h._json({"ok": True, "csrf": csrf}, extra={"Set-Cookie": cookie})

    def logout(h, q, b, s):
        auth.delete_session(app.store, h._cookie_token())
        h._json({"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})

    def me(h, q, b, s):
        cfg = app.settings.get()
        return {"username": s["username"], "csrf": s["csrf"], "must_change": auth.must_change(app.store, s["username"]),
                "onboarding_done": cfg["onboarding_done"], "version": __version__}

    def change_password(h, q, b, s):
        old, new = str(b.get("old") or ""), str(b.get("new") or "")
        if not auth.check_login(app.store, s["username"], old):
            raise ApiError(400, "原密码不正确")
        problem = auth.password_problem(new)
        if problem:
            raise ApiError(400, problem)
        auth.set_password(app.store, s["username"], new)
        token, csrf = auth.create_session(app.store, s["username"])
        app.store.log_event("auth.password_changed", "后台密码已修改", username=s["username"])
        cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={auth.SESSION_TTL}"
        h._json({"ok": True, "csrf": csrf}, extra={"Set-Cookie": cookie})

    def overview(h, q, b, s):
        return views.overview(app.store, app.settings)

    def positions(h, q, b, s):
        return views.positions_view(app.store)

    def equity(h, q, b, s):
        return {"points": views.equity_curve(app.store, int(q.get("days") or 30))}

    def alerts(h, q, b, s):
        return {"alerts": app.store.alerts_page(int(q.get("limit") or 50), int(q["before"]) if q.get("before") else None,
                                                q.get("severity") or None)}

    def events(h, q, b, s):
        return {"events": app.store.events(
            limit=int(q.get("limit") or 100), before_id=int(q["before"]) if q.get("before") else None,
            type_prefix=q.get("type") or None, leader_id=q.get("leader") or None,
            episode_id=int(q["episode"]) if q.get("episode") else None, level=q.get("level") or None,
            q=q.get("q") or None)}

    def episode_list(h, q, b, s):
        return {"episodes": episodes(app.store, q.get("status") or None, q.get("leader") or None,
                                     int(q.get("limit") or 100), int(q["before"]) if q.get("before") else None)}

    def get_settings(h, q, b, s):
        poll, _ = app.store.get_runtime("poll", {})
        view = public_view(app.settings)
        view["leaders"] = [{"id": lr["id"], "name": lr["name"]} for lr in (poll or {}).get("leaders", [])]
        view["credentials"]["status"] = (app.store.get_runtime("credentials", {})[0] or {})
        return view

    def put_settings(h, q, b, s):
        for k in ("credentials", "presets", "preset_names", "smtp_pass_set", "leaders"):
            b.pop(k, None)
        smtp_pass = b.pop("smtp_pass", None)
        try:
            _, changed = app.settings.update(b)
        except SettingsError as e:
            raise ApiError(400, str(e)) from None
        if smtp_pass:
            app.settings.set_secrets(smtp_pass=str(smtp_pass))
            changed.append("notify.smtp_pass")
        if changed:
            app.store.log_event("settings.changed", "修改设置：" + "、".join(changed[:12]), fields=changed,
                                username=s["username"])
            app.wake()
        return {"ok": True, "changed": changed, "settings": get_settings(h, q, {}, s)}

    def apply_preset(h, q, b, s):
        try:
            app.settings.apply_preset(str(b.get("name")))
        except SettingsError as e:
            raise ApiError(400, str(e)) from None
        from ..settings import PRESET_NAMES

        app.store.log_event("settings.preset", f"采用风控预设：{PRESET_NAMES.get(str(b.get('name')), b.get('name'))}",
                            username=s["username"])
        app.wake()
        return {"ok": True, "settings": get_settings(h, q, {}, s)}

    def credentials(h, q, b, s):
        text = str(b.get("curl") or "").strip()
        try:
            if text:
                p20t, csrf = parse_credentials(text)
            else:
                p20t, csrf = str(b.get("p20t") or "").strip(), str(b.get("csrftoken") or "").strip()
                if not (p20t and csrf):
                    raise CurlParseError("请粘贴 cURL，或同时填写 p20t 和 csrftoken")
        except CurlParseError as e:
            raise ApiError(400, str(e)) from None
        ok, msg, rels = app.guard.update(p20t, csrf)
        if not ok:
            raise ApiError(400, msg)
        app.wake()
        return {"ok": True, "message": f"凭证有效，进行中的跟单 {rels} 个。监控已使用新凭证。",
                "credentials": app.settings.credential_meta()}

    def test_email(h, q, b, s):
        cfg = app.settings.get()["notify"]
        form = {**cfg, **{k: v for k, v in b.items() if k.startswith("smtp_")}}
        pw = str(b.get("smtp_pass") or "") or app.settings.secrets().get("smtp_pass", "")
        if not (form.get("smtp_host") and form.get("smtp_user") and pw):
            raise ApiError(400, "请填写 SMTP 服务器、发件邮箱和授权码")
        port = int(form.get("smtp_port") or 465)
        sec = form.get("smtp_security") or ("tls" if port == 465 else "starttls")
        to = [x.strip() for x in str(form.get("smtp_to") or form["smtp_user"]).replace(";", ",").split(",") if x.strip()]
        smtp = SmtpConfig(form["smtp_host"], port, form["smtp_user"], pw, to, sec, form["smtp_user"])
        try:
            smtp_send(smtp, "✅ 币安跟单风控：测试邮件",
                      f"这是一封测试邮件（{time.strftime('%Y-%m-%d %H:%M:%S')}）。收到说明邮件提醒配置正确。\n"
                      "如果在垃圾箱里找到，请把发件地址加入白名单。")
        except Exception as e:  # noqa: BLE001
            app.store.log_event("notify.test_failed", "测试邮件发送失败", "warn", error=describe_smtp_error(e))
            raise ApiError(400, describe_smtp_error(e)) from None
        app.store.log_event("notify.test_sent", f"测试邮件已发送到 {', '.join(to)}")
        return {"ok": True, "message": f"已交给邮件服务器（发往 {', '.join(to)}），请检查收件箱和垃圾箱。测试不会保存配置。"}

    def todo_done(h, q, b, s):
        key = str(b.get("key") or "")
        if not key:
            raise ApiError(400, "缺少 key")
        app.store.mark_todo_done(key)
        app.store.log_event("todo.done", f"标记已处理：{b.get('title') or key}", ref=key, username=s["username"])
        return {"ok": True}

    def onboarding_done(h, q, b, s):
        app.settings.update({"onboarding_done": True})
        app.store.log_event("settings.onboarding", "完成首次引导", username=s["username"])
        return {"ok": True}

    ROUTES = {
        ("GET", "/api/health"): (health, False),
        ("POST", "/api/login"): (login, False),
        ("POST", "/api/logout"): (logout, False),
        ("GET", "/api/me"): (me, True),
        ("POST", "/api/password"): (change_password, True),
        ("GET", "/api/overview"): (overview, True),
        ("GET", "/api/positions"): (positions, True),
        ("GET", "/api/equity"): (equity, True),
        ("GET", "/api/alerts"): (alerts, True),
        ("GET", "/api/events"): (events, True),
        ("GET", "/api/episodes"): (episode_list, True),
        ("GET", "/api/settings"): (get_settings, True),
        ("PUT", "/api/settings"): (put_settings, True),
        ("POST", "/api/settings/preset"): (apply_preset, True),
        ("POST", "/api/credentials"): (credentials, True),
        ("POST", "/api/email/test"): (test_email, True),
        ("POST", "/api/todos/done"): (todo_done, True),
        ("POST", "/api/onboarding/done"): (onboarding_done, True),
    }
    Handler.status = HTTPStatus
    return Handler


def make_server(app, host: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    srv.daemon_threads = True
    return srv
