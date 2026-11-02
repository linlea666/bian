import pytest

from bian.backtest import Costs, RuleParams, build_path, grid_search, run, simulate
from bian.models import ClosedPosition
from bian.profile import profile

M = 60_000
NO_COST = Costs(0, 0)


def cp(entry=100.0, close=110.0, side="LONG", lev=10, n_bars=10, pid="1", t0=0):
    return ClosedPosition("L", pid, "BTCUSDT", side, t0 + 30_000, t0 + n_bars * M + 30_000, entry, close,
                          1, 1, lev, (close - entry) * (1 if side == "LONG" else -1), 0.0)


def bars(prices_hl, t0=0):
    """prices_hl: [(open, high, low, close)]，第 i 根 openTime = i 分钟"""
    return [(t0 + i * M, o, h, l, c) for i, (o, h, l, c) in enumerate(prices_hl)]


def test_baseline_uses_leader_close():
    c = cp(close=110)
    path = build_path(c, bars([(100, 100, 100, 100)] * 12), M)
    r = simulate(path, RuleParams(), NO_COST)
    assert r.result == pytest.approx(1.0) and r.reason == "leader_close"


def test_hard_stop_hit_then_recovery_counts_as_winner_cut():
    c = cp(close=110)
    b = bars([(100, 100, 100, 100), (100, 100.5, 96, 97), (97, 112, 97, 110)] + [(110, 110, 110, 110)] * 9)
    path = build_path(c, b, M)
    r = simulate(path, RuleParams(stop_roi=-0.3), NO_COST)
    assert r.reason == "stop" and r.result == pytest.approx(-0.3)
    s = run([path], RuleParams(stop_roi=-0.3), NO_COST)
    assert s.winners_cut == 1 and s.profit_given_up == pytest.approx(1.3)


def test_gap_open_fills_at_open():
    c = cp(close=90)
    b = bars([(100, 100, 100, 100), (95, 95, 94, 94)] + [(94, 94, 90, 90)] * 10)
    r = simulate(build_path(c, b, M), RuleParams(stop_roi=-0.3), NO_COST)
    assert r.result == pytest.approx(-0.5)  # 跳空到 95，按开盘 -50% 成交


def test_first_bar_is_not_a_gap():
    c = cp(close=90)
    b = bars([(100, 100, 90, 95)] + [(95, 95, 90, 90)] * 11)  # 开仓所在 K 线收在 95（-50%）
    r = simulate(build_path(c, b, M), RuleParams(stop_roi=-0.3), NO_COST)
    assert r.result == pytest.approx(-0.3)


def test_breakeven_and_trailing():
    c = cp(close=95)  # 领航员最终亏损
    b = bars([(100, 100, 100, 100), (100, 104, 100, 103), (103, 103, 99, 99)] + [(99, 99, 95, 95)] * 9)
    path = build_path(c, b, M)
    be = simulate(path, RuleParams(be_trigger=0.3, be_lock=0.02), NO_COST)
    assert be.reason == "breakeven" and be.result == pytest.approx(0.02)
    tr = simulate(path, RuleParams(trail_gap=0.15, trail_trigger=0.2), NO_COST)
    assert tr.reason == "trail" and tr.result == pytest.approx(0.25)  # 峰值 +40% - 15


def test_short_side_and_costs():
    c = cp(entry=100, close=90, side="SHORT")
    path = build_path(c, bars([(100, 100, 100, 100)] * 12), M)
    r = simulate(path, RuleParams(), Costs(fee_rate=0.0005, stop_slippage=0))
    assert r.result == pytest.approx(1.0 - 0.01)  # 2 × 0.05% × 10x


def test_ac10_grid_reproducible():
    paths = []
    for i in range(40):
        close = 110 if i % 3 else 85
        c = cp(close=close, pid=str(i), t0=i * 20 * M)
        b = bars([(100, 100, 100, 100), (100, 103, 96, 98), (98, 106, 97, 105)] + [(close,) * 4] * 9, t0=i * 20 * M)
        paths.append(build_path(c, b, M))
    r1, r2 = grid_search(paths), grid_search(paths)
    assert [s.params for s in r1.ranked] == [s.params for s in r2.ranked]
    assert r1.baseline.total == r2.baseline.total
    assert all(not s.params.is_baseline for s in r1.ranked)
    assert r1.stop_only and all(s.params.be_trigger is None for s in r1.stop_only)
    assert r1.test_best is not None and r1.n_train == 28


def test_profile_basic_and_small_sample_tag():
    cps = [cp(close=110, pid=str(i)) for i in range(5)] + [cp(close=80, pid="x")]
    p = profile(cps)
    assert p["n"] == 6 and p["win_rate"] == pytest.approx(5 / 6)
    assert not p["sample_ok"] and any("样本不足" in t for t in p["tags"])
