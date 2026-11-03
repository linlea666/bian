"""跟单诊断：
- 偏差（deviation）：我的仓位相对领航员是否超配/低配/漏单/残留，以及实际杠杆对比 —— “盈利时为什么没跟上”。
- 损耗（costs）：手续费、分润、开平仓价相对领航员的滑点 —— “跟单比领航员少赚多少”。
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from .api import copy_account, leaders as leaders_api
from .client import BinanceError, Client
from .models import ClosedPosition, Position, fnum

OVER, UNDER = 2.0, 0.5


@dataclass
class LegDiff:
    symbol: str
    side: str
    my_qty: float
    leader_qty: float
    factor: float  # (我的数量/领航员数量) ÷ (我的权益/领航员权益)；1 = 完全按比例
    kind: str  # ok / over / under / missing / residual
    my_notional: float = 0.0


@dataclass
class Deviation:
    nickname: str
    lead_id: str
    my_equity: float
    leader_equity: float
    position_show: bool | None
    my_notional: float
    leader_notional: float
    legs: list[LegDiff] = field(default_factory=list)

    @property
    def equity_ratio(self) -> float:
        return self.my_equity / self.leader_equity if self.leader_equity > 0 else 0.0

    @property
    def my_eff_leverage(self) -> float:
        return self.my_notional / self.my_equity if self.my_equity > 0 else 0.0

    @property
    def leader_eff_leverage(self) -> float:
        return self.leader_notional / self.leader_equity if self.leader_equity > 0 else 0.0


def _notional(p: Position) -> float:
    return p.qty * (p.mark_price or p.entry_price)


def compare_legs(mine: list[Position], theirs: list[Position], equity_ratio: float) -> list[LegDiff]:
    m = {(p.symbol, p.side): p for p in mine}
    t = {(p.symbol, p.side): p for p in theirs}
    out = []
    for k in sorted(set(m) | set(t)):
        mp, tp = m.get(k), t.get(k)
        if mp and tp:
            factor = (mp.qty / tp.qty) / equity_ratio if tp.qty and equity_ratio else 0.0
            kind = "over" if factor > OVER else "under" if factor < UNDER else "ok"
            out.append(LegDiff(k[0], k[1], mp.qty, tp.qty, factor, kind, _notional(mp)))
        elif tp:
            out.append(LegDiff(k[0], k[1], 0.0, tp.qty, 0.0, "missing"))
        else:
            out.append(LegDiff(k[0], k[1], mp.qty, 0.0, 0.0, "residual", _notional(mp)))  # type: ignore[union-attr]
    return out


def deviation(client: Client) -> list[Deviation]:
    res = []
    for r in copy_account.copy_relations(client):
        lead, cpid = str(r.get("leadPortfolioId")), str(r.get("copyPortfolioId"))
        d = leaders_api.detail(client, lead)
        mine = copy_account.copy_positions(client, cpid, lead)
        theirs: list[Position] = []
        if d.get("positionShow"):
            theirs = leaders_api.public_positions(client, lead)
        dv = Deviation(
            nickname=str(r.get("nickname") or lead), lead_id=lead,
            my_equity=fnum(r.get("marginBalance")), leader_equity=fnum(d.get("marginBalance")),
            position_show=d.get("positionShow"),
            my_notional=sum(_notional(p) for p in mine), leader_notional=sum(_notional(p) for p in theirs),
        )
        if theirs or not d.get("positionShow"):
            dv.legs = compare_legs(mine, theirs, dv.equity_ratio) if theirs else []
        res.append(dv)
    return res


KIND_CN = {"ok": "正常", "over": "超配", "under": "低配", "missing": "漏单", "residual": "残留"}


def format_deviation(devs: list[Deviation]) -> str:
    L = ["# 跟单偏差报告", "",
         "倍数 = (我的数量/领航员数量) ÷ (我的权益/领航员权益)。1 表示完全按比例；>2 超配（风险比领航员大），"
         "<0.5 低配或漏单（盈利时跟不上）；“残留”= 我还持有但领航员已经没有的仓位。", ""]
    for d in devs:
        L.append(f"## {d.nickname}")
        L.append(f"- 我的跟单权益 {d.my_equity:.2f}U，领航员权益 {d.leader_equity:.0f}U，权益比 {d.equity_ratio:.4f}")
        lev_line = f"- 实际杠杆（名义价值/权益）：我 {d.my_eff_leverage:.2f}x"
        if d.position_show:
            lev_line += f"，领航员 {d.leader_eff_leverage:.2f}x"
            if d.leader_eff_leverage > 0 and d.my_eff_leverage > d.leader_eff_leverage * 1.5:
                lev_line += "  ⚠️ 我的整体风险明显高于领航员"
            elif d.leader_eff_leverage > 0 and d.my_eff_leverage < d.leader_eff_leverage * 0.5:
                lev_line += "  ⚠️ 我的仓位明显不足，盈利时会跟不上"
        L.append(lev_line)
        if not d.position_show:
            L.append("- 领航员未公开持仓，无法逐仓对比")
            L.append("")
            continue
        bad = [x for x in d.legs if x.kind != "ok"]
        if not d.legs:
            L.append("- 双方都没有持仓")
        elif not bad:
            L.append(f"- {len(d.legs)} 个仓位全部按比例跟上 ✅")
        else:
            L += ["", "| 币种 | 方向 | 我的数量 | 领航员数量 | 倍数 | 判断 |", "|---|---|---|---|---|---|"]
            for x in sorted(d.legs, key=lambda x: ("missing", "residual", "over", "under", "ok").index(x.kind)):
                if x.kind == "ok":
                    continue
                f = f"{x.factor:.2f}" if x.factor else "-"
                L.append(f"| {x.symbol} | {'多' if x.side == 'LONG' else '空'} | {x.my_qty:g} | {x.leader_qty:g} | {f} | {KIND_CN[x.kind]} |")
            ok = len(d.legs) - len(bad)
            if ok:
                L.append(f"\n其余 {ok} 个仓位按比例正常。")
        L.append("")
    L += ["## 怎么改善", "",
          "- 大量“超配/低配”通常是因为跟单金额太小，被最小下单量取整（例如 BTC 最小 0.001）。提高这位领航员的跟单金额，或换成定额模式。",
          "- “漏单”可能是：你开始跟单前领航员就已开的仓；或按比例算出的数量低于最小下单量被跳过；或可用保证金不足。",
          "- “残留”需要人工确认：领航员已平仓、你却还有仓位，币安不会再替你平掉。"]
    return "\n".join(L)


# ================================================================= 损耗
@dataclass
class Costs:
    nickname: str
    days: float  # 已跟单天数
    fills: int
    fees: float  # 手续费合计（正数=支出）
    realized: float  # 已实现盈亏（不含手续费）
    profit_shared: float  # 已分润
    matched: int
    entry_slip_bps: float | None  # 中位数，正数 = 我的开仓价比领航员差
    exit_slip_bps: float | None
    slip_u: float  # 开平滑点折合 U（估算）
    roi_gap: float | None  # 我的 ROI − 领航员 ROI 的中位数


def _closed_from_raw(x: dict, lead: str) -> ClosedPosition:
    return leaders_api.parse_closed(x, lead)


def match_one(symbol: str, side: str, opened_ms: int, candidates, used: set | None = None,
              window_ms: int = 15 * 60_000):
    """在候选里找“同币种 + 同方向 + 开仓时间最接近（±window）”的一个。candidates 需有 symbol/side/opened/position_id。"""
    best, gap = None, window_ms + 1
    for t in candidates:
        if (used and t.position_id in used) or t.symbol != symbol or t.side != side:
            continue
        g = abs(t.opened - opened_ms)
        if g < gap:
            best, gap = t, g
    return best if best is not None and gap <= window_ms else None


def match_positions(mine: list[ClosedPosition], theirs: list[ClosedPosition], window_ms: int = 15 * 60_000):
    """按币种+方向+开仓时间（±15 分钟）把我的仓位和领航员的仓位配对。"""
    used: set[str] = set()
    pairs = []
    for m in mine:
        best = match_one(m.symbol, m.side, m.opened, theirs, used, window_ms)
        if best is not None:
            used.add(best.position_id)
            pairs.append((m, best))
    return pairs


def slippage(m: ClosedPosition, t: ClosedPosition) -> tuple[float, float]:
    """返回 (开仓滑点, 平仓滑点)，比例；正数 = 对我不利。"""
    d = m.direction
    entry = (m.avg_cost / t.avg_cost - 1) * d if t.avg_cost else 0.0
    exit_ = (t.avg_close / m.avg_close - 1) * d if m.avg_close and t.avg_close else 0.0
    return entry, exit_


def costs(client: Client) -> list[Costs]:
    out = []
    for r in copy_account.copy_relations(client):
        lead, cpid = str(r.get("leadPortfolioId")), str(r.get("copyPortfolioId"))
        fills = copy_account.copy_trade_history(client, cpid)
        mine_raw = copy_account.copy_position_history(client, cpid)
        mine = [_closed_from_raw(x, lead) for x in mine_raw if x.get("status") == "All Closed" and x.get("closed")]
        try:
            theirs = leaders_api.position_history(client, lead, max_pages=5)
        except BinanceError:
            theirs = []
        pairs = match_positions(mine, theirs)
        entries, exits, gaps, slip_u = [], [], [], 0.0
        for m, t in pairs:
            e, x = slippage(m, t)
            entries.append(e)
            exits.append(x)
            slip_u += (e * m.avg_cost + x * m.avg_close) * m.closed_qty
            gaps.append(m.roi - t.roi)
        start = fnum(r.get("startDate"))
        out.append(Costs(
            nickname=str(r.get("nickname") or lead),
            days=(time.time() * 1000 - start) / 86_400_000 if start else 0.0,
            fills=len(fills),
            fees=-sum(fnum(f.get("fee")) for f in fills),
            realized=sum(fnum(f.get("realizedProfit")) for f in fills),
            profit_shared=fnum(r.get("profitSharedAmount")),
            matched=len(pairs),
            entry_slip_bps=statistics.median(entries) * 1e4 if entries else None,
            exit_slip_bps=statistics.median(exits) * 1e4 if exits else None,
            slip_u=slip_u,
            roi_gap=statistics.median(gaps) if gaps else None,
        ))
    return out


def format_costs(cs: list[Costs]) -> str:
    def bps(x):
        return "-" if x is None else f"{x:+.1f}"

    L = ["# 跟单损耗报告", "",
         "滑点 = 我的成交均价相对领航员均价，单位 bp（0.01%），正数表示对我不利。"
         "仓位按“币种 + 方向 + 开仓时间 ±15 分钟”配对；有加仓的仓位均价可能不完全可比。", "",
         "| 领航员 | 跟单天数 | 成交笔数 | 手续费U | 已实现盈亏U | 已分润U | 配对仓位 | 开仓滑点bp | 平仓滑点bp | 滑点折合U | ROI 差(中位) |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in cs:
        gap = "-" if c.roi_gap is None else f"{c.roi_gap * 100:+.1f}%"
        L.append(f"| {c.nickname} | {c.days:.1f} | {c.fills} | {c.fees:.2f} | {c.realized:+.2f} | {c.profit_shared:.2f} | {c.matched} | "
                 f"{bps(c.entry_slip_bps)} | {bps(c.exit_slip_bps)} | {c.slip_u:+.2f} | {gap} |")
    tf = sum(c.fees for c in cs)
    tr = sum(c.realized for c in cs)
    ts = sum(c.profit_shared for c in cs)
    L += ["", "注：刚开始跟单时同步建立的仓位，短时间内可能不出现在成交/历史记录中（实测跟单 3 小时内为 0 笔）。",
          f"配对仓位太少（<10）时滑点和 ROI 差没有统计意义。", "", f"合计：手续费 {tf:.2f}U，分润 {ts:.2f}U，已实现盈亏 {tr:+.2f}U → 扣除后 {tr - tf - ts:+.2f}U。"]
    return "\n".join(L)
