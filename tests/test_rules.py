from bian.models import Position
from bian.rules import PortfolioState, RuleConfig, evaluate, evaluate_portfolio

CFG = RuleConfig()


def pos(roi, qty=1.0, entry=100.0, lev=10, liq=0.0):
    mark = entry * (1 + roi / lev)
    return Position("L", "BTCUSDT", "LONG", qty, entry, mark, lev, (mark - entry) * qty, "copy",
                    break_even_price=entry * 1.001, liquidation_price=liq)


def run(seq, cfg=CFG, step=20):
    st, out, t = None, [], 0.0
    for p in seq:
        st, alerts = evaluate(p, st, cfg, t)
        out.append([a.rule_id for a in alerts])
        t += step
    return st, out


def test_ac04_single_r01_then_escalate_once():
    _, out = run([pos(-0.10), pos(-0.19), pos(-0.21), pos(-0.22), pos(-0.25), pos(-0.36), pos(-0.37)])
    assert out == [["R00"], [], ["R01"], [], [], ["R02"], []]


def test_r01_rearms_after_recovery():
    _, out = run([pos(-0.21), pos(-0.10), pos(-0.21)])
    assert out == [["R00", "R01"], [], ["R01"]]


def test_hard_stop_repeats_after_interval_only():
    cfg = RuleConfig(urgent_repeat_minutes=1)
    _, out = run([pos(-0.41), pos(-0.42), pos(-0.43), pos(-0.44)], cfg, step=40)
    assert out[0] == ["R00", "R03"]  # 只发最高一级
    assert out[1] == []
    assert out[2] == ["R03"]  # 80 秒后重复紧急提醒


def test_ac05_breakeven_and_profit_to_loss():
    st, out = run([pos(0.05), pos(0.20), pos(0.12), pos(0.03), pos(0.01)])
    assert out[1] == ["R04"]
    assert "R05" not in out[2]  # 回撤 8 个点 < 15
    assert out[3] == ["R05"]  # 0.20 → 0.03 回撤 17 个点
    assert out[4] == ["R10"]  # 跌回保本线
    assert st.stage == "TRAILING"


def test_no_alert_claims_stop_executed():
    seq = [pos(0.3), pos(0.0), pos(-0.5)]
    st = None
    for p in seq:
        st, alerts = evaluate(p, st, CFG, 0)
        for a in alerts:
            assert "已止损" not in a.title + a.detail + a.suggestion


def test_add_position_bumps_version_and_rearms():
    st, out = run([pos(-0.21), pos(-0.21, qty=2.0, entry=95), pos(-0.22, qty=2.0, entry=95)])
    assert out[0] == ["R00", "R01"]
    assert out[1][0] == "R07" and "R01" in out[1]  # 加仓后新版本重新评估
    assert st.version == 2


def test_liquidation_proximity():
    _, out = run([pos(-0.1, liq=98.0)])  # mark 99, liq 98 → 1% 距离
    assert "R09" in out[0]


def test_portfolio_budget():
    st = PortfolioState()
    st, a = evaluate_portfolio(-50, 100, st, 0)
    assert a is None
    st, a = evaluate_portfolio(-120, 100, st, 10)
    assert a and a.rule_id == "R06"
    st, a = evaluate_portfolio(-130, 100, st, 20)
    assert a is None  # 冷却中
    st, a = evaluate_portfolio(-50, 100, st, 30)  # 恢复到 80% 以内重新武装
    st, a = evaluate_portfolio(-110, 100, st, 40)
    assert a is not None


def test_rule_overrides():
    cfg = CFG.merged({"warn_roi": -0.1, "unknown": 1})
    assert cfg.warn_roi == -0.1 and cfg.severe_roi == CFG.severe_roi
