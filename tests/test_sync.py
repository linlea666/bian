import os
import stat

import pytest

from bian import sync
from bian.backup import backup
from bian.settings import Settings, SettingsError
from bian.store import Store


def fill(t, qty=1.0, realized=0.0, fee=-0.1, symbol="ETHUSDT", side="BUY", ps="LONG", price=100.0):
    return {"time": t, "symbol": symbol, "side": side, "positionSide": ps, "price": price, "qty": qty,
            "quantity": qty * price, "fee": fee, "realizedProfit": realized}


def test_fills_dedupe_and_incremental_stop(monkeypatch):
    store = Store(":memory:")
    pages = [[fill(5000), fill(4000)], [fill(3000), fill(2000)], [fill(1000)]]
    calls = []

    def fake_iter(client, path, cpid, size, max_pages):
        for p in pages:
            calls.append(path)
            yield p

    monkeypatch.setattr(sync.copy_account, "iter_pages", fake_iter)
    out = sync.sync_copy_relation(None, store, {"leadPortfolioId": "L", "copyPortfolioId": "C"})
    assert out["fills"] == 5
    pages.insert(0, [fill(6000), fill(5000)])  # 新成交在最前
    calls.clear()
    out = sync.sync_copy_relation(None, store, {"leadPortfolioId": "L", "copyPortfolioId": "C"})
    assert out["fills"] == 1
    assert calls.count("/copy-portfolio/trade-history") == 2  # 第 2 页整页已有 → 不再往后翻
    assert store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 6


def pos(pid, status="All Closed", closed=2000, closed_qty=1.0):
    return {"positionId": pid, "symbol": "ETHUSDT", "side": "Short", "opened": 1000, "closed": closed,
            "avgCost": 100, "avgClosePrice": 90, "maxOpenInterest": 1, "closedVolume": closed_qty,
            "leverage": "20", "closingPnl": 10, "roi": "2.0", "status": status}


def test_positions_partial_close_updates_and_mae_reset():
    store = Store(":memory:")
    assert sync.upsert_my_positions(store, "L", [pos("1", "Partially Closed", None, 0.4)]) == 1
    assert sync.upsert_my_positions(store, "L", [pos("1", "Partially Closed", None, 0.4)]) == 0  # 没变化
    assert sync.upsert_my_positions(store, "L", [pos("1")]) == 1  # 继续平完
    row = store.db.execute("SELECT status, closed_qty, side FROM my_positions").fetchone()
    assert row == ("All Closed", 1.0, "SHORT")
    sync.upsert_leader_history(store, "L", [pos("9")])
    store.db.execute("UPDATE leader_history SET mae=-0.5")
    assert sync.upsert_leader_history(store, "L", [pos("9")]) == 0
    assert store.db.execute("SELECT mae FROM leader_history").fetchone()[0] == -0.5  # 没变化时保留已算的 MAE


def test_ensure_leader_kind_upgrade():
    store = Store(":memory:")
    assert sync.ensure_leader(store, "L", "甲", "watch") is True
    assert sync.ensure_leader(store, "L", "甲", "copy") is False
    assert store.db.execute("SELECT kind FROM leaders").fetchone()[0] == "copy"
    sync.ensure_leader(store, "L", "甲", "watch")
    assert store.db.execute("SELECT kind FROM leaders").fetchone()[0] == "copy"  # 观察不会覆盖跟单中


def test_backup_keeps_latest_and_permissions(tmp_path, monkeypatch):
    db = str(tmp_path / "b.db")
    Store(db).log_event("x", "测试")
    (tmp_path / "secrets.json").write_text("{}")
    stamps = iter(f"20260101-0000{i:02d}" for i in range(10))
    monkeypatch.setattr("bian.backup.time.strftime", lambda fmt: next(stamps))
    for _ in range(9):
        path = backup(db, str(tmp_path / "backups"), str(tmp_path / "secrets.json"), keep=7)
    files = sorted(os.listdir(tmp_path / "backups"))
    assert len([f for f in files if f.startswith("bian-")]) == 7 and len([f for f in files if f.startswith("secrets-")]) == 7
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert Store(path).events(limit=1)[0]["title"] == "测试"


def test_maintenance_settings_validation(tmp_path):
    st = Settings(str(tmp_path / "b.db"), str(tmp_path / "s.json"))
    assert st.get()["maintenance"]["digest_time"] == "08:30"
    st.update({"maintenance": {"digest_time": "21:05", "backup_enabled": False}})
    assert st.get()["maintenance"] == {"digest_enabled": True, "digest_time": "21:05", "backup_enabled": False, "backup_time": "04:00"}
    with pytest.raises(SettingsError, match="HH:MM"):
        st.update({"maintenance": {"digest_time": "25:00"}})
