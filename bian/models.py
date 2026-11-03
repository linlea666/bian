"""标准化数据结构。所有 ROI 均为小数（0.2 = +20%），口径为“未实现盈亏 / 开仓保证金”，
开仓保证金 = |数量| × 开仓均价 / 杠杆，与币安跟单页面 ROI 口径一致（不含手续费）。"""
from __future__ import annotations

from dataclasses import dataclass, field


def fnum(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class Position:
    """一个当前持仓（领航员公开持仓，或我自己的跟单仓位）。"""

    leader_id: str
    symbol: str
    side: str  # "LONG" / "SHORT" / "HEDGE"（同币种多空合并后的净值视图）
    qty: float  # 绝对值；HEDGE 为多空数量之和
    entry_price: float
    mark_price: float
    leverage: float
    unrealized_pnl: float
    source: str  # "public" 领航员公开持仓 / "copy" 我的跟单仓位
    break_even_price: float = 0.0
    liquidation_price: float = 0.0
    initial_margin: float = 0.0  # 接口有就用，否则按开仓价反推
    update_time: int = 0  # 毫秒；接口没给则为 0

    @property
    def key(self) -> str:
        return f"{self.leader_id}:{self.symbol}:{self.side}"

    @property
    def direction(self) -> int:
        return {"LONG": 1, "SHORT": -1}.get(self.side, 0)  # HEDGE（多空合并）为 0

    @property
    def is_hedge(self) -> bool:
        return self.side == "HEDGE"

    @property
    def margin(self) -> float:
        if self.initial_margin > 0:
            return self.initial_margin
        if self.leverage > 0:
            return self.qty * self.entry_price / self.leverage
        return 0.0

    @property
    def roi(self) -> float:
        """按开仓保证金口径的浮动 ROI；保证金未知时按价格涨跌 × 杠杆估算。"""
        m = self.margin
        if m > 0:
            pnl = self.unrealized_pnl
            if pnl == 0 and self.mark_price > 0:
                pnl = (self.mark_price - self.entry_price) * self.qty * self.direction
            return pnl / m
        return 0.0

    def roi_at(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (price / self.entry_price - 1) * self.direction * max(self.leverage, 1)

    def price_at_roi(self, roi: float) -> float:
        """某个 ROI 对应的价格（用于给出止损/保本建议价）；多空合并仓没有单一价格，返回 0。"""
        if self.is_hedge or self.entry_price <= 0:
            return 0.0
        return self.entry_price * (1 + roi / max(self.leverage, 1) * self.direction)

    def liquidation_valid(self, max_distance: float = 0.5) -> bool:
        """强平价是否可信：必须在不利方向，且距标记价不超过 max_distance。
        实测全仓 + 双向持仓时接口常给 0 或离谱的值（如现价 514、强平价 739467）。"""
        liq, mark = self.liquidation_price, self.mark_price
        if liq <= 0 or mark <= 0 or self.is_hedge:
            return False
        if self.side == "LONG" and not liq < mark:
            return False
        if self.side == "SHORT" and not liq > mark:
            return False
        return abs(mark - liq) / mark <= max_distance


@dataclass
class ClosedPosition:
    """领航员一笔已平仓位（来自公开 position-history）。"""

    leader_id: str
    position_id: str
    symbol: str
    side: str  # "LONG" / "SHORT"
    opened: int  # ms
    closed: int  # ms
    avg_cost: float
    avg_close: float
    max_qty: float
    closed_qty: float
    leverage: float
    pnl: float
    roi: float  # 币安给的 ROI（小数）
    margin_mode: str = "Cross"

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1

    @property
    def duration_hours(self) -> float:
        return max(0, self.closed - self.opened) / 3_600_000

    @property
    def price_roi(self) -> float:
        """仅用开/平均价算出的 ROI（无费用），与回测口径一致。"""
        if self.avg_cost <= 0:
            return 0.0
        return (self.avg_close / self.avg_cost - 1) * self.direction * max(self.leverage, 1)

    @property
    def had_adds(self) -> bool:
        """最大持仓量与平仓量不一致，或多次加减仓时开仓均价不是首仓价——回测精度下降。"""
        return self.max_qty > 0 and abs(self.closed_qty - self.max_qty) / self.max_qty > 0.01


@dataclass
class Leader:
    portfolio_id: str
    nickname: str = ""
    roi: float = 0.0  # 区间 ROI（百分数，接口原值）
    pnl: float = 0.0
    mdd: float = 0.0  # 最大回撤（百分数）
    win_rate: float = 0.0  # 百分数
    aum: float = 0.0
    copier_pnl: float = 0.0
    current_copy_count: int = 0
    max_copy_count: int = 0
    position_show: bool | None = None
    raw: dict = field(default_factory=dict, repr=False)
