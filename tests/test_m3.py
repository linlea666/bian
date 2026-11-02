"""实测后新增的逻辑：多空合并、U 金额门槛、强平价校验、跟单账户级规则、偏差与滑点。"""
import pytest

from bian.api.leaders import parse_position
from bian.diagnostics import compare_legs, match_positions, slippage
from bian.exposure import net_by_symbol
from bian.models import ClosedPosition, Position
from bian.rules import AccountSnapshot, RuleConfig, evaluate, evaluate_account

CFG = RuleConfig()


def leg(side, qty, entry, mark, im, symbol="BCHUSDT", liq=0.0):
    d = 1 if side == "LONG" else -1
    return Position("L", symbol, side, qty, entry, mark, 10, (mark - entry) * qty * d, "copy",
                    initial_margin=im, liquidation_price=liq)


# 实测真实结构（已脱敏）：私有 user-position 没有 leverage 字段
REAL_COPY_ROW = {
    "symbol": "ETHUSDT", "positionSide": "SHORT", "positionAmount": "-0.134", "entryPrice": "2262.16",
    "markPrice": "2291.64", "unrealizedProfit": "-3.951", "notionalValue": "-307.0797",
    "positionInitialMargin": "30.70797600", "initialMargin": "30.70797600", "liquidationPrice": "4538.58",
    "breakEvenPrice": "2261.0", "isolatedWallet": "0", "updateTime": 1793619617510,
}


def test_parse_real_copy_row_infers_leverage():
    p = parse_position(REAL_COPY_ROW, "L", "copy")
    assert p.side == "SHORT" and p.leverage == 10
    assert p.margin == pytest.approx(30.708)
    assert p.roi == pytest.approx(-3.951 / 30.708)
    assert not p.liquidation_valid()  # 距离 98% > 50%：全仓下的强平价没有参考意义


def test_liquidation_sanity():
    assert not leg("LONG", 1, 100, 100, 10, liq=739467).liquidation_valid()  # 多单强平价在上方：无效
    assert not leg("LONG", 1, 100, 100, 10, liq=0).liquidation_valid()
    assert leg("LONG", 1, 100, 100, 10, liq=96).liquidation_valid()
    assert not leg("SHORT", 1, 100, 100, 10, liq=180).liquidation_valid()  # 距离 80% > 50%


def test_net_by_symbol_merges_hedge():
    legs = [leg("LONG", 0.069, 287.16, 278.42, 1.92), leg("SHORT", 0.024, 290.74, 278.42, 0.67),
            leg("LONG", 0.001, 77354, 78000, 7.8, symbol="BTCUSDT")]
    out = {p.symbol: p for p in net_by_symbol(legs)}
    h = out["BCHUSDT"]
    assert h.side == "HEDGE" and h.key == "L:BCHUSDT:HEDGE"
    assert h.unrealized_pnl == pytest.approx(legs[0].unrealized_pnl + legs[1].unrealized_pnl)
    assert h.margin == pytest.approx(2.59)
    assert out["BTCUSDT"].side == "LONG"


def test_hedge_leg_loss_does_not_alert_when_net_is_fine():
    legs = [leg("LONG", 0.069, 287.16, 278.42, 1.92), leg("SHORT", 0.024, 290.74, 278.42, 0.67)]
    single_long = legs[0]
    assert single_long.roi < -0.3  # 单看多单 -31%
    h = net_by_symbol(legs)[0]
    _, alerts = evaluate(h, None, CFG, 0)
    assert [a.rule_id for a in alerts] == ["R00"]  # 合并后 -12%，不报警


def test_hedge_net_loss_alerts_without_price_suggestion():
    legs = [leg("LONG", 1, 100, 90, 10, symbol="X"), leg("SHORT", 0.2, 100, 90, 2, symbol="X")]
    h = net_by_symbol(legs)[0]  # (-10 + 2) / 12 = -66%
    _, alerts = evaluate(h, None, CFG, 0)
    r03 = [a for a in alerts if a.rule_id == "R03"][0]
    assert "对冲" in r03.suggestion and "R04" not in [a.rule_id for a in alerts]


def test_min_alert_u_gates_small_positions():
    p = leg("LONG", 0.069, 287.16, 278.42, 1.92)  # -31%，约 -0.6U
    _, alerts = evaluate(p, None, CFG, 0, margin_u=p.margin)
    assert [a.rule_id for a in alerts] == ["R00"]
    big = leg("LONG", 1, 100, 96.9, 10)  # -31%，-3.1U
    _, alerts = evaluate(big, None, CFG, 0, margin_u=big.margin)
    assert "R01" in [a.rule_id for a in alerts] and "U" in alerts[-1].title


def acct(equity, invested=300.0, stop_u=0.0, trigger=0.0, paused=False):
    return AccountSnapshot("L", "测试", equity, invested, 0, 0, paused, stop_u, trigger,
                           "SELL_ALL" if stop_u else "SELF_MANAGING")


def test_account_drawdown_and_escalation():
    st, al = evaluate_account(acct(300, stop_u=100, trigger=200), None, CFG, 0)
    assert al == []
    st, al = evaluate_account(acct(330, stop_u=100, trigger=200), st, CFG, 10)  # 新高
    st, al = evaluate_account(acct(295, stop_u=100, trigger=200), st, CFG, 20)  # 回撤 10.6%
    assert [a.rule_id for a in al] == ["A01"]
    st, al = evaluate_account(acct(294, stop_u=100, trigger=200), st, CFG, 30)
    assert al == []
    st, al = evaluate_account(acct(260, stop_u=100, trigger=200), st, CFG, 40)  # 回撤 21%
    assert [a.rule_id for a in al] == ["A01b"]


def test_account_withdrawal_is_not_drawdown():
    st, _ = evaluate_account(acct(300, stop_u=100, trigger=200), None, CFG, 0)
    st, al = evaluate_account(acct(200, invested=200, stop_u=100, trigger=100), st, CFG, 10)  # 赎回 100U
    assert al == []


def test_account_native_stop_missing_once_per_day_and_near_trigger():
    st, al = evaluate_account(acct(300), None, CFG, 0)
    assert [a.rule_id for a in al] == ["A02"] and "60U" in al[0].suggestion
    st, al = evaluate_account(acct(300), st, CFG, 3600)
    assert al == []
    st, al = evaluate_account(acct(300), st, CFG, 25 * 3600)
    assert [a.rule_id for a in al] == ["A02"]
    st2, al = evaluate_account(acct(225, stop_u=100, trigger=201), None, CFG, 0)  # 只剩 24U < 30U
    assert "A03" in [a.rule_id for a in al]


def test_compare_legs_classification():
    mine = [leg("LONG", 0.001, 1, 1, 1, symbol="BTCUSDT"), leg("LONG", 0.01, 1, 1, 1, symbol="BNBUSDT"),
            leg("SHORT", 1, 1, 1, 1, symbol="OLD")]
    theirs = [leg("LONG", 0.006, 1, 1, 1, symbol="BTCUSDT"), leg("LONG", 0.8, 1, 1, 1, symbol="BNBUSDT"),
              leg("LONG", 2.67, 1, 1, 1, symbol="BZUSDT")]
    kinds = {(d.symbol, d.kind) for d in compare_legs(mine, theirs, 0.0318)}
    assert kinds == {("BTCUSDT", "over"), ("BNBUSDT", "under"), ("BZUSDT", "missing"), ("OLD", "residual")}


def cp(pid, opened, cost, close, side="LONG"):
    return ClosedPosition("L", pid, "ETHUSDT", side, opened, opened + 3_600_000, cost, close, 1, 1, 10, 0, 0)


def test_match_and_slippage():
    mine = [cp("m1", 1_000_000, 101, 109), cp("m2", 99_000_000, 50, 55)]
    theirs = [cp("t1", 1_000_000 - 60_000, 100, 110), cp("t2", 50_000_000, 50, 55)]
    pairs = match_positions(mine, theirs)
    assert [(m.position_id, t.position_id) for m, t in pairs] == [("m1", "t1")]
    e, x = slippage(*pairs[0])
    assert e == pytest.approx(0.01) and x == pytest.approx(110 / 109 - 1)
    es, xs = slippage(cp("a", 0, 99, 111, "SHORT"), cp("b", 0, 100, 110, "SHORT"))
    assert es == pytest.approx(0.01) and xs == pytest.approx(-(110 / 111 - 1))
