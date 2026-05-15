"""
cooldown.py — Phase 1 cooldown / alert-quality helper.

Lightweight, file-based, dependency-free. Scanner imports `Cooldown` and
calls `allow(symbol)` BEFORE evaluate() and `record(symbol, was_loss=)`
AFTER a signal fires (or after a trade resolves).

Rules (15m bar = 15 min):
  • global    : 3 bars after ANY signal     (45 min)
  • per-symbol: 12 bars after a signal      (3 h)
  • post-loss : 24 bars after a losing trade on that symbol (6 h)

Optional cluster cap: `pick_top(signals, n=5)` returns the top-N signals
by score to send in one scan tick — drops the rest, prevents alert spam.

Designed to be stateless across processes via a tiny JSON file. Safe to
import even if the file doesn't exist yet — it auto-creates.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable, Optional

DEFAULT_STATE = Path(os.environ.get("COOLDOWN_STATE_FILE", "cooldown_state.json"))

GLOBAL_COOLDOWN_MIN     = int(os.environ.get("GLOBAL_COOLDOWN_MIN",      45))   # 3 bars
PER_SYMBOL_COOLDOWN_MIN = int(os.environ.get("PER_SYMBOL_COOLDOWN_MIN",  180))  # 12 bars
POST_LOSS_COOLDOWN_MIN  = int(os.environ.get("POST_LOSS_COOLDOWN_MIN",   360))  # 24 bars


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        return datetime.fromisoformat(iso)
    except Exception:
        return None


class Cooldown:
    def __init__(self, state_path: Path | str = DEFAULT_STATE):
        self.path = Path(state_path)
        self._state = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"global_last": None, "by_symbol": {}}
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"global_last": None, "by_symbol": {}}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._state, indent=2))
        except Exception:
            pass  # silent — never break the scanner

    # ── public API ─────────────────────────────────────────────────────
    def allow(self, symbol: str) -> tuple[bool, str]:
        """Returns (allowed, reason). Reason is empty when allowed."""
        now = _now()
        # global
        g = _parse(self._state.get("global_last"))
        if g and (now - g) < timedelta(minutes=GLOBAL_COOLDOWN_MIN):
            return False, "global_cooldown"
        # per-symbol
        sym = self._state.get("by_symbol", {}).get(symbol, {})
        last_sig = _parse(sym.get("last_signal"))
        if last_sig and (now - last_sig) < timedelta(minutes=PER_SYMBOL_COOLDOWN_MIN):
            return False, "symbol_cooldown"
        last_loss = _parse(sym.get("last_loss"))
        if last_loss and (now - last_loss) < timedelta(minutes=POST_LOSS_COOLDOWN_MIN):
            return False, "post_loss_cooldown"
        return True, ""

    def record_signal(self, symbol: str) -> None:
        now_iso = _now().isoformat()
        self._state["global_last"] = now_iso
        self._state.setdefault("by_symbol", {}).setdefault(symbol, {})["last_signal"] = now_iso
        self._save()

    def record_loss(self, symbol: str) -> None:
        self._state.setdefault("by_symbol", {}).setdefault(symbol, {})["last_loss"] = _now().isoformat()
        self._save()

    # ── alert cluster cap ──────────────────────────────────────────────
    @staticmethod
    def pick_top(signals: Iterable[dict], n: int = 5) -> list[dict]:
        """Keep only the top-N signals per scan tick (by score_total / score).
        Use to prevent 30-alert avalanches when BTC moves and all alts fire.
        """
        items = list(signals or [])
        items.sort(key=lambda s: (s.get("score_total") or s.get("score") or 0), reverse=True)
        return items[:max(0, n)]
