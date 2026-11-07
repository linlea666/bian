"""交易所账户管理（只读强制、密钥只进 secrets.json、移除保留 / 删除数据）、流水同步、统计按来源筛选。"""
import json
import os
import time

import pytest

from bian import accounts, stats
from bian.app import App
from bian.config import AppConfig
from bian.exchanges import AccountInfo, LedgerRow, Permissions
from bian.store import Store
from bian.sync import sync_account_ledger, upsert_fills
from tests.test_web import web  # noqa: F401

KEYS = {"api_key": "AK0123456789XYZ", "secret": "SK-very-secret", "passphrase": "pp"}


class FakeAd:
    def __init__(self, perm=Permissions(True, False, False, True)):
        self.perm, self.rows = perm, []

    def permissions(self):
        return self.perm

    def account(self):
        return AccountInfo(1000, 900, 0, 10, 0.01)

    def positions(self):
        return []

    def stop_orders(self):
        return []

    def ledger(self, since, until):
        return [r for r in self.rows if since <= r.ts_ms < until]


def fake_tester(ad):
    return lambda ex, keys, aid="x000000": accounts.test_connection(ex, keys, aid, adapter_factory=lambda *a: ad)


@pytest.fixture
def app(tmp_path):
    return App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))


def test_add_requires_read_only_key_and_stores_secret_only_in_secrets_file(app):
    store = Store(app.db_path)
    with pytest.raises(accounts.AccountError, match="交易权限"):
        accounts.add(store, app.settings, "binance", "主", KEYS, tester=fake_tester(FakeAd(Permissions(True, True, False))))
    with pytest.raises(accounts.AccountError, match="提现"):
        accounts.add(store, app.settings, "okx", "主", KEYS, tester=fake_tester(FakeAd(Permissions(True, False, True))))
    with pytest.raises(accounts.AccountError, match="Passphrase"):
        accounts.add(store, app.settings, "okx", "主", {"api_key": "a", "secret": "b"}, tester=fake_tester(FakeAd()))
    out = accounts.add(store, app.settings, "binance", "主账户", KEYS, tester=fake_tester(FakeAd()))
    aid = out["id"]
    assert accounts.ID_RE.match(aid) and out["equity"] == 1000
    row = accounts.get(store, aid)
    assert "SK-very-secret" not in json.dumps(row) and row["key_hint"].endswith("XYZ")
    assert app.settings.exchange_keys(aid)["secret"] == "SK-very-secret"
    assert oct(os.stat(app.settings.secrets_path).st_mode & 0o777) == "0o600"
    with pytest.raises(accounts.AccountError, match="已经添加过"):
        accounts.add(store, app.settings, "binance", "又一个", KEYS, tester=fake_tester(FakeAd()))


def test_update_and_remove_keep_or_delete(app, tmp_path):
    store = Store(app.db_path)
    aid = accounts.add(store, app.settings, "binance", "主账户", KEYS, tester=fake_tester(FakeAd()))["id"]
    accounts.update(store, app.settings, aid, name="小号", stop_check=False)
    a = accounts.get(store, aid)
    assert a["name"] == "小号" and a["stop_check"] == 0
    store.db.execute("INSERT INTO account_ledger VALUES ('u1', ?, 1, 'realized', 'BTCUSDT', 5)", (aid,))
    store.db.commit()
    accounts.remove(store, app.settings, aid, delete_data=False, backup_dir=str(tmp_path / "b"))
    assert accounts.get(store, aid)["removed"] == 1 and not app.settings.exchange_keys(aid)
    assert store.db.execute("SELECT COUNT(*) FROM account_ledger").fetchone()[0] == 1  # 数据保留
    assert accounts.active(store) == []
    with pytest.raises(accounts.AccountError):
        accounts.remove(store, app.settings, aid, delete_data=True, backup_dir=str(tmp_path / "b"), confirm="错的")
    out = accounts.remove(store, app.settings, aid, delete_data=True, backup_dir=str(tmp_path / "b"), confirm="小号")
    assert out["deleted"] and os.path.exists(out["backup"]) and accounts.get(store, aid) is None
    assert store.db.execute("SELECT COUNT(*) FROM account_ledger").fetchone()[0] == 0


def test_ledger_sync_incremental_and_dedupe():
    store = Store(":memory:")
    store.db.execute("INSERT INTO exchange_accounts (id, exchange, name) VALUES ('xabc123', 'okx', 'a')")
    ad = FakeAd()
    now = time.time()
    t = int(now * 1000)
    ad.rows = [LedgerRow("a", t - 5000, "realized", "BTCUSDT", 10), LedgerRow("b", t - 4000, "fee", "BTCUSDT", -1)]
    assert sync_account_ledger(store, ad, "xabc123", now) == 2
    ad.rows.append(LedgerRow("c", t - 1000, "funding", "BTCUSDT", -0.5))
    assert sync_account_ledger(store, ad, "xabc123", now) == 1  # 只拉最后一条之后的，重复的不再插入
    assert store.db.execute("SELECT ledger_synced FROM exchange_accounts").fetchone()[0] == now


def test_stats_source_filter():
    store = Store(":memory:")
    ts = int(time.mktime((2026, 11, 3, 12, 0, 0, 0, 0, -1)) * 1000)
    upsert_fills(store, "111", [{"time": ts, "symbol": "ETHUSDT", "side": "SELL", "positionSide": "LONG", "price": 1,
                                 "qty": 1, "quantity": 1, "fee": -1, "realizedProfit": 20}])
    for uid, kind, amt in (("r", "realized", 50), ("f", "fee", -2), ("fu", "funding", -3), ("t", "transfer", 500)):
        store.db.execute("INSERT INTO account_ledger VALUES (?, 'xabc123', ?, ?, 'BTCUSDT', ?)", (uid, ts, kind, amt))
    store.db.commit()
    start, end = ts / 1000 - 3600, ts / 1000 + 3600
    day = time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
    allv = stats.daily_realized(store, start, end)[day]
    assert (allv["realized"], allv["fee"], allv["funding"], allv["net"], allv["trades"]) == (70, -3, -3, 64, 2)
    assert stats.daily_realized(store, start, end, source="copy")[day]["net"] == 19
    acct = stats.daily_realized(store, start, end, source="xabc123")[day]
    assert acct["net"] == 45 and acct["funding"] == -3  # 划转不算盈亏
    assert stats.daily_realized(store, start, end, lead_id="111")[day]["net"] == 19
    assert stats.daily_realized(store, start, end, lead_id="xabc123")[day]["net"] == 45


def test_web_account_endpoints(web, monkeypatch):  # noqa: F811
    app_, c = web
    assert c.login("Passw0rd123")[0] == 200
    monkeypatch.setattr("bian.accounts.make_adapter", lambda *a, **k: FakeAd())
    s, d = c.call("POST", "/api/accounts/test", {"exchange": "binance", "api_key": "a", "secret": "b"})
    assert s == 200 and d["permissions"]["read_only"] and d["overlap"] == []
    s, d = c.call("POST", "/api/accounts", {"exchange": "binance", "name": "主账户", "api_key": "AK1", "secret": "S1"})
    assert s == 200
    aid = d["id"]
    s, d = c.call("GET", "/api/accounts")
    assert [a["name"] for a in d["accounts"]] == ["主账户"] and "S1" not in json.dumps(d)
    s, d = c.call("POST", "/api/accounts/update", {"id": aid, "enabled": False})
    assert s == 200 and d["account"]["enabled"] == 0
    s, d = c.call("POST", "/api/accounts/test", {"exchange": "kraken", "api_key": "a", "secret": "b"})
    assert s == 400
    s, d = c.call("GET", f"/api/stats?source={aid}")
    assert s == 200 and any(x["id"] == aid for x in d["sources"])
    assert c.call("GET", "/api/stats?source=bad")[0] == 400
    s, d = c.call("POST", "/api/accounts/remove", {"id": aid})
    assert s == 200 and not d["deleted"]
