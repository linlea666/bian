"""公开行情（走 www.binance.com/fapi，避开 fapi.binance.com 的地区限制）。"""
from __future__ import annotations

import time

from ..client import Client
from ..models import fnum

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}


def mark_prices(client: Client) -> dict[str, tuple[float, int]]:
    """全部 U 本位合约标记价：symbol -> (markPrice, 交易所时间 ms)。"""
    rows = client.get("/fapi/v1/premiumIndex") or []
    return {r["symbol"]: (fnum(r.get("markPrice")), int(fnum(r.get("time")))) for r in rows}


def server_time(client: Client) -> int:
    return int((client.get("/fapi/v1/time") or {}).get("serverTime") or time.time() * 1000)


def pick_interval(start_ms: int, end_ms: int, max_bars: int = 1500) -> str:
    span = max(1, end_ms - start_ms)
    for iv in ("1m", "5m", "15m", "1h", "4h", "1d"):
        if span / INTERVAL_MS[iv] <= max_bars:
            return iv
    return "1d"


def klines(
    client: Client, symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1500
) -> list[tuple[int, float, float, float, float]]:
    """K 线：[(openTime, open, high, low, close)]，自动分页。"""
    out: list[tuple[int, float, float, float, float]] = []
    step = INTERVAL_MS[interval]
    cursor = start_ms - start_ms % step
    while cursor <= end_ms:
        rows = client.get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": limit},
        ) or []
        if not rows:
            break
        for r in rows:
            out.append((int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        if len(rows) < limit:
            break
        cursor = int(rows[-1][0]) + step
    return out
