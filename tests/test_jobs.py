from bian import jobs
from bian.store import Store
from bian.sync import upsert_leader_history

M = 60_000


def lh(i, opened_min, close_min, cost=100.0, close=110.0, side="Long"):
    return {"positionId": str(i), "symbol": "BTCUSDT", "side": side, "opened": opened_min * M,
            "closed": close_min * M, "avgCost": cost, "avgClosePrice": close, "maxOpenInterest": 1,
            "closedVolume": 1, "leverage": "10", "closingPnl": (close - cost), "roi": str((close / cost - 1) * 10),
            "status": "All Closed", "updateTime": close_min * M}


class FakeKlines:
    """按开平时间生成 1 分钟 K 线：价格先跌 5% 再涨到平仓价。请求计数用于验证断点续传。"""

    base_url = "x"

    def __init__(self):
        self.calls = 0

    def get(self, path, params=None, private=False):
        self.calls += 1
        s, e = params["startTime"], params["endTime"]
        rows, t = [], s
        while t <= e:
            rows.append([t, "100", "111", "95", "105", 0, 0])
            t += M
        return rows


def test_job_resumes_and_builds_profile(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "b.db"))
    upsert_leader_history(store, "L", [lh(i, i * 100, i * 100 + 30) for i in range(1, 13)])
    monkeypatch.setattr(jobs, "sync_leader_history", lambda *a, **k: 0)
    assert jobs.enqueue(store, "L", "测试") and not jobs.enqueue(store, "L", "重复")
    kc = FakeKlines()
    ticks = iter(range(0, 10_000, 3))  # 每次取时间 +3 秒，budget 10 秒 → 每步处理约 3 笔
    more = jobs.step(store, None, kc, budget_s=10, cache_dir=str(tmp_path / "k"), clock=lambda: next(ticks))
    job = jobs.current_job(store, "L")
    assert more and job["state"] == "running" and 0 < job["progress"] < 12
    done_before = store.db.execute("SELECT COUNT(*) FROM leader_history WHERE path_interval IS NOT NULL").fetchone()[0]
    calls_before = kc.calls
    while jobs.current_job(store, "L")["state"] == "running":
        jobs.step(store, None, kc, budget_s=10_000, cache_dir=str(tmp_path / "k"), clock=lambda: 0)
    assert jobs.current_job(store, "L")["state"] == "done"
    assert kc.calls - calls_before == 12 - done_before  # 已算过的不再请求 K 线（回测读本地缓存）
    p = jobs.get_profile(store, "L")
    assert p["n"] == 12 and p["profile"]["mae_median"] < 0 and p["profile"]["winner_mfe_p50"] > 0
    assert p["backtest"]["baseline"]["n"] == 12 and p["backtest"]["stop_only"]
    assert not jobs.needs_refresh(store, "L")
    upsert_leader_history(store, "L", [lh(i, i * 100, i * 100 + 30) for i in range(13, 18)])
    assert jobs.needs_refresh(store, "L")


def test_closed_portfolio_stops_and_temporary_error_backs_off(tmp_path, monkeypatch):
    from bian.client import BinanceError

    store = Store(str(tmp_path / "b.db"))
    calls = []

    def boom(code):
        def f(*a, **k):
            calls.append(code)
            raise BinanceError("x", code=code)
        return f

    monkeypatch.setattr(jobs, "sync_leader_history", boom("11012030"))
    jobs.enqueue(store, "A", "t")
    jobs.step(store, None, FakeKlines(), cache_dir=str(tmp_path / "k"))
    assert jobs.current_job(store, "A")["state"] == "error"
    monkeypatch.setattr(jobs, "sync_leader_history", boom("500"))
    jobs.enqueue(store, "B", "t")
    jobs.step(store, None, FakeKlines(), cache_dir=str(tmp_path / "k"))
    assert jobs.step(store, None, FakeKlines(), cache_dir=str(tmp_path / "k")) is False  # 10 分钟内不再重试
    assert calls == ["11012030", "500"]
