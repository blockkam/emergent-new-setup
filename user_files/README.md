# MySetup v15 — Integration guide

This folder contains everything you need to plug your existing scanner into the
**hosted MySetup tracker dashboard**.

## What you got

| File | Purpose |
|---|---|
| `strategy_v15.py` | Drop-in replacement for `strategy.py` with all v15 upgrades |
| `tracker_client.py` | Tiny HTTP helper that POSTs each signal to the dashboard |

## Step 1 — replace `strategy.py`

```bash
mv strategy.py strategy_v14_backup.py
cp strategy_v15.py strategy.py
```

The `evaluate()` signature is unchanged (drop-in), so nothing else needs to be
edited *unless* you want to enable the optional BTC correlation gate (see step 4).

## Step 2 — add `tracker_client.py` next to `strategy.py`

Just copy the file into the same folder as `strategy.py` and `config.py`.

## Step 3 — wire it into your scanner loop

In your main scanner file (wherever you call `strategy.evaluate(...)`), import
the client and POST every signal:

```python
from tracker_client import post_signal

sig = strategy.evaluate(df_15m, df_1h, df_4h, df_1d, symbol)
if sig:
    send_telegram_alert(sig)   # whatever you do today
    post_signal(sig)           # NEW — fires-and-forgets to dashboard
```

Then set the dashboard URL in your `.env`:

```
DASHBOARD_URL=https://<your-app>.preview.emergentagent.com
```

That's it. Failures inside `post_signal` are swallowed silently — your scanner
will never crash because the dashboard is down.

## Step 4 — (optional) BTC correlation gate

To skip alt shorts when BTC 1h is bullish (and vice-versa), pass the BTC 1h
DataFrame as the last argument:

```python
btc_df = fetch_klines("BTCUSDT", "1h")   # however you fetch them
sig = strategy.evaluate(df_15m, df_1h, df_4h, df_1d, symbol, btc_df_1h=btc_df)
```

Pass `None` (or just omit) to disable.

## Step 5 — (optional) v15 knobs

Add any of these to your `config.py` to override defaults:

```python
FVG_GRACE_BARS         = 5     # keep FVG eligible for retest 5 bars after first touch
SL_ATR_BUFFER          = 0.25  # SL = sweep_wick - 0.25 * ATR  (was 0.10)
CONFIRM_WINDOW         = 7     # sweep→BOS confirm window (was 12)
MIN_STRENGTH_TREND     = 0.55  # strength gate when regime=TRENDING
MIN_STRENGTH_RANGE     = 0.75  # strength gate when regime=RANGING
MIN_RR                 = 1.8   # was 1.5
MIN_BOS_BODY_ATR       = 0.6   # BOS bar body must exceed this × ATR
MIN_BOS_VOL_MULT       = 1.2   # BOS bar volume must exceed this × 20-bar avg
HARD_COOLDOWN_BARS     = 3
```

## What's new in v15 vs v14

1. **Soft FVG mitigation** — FVG stays eligible for retest 5 bars after first
   touch, instead of being deleted instantly. Captures more legitimate
   pullback entries.
2. **Volume-confirmed BOS** — BOS now requires body > 0.6 ATR AND volume >
   1.2× 20-bar avg. Kills fake breakouts.
3. **HTF structure check** — beyond just 4h EMA bias, also requires 4h
   higher-low (longs) or lower-high (shorts) to be making progress.
4. **Tighter sweep window** — 7 bars (~1h 45m) instead of 12.
5. **Wider SL** — 0.25 ATR buffer survives normal hunt-wicks.
6. **Higher RR floor** — TP1 needs 1.8R minimum.
7. **Tier classification** — every signal is now S / A / B:
   - **S** = Sweep→BOS + trending regime + prime session + liquidity wick + vol > 1.2× + grade A/A+
   - **A** = grade A/A+ with RR ≥ 1.8
   - **B** = everything else that still passed all gates
8. **BTC gate (optional)** — alts get blocked against BTC.
9. **Regime-adaptive strength threshold** — 0.55 trending / 0.75 ranging.

## The signal dict shape (for the dashboard API)

The dict returned by `evaluate()` already matches the dashboard's
`POST /api/signals` schema. No transformation needed. The dashboard accepts
extra fields silently, so future additions are safe.
