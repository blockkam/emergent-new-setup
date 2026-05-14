"""
MySetup v15 strategy — drop-in replacement for strategy.py.

Upgrades over v14:
  1. Soft FVG mitigation — keeps FVG eligible for retest entries up to
     FVG_GRACE_BARS bars after first touch (was: deleted on first touch).
  2. Tighter sweep confirmation window (default 7 instead of 12).
  3. Wider SL buffer (0.25 ATR instead of 0.10) — survives normal hunt-wicks.
  4. Volume + body confirmation on BOS bar — kills fake breakouts.
  5. HTF structure check (4h higher-low rising) — beyond just EMA bias.
  6. Tier classification: S / A / B (S = premium, A = standard, B = early watch).
  7. BTC regime gate — skip alt shorts when BTC 1h is bullish (and vice-versa).
     Pass `btc_df_1h` into evaluate() to enable; pass None to disable.
  8. Hard cooldown — 3 bars minimum, no A-grade bypass.

Public entry point:
    evaluate(df_15m, df_1h, df_4h, df_1d, symbol, btc_df_1h=None) -> dict | None

The returned dict is shape-compatible with v14 plus a new `tier` field
and a richer `confluence` dict. Drop the dict straight into your
tracker_client.post_signal(sig) call.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

import config as C
import indicators as I

# ── v15 knobs (override via config.py if you want) ──────────────────────
FVG_GRACE_BARS         = getattr(C, "FVG_GRACE_BARS", 5)
SL_ATR_BUFFER          = getattr(C, "SL_ATR_BUFFER", 0.25)
CONFIRM_WINDOW         = getattr(C, "CONFIRM_WINDOW", 7)
MIN_STRENGTH_TREND     = getattr(C, "MIN_STRENGTH_TREND", 0.55)
MIN_STRENGTH_RANGE     = getattr(C, "MIN_STRENGTH_RANGE", 0.75)
MIN_RR                 = getattr(C, "MIN_RR", 1.8)
MIN_BOS_BODY_ATR       = getattr(C, "MIN_BOS_BODY_ATR", 0.6)
MIN_BOS_VOL_MULT       = getattr(C, "MIN_BOS_VOL_MULT", 1.2)
HARD_COOLDOWN_BARS     = getattr(C, "HARD_COOLDOWN_BARS", 3)


def _detect_structure(df: pd.DataFrame, atr_s: pd.Series, pivot_len: int) -> dict:
    high, low, close, open_ = (df["high"].values, df["low"].values,
                                df["close"].values, df["open"].values)
    volume = df["volume"].values
    vol_ma = pd.Series(volume).rolling(20).mean().fillna(method="bfill").values
    atr = atr_s.values
    n = len(df)

    piv_hi = I.pivot_high(df["high"], pivot_len, pivot_len).values
    piv_lo = I.pivot_low(df["low"], pivot_len, pivot_len).values

    last_sh = np.nan; last_sl = np.nan
    prior_bull = False; prior_bear = False
    bull_bos = np.zeros(n, dtype=bool); bear_bos = np.zeros(n, dtype=bool)
    bull_choch = np.zeros(n, dtype=bool); bear_choch = np.zeros(n, dtype=bool)

    awaiting_b = False; awaiting_s = False
    sb_b = -10**9; sb_s = -10**9
    swick_lo = np.nan; swick_hi = np.nan
    sliq_b = False; sliq_s = False
    confirmed_b_last = False; confirmed_s_last = False

    fvg_bull: list[dict] = []  # soft mitigation: each has 'touched_bar'
    fvg_bear: list[dict] = []

    ob_bH = np.nan; ob_bL = np.nan; ob_bV = False; ob_bBreak = 0
    ob_sH = np.nan; ob_sL = np.nan; ob_sV = False; ob_sBreak = 0

    sweep_lb = C.SWEEP_LOOKBACK

    for i in range(5, n):
        a = atr[i] if not np.isnan(atr[i]) else 0.0

        # swing update
        if (high[i-3] > high[i-5] and high[i-3] > high[i-4] and
                high[i-3] > high[i-2] and high[i-3] > high[i-1]):
            last_sh = high[i-3]
        elif not np.isnan(piv_hi[i]):
            last_sh = piv_hi[i]
        if (low[i-3] < low[i-5] and low[i-3] < low[i-4] and
                low[i-3] < low[i-2] and low[i-3] < low[i-1]):
            last_sl = low[i-3]
        elif not np.isnan(piv_lo[i]):
            last_sl = piv_lo[i]

        # BOS with v15 volume + body filter
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

        # sweeps
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

        # FVG with soft mitigation
        if i >= 2:
            if low[i] > high[i-2] and (low[i] - high[i-2]) > a * 0.15:
                fvg_bull.append({"top": low[i], "bot": high[i-2], "bar": i, "touched_bar": None})
            if high[i] < low[i-2] and (low[i-2] - high[i]) > a * 0.15:
                fvg_bear.append({"top": high[i], "bot": low[i-2], "bar": i, "touched_bar": None})

        # soft-mitigation logic
        new_bull = []
        for g in fvg_bull:
            if low[i] <= g["bot"]:
                if g["touched_bar"] is None:
                    g["touched_bar"] = i
                # keep for FVG_GRACE_BARS after first touch
                if (i - g["touched_bar"]) <= FVG_GRACE_BARS:
                    new_bull.append(g)
            else:
                new_bull.append(g)
        fvg_bull = new_bull
        new_bear = []
        for g in fvg_bear:
            if high[i] >= g["top"]:
                if g["touched_bar"] is None:
                    g["touched_bar"] = i
                if (i - g["touched_bar"]) <= FVG_GRACE_BARS:
                    new_bear.append(g)
            else:
                new_bear.append(g)
        fvg_bear = new_bear

        # OB creation on BOS
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
        # 2-close OB invalidation
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

    # active FVG only if not yet touched OR within grace window
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


def _htf_structure_bull(df_4h: pd.DataFrame) -> bool:
    """4h higher-low rising over last ~10 bars."""
    if df_4h is None or len(df_4h) < 12:
        return True
    lows = df_4h["low"].values
    return lows[-3:].min() > lows[-10:-5].min()


def _htf_structure_bear(df_4h: pd.DataFrame) -> bool:
    if df_4h is None or len(df_4h) < 12:
        return True
    highs = df_4h["high"].values
    return highs[-3:].max() < highs[-10:-5].max()


def _btc_blocks_alt_short(btc_df_1h: Optional[pd.DataFrame]) -> bool:
    if btc_df_1h is None or len(btc_df_1h) < 50:
        return False
    ema = I.ema(btc_df_1h["close"], 50).iloc[-1]
    return float(btc_df_1h["close"].iloc[-1]) > float(ema)


def _btc_blocks_alt_long(btc_df_1h: Optional[pd.DataFrame]) -> bool:
    if btc_df_1h is None or len(btc_df_1h) < 50:
        return False
    ema = I.ema(btc_df_1h["close"], 50).iloc[-1]
    return float(btc_df_1h["close"].iloc[-1]) < float(ema)


def evaluate(df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame,
             df_1d: pd.DataFrame, symbol: str,
             btc_df_1h: Optional[pd.DataFrame] = None) -> Optional[dict]:
    if df_15m is None or len(df_15m) < 120 or df_4h is None or len(df_4h) < 60 or df_1h is None or len(df_1h) < 60:
        return None

    df = df_15m
    close, open_, high, low, vol = df["close"], df["open"], df["high"], df["low"], df["volume"]

    ema50_s = I.ema(close, C.EMA_LEN)
    atr_s = I.atr(df, C.ATR_LEN)
    atr_fast = I.atr(df, 7); atr_slow = I.atr(df, 28)
    ma7 = I.ema(close, 7); ma26 = I.ema(close, 26); ma99 = I.ema(close, 99)
    vwap_s = I.vwap(df, 200)
    if pd.isna(atr_s.iloc[-1]) or atr_s.iloc[-1] <= 0:
        return None

    c = float(close.iloc[-1]); o = float(open_.iloc[-1]); a = float(atr_s.iloc[-1])
    ema50 = float(ema50_s.iloc[-1])
    ema50_5 = float(ema50_s.iloc[-6]) if len(ema50_s) >= 6 else ema50
    ema50_10 = float(ema50_s.iloc[-11]) if len(ema50_s) >= 11 else ema50

    htf_close = float(df_4h["close"].iloc[-1]); htf_ema = float(I.ema(df_4h["close"], C.EMA_LEN).iloc[-1])
    mtf_close = float(df_1h["close"].iloc[-1]); mtf_ema = float(I.ema(df_1h["close"], C.EMA_LEN).iloc[-1])
    trend_close = float(df_1d["close"].iloc[-1]) if df_1d is not None and len(df_1d) else htf_close
    trend_ema = float(I.ema(df_1d["close"], C.EMA_LEN).iloc[-1]) if df_1d is not None and len(df_1d) else htf_ema

    htf_bull = htf_close > htf_ema; htf_bear = htf_close < htf_ema
    mtf_bull = mtf_close > mtf_ema; mtf_bear = mtf_close < mtf_ema
    trend_bull = trend_close > trend_ema; trend_bear = trend_close < trend_ema

    # v15: HTF structure check on top of MA bias
    htf_struct_bull = _htf_structure_bull(df_4h)
    htf_struct_bear = _htf_structure_bear(df_4h)

    hh = float(I.highest(high, C.LOOKBACK).iloc[-1])
    ll = float(I.lowest(low, C.LOOKBACK).iloc[-1])
    range_size = hh - ll
    premium = hh - range_size * 0.25
    discount = ll + range_size * 0.25

    is_trending = (atr_fast.iloc[-1] > atr_slow.iloc[-1] * 1.05 and abs(ema50 - ema50_10) / a > 0.3)
    regime_mult = 1.0 if is_trending else 0.75
    min_strength = MIN_STRENGTH_TREND if is_trending else MIN_STRENGTH_RANGE

    utc_h = df["close_time"].iloc[-1].hour if "close_time" in df.columns else datetime.now(timezone.utc).hour
    in_london = C.LONDON_OPEN <= utc_h < C.LONDON_OPEN + C.SESSION_BUFFER
    in_ny     = C.NY_OPEN     <= utc_h < C.NY_OPEN + C.SESSION_BUFFER
    in_prime  = in_london or in_ny
    session_bonus = 1 if (C.USE_SESSION and in_prime) else 0

    above_vwap = c > float(vwap_s.iloc[-1]) if not pd.isna(vwap_s.iloc[-1]) else False
    below_vwap = c < float(vwap_s.iloc[-1]) if not pd.isna(vwap_s.iloc[-1]) else False
    ma_long_bias  = c > float(ma7.iloc[-1]) and c > float(ma26.iloc[-1])
    ma_short_bias = c < float(ma7.iloc[-1]) and c < float(ma26.iloc[-1])
    ma_long_99    = c > float(ma99.iloc[-1])
    ma_short_99   = c < float(ma99.iloc[-1])
    bull = c > ema50; bear = c < ema50

    bull_score = int(bull) + int(htf_bull) + int(ma_long_bias) + int(above_vwap) + int(ma_long_99) + int(mtf_bull) + session_bonus
    bear_score = int(bear) + int(htf_bear) + int(ma_short_bias) + int(below_vwap) + int(ma_short_99) + int(mtf_bear) + session_bonus

    # strength
    momentum = abs(c - o) / a
    impulse = abs(c - float(close.iloc[-2])) / a if len(close) >= 2 else 0.0
    push = abs(c - float(close.iloc[-4])) / a if len(close) >= 4 else 0.0
    trend_slope = abs(ema50 - ema50_5) / a
    rng = float(high.iloc[-1] - low.iloc[-1])
    imbalance = abs(c - o) / rng if rng > 0 else 0.5
    vol_avg = float(I.sma(vol, 20).iloc[-1]) if len(vol) >= 20 else float(vol.mean())
    vol_ema5 = float(I.ema(vol, 5).iloc[-1])
    vol_score = min(vol_ema5 / vol_avg, 1.8) if vol_avg > 0 else 1.0
    strength = (momentum * 0.25 + impulse * 0.20 + push * 0.15 + trend_slope * 0.15 + imbalance * 0.15 + vol_score * 0.10) * regime_mult
    valid_strength = strength >= min_strength
    s_label = "WEAK" if strength < 0.6 else "MED" if strength < 1.0 else "STRONG" if strength < 1.4 else "ELITE"

    st = _detect_structure(df, atr_s, C.PIVOT_LEN)

    # SL / TP
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
    rr_gate_bull = long_rr1 >= MIN_RR
    rr_gate_bear = short_rr1 >= MIN_RR

    near_disc = c <= discount + a * 0.75
    near_prem = c >= premium - a * 0.75
    near_long_zone  = (near_disc or
                      (st["active_bull_fvg"] and c <= st["bull_fvg_top"] + a * 0.5) or
                      (st["ob_bull_valid"]  and c <= st["ob_bull_high"] + a * 0.5))
    near_short_zone = (near_prem or
                      (st["active_bear_fvg"] and c >= st["bear_fvg_bot"] - a * 0.5) or
                      (st["ob_bear_valid"]  and c >= st["ob_bear_low"]  - a * 0.5))

    body0 = abs(c - o)
    body1 = abs(float(close.iloc[-2]) - float(open_.iloc[-2])) if len(close) >= 2 else 0
    body2 = abs(float(close.iloc[-3]) - float(open_.iloc[-3])) if len(close) >= 3 else 0
    displacement = max(body0, body1, body2) > a * 0.5

    adj_min = C.MIN_SCORE if is_trending else max(C.MIN_SCORE - 1, 1)
    liq_need = adj_min + 1

    retest_long  = ((st["bull_bos"] or st["bull_choch"]) and (st["active_bull_fvg"] or st["ob_bull_valid"]) and htf_bull)
    retest_short = ((st["bear_bos"] or st["bear_choch"]) and (st["active_bear_fvg"] or st["ob_bear_valid"]) and htf_bear)

    regime_gate = is_trending or strength > min_strength * 1.2
    bull_ok = bull_score >= (liq_need if st["sweep_was_liq_bull"] else adj_min)
    bear_ok = bear_score >= (liq_need if st["sweep_was_liq_bear"] else adj_min)

    # v15: BTC correlation gate (only applied to alts; pass btc_df_1h=None to disable)
    is_btc = symbol.upper().startswith("BTC")
    btc_block_short = (not is_btc) and _btc_blocks_alt_short(btc_df_1h)
    btc_block_long  = (not is_btc) and _btc_blocks_alt_long(btc_df_1h)

    long_entry  = ((st["bull_confirmed"] or retest_long)  and htf_bull and htf_struct_bull and
                   valid_strength and rr_gate_bull and displacement and near_long_zone and
                   regime_gate and bull_ok and not btc_block_long)
    short_entry = ((st["bear_confirmed"] or retest_short) and htf_bear and htf_struct_bear and
                   valid_strength and rr_gate_bear and displacement and near_short_zone and
                   regime_gate and bear_ok and not btc_block_short)

    if not (long_entry or short_entry):
        return None

    side = "LONG" if long_entry else "SHORT"
    entry = c
    sl = long_sl if long_entry else short_sl
    tp1 = long_tp1 if long_entry else short_tp1
    tp2 = long_tp2 if long_entry else short_tp2
    tp3 = long_tp3 if long_entry else short_tp3
    rr1 = long_rr1 if long_entry else short_rr1
    rr2 = long_rr2 if long_entry else short_rr2
    rr3 = long_rr3 if long_entry else short_rr3

    entry_path = "Sweep→BOS" if ((long_entry and st["bull_confirmed"]) or (short_entry and st["bear_confirmed"])) else "BOS+Retest"

    dom = max(bull_score, bear_score)
    total = dom + (1 if valid_strength else 0)
    pct = total / 8.0
    grade = "A+" if pct >= 0.85 else "A" if pct >= 0.72 else "B" if pct >= 0.58 else "C" if pct >= 0.44 else "D"

    # v15 tier classification
    if (grade in ("A+", "A") and is_trending and in_prime and strength >= 1.0
            and vol_score >= 1.2 and entry_path == "Sweep→BOS"
            and ((long_entry and st["sweep_was_liq_bull"]) or (short_entry and st["sweep_was_liq_bear"]))):
        tier = "S"
    elif grade in ("A+", "A") and ((long_entry and rr1 >= 1.8) or (short_entry and rr1 >= 1.8)):
        tier = "A"
    else:
        tier = "B"

    risk_pct = abs(entry - sl) / entry * 100.0

    # ── v15.1 advanced classification ─────────────────────────────────
    # setup_type: pick the strongest active structure feature
    if entry_path == "Sweep→BOS":
        setup_type = "sweep_reclaim"
    elif (long_entry and st["ob_bull_valid"]) or (short_entry and st["ob_bear_valid"]):
        setup_type = "ob_reversal"
    elif (long_entry and st["active_bull_fvg"]) or (short_entry and st["active_bear_fvg"]):
        setup_type = "fvg_continuation"
    else:
        setup_type = "deviation_breakout"

    # entry_model: aggressive (no confirmation), confirmation (BOS/CHoCH already closed),
    # reclaim (price reclaimed the broken level)
    if (long_entry and st["bull_confirmed"]) or (short_entry and st["bear_confirmed"]):
        entry_model = "confirmation"
    elif (long_entry and st["bull_bos"]) or (short_entry and st["bear_bos"]):
        entry_model = "reclaim"
    else:
        entry_model = "aggressive"

    # liquidity_event: only set when the entry was preceded by a sweep
    liquidity_event = None
    if long_entry and not math.isnan(st["sweep_wick_low"]):
        liquidity_event = "asia_low_swept" if (in_london or in_ny) else "swing_low_swept"
        if st["sweep_was_liq_bull"]:
            liquidity_event = "liq_wick_low_swept"
    elif short_entry and not math.isnan(st["sweep_wick_high"]):
        liquidity_event = "asia_high_swept" if (in_london or in_ny) else "swing_high_swept"
        if st["sweep_was_liq_bear"]:
            liquidity_event = "liq_wick_high_swept"

    # htf_bias: normalized lowercase for clean grouping
    if htf_bull and htf_struct_bull:
        htf_bias = "bull"
    elif htf_bear and htf_struct_bear:
        htf_bias = "bear"
    else:
        htf_bias = "neutral"

    # normalized regime + session (lowercase, matches user's classification spec)
    regime_norm = "trending" if is_trending else "ranging"
    if in_london:
        session_norm = "london"
    elif in_ny:
        session_norm = "new_york"
    elif utc_h < 7:
        session_norm = "asia"
    else:
        session_norm = "off"

    confluence = {
        "EMA50 bias": "BULL" if bull else "BEAR",
        "HTF (4h)":   "BULL" if htf_bull else "BEAR",
        "HTF struct": "BULL" if htf_struct_bull else "BEAR" if htf_struct_bear else "MIXED",
        "MTF (1h)":   "BULL" if mtf_bull else "BEAR",
        "Trend (1d)": "BULL" if trend_bull else "BEAR" if trend_bear else "FLAT",
        "MA 7/26":    "BULL" if ma_long_bias else "BEAR" if ma_short_bias else "MIXED",
        "MA99":       "ABOVE" if ma_long_99 else "BELOW",
        "VWAP":       "ABOVE" if above_vwap else "BELOW",
        "Session":    "LONDON" if in_london else "NY" if in_ny else "OFF",
        "Vol score":  f"{vol_score:.2f}x",
        "Zone":       "PREMIUM" if c >= premium else "DISCOUNT" if c <= discount else "EQUIL",
        "Regime":     "TRENDING" if is_trending else "RANGING",
        "BTC gate":   "BLOCKED" if (btc_block_long or btc_block_short) else "OK",
    }

    return {
        "symbol": symbol, "side": side, "timeframe": C.ENTRY_TF,
        "entry": float(entry), "sl": float(sl),
        "tp1": float(tp1), "tp2": float(tp2), "tp3": float(tp3),
        "rr1": float(rr1), "rr2": float(rr2), "rr3": float(rr3),
        "risk_pct": float(risk_pct),
        "score": int(total), "max_score": 8, "pct": float(pct),
        "grade": grade, "tier": tier,
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
