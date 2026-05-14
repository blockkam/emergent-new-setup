# MySetup v15 — Signal Performance Tracker

## Problem Statement
User has a Python scanner (Binance perp pairs) firing Telegram alerts based on MySetup v14 Pine Script logic. Wanted: (1) deep analysis & quality upgrades, (2) ability to track overall success rate, (3) dashboard alongside existing tracker, (4) daily Telegram digest, (5) patched v15 strategy with all upgrades.

## Architecture
- **Existing tracker** stays on user's Mac, scans Binance fapi (geo-OK from Mac).
- **Dashboard hosted on Emergent**: FastAPI + MongoDB + React, public URL.
- **Resolver** uses OKX klines (Binance/Bybit are geo-blocked from Emergent region).
- **Scanner integration** = 1 line: `post_signal(sig)` after `evaluate()`.

## What's Implemented (2026-01-14)
- `POST /api/signals` ingestion endpoint
- `GET /api/signals` with filters (status/side/tier/symbol/path/session)
- `GET /api/metrics?days=N` — win rate, expectancy, total R, MFE/MAE histograms, breakdowns by tier/path/symbol/session/regime/side, equity curve
- `POST /api/resolve` manual resolver trigger
- `POST /api/digest` manual Telegram digest
- APScheduler: resolver every 15min, daily digest at 00:05 UTC
- React UI: dark trading-terminal aesthetic, JetBrains Mono numbers, 6 KPI cards, equity curve (Recharts), MFE/MAE bar charts, 6 grouped breakdown tables, recent-signals table with filters
- User files in `/app/user_files/`: `strategy_v15.py`, `tracker_client.py`, `README.md`

## v15 Strategy Upgrades vs v14
1. Soft FVG mitigation (5-bar grace after first touch)
2. Volume + body filter on BOS bars (kills fakes)
3. HTF structure check (4h HL rising / LH falling) beyond MA bias
4. Sweep confirm window 12 → 7 bars
5. SL buffer 0.10 → 0.25 ATR
6. RR floor 1.5 → 1.8
7. Regime-adaptive strength threshold (0.55 trend / 0.75 range)
8. Tier classification: S / A / B
9. Optional BTC correlation gate for alts
10. Hard cooldown (no A-grade bypass)

## Verified
- 4 test signals → resolver pulled OKX klines → 50% WR, +5.67R total, expectancy +1.42R
- All API endpoints return correct shape
- Dashboard renders, no console errors

## Backlog (P1)
- Authentication if user later wants to make it private
- Per-symbol equity curves
- Drawdown / max drawdown stats
- Export to CSV
- Bybit symbol fallback if OKX rate-limits
