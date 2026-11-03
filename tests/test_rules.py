from bian.models import Position
from bian.rules import PortfolioState, RuleConfig, evaluate, evaluate_portfolio

CFG = RuleConfig()


def pos(roi, qty=1.0, entry=100.0, lev=10, liq=0.0):  # noqa: D401
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


def test_ac04_tier_once_then_escalate_once():
    cfg = RuleConfig(loss_tiers=(20, 35, 40))
    _, out = run([pos(-0.10), pos(-0.19), pos(-0.21), pos(-0.22), pos(-0.25), pos(-0.36), pos(-0.37)], cfg)
    assert out == [["R00"], [], ["L1"], [], [], ["L2"], []]


def test_tier_rearms_after_clear_recovery_but_not_twice_same_day():
    cfg = RuleConfig(loss_tiers=(20,), loss_cooldown_minutes=1)
    # 回升到 -10% 以上（20 - min(10, 10) = 10）才重新武装；但同一档当天只提醒一次
    _, out = run([pos(-0.21), pos(-0.12), pos(-0.21), pos(-0.09), pos(-0.21), pos(-0.21)], cfg, step=20)
    assert out == [["R00", "L1"], [], [], [], [], []]
    # 第二天：回升后再跌回该档，重新提醒
    st, out = run([pos(-0.21), pos(-0.09)], cfg, step=20)
    st, a = evaluate(pos(-0.21), st, cfg, 86400 * 2)
    assert [x.rule_id for x in a] == ["L1"]


def test_same_day_recovery_then_deeper_tier_still_escalates():
    cfg = RuleConfig(loss_tiers=(20, 40), loss_cooldown_minutes=0)
    _, out = run([pos(-0.21), pos(-0.05), pos(-0.21), pos(-0.45)], cfg)
    assert out == [["R00", "L1"], [], [], ["L2"]]


def test_escalation_skips_cooldown_and_jump_sends_highest_only():
    cfg = RuleConfig(loss_tiers=(50, 100, 150), loss_cooldown_minutes=60)
    _, out = run([pos(-0.55), pos(-1.2), pos(-1.6)], cfg, step=10)
    assert out == [["R00", "L1"], ["L2"], ["L3"]]
    _, out = run([pos(-1.6)], cfg)
    assert out == [["R00", "L3"]]  # 直接跳到第 3 档只发一条


def test_daily_cap_and_top_tier_repeat():
    cfg = RuleConfig(loss_tiers=(10, 20), loss_cooldown_minutes=0, loss_max_per_day=2,
                     loss_top_tier_repeat_hours=1)
    seq = [pos(-0.25)] + [pos(-0.25)] * 3
    st, out = run(seq, cfg, step=1800)
    assert out[0] == ["R00", "L2"]
    assert out[1] == [] and out[2] == ["L2"]  # 1 小时后最高档重复一次
    assert out[3] == []  # 达到每日上限 2 条


def test_add_position_does_not_reset_loss_tiers():
    cfg = RuleConfig(loss_tiers=(20,))
    st, out = run([pos(-0.21), pos(-0.21, qty=2.0, entry=95), pos(-0.22, qty=2.0, entry=95)], cfg)
    assert out[0] == ["R00", "L1"]
    assert out[1] == ["R07L"]  # 亏损超过第 1 档还加仓：摊平扛单提醒，不重复亏损通知
    assert st.version == 2 and out[2] == []


def test_add_in_loss_once_per_day_and_normal_add():
    cfg = RuleConfig(loss_tiers=(20,))
    _, out = run([pos(-0.21), pos(-0.21, qty=2.0), pos(-0.21, qty=3.0), pos(-0.21, qty=4.0)], cfg)
    assert out[1] == ["R07L"] and out[2] == ["R07"] and out[3] == ["R07"]  # 当天第二次起按普通加仓
    _, out = run([pos(0.05), pos(0.05, qty=2.0)], cfg)
    assert out[1] == ["R07"]  # 没亏损的加仓：普通加仓（默认不发邮件）


def test_price_mode_scales_with_leverage():
    cfg = RuleConfig(loss_tiers=(3, 5), loss_tiers_mode="price")
    _, out = run([pos(-0.31, lev=10)], cfg)  # 10x：-30% ROI = 价格反向 3%
    assert "L1" in out[0]
    _, out = run([pos(-0.31, lev=20)], cfg)  # 20x：第 1 档是 -60%
    assert out[0] == ["R00"]


def test_min_alert_u_blocks_tiny_loss_without_marking():
    st, alerts = evaluate(pos(-0.6), None, CFG, 0, margin_u=1.0)  # -0.6U
    assert [a.rule_id for a in alerts] == ["R00"] and st.loss["fired"] == []
    st, alerts = evaluate(pos(-0.6), st, CFG, 10, margin_u=5.0)  # 加仓后 -3U
    assert "L1" in [a.rule_id for a in alerts]


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
    assert a is None  # 当天已提醒，没有明显恶化
    st, a = evaluate_portfolio(-50, 100, st, 30)
    st, a = evaluate_portfolio(-110, 100, st, 40)
    assert a is None  # 回升后又跌回：当天不重复
    st, a = evaluate_portfolio(-151, 100, st, 50)
    assert a is not None  # 比上次（120U）扩大 25% 以上：立即再提醒
    st, a = evaluate_portfolio(-151, 100, st, 86400 * 2)
    assert a is not None  # 第二天状况仍在：再提醒一次


def test_rule_overrides():
    cfg = CFG.merged({"loss_tiers": [150, 50], "loss_tiers_mode": "price", "min_alert_u": 5, "unknown": 1})
    assert cfg.loss_tiers == (50.0, 150.0) and cfg.loss_tiers_mode == "price" and cfg.min_alert_u == 5
    assert cfg.trail_gap_roi == CFG.trail_gap_roi
    import pytest
    with pytest.raises(ValueError):
        CFG.merged({"loss_tiers_mode": "abc"})
