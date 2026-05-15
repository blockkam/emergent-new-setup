"""
MySetup v15 — Phase 1 refinements.

Drop-in replacement for strategy.py. Preserves the public signature
    evaluate(df_15m, df_1h, df_4h, df_1d, symbol, btc_df_1h=None) -> dict | None

Phase 1 changes (highest-ROI only):
  • 2-bar confirmed rolling swing replaces lagging pivots
  • Mandatory range-expansion / chop filter
  • Weighted scoring (0..15) → tier A+/A/B/C  (C = log-only)
  • Simplified 2-component strength formula (was 6-component, correlated)
  • Min RR 1.8 hard gate; risk cap ≤ 1.5 ATR
  • Removed: MA 7/26 cross, MA99, imbalance/push, adaptive threshold,
            cooldown A-grade bypass (handled in cooldown.py)
  • BTC alignment becomes a +2 weighted bonus instead of a hard gate
  • Output dict adds: score_total (0..15), range_expansion_ratio

Cooldown lives in cooldown.py (a tiny separate helper). Scanner calls it
before evaluate() and records after — keeps this module stateless and
unit-testable.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

import config as C
import indicators as I

# ── Phase 1 knobs (override via config.py if needed) ────────────────────
SL_ATR_BUFFER          = getattr(C, "SL_ATR_BUFFER", 0.25)
RISK_CAP_ATR           = getattr(C, "RISK_CAP_ATR", 1.5)
MIN_BOS_BODY_ATR       = getattr(C, "MIN_BOS_BODY_ATR", 0.5)
MIN_BOS_VOL_MULT       = getattr(C, "MIN_BOS_VOL_MULT", 1.1)
MIN_RR                 = getattr(C, "MIN_RR", 1.8)
RANGE_EXP_ATR_RATIO    = getattr(C, "RANGE_EXP_ATR_RATIO", 1.1)
RANGE_EXP_BAR_RATIO    = getattr(C, "RANGE_EXP_BAR_RATIO", 0.7)
MIN_STRENGTH           = getattr(C, "MIN_STRENGTH", 0.7)
FVG_GRACE_BARS         = getattr(C, "FVG_GRACE_BARS", 5)
CONFIRM_WINDOW         = getattr(C, "CONFIRM_WINDOW", 7)

# Tier thresholds (max score = 15)
TIER_AP                = getattr(C, "TIER_AP", 12)
TIER_A                 = getattr(C, "TIER_A", 9)
TIER_B                 = getattr(C, "TIER_B", 6)


# ════════════════════════════════════════════════════════════════════════
# 2-bar confirmed rolling swing (drop-in for pivot detection)
# ════════════════════════════════════════════════════════════════════════
def _last_confirmed_swing_high(high: np.ndarray, close: np.ndarray, i: int,
                                window: int = 5) -> tuple[float, int] | tuple[float, int]:
    """A swing high at index t is confirmed when:
        • high[t] == max(high[t-window+1 : t+1])  AND  t ∈ {i-1, i-2}
        • close[i]   < high[t]
        • close[i-1] < high[t]
    Confirms in 2 bars (vs 3..5 with pivots). No repaint, no lookahead.
    Returns (high_value, t_index) or (nan, -1).
    """
    if i < window:
        return float("nan"), -1
    for t in (i - 1, i - 2):
        if t - window + 1 < 0:
            continue
        seg = high[t - window + 1 : t + 1]
        if high[t] == seg.max() and close[i] < high[t] and close[i - 1] < high[t]:
            return float(high[t]), t
    return float("nan"), -1


def _last_confirmed_swing_low(low: np.ndarray, close: np.ndarray, i: int,
                               window: int = 5) -> tuple[float, int]:
    if i < window:
        return float("nan"), -1
    for t in (i - 1, i - 2):
        if t - window + 1 < 0:
            continue
        seg = low[t - window + 1 : t + 1]
        if low[t] == seg.min() and close[i] > low[t] and close[i - 1] > low[t]:
            return float(low[t]), t
    return float("nan"), -1


# ════════════════════════════════════════════════════════════════════════
# Structure detection (uses confirmed swing instead of pivots)
# ════════════════════════════════════════════════════════════════════════
def _detect_structure(df: pd.DataFrame, atr_s: pd.Series) -> dict:
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    open_ = df["open"].values
    volume = df["volume"].values
    vol_ma = pd.Series(volume).rolling(20).mean().fillna(method="bfill").values
    atr = atr_s.values
    n = len(df)

    last_sh = np.nan; last_sl = np.nan
    prior_bull = False; prior_bear = False
    bull_bos = np.zeros(n, dtype=bool); bear_bos = np.zeros(n, dtype=bool)
    bull_choch = np.zeros(n, dtype=bool); bear_choch = np.zeros(n, dtype=bool)

    awaiting_b = False; awaiting_s = False
    sb_b = -10**9; sb_s = -10**9
    swick_lo = np.nan; swick_hi = np.nan
    sliq_b = False; sliq_s = False
    confirmed_b_last = False; confirmed_s_last = False

    fvg_bull: list[dict] = []
    fvg_bear: list[dict] = []

    ob_bH = np.nan; ob_bL = np.nan; ob_bV = False; ob_bBreak = 0
    ob_sH = np.nan; ob_sL = np.nan; ob_sV = False; ob_sBreak = 0

    sweep_lb = C.SWEEP_LOOKBACK

    for i in range(5, n):
        a = atr[i] if not np.isnan(atr[i]) else 0.0

        # ── confirmed rolling swing (replaces pivot fallback) ───────────
        sh, _ = _last_confirmed_swing_high(high, close, i)
        if not math.isnan(sh):
            last_sh = sh
        sl_, _ = _last_confirmed_swing_low(low, close, i)
        if not math.isnan(sl_):
            last_sl = sl_

        # ── BOS w/ body + volume filter ─────────────────────────────────
        body = abs(close[i] - open_[i])
        vol_mult = (volume[i] / vol_ma[i]) if vol_ma[i] and not np.isnan(vol_ma[i]) and vol_ma[i] > 0 else 1.0
        bos_quality = (body > a * MIN_BOS_BODY_ATR) and (vol_mult >= MIN_BOS_VOL_MULT)

        bbos = (not np.isnan(last_sh)) and close[i] > last_sh and close[i-1] <= last_sh and bos_quality
        sbos = (not np.isnan(last_sl)) and close[i] < last_sl and close[i-1] >= last_sl and bos_quality

        bull_bos[i] = bbos; bear_bos[i] = sbos
        if bbos:
            bull_choch[i] = prior_bear
            prior_bull, prior_bear = True, False
        if sbos:
            bear_choch[i] = prior_bull
            prior_bear, prior_bull = True, False

        # ── liquidity sweeps ────────────────────────────────────────────
        if i > sweep_lb:
            hh_prev = high[i - sweep_lb - 1: i].max()
            ll_prev = low [i - sweep_lb - 1: i].min()
            ext = a * 0.15
            raw_sh = high[i] > hh_prev + ext
            raw_sl = low[i] < ll_prev - ext
            rng = max(high[i] - low[i], 1e-9)
            wh = (high[i] - max(open_[i], close[i])) / rng
            wl = (min(open_[i], close[i]) - low[i]) / rng
            br = abs(close[i] - open_[i]) / rng
            rej_h = (wh > 0.35) or (br < 0.25)
            rej_l = (wl > 0.35) or (br < 0.25)
            liq_h = br < 0.15; liq_l = br < 0.15
            sweep_h_now = raw_sh and rej_h and (high[i] - max(open_[i], close[i])) > a * 0.25
            sweep_l_now = raw_sl and rej_l and (min(open_[i], close[i]) - low[i]) > a * 0.25
            if sweep_l_now:
                awaiting_b = True; sb_b = i; swick_lo = low[i]; sliq_b = liq_l
            if sweep_h_now:
                awaiting_s = True; sb_s = i; swick_hi = high[i]; sliq_s = liq_h

        b_conf_now = awaiting_b and (bbos or bull_choch[i])
        s_conf_now = awaiting_s and (sbos or bear_choch[i])
        if b_conf_now: awaiting_b = False
        if s_conf_now: awaiting_s = False
        if awaiting_b and (i - sb_b) > CONFIRM_WINDOW: awaiting_b = False
        if awaiting_s and (i - sb_s) > CONFIRM_WINDOW: awaiting_s = False
        confirmed_b_last = bool(b_conf_now); confirmed_s_last = bool(s_conf_now)

        # ── FVG (soft mitigation kept from v15) ─────────────────────────
        if i >= 2:
            if low[i] > high[i-2] and (low[i] - high[i-2]) > a * 0.15:
                fvg_bull.append({"top": low[i], "bot": high[i-2], "touched_bar": None})
            if high[i] < low[i-2] and (low[i-2] - high[i]) > a * 0.15:
                fvg_bear.append({"top": high[i], "bot": low[i-2], "touched_bar": None})
        new_bull = []
        for g in fvg_bull:
            if low[i] <= g["bot"]:
                if g["touched_bar"] is None: g["touched_bar"] = i
                if (i - g["touched_bar"]) <= FVG_GRACE_BARS: new_bull.append(g)
            else:
                new_bull.append(g)
        fvg_bull = new_bull
        new_bear = []
        for g in fvg_bear:
            if high[i] >= g["top"]:
                if g["touched_bar"] is None: g["touched_bar"] = i
                if (i - g["touched_bar"]) <= FVG_GRACE_BARS: new_bear.append(g)
            else:
                new_bear.append(g)
        fvg_bear = new_bear

        # ── OB creation on BOS ──────────────────────────────────────────
        if bbos and i >= 1:
            for k in range(1, min(7, i)):
                cR = high[i-k] - low[i-k]
                if cR <= 0: continue
                bear_c = close[i-k] < open_[i-k]
                disp = abs(close[max(i-k-1, 0)] - open_[max(i-k-1, 0)]) / a > 1.0 if a > 0 else False
                good = (close[i-k] - low[i-k]) / cR < 0.4 and disp
                if bear_c and good:
                    ob_bH, ob_bL, ob_bV, ob_bBreak = high[i-k], low[i-k], True, 0
                    break
        if sbos and i >= 1:
            for k in range(1, min(7, i)):
                cR = high[i-k] - low[i-k]
                if cR <= 0: continue
                bull_c = close[i-k] > open_[i-k]
                disp = abs(close[max(i-k-1, 0)] - open_[max(i-k-1, 0)]) / a > 1.0 if a > 0 else False
                good = (high[i-k] - close[i-k]) / cR < 0.4 and disp
                if bull_c and good:
                    ob_sH, ob_sL, ob_sV, ob_sBreak = high[i-k], low[i-k], True, 0
                    break
        if ob_bV:
            if close[i] < ob_bL:
                ob_bBreak += 1
                if ob_bBreak >= 2: ob_bV = False
            else: ob_bBreak = 0
        if ob_sV:
            if close[i] > ob_sH:
                ob_sBreak += 1
                if ob_sBreak >= 2: ob_sV = False
            else: ob_sBreak = 0

    active_bull = [g for g in fvg_bull if g["touched_bar"] is None or (n - 1 - g["touched_bar"]) <= FVG_GRACE_BARS]
    active_bear = [g for g in fvg_bear if g["touched_bar"] is None or (n - 1 - g["touched_bar"]) <= FVG_GRACE_BARS]

    return dict(
        bull_bos=bool(bull_bos[-1]), bear_bos=bool(bear_bos[-1]),
        bull_choch=bool(bull_choch[-1]), bear_choch=bool(bear_choch[-1]),
        bull_confirmed=confirmed_b_last, bear_confirmed=confirmed_s_last,
        sweep_wick_low=swick_lo, sweep_wick_high=swick_hi,
        sweep_was_liq_bull=sliq_b, sweep_was_liq_bear=sliq_s,
        active_bull_fvg=len(active_bull) > 0,
        active_bear_fvg=len(active_bear) > 0,
        bull_fvg_top=active_bull[-1]["top"] if active_bull else float("nan"),
        bear_fvg_bot=active_bear[-1]["bot"] if active_bear else float("nan"),
        ob_bull_valid=ob_bV, ob_bull_high=ob_bH, ob_bull_low=ob_bL,
        ob_bear_valid=ob_sV, ob_bear_high=ob_sH, ob_bear_low=ob_sL,
    )


# ════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════
def _btc_dir(btc_df_1h: Optional[pd.DataFrame]) -> Optional[str]:
    if btc_df_1h is None or len(btc_df_1h) < 50:
        return None
    ema = float(I.ema(btc_df_1h["close"], 50).iloc[-1])
    c = float(btc_df_1h["close"].iloc[-1])
    return "bull" if c > ema else "bear"


def _tier(score: int) -> str:
    if score >= TIER_AP: return "A+"
    if score >= TIER_A:  return "A"
    if score >= TIER_B:  return "B"
    return "C"


# ════════════════════════════════════════════════════════════════════════
# Main entry point
# ════════════════════════════════════════════════════════════════════════
def evaluate(df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame,
             df_1d: pd.DataFrame, symbol: str,
             btc_df_1h: Optional[pd.DataFrame] = None) -> Optional[dict]:
    if df_15m is None or len(df_15m) < 120 or df_4h is None or len(df_4h) < 60 or df_1h is None or len(df_1h) < 60:
        return None

    df = df_15m
    close, open_, high, low, vol = df["close"], df["open"], df["high"], df["low"], df["volume"]

    ema50_s = I.ema(close, C.EMA_LEN)
    atr14 = I.atr(df, 14)
    atr50 = I.atr(df, 50)
    vwap_s = I.vwap(df, 200)
    if pd.isna(atr14.iloc[-1]) or atr14.iloc[-1] <= 0:
        return None

    c = float(close.iloc[-1]); o = float(open_.iloc[-1])
    h = float(high.iloc[-1]); l = float(low.iloc[-1])
    a = float(atr14.iloc[-1])
    a50 = float(atr50.iloc[-1]) if not pd.isna(atr50.iloc[-1]) else a
    ema50 = float(ema50_s.iloc[-1])

    # ── HTF / MTF / Daily bias ─────────────────────────────────────────
    htf_close = float(df_4h["close"].iloc[-1]); htf_ema = float(I.ema(df_4h["close"], C.EMA_LEN).iloc[-1])
    mtf_close = float(df_1h["close"].iloc[-1]); mtf_ema = float(I.ema(df_1h["close"], C.EMA_LEN).iloc[-1])
    trend_close = float(df_1d["close"].iloc[-1]) if df_1d is not None and len(df_1d) else htf_close
    trend_ema   = float(I.ema(df_1d["close"], C.EMA_LEN).iloc[-1]) if df_1d is not None and len(df_1d) else htf_ema

    htf_bull = htf_close > htf_ema; htf_bear = htf_close < htf_ema
    mtf_bull = mtf_close > mtf_ema; mtf_bear = mtf_close < mtf_ema
    trend_bull = trend_close > trend_ema; trend_bear = trend_close < trend_ema

    # ── premium / discount zones (existing) ────────────────────────────
    hh = float(I.highest(high, C.LOOKBACK).iloc[-1])
    ll = float(I.lowest(low, C.LOOKBACK).iloc[-1])
    range_size = hh - ll
    premium  = hh - range_size * 0.25
    discount = ll + range_size * 0.25

    # ── range expansion gate (MANDATORY) ───────────────────────────────
    bar_range = h - l
    range_expansion_ratio = a / a50 if a50 > 0 else 1.0
    range_expansion = (range_expansion_ratio >= RANGE_EXP_ATR_RATIO) or (bar_range >= RANGE_EXP_BAR_RATIO * a)

    # ── session (lowercase, matches dashboard spec) ────────────────────
    utc_h = df["close_time"].iloc[-1].hour if "close_time" in df.columns else datetime.now(timezone.utc).hour
    in_london = C.LONDON_OPEN <= utc_h < C.LONDON_OPEN + C.SESSION_BUFFER
    in_ny     = C.NY_OPEN     <= utc_h < C.NY_OPEN     + C.SESSION_BUFFER
    in_prime  = in_london or in_ny
    session_norm = "london" if in_london else "new_york" if in_ny else ("asia" if utc_h < 7 else "off")

    above_vwap = c > float(vwap_s.iloc[-1]) if not pd.isna(vwap_s.iloc[-1]) else False
    below_vwap = c < float(vwap_s.iloc[-1]) if not pd.isna(vwap_s.iloc[-1]) else False

    # ── simplified strength (2 orthogonal components) ──────────────────
    body_atr = abs(c - o) / a if a > 0 else 0
    vol_avg = float(I.sma(vol, 20).iloc[-1]) if len(vol) >= 20 else float(vol.mean())
    vol_ema5 = float(I.ema(vol, 5).iloc[-1])
    vol_z = min(vol_ema5 / vol_avg, 1.5) if vol_avg > 0 else 1.0
    strength = 0.5 * body_atr + 0.5 * vol_z
    s_label = "WEAK" if strength < 0.6 else "MED" if strength < 1.0 else "STRONG" if strength < 1.4 else "ELITE"

    # ── structure module ───────────────────────────────────────────────
    st = _detect_structure(df, atr14)

    # ── SL / TP / RR ───────────────────────────────────────────────────
    swing_low5  = float(I.lowest(low, 5).iloc[-1])
    swing_high5 = float(I.highest(high, 5).iloc[-1])
    eq_hi1 = float(I.highest(high, C.LOOKBACK).iloc[-1])
    eq_hi2 = float(I.highest(high, C.LOOKBACK * 2).iloc[-1])
    eq_lo1 = float(I.lowest(low,  C.LOOKBACK).iloc[-1])
    eq_lo2 = float(I.lowest(low,  C.LOOKBACK * 2).iloc[-1])

    long_sl_base  = st["sweep_wick_low"]  if not math.isnan(st["sweep_wick_low"])  else swing_low5
    short_sl_base = st["sweep_wick_high"] if not math.isnan(st["sweep_wick_high"]) else swing_high5
    long_sl  = long_sl_base  - a * SL_ATR_BUFFER
    short_sl = short_sl_base + a * SL_ATR_BUFFER
    long_risk  = c - long_sl
    short_risk = short_sl - c

    # risk-cap mandatory: skip oversized stops
    if long_risk > a * RISK_CAP_ATR and short_risk > a * RISK_CAP_ATR:
        return None

    long_tp1  = eq_hi1 if eq_hi1 > c else c + long_risk * MIN_RR
    long_tp2  = eq_hi2 if eq_hi2 > long_tp1 else long_tp1 + long_risk
    long_tp3  = long_tp2 + a
    short_tp1 = eq_lo1 if eq_lo1 < c else c - short_risk * MIN_RR
    short_tp2 = eq_lo2 if eq_lo2 < short_tp1 else short_tp1 - short_risk
    short_tp3 = short_tp2 - a
    long_rr1  = (long_tp1 - c) / long_risk if long_risk > 0 else 0
    short_rr1 = (c - short_tp1) / short_risk if short_risk > 0 else 0
    long_rr2  = (long_tp2 - c) / long_risk if long_risk > 0 else 0
    short_rr2 = (c - short_tp2) / short_risk if short_risk > 0 else 0
    long_rr3  = (long_tp3 - c) / long_risk if long_risk > 0 else 0
    short_rr3 = (c - short_tp3) / short_risk if short_risk > 0 else 0

    # ── near-zone (for entry validity) ─────────────────────────────────
    near_disc = c <= discount + a * 0.75
    near_prem = c >= premium  - a * 0.75
    near_long_zone  = (near_disc or
                       (st["active_bull_fvg"] and c <= st["bull_fvg_top"] + a * 0.5) or
                       (st["ob_bull_valid"]  and c <= st["ob_bull_high"] + a * 0.5))
    near_short_zone = (near_prem or
                       (st["active_bear_fvg"] and c >= st["bear_fvg_bot"] - a * 0.5) or
                       (st["ob_bear_valid"]  and c >= st["ob_bear_low"]  - a * 0.5))

    # ── retest path (for BOS+retest entries) ───────────────────────────
    retest_long  = ((st["bull_bos"] or st["bull_choch"]) and (st["active_bull_fvg"] or st["ob_bull_valid"]) and htf_bull)
    retest_short = ((st["bear_bos"] or st["bear_choch"]) and (st["active_bear_fvg"] or st["ob_bear_valid"]) and htf_bear)

    # ── MANDATORY GATES ────────────────────────────────────────────────
    long_mandatory = (
        htf_bull
        and (st["bull_confirmed"] or retest_long)
        and range_expansion
        and near_long_zone
        and long_rr1 >= MIN_RR
        and long_risk > 0 and long_risk <= a * RISK_CAP_ATR
    )
    short_mandatory = (
        htf_bear
        and (st["bear_confirmed"] or retest_short)
        and range_expansion
        and near_short_zone
        and short_rr1 >= MIN_RR
        and short_risk > 0 and short_risk <= a * RISK_CAP_ATR
    )
    if not (long_mandatory or short_mandatory):
        return None

    side = "LONG" if long_mandatory else "SHORT"
    is_long = side == "LONG"
    is_btc = symbol.upper().startswith("BTC")
    btc_dir = _btc_dir(btc_df_1h)
    btc_aligned = (not is_btc) and btc_dir is not None and (
        (is_long and btc_dir == "bull") or ((not is_long) and btc_dir == "bear")
    )

    # ── WEIGHTED SCORE (max 15) ────────────────────────────────────────
    score = 0
    flags: dict[str, int] = {}

    def _add(name: str, cond: bool, weight: int):
        nonlocal score
        if cond:
            score += weight
            flags[name] = weight
        else:
            flags[name] = 0

    _add("mtf_aligned",   (is_long and mtf_bull) or ((not is_long) and mtf_bear),     2)
    _add("daily_aligned", (is_long and trend_bull) or ((not is_long) and trend_bear), 1)
    _add("liq_sweep",     (is_long and not math.isnan(st["sweep_wick_low"])) or
                          ((not is_long) and not math.isnan(st["sweep_wick_high"])),  2)
    _add("fvg_or_ob",     (is_long and (st["active_bull_fvg"] or st["ob_bull_valid"])) or
                          ((not is_long) and (st["active_bear_fvg"] or st["ob_bear_valid"])), 2)
    _add("zone",          (is_long and c <= discount) or ((not is_long) and c >= premium), 2)
    _add("vwap_aligned",  (is_long and above_vwap) or ((not is_long) and below_vwap),  1)
    _add("prime_session", in_prime,                                                    1)
    _add("strength_ok",   strength >= MIN_STRENGTH,                                    2)
    _add("btc_aligned",   btc_aligned,                                                 2)

    tier = _tier(score)

    # ── derive classification fields (unchanged contract) ──────────────
    entry_path = "Sweep→BOS" if ((is_long and st["bull_confirmed"]) or
                                  ((not is_long) and st["bear_confirmed"])) else "BOS+Retest"
    if entry_path == "Sweep→BOS":
        setup_type = "sweep_reclaim"
    elif (is_long and st["ob_bull_valid"]) or ((not is_long) and st["ob_bear_valid"]):
        setup_type = "ob_reversal"
    elif (is_long and st["active_bull_fvg"]) or ((not is_long) and st["active_bear_fvg"]):
        setup_type = "fvg_continuation"
    else:
        setup_type = "deviation_breakout"
    entry_model = "confirmation" if (st["bull_confirmed"] or st["bear_confirmed"]) else \
                  "reclaim" if (st["bull_bos"] or st["bear_bos"]) else "aggressive"
    liquidity_event = None
    if is_long and not math.isnan(st["sweep_wick_low"]):
        liquidity_event = "liq_wick_low_swept" if st["sweep_was_liq_bull"] else "swing_low_swept"
    elif (not is_long) and not math.isnan(st["sweep_wick_high"]):
        liquidity_event = "liq_wick_high_swept" if st["sweep_was_liq_bear"] else "swing_high_swept"
    htf_bias = "bull" if htf_bull else "bear" if htf_bear else "neutral"
    regime_norm = "trending" if range_expansion_ratio >= 1.15 else "ranging"

    entry = c
    sl  = long_sl  if is_long else short_sl
    tp1 = long_tp1 if is_long else short_tp1
    tp2 = long_tp2 if is_long else short_tp2
    tp3 = long_tp3 if is_long else short_tp3
    rr1 = long_rr1 if is_long else short_rr1
    rr2 = long_rr2 if is_long else short_rr2
    rr3 = long_rr3 if is_long else short_rr3
    risk_pct = abs(entry - sl) / entry * 100.0

    confluence = {
        "HTF (4h)":    "bull" if htf_bull else "bear",
        "MTF (1h)":    "bull" if mtf_bull else "bear",
        "Daily":       "bull" if trend_bull else "bear" if trend_bear else "flat",
        "VWAP":        "above" if above_vwap else "below",
        "Zone":        "premium" if c >= premium else "discount" if c <= discount else "equil",
        "Session":     session_norm,
        "RangeExp":    f"{range_expansion_ratio:.2f}×",
        "BTC":         btc_dir or "n/a",
        "Flags":       flags,
    }

    return {
        "symbol": symbol, "side": side, "timeframe": C.ENTRY_TF,
        "entry": float(entry), "sl": float(sl),
        "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3),
        "rr1": float(rr1), "rr2": float(rr2), "rr3": float(rr3),
        "risk_pct": float(risk_pct),
        # Phase 1 score is the canonical one — exposed both as `score`
        # (back-compat numeric) and `score_total` (new dashboard field).
        "score": int(score), "max_score": 15, "pct": float(score / 15.0),
        "score_total": int(score),
        "range_expansion_ratio": float(range_expansion_ratio),
        "grade": tier, "tier": tier,
        "strength": float(strength), "strength_label": s_label,
        "regime": regime_norm,
        "entry_path": entry_path,
        "session": session_norm,
        "setup_type": setup_type,
        "entry_model": entry_model,
        "liquidity_event": liquidity_event,
        "htf_bias": htf_bias,
        "confluence": confluence,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
