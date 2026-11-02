"""领航员公开数据：榜单、详情、公开持仓、已平仓历史。"""
from __future__ import annotations

from ..client import Client
from ..models import ClosedPosition, Leader, Position, fnum

P = "/bapi/futures/v1/friendly/future/copy-trade"


def leaderboard(
    client: Client,
    time_range: str = "30D",
    sort_by: str = "ROI",
    page: int = 1,
    page_size: int = 20,
    nickname: str = "",
) -> tuple[int, list[Leader]]:
    """sort_by: ROI / PNL / MDD / WIN_RATE / AUM / COPIER_PNL / SHARP_RATIO。"""
    data = client.post(
        P + "/home-page/query-list",
        {
            "pageNumber": page,
            "pageSize": page_size,
            "timeRange": time_range,
            "dataType": sort_by,
            "favoriteOnly": False,
            "hideFull": False,
            "nickname": nickname,
            "order": "DESC",
            "userAsset": 0,
            "portfolioType": "PUBLIC",
            "useAiRecommended": False,
        },
    )
    leaders = [
        Leader(
            portfolio_id=str(x.get("leadPortfolioId")),
            nickname=x.get("nickname") or "",
            roi=fnum(x.get("roi")),
            pnl=fnum(x.get("pnl")),
            mdd=fnum(x.get("mdd")),
            win_rate=fnum(x.get("winRate")),
            aum=fnum(x.get("aum")),
            copier_pnl=fnum(x.get("copierPnl")),
            current_copy_count=int(fnum(x.get("currentCopyCount"))),
            max_copy_count=int(fnum(x.get("maxCopyCount"))),
            raw=x,
        )
        for x in (data or {}).get("list") or []
    ]
    return int((data or {}).get("total") or 0), leaders


def detail(client: Client, portfolio_id: str, private: bool = False) -> dict:
    """领航员详情。带凭证（private=True）时额外返回我对该领航员的 copyPortfolioId。"""
    return client.get(
        P + "/lead-portfolio/detail", {"portfolioId": portfolio_id}, private=private
    ) or {}


def leader_from_detail(d: dict) -> Leader:
    return Leader(
        portfolio_id=str(d.get("leadPortfolioId")),
        nickname=d.get("nickname") or "",
        aum=fnum(d.get("aumAmount")),
        copier_pnl=fnum(d.get("copierPnl")),
        current_copy_count=int(fnum(d.get("currentCopyCount"))),
        max_copy_count=int(fnum(d.get("maxCopyCount"))),
        position_show=d.get("positionShow"),
        raw=d,
    )


def parse_position(x: dict, leader_id: str, source: str) -> Position | None:
    amount = fnum(x.get("positionAmount"))
    if amount == 0:
        return None
    side = str(x.get("positionSide") or "BOTH").upper()
    if side not in ("LONG", "SHORT"):
        side = "LONG" if amount > 0 else "SHORT"
    entry = fnum(x.get("entryPrice"))
    notional = abs(fnum(x.get("notionalValue")))
    initial_margin = fnum(x.get("positionInitialMargin")) or fnum(x.get("initialMargin"))
    lev = fnum(x.get("leverage"))
    if lev <= 0 and initial_margin > 0 and notional > 0:
        lev = round(notional / initial_margin)  # 私有接口常不给 leverage，按名义价值/保证金反推
    if initial_margin <= 0 and lev > 0:
        initial_margin = 0.0  # 交给 Position.margin 按开仓价反推，口径与页面一致
    return Position(
        leader_id=leader_id,
        symbol=str(x.get("symbol")),
        side=side,
        qty=abs(amount),
        entry_price=entry,
        mark_price=fnum(x.get("markPrice")),
        leverage=lev,
        unrealized_pnl=fnum(x.get("unrealizedProfit")),
        source=source,
        break_even_price=fnum(x.get("breakEvenPrice")),
        liquidation_price=fnum(x.get("liquidationPrice")),
        initial_margin=initial_margin,
        update_time=int(fnum(x.get("updateTime"))),
    )


def public_positions(client: Client, portfolio_id: str) -> list[Position]:
    """领航员公开持仓。仅当领航员开启了“展示持仓”(positionShow=true) 时有数据；
    否则接口返回空列表——空列表不代表空仓，调用方需结合 positionShow 判断。"""
    rows = client.get(P + "/lead-data/positions", {"portfolioId": portfolio_id}) or []
    out = []
    for x in rows:
        p = parse_position(x, portfolio_id, "public")
        if p:
            out.append(p)
    return out


def parse_closed(x: dict, leader_id: str) -> ClosedPosition:
    return ClosedPosition(
        leader_id=leader_id,
        position_id=str(x.get("positionId") or x.get("id")),
        symbol=str(x.get("symbol")),
        side=str(x.get("side") or "").upper(),
        opened=int(fnum(x.get("opened"))),
        closed=int(fnum(x.get("closed")) or fnum(x.get("updateTime"))),
        avg_cost=fnum(x.get("avgCost")),
        avg_close=fnum(x.get("avgClosePrice")),
        max_qty=fnum(x.get("maxOpenInterest")),
        closed_qty=fnum(x.get("closedVolume")),
        leverage=fnum(x.get("leverage"), 1.0),
        pnl=fnum(x.get("closingPnl")),
        roi=fnum(x.get("roi")),
        margin_mode=str(x.get("isolated") or ""),
    )


def position_history(
    client: Client, portfolio_id: str, max_pages: int = 20, page_size: int = 100
) -> list[ClosedPosition]:
    """领航员全部已平仓位（只保留 status=All Closed），按开仓时间升序。"""
    out: list[ClosedPosition] = []
    for page in range(1, max_pages + 1):
        data = client.post(
            P + "/lead-portfolio/position-history",
            {"pageNumber": page, "pageSize": page_size, "portfolioId": portfolio_id},
        ) or {}
        rows = data.get("list") or []
        for x in rows:
            if str(x.get("status")) == "All Closed" and x.get("closed"):
                out.append(parse_closed(x, portfolio_id))
        if len(rows) < page_size or page * page_size >= int(data.get("total") or 0):
            break
    out.sort(key=lambda c: c.opened)
    return out
