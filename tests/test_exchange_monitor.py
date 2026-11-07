"""交易所账户接入监控：持仓走完整规则链路、止损单检查 R14、账户级 A01/A05/A06、划转不算回撤、增删账户。"""
import pytest

from bian.config import AppConfig
from bian.exchanges import AccountInfo, ExchangeError, StopOrder
from bian.lifecycle import EpisodeRecorder
from bian.models import Position
from bian.monitor import Monitor
from bian.store import Store
from tests.test_monitor import Clock, FakeClient, FakeNotifier

AID = "xabc123"


class FakeAdapter:
    def __init__(self):
        self.pos, self.stops, self.info, self.fail = [], [], AccountInfo(1000, 800, 0, 50, 0.05), None
        self.stop_calls = 0

    def positions(self):
        if self.fail:
            raise self.fail
        return [Position(AID, s, side, q, e, m, 10, (m - e) * q * (1 if side == "LONG" else -1), "exchange",
                         initial_margin=q * e / 10) for s, side, q, e, m in self.pos]

    def stop_orders(self):
        self.stop_calls += 1
        return list(self.stops)

    def account(self):
        return self.info


def make(store=None, accounts=None):
    store = store or Store(":memory:")
    ad = FakeAdapter()
    acc = {"id": AID, "exchange": "binance", "name": "主账户", "stop_check": 1, "key_hint": "k1",
           "baseline_equity": None, "baseline_ts": None}
    store.db.execute("INSERT INTO exchange_accounts (id, exchange, name, enabled, removed, stop_check, created) "
                     "VALUES (?, 'binance', '主账户', 1, 0, 1, 0)", (AID,))
    rows = accounts if accounts is not None else [acc]
    clock = Clock()
    mon = Monitor(AppConfig(stale_seconds=60), FakeClient(), FakeNotifier(), store, clock,
                  recorder=EpisodeRecorder(store, clock), exchange_provider=lambda: [(a, lambda: ad) for a in rows])
    return mon, ad, clock, store, rows


def ids(alerts):
    return [a.rule_id for a in alerts]


def test_exchange_positions_go_through_rules_with_real_money():
    mon, ad, clock, store, _ = make()
    ad.pos = [("BTCUSDT", "LONG", 0.1, 60000, 60000)]
    mon.poll_once()  # 启动：已有仓位并入汇总
    assert mon.leaders[0].source == "exchange" and mon.leaders[0].name == "币安 · 主账户"
    clock.t += 20
    ad.pos = [("BTCUSDT", "LONG", 0.1, 60000, 56000)]  # ROI -66.7%，-400U
    sent = mon.poll_once()
    (l1,) = [a for a in sent if a.rule_id == "L1"]
    ep = store.db.execute("SELECT source, last_u FROM episodes").fetchone()
    assert ep == ("exchange", pytest.approx(-400))  # 回合里有 U 金额（是我的钱）
    assert store.db.execute("SELECT COUNT(*) FROM exits").fetchone()[0] == 0  # 离场判定只用于跟单


def test_web_credentials_paused_does_not_stop_exchange_accounts():
    mon, ad, clock, _, _ = make()

    class Guard:
        paused = True
    mon.guard = Guard()
    ad.pos = [("ETHUSDT", "SHORT", 1, 3000, 3000)]
    mon.poll_once()
    assert mon.leaders[0].last_ok == clock.t


def test_no_stop_alert_after_grace_and_once_per_version():
    mon, ad, clock, _, _ = make()
    ad.pos = [("BTCUSDT", "LONG", 0.1, 60000, 60000)]
    mon.poll_once()
    clock.t += 60
    assert "R14" not in ids(mon.poll_once())  # 开仓不到 2 分钟：留时间挂单
    clock.t += 70
    assert "R14" in ids(mon.poll_once())
    clock.t += 30
    assert "R14" not in ids(mon.poll_once())  # 同一版本只提醒一次
    ad.stops = [StopOrder("BTCUSDT", "LONG", 58000, 0.1, "stop")]
    clock.t += 61
    mon.poll_once()
    assert mon.leaders[0].positions[f"{AID}:BTCUSDT:LONG"].stop_price == 58000
    ad.pos = [("BTCUSDT", "LONG", 0.2, 60000, 60000)]  # 加仓：止损单只覆盖一半
    clock.t += 20
    mon.poll_once()
    clock.t += 130
    sent = mon.poll_once()
    assert [a.title for a in sent if a.rule_id == "R14"] == ["止损单只覆盖了 50% 的仓位"]


def test_stop_orders_queried_once_a_minute_and_can_be_disabled():
    mon, ad, clock, _, rows = make()
    ad.pos = [("BTCUSDT", "LONG", 0.1, 60000, 60000)]
    for _ in range(3):
        mon.poll_once()
        clock.t += 20
    assert ad.stop_calls == 1
    rows[0]["stop_check"] = 0
    clock.t += 200
    sent = mon.poll_once()
    assert mon.leaders[0].positions[f"{AID}:BTCUSDT:LONG"].stop_price is None and "R14" not in ids(sent)


def test_account_rules_margin_ratio_and_transfers_not_drawdown():
    mon, ad, clock, store, rows = make()
    ad.info = AccountInfo(1000, 800, 0, 50, 0.05)
    mon.poll_once()
    assert rows[0]["baseline_equity"] == 1000
    view = {a["lead_id"]: a for a in store.get_runtime("accounts", [])[0]}[AID]
    assert view["kind"] == "exchange" and view["invested"] == 1000 and view["available"] == 800
    # 转出 300U：权益变成 700，不是回撤
    store.db.execute("INSERT INTO account_ledger VALUES ('t1', ?, ?, 'transfer', '', -300)", (AID, int((clock.t + 1) * 1000)))
    clock.t += 20
    ad.info = AccountInfo(700, 500, 0, 50, 0.07)
    sent = mon.poll_once()
    assert not [a for a in sent if a.rule_id.startswith("A01")]
    clock.t += 20
    ad.info = AccountInfo(700, 100, 0, 420, 0.6)  # 维持保证金率 60%
    sent = mon.poll_once()
    assert [a.rule_id for a in sent if a.rule_id.startswith("A06")] == ["A06"]
    clock.t += 20
    ad.info = AccountInfo(700, 50, 0, 600, 0.85)  # 进入紧急线
    sent = mon.poll_once()
    assert [a.rule_id for a in sent if a.rule_id.startswith("A06")] == ["A06b"]
    assert not [a for a in sent if a.rule_id in ("A02", "A03", "A04")]  # 跟单专属规则不适用


def test_errors_recorded_and_account_removal_closes_episodes():
    mon, ad, clock, store, rows = make()
    ad.pos = [("BTCUSDT", "LONG", 0.1, 60000, 60000)]
    mon.poll_once()
    ad.fail = ExchangeError("ip", "-2015")
    clock.t += 20
    mon.poll_once()
    assert "白名单" in mon.leaders[0].last_error
    rows.clear()  # 账户被停用 / 移除
    clock.t += 20
    mon.poll_once()
    assert not [x for x in mon.leaders if x.source == "exchange"]
    assert store.db.execute("SELECT status, exit_type FROM episodes").fetchone() == ("closed", "停止监控")


def test_setup_rerun_after_credentials_recover_keeps_exchange_source():
    mon, ad, clock, _, _ = make()
    ad.pos = [("BTCUSDT", "LONG", 0.1, 60000, 60000)]
    mon.poll_once()
    mon.setup()  # 网页凭证恢复后会重跑 setup
    lr = [x for x in mon.leaders if x.cfg.portfolio_id == AID][0]
    assert lr.source == "exchange" and not lr.blocked_reason


def test_first_account_read_failure_does_not_break_dashboard(tmp_path):
    from bian.app import App
    from bian.web import views
    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    store = Store(app.db_path)
    mon, ad, clock, _, _ = make(store)

    def boom():
        raise ExchangeError("auth", "-2014")
    ad.account = boom
    mon.poll_once()
    assert AID not in mon.exchange_view
    assert "API Key" in store.db.execute("SELECT last_error FROM exchange_accounts").fetchone()[0]
    ov = views.overview(store, app.settings)  # 不能因为缺权益字段而出错
    assert ov["totals"]["exchange_equity"] == 0
