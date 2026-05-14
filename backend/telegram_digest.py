"""Daily Telegram digest worker."""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
import httpx

log = logging.getLogger("digest")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


async def _send(text: str) -> tuple[bool, str]:
    """Send a Telegram message. Returns (sent, reason)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("telegram disabled · TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set")
        return False, "telegram_not_configured"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as c:
        try:
            r = await c.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=10)
            if 200 <= r.status_code < 300:
                return True, "sent"
            log.warning("telegram non-2xx: %s · %s", r.status_code, r.text[:200])
            return False, f"telegram_http_{r.status_code}"
        except Exception as e:
            log.warning("telegram send failed: %s", e)
            return False, f"telegram_error: {e}"


def _fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


async def send_daily_digest(db) -> tuple[bool, str]:
    """Build and send digest covering the last 24h of resolved signals + open snapshot.
    Returns (sent, reason). When telegram creds are missing, returns (False, 'telegram_not_configured')
    so the worker can no-op gracefully without failing the scheduled job.
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()

    resolved = await db.signals.find(
        {"resolved_at": {"$gte": since}, "result_r": {"$ne": None}},
        {"_id": 0}
    ).to_list(length=5000)
    open_count = await db.signals.count_documents({"status": "OPEN"})
    fired_today = await db.signals.count_documents({"created_at": {"$gte": since}})

    if not resolved:
        msg = (
            "<b>📊 MySetup Daily Digest</b>\n"
            f"<code>{now.strftime('%Y-%m-%d %H:%M UTC')}</code>\n\n"
            f"Signals fired (24h): <b>{fired_today}</b>\n"
            f"Currently open: <b>{open_count}</b>\n"
            "No resolved trades in the last 24h."
        )
        return await _send(msg)

    n = len(resolved)
    wins = [r for r in resolved if (r.get("result_r") or 0) > 0]
    losses = [r for r in resolved if (r.get("result_r") or 0) <= 0]
    win_rate = len(wins) / n if n else 0
    total_r = sum((r.get("result_r") or 0) for r in resolved)
    avg_win = sum((r.get("result_r") or 0) for r in wins) / len(wins) if wins else 0
    avg_loss = sum((r.get("result_r") or 0) for r in losses) / len(losses) if losses else 0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    # by tier
    by_tier: Dict[str, Dict[str, Any]] = {}
    for r in resolved:
        t = r.get("tier") or "A"
        d = by_tier.setdefault(t, {"n": 0, "w": 0, "r": 0.0})
        d["n"] += 1
        if (r.get("result_r") or 0) > 0:
            d["w"] += 1
        d["r"] += r.get("result_r") or 0

    tier_lines = []
    for t in sorted(by_tier.keys()):
        d = by_tier[t]
        tier_lines.append(f"  <code>{t}</code> · {d['n']:>2}t · WR {_fmt_pct(d['w']/d['n'])} · {d['r']:+.2f}R")

    # by path
    by_path: Dict[str, Dict[str, Any]] = {}
    for r in resolved:
        p = r.get("entry_path") or "n/a"
        d = by_path.setdefault(p, {"n": 0, "w": 0, "r": 0.0})
        d["n"] += 1
        if (r.get("result_r") or 0) > 0:
            d["w"] += 1
        d["r"] += r.get("result_r") or 0
    path_lines = []
    for p, d in by_path.items():
        path_lines.append(f"  <code>{p[:16]:<16}</code> · {d['n']:>2}t · WR {_fmt_pct(d['w']/d['n'])} · {d['r']:+.2f}R")

    msg = (
        "<b>📊 MySetup Daily Digest</b>\n"
        f"<code>{now.strftime('%Y-%m-%d %H:%M UTC')}</code>\n\n"
        f"Fired (24h): <b>{fired_today}</b>  ·  Open: <b>{open_count}</b>\n"
        f"Resolved (24h): <b>{n}</b>\n"
        f"Win rate: <b>{_fmt_pct(win_rate)}</b>  ·  Total: <b>{total_r:+.2f}R</b>\n"
        f"Expectancy/trade: <b>{expectancy:+.3f}R</b>\n"
        f"Avg win: {avg_win:+.2f}R  ·  Avg loss: {avg_loss:+.2f}R\n\n"
        "<b>By Tier</b>\n" + ("\n".join(tier_lines) or "  —") + "\n\n"
        "<b>By Entry Path</b>\n" + ("\n".join(path_lines) or "  —")
    )
    return await _send(msg)
