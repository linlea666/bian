"""币安 U 本位永续（USDT-M）只读接口。

签名：查询串（含 timestamp、recvWindow）做 HMAC-SHA256，十六进制附在 signature 参数；key 放 X-MBX-APIKEY 头。
接口：
- 持仓 /fapi/v3/positionRisk（只要数量不为 0 的；单向持仓 positionSide=BOTH 按数量正负判断方向）
- 账户 /fapi/v3/account（总权益、可用、维持保证金）
- 挂单 /fapi/v1/openOrders + /fapi/v1/openAlgoOrders（条件单迁到算法单服务后在这里；接口不存在时忽略）
- 流水 /fapi/v1/income（已实现盈亏、手续费、资金费、划转；最多 3 个月）
- 权限 api.binance.com /sapi/v1/account/apiRestrictions
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse

from ..models import Position, fnum
from . import AccountInfo, ExchangeError, LedgerRow, Permissions, StopOrder
from .http import SignedHttp

FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"

AUTH_CODES = {-2014, -2015, -1022, -2008}  # key 格式 / key 无效、IP 或权限不符 / 签名不对 / key 不存在
STOP_TYPES = {"STOP", "STOP_MARKET", "TRAILING_STOP_MARKET"}
TP_TYPES = {"TAKE_PROFIT", "TAKE_PROFIT_MARKET"}
INCOME_KINDS = {"REALIZED_PNL": "realized", "COMMISSION": "fee", "FUNDING_FEE": "funding", "TRANSFER": "transfer"}


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def classify(status: int, body) -> ExchangeError | None:
    if status == 429 or status == 418:
        return ExchangeError("rate", f"HTTP {status}", status=status)
    if status == 451:
        return ExchangeError("region", "HTTP 451", status=status)
    if not isinstance(body, dict) or "code" not in body or status < 400 and int(fnum(body.get("code"), 0)) >= 0:
        return None
    code = int(fnum(body.get("code"), 0))
    msg = str(body.get("msg") or "")
    if code == -2015 and "ip" in msg.lower():
        kind = "ip"
    elif code in AUTH_CODES:
        kind = "auth"
    elif code == -1021:
        kind = "time"
    elif code in (-1003, -1015):
        kind = "rate"
    elif code in (-1001, -1000, -1016):
        kind = "maintenance"
    elif code == -2011 or "permission" in msg.lower():
        kind = "perm"
    else:
        kind = "other"
    return ExchangeError(kind, f"{code} {msg}", code=str(code), status=status)


def side_of(position_side: str, amt: float) -> str:
    if position_side in ("LONG", "SHORT"):
        return position_side
    return "LONG" if amt > 0 else "SHORT"


def parse_position(x: dict, account_id: str) -> Position | None:
    amt = fnum(x.get("positionAmt"))
    if amt == 0:
        return None
    notional = abs(fnum(x.get("notional")))
    im = fnum(x.get("positionInitialMargin")) or fnum(x.get("initialMargin")) or fnum(x.get("isolatedMargin"))
    lev = fnum(x.get("leverage")) or (notional / im if im > 0 else 0.0)  # v3 不再返回 leverage，按名义价值 / 保证金反推
    return Position(
        leader_id=account_id, symbol=str(x.get("symbol")), side=side_of(str(x.get("positionSide") or "BOTH"), amt),
        qty=abs(amt), entry_price=fnum(x.get("entryPrice")), mark_price=fnum(x.get("markPrice")),
        leverage=round(lev, 2), unrealized_pnl=fnum(x.get("unRealizedProfit")), source="exchange",
        break_even_price=fnum(x.get("breakEvenPrice")), liquidation_price=fnum(x.get("liquidationPrice")),
        initial_margin=im, update_time=int(fnum(x.get("updateTime"))))


def parse_stop(x: dict) -> StopOrder | None:
    otype = str(x.get("type") or x.get("orderType") or "")
    if otype not in STOP_TYPES | TP_TYPES:
        return None
    ps, oside = str(x.get("positionSide") or "BOTH"), str(x.get("side") or "")
    if ps in ("LONG", "SHORT"):  # 双向持仓：卖出 + LONG = 平多，买入 + SHORT = 平空；反过来是开仓条件单
        closing, side = (ps == "LONG") == (oside == "SELL"), ps
    else:  # 单向持仓：只认只减仓 / 全部平仓的单；卖出保护多单，买入保护空单
        closing = bool(x.get("closePosition")) or bool(x.get("reduceOnly"))
        side = "LONG" if oside == "SELL" else "SHORT"
    if not closing:
        return None  # 开仓用的条件单，不是保护单
    trigger = fnum(x.get("stopPrice")) or fnum(x.get("triggerPrice")) or fnum(x.get("activatePrice"))
    qty = 0.0 if x.get("closePosition") else fnum(x.get("origQty")) or fnum(x.get("quantity"))
    return StopOrder(str(x.get("symbol")), side, trigger, qty, "stop" if otype in STOP_TYPES else "take_profit")


class BinanceFutures:
    exchange = "binance"

    def __init__(self, account_id: str, api_key: str, secret: str, base_url: str | None = None, http=None, sapi=None):
        self.account_id, self.api_key, self.secret = account_id, api_key, secret
        self.http = http or SignedHttp(base_url or FAPI)
        self.sapi = sapi or SignedHttp(SAPI)

    # ------------------------------------------------------------ 请求
    def _server_ms(self) -> int:
        _, body = self.http.get(f"{self.http.base}/fapi/v1/time", classify=classify)
        return int(body["serverTime"])

    def _signed(self, path: str, params: dict | None = None, http: SignedHttp | None = None):
        http = http or self.http
        http.sync_time(self._server_ms)
        q = dict(params or {})
        q.update(timestamp=http.now_ms(), recvWindow=10000)
        query = urllib.parse.urlencode(q)
        url = f"{http.base}{path}?{query}&signature={sign(self.secret, query)}"
        _, body = http.get(url, {"X-MBX-APIKEY": self.api_key}, classify)
        return body

    # ------------------------------------------------------------ 数据
    def positions(self) -> list[Position]:
        rows = self._signed("/fapi/v3/positionRisk") or []
        return [p for p in (parse_position(x, self.account_id) for x in rows) if p]

    def account(self) -> AccountInfo:
        a = self._signed("/fapi/v3/account") or {}
        equity = fnum(a.get("totalMarginBalance"))
        maint = fnum(a.get("totalMaintMargin"))
        return AccountInfo(equity=equity, available=fnum(a.get("availableBalance")),
                           unrealized=fnum(a.get("totalUnrealizedProfit")), maint_margin=maint,
                           margin_ratio=(maint / equity) if equity > 0 else None)

    def stop_orders(self) -> list[StopOrder]:
        rows = list(self._signed("/fapi/v1/openOrders") or [])
        try:
            algo = self._signed("/fapi/v1/openAlgoOrders") or []
            rows += algo.get("orders", []) if isinstance(algo, dict) else list(algo)
        except ExchangeError as e:
            if e.kind not in ("other", "perm"):  # 接口不存在 / 不支持：忽略；网络、限频等照常上抛
                raise
        return [s for s in (parse_stop(x) for x in rows) if s]

    def ledger(self, since_ms: int, until_ms: int) -> list[LedgerRow]:
        """[since, until) 内的流水，按 7 天一个窗口翻页（接口单次最多 1000 条）。"""
        out: list[LedgerRow] = []
        start = since_ms
        while start < until_ms:
            end = min(until_ms, start + 7 * 86400_000)
            cursor = start
            while True:
                rows = self._signed("/fapi/v1/income", {"startTime": cursor, "endTime": end - 1, "limit": 1000}) or []
                for x in rows:
                    kind = INCOME_KINDS.get(str(x.get("incomeType")))
                    if kind and str(x.get("asset") or "USDT") == "USDT":
                        out.append(LedgerRow(f"bn:{x.get('tranId')}:{x.get('incomeType')}:{x.get('tradeId', '')}",
                                             int(fnum(x.get("time"))), kind, str(x.get("symbol") or ""),
                                             fnum(x.get("income"))))
                if len(rows) < 1000:
                    break
                cursor = int(fnum(rows[-1].get("time"))) + 1
            start = end
        return out

    def permissions(self) -> Permissions:
        d = self._signed("/sapi/v1/account/apiRestrictions", http=self.sapi) or {}
        trade = any(bool(d.get(k)) for k in ("enableFutures", "enableSpotAndMarginTrading", "enableMargin",
                                              "enableVanillaOptions", "enablePortfolioMarginTrading"))
        withdraw = any(bool(d.get(k)) for k in ("enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer"))
        return Permissions(can_read=bool(d.get("enableReading", True)), can_trade=trade, can_withdraw=withdraw,
                           ip_restricted=bool(d.get("ipRestrict")), detail=d)
