import os
import time

import pytest

from bian import stats
from bian.app import App
from bian.config import AppConfig
from bian.store import Store
from bian.sync import ensure_leader, upsert_fills, upsert_my_positions


@pytest.fixture(autouse=True)
def shanghai(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def ts(s):  # '2026-11-03 23:59' 本地时间 → 时间戳
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))


def f(t, realized, fee=-0.5, lead="L", sym="ETHUSDT"):
    return {"time": int(t * 1000), "symbol": sym, "side": "SELL", "positionSide": "LONG", "price": 1, "qty": 1,
            "quantity": 1, "fee": fee, "realizedProfit": realized}


def test_daily_realized_day_boundary_and_calendar():
    store = Store(":memory:")
    upsert_fills(store, "L", [f(ts("2026-11-02 23:59"), 10), f(ts("2026-11-03 00:01"), -4), f(ts("2026-11-03 10:00"), 0, fee=-0.2)])
    d = stats.daily_realized(store, ts("2026-11-01 00:00"), ts("2026-11-04 00:00"))
    assert d["2026-11-02"]["net"] == pytest.approx(9.5)
    assert d["2026-11-03"]["net"] == pytest.approx(-4.7) and d["2026-11-03"]["trades"] == 1  # 开仓手续费不算一笔平仓
    cal = stats.calendar_month(store, 2026, 11)
    assert len(cal["days"]) == 30 and cal["days"][1]["net"] == pytest.approx(9.5) and cal["days"][0]["net"] is None
    assert cal["win_days"] == 1 and cal["loss_days"] == 1 and cal["first_weekday"] == 6  # 2026-11-01 是周日


def test_equity_change_ignores_deposits():
    store = Store(":memory:")
    store.add_equity(ts("2026-11-02 20:00"), "L", 300, 300, 0, 0)
    store.add_equity(ts("2026-11-03 09:00"), "L", 500, 500, 0, 0)  # 追加 200U
    store.add_equity(ts("2026-11-03 20:00"), "L", 490, 500, 0, 0)  # 实际亏 10
    store.add_equity(ts("2026-11-03 21:00"), "M", 100, 100, 0, 0)  # 新跟单当天出现：不算变化
    ch = stats.daily_equity_change(store, ts("2026-11-03 00:00"), ts("2026-11-04 00:00"))
    assert ch == {"2026-11-03": pytest.approx(-10)}


def test_profit_shared_and_composition():
    store = Store(":memory:")
    store.add_equity(ts("2026-10-20 10:00"), "L", 300, 300, 0, 0, profit_shared=1.0)
    store.add_equity(ts("2026-11-02 10:00"), "L", 300, 300, 0, 0, profit_shared=3.0)
    assert stats._profit_shared_change(store, 0) == pytest.approx(3.0)
    assert stats._profit_shared_change(store, ts("2026-11-01 00:00")) == pytest.approx(2.0)


def test_losses_streak_and_ranking():
    store = Store(":memory:")
    ensure_leader(store, "L", "甲", "copy")
    rows = [{"positionId": str(i), "symbol": "ETHUSDT", "side": "Long", "opened": 1, "closed": 2, "avgCost": 1,
             "avgClosePrice": 1, "maxOpenInterest": 1, "closedVolume": 1, "leverage": "10", "closingPnl": p,
             "roi": "0", "status": "All Closed"} for i, p in enumerate([-60, -3, 5, 8])]
    upsert_my_positions(store, "L", rows)
    upsert_fills(store, "L", [f(ts("2026-11-01 10:00"), -1), f(ts("2026-11-02 10:00"), -1), f(ts("2026-11-04 10:00"), -1)])
    lo = stats.losses(store)
    assert [w["pnl"] for w in lo["worst"]] == [-60, -3] and lo["worst"][0]["leader_name"] == "甲"
    assert lo["max_loss_streak_days"] == 2 and sum(b["count"] for b in lo["distribution"]) == 4
    (r,) = stats.ranking(store, now=ts("2026-11-05 10:00"))
    assert r["closed"] == 4 and r["win_rate"] == 0.5 and r["payoff"] == pytest.approx(6.5 / 31.5)
    assert r["month_net"] == pytest.approx(-4.5)


def test_maintenance_once_per_day_and_digest(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("bian.app.smtp_send", lambda cfg, subj, body: sent.append((subj, body)))
    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    app.settings.update({"notify": {"smtp_user": "a@163.com"}, "maintenance": {"digest_time": "08:30", "backup_time": "04:00"}})
    app.settings.set_secrets(smtp_pass="x")
    store = Store(app.db_path)
    ensure_leader(store, "L", "甲", "copy")
    upsert_fills(store, "L", [f(ts("2026-11-02 15:00"), 12)])
    assert app.maintenance(store, ts("2026-11-03 03:00")) == []
    assert app.maintenance(store, ts("2026-11-03 05:00")) == ["backup"]
    assert app.maintenance(store, ts("2026-11-03 09:00")) == ["digest"]
    assert app.maintenance(store, ts("2026-11-03 22:00")) == []  # 当天不重复
    subj, body = sent[0]
    assert "11-02" in subj and "+11.50U" in subj and "甲：+11.50U" in body and "凭证" in body
    assert os.listdir(os.path.join(str(tmp_path), "backups"))


def test_maintenance_backup_failure_does_not_block_digest(tmp_path, monkeypatch):
    """备份失败（如磁盘满）：记一条警告事件，当天不反复重试，也不耽误摘要邮件。"""
    sent = []
    monkeypatch.setattr("bian.app.smtp_send", lambda cfg, subj, body: sent.append(subj))

    def boom(*a, **k):
        raise OSError("No space left on device")

    monkeypatch.setattr("bian.backup.backup", boom)
    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    app.settings.update({"notify": {"smtp_user": "a@163.com"}, "maintenance": {"digest_time": "08:30", "backup_time": "04:00"}})
    app.settings.set_secrets(smtp_pass="x")
    store = Store(app.db_path)
    assert app.maintenance(store, ts("2026-11-03 09:00")) == ["backup", "digest"]
    assert app.maintenance(store, ts("2026-11-03 09:01")) == []
    assert len(sent) == 1
    ev = store.db.execute("SELECT type, level FROM events WHERE type='maintenance.backup_failed'").fetchall()
    assert ev == [("maintenance.backup_failed", "warn")]


def test_digest_mentions_recorded_only_alerts(tmp_path):
    from bian import digest
    from bian.notify import SKIPPED
    from bian.rules import Alert

    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    store = Store(app.db_path)
    y = ts("2026-11-02 12:00")
    for rule in ("R05", "R05", "R07", "L1"):
        rid = store.add_alert(Alert(rule, "warn", "k", 1, "t", ts=y), "m", False, "待发送")
        store.update_delivery(rid, rule == "L1", "" if rule == "L1" else "email: " + SKIPPED)
    _, body = digest.build(store, app.settings, ts("2026-11-03 09:00"))
    assert "浮盈回撤 2 条" in body and "领航员加仓 1 条" in body and "仓位亏损" not in body.split("没有发邮件")[1][:40]


def test_restore_held_alerts_after_restart(tmp_path):
    from bian.notify import EmailNotifier, Outgoing
    from bian.rules import Alert

    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    app.settings.update({"notify": {"smtp_user": "a@163.com"}})
    app.settings.set_secrets(smtp_pass="x")
    store = Store(app.db_path)
    now = app.clock()
    fresh = store.add_alert(Alert("L1", "warn", "k", 1, "t", ts=now - 600), "新", False, "email: 等待合并，约 10:00 汇总发送")
    old = store.add_alert(Alert("L1", "warn", "k", 1, "t", ts=now - 7 * 3600), "旧", False, "email: 等待合并，约 01:00 汇总发送")
    app.notifier()
    assert app.restore_held(store) == 1
    em = app._email()
    assert isinstance(em, EmailNotifier) and [it.row_id for it in em.pending] == [fresh]
    assert "过期" in store.db.execute("SELECT error FROM alerts WHERE id=?", (old,)).fetchone()[0]
    # 改设置重建通知器：等待中的不丢
    app.settings.update({"notify": {"email_batch_minutes": 30}})
    app.notifier()
    assert [it.row_id for it in app._email().pending] == [fresh]
    assert Outgoing  # noqa: B018
