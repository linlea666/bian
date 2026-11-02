import json

import pytest

from bian.api.leaders import parse_closed, parse_position
from bian.client import AuthExpired, BinanceError, _unwrap


def test_parse_position_both_mode_short():
    p = parse_position({"symbol": "FILUSDT", "positionAmount": "-100", "entryPrice": "1.0",
                        "markPrice": "0.9", "unrealizedProfit": "10", "notionalValue": "-90",
                        "leverage": 10, "positionSide": "BOTH", "breakEvenPrice": "0.999"}, "L1", "public")
    assert p.side == "SHORT" and p.qty == 100
    assert p.margin == pytest.approx(10.0)  # 100 * 1.0 / 10
    assert p.roi == pytest.approx(1.0)  # +10U / 10U
    assert p.key == "L1:FILUSDT:SHORT"
    assert p.price_at_roi(-0.3) == pytest.approx(1.03)  # 空单 ROI -30% @10x = 价格 +3%


def test_parse_position_zero_is_skipped():
    assert parse_position({"symbol": "X", "positionAmount": "0", "positionSide": "LONG"}, "L", "public") is None


def test_parse_position_private_infers_leverage_and_margin():
    p = parse_position({"symbol": "ETHUSDT", "positionAmount": "1", "entryPrice": "2000", "markPrice": "1900",
                        "unrealizedProfit": "-100", "notionalValue": "1900", "positionInitialMargin": "190",
                        "liquidationPrice": "1500", "positionSide": "LONG"}, "L", "copy")
    assert p.leverage == 10
    assert p.margin == 190
    assert p.liquidation_price == 1500
    assert p.roi == pytest.approx(-100 / 190)


def test_parse_closed():
    c = parse_closed({"positionId": "9", "symbol": "BTCUSDT", "side": "Short", "opened": 1000, "closed": 3_601_000,
                      "avgCost": 100, "avgClosePrice": 90, "maxOpenInterest": 2, "closedVolume": 2,
                      "leverage": "10", "closingPnl": 20, "roi": "0.99", "status": "All Closed"}, "L")
    assert c.side == "SHORT" and c.leverage == 10
    assert c.price_roi == pytest.approx(1.0)
    assert c.duration_hours == pytest.approx(1.0)
    assert not c.had_adds


def test_unwrap_bapi_success_and_errors():
    assert _unwrap(json.dumps({"code": "000000", "data": [1], "success": True}).encode(), 200) == [1]
    with pytest.raises(AuthExpired):
        _unwrap(json.dumps({"code": "100001005", "data": None}).encode(), 200)
    with pytest.raises(BinanceError) as e:
        _unwrap(json.dumps({"code": "11012030", "message": "closed", "data": None}).encode(), 200)
    assert e.value.code == "11012030"


def test_unwrap_fapi_passthrough():
    assert _unwrap(b'{"serverTime": 1}', 200) == {"serverTime": 1}
    assert _unwrap(b"[[1,2]]", 200) == [[1, 2]]
