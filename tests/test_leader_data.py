"""停止跟单后的数据：状态标记、导出、删除（M8）。"""
import io
import json
import os
import urllib.request
import zipfile

import pytest

from bian import leader_data
from bian.app import App
from bian.config import AppConfig, LeaderConfig
from bian.monitor import Monitor
from bian.rules import Alert
from bian.store import Store
from bian.sync import ensure_leader, mark_copy_set, upsert_fills, upsert_my_positions
from tests.test_web import web  # noqa: F401  复用网页测试夹具


def kind(store, lid):
    return store.db.execute("SELECT kind, ended_ts FROM leaders WHERE lead_id=?", (lid,)).fetchone()


def test_mark_copy_set_and_ensure_leader_transitions():
    store = Store(":memory:")
    ensure_leader(store, "A", "甲", "copy")
    ensure_leader(store, "B", "乙", "copy")
    ensure_leader(store, "W", "观察", "watch")
    assert mark_copy_set(store, ["A"], now=100) == ["B"]
    assert kind(store, "B") == ("ended", 100) and kind(store, "W")[0] == "watch"
    assert mark_copy_set(store, ["A"], now=200) == []  # 不重复标记
    ensure_leader(store, "B", "乙", "watch")  # 手动分析 → 转为观察
    assert kind(store, "B") == ("watch", None)
    mark_copy_set(store, ["A", "B"], now=300)  # 又开始跟单
    assert kind(store, "B") == ("copy", None)
    ensure_leader(store, "A", "甲", "watch")  # 跟单中不会被降为观察
    assert kind(store, "A")[0] == "copy"


def test_monitor_marks_relation_ended_while_service_was_down():
    store = Store(":memory:")
    ensure_leader(store, "OLD", "老领航员", "copy")
    mon = Monitor(AppConfig(leaders=[LeaderConfig(portfolio_id="X")]), None, None, store)
    for t in (1000, 1300, 1601):  # 整个列表为空：要连续 3 次且持续 10 分钟才确认
        mon._sync_relations([], now=t)
        assert kind(store, "OLD")[0] == ("copy" if t < 1601 else "ended")
    assert kind(store, "OLD") == ("ended", 1601)
    (title,) = [r[0] for r in store.db.execute("SELECT title FROM events WHERE type='leader.ended'")]
    assert "老领航员" in title and "保留" in title


def seed(store, lid="L", other="O"):
    for x in (lid, other):
        ensure_leader(store, x, f"名字{x}", "copy")
        upsert_fills(store, x, [{"time": 1000, "symbol": "ETHUSDT", "side": "BUY", "positionSide": "SHORT",
                                 "price": 1, "qty": 1, "quantity": 1, "fee": -0.1, "realizedProfit": "=1+1"}])
        upsert_my_positions(store, x, [{"positionId": "1", "symbol": "ETHUSDT", "side": "Short", "opened": 1,
                                        "closed": 2, "status": "All Closed"}])
        store.add_alert(Alert("L1", "warn", f"{x}:ETHUSDT:SHORT", 1, "t"), "m", True, "")
        store.add_alert(Alert("A01", "warn", f"acct:{x}", 1, "t"), "m", True, "")
        store.log_event("position.open", "开仓", leader_id=x)
        store.db.execute("INSERT INTO equity (ts, leader_id, equity, invested) VALUES (1, ?, 1, 1)", (x,))
    store.db.commit()


def test_export_zip_contains_all_tables_and_is_formula_safe(tmp_path):
    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    store = Store(app.db_path)
    seed(store)
    z = zipfile.ZipFile(io.BytesIO(leader_data.export_zip(store, app.settings, "L")))
    names = set(z.namelist())
    assert {"fills.csv", "my_positions.csv", "alerts.csv", "events.csv", "equity.csv", "all.json"} <= names
    full = json.loads(z.read("all.json"))
    assert len(full["alerts"]) == 2 and all(a["key"].endswith(("L:ETHUSDT:SHORT", "acct:L")) for a in full["alerts"])
    assert z.read("fills.csv").decode("utf-8-sig").startswith("uid,")
    ensure_leader(store, "L", "=HYPERLINK(1)", "copy")  # 名字里带公式：CSV 里要转义
    z = zipfile.ZipFile(io.BytesIO(leader_data.export_zip(store, app.settings, "L")))
    assert "'=HYPERLINK(1)" in z.read("leaders.csv").decode("utf-8-sig")


def test_delete_backs_up_first_and_leaves_others(tmp_path):
    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    app.settings.update({"leader_rules": {"L": {"min_alert_u": 3}, "O": {"min_alert_u": 4}}})
    store = Store(app.db_path)
    seed(store)
    with pytest.raises(leader_data.DeleteRefused):
        leader_data.delete(store, app.settings, "L", str(tmp_path / "backups"))  # 跟单中不能删
    mark_copy_set(store, ["O"], now=5)
    path, counts = leader_data.delete(store, app.settings, "L", str(tmp_path / "backups"))
    assert os.path.exists(path) and oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert counts["fills"] == 1 and counts["alerts"] == 2 and counts["leaders"] == 1
    for table, col in (("fills", "lead_id"), ("my_positions", "lead_id"), ("equity", "leader_id"), ("leaders", "lead_id")):
        assert store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}='L'").fetchone()[0] == 0
        assert store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}='O'").fetchone()[0] == 1
    assert store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2  # 只剩 O 的
    assert list(app.settings.get()["leader_rules"]) == ["O"]
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE type='leader.deleted'").fetchone()[0] == 1


def test_web_export_and_delete(web):  # noqa: F811
    app, c = web
    assert c.login("Passw0rd123")[0] == 200
    store = Store(app.db_path)
    seed(store, "111", "222")
    req = urllib.request.Request(c.base + "/api/leaders/export?id=111", headers={"Cookie": c.cookie})
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.headers["Content-Type"] == "application/zip" and zipfile.ZipFile(io.BytesIO(r.read())).namelist()
    s, d = c.call("POST", "/api/leaders/delete", {"id": "111", "confirm": "名字111"})
    assert s == 400 and "不能删除" in d["error"]
    mark_copy_set(store, ["222"], now=5)
    s, d = c.call("POST", "/api/leaders/delete", {"id": "111", "confirm": "错的名字"})
    assert s == 400
    s, d = c.call("POST", "/api/leaders/delete", {"id": "111", "confirm": "名字111"})
    assert s == 200 and d["backup"].startswith("leader-111-")
    s, d = c.call("GET", "/api/leaders/detail?id=111")
    assert s == 404



# ---------------------------------------------------------------- 停止跟单的确认保护
from bian.lifecycle import EpisodeRecorder  # noqa: E402
from bian.models import Position  # noqa: E402
from bian.monitor import LeaderRuntime  # noqa: E402
from bian.rules import RuleConfig  # noqa: E402


def copy_monitor(*pids):
    store = Store(":memory:")
    mon = Monitor(AppConfig(), None, None, store, recorder=EpisodeRecorder(store))
    for pid in pids:
        ensure_leader(store, pid, f"名字{pid}", "copy")
        mon.leaders.append(LeaderRuntime(LeaderConfig(portfolio_id=pid, source="copy"), RuleConfig(), source="copy",
                                         copy_portfolio_id="C" + pid, name=f"名字{pid}"))
    return store, mon


def rel(pid):
    return {"leadPortfolioId": pid, "copyPortfolioId": "C" + pid, "nickname": f"名字{pid}"}


def test_one_glitch_does_not_end_relation():
    store, mon = copy_monitor("A", "B")
    mon._sync_relations([rel("A")], now=0)  # 一次少了 B
    mon._sync_relations([rel("A"), rel("B")], now=20)  # 又出现了
    for t in (40, 60):
        mon._sync_relations([rel("A")], now=t)  # 重新计数：只有 2 次
    assert kind(store, "B")[0] == "copy" and len(mon.leaders) == 2
    assert not store.db.execute("SELECT COUNT(*) FROM events WHERE type IN ('leader.removed','leader.ended')").fetchone()[0]


def test_relation_ends_after_three_reads_and_two_minutes():
    store, mon = copy_monitor("A", "B")
    for t in (0, 20, 40):
        mon._sync_relations([rel("A")], now=t)
    assert kind(store, "B")[0] == "copy"  # 3 次了，但还不到 2 分钟
    mon._sync_relations([rel("A")], now=121)
    assert kind(store, "B")[0] == "ended" and [lr.cfg.portfolio_id for lr in mon.leaders] == ["A"]
    assert store.db.execute("SELECT COUNT(*) FROM events WHERE type='leader.removed'").fetchone()[0] == 1


def test_positions_still_visible_means_relation_alive():
    store, mon = copy_monitor("A", "B")
    lr_b = mon.leaders[1]
    p = Position("B", "ETHUSDT", "LONG", 1, 100, 100, 10, 0, "copy")
    lr_b.positions = {p.key: p}
    for t in range(0, 700, 20):
        lr_b.last_ok = t  # 每轮都还能查到他的跟单仓位
        mon._sync_relations([rel("A")], now=t)
    assert kind(store, "B")[0] == "copy" and len(mon.leaders) == 2
    lr_b.positions = {}  # 仓位没了，列表里也一直没有 → 确认停止
    mon._sync_relations([rel("A")], now=720)
    assert kind(store, "B")[0] == "ended"


def test_empty_list_needs_ten_minutes_and_does_not_close_episodes_early():
    store, mon = copy_monitor("A")
    mon.recorder.observe("A", "名字A", [Position("A", "ETHUSDT", "LONG", 1, 100, 100, 10, 0, "copy")], now=0)
    for t in (20, 40, 60, 200, 400):
        mon._sync_relations([], now=t)
    assert kind(store, "A")[0] == "copy"
    assert store.db.execute("SELECT status FROM episodes").fetchone()[0] == "open"  # 回合没有被提前结束
    mon._sync_relations([], now=620)
    assert kind(store, "A")[0] == "ended" and store.db.execute("SELECT status FROM episodes").fetchone()[0] == "closed"
