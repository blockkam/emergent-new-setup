"""Pydantic models for MySetup v15 signal tracker."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, ConfigDict
import uuid


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SignalCreate(BaseModel):
    """Payload your scanner POSTs to /api/signals."""
    model_config = ConfigDict(extra="allow")

    symbol: str
    side: Literal["LONG", "SHORT"]
    entry: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    rr1: float = 0.0
    rr2: float = 0.0
    rr3: float = 0.0
    risk_pct: float = 0.0

    tier: Optional[str] = "A"        # S / A / B / C
    grade: Optional[str] = None      # A+/A/B/C/D
    entry_path: Optional[str] = None # Sweep→BOS / BOS+Retest
    regime: Optional[str] = None     # trending / ranging / volatile / compressed
    strength: Optional[float] = None
    strength_label: Optional[str] = None
    score: Optional[int] = None
    max_score: Optional[int] = None
    pct: Optional[float] = None
    session: Optional[str] = None    # asia / london / new_york
    confluence: Optional[Dict[str, Any]] = None
    timeframe: Optional[str] = "15m"
    timestamp: Optional[str] = None  # client-side timestamp (informational)

    # ── v15.1 advanced classification (all optional for backward compat) ──
    setup_type: Optional[str] = None        # sweep_reclaim / fvg_continuation / ob_reversal / deviation_breakout
    entry_model: Optional[str] = None       # aggressive / confirmation / reclaim
    liquidity_event: Optional[str] = None   # e.g. asia_low_swept, pdh_swept
    htf_bias: Optional[str] = None          # bull / bear / neutral

    # ── Phase 1 numeric exposure (optional) ───────────────────────────────
    score_total: Optional[int] = None             # 0..15 weighted score
    range_expansion_ratio: Optional[float] = None # ATR(14) / ATR(50) at entry


class Signal(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    side: str
    tier: str = "A"
    grade: Optional[str] = None
    entry_path: Optional[str] = None
    regime: Optional[str] = None

    entry: float
    sl: float          # current SL (may trail)
    sl_initial: float  # original SL — never changes
    tp1: float
    tp2: float
    tp3: float
    rr1: float = 0.0
    rr2: float = 0.0
    rr3: float = 0.0
    risk_pct: float = 0.0

    strength: Optional[float] = None
    strength_label: Optional[str] = None
    score: Optional[int] = None
    max_score: Optional[int] = None
    pct: Optional[float] = None
    session: Optional[str] = None
    confluence: Optional[Dict[str, Any]] = None
    timeframe: str = "15m"

    # ── v15.1 advanced classification (all optional for backward compat) ──
    setup_type: Optional[str] = None
    entry_model: Optional[str] = None
    liquidity_event: Optional[str] = None
    htf_bias: Optional[str] = None

    # ── Phase 1 numeric exposure (optional) ───────────────────────────────
    score_total: Optional[int] = None
    range_expansion_ratio: Optional[float] = None

    status: str = "OPEN"  # OPEN, TP1, TP2, TP3, STOPPED, BE_STOP, EXPIRED
    hit_tp1: bool = False
    hit_tp2: bool = False
    hit_tp3: bool = False
    bars_to_tp1: Optional[int] = None
    bars_to_tp2: Optional[int] = None
    bars_to_tp3: Optional[int] = None
    bars_elapsed: int = 0

    max_favorable_r: float = 0.0
    max_adverse_r: float = 0.0
    result_r: Optional[float] = None  # set when status != OPEN

    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)
    resolved_at: Optional[str] = None
    last_resolved_open_time: Optional[int] = None  # ms epoch of last kline processed
