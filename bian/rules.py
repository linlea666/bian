"""持仓风险规则引擎（纯函数 + 可序列化状态，便于测试和重启恢复）。

阶段：UNPROTECTED → BREAKEVEN_SUGGESTED（浮盈达门槛，建议保本）→ TRAILING（浮盈回撤保护）→ 平仓移除。
所有动作都是“建议”，必须由用户在币安页面执行；提醒文案不得声称“已止损”。
"""
from __future__ import annotations

import time as _time
from dataclasses import asdict, dataclass, field, fields, replace

from .models import Position

SEVERITY_ORDER = {"info": 0, "warn": 1, "urgent": 2}


@dataclass(frozen=True)
class RuleConfig:
    # ---- 仓位亏损分档通知（L1/L2/L3...）
    loss_tiers: tuple = (50.0, 100.0, 150.0)  # 亏损百分比档位；第 1 档“警告”，之后“紧急”
    loss_tiers_mode: str = "roi"  # roi：按仓位 ROI（与币安仓位页一致）；price：按价格反向波动 %，自动 × 杠杆
    loss_rearm_gap: float = 10.0  # 回升到“档位 − min(该值, 档位/2)”以上才重新武装该档（百分点）
    loss_cooldown_minutes: float = 15  # 同一仓位两条亏损通知的最小间隔（升级到更高档除外）
    loss_max_per_day: float = 4  # 每个仓位每天最多几条亏损通知
    loss_top_tier_repeat_hours: float = 0  # 停在最高档超过 N 小时再提醒一次；0 = 关闭
    breakeven_trigger_roi: float = 0.20  # R04 浮盈达到后建议保本
    breakeven_lock_roi: float = 0.02  # 保本线锁定 ROI（覆盖手续费）
    trail_trigger_roi: float = 0.20  # R05 浮盈回撤保护启动门槛
    trail_gap_roi: float = 0.15  # R05 从最高浮盈回撤多少个 ROI 点提醒
    liq_distance_pct: float = 0.05  # R09 标记价距强平价 ≤5% 紧急提醒
    rearm_gap: float = 0.05  # 回升超过阈值 + 该值后重新武装提醒（防抖）
    urgent_repeat_minutes: float = 30  # 紧急状态持续时的重复提醒间隔
    add_threshold: float = 0.01  # 数量变化超过 1% 视为加/减仓
    min_alert_u: float = 2.0  # 我的盈亏金额（U）低于该值时，ROI 类提醒不发（避免小仓位刷屏）
    liq_sanity_pct: float = 0.5  # 强平价距标记价超过 50% 视为无效数据
    # ---- 跟单账户级（每个跟单关系）
    account_dd_warn: float = 0.10  # 跟单权益从最高点回撤 10% 提醒
    account_dd_severe: float = 0.20  # 回撤 20% 紧急提醒
    recommend_total_stop_pct: float = 0.20  # 未设原生总止损时，建议的止损额 = 净投入 × 该比例
    native_stop_near_pct: float = 0.30  # 距原生总止损触发只剩止损额的 30% 时提醒

    def merged(self, overrides: dict | None) -> "RuleConfig":
        if not overrides:
            return self
        out = {}
        for f in fields(self):
            if f.name not in overrides:
                continue
            v = overrides[f.name]
            if f.name == "loss_tiers":
                tiers = tuple(sorted(float(x) for x in v if float(x) > 0))
                if not tiers:
                    raise ValueError("loss_tiers 至少需要一个正数档位")
                out[f.name] = tiers
            elif f.name == "loss_tiers_mode":
                if v not in ("roi", "price"):
                    raise ValueError("loss_tiers_mode 只能是 roi 或 price")
                out[f.name] = v
            else:
                out[f.name] = float(v)
        return replace(self, **out)

    def tier_rois(self, leverage: float) -> list[float]:
        """把档位换算成 ROI 亏损（正小数，0.5 = -50%）。"""
        mult = max(leverage, 1.0) if self.loss_tiers_mode == "price" else 1.0
        return [t / 100 * mult for t in self.loss_tiers]


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
    # 亏损分档状态：不随加仓版本重置（避免领航员边亏边加时反复通知）
    loss: dict = field(default_factory=lambda: {"fired": [], "last_ts": 0.0, "day": "", "day_count": 0, "top_ts": 0.0})
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


def evaluate(
    pos: Position, st: PosState | None, cfg: RuleConfig, now: float, margin_u: float | None = None
) -> tuple[PosState, list[Alert]]:
    """margin_u：我在该仓位上的保证金（U）。用于把 ROI 换算成我的盈亏金额并应用 min_alert_u 门槛；
    None 表示未知（不做金额门槛）。"""
    alerts: list[Alert] = []
    roi = pos.roi

    def u(r: float) -> str:
        return f" ≈ {r * margin_u:+.2f}U" if margin_u else ""

    def big(r: float) -> bool:
        return not margin_u or abs(r * margin_u) >= cfg.min_alert_u

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

    alerts += _loss_tiers(pos, st, cfg, now, margin_u)

    # ---- R09 接近强平
    if pos.liquidation_valid(cfg.liq_sanity_pct):
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
    if pos.is_hedge:
        return st, alerts  # 多空合并仓没有单一保本价，盈利保护类规则只对单边仓位

    if st.stage == "UNPROTECTED" and st.peak_roi >= cfg.breakeven_trigger_roi and big(st.peak_roi):
        be = pos.break_even_price if pos.break_even_price > 0 else pos.entry_price
        lock_px = pos.price_at_roi(cfg.breakeven_lock_roi)
        st.stage = "BREAKEVEN_SUGGESTED"
        add("R04", "info", f"浮盈达到 {_fmt_pct(st.peak_roi)}{u(st.peak_roi)}，建议上移止损到保本",
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
        if giveback >= cfg.trail_gap_roi and big(giveback) and _can_fire(st, "R05", now):
            add("R05", "warn",
                f"浮盈回撤 {giveback * 100:.0f} 个点{u(-giveback)}（最高 {_fmt_pct(st.peak_roi)} → {_fmt_pct(roi)}）",
                "",
                f"考虑部分止盈，或把止损提到约 {_px(pos.price_at_roi(roi - cfg.trail_gap_roi / 2))}")
            _mark(st, "R05", now, ref=st.peak_roi)

    # ---- R10 盈转亏：曾达到保本门槛，现已跌回保本线以下
    if st.stage in ("BREAKEVEN_SUGGESTED", "TRAILING") and roi <= cfg.breakeven_lock_roi and big(st.peak_roi):
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


# ============================================================== 跟单账户级
def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class AccountSnapshot:
    """一个跟单关系（copy-portfolio/detail-list 的一项）。"""

    lead_id: str
    nickname: str
    equity: float  # marginBalance：我的跟单权益（含浮动盈亏）
    invested: float  # netCopyAmount：净投入
    unrealized: float
    realized: float
    is_paused: bool
    native_stop_u: float  # totalStopLossUsdt：原生总止损额（0 = 未设置）
    native_stop_trigger_equity: float  # totalStopLossAmount：权益跌到该值时触发（推断）
    disposal: str  # SELL_ALL / SELF_MANAGING ...

    @classmethod
    def from_relation(cls, r: dict) -> "AccountSnapshot":
        return cls(
            lead_id=str(r.get("leadPortfolioId")),
            nickname=str(r.get("nickname") or ""),
            equity=_f(r.get("marginBalance")),
            invested=_f(r.get("netCopyAmount")),
            unrealized=_f(r.get("unrealizedPnl")),
            realized=_f(r.get("realizedPnl")),
            is_paused=bool(r.get("isPaused")),
            native_stop_u=_f(r.get("totalStopLossUsdt")),
            native_stop_trigger_equity=_f(r.get("totalStopLossAmount")),
            disposal=str(r.get("disposalStrategy") or ""),
        )

    @property
    def has_native_stop(self) -> bool:
        return self.native_stop_u > 0 and self.native_stop_trigger_equity > 0


@dataclass
class AccountState:
    key: str
    peak_equity: float = 0.0
    last_equity: float = 0.0
    last_invested: float = 0.0
    version: int = 1
    fired: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AccountState":
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in fields(cls)}})


def evaluate_account(
    a: AccountSnapshot, st: AccountState | None, cfg: RuleConfig, now: float, recommended_stop_u: float = 0.0
) -> tuple[AccountState, list[Alert]]:
    """A01 权益回撤；A02 未设置原生总止损；A03 接近原生总止损触发线。"""
    key = f"acct:{a.lead_id}"
    alerts: list[Alert] = []
    if st is None:
        st = AccountState(key=key, peak_equity=max(a.equity, a.invested), last_invested=a.invested)
    if abs(a.invested - st.last_invested) > 0.01:
        # 追加/赎回投入：回撤基准随之平移，避免把出金当成亏损
        st.peak_equity += a.invested - st.last_invested
        st.last_invested = a.invested
        st.version += 1
    st.peak_equity = max(st.peak_equity, a.equity)
    st.last_equity = a.equity
    repeat_s = cfg.urgent_repeat_minutes * 60

    def add(rule, sev, title, detail="", suggestion=""):
        alerts.append(Alert(rule, sev, key, st.version, title, detail, suggestion, now))

    # 复用 PosState 的去重工具
    shim = PosState(key=key, version=st.version, fired=st.fired)

    dd = (st.peak_equity - a.equity) / st.peak_equity if st.peak_equity > 0 else 0.0
    loss_u = a.equity - a.invested
    detail = (f"跟单权益 {a.equity:.2f}U（最高 {st.peak_equity:.2f}U，净投入 {a.invested:.2f}U，"
              f"相对净投入 {loss_u:+.2f}U）")
    emitted = False
    for rule, thr, sev, rep in (("A01b", cfg.account_dd_severe, "urgent", repeat_s),
                                ("A01", cfg.account_dd_warn, "warn", None)):
        if dd >= thr and (st.peak_equity - a.equity) >= cfg.min_alert_u:
            if _can_fire(shim, rule, now, rep):
                if not emitted:
                    add(rule, sev, f"跟单权益从最高点回撤 {dd * 100:.1f}%（{a.equity - st.peak_equity:+.2f}U）", detail,
                        "考虑在币安暂停该跟单（不再开新仓）或减仓；若未设置跟单总止损，现在设置")
                    emitted = True
                _mark(shim, rule, now)
        elif dd < thr - 0.03:
            _rearm(shim, rule)

    if not a.has_native_stop:
        rec = recommended_stop_u or round(a.invested * cfg.recommend_total_stop_pct, 2)
        if a.invested > 0 and _can_fire(shim, "A02", now, 24 * 3600):
            add("A02", "info", "该跟单未设置“总止损”",
                f"净投入 {a.invested:.2f}U，当前权益 {a.equity:.2f}U",
                f"在币安该跟单的设置里开启总止损，建议亏损额 ≈ {rec:.0f}U（权益跌到约 {a.invested - rec:.0f}U 时由交易所自动全部平仓）")
            _mark(shim, "A02", now)
    else:
        room = a.equity - a.native_stop_trigger_equity
        if room <= a.native_stop_u * cfg.native_stop_near_pct:
            if _can_fire(shim, "A03", now, repeat_s):
                add("A03", "urgent", f"距跟单总止损触发只剩 {room:.2f}U",
                    f"{detail}；总止损 {a.native_stop_u:.0f}U，触发权益约 {a.native_stop_trigger_equity:.2f}U，"
                    f"触发后处理方式 {a.disposal}",
                    "交易所会按设置自动处理；如不希望全部平仓，请提前手动减仓")
                _mark(shim, "A03", now)
        elif room > a.native_stop_u * cfg.native_stop_near_pct * 1.5:
            _rearm(shim, "A03")

    if a.is_paused and _can_fire(shim, "A04", now, 24 * 3600):
        add("A04", "info", "该跟单处于暂停状态（不会跟新仓，已有仓位仍在）")
        _mark(shim, "A04", now)

    st.fired = shim.fired
    return st, alerts


# ============================================================== 仓位亏损分档
def _day(now: float) -> str:
    return _time.strftime("%Y-%m-%d", _time.localtime(now))


def _loss_tiers(pos: Position, st: PosState, cfg: RuleConfig, now: float, margin_u: float | None) -> list[Alert]:
    """仓位亏损分档通知（参考 nofx-1 position_loss_alert，增加分档、冷却、每日上限）。

    - 每档对每个仓位只通知一次；越过更高档立即再通知（升级不受冷却限制）。
    - 回升到“档位 − min(rearm_gap, 档位/2)”以上，才重新武装该档。
    - 同一仓位两条通知至少间隔 loss_cooldown_minutes（升级除外）；每天最多 loss_max_per_day 条。
    - 被冷却/上限挡住的不标记，条件仍满足时稍后再发。
    """
    ls = st.loss
    ls.setdefault("fired", [])
    rois = cfg.tier_rois(pos.leverage)
    loss = -pos.roi  # 正数 = 亏损
    # 重新武装：亏损明显回升的档位
    for i in list(ls["fired"]):
        if i >= len(rois):
            ls["fired"].remove(i)
            continue
        tier_pct = rois[i] * 100
        if loss * 100 < tier_pct - min(cfg.loss_rearm_gap, tier_pct / 2):
            ls["fired"].remove(i)
    reached = max((i for i, r in enumerate(rois) if loss >= r), default=-1)
    if reached < 0:
        return []
    if margin_u and loss * margin_u < cfg.min_alert_u:
        return []  # 金额太小，不发也不标记

    if ls.get("day") != _day(now):
        ls["day"], ls["day_count"] = _day(now), 0
    top_fired = max(ls["fired"], default=-1)
    escalation = top_fired >= 0 and reached > top_fired
    fresh = top_fired < 0
    repeat = (reached == len(rois) - 1 and reached <= top_fired and cfg.loss_top_tier_repeat_hours > 0
              and now - ls.get("top_ts", 0) >= cfg.loss_top_tier_repeat_hours * 3600)
    if not (escalation or fresh or repeat):
        return []
    last = ls.get("last_ts") or None  # 从未通知过：不受冷却限制
    if not escalation and last is not None and now - last < cfg.loss_cooldown_minutes * 60:
        return []
    if ls.get("day_count", 0) >= cfg.loss_max_per_day:
        return []

    for i in range(reached + 1):
        if i not in ls["fired"]:
            ls["fired"].append(i)
    ls["fired"].sort()
    ls["last_ts"] = now
    ls["day_count"] = ls.get("day_count", 0) + 1
    if reached == len(rois) - 1:
        ls["top_ts"] = now

    n = len(rois)
    tier_roi = rois[reached]
    mode = f"（价格反向 {cfg.loss_tiers[reached]:g}% × {max(pos.leverage, 1):g}x）" if cfg.loss_tiers_mode == "price" else ""
    u = f" ≈ {-loss * margin_u:+.2f}U" if margin_u else ""
    title = (f"仓位亏损达到第 {reached + 1}/{n} 档 -{tier_roi * 100:.0f}%{mode}："
             f"当前 ROI {pos.roi * 100:+.1f}%{u}")
    if repeat and not escalation and not fresh:
        title = "仍处于最高亏损档：" + title
    if pos.is_hedge:
        suggestion = "多空对冲仓合计亏损：检查两条腿，减掉亏损的一边或整体平仓"
    elif reached < n - 1:
        nxt = rois[reached + 1]
        suggestion = (f"请在币安该跟单仓位设置止损或手动减仓；下一档 -{nxt * 100:.0f}% 对应价格约 "
                      f"{_px(pos.price_at_roi(-nxt))}，可把止损设在此价之前")
    else:
        suggestion = "已达最高档：建议立即在币安手动平仓或减仓"
    sev = "warn" if reached == 0 else "urgent"
    return [Alert(f"L{reached + 1}", sev, pos.key, st.version, title,
                  f"开仓 {_px(pos.entry_price)}，现价 {_px(pos.mark_price)}", suggestion, now)]
