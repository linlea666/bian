"""交易所账户（API Key 只读）：币安 U 本位永续、OKX USDT 永续。

只做数据接入：把交易所的持仓、账户、止损单、资金流水统一成项目里已有的结构（Position 等），
之后的风控规则、提醒、邮件、作战室、统计全部复用现有逻辑。系统永远不下单，只接受只读 key。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..client import BinanceError

EXCHANGES = {"binance": "币安", "okx": "OKX"}


class ExchangeError(BinanceError):
    """交易所接口错误。kind 用于页面提示和“无法监控”提醒：
    auth 密钥无效 / ip 不在白名单 / perm 权限不足 / time 时间不同步 / rate 限频 /
    region 地区限制 / network 网络不通 / maintenance 维护 / other 其他。"""

    HINTS = {
        "auth": "API Key 或 Secret 不正确（OKX 还要核对 Passphrase），或者 key 已被删除",
        "ip": "服务器 IP 不在这个 key 的白名单里：到交易所的 API 管理里把服务器 IP 加进去",
        "perm": "这个 key 没有读取合约账户的权限：创建 key 时勾选“读取”",
        "time": "服务器时间和交易所相差太大：检查服务器时间同步（timedatectl）",
        "rate": "请求太频繁被交易所限流，稍后自动重试",
        "region": "服务器所在地区被交易所限制访问（HTTP 451/403）",
        "network": "连不上交易所接口，检查服务器网络",
        "maintenance": "交易所维护中，稍后自动重试",
        "other": "交易所返回错误",
    }

    def __init__(self, kind: str, message: str, code: str | None = None, status: int | None = None):
        super().__init__(f"{self.HINTS.get(kind, '')}（{message}）" if kind in self.HINTS else message, code, status)
        self.kind = kind


@dataclass
class AccountInfo:
    equity: float  # 合约账户权益（USDT，含浮动盈亏）
    available: float  # 可用于开仓
    unrealized: float
    maint_margin: float  # 维持保证金
    margin_ratio: float | None  # 维持保证金率 = 维持保证金 / 权益（越高越危险，100% 爆仓）；不可得为 None


@dataclass
class StopOrder:
    symbol: str  # BTCUSDT
    side: str  # 保护的是哪个方向的仓位：LONG / SHORT（单向持仓时按下单方向推断）
    trigger: float  # 触发价
    qty: float  # 0 = 全部平仓（closePosition）
    kind: str  # stop 止损 / take_profit 止盈


@dataclass
class LedgerRow:
    uid: str
    ts_ms: int
    kind: str  # realized 已实现盈亏 / fee 手续费 / funding 资金费 / transfer 划转（转入为正）
    symbol: str
    amount: float


@dataclass
class Permissions:
    can_read: bool
    can_trade: bool
    can_withdraw: bool
    ip_restricted: bool | None = None
    detail: dict = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        return self.can_read and not self.can_trade and not self.can_withdraw


def make_adapter(exchange: str, account_id: str, keys: dict, base_url: str | None = None):
    if exchange == "binance":
        from .binance import BinanceFutures
        return BinanceFutures(account_id, keys["api_key"], keys["secret"], base_url=base_url)
    if exchange == "okx":
        from .okx import OkxSwap
        return OkxSwap(account_id, keys["api_key"], keys["secret"], keys.get("passphrase", ""), base_url=base_url)
    raise ValueError(f"不支持的交易所：{exchange}")


def classify_stops(positions, stops: list[StopOrder]) -> dict[str, tuple[float, float]]:
    """每个仓位（key）→ (止损触发价, 覆盖比例)。只算同币种、同方向、价格在不利方向的止损单；
    触发价取离现价最近的一张；覆盖比例 = 这些止损单的数量合计 ÷ 仓位数量（全部平仓的单算 100%），最多 1。
    没有止损单记为 (0, 0)。"""
    out = {}
    for p in positions:
        best, covered = 0.0, 0.0
        for s in stops:
            if s.symbol != p.symbol or s.side != p.side or s.kind != "stop" or s.trigger <= 0:
                continue
            adverse = s.trigger < p.mark_price if p.side == "LONG" else s.trigger > p.mark_price
            if not adverse:
                continue
            covered += p.qty if s.qty <= 0 else s.qty
            if not best or abs(p.mark_price - s.trigger) < abs(p.mark_price - best):
                best = s.trigger
        out[p.key] = (best, min(1.0, covered / p.qty) if p.qty > 0 else 0.0)
    return out
