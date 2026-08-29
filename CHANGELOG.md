# Changelog

All notable bot / strategy changes. Format: newest first.

---

## 2026-08-25 — DTE control + runner-capture instrumentation

- **`min_dte` knob** (default `0` = unchanged). `get_nearest_expiry_contract`
  used `parsed_date >= today`, so on expiry day the bot bought **0-DTE** ATM
  options where theta alone can hit the -10% stop with no adverse spot move.
  Set `1` to skip expiry day, `2` to force the next weekly.
- Expiry-day comparison now uses **IST**, not host-local `datetime.now()`.
- Trades record **`expiry`, `dte`** and **`max_favorable_price`** (peak premium
  while open, mirrored from the existing TSL peak tracking).
- Scorecard adds **`by DTE`** and **runner capture** — realised gain as a share of
  the gain that was on the table — plus a count of trades >= INR 1500.

**Why:** neither "does 0-DTE cause the early stop-outs" nor "are runners being
captured" was answerable from the stored data. Now both are.

---

## 2026-08-25 — IST correctness, per-index cap, feed-gap safety

- **New `ist_time.py`** — every date/stamp now IST. `database.py`, `scorecard.py`,
  `daily_summary.py`, `risk_monitors.py` and the heartbeat used host-local
  `datetime.now()`; on a non-IST host that made `count_entries_today` /
  `fetch_closed_today` query the wrong day, so the daily cap and the loss
  circuit breaker both saw zero trades and never fired.
- **Per-index daily entry cap** (`max_daily_entries_per_index` 3 live / 6 paper).
  `max_open_per_index` was only a *concurrent* cap — one index could churn the
  whole daily budget alone.
- Heartbeat now reports **entries / cap, open, closed and realised PnL per index**.
- **Feed-gap safety**: a reconnect spanning several bars used to close one partial
  bar. Volume side now drops it and clears sticky expansion (a partial bar dragged
  the SMA down and faked a breakout); price side pauses entries until clean bars
  are rebuilt (`stale_bars`).
- **LTP decode fixed**: `ltp/100 if ltp > 1000000` fed a 100x price to the strategy
  for any index under 10,000. Websocket LTP is always paise — always divide, and
  drop ticks outside a per-index `spot_min`/`spot_max` band.
- `time_stop` age compared IST-stamped `entry_time` against local `now()`.
- Removed dead `startup_sync.py` (duplicate `TradeReconciler`, filtered `NFO` only
  so it missed every SENSEX/BFO position) and unused `historical_features.py`.
- Dropped the deprecated `datetime.utcnow()` calls.

---

## 2026-08-25 — Startup history seeding (09:45 ready)

- New `history_seeder.py`: pulls closed **FIVE_MINUTE** candles from `getCandleData`
  at startup and fills `price_histories` (index) + `closed_volumes` (futures RVOL).
- Removes the ~110-min live warmup (22 x 5-min bars); the bot is signal-ready at
  the **09:45** session start instead of ~11:35.
- Seeder drops the in-progress candle, seeds `last_closed_rsi` + the 5-bar
  `closed_rsi` ring (RSI_HOOK confirmation), and clears any breakout event so
  historical bars cannot fire a stale entry.
- Volume gate discards its **partial first live bar** (mid-bucket join) — it used
  to dilute the RVOL SMA and fake an expansion on the next bar.
- `rsi_state.json` date now stamped in **IST**, not host-local time; `closed_rsi`
  is persisted/restored across restarts.

**Why:** 09:45–11:35 was dead time every session; the two best NIFTY/SENSEX
expansion windows sit inside it.

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
| Risk | Loss/streak halt, open caps, paper 12 / live 4 daily, per-index 6/3, trend soft-cap 4 |
| Session | 09:45–14:30 entries; 15:15 EOD |
| Warmup | Seeded from broker 5-min candles at startup |
| Scorecard | From 2026-08-21 |
