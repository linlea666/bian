"""把同一领航员、同一币种的多空两条腿合并成一个“净敞口”视图。

实测领航员普遍双向持仓（同币种多空并存），单看一条腿的 ROI 会误报：
例如 BCH 多单 -31%、空单 +44%，合并后实际是盈利。规则引擎对合并后的 HEDGE 仓评估亏损，
单腿仍保留在明细里。
"""
from __future__ import annotations

from collections import defaultdict

from .models import Position


def net_by_symbol(positions: list[Position]) -> list[Position]:
    groups: dict[tuple[str, str], list[Position]] = defaultdict(list)
    for p in positions:
        groups[(p.leader_id, p.symbol)].append(p)
    out: list[Position] = []
    for (leader, symbol), legs in groups.items():
        sides = {p.side for p in legs}
        if len(legs) == 1 or not {"LONG", "SHORT"} <= sides:
            out.extend(legs)
            continue
        long_q = sum(p.qty for p in legs if p.side == "LONG")
        short_q = sum(p.qty for p in legs if p.side == "SHORT")
        out.append(Position(
            leader_id=leader,
            symbol=symbol,
            side="HEDGE",
            qty=long_q + short_q,
            entry_price=0.0,
            mark_price=legs[0].mark_price,
            leverage=max(p.leverage for p in legs),
            unrealized_pnl=sum(p.unrealized_pnl for p in legs),
            source=legs[0].source,
            initial_margin=sum(p.margin for p in legs),
            update_time=max(p.update_time for p in legs),
        ))
    return out


def describe_legs(positions: list[Position], symbol: str) -> str:
    parts = []
    for p in positions:
        if p.symbol == symbol and p.side in ("LONG", "SHORT"):
            parts.append(f"{'多' if p.side == 'LONG' else '空'} {p.qty:g}@{p.entry_price:.6g} ROI {p.roi * 100:+.1f}%")
    return "；".join(parts)
