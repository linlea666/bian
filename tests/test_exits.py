import pytest

from bian import exits
from bian.config import AppConfig, LeaderConfig
from bian.lifecycle import EpisodeRecorder
from bian.models import Position
from bian.monitor import Monitor
from bian.store import Store
from bian.sync import upsert_fills, upsert_leader_history

T0 = 1_000_000.0  # 开仓时间（秒）


def pos(qty=1.0, entry=2000.0, mark=2100.0, side="SHORT", lev=20, symbol="ETHUSDT", source="copy"):
    d = 1 if side == "LONG" else -1
    margin = qty * entry / lev
    return Position("L", symbol, side, qty, entry, mark, lev, (mark - entry) * qty * d, source, initial_margin=margin)


def leader(pid="P1", opened=T0, closed=None, avg_close=0.0, status="All Closed", update=None, side="Short"):
    return {"positionId": pid, "symbol": "ETHUSDT", "side": side, "opened": int(opened * 1000),
            "closed": int(closed * 1000) if closed else None, "avgCost": 2000, "avgClosePrice": avg_close,
            "maxOpenInterest": 10, "closedVolume": 10 if status == "All Closed" else 3, "leverage": "20",
            "closingPnl": 0, "roi": "0", "status": status, "updateTime": int((update or closed or opened) * 1000)}


def close_fill(t, qty=1.0, price=2100.0, realized=-100.0, fee=-1.0, side="BUY", ps="SHORT"):
    return {"time": int(t * 1000), "symbol": "ETHUSDT", "side": side, "positionSide": ps, "price": price,
            "qty": qty, "quantity": qty * price, "fee": fee, "realizedProfit": realized}


def setup_exit(close_at=T0 + 3600, qty_after=None):
    store = Store(":memory:")
    rec = EpisodeRecorder(store)
    rec.observe("L", "测试", [pos()], now=T0)
    rec.observe("L", "测试", [pos(qty=qty_after)] if qty_after else [], now=close_at)
    return store, rec


def test_recorder_writes_exit_only_for_copy_positions():
    store, _ = setup_exit()
    assert store.db.execute("SELECT COUNT(*), MIN(exit_type) FROM exits").fetchone() == (1, "待判定")
    s2 = Store(":memory:")
    r2 = EpisodeRecorder(s2)
    r2.observe("L", "测试", [pos(source="public")], now=T0)
    r2.observe("L", "测试", [], now=T0 + 60)
    assert s2.db.execute("SELECT COUNT(*) FROM exits").fetchone()[0] == 0  # 领航员仓位不是我的钱


def test_early_exit_stop_saved_after_leader_closes_worse():
    store, _ = setup_exit()
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    upsert_leader_history(store, "L", [leader(status="Partially Closed", closed=None, update=T0 + 100)])
    (r,) = exits.resolve(store, 20, now=T0 + 3700) or [None]
    assert r is None
    ex = exits.exits_list(store)[0]
    assert ex["exit_type"] == "提前离场" and ex["status"] == "waiting" and ex["actual_u"] == pytest.approx(-101)
    # 领航员后来在 2200 平仓（空单亏得更多）
    upsert_leader_history(store, "L", [leader(closed=T0 + 9000, avg_close=2200)])
    (done,) = exits.resolve(store, 20, now=T0 + 9100)
    assert done["verdict"] == "止损挽回"
    assert done["hypo_u"] == pytest.approx((2200 - 2000) * -1 - 1)  # -201
    assert done["delta_u"] == pytest.approx(-101 - (-201))
    ev = store.events(type_prefix="exit.verdict")
    assert ev and "止损挽回 100.00U" in ev[0]["title"]


def test_early_exit_wasted_when_leader_recovers():
    store, _ = setup_exit()
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    upsert_leader_history(store, "L", [leader(closed=T0 + 9000, avg_close=1950)])  # 领航员扛回来盈利
    (done,) = exits.resolve(store, 20, now=T0 + 9100)
    assert done["exit_type"] == "提前离场" and done["verdict"] == "白止损"
    assert done["delta_u"] == pytest.approx(-101 - (50 - 1))


def test_follow_close():
    store, _ = setup_exit()
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    upsert_leader_history(store, "L", [leader(closed=T0 + 3585, avg_close=2100)])
    (done,) = exits.resolve(store, 20, now=T0 + 3700)
    assert done["exit_type"] == "跟随平仓" and done["verdict"] == "跟随" and done["delta_u"] is None


def test_lagging_close_when_leader_closed_long_before():
    store, _ = setup_exit()
    upsert_fills(store, "L", [close_fill(T0 + 3590)])  # 我在 2100 才平（亏 100 + 手续费 1）
    upsert_leader_history(store, "L", [leader(closed=T0 + 1000, avg_close=2080)])  # 领航员早就在 2080 平了
    (done,) = exits.resolve(store, 20, now=T0 + 3700)
    assert done["exit_type"] == "滞后平仓" and done["verdict"] == "比领航员多亏"


def test_partial_early_exit():
    store, _ = setup_exit(qty_after=0.4)
    upsert_fills(store, "L", [close_fill(T0 + 3590, qty=0.6, realized=-60, fee=-0.6)])
    upsert_leader_history(store, "L", [leader(status="Partially Closed", closed=None, update=T0 + 100)])
    exits.resolve(store, 20, now=T0 + 3700)
    ex = exits.exits_list(store)[0]
    assert ex["partial"] == 1 and ex["exit_type"] == "提前部分离场" and ex["qty"] == pytest.approx(0.6)


def test_missing_fills_wait_then_fallback_estimate():
    store, _ = setup_exit()
    upsert_leader_history(store, "L", [leader(closed=T0 + 9000, avg_close=2200)])
    assert exits.resolve(store, 20, now=T0 + 3700) == []  # 等成交同步
    (done,) = exits.resolve(store, 20, now=T0 + 3600 + 2 * 3600 + 10)
    assert done["actual_u"] == pytest.approx(0.05 * 100 * 0 + (-100) - 2100 * 0.0005, rel=0.2)


def test_unknown_after_30_days_without_leader_position():
    store, _ = setup_exit()
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    assert exits.resolve(store, 20, now=T0 + 4000) == []
    (done,) = exits.resolve(store, 20, now=T0 + 31 * 86400)
    assert done["verdict"] == "未知" and done["exit_type"] == "未知"


def test_remarks_total_stop_and_alert_response():
    store = Store(":memory:")
    rec = EpisodeRecorder(store)
    rec.observe("L", "测试", [pos(), pos(symbol="BTCUSDT", entry=70000)], now=T0)
    store.mark_todo_done("pos:L:ETHUSDT:SHORT:tier1")
    store.db.execute("UPDATE todos_done SET ts=?", (T0 + 3000,))
    rec.observe("L", "测试", [], now=T0 + 3600)
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    upsert_leader_history(store, "L", [leader(closed=T0 + 9000, avg_close=2200)])
    exits.resolve(store, 20, now=T0 + 9100)
    eth = [e for e in exits.exits_list(store) if e["symbol"] == "ETHUSDT"][0]
    assert "疑似总止损" in eth["remarks"] and "响应提醒" in eth["remarks"]


def test_manual_override_and_summary():
    store, _ = setup_exit()
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    upsert_leader_history(store, "L", [leader(closed=T0 + 9000, avg_close=1950)])
    exits.resolve(store, 20, now=T0 + 9100)
    s = exits.summary(store)
    assert s["wasted_n"] == 1 and s["net_u"] == pytest.approx(-150)
    ex_id = exits.exits_list(store)[0]["id"]
    exits.set_manual(store, ex_id, "跟随平仓")
    assert exits.summary(store)["follow"] == 1 and exits.summary(store)["wasted_n"] == 0
    with pytest.raises(ValueError):
        exits.set_manual(store, ex_id, "乱写")


def test_residual_detection_and_monitor_alert_once():
    store = Store(":memory:")
    rec = EpisodeRecorder(store)
    rec.observe("L", "测试", [pos()], now=T0)
    upsert_leader_history(store, "L", [leader(closed=T0 + 100, avg_close=2050)])
    res = exits.residuals(store, 20, now=T0 + 400)
    assert list(res) == ["L:ETHUSDT:SHORT"]
    store.set_runtime("residuals", res)

    class N:
        def send(self, text, severity="info"):
            return True, ""

    mon = Monitor(AppConfig(db_path=":memory:", leaders=[LeaderConfig("L", "测试", "copy")]),
                  type("C", (), {"has_credentials": False})(), N(), store, lambda: T0 + 400)
    lr = mon.leaders[0]
    lr.name, lr.source, lr.initialized = "测试", "copy", True
    seq = iter([[pos()], [pos()]])
    mon.fetch = lambda _lr: next(seq)
    assert "R12" in [a.rule_id for a in mon.poll_once()]
    assert "R12" not in [a.rule_id for a in mon.poll_once()]  # 同一回合只提醒一次


# ---------------------------------------------------------------- 领航员仍持仓 / 历史回补（M8）
from bian import sync  # noqa: E402
from bian.sync import ensure_leader, upsert_my_positions  # noqa: E402


def _synced(store, ts):
    store.db.execute("UPDATE leaders SET history_ok=? WHERE lead_id='L'", (ts,))
    store.db.commit()


def test_leader_still_holding_becomes_early_exit_after_successful_sync():
    store, _ = setup_exit()
    ensure_leader(store, "L", "测试", "copy")
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    exits.resolve(store, 20, now=T0 + 3700)
    assert exits.exits_list(store)[0]["status"] == "pending"  # 还没有离场之后的成功同步
    _synced(store, T0 + 3600 + 100)  # 离场后不到 5 分钟的同步不算
    exits.resolve(store, 20, now=T0 + 3800)
    assert exits.exits_list(store)[0]["status"] == "pending"
    _synced(store, T0 + 3600 + 400)
    exits.resolve(store, 20, now=T0 + 4100)
    ex = exits.exits_list(store)[0]
    assert (ex["exit_type"], ex["status"]) == ("提前离场", "waiting")
    assert exits.summary(store)["waiting"] == 1
    # 他后来在更差的价格平仓 → 结算为止损挽回
    upsert_leader_history(store, "L", [leader(closed=T0 + 9000, avg_close=2200)])
    (done,) = exits.resolve(store, 20, now=T0 + 9100)
    assert (done["exit_type"], done["verdict"]) == ("提前离场", "止损挽回")


def test_assumed_holding_is_corrected_to_follow_when_leader_record_appears():
    store, _ = setup_exit()
    ensure_leader(store, "L", "测试", "copy")
    upsert_fills(store, "L", [close_fill(T0 + 3590)])
    _synced(store, T0 + 4000)
    exits.resolve(store, 20, now=T0 + 4100)
    assert exits.exits_list(store)[0]["exit_type"] == "提前离场"
    # 他的平仓记录延迟出现，其实和我同时平的 → 改为跟随平仓
    upsert_leader_history(store, "L", [leader(closed=T0 + 3595, avg_close=2100)])
    (done,) = exits.resolve(store, 20, now=T0 + 4200)
    assert (done["exit_type"], done["verdict"]) == ("跟随平仓", "跟随")


def test_failed_leader_sync_does_not_mark_history_ok(monkeypatch):
    store = Store(":memory:")
    ensure_leader(store, "L", "测试", "copy")

    def boom(*a, **k):
        raise RuntimeError("网络错误")
        yield  # pragma: no cover

    monkeypatch.setattr(sync.leaders_api, "iter_position_history_pages", boom)
    with pytest.raises(RuntimeError):
        sync.sync_leader_history(None, store, "L")
    assert store.db.execute("SELECT history_ok FROM leaders").fetchone()[0] is None
    monkeypatch.setattr(sync.leaders_api, "iter_position_history_pages", lambda *a, **k: iter([[]]))
    sync.sync_leader_history(None, store, "L")
    assert store.db.execute("SELECT history_ok FROM leaders").fetchone()[0] > 0


def my_pos(pid="M1", opened=T0, closed=T0 + 3600, pnl=-45.12):
    return {"positionId": pid, "symbol": "ETHUSDT", "side": "Short", "opened": int(opened * 1000),
            "closed": int(closed * 1000), "avgCost": 2000, "avgClosePrice": 2100, "maxOpenInterest": 1,
            "closedVolume": 1, "leverage": "20", "closingPnl": pnl, "roi": "-1.0", "status": "All Closed",
            "updateTime": int(closed * 1000)}


def test_backfill_closes_missed_by_live_recorder():
    """第二期上线前 / 凭证失效期间手动止损的仓位：用历史仓位补出离场记录，之后照常判定。"""
    store = Store(":memory:")
    ensure_leader(store, "L", "测试", "copy")
    upsert_fills(store, "L", [close_fill(T0 + 3600, realized=-46.0, fee=-1.0)])
    upsert_my_positions(store, "L", [my_pos()])
    now = T0 + 7200
    assert exits.backfill(store, 20, now) == 1
    assert exits.backfill(store, 20, now) == 0  # 不重复补
    ex = exits.exits_list(store)[0]
    assert ex["actual_u"] == pytest.approx(-47.0) and ex["remarks"] == "历史回补" and ex["opened_ts"] == T0
    _synced(store, now)
    exits.resolve(store, 20, now)
    ex = exits.exits_list(store)[0]
    assert (ex["exit_type"], ex["status"]) == ("提前离场", "waiting") and "历史回补" in ex["remarks"]
    upsert_leader_history(store, "L", [leader(closed=T0 + 20000, avg_close=1950)])
    (done,) = exits.resolve(store, 20, T0 + 20100)
    assert done["verdict"] == "白止损" and done["delta_u"] < 0 and "历史回补" in done["remarks"]


def test_backfill_skips_positions_already_recorded_live_and_old_ones():
    store, _ = setup_exit()  # 实时记录器已经记了这次平仓
    upsert_my_positions(store, "L", [my_pos(), my_pos("OLD", opened=T0 - 40 * 86400, closed=T0 - 39 * 86400)])
    assert exits.backfill(store, 20, T0 + 7200) == 0


def test_backfill_without_fills_uses_position_pnl():
    store = Store(":memory:")
    upsert_my_positions(store, "L", [my_pos(pnl=-30.5)])
    assert exits.backfill(store, 20, T0 + 7200) == 1
    ex = exits.exits_list(store)[0]
    assert ex["actual_u"] == pytest.approx(-30.5) and ex["leader_name"] == "L"
