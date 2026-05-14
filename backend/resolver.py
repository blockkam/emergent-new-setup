"""Background resolver — pulls Binance fapi 15m klines for OPEN signals
and updates MFE/MAE/status/result_r.

Pricing model for result_r:
  - Equal 1/3 scale-out at TP1, TP2, TP3.
  - After TP1 hit, SL moves to break-even (entry).
  - After TP2 hit, SL trails to most recent 3-bar structure (we approximate with TP1 itself for simplicity).
  - Stopped before TP1 hit  -> result_r = -1.0
  - Stopped after TP1 (BE)  -> result_r =  0.33 * rr1
  - Stopped after TP2       -> result_r =  0.33 * (rr1 + rr2)
  - All three TPs hit       -> result_r =  0.33 * (rr1 + rr2 + rr3)
  - Expired (>SIGNAL_EXPIRY_BARS bars, no resolution) -> mark EXPIRED, result_r = realized partials only
"""
from __future__ import annotations
import os
import logging
import httpx
from datetime import datetime, timezone
from typing import List, Dict, Any

from binance_client import fetch_klines

log = logging.getLogger("resolver")
EXPIRY_BARS = int(os.environ.get("SIGNAL_EXPIRY_BARS", 96))


def _iso_to_ms(iso: str) -> int:
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _partial_r(hit_tp1: bool, hit_tp2: bool, hit_tp3: bool,
               rr1: float, rr2: float, rr3: float) -> float:
    s = 0.0
    if hit_tp1: s += rr1 / 3.0
    if hit_tp2: s += rr2 / 3.0
    if hit_tp3: s += rr3 / 3.0
    return s


async def _resolve_one(db, client: httpx.AsyncClient, sig: Dict[str, Any]) -> None:
    symbol = sig["symbol"]
    side = sig["side"]
    entry = float(sig["entry"])
    tp1 = float(sig["tp1"]); tp2 = float(sig["tp2"]); tp3 = float(sig["tp3"])
    rr1 = float(sig.get("rr1") or 0); rr2 = float(sig.get("rr2") or 0); rr3 = float(sig.get("rr3") or 0)
    sl_initial = float(sig["sl_initial"])
    cur_sl = float(sig["sl"])
    risk_abs = abs(entry - sl_initial)
    if risk_abs <= 0:
        return

    hit_tp1 = bool(sig.get("hit_tp1", False))
    hit_tp2 = bool(sig.get("hit_tp2", False))
    hit_tp3 = bool(sig.get("hit_tp3", False))
    mfe = float(sig.get("max_favorable_r") or 0)
    mae = float(sig.get("max_adverse_r") or 0)
    bars_elapsed = int(sig.get("bars_elapsed") or 0)
    bars_to_tp1 = sig.get("bars_to_tp1")
    bars_to_tp2 = sig.get("bars_to_tp2")
    bars_to_tp3 = sig.get("bars_to_tp3")

    last_ot = sig.get("last_resolved_open_time")
    if last_ot is not None:
        start_ms = int(last_ot) + 1
    else:
        start_ms = _iso_to_ms(sig["created_at"])

    klines = await fetch_klines(client, symbol, "15m", start_ms=start_ms, limit=500)
    if not klines:
        return

    status = sig["status"]
    last_open_time = last_ot

    for k in klines:
        open_time = int(k[0])
        high = float(k[2]); low = float(k[3])
        last_open_time = open_time
        bars_elapsed += 1

        # MFE / MAE on every bar
        if side == "LONG":
            mfe = max(mfe, (high - entry) / risk_abs)
            mae = min(mae, (low - entry) / risk_abs)
        else:
            mfe = max(mfe, (entry - low) / risk_abs)
            mae = min(mae, (entry - high) / risk_abs)

        # Stop check FIRST (conservative — assume stops fill on adverse wick)
        if side == "LONG" and low <= cur_sl:
            if hit_tp2:
                status = "TP2"
            elif hit_tp1:
                status = "BE_STOP"
            else:
                status = "STOPPED"
            break
        if side == "SHORT" and high >= cur_sl:
            if hit_tp2:
                status = "TP2"
            elif hit_tp1:
                status = "BE_STOP"
            else:
                status = "STOPPED"
            break

        # TP checks (in order — best case favorable wick reaches TP3)
        if side == "LONG":
            if not hit_tp1 and high >= tp1:
                hit_tp1 = True
                bars_to_tp1 = bars_elapsed
                cur_sl = max(cur_sl, entry)  # BE
            if not hit_tp2 and high >= tp2:
                hit_tp2 = True
                bars_to_tp2 = bars_elapsed
                cur_sl = max(cur_sl, tp1)    # trail to TP1
            if not hit_tp3 and high >= tp3:
                hit_tp3 = True
                bars_to_tp3 = bars_elapsed
                status = "TP3"
                break
        else:
            if not hit_tp1 and low <= tp1:
                hit_tp1 = True
                bars_to_tp1 = bars_elapsed
                cur_sl = min(cur_sl, entry)
            if not hit_tp2 and low <= tp2:
                hit_tp2 = True
                bars_to_tp2 = bars_elapsed
                cur_sl = min(cur_sl, tp1)
            if not hit_tp3 and low <= tp3:
                hit_tp3 = True
                bars_to_tp3 = bars_elapsed
                status = "TP3"
                break

        if bars_elapsed >= EXPIRY_BARS:
            status = "EXPIRED"
            break

    # compute result_r
    result_r = None
    if status == "STOPPED":
        result_r = -1.0
    elif status in ("BE_STOP", "TP1"):
        result_r = (rr1 / 3.0) if hit_tp1 else -1.0
    elif status == "TP2":
        result_r = _partial_r(hit_tp1, hit_tp2, False, rr1, rr2, rr3)
    elif status == "TP3":
        result_r = _partial_r(True, True, True, rr1, rr2, rr3)
    elif status == "EXPIRED":
        result_r = _partial_r(hit_tp1, hit_tp2, hit_tp3, rr1, rr2, rr3)

    update: Dict[str, Any] = {
        "sl": cur_sl,
        "hit_tp1": hit_tp1,
        "hit_tp2": hit_tp2,
        "hit_tp3": hit_tp3,
        "bars_to_tp1": bars_to_tp1,
        "bars_to_tp2": bars_to_tp2,
        "bars_to_tp3": bars_to_tp3,
        "bars_elapsed": bars_elapsed,
        "max_favorable_r": round(mfe, 4),
        "max_adverse_r": round(mae, 4),
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_resolved_open_time": last_open_time,
    }
    if status != "OPEN" and result_r is not None:
        update["result_r"] = round(result_r, 4)
        update["resolved_at"] = update["updated_at"]

    await db.signals.update_one({"id": sig["id"]}, {"$set": update})


async def resolve_open_signals(db) -> int:
    """Iterate OPEN signals and update them.

    Returns count of signals ITERATED (not necessarily state-changed). A signal
    stays OPEN across ticks until either (a) price hits SL/TP1/TP2/TP3, or
    (b) it ages out past SIGNAL_EXPIRY_BARS bars -> EXPIRED. The cursor
    `last_resolved_open_time` guarantees we never re-walk the same kline twice.
    """
    cursor = db.signals.find({"status": "OPEN"}, {"_id": 0})
    open_sigs: List[Dict[str, Any]] = await cursor.to_list(length=5000)
    if not open_sigs:
        return 0

    async with httpx.AsyncClient() as client:
        for sig in open_sigs:
            try:
                await _resolve_one(db, client, sig)
            except Exception as e:
                log.warning("resolver error %s: %s", sig.get("symbol"), e)
    return len(open_sigs)
