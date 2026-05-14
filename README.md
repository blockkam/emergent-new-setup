# MySetup v15 — Signal Performance Tracker

A FastAPI + React + MongoDB dashboard that ingests trade signals from your
Binance perp scanner, auto-resolves their outcomes against historical klines,
and surfaces win rate / expectancy / R-multiple breakdowns by tier, entry
path, symbol, session and regime. Includes an optional daily Telegram digest.

```
┌──────────────────────┐  POST /api/signals  ┌──────────────────────────┐
│  Your Mac scanner    │ ──────────────────► │   Dashboard (this repo)  │
│  strategy_v15.py     │                     │   • MongoDB persistence  │
│  tracker_client.py   │                     │   • OKX kline resolver   │
└──────────────────────┘                     │   • Daily TG digest      │
                                             │   • React UI             │
                                             └──────────────────────────┘
```

> **Note on the klines source.** The auto-resolver pulls 15m klines from
> **OKX** (`api/v5/market/history-candles`) because Binance fapi and Bybit
> are geo-blocked from the hosting region. OKX carries the same perp
> symbols; only the resolver uses it. Your scanner on your Mac continues
> to use Binance.

---

## 1. Project layout

```
/app
├── backend/
│   ├── server.py            # FastAPI app + scheduler wiring
│   ├── models.py            # Pydantic models (Signal, SignalCreate)
│   ├── resolver.py          # OKX-driven outcome resolver
│   ├── binance_client.py    # OKX kline fetcher (legacy filename)
│   ├── telegram_digest.py   # Daily digest builder + sender
│   ├── requirements.txt
│   └── .env                 # ← configure here (Telegram etc.)
├── frontend/
│   ├── src/App.js           # Single-page dashboard
│   ├── src/index.css        # Tokens + JetBrains Mono numbers
│   ├── package.json
│   └── .env                 # REACT_APP_BACKEND_URL
└── user_files/              # Copy these to YOUR scanner folder on Mac
    ├── strategy_v15.py      # Drop-in replacement for strategy.py
    ├── tracker_client.py    # post_signal() HTTP helper
    └── README.md            # Scanner-integration guide
```

---

## 2. Telegram setup (required for the daily digest)

By default Telegram is **disabled** — the dashboard works fine without it.
To enable the daily digest:

1. Open Telegram and start a chat with **@BotFather**.
2. Run `/newbot`, give it a name, copy the **token** it returns (looks like
   `1234567890:AAH...`).
3. DM your new bot once (any message). This is required so the bot can DM
   you back.
4. Open this URL in a browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find your `chat.id` in the response — it's a long integer.
5. Edit `/app/backend/.env`:
   ```env
   TELEGRAM_TOKEN="1234567890:AAH..."
   TELEGRAM_CHAT_ID="123456789"
   ```
6. Restart the backend:
   ```bash
   sudo supervisorctl restart backend
   ```
7. Verify:
   ```bash
   curl https://<your-app>/api/config/status
   # → "telegram_configured": true
   curl -X POST https://<your-app>/api/digest
   # → {"sent": true, "reason": "sent"}   ← message lands in your chat
   ```

If you don't set those env vars, `POST /api/digest` returns
`{"sent": false, "reason": "telegram_not_configured"}` and the scheduled
daily job logs `telegram disabled` and no-ops. Nothing crashes.

---

## 3. Local startup (Mac / Linux)

You don't need this if you're using the Emergent-hosted URL — but if you
ever want to self-host the dashboard alongside your scanner:

### Prerequisites
- Python 3.11+
- Node 18+ and Yarn 1.x
- MongoDB 6+ running locally on `:27017`

### Backend
```bash
cd /app/backend
pip install -r requirements.txt
# edit .env if you want Telegram (see section 2)
uvicorn server:app --host 0.0.0.0 --port 8001 --reload
```

### Frontend
```bash
cd /app/frontend
yarn install
# point at your backend
echo 'REACT_APP_BACKEND_URL=http://localhost:8001' > .env
yarn start
# opens http://localhost:3000
```

### MongoDB (one-time)
```bash
brew install mongodb-community            # macOS
brew services start mongodb-community
# OR: docker run -d -p 27017:27017 --name mongo mongo:6
```

---

## 4. Scanner integration (your Mac)

In your existing tracker folder (the one that already has `strategy.py`,
`config.py`, `indicators.py`):

```bash
# 1. swap in v15 strategy
mv strategy.py strategy_v14_backup.py
cp /app/user_files/strategy_v15.py strategy.py

# 2. drop in the client
cp /app/user_files/tracker_client.py .

# 3. add ONE env var
echo 'DASHBOARD_URL=https://<your-app>.preview.emergentagent.com' >> .env
```

Then in whatever file calls `strategy.evaluate(...)`:

```python
from tracker_client import post_signal

sig = strategy.evaluate(df_15m, df_1h, df_4h, df_1d, symbol)
if sig:
    send_telegram_alert(sig)   # whatever you do today
    post_signal(sig)           # NEW — POSTs to dashboard, fire-and-forget
```

That's it. Failures are swallowed so your scanner can never crash because the
dashboard is down. Full scanner-side guide: `/app/user_files/README.md`.

---

## 5. API reference

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/`                | Health + open/total counts |
| GET    | `/api/config/status`   | Telegram/resolver introspection |
| POST   | `/api/signals`         | Ingest one signal (scanner → dashboard) |
| GET    | `/api/signals`         | List with filters: `status, side, tier, symbol, entry_path, session, limit, offset` |
| DELETE | `/api/signals/{id}`    | Remove a signal |
| POST   | `/api/resolve`         | Trigger resolver immediately (debug) |
| POST   | `/api/digest`          | Build + send Telegram digest now |
| GET    | `/api/metrics?days=N`  | Win rate, expectancy, total R, equity curve, MFE/MAE histograms, group breakdowns |

### Signal POST schema (minimum)
```json
{
  "symbol": "BTCUSDT",
  "side": "LONG",
  "entry": 65000,
  "sl": 64200,
  "tp1": 66600, "tp2": 68200, "tp3": 69800
}
```
Optional fields the scanner already produces: `tier, grade, entry_path, regime,
strength, strength_label, score, max_score, pct, session, rr1/2/3, risk_pct,
confluence, timeframe, timestamp` — they all flow through unchanged.

---

## 6. Background jobs

| Job | Schedule | Notes |
|---|---|---|
| Resolver | every `RESOLVER_INTERVAL_MIN` (default 15 min) | Walks OKX klines since last cursor; updates MFE/MAE, fires TP/SL state machine, marks EXPIRED after `SIGNAL_EXPIRY_BARS` (default 96 bars = 24h on 15m). |
| Daily digest | cron `DIGEST_HOUR_UTC:DIGEST_MINUTE_UTC` (default 00:05 UTC) | Last-24h summary to Telegram. No-ops cleanly if Telegram not configured. |

Both are managed by APScheduler inside the FastAPI process — no separate
worker needed.

---

## 7. Result-R accounting (so you know what hit-rate means)

The resolver assumes a **1/3 scale-out at TP1, TP2, TP3** with trailing stops:

| Outcome | Result_R |
|---|---|
| Stopped before TP1                                | `−1.0R` |
| Hit TP1, then BE-stopped                          | `rr1 / 3` |
| Hit TP1+TP2, then trail-stopped                   | `(rr1 + rr2) / 3` |
| All three TPs hit                                 | `(rr1 + rr2 + rr3) / 3` |
| Expired (96 bars, no resolution)                  | sum of partials hit so far |

This is conservative — it treats wick-tag SL hits as full stops and assumes
no slippage at TPs. Tweak the formulas in `resolver.py::_partial_r` if you
prefer a different scale-out model.
