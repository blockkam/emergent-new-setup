"""
tracker_client.py — drop-in HTTP client for the MySetup v15 dashboard.

Place this file next to strategy.py and config.py in your tracker folder,
then call `post_signal(sig)` after a signal dict is produced by strategy.evaluate().

Set DASHBOARD_URL in your .env (or as an environment variable) to the
public URL of your hosted dashboard, e.g.:

    DASHBOARD_URL=https://your-app.preview.emergentagent.com

Failures are swallowed silently so they never crash your scanner.
"""
from __future__ import annotations
import os
import logging
from typing import Any, Dict
import requests

log = logging.getLogger("tracker_client")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").rstrip("/")
TIMEOUT = float(os.getenv("DASHBOARD_TIMEOUT", "5"))


def post_signal(sig: Dict[str, Any]) -> bool:
    """POST a signal dict to /api/signals. Returns True on 2xx, False otherwise."""
    if not DASHBOARD_URL:
        return False
    try:
        r = requests.post(f"{DASHBOARD_URL}/api/signals", json=sig, timeout=TIMEOUT)
        if r.status_code >= 200 and r.status_code < 300:
            return True
        log.warning("dashboard non-2xx: %s · %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("dashboard post failed: %s", e)
    return False
