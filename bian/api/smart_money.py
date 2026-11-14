"""币安“聪明钱”（smart-money）公开实盘：主页资料、当前持仓。只读。

- 主页资料 /bapi/asset/v1/friendly/future/smart-money/profile?topTraderId=  公开接口，不需要登录。
- 当前持仓 /bapi/asset/v1/private/future/smart-money/profile/query-positions  需要网页登录凭证（p20t + csrftoken），
  只有对方开启了“分享持仓”（sharingPosition）才有数据；每页最多 9 条，按页翻完。
主页 ID（topTraderId）是 19 位数字，和跟单领航员的 portfolioId 是两套 ID。
"""
from __future__ import annotations

import re
import urllib.parse

from ..client import BinanceError, Client
from ..models import Position, fnum

PROFILE = "/bapi/asset/v1/friendly/future/smart-money/profile"
POSITIONS = "/bapi/asset/v1/private/future/smart-money/profile/query-positions"
PROFILE_URL = "https://www.binance.com/zh-CN/smart-money/profile/{}"
ROWS = 9  # 接口单页上限
MAX_PAGES = 30


def normalize_id(text: str) -> str:
    """接受主页链接（…/smart-money/profile/<ID>）或 19 位数字 ID，返回 ID；不合法抛 ValueError。"""
    s = (text or "").strip()
    if "/" in s or s.startswith("http"):
        u = urllib.parse.urlparse(s if "://" in s else "https://" + s)
        host = (u.hostname or "").lower()
        if host != "binance.com" and not host.endswith(".binance.com"):
            raise ValueError("只支持 binance.com 的聪明钱主页链接")
        m = re.search(r"/smart-money/profile/(\d+)", u.path)
        if not m:
            raise ValueError("链接里没找到主页 ID，格式应为 …/smart-money/profile/19位数字")
        s = m.group(1)
    if not re.fullmatch(r"\d{19}", s):
        raise ValueError("主页 ID 应为 19 位数字（或直接粘贴主页链接）")
    return s


def profile(client: Client, top_id: str) -> dict:
    """主页资料（公开）。返回精简后的字段，供页面展示和关注时保存名字。"""
    d = client.get(PROFILE, {"topTraderId": top_id}) or {}
    if not d.get("topTraderId") and not d.get("traderName"):
        raise BinanceError("没有找到这个聪明钱主页（ID 不存在或已关闭）")
    return {
        "id": str(d.get("topTraderId") or top_id),
        "name": str(d.get("traderName") or d.get("accountName") or top_id),
        "avatar": d.get("avatarUrl") or "",
        "intro": d.get("introduction") or "",
        "enabled": d.get("enable") is not False,
        "sharing_position": bool(d.get("sharingPosition")),
        "roi": fnum(d.get("roi")),
        "pnl": fnum(d.get("pnl")),
        "mdd": fnum(d.get("mdd")),
        "win_rate": fnum(d.get("winRate")),
        "balance": fnum(d.get("umMarginBalance")),
        "days": int(fnum(d.get("daysActive"))),
        "subscribers": int(fnum(d.get("subscribers"))),
        "url": PROFILE_URL.format(top_id),
    }


def _rows(data) -> tuple[list, int | None]:
    if isinstance(data, list):
        return data, None
    if not isinstance(data, dict):
        return [], None
    total = next((int(fnum(data[k])) for k in ("total", "totalCount", "count") if k in data), None)
    for k in ("positions", "rows", "list", "items", "data"):
        if isinstance(data.get(k), list):
            return data[k], total
    return [], total


def parse_position(x: dict, top_id: str) -> Position | None:
    amount = fnum(x.get("amount"))
    if amount == 0:
        return None
    side = "LONG" if amount > 0 else "SHORT"  # 数量正负是方向依据
    declared = str(x.get("side") or "").upper()
    if declared in ("LONG", "SHORT") and declared != side:
        raise BinanceError(f"聪明钱持仓 {x.get('symbol')} 方向字段和数量正负不一致，接口格式可能变了")
    return Position(
        leader_id=top_id, symbol=str(x.get("symbol") or "").upper(), side=side, qty=abs(amount),
        entry_price=fnum(x.get("entryPrice")), mark_price=fnum(x.get("markPrice")),
        leverage=fnum(x.get("leverage")), unrealized_pnl=fnum(x.get("pnl")), source="smart",
        liquidation_price=fnum(x.get("liqPrice")), initial_margin=fnum(x.get("margin")))


def positions(client: Client, top_id: str) -> list[Position]:
    """当前 U 本位持仓（需要网页凭证）。翻页直到取够 total 或某页不满；翻页异常（重复页 / 数量对不上）直接报错，
    宁可本轮不判断，也不拿不完整的列表去判断“平仓”。"""
    out: list[Position] = []
    seen: set[str] = set()
    n_rows = 0
    for page in range(1, MAX_PAGES + 1):
        data = client.get(POSITIONS, {"topTraderId": top_id, "marketType": "UM", "page": page, "rows": ROWS},
                          private=True)
        rows, total = _rows(data)
        n_rows += len(rows)
        for x in rows:
            p = parse_position(x, top_id)
            if p is None:
                continue
            if p.key in seen:
                raise BinanceError("聪明钱持仓翻页出现重复，本轮跳过")
            seen.add(p.key)
            out.append(p)
        if total is not None and n_rows >= total:
            break
        if len(rows) < ROWS:
            if total is not None and n_rows < total:
                raise BinanceError(f"聪明钱持仓翻页不完整（{n_rows}/{total}），本轮跳过")
            break
    return out
