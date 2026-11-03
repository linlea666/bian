from bian.client import BinanceError
from bian.config import AppConfig, LeaderConfig
from bian.models import Position
from bian.monitor import Monitor
from bian.store import Store


class FakeNotifier:
    name = "fake"

    def __init__(self, ok=True):
        self.msgs, self.ok = [], ok

    def send(self, text, severity="info"):
        self.msgs.append((severity, text))
        return self.ok, "" if self.ok else "boom"


class FakeClient:
    has_credentials = False
    position_show = True

    def get(self, path, params=None, private=False):  # 仅用于空快照时复查 positionShow
        return {"positionShow": self.position_show}


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def make(snapshots, notifier=None, db=":memory:", budget=0.0):
    cfg = AppConfig(stale_seconds=60, empty_confirm_polls=2, db_path=db, loss_budget_u=budget,
                    leaders=[LeaderConfig("L", "测试领航员", "public", position_margin_u=10)])
    clock = Clock()
    mon = Monitor(cfg, FakeClient(), notifier or FakeNotifier(), Store(db), clock)
    lr = mon.leaders[0]
    lr.name, lr.source = "测试领航员", "public"
    it = iter(snapshots)

    def fetch(_lr):
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v

    mon.fetch = fetch
    return mon, clock


def p(roi, qty=1.0):
    mark = 100 * (1 + roi / 10)
    return Position("L", "ETHUSDT", "LONG", qty, 100.0, mark, 10, (mark - 100) * qty, "public")


def rules(alerts):
    return [a.rule_id for a in alerts]


def test_open_alert_and_close_after_confirmed_empty():
    mon, clock = make([[], [p(0.0)], [], [], []])
    mon.poll_once()  # 启动时空仓
    clock.t += 20
    assert rules(mon.poll_once()) == ["R00"]
    clock.t += 20
    assert rules(mon.poll_once()) == []  # 第一次空快照：等待确认
    clock.t += 20
    assert rules(mon.poll_once()) == ["R11"]  # 二次确认后才判定平仓
    assert not mon.states


def test_ac06_stale_data_alert_and_recovery():
    mon, clock = make([[p(0.0)], BinanceError("x"), BinanceError("x"), [p(0.0)]])
    mon.poll_once()
    clock.t += 30
    assert rules(mon.poll_once()) == []
    clock.t += 40  # 距上次成功 70 秒 > 60
    out = mon.poll_once()
    assert rules(out) == ["R08"] and out[0].severity == "warn"
    clock.t += 10
    assert rules(mon.poll_once()) == ["R08"]  # 数据恢复提醒


def test_restart_does_not_resend(tmp_path):
    db = str(tmp_path / "t.db")
    mon, _ = make([[p(-0.6)]], db=db)
    assert rules(mon.poll_once()) == ["L1", "R00"]  # 亏损通知单发 + 启动汇总
    n = FakeNotifier()
    mon2, _ = make([[p(-0.61)]], notifier=n, db=db)
    assert rules(mon2.poll_once()) == []


def test_message_content_and_failed_delivery_recorded():
    n = FakeNotifier(ok=False)
    mon, _ = make([[p(-1.2)]], notifier=n)
    mon.poll_once()
    sev, text = n.msgs[0]  # 紧急提醒先发，启动汇总在后
    assert sev == "urgent" and "ETHUSDT 多 10x" in text and "lead-details/L" in text
    assert "不会自动下单" in text and "已止损" not in text
    rows = mon.store.recent_alerts()
    assert rows and all(r[5] == 0 and r[6] == "boom" for r in rows)


def test_portfolio_budget_uses_position_margin_estimate():
    mon, _ = make([[p(-0.5)]], budget=4)
    out = mon.poll_once()
    assert "R06" in rules(out)  # -50% × 10U = -5U ≤ -4U


def test_public_leader_hiding_positions_is_not_a_close():
    mon, clock = make([[p(0.0)], [], []])
    mon.client.position_show = False
    mon.poll_once()
    clock.t += 20
    mon.poll_once()
    clock.t += 20
    out = mon.poll_once()
    assert rules(out) == ["R08"] and mon.leaders[0].blocked_reason
    assert mon.states  # 状态保留，不误报平仓


def test_startup_summary_instead_of_flood():
    n = FakeNotifier()
    mon, clock = make([[p(0.3), p(0.1, qty=2).__class__("L", "BTCUSDT", "SHORT", 1, 100, 99, 10, 1, "public")],
                       [p(0.3), p(0.0).__class__("L", "BTCUSDT", "SHORT", 1, 100, 99, 10, 1, "public")]], notifier=n)
    out = mon.poll_once()
    assert rules(out) == ["R00"]  # 只有一条汇总
    assert "2 个持仓" in n.msgs[0][1] and "建议止损上移" in n.msgs[0][1]
    clock.t += 20
    assert rules(mon.poll_once()) == []  # 第二轮不再重复 R04


class CredClient:
    """模拟带凭证的客户端：detail-list 返回一个跟单关系。"""

    has_credentials = True

    def __init__(self, rel):
        self.rel = rel

    def get(self, path, params=None, private=False):
        if path.endswith("/copy-portfolio/detail-list"):
            return [self.rel]
        return {"nickname": "X", "positionShow": False}


def test_auto_discover_and_account_rule():
    rel = {"leadPortfolioId": "L", "copyPortfolioId": "C", "nickname": "自动发现", "marginBalance": "300",
           "netCopyAmount": "300", "totalStopLossUsdt": None, "totalStopLossAmount": "0", "isPaused": False}
    cfg = AppConfig(db_path=":memory:")
    n = FakeNotifier()
    mon = Monitor(cfg, CredClient(rel), n, Store(":memory:"), Clock())
    mon.setup()
    assert [(lr.cfg.portfolio_id, lr.source, lr.copy_portfolio_id) for lr in mon.leaders] == [("L", "copy", "C")]
    mon.fetch = lambda lr: []
    out = mon.poll_once()
    assert "A02" in rules(out) and "总止损" in n.msgs[-1][1]


def leg(side, qty=1.0, pnl=0.0):
    return Position("L", "ETHUSDT", side, qty, 100.0, 100.0, 10, pnl, "copy", initial_margin=10)


def test_hedge_transition_is_not_open_or_close():
    mon, clock = make([[leg("LONG")], [leg("LONG"), leg("SHORT")], [leg("LONG")]])
    mon.leaders[0].initialized = True
    assert rules(mon.poll_once()) == ["R00"]  # 新开多单
    clock.t += 20
    assert rules(mon.poll_once()) == []  # 变成对冲：不报新开仓/平仓
    assert "L:ETHUSDT:HEDGE" in mon.states
    clock.t += 20
    assert rules(mon.poll_once()) == []


def test_restart_after_downtime_with_new_entry_is_new_position(tmp_path):
    db = str(tmp_path / "t.db")
    mon, clock = make([[p(-0.6)]], db=db)
    mon.poll_once()  # 旧仓位已发 L1
    # 停机 1 小时，期间平仓后同币种重新开仓（开仓价不同）
    newpos = Position("L", "ETHUSDT", "LONG", 1.0, 90.0, 90 * (1 - 0.06), 10, -5.4, "public")
    mon2, clock2 = make([[newpos]], db=db)
    clock2.t = clock.t + 3600
    assert rules(mon2.poll_once()) == ["L1", "R00"]  # 新仓位重新通知


def test_flush_records_delivery_result():
    mon, _ = make([[p(-1.2)]])
    mon.poll_once()
    rows = mon.store.db.execute("SELECT rule_id, delivered, error FROM alerts").fetchall()
    assert rows and all(r[1] == 1 and r[2] == "" for r in rows)
