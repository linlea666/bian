import pytest

from bian import credentials as cred_mod
from bian.client import AuthExpired, BinanceError, Client
from bian.credentials import CredentialGuard
from bian.settings import Settings
from bian.store import Store

P20T = "web.1000000001.ABCDEF0123456789ABCDEF0123456789"
CSRF = "abcdef0123456789abcdef0123456789"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = Settings(str(tmp_path / "b.db"), str(tmp_path / "s.json"))
    st.set_secrets(binance_p20t=P20T, binance_csrftoken=CSRF, binance_updated_at=1_000_000.0 - 86400 * 5)
    store = Store(str(tmp_path / "b.db"))
    sent = []
    clock = Clock()
    result = {"auth": "ok"}

    def check_auth(client):
        if result["auth"] == "expired":
            raise AuthExpired("凭证失效 code=100001005")
        if result["auth"] == "net":
            raise BinanceError("网络错误")
        return True

    monkeypatch.setattr(cred_mod.copy_account, "check_auth", check_auth)
    monkeypatch.setattr(cred_mod.copy_account, "copy_relations", lambda c: [{}, {}])
    g = CredentialGuard(Client(), st, store, lambda t, b, s: sent.append((t, b, s)), clock,
                        check_interval=600, panel_url="http://1.2.3.4:3100")
    return g, st, store, sent, clock, result


def test_valid_then_two_failures_expire_and_email_once(env):
    g, st, store, sent, clock, result = env
    assert g.client.p20t == P20T
    g.tick()
    assert g.status.state == "valid" and g.status.relations == 2 and not g.paused
    result["auth"] = "expired"
    clock.t += 600
    g.tick()
    assert g.status.state == "valid" and g.status.consecutive_failures == 1 and sent == []
    clock.t += 60  # 第一次失败后 1 分钟就复查
    g.tick()
    assert g.status.state == "expired" and g.paused
    assert len(sent) == 1 and "已失效" in sent[0][0] and "Copy as cURL" in sent[0][1] and "3100" in sent[0][1]
    assert sent[0][2] == "urgent"
    assert st.credential_meta()["lifetimes_days"] == [pytest.approx(5.0, abs=0.1)]
    clock.t += 3600
    g.tick()
    assert len(sent) == 1  # 6 小时内不重复
    clock.t += 6 * 3600
    g.tick()
    assert len(sent) == 2 and "仍未更新" in sent[1][0]


def test_network_errors_do_not_count(env):
    g, st, store, sent, clock, result = env
    result["auth"] = "net"
    for _ in range(3):
        clock.t += 600
        g.tick()
    assert g.status.state != "expired" and g.status.consecutive_failures == 0 and sent == []


def test_monitor_failures_within_a_minute_count_once(env):
    g, *_ = env
    g.report_failure("x")
    g.report_failure("x")
    assert g.status.consecutive_failures == 1 and g.status.state != "expired"


def test_update_validates_before_saving_and_sends_recovery(env):
    g, st, store, sent, clock, result = env
    result["auth"] = "expired"
    g.tick(force=True)
    clock.t += 61
    g.tick(force=True)
    assert g.status.state == "expired"
    ok, msg, _ = g.update("web.2.NEW", "badcsrf", validator=lambda p, c, b: (False, "无效", 0))
    assert not ok and st.binance_credentials() == (P20T, CSRF)  # 未保存
    new = "web.1000000002.0123456789ABCDEF0123456789ABCDEF"
    ok, msg, rels = g.update(new, CSRF, validator=lambda p, c, b: (True, "有效", 3))
    assert ok and st.binance_credentials()[0] == new and g.client.p20t == new
    assert g.status.state == "valid" and not g.paused
    assert "已恢复" in sent[-1][0]
    types = [e["type"] for e in store.events(limit=20)]
    assert "credential.updated" in types and "credential.update_rejected" in types and "credential.expired" in types


def test_missing_credentials(tmp_path):
    st = Settings(str(tmp_path / "b.db"), str(tmp_path / "s.json"))
    g = CredentialGuard(Client(), st, Store(str(tmp_path / "b.db")), lambda *a: None)
    g.tick()
    assert g.status.state == "missing" and g.paused
