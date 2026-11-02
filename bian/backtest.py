"""止损/保本/浮盈回撤规则回测（基于领航员已平仓历史 + K 线路径重建）。

精度说明（必须随报告展示）：
- 公开历史只有“开仓时间、平均开仓价、平仓时间、平均平仓价”，没有逐笔加减仓，
  因此路径按“开仓时一次性以均价建仓、平仓时一次性以均价平仓”近似。有加仓的单子误差更大。
- K 线内高低点先后顺序未知：一律假设“先不利、后有利”（保守）。
- 第一根 K 线只用“开仓价→收盘价”（避免把开仓前的波动算进来）；最后一根不计，直接按领航员平仓均价出场。
- 跟单者实际成交还受滑点、分润、资金费影响；这里只计手续费 + 止损滑点假设。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .models import ClosedPosition

Bar = tuple[int, float, float, float, float]  # (openTime, o, h, l, c)


@dataclass(frozen=True)
class RuleParams:
    stop_roi: float | None = None  # 硬止损，如 -0.30（ROI 跌到 -30% 平仓）
    be_trigger: float | None = None  # 浮盈达到该 ROI 后，止损上移到保本
    be_lock: float = 0.02  # 保本线锁定的 ROI（覆盖手续费）
    trail_gap: float | None = None  # 浮盈回撤保护：止损 = 最高浮盈 - gap
    trail_trigger: float | None = None  # 开始回撤保护的最高浮盈门槛（默认同 be_trigger 或 0.2）

    @property
    def is_baseline(self) -> bool:
        return self.stop_roi is None and self.be_trigger is None and self.trail_gap is None

    def label(self) -> str:
        if self.is_baseline:
            return "A 原始跟单"
        parts = []
        if self.stop_roi is not None:
            parts.append(f"止损{self.stop_roi:+.0%}")
        if self.be_trigger is not None:
            parts.append(f"浮盈{self.be_trigger:+.0%}保本")
        if self.trail_gap is not None:
            parts.append(f"回撤{self.trail_gap:.0%}止盈(≥{self._trail_trigger():+.0%})")
        return " + ".join(parts)

    def _trail_trigger(self) -> float:
        if self.trail_trigger is not None:
            return self.trail_trigger
        return self.be_trigger if self.be_trigger is not None else 0.2


@dataclass
class Costs:
    fee_rate: float = 0.0005  # 单边手续费（名义价值比例），taker 0.05%
    stop_slippage: float = 0.0005  # 规则出场额外滑点（名义价值比例）

    def roi_cost(self, leverage: float, rule_exit: bool) -> float:
        return (2 * self.fee_rate + (self.stop_slippage if rule_exit else 0.0)) * max(leverage, 1)


@dataclass
class Path:
    """一笔仓位按 K 线重建后的 ROI 序列：每根 (不利 ROI, 有利 ROI, 收盘 ROI, 开盘 ROI)。"""

    cp: ClosedPosition
    steps: list[tuple[float, float, float, float]]
    interval: str = ""

    @property
    def mae(self) -> float:
        vals = [s[0] for s in self.steps] + [self.final_roi]
        return min(min(vals), 0.0)

    @property
    def mfe(self) -> float:
        vals = [s[1] for s in self.steps] + [self.final_roi]
        return max(max(vals), 0.0)

    @property
    def final_roi(self) -> float:
        # 全仓模式下单仓 ROI 可以低于 -100%（亏损超过该仓开仓保证金），不截断
        return self.cp.price_roi


def build_path(cp: ClosedPosition, bars: list[Bar], interval_ms: int, interval: str = "") -> Path:
    lev = max(cp.leverage, 1)
    d = cp.direction

    def roi(px: float) -> float:
        return (px / cp.avg_cost - 1) * d * lev if cp.avg_cost > 0 else 0.0

    steps = []
    for t, o, h, l, c in bars:
        if t + interval_ms <= cp.opened or t + interval_ms > cp.closed:
            continue  # 开仓前、或包含平仓时刻（之后）的 K 线
        if t <= cp.opened:  # 开仓所在 K 线：只知道“从开仓价走到收盘价”，开盘视为 0（不算跳空）
            r = roi(c)
            steps.append((min(r, 0.0), max(r, 0.0), r, 0.0))
            continue
        adverse, favorable = (roi(l), roi(h)) if d > 0 else (roi(h), roi(l))
        steps.append((adverse, favorable, roi(c), roi(o)))
    return Path(cp, steps, interval)


@dataclass
class TradeResult:
    cp: ClosedPosition
    baseline: float  # 原始跟单净 ROI
    result: float  # 应用规则后的净 ROI
    reason: str  # leader_close / stop / breakeven / trail


def simulate(path: Path, p: RuleParams, costs: Costs) -> TradeResult:
    cp = path.cp
    lev = cp.leverage
    base = path.final_roi - costs.roi_cost(lev, False)
    if p.is_baseline:
        return TradeResult(cp, base, base, "leader_close")

    trail_trig = p._trail_trigger()
    peak = 0.0

    def stop_level() -> tuple[float, str]:
        level, why = float("-inf"), "none"
        if p.stop_roi is not None and p.stop_roi > level:
            level, why = p.stop_roi, "stop"
        if p.be_trigger is not None and peak >= p.be_trigger and p.be_lock > level:
            level, why = p.be_lock, "breakeven"
        if p.trail_gap is not None and peak >= trail_trig and peak - p.trail_gap > level:
            level, why = peak - p.trail_gap, "trail"
        return level, why

    def exit_at(level: float, why: str, gap_open: float | None) -> TradeResult:
        fill = level if gap_open is None else min(level, gap_open)  # 跳空直接按开盘成交
        return TradeResult(cp, base, fill - costs.roi_cost(lev, True), why)

    for adverse, favorable, close, open_ in path.steps:
        level, why = stop_level()
        if open_ <= level:
            return exit_at(level, why, open_)
        if adverse <= level:
            return exit_at(level, why, None)
        peak = max(peak, favorable)
        level, why = stop_level()
        if close <= level:  # 冲高后回落到新保护线之下
            return exit_at(level, why, None)
    return TradeResult(cp, base, base, "leader_close")


@dataclass
class Summary:
    params: RuleParams
    n: int
    total: float  # 以“每笔 1 份保证金”为单位的累计收益
    win_rate: float
    profit_factor: float
    worst: float
    max_dd: float  # 累计收益曲线的最大回撤（同单位）
    rule_exits: int
    winners_cut: int  # 原本盈利、被规则提前出场且少赚的单数
    profit_given_up: float  # 规则导致少赚的总额
    loss_saved: float  # 规则减少的亏损总额
    blowups: int  # 单笔亏损 ≥ 100% 开仓保证金的笔数
    results: list[TradeResult] = field(default_factory=list, repr=False)

    def score(self, risk_aversion: float = 0.5) -> float:
        return self.total - risk_aversion * self.max_dd


def summarize(params: RuleParams, results: list[TradeResult]) -> Summary:
    ordered = sorted(results, key=lambda r: r.cp.closed)
    rets = [r.result for r in ordered]
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x < 0]
    eq = peak = dd = 0.0
    for x in rets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gave_up = sum(r.baseline - r.result for r in ordered if r.result < r.baseline)
    saved = sum(r.result - r.baseline for r in ordered if r.result > r.baseline)
    return Summary(
        params=params,
        n=len(rets),
        total=sum(rets),
        win_rate=len(wins) / len(rets) if rets else 0.0,
        profit_factor=(sum(wins) / -sum(losses)) if losses else float("inf"),
        worst=min(rets) if rets else 0.0,
        max_dd=dd,
        rule_exits=sum(1 for r in ordered if r.reason not in ("leader_close",)),
        winners_cut=sum(1 for r in ordered if r.baseline > 0 and r.result < r.baseline),
        profit_given_up=gave_up,
        loss_saved=saved,
        blowups=sum(1 for r in ordered if r.result <= -1.0),
        results=ordered,
    )


def run(paths: list[Path], params: RuleParams, costs: Costs) -> Summary:
    return summarize(params, [simulate(p, params, costs) for p in paths])


DEFAULT_GRID = {
    "stop_roi": [None, -0.1, -0.2, -0.3, -0.4, -0.5, -0.7],
    "be_trigger": [None, 0.1, 0.2, 0.3, 0.5],
    "trail_gap": [None, 0.1, 0.15, 0.2, 0.3],
}


def grid(spec: dict | None = None, be_lock: float = 0.02) -> list[RuleParams]:
    spec = spec or DEFAULT_GRID
    out = []
    for s, b, t in itertools.product(spec["stop_roi"], spec["be_trigger"], spec["trail_gap"]):
        out.append(RuleParams(stop_roi=s, be_trigger=b, be_lock=be_lock, trail_gap=t))
    return out


@dataclass
class GridReport:
    baseline: Summary
    ranked: list[Summary]  # 全样本，按 score 降序（不含原始跟单）
    stop_only: list[Summary]  # 只用硬止损的方案（币安跟单可直接设置），按止损从紧到松
    train_best: Summary | None  # 训练集最优参数在训练集上的表现
    test_baseline: Summary | None
    test_best: Summary | None  # 同一参数在样本外（时间靠后 30%）的表现
    n_train: int = 0
    n_test: int = 0


def grid_search(
    paths: list[Path],
    costs: Costs | None = None,
    spec: dict | None = None,
    risk_aversion: float = 0.5,
    train_frac: float = 0.7,
) -> GridReport:
    costs = costs or Costs()
    combos = grid(spec)
    baseline = run(paths, RuleParams(), costs)
    results = [run(paths, c, costs) for c in combos if not c.is_baseline]
    ranked = sorted(results, key=lambda s: -s.score(risk_aversion))
    stop_only = sorted(
        (r for r in results if r.params.be_trigger is None and r.params.trail_gap is None),
        key=lambda r: -(r.params.stop_roi or 0),
    )

    ordered = sorted(paths, key=lambda p: p.cp.closed)
    cut = int(len(ordered) * train_frac)
    train, test = ordered[:cut], ordered[cut:]
    train_best = test_best = test_base = None
    if len(train) >= 20 and len(test) >= 10:
        train_best = max((run(train, c, costs) for c in combos), key=lambda s: s.score(risk_aversion))
        test_best = run(test, train_best.params, costs)
        test_base = run(test, RuleParams(), costs)
    return GridReport(baseline, ranked, stop_only, train_best, test_base, test_best, len(train), len(test))
