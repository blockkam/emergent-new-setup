"""OKX USD-M perp klines client (replacement for Binance — Binance fapi
geo-blocks our hosting region). Symbol mapping: 'BTCUSDT' -> 'BTC-USDT-SWAP'.
Bar interval mapping: 15m -> '15m'.

Returns a normalized list of [open_time_ms, open, high, low, close, volume]
sorted ascending — matches what resolver.py expects.
"""
from __future__ import annotations
import os
import httpx
from typing import List

OKX_BASE = os.environ.get("OKX_BASE", "https://www.okx.com")


def _to_okx_symbol(symbol: str) -> str:
    s = symbol.upper()
    # BTCUSDT / ETHUSDT / SOLUSDT -> BTC-USDT-SWAP
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT-SWAP"
    if s.endswith("USDC"):
        return f"{s[:-4]}-USDC-SWAP"
    if s.endswith("USD"):
        return f"{s[:-3]}-USD-SWAP"
    return s


def _to_okx_bar(interval: str) -> str:
    m = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "60m": "1H", "2h": "2H", "4h": "4H", "240": "4H",
        "1d": "1D", "D": "1D",
    }
    return m.get(interval, interval)


async def fetch_klines(
    client: httpx.AsyncClient,
    symbol: str,
    interval: str = "15m",
    start_ms: int | None = None,
    limit: int = 200,
) -> List[list]:
    """Returns klines as: [[open_time_ms, o, h, l, c, v], ...] ascending."""
    okx_sym = _to_okx_symbol(symbol)
    okx_bar = _to_okx_bar(interval)
    out: List[list] = []
    cursor_after_ms: int | None = None

    remaining = max(1, min(limit, 1000))
    while remaining > 0:
        # OKX returns DESC. To get bars FROM start_ms going forward, we
        # use the /history-candles endpoint with `before` (oldest cap).
        params = {"instId": okx_sym, "bar": okx_bar, "limit": str(min(100, remaining))}
        if start_ms is not None:
            params["before"] = str(start_ms - 1)  # OKX: before = exclusive older bound
        if cursor_after_ms is not None:
            params["after"] = str(cursor_after_ms)
        url = f"{OKX_BASE}/api/v5/market/history-candles"
        try:
            r = await client.get(url, params=params, timeout=10)
            if r.status_code != 200:
                break
            payload = r.json()
            data = payload.get("data") or []
        except Exception:
            break
        if not data:
            break
        # data is DESC; normalize and append
        chunk = []
        for row in data:
            try:
                ts = int(row[0])
                o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
                v = float(row[5]) if len(row) > 5 else 0.0
                chunk.append([ts, o, h, l, c, v])
            except (ValueError, IndexError):
                continue
        if not chunk:
            break
        out.extend(chunk)
        remaining -= len(chunk)
        # next page goes OLDER (smaller ts). after = oldest ts in this page
        cursor_after_ms = min(c[0] for c in chunk)
        if start_ms is not None and cursor_after_ms <= start_ms:
            break
        if len(chunk) < 100:
            break

    # sort ascending and trim anything older than start_ms
    out.sort(key=lambda x: x[0])
    if start_ms is not None:
        out = [x for x in out if x[0] >= start_ms]
    # Match the kline tuple shape resolver expects: index 0=open_time, 2=high, 3=low
    # Our normalized rows already match that (and have close at idx 4, vol at 5).
    return out
