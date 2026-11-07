"""交易所适配器：签名、解析、止损单识别、流水、权限、错误分类（全部用构造的样本，不连真实交易所）。"""
import base64
import hashlib
import hmac
import urllib.parse

import pytest

from bian.exchanges import ExchangeError, Permissions, StopOrder, classify_stops
from bian.exchanges import binance as bn, okx
from bian.models import Position


class FakeHttp:
    """按路径返回预设响应；记录请求的 URL 和请求头，用来检查签名。"""

    def __init__(self, routes, base="https://x"):
        self.routes, self.base, self.calls = routes, base, []

    def sync_time(self, fetch):
        pass

    def now_ms(self):
        return 1_700_000_000_000

    def get(self, url, headers=None, classify=None):
        self.calls.append((url, headers or {}))
        path = urllib.parse.urlparse(url).path
        status, body = self.routes[path](url) if callable(self.routes[path]) else (200, self.routes[path])
        if classify:
            err = classify(status, body)
            if err:
                raise err
        return status, body


# ---------------------------------------------------------------- 签名
def test_binance_signature_matches_official_example():
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    q = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert bn.sign(secret, q) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_binance_request_is_signed_with_key_header():
    http = FakeHttp({"/fapi/v3/positionRisk": []})
    bn.BinanceFutures("x1", "KEY", "SECRET", http=http, sapi=http).positions()
    url, headers = http.calls[0]
    query, sig = url.split("?", 1)[1].rsplit("&signature=", 1)
    assert headers["X-MBX-APIKEY"] == "KEY" and "timestamp=1700000000000" in query
    assert sig == hmac.new(b"SECRET", query.encode(), hashlib.sha256).hexdigest()


def test_okx_signature_and_headers():
    http = FakeHttp({"/api/v5/account/config": {"code": "0", "data": [{"perm": "read_only"}]}})
    okx.OkxSwap("x2", "K", "S", "P", http=http).permissions()
    url, h = http.calls[0]
    assert h["OK-ACCESS-TIMESTAMP"] == "2023-11-14T22:13:20.000Z" and h["OK-ACCESS-PASSPHRASE"] == "P"
    expect = base64.b64encode(hmac.new(b"S", (h["OK-ACCESS-TIMESTAMP"] + "GET/api/v5/account/config").encode(),
                                       hashlib.sha256).digest()).decode()
    assert h["OK-ACCESS-SIGN"] == expect


# ---------------------------------------------------------------- 持仓
BN_POS = [
    {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-0.010", "entryPrice": "60000", "markPrice": "61000",
     "unRealizedProfit": "-10", "liquidationPrice": "90000", "notional": "-610", "positionInitialMargin": "61",
     "breakEvenPrice": "59990", "updateTime": 1},
    {"symbol": "ETHUSDT", "positionSide": "LONG", "positionAmt": "0.5", "entryPrice": "3000", "markPrice": "3100",
     "unRealizedProfit": "50", "liquidationPrice": "0", "notional": "1550", "positionInitialMargin": "77.5"},
    {"symbol": "XRPUSDT", "positionSide": "BOTH", "positionAmt": "0", "entryPrice": "0", "markPrice": "0.5"},
]


def test_binance_positions_one_way_and_hedge():
    http = FakeHttp({"/fapi/v3/positionRisk": BN_POS})
    ps = bn.BinanceFutures("x1", "K", "S", http=http, sapi=http).positions()
    assert [(p.symbol, p.side, p.qty, p.leverage, p.source, p.leader_id) for p in ps] == [
        ("BTCUSDT", "SHORT", 0.01, 10.0, "exchange", "x1"), ("ETHUSDT", "LONG", 0.5, 20.0, "exchange", "x1")]
    assert ps[0].roi == pytest.approx(-10 / 61)


OKX_INST = {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01"}, {"instId": "ETH-USDT-SWAP", "ctVal": "0.1"},
                                  {"instId": "BTC-USD-SWAP", "ctVal": "100"}]}
OKX_POS = {"code": "0", "data": [
    {"instId": "BTC-USDT-SWAP", "pos": "3", "posSide": "net", "avgPx": "60000", "markPx": "61000", "upl": "30",
     "lever": "10", "liqPx": "50000", "imr": "18.3", "mgnMode": "cross",
     "closeOrderAlgo": [{"slTriggerPx": "58000", "tpTriggerPx": "", "closeFraction": "1"}]},
    {"instId": "ETH-USDT-SWAP", "pos": "20", "posSide": "short", "avgPx": "3000", "markPx": "2900", "upl": "200",
     "lever": "5", "liqPx": "4000", "margin": "1160", "mgnMode": "isolated", "closeOrderAlgo": []},
    {"instId": "BTC-USD-SWAP", "pos": "5", "posSide": "net", "avgPx": "60000", "markPx": "61000", "upl": "0.001"},
    {"instId": "SOL-USDT-SWAP", "pos": "0", "posSide": "net"},
]}


def okx_adapter(extra=None):
    routes = {"/api/v5/public/instruments": OKX_INST, "/api/v5/account/positions": OKX_POS, **(extra or {})}
    http = FakeHttp(routes)
    return okx.OkxSwap("x2", "K", "S", "P", http=http), http


def test_okx_positions_contracts_converted_and_non_usdt_filtered():
    ps = okx_adapter()[0].positions()
    assert [(p.symbol, p.side, p.qty, p.margin) for p in ps] == [("BTCUSDT", "LONG", 0.03, 18.3),
                                                                 ("ETHUSDT", "SHORT", 2.0, 1160.0)]


# ---------------------------------------------------------------- 止损单
def test_binance_stop_orders_one_way_hedge_and_opening_orders_ignored():
    orders = [
        {"symbol": "BTCUSDT", "type": "STOP_MARKET", "side": "BUY", "positionSide": "BOTH", "stopPrice": "63000",
         "closePosition": True},  # 单向：买入全平 → 保护空单
        {"symbol": "ETHUSDT", "type": "STOP_MARKET", "side": "SELL", "positionSide": "LONG", "stopPrice": "2900",
         "origQty": "0.5"},  # 双向：卖出 + LONG = 平多
        {"symbol": "ETHUSDT", "type": "STOP_MARKET", "side": "BUY", "positionSide": "LONG", "stopPrice": "3500"},  # 开仓单
        {"symbol": "ETHUSDT", "type": "LIMIT", "side": "SELL", "positionSide": "LONG", "price": "3300"},
        {"symbol": "ETHUSDT", "type": "TAKE_PROFIT_MARKET", "side": "SELL", "positionSide": "LONG", "stopPrice": "3300"},
    ]
    algo = {"orders": [{"symbol": "XRPUSDT", "orderType": "STOP_MARKET", "side": "SELL", "positionSide": "BOTH",
                        "triggerPrice": "0.4", "reduceOnly": True, "quantity": "100"}]}
    http = FakeHttp({"/fapi/v1/openOrders": orders, "/fapi/v1/openAlgoOrders": algo})
    stops = bn.BinanceFutures("x1", "K", "S", http=http, sapi=http).stop_orders()
    assert [(s.symbol, s.side, s.trigger, s.kind) for s in stops] == [
        ("BTCUSDT", "SHORT", 63000, "stop"), ("ETHUSDT", "LONG", 2900, "stop"),
        ("ETHUSDT", "LONG", 3300, "take_profit"), ("XRPUSDT", "LONG", 0.4, "stop")]


def test_binance_algo_endpoint_missing_is_ignored():
    http = FakeHttp({"/fapi/v1/openOrders": [], "/fapi/v1/openAlgoOrders": lambda u: (404, {"code": -5000, "msg": "x"})})
    assert bn.BinanceFutures("x1", "K", "S", http=http, sapi=http).stop_orders() == []


def test_okx_stop_orders_from_algo_and_attached():
    ad, _ = okx_adapter({"/api/v5/trade/orders-algo-pending": lambda u: (200, {"code": "0", "data": [
        {"instId": "ETH-USDT-SWAP", "posSide": "short", "side": "buy", "slTriggerPx": "3100", "tpTriggerPx": "2500",
         "sz": "20"}] if "conditional" in u else []})})
    stops = ad.stop_orders()
    assert [(s.symbol, s.side, s.trigger, s.kind, s.qty) for s in stops] == [
        ("ETHUSDT", "SHORT", 3100, "stop", 2.0), ("ETHUSDT", "SHORT", 2500, "take_profit", 2.0),
        ("BTCUSDT", "LONG", 58000, "stop", 0.0)]


def test_classify_stops_picks_adverse_stop_per_position():
    long_ = Position("x", "BTCUSDT", "LONG", 1, 60000, 61000, 10, 10, "exchange")
    short = Position("x", "ETHUSDT", "SHORT", 1, 3000, 2900, 10, 10, "exchange")
    stops = [StopOrder("BTCUSDT", "LONG", 58000, 0.4, "stop"), StopOrder("BTCUSDT", "LONG", 59000, 0.4, "stop"),
             StopOrder("BTCUSDT", "LONG", 65000, 0, "stop"),  # 有利方向：不是止损
             StopOrder("ETHUSDT", "SHORT", 2800, 0, "take_profit")]
    assert classify_stops([long_, short], stops) == {long_.key: (59000, 0.8), short.key: (0.0, 0.0)}
    full = [StopOrder("BTCUSDT", "LONG", 58000, 0, "stop")]  # 全部平仓的止损单
    assert classify_stops([long_], full) == {long_.key: (58000, 1.0)}


# ---------------------------------------------------------------- 账户 / 流水 / 权限
def test_accounts_and_margin_ratio():
    http = FakeHttp({"/fapi/v3/account": {"totalMarginBalance": "1000", "totalMaintMargin": "250",
                                           "availableBalance": "600", "totalUnrealizedProfit": "-20"}})
    a = bn.BinanceFutures("x1", "K", "S", http=http, sapi=http).account()
    assert (a.equity, a.available, a.margin_ratio) == (1000, 600, 0.25)
    ad, _ = okx_adapter({"/api/v5/account/balance": {"code": "0", "data": [
        {"adjEq": "2000", "mmr": "100", "mgnRatio": "20", "upl": "5", "details": [{"ccy": "USDT", "availEq": "1500"}]}]}})
    a = ad.account()
    assert (a.equity, a.available, a.margin_ratio) == (2000, 1500, 0.05)


def test_binance_ledger_pages_and_kinds():
    t0 = 1_700_000_000_000
    page = [{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "5", "asset": "USDT", "time": t0 + i,
             "tranId": i, "tradeId": i} for i in range(1000)]
    rest = [{"symbol": "", "incomeType": "TRANSFER", "income": "-100", "asset": "USDT", "time": t0 + 2000, "tranId": 9},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.1", "asset": "BNB", "time": t0 + 2001,
             "tranId": 10}]
    calls = []

    def income(url):
        q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
        calls.append(int(q["startTime"]))
        return 200, page if int(q["startTime"]) == t0 else rest

    http = FakeHttp({"/fapi/v1/income": income})
    rows = bn.BinanceFutures("x1", "K", "S", http=http, sapi=http).ledger(t0, t0 + 86400_000)
    assert calls == [t0, t0 + 1000] and len(rows) == 1001  # BNB 手续费不计（只统计 USDT）
    assert rows[-1].kind == "transfer" and rows[-1].amount == -100


def test_okx_ledger_kinds():
    ad, _ = okx_adapter({"/api/v5/account/bills-archive": {"code": "0", "data": [
        {"billId": "3", "ts": "1700000003000", "type": "2", "instId": "BTC-USDT-SWAP", "pnl": "12", "fee": "-0.3"},
        {"billId": "2", "ts": "1700000002000", "type": "8", "instId": "BTC-USDT-SWAP", "balChg": "-0.05"},
        {"billId": "1", "ts": "1700000001000", "type": "1", "balChg": "500"}]}})
    rows = ad.ledger(1_699_000_000_000, 1_800_000_000_000)
    assert [(r.kind, r.amount) for r in rows] == [("realized", 12), ("fee", -0.3), ("funding", -0.05), ("transfer", 500)]


def test_permissions():
    http = FakeHttp({"/sapi/v1/account/apiRestrictions": {"enableReading": True, "enableFutures": False,
                                                          "enableWithdrawals": False, "ipRestrict": True}})
    p = bn.BinanceFutures("x1", "K", "S", http=http, sapi=http).permissions()
    assert p.read_only and p.ip_restricted
    http.routes["/sapi/v1/account/apiRestrictions"] = {"enableReading": True, "enableFutures": True}
    assert not bn.BinanceFutures("x1", "K", "S", http=http, sapi=http).permissions().read_only
    ad, h = okx_adapter({"/api/v5/account/config": {"code": "0", "data": [{"perm": "read_only,withdraw"}]}})
    assert ad.permissions().can_withdraw
    assert Permissions(True, False, False).read_only


# ---------------------------------------------------------------- 错误分类
@pytest.mark.parametrize("status,body,kind", [
    (401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action, request ip: 1.2.3.4"}, "ip"),
    (401, {"code": -2014, "msg": "API-key format invalid."}, "auth"),
    (400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}, "time"),
    (429, {"code": -1003, "msg": "Too many requests"}, "rate"),
    (451, {"code": 0, "msg": "restricted location"}, "region"),
])
def test_binance_error_classification(status, body, kind):
    err = bn.classify(status, body)
    assert isinstance(err, ExchangeError) and err.kind == kind and err.HINTS[kind] in str(err)


@pytest.mark.parametrize("code,kind", [("50111", "auth"), ("50110", "ip"), ("50102", "time"), ("50011", "rate"),
                                      ("50105", "auth"), ("51000", "other")])
def test_okx_error_classification(code, kind):
    assert okx.classify(200, {"code": code, "msg": "x", "data": []}).kind == kind
    assert okx.classify(200, {"code": "0", "data": []}) is None
