"""持仓风险规则引擎（纯函数 + 可序列化状态，便于测试和重启恢复）。

阶段：UNPROTECTED → BREAKEVEN_SUGGESTED（浮盈达门槛，建议保本）→ TRAILING（浮盈回撤保护）→ 平仓移除。
所有动作都是“建议”，必须由用户在币安页面执行；提醒文案不得声称“已止损”。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace

from .models import Position

SEVERITY_ORDER = {"info": 0, "warn": 1, "urgent": 2}


@dataclass(frozen=True)
class RuleConfig:
    warn_roi: float = -0.20  # R01 单仓风险提醒
    severe_roi: float = -0.35  # R02 单仓严重亏损
    hard_stop_roi: float = -0.40  # R03 计划硬止损线（建议立即手动平仓）
    breakeven_trigger_roi: float = 0.20  # R04 浮盈达到后建议保本
    breakeven_lock_roi: float = 0.02  # 保本线锁定 ROI（覆盖手续费）
    trail_trigger_roi: float = 0.20  # R05 浮盈回撤保护启动门槛
    trail_gap_roi: float = 0.15  # R05 从最高浮盈回撤多少个 ROI 点提醒
    liq_distance_pct: float = 0.05  # R09 标记价距强平价 ≤5% 紧急提醒
    rearm_gap: float = 0.05  # 回升超过阈值 + 该值后重新武装提醒（防抖）
    urgent_repeat_minutes: float = 30  # 紧急状态持续时的重复提醒间隔
    add_threshold: float = 0.01  # 数量变化超过 1% 视为加/减仓

    def merged(self, overrides: dict | None) -> "RuleConfig":
        if not overrides:
            return self
        names = {f.name for f in fields(self)}
        return replace(self, **{k: float(v) for k, v in overrides.items() if k in names})


@dataclass
class Alert:
    rule_id: str
    severity: str  # info / warn / urgent
    key: str  # 仓位 key 或 "portfolio" / "leader:<id>"
    version: int
    title: str
    detail: str = ""
    suggestion: str = ""
    ts: float = 0.0


@dataclass
class PosState:
    key: str
    version: int = 1
    qty: float = 0.0
    entry_price: float = 0.0
    peak_roi: float = 0.0
    trough_roi: float = 0.0
    last_roi: float = 0.0
    stage: str = "UNPROTECTED"
    fired: dict = field(default_factory=dict)  # rule_id -> {"version": v, "ts": t, "armed": bool, "ref": x}
    first_seen: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PosState":
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in fields(cls)}})


def _fmt_pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def _px(x: float) -> str:
    if x <= 0:
        return "-"
    return f"{x:.6g}"


def _can_fire(st: PosState, rule: str, now: float, repeat_s: float | None = None) -> bool:
    f = st.fired.get(rule)
    if not f or f.get("version") != st.version:
        return True
    if not f.get("armed", False):
        if repeat_s is not None and now - f.get("ts", 0) >= repeat_s:
            return True
        return False
    return True


def _mark(st: PosState, rule: str, now: float, ref: float = 0.0) -> None:
    st.fired[rule] = {"version": st.version, "ts": now, "armed": False, "ref": ref}


def _rearm(st: PosState, rule: str) -> None:
    f = st.fired.get(rule)
    if f and f.get("version") == st.version:
        f["armed"] = True


def evaluate(pos: Position, st: PosState | None, cfg: RuleConfig, now: float) -> tuple[PosState, list[Alert]]:
    alerts: list[Alert] = []
    roi = pos.roi

    def add(rule: str, sev: str, title: str, detail: str = "", suggestion: str = "") -> None:
        alerts.append(Alert(rule, sev, pos.key, st.version, title, detail, suggestion, now))  # type: ignore[union-attr]

    if st is None:
        st = PosState(key=pos.key, qty=pos.qty, entry_price=pos.entry_price, first_seen=now,
                      peak_roi=max(roi, 0.0), trough_roi=min(roi, 0.0))
        add("R00", "info", "新开仓")
    elif pos.qty > 0 and st.qty > 0 and abs(pos.qty - st.qty) / st.qty > cfg.add_threshold:
        increased = pos.qty > st.qty
        st.version += 1
        st.peak_roi = max(roi, 0.0)
        st.trough_roi = min(roi, 0.0)
        if st.stage != "UNPROTECTED" and roi < cfg.breakeven_trigger_roi:
            st.stage = "UNPROTECTED"
        change = f"数量 {st.qty:g} → {pos.qty:g}，均价 {_px(st.entry_price)} → {_px(pos.entry_price)}"
        if increased:
            add("R07", "warn", "领航员加仓（风险敞口扩大）", change,
                "加仓后均价改变：请检查币安止损/止盈是否仍覆盖整个仓位（跟单的 TP/SL 只对设置时的新仓有效）")
        else:
            add("R07b", "info", "部分减仓", change)
        st.qty, st.entry_price = pos.qty, pos.entry_price

    st.last_seen = now
    st.last_roi = roi
    st.peak_roi = max(st.peak_roi, roi)
    st.trough_roi = min(st.trough_roi, roi)
    repeat_s = cfg.urgent_repeat_minutes * 60

    # ---- 亏损分级：同一次评估只发最高一级，低级别同时标记为已发（避免升级时重复轰炸）
    stop_px = _px(pos.price_at_roi(cfg.hard_stop_roi))
    levels = [
        ("R03", cfg.hard_stop_roi, "urgent", "触及计划硬止损线",
         f"已越过计划止损价 {stop_px}，建议立即在币安手动平仓或减仓", repeat_s),
        ("R02", cfg.severe_roi, "urgent", "单仓严重亏损",
         f"确认币安上是否已设止损；计划硬止损价约 {stop_px}", None),
        ("R01", cfg.warn_roi, "warn", "单仓风险提醒",
         f"关注该仓；计划硬止损价约 {stop_px}", None),
    ]
    emitted = False
    for rule, thr, sev, title, sug, rep in levels:
        if roi <= thr:
            if _can_fire(st, rule, now, rep):
                if not emitted:
                    add(rule, sev, f"{title}（ROI {_fmt_pct(roi)} ≤ {_fmt_pct(thr)}）", "", sug)
                    emitted = True
                _mark(st, rule, now)
        elif roi > thr + cfg.rearm_gap:
            _rearm(st, rule)

    # ---- R09 接近强平
    if pos.liquidation_price > 0 and pos.mark_price > 0:
        dist = abs(pos.mark_price - pos.liquidation_price) / pos.mark_price
        if dist <= cfg.liq_distance_pct:
            if _can_fire(st, "R09", now, repeat_s):
                add("R09", "urgent", f"距强平价仅 {dist * 100:.1f}%",
                    f"现价 {_px(pos.mark_price)}，强平价 {_px(pos.liquidation_price)}",
                    "止损可能来不及：考虑立即减仓/平仓或追加保证金")
                _mark(st, "R09", now)
        elif dist > cfg.liq_distance_pct * 1.5:
            _rearm(st, "R09")

    # ---- R04 浮盈达门槛 → 建议保本
    if st.stage == "UNPROTECTED" and st.peak_roi >= cfg.breakeven_trigger_roi:
        be = pos.break_even_price if pos.break_even_price > 0 else pos.entry_price
        lock_px = pos.price_at_roi(cfg.breakeven_lock_roi)
        st.stage = "BREAKEVEN_SUGGESTED"
        add("R04", "info", f"浮盈达到 {_fmt_pct(st.peak_roi)}，建议上移止损到保本",
            f"保本价 {_px(be)}；锁定 {_fmt_pct(cfg.breakeven_lock_roi)} 的止损价约 {_px(lock_px)}",
            "在币安该跟单仓位把止损设到上述价格附近")

    # ---- R05 浮盈回撤保护
    if st.peak_roi >= cfg.trail_trigger_roi:
        if st.stage == "BREAKEVEN_SUGGESTED":
            st.stage = "TRAILING"
        giveback = st.peak_roi - roi
        f = st.fired.get("R05")
        if f and f.get("version") == st.version and st.peak_roi >= f.get("ref", 0) + cfg.trail_gap_roi / 2:
            f["armed"] = True  # 创出新高后重新武装
        if giveback >= cfg.trail_gap_roi and _can_fire(st, "R05", now):
            add("R05", "warn",
                f"浮盈回撤 {giveback * 100:.0f} 个点（最高 {_fmt_pct(st.peak_roi)} → {_fmt_pct(roi)}）",
                "",
                f"考虑部分止盈，或把止损提到约 {_px(pos.price_at_roi(roi - cfg.trail_gap_roi / 2))}")
            _mark(st, "R05", now, ref=st.peak_roi)

    # ---- R10 盈转亏：曾达到保本门槛，现已跌回保本线以下
    if st.stage in ("BREAKEVEN_SUGGESTED", "TRAILING") and roi <= cfg.breakeven_lock_roi:
        if _can_fire(st, "R10", now):
            add("R10", "urgent", f"盈利单回到保本线以下（最高 {_fmt_pct(st.peak_roi)} → {_fmt_pct(roi)}）",
                "", "若尚未设置保本止损，现在处理，避免盈利单变成亏损单")
            _mark(st, "R10", now)
    elif roi > cfg.breakeven_lock_roi + cfg.rearm_gap:
        _rearm(st, "R10")

    return st, alerts


@dataclass
class PortfolioState:
    fired: bool = False
    last_ts: float = 0.0


def evaluate_portfolio(
    est_pnl_u: float, budget_u: float, st: PortfolioState, now: float, repeat_s: float = 1800
) -> tuple[PortfolioState, Alert | None]:
    """R06 组合浮亏预算。est_pnl_u 为所有仓位的预估浮动盈亏（U，亏损为负）。"""
    if budget_u <= 0:
        return st, None
    if est_pnl_u <= -budget_u:
        if not st.fired or now - st.last_ts >= repeat_s:
            st.fired, st.last_ts = True, now
            return st, Alert("R06", "urgent", "portfolio", 0,
                             f"组合预估浮亏 {est_pnl_u:.2f}U 超过预算 {budget_u:.0f}U",
                             suggestion="考虑在币安暂停跟单（停止开新仓）并减仓亏损最大的仓位")
    elif est_pnl_u > -0.8 * budget_u:
        st.fired = False
    return st, None
