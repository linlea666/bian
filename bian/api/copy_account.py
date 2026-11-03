"""我的跟单账户（私有接口，需要 p20t + csrftoken）。全部只读。

数据模型（参考 nofx-1 copytrade/binance_provider.go）：
- leadPortfolioId：领航员主页 ID；copyPortfolioId：我跟单该领航员后生成的跟单关系 ID。
- user-position / trade-history / position-history 必须用 copyPortfolioId。
"""
from __future__ import annotations

from ..client import Client
from ..models import Position, fnum
from .leaders import detail, parse_position

PRIV = "/bapi/futures/v1/private/future/copy-trade"
USER_POSITION = "/bapi/futures/v6/private/future/user-data/user-position"
ACCOUNT_BASE_INFO = "/bapi/accounts/v1/private/account/get-user-base-info"


def check_auth(client: Client) -> bool:
    """凭证是否有效（只返回布尔，不暴露用户信息）。"""
    data = client.get(ACCOUNT_BASE_INFO, private=True) or {}
    return bool(str(data.get("userId") or "").strip())


def copy_relations(client: Client, ongoing: bool = True) -> list[dict]:
    """我当前所有跟单关系：leadPortfolioId、copyPortfolioId、nickname、marginBalance（我的跟单权益）、
    netCopyAmount（净投入）、unrealizedPnl、realizedPnl、copyMode、isPaused 等。"""
    data = client.get(
        PRIV + "/copy-portfolio/detail-list", {"ongoing": str(ongoing).lower()}, private=True
    )
    return list(data or [])


def resolve_copy_portfolio_id(client: Client, lead_portfolio_id: str) -> str | None:
    for r in copy_relations(client):
        if str(r.get("leadPortfolioId")) == str(lead_portfolio_id) and r.get("copyPortfolioId"):
            return str(r["copyPortfolioId"])
    d = detail(client, lead_portfolio_id, private=True)
    return str(d["copyPortfolioId"]) if d.get("copyPortfolioId") else None


def copy_positions(client: Client, copy_portfolio_id: str, lead_portfolio_id: str) -> list[Position]:
    """我在该跟单关系下的当前仓位（含强平价、保证金）。"""
    rows = client.post(
        USER_POSITION,
        {"copyTradeType": "COPY", "portfolioId": copy_portfolio_id},
        private=True,
    ) or []
    out = []
    for x in rows:
        p = parse_position(x, lead_portfolio_id, "copy")
        if p:
            out.append(p)
    return out


def iter_pages(client: Client, path: str, copy_portfolio_id: str, page_size: int = 50, max_pages: int = 20):
    """逐页返回私有历史接口的记录（最新的在前）。调用方可以在看到已同步的记录时提前停止。
    实测：分页参数是 pageNumber/pageSize（page/rows 会被忽略、固定返回 10 条）。"""
    got = 0
    for page in range(1, max_pages + 1):
        d = client.post(
            PRIV + path,
            {"copyTradeType": "COPY", "portfolioId": copy_portfolio_id, "pageNumber": page, "pageSize": page_size},
            private=True,
        ) or {}
        rows = d.get("list") or []
        got += len(rows)
        yield rows
        if len(rows) < page_size or got >= int(d.get("total") or 0):
            return


def _paged(client: Client, path: str, copy_portfolio_id: str, max_pages: int, page_size: int) -> list[dict]:
    out: list[dict] = []
    for rows in iter_pages(client, path, copy_portfolio_id, page_size, max_pages):
        out += rows
    return out


def copy_trade_history(client: Client, copy_portfolio_id: str, max_pages: int = 20, page_size: int = 50) -> list[dict]:
    """我的逐笔成交：time, symbol, side, positionSide, price, qty, quantity(USDT), fee(负数=支出), realizedProfit。"""
    return _paged(client, "/copy-portfolio/trade-history", copy_portfolio_id, max_pages, page_size)


def copy_position_history(client: Client, copy_portfolio_id: str, max_pages: int = 20, page_size: int = 50) -> list[dict]:
    """我的历史仓位（含部分平仓）：opened, closed, avgCost, avgClosePrice, maxOpenInterest, closedVolume,
    closingPnl, leverage, roi, side, status。"""
    return _paged(client, "/copy-portfolio/position-history", copy_portfolio_id, max_pages, page_size)


def relation_summary(r: dict) -> dict:
    return {
        "leadPortfolioId": r.get("leadPortfolioId"),
        "nickname": r.get("nickname"),
        "marginBalance": fnum(r.get("marginBalance")),
        "netCopyAmount": fnum(r.get("netCopyAmount")),
        "unrealizedPnl": fnum(r.get("unrealizedPnl")),
        "realizedPnl": fnum(r.get("realizedPnl")),
        "copyMode": r.get("copyMode"),
        "isPaused": r.get("isPaused"),
    }
