"""关注币安聪明钱（公开实盘）：主页 ID 解析、持仓翻页、只提醒交易动态、关注 / 取消关注、邮件。"""
import pytest

from bian.api import smart_money
from bian.client import BinanceError
from bian.config import AppConfig
from bian.lifecycle import EpisodeRecorder
from bian.monitor import Monitor
from bian.notify import EmailNotifier, SmtpConfig
from bian.settings import Settings
from bian.smart import SmartError, follow, list_traders, unfollow
from bian.store import Store
from tests.test_monitor import Clock, FakeNotifier

TID = "4555302160171872539"


def test_normalize_id():
    assert smart_money.normalize_id(f"https://www.binance.com/zh-CN/smart-money/profile/{TID}") == TID
    assert smart_money.normalize_id(f"www.binance.com/en/smart-money/profile/{TID}?ref=x") == TID
    assert smart_money.normalize_id(f" {TID} ") == TID
    for bad in ("123", "https://evil.com/smart-money/profile/" + TID, "https://www.binance.com/zh-CN/copy-trading", ""):
        with pytest.raises(ValueError):
            smart_money.normalize_id(bad)


class PagedClient:
    has_credentials = True

    def __init__(self, rows, total=True, profile=None):
        self.rows, self.total, self.calls, self.prof = rows, total, [], profile or {}

    def get(self, path, params=None, private=False):
        self.calls.append((path, dict(params or {}), private))
        if path == smart_money.PROFILE:
            return {"topTraderId": TID, "traderName": "万倍幻想家", "sharingPosition": True, **self.prof}
        if path != smart_money.POSITIONS:
            return []  # 跟单关系列表等：没有跟单
        page = params["page"]
        chunk = self.rows[(page - 1) * 9: page * 9]
        return {"total": len(self.rows), "list": chunk} if self.total else chunk


def row(sym, amount, entry=100.0, mark=100.0, lev=10):
    return {"symbol": sym, "amount": amount, "entryPrice": entry, "markPrice": mark, "leverage": lev,
            "margin": abs(amount) * entry / lev, "pnl": (mark - entry) * amount}


def test_positions_paging_and_sides():
    rows = [row(f"C{i}USDT", (-1) ** i * (i + 1)) for i in range(12)]
    c = PagedClient(rows)
    ps = smart_money.positions(c, TID)
    assert len(ps) == 12 and [x[2] for x in c.calls] == [True, True]  # 私有接口、翻了 2 页
    assert ps[0].side == "LONG" and ps[1].side == "SHORT" and ps[1].qty == 2 and ps[0].source == "smart"
    with pytest.raises(BinanceError):  # 方向字段和数量正负矛盾：宁可本轮跳过
        smart_money.positions(PagedClient([{**row("ETHUSDT", 1), "side": "SHORT"}]), TID)


def make(notifier=None, la=None):
    store = Store(":memory:")
    clock = Clock()
    rec = EpisodeRecorder(store, clock)
    mon = Monitor(AppConfig(stale_seconds=60, empty_confirm_polls=2, db_path=":memory:"), PagedClient([]),
                  notifier or FakeNotifier(), store, clock, recorder=rec, smart_provider=lambda: [(TID, "万倍幻想家")])
    state = {"rows": []}
    mon.client.rows = state["rows"]
    return mon, clock, state, store


def poll(mon, clock, rows):
    mon.client.rows = rows
    clock.t += 20
    return [(a.rule_id, a.severity, a.title) for a in mon.poll_once()]


def test_smart_only_trade_actions_no_risk_no_episodes():
    mon, clock, _, store = make()
    assert poll(mon, clock, []) == []
    assert [r for r, _, _ in poll(mon, clock, [row("ETHUSDT", 1)])] == ["R00"]
    # 深度亏损：亏损档位 / 强平等风险提醒不发
    assert poll(mon, clock, [row("ETHUSDT", 1, mark=80)]) == []
    # 亏损中加仓：按普通加仓提醒（info），不是风险提醒
    out = poll(mon, clock, [row("ETHUSDT", 2, entry=90, mark=80)])
    assert [(r, s) for r, s, _ in out] == [("R07", "info")]
    assert [r for r, _, _ in poll(mon, clock, [row("ETHUSDT", 1, entry=90, mark=80)])] == ["R07b"]
    # 开空形成对冲、平空、全平（空列表二次确认）
    assert [r for r, _, _ in poll(mon, clock, [row("ETHUSDT", 1), row("ETHUSDT", -1)])] == ["R00"]
    assert [r for r, _, _ in poll(mon, clock, [row("ETHUSDT", 1)])] == ["R11"]
    assert poll(mon, clock, []) == []
    assert [r for r, _, _ in poll(mon, clock, [])] == ["R11"]
    assert store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0  # 不记仓位回合
    runtime = store.get_runtime("poll", {})[0]
    assert runtime["leaders"][0]["source"] == "smart"


def test_smart_sharing_off_does_not_fake_close():
    mon, clock, _, _ = make()
    poll(mon, clock, [])
    poll(mon, clock, [row("BTCUSDT", 1)])
    mon.client.prof = {"sharingPosition": False}
    assert poll(mon, clock, []) == []
    out = poll(mon, clock, [])
    assert [r for r, _, _ in out] == ["R08"] and "关闭了持仓分享" in out[0][2]  # 不当成平仓
    assert poll(mon, clock, []) == []  # 只提醒一次
    mon.client.prof = {}
    assert poll(mon, clock, [row("BTCUSDT", 1)]) == []  # 重新公开：恢复，仓位没变不重复提醒


def test_smart_skipped_without_credentials_and_unfollow_clears_state():
    traders = [(TID, "万倍幻想家")]
    store = Store(":memory:")
    clock = Clock()
    mon = Monitor(AppConfig(db_path=":memory:"), PagedClient([row("ETHUSDT", 1)]), FakeNotifier(), store, clock,
                  smart_provider=lambda: list(traders))
    mon.client.has_credentials = False
    mon.poll_once()
    assert not any(k.startswith(TID) for k in mon.states)
    assert not [c for c in mon.client.calls if c[0] == smart_money.POSITIONS]
    mon.client.has_credentials = True
    clock.t += 20
    mon.poll_once()
    assert any(k.startswith(TID) for k in mon.states)
    traders.clear()
    clock.t += 20
    mon.poll_once()
    assert not any(k.startswith(TID) for k in mon.states) and not [x for x in mon.leaders if x.source == "smart"]


def test_follow_unfollow_sets_email_switch(tmp_path):
    st = Settings(str(tmp_path / "b.db"), str(tmp_path / "s.json"))
    store = Store(str(tmp_path / "b.db"))
    c = PagedClient([])
    out = follow(store, st, c, f"https://www.binance.com/zh-CN/smart-money/profile/{TID}")
    assert out == {"id": TID, "name": "万倍幻想家", "email": True}
    assert st.get()["notify"]["leader_actions"] == {TID: True}
    assert [t["id"] for t in list_traders(store)] == [TID]
    with pytest.raises(SmartError):
        follow(store, st, c, TID)  # 重复关注
    unfollow(store, st, TID)
    assert st.get()["notify"]["leader_actions"] == {} and list_traders(store) == []
    with pytest.raises(SmartError):  # 没公开持仓的不能关注
        follow(store, st, PagedClient([], profile={"sharingPosition": False}), TID)
    follow(store, st, c, TID, email=False)
    assert st.get()["notify"]["leader_actions"] == {}


def test_smart_open_emails_immediately_with_link():
    mails = []
    em = EmailNotifier(SmtpConfig("h", 465, "u", "p", ["t"], "tls", "u"), events=[], batch_minutes=60,
                       sender=lambda cfg, subj, body: mails.append((subj, body)), leader_actions={TID: True})
    mon, clock, _, _ = make(notifier=em)
    poll(mon, clock, [])
    poll(mon, clock, [row("SOLUSDT", -3)])
    assert len(mails) == 1 and "[万倍幻想家] SOLUSDT 空" in mails[0][0]
    assert "smart-money/profile/" + TID in mails[0][1] and "他的仓位" in mails[0][1]
