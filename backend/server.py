"""MySetup Tracker API — FastAPI + MongoDB."""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from models import Signal, SignalCreate, _utc_now_iso
from resolver import resolve_open_signals
from telegram_digest import send_daily_digest

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s · %(message)s")
log = logging.getLogger("api")

mongo = AsyncIOMotorClient(os.environ["MONGO_URL"])
db = mongo[os.environ["DB_NAME"]]

app = FastAPI(title="MySetup v15 Tracker")
api = APIRouter(prefix="/api")

scheduler: Optional[AsyncIOScheduler] = None


# ─── helpers ──────────────────────────────────────────────────────────
def _session_from_hour(h: int) -> str:
    """Normalize derived session label to lowercase spec values."""
    if 0 <= h < 7: return "asia"
    if 7 <= h < 13: return "london"
    if 13 <= h < 21: return "new_york"
    return "off"


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    return doc


# ─── routes ───────────────────────────────────────────────────────────
@api.get("/")
async def root():
    open_n = await db.signals.count_documents({"status": "OPEN"})
    total = await db.signals.count_documents({})
    return {"service": "MySetup v15 Tracker", "open": open_n, "total": total}


@api.post("/signals", response_model=Signal)
async def create_signal(payload: SignalCreate):
    """Scanner posts each fired signal here."""
    data = payload.model_dump()

    # derive session if missing
    if not data.get("session"):
        data["session"] = _session_from_hour(datetime.now(timezone.utc).hour)

    sig = Signal(
        symbol=data["symbol"],
        side=data["side"],
        tier=data.get("tier") or "A",
        grade=data.get("grade"),
        entry_path=data.get("entry_path"),
        regime=data.get("regime"),
        entry=float(data["entry"]),
        sl=float(data["sl"]),
        sl_initial=float(data["sl"]),
        tp1=float(data["tp1"]),
        tp2=float(data["tp2"]),
        tp3=float(data["tp3"]),
        rr1=float(data.get("rr1") or 0),
        rr2=float(data.get("rr2") or 0),
        rr3=float(data.get("rr3") or 0),
        risk_pct=float(data.get("risk_pct") or 0),
        strength=data.get("strength"),
        strength_label=data.get("strength_label"),
        score=data.get("score"),
        max_score=data.get("max_score"),
        pct=data.get("pct"),
        session=data.get("session"),
        confluence=data.get("confluence"),
        timeframe=data.get("timeframe") or "15m",
        setup_type=data.get("setup_type"),
        entry_model=data.get("entry_model"),
        liquidity_event=data.get("liquidity_event"),
        htf_bias=data.get("htf_bias"),
    )
    await db.signals.insert_one(sig.model_dump())
    return sig


@api.get("/signals")
async def list_signals(
    status: Optional[str] = None,
    side: Optional[str] = None,
    tier: Optional[str] = None,
    symbol: Optional[str] = None,
    entry_path: Optional[str] = None,
    session: Optional[str] = None,
    setup_type: Optional[str] = None,
    entry_model: Optional[str] = None,
    liquidity_event: Optional[str] = None,
    htf_bias: Optional[str] = None,
    regime: Optional[str] = None,
    limit: int = Query(200, le=2000),
    offset: int = 0,
):
    q: Dict[str, Any] = {}
    for k, v in [("status", status), ("side", side), ("tier", tier),
                 ("symbol", symbol), ("entry_path", entry_path), ("session", session),
                 ("setup_type", setup_type), ("entry_model", entry_model),
                 ("liquidity_event", liquidity_event), ("htf_bias", htf_bias),
                 ("regime", regime)]:
        if v:
            q[k] = v
    total = await db.signals.count_documents(q)
    cursor = db.signals.find(q, {"_id": 0}).sort("created_at", -1).skip(offset).limit(limit)
    items = await cursor.to_list(length=limit)
    return {"total": total, "items": items}


@api.delete("/signals/{sid}")
async def delete_signal(sid: str):
    r = await db.signals.delete_one({"id": sid})
    if r.deleted_count == 0:
        raise HTTPException(404, "not found")
    return {"ok": True}


@api.post("/resolve")
async def manual_resolve():
    n = await resolve_open_signals(db)
    return {"processed": n}


@api.post("/digest")
async def manual_digest():
    sent, reason = await send_daily_digest(db)
    return {"sent": sent, "reason": reason}


@api.get("/config/status")
async def config_status():
    """Lightweight introspection for the dashboard footer or quick CLI checks."""
    return {
        "telegram_configured": bool(os.environ.get("TELEGRAM_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID")),
        "resolver_interval_min": int(os.environ.get("RESOLVER_INTERVAL_MIN", 15)),
        "digest_time_utc": f"{int(os.environ.get('DIGEST_HOUR_UTC', 0)):02d}:{int(os.environ.get('DIGEST_MINUTE_UTC', 5)):02d}",
        "klines_source": "OKX (https://www.okx.com/api/v5/market/history-candles)",
    }


@api.get("/metrics")
async def metrics(days: int = 30):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    docs = await db.signals.find(
        {"created_at": {"$gte": since}}, {"_id": 0}
    ).to_list(length=10000)

    resolved = [d for d in docs if d.get("result_r") is not None]
    open_n = sum(1 for d in docs if d.get("status") == "OPEN")
    fired = len(docs)
    n = len(resolved)
    wins = [d for d in resolved if (d.get("result_r") or 0) > 0]
    losses = [d for d in resolved if (d.get("result_r") or 0) <= 0]
    win_rate = (len(wins) / n) if n else 0
    total_r = sum((d.get("result_r") or 0) for d in resolved)
    avg_win = (sum((d.get("result_r") or 0) for d in wins) / len(wins)) if wins else 0
    avg_loss = (sum((d.get("result_r") or 0) for d in losses) / len(losses)) if losses else 0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss) if n else 0

    def group_by(key: str) -> List[Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for d in resolved:
            k = d.get(key) or "—"
            g = out.setdefault(k, {"key": k, "n": 0, "w": 0, "r": 0.0,
                                   "mfe_sum": 0.0, "mae_sum": 0.0})
            g["n"] += 1
            if (d.get("result_r") or 0) > 0:
                g["w"] += 1
            g["r"] += d.get("result_r") or 0
            g["mfe_sum"] += d.get("max_favorable_r") or 0
            g["mae_sum"] += d.get("max_adverse_r") or 0
        rows = []
        for g in out.values():
            rows.append({
                "key": g["key"], "n": g["n"], "wins": g["w"],
                "win_rate": (g["w"] / g["n"]) if g["n"] else 0,
                "total_r": round(g["r"], 2),
                "avg_r": round(g["r"] / g["n"], 3) if g["n"] else 0,
                "avg_mfe": round(g["mfe_sum"] / g["n"], 2) if g["n"] else 0,
                "avg_mae": round(g["mae_sum"] / g["n"], 2) if g["n"] else 0,
            })
        rows.sort(key=lambda r: r["n"], reverse=True)
        return rows

    # equity curve
    resolved_sorted = sorted(resolved, key=lambda d: d.get("resolved_at") or d.get("created_at"))
    equity = []
    cum = 0.0
    for d in resolved_sorted:
        cum += d.get("result_r") or 0
        equity.append({
            "t": d.get("resolved_at") or d.get("created_at"),
            "r": round(cum, 3),
            "symbol": d.get("symbol"),
            "side": d.get("side"),
        })

    # MFE/MAE histogram (bucketed -3 .. +5 R, 0.5 step)
    def hist(field: str, lo: float, hi: float, step: float = 0.5):
        buckets = {}
        cur = lo
        while cur <= hi + 1e-9:
            buckets[round(cur, 2)] = 0
            cur += step
        for d in resolved:
            v = d.get(field) or 0
            v = max(lo, min(hi, v))
            b = round(round(v / step) * step, 2)
            if b in buckets:
                buckets[b] += 1
        return [{"bucket": k, "count": v} for k, v in sorted(buckets.items())]

    return {
        "window_days": days,
        "fired": fired,
        "open": open_n,
        "resolved": n,
        "win_rate": round(win_rate, 4),
        "total_r": round(total_r, 3),
        "expectancy": round(expectancy, 4),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "by_tier": group_by("tier"),
        "by_path": group_by("entry_path"),
        "by_symbol": group_by("symbol")[:25],
        "by_session": group_by("session"),
        "by_side": group_by("side"),
        "by_regime": group_by("regime"),
        "by_setup_type": group_by("setup_type"),
        "by_entry_model": group_by("entry_model"),
        "by_liquidity_event": group_by("liquidity_event"),
        "by_htf_bias": group_by("htf_bias"),
        "equity": equity,
        "mfe_hist": hist("max_favorable_r", 0, 6, 0.5),
        "mae_hist": hist("max_adverse_r", -3, 0, 0.25),
    }


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    global scheduler
    await db.signals.create_index("id", unique=True)
    await db.signals.create_index("status")
    await db.signals.create_index("created_at")
    await db.signals.create_index("symbol")

    interval = int(os.environ.get("RESOLVER_INTERVAL_MIN", 15))
    digest_h = int(os.environ.get("DIGEST_HOUR_UTC", 0))
    digest_m = int(os.environ.get("DIGEST_MINUTE_UTC", 5))

    async def _resolver_job():
        try:
            n = await resolve_open_signals(db)
            log.info("resolver tick · processed=%d", n)
        except Exception as e:
            log.warning("resolver job failed: %s", e)

    async def _digest_job():
        try:
            sent, reason = await send_daily_digest(db)
            log.info("digest tick · sent=%s · reason=%s", sent, reason)
        except Exception as e:
            log.warning("digest job failed: %s", e)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(_resolver_job, "interval", minutes=interval, id="resolver",
                      next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20))
    scheduler.add_job(_digest_job, "cron", hour=digest_h, minute=digest_m, id="digest")
    scheduler.start()
    log.info("scheduler started · resolver=%dm · digest=%02d:%02d UTC",
             interval, digest_h, digest_m)


@app.on_event("shutdown")
async def _shutdown():
    if scheduler:
        scheduler.shutdown(wait=False)
    mongo.close()
