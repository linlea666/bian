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


def copy_trade_history(client: Client, copy_portfolio_id: str, page: int = 1, rows: int = 50) -> dict:
    return client.post(
        PRIV + "/copy-portfolio/trade-history",
        {"copyTradeType": "COPY", "portfolioId": copy_portfolio_id, "page": page, "rows": rows},
        private=True,
    ) or {}


def copy_position_history(client: Client, copy_portfolio_id: str, page: int = 1, rows: int = 50) -> dict:
    return client.post(
        PRIV + "/copy-portfolio/position-history",
        {"copyTradeType": "COPY", "portfolioId": copy_portfolio_id, "page": page, "rows": rows},
        private=True,
    ) or {}


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
