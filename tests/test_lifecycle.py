import pytest

from bian.config import AppConfig, LeaderConfig
from bian.lifecycle import EpisodeRecorder, episodes, labels
from bian.models import Position
from bian.monitor import Monitor
from bian.store import Store


def pos(roi, qty=1.0, entry=100.0, side="LONG", symbol="ETHUSDT", lev=10):
    d = 1 if side == "LONG" else -1
    mark = entry * (1 + roi / lev * d)
    margin = qty * entry / lev
    return Position("L", symbol, side, qty, entry, mark, lev, roi * margin, "copy", initial_margin=margin)


def test_episode_tracks_extremes_adds_reduces_and_close_labels():
    store = Store(":memory:")
    rec = EpisodeRecorder(store)
    rec.observe("L", "测试", [pos(0.1)], now=100)
    rec.observe("L", "测试", [pos(0.6)], now=200)  # 最大浮盈 +60%
    rec.observe("L", "测试", [pos(-0.2, qty=2, entry=105)], now=300)  # 加仓后浮亏
    rec.observe("L", "测试", [pos(0.2, qty=1.5, entry=105)], now=400)  # 盈利中减仓，利润已回吐过半
    rec.note_alert("L", "ETHUSDT", "L2")
    rec.observe("L", "测试", [], now=500)  # 平仓
    (ep,) = episodes(store)
    assert ep["status"] == "closed" and ep["exit_type"] == "已平仓"
    assert ep["mfe_roi"] == pytest.approx(0.6) and ep["mfe_ts"] == 200
    assert ep["mae_roi"] == pytest.approx(-0.2) and ep["mae_u"] == pytest.approx(-0.2 * 21.0)
    assert ep["adds"] == 1 and ep["reduces"] == 1 and ep["reduces_in_profit"] == 1
    assert ep["max_tier"] == 2 and ep["alerts"] == 1
    assert "利润回吐" in ep["labels"] and "分批止盈" in ep["labels"]
    types = [e["type"] for e in store.events(limit=50)]
    for t in ("position.open", "position.add", "position.reduce", "position.close"):
        assert t in types


def test_profit_to_loss_label_and_restart_continuity(tmp_path):
    db = str(tmp_path / "b.db")
    rec = EpisodeRecorder(Store(db))
    rec.observe("L", "测试", [pos(0.5)], now=100)
    rec2 = EpisodeRecorder(Store(db))  # 重启后接着同一个回合
    rec2.observe("L", "测试", [pos(-0.1)], now=200)
    rec2.observe("L", "测试", [], now=300)
    (ep,) = episodes(Store(db))
    assert ep["opened_ts"] == 100 and "盈转亏" in ep["labels"] and "未分批止盈" in ep["labels"]


def test_reopened_during_downtime_starts_new_episode(tmp_path):
    db = str(tmp_path / "b.db")
    EpisodeRecorder(Store(db)).observe("L", "测试", [pos(0.1, entry=100)], now=100)
    EpisodeRecorder(Store(db)).observe("L", "测试", [pos(0.1, entry=90)], now=100 + 3600)
    eps = episodes(Store(db))
    assert [e["status"] for e in eps] == ["open", "closed"] and eps[1]["exit_type"] == "停机期间结束"


def test_labels_rules():
    assert labels({"mfe_roi": 0.2, "last_roi": -0.1}) == []  # 浮盈不到 30% 不评估回吐
    assert "浮亏超过保证金" in labels({"mae_roi": -1.2})


class FakeNotifier:
    def send(self, text, severity="info"):
        return True, ""


def test_monitor_records_episodes_runtime_and_alert_events():
    store = Store(":memory:")
    cfg = AppConfig(db_path=":memory:", leaders=[LeaderConfig("L", "测试", "public", position_margin_u=10)])
    rec = EpisodeRecorder(store)
    mon = Monitor(cfg, type("C", (), {"has_credentials": False})(), FakeNotifier(), store, lambda: 1000.0,
                  recorder=rec)
    lr = mon.leaders[0]
    lr.name, lr.source, lr.initialized = "测试", "public", True
    seq = iter([[pos(-0.6, side="LONG"), pos(0.1, side="SHORT", symbol="BTCUSDT")]])
    mon.fetch = lambda _lr: next(seq)
    mon.poll_once()
    assert len(episodes(store, status="open")) == 2
    positions, _ = store.get_runtime("positions")
    eth = [p for p in positions if p["symbol"] == "ETHUSDT"][0]
    assert eth["tier"] == 1 and eth["leader_name"] == "测试"
    poll, _ = store.get_runtime("poll")
    assert poll["leaders"][0]["positions"] == 2
    ev = store.events(type_prefix="alert.L1")
    assert ev and ev[0]["episode_id"] is not None
