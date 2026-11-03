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
from ..csvutil import to_csv
from ..curl_parse import CurlParseError, parse_credentials
from .. import exits as exits_mod, jobs as jobs_mod, leader_data, recommend as recommend_mod, stats as stats_mod
from ..api import leaders as leaders_api
from ..client import BinanceError, Client
from ..lifecycle import episodes, get_episode
from ..sync import ensure_leader
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

    # ---------------------------------------------------------- 第二期
    def stats(h, q, b, s):
        now = time.time()
        lt = time.localtime(now)
        try:
            y, m = (int(x) for x in str(q.get("month") or f"{lt.tm_year}-{lt.tm_mon}").split("-"))
            if not (2000 <= y <= 2100 and 1 <= m <= 12):
                raise ValueError
        except ValueError:
            raise ApiError(400, "月份格式应为 YYYY-MM") from None
        synced = app.store.db.execute("SELECT COUNT(*), MIN(ts_ms) FROM fills").fetchone()
        return {
            "calendar": stats_mod.calendar_month(app.store, y, m),
            "periods": stats_mod.periods(app.store, now),
            "composition": stats_mod.composition(app.store, now),
            "losses": stats_mod.losses(app.store),
            "exits": exits_mod.summary(app.store, 0),
            "exits_month": exits_mod.summary(app.store, stats_mod.period_starts(now)["month"]),
            "giveback": stats_mod.giveback(app.store),
            "sync": {"fills": synced[0], "since_ms": synced[1]},
        }

    def leaders_list(h, q, b, s):
        return {"leaders": stats_mod.ranking(app.store)}

    def _lead_id(q_or_b) -> str:
        lid = str(q_or_b.get("id") or "").strip()
        if not lid.isdigit() or len(lid) > 30:
            raise ApiError(400, "领航员 ID 应为数字（领航员主页网址里 lead-details/ 后面那串数字）")
        return lid

    def leader_detail(h, q, b, s):
        lid = _lead_id(q)
        row = app.store.db.execute("SELECT name, kind, added, history_synced, ended_ts FROM leaders WHERE lead_id=?",
                                   (lid,)).fetchone()
        if not row:
            raise ApiError(404, "还没有这个领航员的数据")
        rank = next((r for r in stats_mod.ranking(app.store) if r["lead_id"] == lid), None)
        cur = app.store.db.execute(
            "SELECT symbol, side, opened_ms, closed_ms, avg_cost, avg_close, leverage, pnl, roi, status FROM my_positions "
            "WHERE lead_id=? ORDER BY opened_ms DESC LIMIT 50", (lid,))
        cols = [c[0] for c in cur.description]
        dist = app.store.db.execute(
            "SELECT mae, mfe, pnl FROM leader_history WHERE lead_id=? AND mae IS NOT NULL", (lid,)).fetchall()
        return {
            "lead_id": lid, "name": row[0], "kind": row[1], "added": row[2], "history_synced": row[3], "ended_ts": row[4],
            "ranking": rank, "profile": jobs_mod.get_profile(app.store, lid), "job": jobs_mod.current_job(app.store, lid),
            "my_positions": [dict(zip(cols, r)) for r in cur.fetchall()],
            "exits": exits_mod.exits_list(app.store, 100, lid), "exits_summary": exits_mod.summary(app.store, 0, lid),
            "mae_mfe": [{"mae": r[0], "mfe": r[1], "win": (r[2] or 0) > 0} for r in dist],
            "link": views.LEAD_URL.format(lid),
            "recommend": recommend_mod.recommend(app.store, app.settings, lid),
        }

    def leader_adopt(h, q, b, s):
        lid = _lead_id(b)
        try:
            out = recommend_mod.adopt(app.store, app.settings, lid, str(b.get("what") or ""))
        except (ValueError, SettingsError) as e:
            raise ApiError(400, str(e)) from None
        app.wake()  # 下一轮立即按新阈值评估
        return out

    discover_cache: dict = {}

    def discover(h, q, b, s):
        """币安领航员榜单（只读、公开接口），服务端缓存 10 分钟，前端再按条件筛选。"""
        rng = q.get("range") if q.get("range") in ("7D", "30D", "90D", "180D") else "30D"
        sort = q.get("sort") if q.get("sort") in ("ROI", "PNL", "MDD", "WIN_RATE", "AUM", "COPIER_PNL", "SHARP_RATIO") \
            else "ROI"
        key = (rng, sort)
        hit = discover_cache.get(key)
        if not hit or time.time() - hit[0] > 600:
            client = discover_cache.setdefault("client", Client(min_interval=1.0))
            items = []
            try:
                for page in (1, 2, 3):  # 接口每页最多 30 条：取前 90 名
                    _, got = leaders_api.leaderboard(client, time_range=rng, sort_by=sort, page=page, page_size=30)
                    items += got
                    if len(got) < 30:
                        break
            except BinanceError as e:
                if not items:
                    raise ApiError(502, f"读取币安榜单失败：{e}") from None
            rows = [{"id": x.portfolio_id, "name": x.nickname, "roi": x.roi, "pnl": x.pnl, "mdd": x.mdd,
                     "win_rate": x.win_rate, "aum": x.aum, "copier_pnl": x.copier_pnl,
                     "copiers": x.current_copy_count, "max_copiers": x.max_copy_count,
                     "sharp": x.raw.get("sharpRatio")} for x in items]
            hit = discover_cache[key] = (time.time(), rows)
        kinds = dict(app.store.db.execute("SELECT lead_id, kind FROM leaders").fetchall())
        return {"range": rng, "sort": sort, "fetched_at": hit[0],
                "items": [{**r, "kind": kinds.get(r["id"])} for r in hit[1]]}

    def leader_analyze(h, q, b, s):
        lid = _lead_id(b)
        new = ensure_leader(app.store, lid, str(b.get("name") or lid), "watch")
        queued = jobs_mod.enqueue(app.store, lid, "手动重新分析" if not new else "手动加入观察")
        return {"ok": True, "queued": queued, "message": "已开始分析，几分钟到半小时完成（取决于历史仓位数量）"
                if queued else "这个领航员已经在分析队列里了"}

    def leader_export(h, q, b, s):
        lid = _lead_id(q)
        if not leader_data.leader_row(app.store, lid):
            raise ApiError(404, "还没有这个领航员的数据")
        body = leader_data.export_zip(app.store, app.settings, lid)
        h._send(200, body, "application/zip", {
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="bian-leader-{lid}-{time.strftime("%Y%m%d-%H%M%S")}.zip"'})
        return None

    def leader_delete(h, q, b, s):
        lid = _lead_id(b)
        row = leader_data.leader_row(app.store, lid)
        if not row:
            raise ApiError(404, "还没有这个领航员的数据")
        if str(b.get("confirm") or "").strip() != (row["name"] or lid):
            raise ApiError(400, "请输入领航员名字确认删除")
        try:
            path, counts = leader_data.delete(app.store, app.settings, lid, os.path.join(app.data_dir, "backups"))
        except leader_data.DeleteRefused as e:
            raise ApiError(400, str(e)) from None
        return {"ok": True, "backup": os.path.basename(path), "deleted": counts,
                "message": f"已删除，删除前的数据已备份到服务器 data/backups/{os.path.basename(path)}"}

    def episode_detail(h, q, b, s):
        try:
            eid = int(q.get("id") or 0)
        except ValueError:
            raise ApiError(400, "参数错误") from None
        ep = get_episode(app.store, eid)
        if not ep:
            raise ApiError(404, "找不到这个仓位回合")
        pts = app.store.db.execute("SELECT ts, roi, u, mark FROM episode_points WHERE episode_id=? ORDER BY ts",
                                   (eid,)).fetchall()
        exs = [x for x in exits_mod.exits_list(app.store, 1000, ep["leader_id"]) if x["episode_id"] == eid]
        lp = exits_mod.leader_match(app.store, ep["leader_id"], ep["symbol"], ep["side"], ep["opened_ts"])
        return {"episode": ep, "points": [{"ts": p[0], "roi": p[1], "u": p[2], "mark": p[3]} for p in pts],
                "events": app.store.events(limit=300, episode_id=eid), "exits": exs,
                "leader_position": lp.__dict__ if lp else None, "link": views.LEAD_URL.format(ep["leader_id"])}

    def exits_list(h, q, b, s):
        return {"exits": exits_mod.exits_list(app.store, int(q.get("limit") or 200), q.get("leader") or None)}

    def exit_manual(h, q, b, s):
        try:
            exits_mod.set_manual(app.store, int(b.get("id") or 0), str(b.get("exit_type") or ""))
        except ValueError as e:
            raise ApiError(400, str(e)) from None
        return {"ok": True}

    def events_export(h, q, b, s):
        rows = app.store.events(
            limit=10000, type_prefix=q.get("type") or None, leader_id=q.get("leader") or None,
            episode_id=int(q["episode"]) if q.get("episode") else None, level=q.get("level") or None,
            q=q.get("q") or None)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if q.get("format") == "json":
            body = json.dumps(rows, ensure_ascii=False, indent=1, default=str).encode()
            h._send(200, body, "application/json; charset=utf-8",
                    {"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="bian-events-{stamp}.json"'})
            return None
        body = to_csv(["时间", "类型", "级别", "领航员", "回合", "标题", "详情"], (
            [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["type"], r["level"],
             r["leader_id"] or "", r["episode_id"] or "", r["title"],
             json.dumps(r["detail"], ensure_ascii=False) if r["detail"] else ""] for r in rows))
        h._send(200, body, "text/csv; charset=utf-8",
                {"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="bian-events-{stamp}.csv"'})
        return None

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
        ("GET", "/api/stats"): (stats, True),
        ("GET", "/api/leaders"): (leaders_list, True),
        ("GET", "/api/leaders/detail"): (leader_detail, True),
        ("POST", "/api/leaders/analyze"): (leader_analyze, True),
        ("GET", "/api/episode"): (episode_detail, True),
        ("GET", "/api/exits"): (exits_list, True),
        ("POST", "/api/exits/manual"): (exit_manual, True),
        ("GET", "/api/events/export"): (events_export, True),
        ("GET", "/api/leaders/export"): (leader_export, True),
        ("POST", "/api/leaders/adopt"): (leader_adopt, True),
        ("GET", "/api/discover"): (discover, True),
        ("POST", "/api/leaders/delete"): (leader_delete, True),
    }
    Handler.status = HTTPStatus
    return Handler


def make_server(app, host: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    srv.daemon_threads = True
    return srv
