# Changelog

All notable bot / strategy changes. Format: newest first.

---

## 2026-08-24 — PR #6 Hybrid 5-min signal bars

**Branch:** `cursor/signal-5min-hybrid-c12f`

- Signal logic moved from **1-min** to **5-min IST** closed bars: regime, `TREND_CONT`, `RSI_HOOK`, futures RVOL / `VOLUME_BREAKOUT`.
- Exits unchanged on tick / monitor loop (trailing SL, time-stop, EOD).
- `breakout_max_age_sec` / `volume_ok_hold_sec` = **600** (~2 signal bars).
- Old 1-min `rsi_state.json` ignored when `signal_bar_sec` mismatches.

**Why:** Aug 24 early `TREND_CONT` fires were classic 1-min noise; holds are typically 15–45+ minutes.

---

## 2026-08-24 — PR #5 TREND_CONT + paper entry budget

**Branch:** `cursor/trend-cont-paper-budget-c12f`

- `TREND_CONT` requires **breakout-grade** expansion (RVOL ≥ `volume_mult` 1.2), not average volume (1.0).
- Paper daily entries raised to **12** (`paper_max_daily_entries`); live stays at **4**.
- Soft-cap **4/day** on `TREND_CONT` + `RSI_HOOK` so they cannot starve `VOLUME_BREAKOUT`.
- Session start left at **09:45** (no clock delay).

**Why:** Trades 78/79 entered SENSEX/NIFTY PE in morning chop on weak volume and burned the 4-slot budget before the ~10:50 move.

---

## 2026-08-21 — PR #4 Runtime correctness

**Branch:** `cursor/debug-runtime-bugs-c12f`

- Dedupe futures quote ticks (sequence / signature) so `last_traded_qty` is not stacked into the volume bar.
- `execute_exit` refuses non-positive fill; EOD retries; reconcile no longer books ₹0 closes.
- Websocket: more retries + reconnect loop; correlation IDs shortened to 10 chars.
- Rate limiter holds lock for the API call duration.
- `daily_summary` uses stored `qty` / current lot fallbacks.
- Stop tracking `.env` (credentials must stay local).

---

## 2026-08-21 — PR #3 Liquidity + TREND_CONT grind fix

**Branch:** `cursor/volume-gate-no-trades-82f0` (follow-up)

- Liquidity check no longer invents a fake **2%** spread when SmartAPI depth is missing (volume-only pass).
- Mild EMA grinds that stayed `CHOPPY` can classify `BULLISH`/`BEARISH` (EMA vs last close vs EMA21).
- `TREND_CONT` path when RSI is already in trend band (not only 50-cross hook).

---

## 2026-08-21 — PR #1 Volume gate unblock

**Branch:** `cursor/volume-gate-no-trades-82f0`

- Empty mornings: volume gate was fail-closed with no usable futures volume path.
- Futures subscribe for RVOL; CHOPPY mornings tradeable when volume / EMA confirms.
- Volume breakout path + sticky hold; hook vs breakout multipliers.
- Scorecard from `2026-08-21`; stop tracking `trade_history.db` in git.

---

## Earlier foundation (pre-PR series)

- Multi-index framework: **NIFTY** (NFO) + **SENSEX** (BFO).
- Paper trading engine, SQLite trade log, TSL monitor, EOD square-off, heartbeat.
- Wilder RSI, EMA regime, ATM options chain from scrip master.
- Angel One SmartAPI session + websocket market data.

---

## Open / draft

| PR | Status | Notes |
|----|--------|--------|
| #2 | Draft | Cloud Agent / env setup |
| #7 | (this docs PR) | README + CHANGELOG |

---

## Current feature snapshot (post #6)

| Area | Status |
|------|--------|
| Indices | NIFTY + SENSEX |
| Paper mode | Default ON |
| Signal bar | 5-min IST |
| Exit monitors | Tick / ~5s |
| Entries | VOLUME_BREAKOUT → TREND_CONT / RSI_HOOK |
| Choppy extreme RSI | OFF |
| Volume gate | Futures RVOL, sticky hold, tick dedupe |
| Liquidity | Volume + real depth spread (no fake 2%) |
| Risk | Loss/streak halt, open caps, paper 12 / live 4, trend soft-cap 4 |
| Session | 09:45–14:30 entries; 15:15 EOD |
| Scorecard | From 2026-08-21 |
