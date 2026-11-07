"""OKX USDT 永续（SWAP，线性合约）只读接口。

签名：timestamp(ISO 毫秒) + 方法 + 请求路径(含查询串) + 请求体，HMAC-SHA256 后 base64；
请求头 OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP / OK-ACCESS-PASSPHRASE。
接口：
- 持仓 /api/v5/account/positions?instType=SWAP（数量是“张”，乘以合约面值 ctVal 才是币的数量）
- 面值 /api/v5/public/instruments?instType=SWAP（公开接口，缓存一天）
- 账户 /api/v5/account/balance（adjEq / totalEq、mmr、USDT 可用）
- 止损 /api/v5/trade/orders-algo-pending（conditional、oco）+ 仓位上附带的 closeOrderAlgo
- 流水 /api/v5/account/bills-archive（近 3 个月；交易的已实现盈亏和手续费、资金费、划转）
- 权限 /api/v5/account/config 的 perm（read_only / trade / withdraw）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime, timezone

from ..models import Position, fnum
from . import AccountInfo, ExchangeError, LedgerRow, Permissions, StopOrder
from .http import SignedHttp

BASE = "https://www.okx.com"
AUTH_CODES = {"50111", "50112", "50113", "50105", "50100", "50101", "50103", "50104", "50106", "50107", "50119"}
IP_CODES = {"50110"}
PERM_CODES = {"50120", "50121", "50030"}
TIME_CODES = {"50102"}
RATE_CODES = {"50011", "50061"}
MAINT_CODES = {"50001", "50013", "50026"}


def sign(secret: str, ts: str, method: str, path: str, body: str = "") -> str:
    mac = hmac.new(secret.encode(), (ts + method + path + body).encode(), hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"


def classify(status: int, body) -> ExchangeError | None:
    if status == 429:
        return ExchangeError("rate", "HTTP 429", status=status)
    if not isinstance(body, dict):
        return None
    code = str(body.get("code", "0"))
    if code == "0":
        return None
    msg = f"{code} {body.get('msg') or ''}".strip()
    for kind, codes in (("auth", AUTH_CODES), ("ip", IP_CODES), ("perm", PERM_CODES), ("time", TIME_CODES),
                        ("rate", RATE_CODES), ("maintenance", MAINT_CODES)):
        if code in codes:
            return ExchangeError(kind, msg, code=code, status=status)
    return ExchangeError("other", msg, code=code, status=status)


def symbol_of(inst_id: str) -> str:
    return inst_id.replace("-SWAP", "").replace("-", "")  # BTC-USDT-SWAP → BTCUSDT


def is_usdt_swap(inst_id: str) -> bool:
    return inst_id.endswith("-USDT-SWAP")


def parse_position(x: dict, account_id: str, ct_val: float) -> Position | None:
    inst = str(x.get("instId") or "")
    pos = fnum(x.get("pos"))
    if not is_usdt_swap(inst) or pos == 0 or ct_val <= 0:
        return None
    ps = str(x.get("posSide") or "net")
    side = ps.upper() if ps in ("long", "short") else ("LONG" if pos > 0 else "SHORT")
    im = fnum(x.get("imr")) or fnum(x.get("margin"))  # 全仓用 imr，逐仓用 margin
    lev = fnum(x.get("lever"))
    return Position(
        leader_id=account_id, symbol=symbol_of(inst), side=side, qty=abs(pos) * ct_val,
        entry_price=fnum(x.get("avgPx")), mark_price=fnum(x.get("markPx")), leverage=lev,
        unrealized_pnl=fnum(x.get("upl")), source="exchange", break_even_price=fnum(x.get("bePx")),
        liquidation_price=fnum(x.get("liqPx")), initial_margin=im, update_time=int(fnum(x.get("uTime"))))


def _stop_side(x: dict) -> str:
    ps = str(x.get("posSide") or "net")
    if ps in ("long", "short"):
        return ps.upper()
    return "LONG" if str(x.get("side")) == "sell" else "SHORT"  # 单向持仓：卖出保护多单，买入保护空单


def parse_algo(x: dict, ct_val: float) -> list[StopOrder]:
    inst = str(x.get("instId") or "")
    if not is_usdt_swap(inst):
        return []
    side, sym = _stop_side(x), symbol_of(inst)
    qty = 0.0 if x.get("closeFraction") in ("1", 1) else fnum(x.get("sz")) * ct_val
    out = []
    if fnum(x.get("slTriggerPx")) > 0:
        out.append(StopOrder(sym, side, fnum(x.get("slTriggerPx")), qty, "stop"))
    if fnum(x.get("tpTriggerPx")) > 0:
        out.append(StopOrder(sym, side, fnum(x.get("tpTriggerPx")), qty, "take_profit"))
    return out


class OkxSwap:
    exchange = "okx"
    CT_TTL = 86400

    def __init__(self, account_id: str, api_key: str, secret: str, passphrase: str, base_url: str | None = None,
                 http=None):
        self.account_id, self.api_key, self.secret, self.passphrase = account_id, api_key, secret, passphrase
        self.http = http or SignedHttp(base_url or BASE)
        self._ct: dict[str, float] = {}
        self._ct_at = 0.0

    # ------------------------------------------------------------ 请求
    def _server_ms(self) -> int:
        _, body = self.http.get(f"{self.http.base}/api/v5/public/time", classify=classify)
        return int(body["data"][0]["ts"])

    def _signed(self, path: str, params: dict | None = None) -> list:
        self.http.sync_time(self._server_ms)
        q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        req_path = path + ("?" + urllib.parse.urlencode(q) if q else "")
        ts = iso_ms(self.http.now_ms())
        headers = {"OK-ACCESS-KEY": self.api_key, "OK-ACCESS-SIGN": sign(self.secret, ts, "GET", req_path),
                   "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase}
        _, body = self.http.get(self.http.base + req_path, headers, classify)
        return list((body or {}).get("data") or [])

    def ct_vals(self) -> dict[str, float]:
        if not self._ct or time.time() - self._ct_at > self.CT_TTL:
            _, body = self.http.get(f"{self.http.base}/api/v5/public/instruments?instType=SWAP", classify=classify)
            self._ct = {x["instId"]: fnum(x.get("ctVal")) for x in (body or {}).get("data") or []}
            self._ct_at = time.time()
        return self._ct

    # ------------------------------------------------------------ 数据
    def _raw_positions(self) -> list[dict]:
        return self._signed("/api/v5/account/positions", {"instType": "SWAP"})

    def positions(self) -> list[Position]:
        ct = self.ct_vals()
        return [p for p in (parse_position(x, self.account_id, ct.get(x.get("instId"), 0.0))
                            for x in self._raw_positions()) if p]

    def account(self) -> AccountInfo:
        d = (self._signed("/api/v5/account/balance") or [{}])[0]
        equity = fnum(d.get("adjEq")) or fnum(d.get("totalEq"))
        mmr = fnum(d.get("mmr"))
        usdt = next((x for x in d.get("details") or [] if x.get("ccy") == "USDT"), {})
        ratio = (mmr / equity) if equity > 0 and mmr > 0 else (1 / fnum(d.get("mgnRatio")) if fnum(d.get("mgnRatio")) > 0
                                                               else None)
        return AccountInfo(equity=equity, available=fnum(usdt.get("availEq")) or fnum(usdt.get("availBal")),
                           unrealized=fnum(d.get("upl")) or fnum(usdt.get("upl")), maint_margin=mmr,
                           margin_ratio=ratio)

    def stop_orders(self) -> list[StopOrder]:
        ct = self.ct_vals()
        out: list[StopOrder] = []
        for ord_type in ("conditional", "oco"):
            for x in self._signed("/api/v5/trade/orders-algo-pending", {"ordType": ord_type, "instType": "SWAP"}):
                out += parse_algo(x, ct.get(x.get("instId"), 0.0))
        for p in self._raw_positions():  # 下单时附带的止盈止损（挂在仓位上）
            for a in p.get("closeOrderAlgo") or []:
                out += parse_algo({**a, "instId": p.get("instId"), "posSide": p.get("posSide"),
                                   "side": "sell" if fnum(p.get("pos")) > 0 else "buy"}, ct.get(p.get("instId"), 0.0))
        return out

    def ledger(self, since_ms: int, until_ms: int) -> list[LedgerRow]:
        """近 3 个月的账单，按 billId 向前翻页，直到早于 since。"""
        out: list[LedgerRow] = []
        after = None
        for _ in range(200):  # 安全上限：200 页 × 100 条
            rows = self._signed("/api/v5/account/bills-archive", {"ccy": "USDT", "limit": 100, "after": after,
                                                                    "begin": since_ms, "end": until_ms})
            for x in rows:
                ts, inst, bid = int(fnum(x.get("ts"))), str(x.get("instId") or ""), x.get("billId")
                sym = symbol_of(inst) if inst else ""
                btype = str(x.get("type"))
                if btype == "2" and is_usdt_swap(inst):  # 交易：已实现盈亏 + 手续费
                    if fnum(x.get("pnl")):
                        out.append(LedgerRow(f"okx:{bid}:pnl", ts, "realized", sym, fnum(x.get("pnl"))))
                    if fnum(x.get("fee")):
                        out.append(LedgerRow(f"okx:{bid}:fee", ts, "fee", sym, fnum(x.get("fee"))))
                elif btype == "8" and is_usdt_swap(inst):
                    out.append(LedgerRow(f"okx:{bid}:funding", ts, "funding", sym, fnum(x.get("balChg"))))
                elif btype == "1":
                    out.append(LedgerRow(f"okx:{bid}:transfer", ts, "transfer", "", fnum(x.get("balChg"))))
            if len(rows) < 100 or int(fnum(rows[-1].get("ts"))) < since_ms:
                break
            after = rows[-1].get("billId")
        return [r for r in out if since_ms <= r.ts_ms < until_ms]

    def permissions(self) -> Permissions:
        d = (self._signed("/api/v5/account/config") or [{}])[0]
        perm = str(d.get("perm") or "")
        return Permissions(can_read="read_only" in perm, can_trade="trade" in perm, can_withdraw="withdraw" in perm,
                           ip_restricted=bool(d.get("ip")), detail={"perm": perm, "acctLv": d.get("acctLv"),
                                                                    "posMode": d.get("posMode")})
