# MySetup v15 · Phase 1 — Integration guide

This folder contains the **Phase 1 refined** strategy + cooldown helper.
The dashboard, backend, and tracker_client are unchanged.

## Files

| File | Purpose |
|---|---|
| `strategy_v15.py` | Drop-in replacement for `strategy.py` — Phase 1 logic |
| `cooldown.py`     | Cooldown + alert-cluster cap helper (lightweight) |
| `tracker_client.py` | HTTP client that POSTs signals to the dashboard |

## What Phase 1 changed

1. **2-bar confirmed rolling swing** replaces lagging pivot detection — saves ~30 min on 15m
2. **Mandatory range-expansion gate**: `ATR(14) ≥ 1.1 × ATR(50)` OR `bar_range ≥ 0.7 × ATR`
3. **Weighted scoring 0..15** → tier A+/A/B/C (C is log-only)
4. **Simplified 2-component strength** (was 6, all correlated)
5. **Min RR 1.8** + risk cap ≤ 1.5 ATR
6. **Removed**: MA7/26, MA99, imbalance/push, adaptive thresholds, A-grade cooldown bypass
7. **BTC alignment** is now a +2 weighted bonus (was a hard gate)
8. **Cooldown** is now in `cooldown.py`, callable from the scanner

## Mandatory gates (a signal returns `None` unless all pass)

| # | Condition |
|---|---|
| M1 | HTF (4h) close on the correct side of EMA50 |
| M2 | 15m BOS w/ body ≥ 0.5 ATR AND volume ≥ 1.1× SMA20 |
| M3 | Range expansion ON (ATR14/ATR50 ≥ 1.1 OR bar-range ≥ 0.7×ATR) |
| M4 | Price near zone (discount/premium OR active FVG/OB) |
| M5 | RR(TP1) ≥ 1.8 AND risk ≤ 1.5×ATR |

## Weighted score (max 15)

| Signal | Weight |
|---|---|
| MTF (1h) aligned                    | +2 |
| Daily aligned                       | +1 |
| Liquidity sweep present             | +2 |
| Active FVG **or** OB (counts once)  | +2 |
| In discount (long) / premium (short)| +2 |
| VWAP aligned                        | +1 |
| London or NY session                | +1 |
| Strength ≥ 0.7                      | +2 |
| BTC aligned (alts only)             | +2 |

Tier mapping: **A+ ≥ 12, A ≥ 9, B ≥ 6, C < 6** (C = log only, don't alert).

## Configurable knobs (override in `config.py` if desired)

```python
SL_ATR_BUFFER        = 0.25
RISK_CAP_ATR         = 1.5
MIN_BOS_BODY_ATR     = 0.5
MIN_BOS_VOL_MULT     = 1.1
MIN_RR               = 1.8
RANGE_EXP_ATR_RATIO  = 1.1
RANGE_EXP_BAR_RATIO  = 0.7
MIN_STRENGTH         = 0.7
FVG_GRACE_BARS       = 5
CONFIRM_WINDOW       = 7
TIER_AP              = 12
TIER_A               = 9
TIER_B               = 6
```

And cooldown (override via `os.environ` or a `.env`):
```
GLOBAL_COOLDOWN_MIN      = 45     # 3 bars
PER_SYMBOL_COOLDOWN_MIN  = 180    # 12 bars
POST_LOSS_COOLDOWN_MIN   = 360    # 24 bars
COOLDOWN_STATE_FILE      = cooldown_state.json
```

## Scanner-loop integration (3 patches)

```python
from strategy import evaluate
from tracker_client import post_signal
from cooldown import Cooldown

cd = Cooldown()           # auto-creates cooldown_state.json

candidates = []
for symbol in symbols:
    ok, reason = cd.allow(symbol)
    if not ok:
        continue
    sig = evaluate(df_15m, df_1h, df_4h, df_1d, symbol, btc_df_1h=btc_df)
    if sig is None:
        continue
    if sig["tier"] == "C":
        post_signal(sig)              # still log to dashboard for analytics
        continue
    candidates.append(sig)

# alert-cluster cap: keep only the top 5 per tick
for sig in Cooldown.pick_top(candidates, n=5):
    send_telegram_alert(sig)
    post_signal(sig)
    cd.record_signal(sig["symbol"])
```

When a trade resolves as a loss (you can hook this into your tracker
poller or just call it manually for now):

```python
cd.record_loss(symbol)
```

## Output dict shape

Adds two new numeric fields next to the existing classification fields:

```python
{
  # ...existing fields...
  "score_total": 11,               # 0..15
  "range_expansion_ratio": 1.27,   # ATR(14)/ATR(50) at entry
  "tier": "A",                     # A+/A/B/C
  "confluence": { "Flags": {...weighted breakdown per signal...} }
}
```

The dashboard already accepts these (extra fields are silently allowed
on the API). The fields are also persisted in MongoDB so you can later
slice the metrics dashboard by `score_total` bucket and
`range_expansion_ratio` bin.

## Migration notes

* **Output shape is back-compatible.** Existing `score`, `max_score`,
  `grade`, `tier` are still present. `score` and `score_total` are now
  the same Phase 1 weighted value; old consumers won't break.
* **Tier scheme** is now 4 buckets (A+/A/B/C). Drop the old D tier from
  any client-side filters/alerts.
* **Telegram alerts**: filter to `tier in {"A+", "A", "B"}` (or just
  A/A+). Drop C from alerts; keep them in dashboard.
* **Cooldown state** is process-local (JSON file). If you run multiple
  scanner processes, point them at the same file via
  `COOLDOWN_STATE_FILE`.
* **BTC alignment** is now optional (weighted +2). If you don't pass
  `btc_df_1h`, you simply forfeit that 2-point bonus — nothing breaks.
* **Calibration target**: after ~60 resolved trades, regress
  `result_r` against the `confluence.Flags.<name>` weights; bump any
  flag's weight ±1 where the regression coefficient strongly disagrees.

## Postponed (do NOT implement until ≥ 200 resolved trades)

* Taker buy/sell delta gate
* BTC dominance regime
* Volatility-regime adaptive weights
* ML scoring
* Per-symbol parameter tuning
