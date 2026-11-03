import json
import threading
import urllib.error
import urllib.request

import pytest

from bian.app import App
from bian.config import AppConfig
from bian.store import Store
from bian.web import auth
from bian.web.server import make_server

P20T = "web.1000000001.ABCDEF0123456789ABCDEF0123456789"
CSRF = "abcdef0123456789abcdef0123456789"
CURL = f"curl 'https://www.binance.com/bapi/x' -b 'lang=zh; p20t={P20T}; theme=dark' -H 'csrftoken: {CSRF}'"


class Client:
    def __init__(self, base):
        self.base, self.cookie, self.csrf = base, "", ""

    def call(self, method, path, body=None, csrf=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"content-type": "application/json"})
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        if csrf and self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status, raw, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), e.headers
        sc = headers.get("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0]
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode()

    def login(self, pw):
        s, d = self.call("POST", "/api/login", {"username": "admin", "password": pw})
        if s == 200:
            self.csrf = d["csrf"]
        return s, d


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("BINANCE_P20T", "BINANCE_CSRFTOKEN", "SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(k, raising=False)
    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    auth.set_password(Store(app.db_path), "admin", "Passw0rd123")
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, Client(f"http://127.0.0.1:{srv.server_address[1]}")
    srv.shutdown()


def test_health_and_static_without_login(web):
    app, c = web
    assert c.call("GET", "/api/health")[1]["ok"] is True
    s, body = c.call("GET", "/")
    assert s == 200 and "<html" in body.lower()
    s, _ = c.call("GET", "/../../etc/passwd")
    assert s == 200  # 路径穿越回落到首页，不会读到系统文件
    assert c.call("GET", "/api/overview")[0] == 401


def test_login_rate_limit_and_csrf(web):
    app, c = web
    for _ in range(5):
        assert c.login("wrong")[0] == 401
    assert c.login("Passw0rd123")[0] == 429  # 10 分钟内失败 5 次后锁定


def test_settings_roundtrip_validation_and_csrf(web):
    app, c = web
    assert c.login("Passw0rd123")[0] == 200
    s, view = c.call("GET", "/api/settings")
    assert s == 200 and view["rules"]["loss_tiers"] == [50, 100, 150]
    good = c.csrf
    c.csrf = "bad"
    assert c.call("PUT", "/api/settings", {"rules": {"loss_tiers": [40, 80]}})[0] == 403
    c.csrf = good
    s, d = c.call("PUT", "/api/settings", {"rules": {"loss_tiers": [-1]}})
    assert s == 400 and "档位" in d["error"]
    s, d = c.call("PUT", "/api/settings", {"rules": {"loss_tiers": [80, 40]}, "preset": "custom",
                                           "notify": {"smtp_user": "a@163.com"}, "smtp_pass": "auth#code"})
    assert s == 200 and "rules.loss_tiers" in d["changed"] and d["settings"]["smtp_pass_set"]
    assert "auth#code" not in json.dumps(d)
    assert app.settings.rules_config().loss_tiers == (40.0, 80.0)
    s, d = c.call("POST", "/api/settings/preset", {"name": "aggressive"})
    assert s == 200 and d["settings"]["preset"] == "aggressive"
    ev = [e["type"] for e in Store(app.db_path).events(limit=20)]
    assert "settings.changed" in ev and "settings.preset" in ev


def test_credentials_paste_validates_and_never_leaks(web):
    app, c = web
    c.login("Passw0rd123")
    s, d = c.call("POST", "/api/credentials", {"curl": "随便一段文字"})
    assert s == 400 and "p20t" in d["error"]
    app.guard.update.__func__  # 确认是绑定方法
    orig = app.guard.update
    app.guard.update = lambda p, cs: orig(p, cs, validator=lambda *a: (True, "有效", 4))
    s, d = c.call("POST", "/api/credentials", {"curl": CURL})
    assert s == 200 and "4 个" in d["message"]
    assert app.settings.binance_credentials() == (P20T, CSRF)
    for path in ("/api/settings", "/api/overview", "/api/events", "/api/alerts"):
        s, body = c.call("GET", path)
        assert s == 200 and P20T not in json.dumps(body) and CSRF not in json.dumps(body)


def test_overview_todos_and_done(web):
    app, c = web
    c.login("Passw0rd123")
    s, ov = c.call("GET", "/api/overview")
    keys = [t["key"] for t in ov["todos"]]
    assert "cred:missing" in keys and "email:missing" in keys
    assert c.call("POST", "/api/todos/done", {"key": "email:missing"})[0] == 200
    keys = [t["key"] for t in c.call("GET", "/api/overview")[1]["todos"]]
    assert "email:missing" not in keys


def test_password_change_rotates_session(web):
    app, c = web
    c.login("Passw0rd123")
    s, d = c.call("POST", "/api/password", {"old": "x", "new": "NewPassw0rd"})
    assert s == 400
    s, d = c.call("POST", "/api/password", {"old": "Passw0rd123", "new": "short"})
    assert s == 400 and "8" in d["error"]
    s, d = c.call("POST", "/api/password", {"old": "Passw0rd123", "new": "NewPassw0rd9"})
    assert s == 200
    c.csrf = d["csrf"]
    assert c.call("GET", "/api/me")[0] == 200
    c2 = Client(c.base)
    assert c2.login("Passw0rd123")[0] == 401 and c2.login("NewPassw0rd9")[0] == 200


def test_email_failure_chip_ignores_intentional_skips(web):
    from bian.rules import Alert
    from bian.web import views

    app, c = web
    store = Store(app.db_path)
    a = Alert("R00", "info", "k", 0, "开仓", ts=__import__("time").time())
    rid = store.add_alert(a, "x", False, "")
    store.update_delivery(rid, False, "email: 低于邮件通知级别，未发送")
    rid = store.add_alert(a, "x", False, "")
    store.update_delivery(rid, False, "email: 达到每小时上限，稍后合并补发")
    assert views.overview(store, app.settings)["email"]["last_failure"] is None  # 有意不发 ≠ 故障
    rid = store.add_alert(a, "x", False, "")
    store.update_delivery(rid, False, "email: SMTP 认证失败：请核对发件账号和邮箱“授权码”")
    fail = views.overview(store, app.settings)["email"]["last_failure"]
    assert fail and "认证失败" in fail["error"]


def test_phase2_endpoints_and_export(web):
    from bian.sync import ensure_leader

    app, c = web
    c.login("Passw0rd123")
    store = Store(app.db_path)
    ensure_leader(store, "123", "=HYPERLINK(\"x\")", "copy")
    store.log_event("position.open", "=HYPERLINK(\"x\") 开仓", leader_id="123")
    s, st = c.call("GET", "/api/stats?month=2026-11")
    assert s == 200 and len(st["calendar"]["days"]) == 30 and "exits" in st
    assert c.call("GET", "/api/stats?month=abc")[0] == 400
    s, lst = c.call("GET", "/api/leaders")
    assert s == 200 and lst["leaders"][0]["lead_id"] == "123"
    assert c.call("GET", "/api/leaders/detail?id=999")[0] == 404
    s, d = c.call("GET", "/api/leaders/detail?id=123")
    assert s == 200 and d["profile"] is None
    assert c.call("POST", "/api/leaders/analyze", {"id": "abc"})[0] == 400
    s, d = c.call("POST", "/api/leaders/analyze", {"id": "123"})
    assert s == 200 and d["queued"] is True
    assert c.call("POST", "/api/leaders/analyze", {"id": "123"})[1]["queued"] is False
    assert c.call("GET", "/api/episode?id=1")[0] == 404
    assert c.call("POST", "/api/exits/manual", {"id": 1, "exit_type": "乱写"})[0] == 400
    s, body = c.call("GET", "/api/events/export?format=csv")
    assert s == 200 and body.startswith("﻿时间") and "'=HYPERLINK" in body
    s, body = c.call("GET", "/api/events/export?format=json")
    assert s == 200 and isinstance(body, list)
