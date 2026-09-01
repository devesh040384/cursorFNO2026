# Changelog

All notable bot / strategy changes. Format: newest first.

---

## 2026-09-01 — NIFTY history token (backtest tooling)

- `getCandleData` returned an **empty series** for NIFTY, silently: token `26000`
  is the *websocket* form. The candle API needs the AMXIDX form,
  **`99926000` ("Nifty 50")**. SENSEX already used its AMXIDX token
  (`99919000`) for both, which is why only NIFTY came back empty.
- Added `history_token` per index plus a `history_token()` helper.
  `index_token` is **unchanged** — the live websocket depends on it.
- Wired into `backtest_data.py`, `backtest_engine.py` and `trade_analysis.py`.

**Open question:** `history_seeder.py:106` still uses `index_token`, so live
NIFTY seeding may be failing silently the same way. Confirm with
`grep "\[seed\]" trading_bot.log` before changing anything — the fix would
alter which trades NIFTY takes, so it is a Sep 15 decision, not a mid-window one.

---

## 2026-09-01 — Multi-timeframe scaffolding (inert)

- New `timeframes.py`: 1-minute bar aggregation, a pending-entry state machine
  (`continuation` / `pullback`) and index-based structural stops.
- New `RISK` knobs — `entry_timing`, `stop_mode`, `confirm_window_min`,
  `pullback_pct` — whose **defaults reproduce current behaviour exactly**.
  Nothing in the live path reads them yet.
- `backtest_data.py` gains `--interval` and `--also-1min`. One-minute data is 5x
  denser, so it chunks at 7 days per request instead of 30.

**Design note:** the 1-minute timeframe reads the **index**, never the option.
The bot subscribes to index ticks continuously but never to option ticks, so a
1-minute option series does not exist before entry — and the index is the signal
source anyway.

**Why continuation over pullback:** on all three "missed move" charts from Aug 31
and Sep 1, price peaked *at* the volume spike and faded. Pullback would have
filled into every one of those fades; continuation never gets a new extreme, so
it declines to fill. That said, 7 of 10 Sep 1 entries already went favourable
before reversing, so entry price is not the main leak — the structural stop is
the larger lever, and both need sweeping before either goes live.

---

## 2026-09-01 — Index excursion analysis

- New `trade_analysis.py` backfills, per closed trade, where the **index** went
  while the position was open: entry/high/low/exit, favourable and adverse
  excursion as a percentage, and a verdict.
- Splits losses into `EXIT_WRONG` (index moved our way, we still lost) and
  `SIGNAL_WRONG` (index never moved our way) — the distinction that decides
  whether to fix entries or exits.
- Sign convention is direction-aware: for a PE a falling index is favourable.
- Post-hoc and standalone, so it touches no trading file and is safe to run
  during the frozen config window. Works retroactively on existing trades.

---

## 2026-09-01 — Backtest harness

- `backtest_engine.py` replays the **live** `StrategyBrain` and the real
  `_trailed_stop` over cached candles. Nothing is reimplemented, so the backtest
  cannot drift from the bot.
- `backtest_options.py`: Black-Scholes ATM pricing (delta, gamma **and** theta,
  which is what the 0-DTE question needs), `implied_iv`, per-index IV
  calibration from real fills, and an Angel One round-trip cost model.
- `backtest_data.py`: chunked candle fetch with a CSV cache, so the slow pull
  happens once and sweeps read from disk.
- Clock substitution is reverted on exit and covered by a test — a leaked patch
  would corrupt the live modules in the same process.

**Calibration finding:** NIFTY and SENSEX do not trade at the same IV. Backed out
of the Aug 31 fills, NIFTY sits near 13% and SENSEX near 8%; the 14% default
over-priced SENSEX by 71%. Per-index IV is required, not optional.

**Cost finding:** ~Rs60 per round trip, and brokerage is flat, so it is ~1% of a
Rs6,000 notional — roughly half the measured edge. No result that excludes costs
is meaningful.

---

## 2026-08-29 — Telegram notifier (standalone)

- New `telegram_notifier.py`, a **separate process**. No existing file was
  modified: it tails `trading_bot.log` rather than hooking the bot's logging, and
  opens `trade_history.db` with a `mode=ro` URI so it cannot write to it.
- Pushes entries, exits and every abort condition; urgent alerts bypass `/mute`.
- Answers `/status`, `/open`, `/pnl`, `/trades`, `/log`, `/mute`, `/unmute` from
  your phone. Only `TELEGRAM_CHAT_ID` is answered.
- Follows `RotatingFileHandler` rotations (inode + truncation detection) and
  starts at EOF, so restarting it never replays the day as a burst of messages.

**Note:** this does *not* satisfy `ALERT_WEBHOOK_URL`. `broker_health.AlertHandler`
posts `{"text": ...}`; Telegram requires `chat_id`. The two are independent, and
preflight's live gate still checks the webhook separately.

---

## 2026-08-29 — Sep 15 go-live prep

- **`preflight_check.py` rewritten.** The old one could never pass: it required
  an env var `TRADING_PIN` that nothing reads, and queried a table
  `trade_history` when the table is `trades`. It now gates on credentials
  (using the names `main.py` actually reads), live-path imports, DB schema,
  stale OPEN rows from a previous day, scrip master, risk/trail/session
  consistency, capital sizing (`--capital`), and — in live mode —
  `ALERT_WEBHOOK_URL`. Exit 1 = do not start.
- **Scorecard now reports execution cost**, which it never did despite the
  columns existing: `execution: slippage INR .. | avg entry spread ..%` and
  `execution drag: N% of net PnL`. This is the go/no-go metric — the measured
  edge is ~2% of notional and a MARKET round trip can exceed that.
  `load_trades` was also not selecting `slippage` / `entry_spread_pct`, so the
  data was being written but never read.
- **Untracked runtime artifacts**: `rsi_state.json` (regenerated every session)
  and `logs/` + `*.log` (~1.5 MB). Both were dirtying `git status` on every run.

**Still tracked and worth a decision:** `scrip_master.json` +
`OpenAPIScripMaster.json` are ~68 MB of the repo. They are regenerable from the
API but `main.py` loads `scrip_master.json` at startup, so untracking them means
a fresh clone cannot run without a fetch step.

---

## 2026-08-29 — Trade history viewer

- `view_completed.py` was hardcoded to **today** and used host-local
  `datetime.now()`, so on the UTC EC2 box it showed the wrong day. It also did
  `SELECT *`, which now dumps 20+ columns unreadably.
- Rewritten with date ranges (`--days` / `--all` / `--date` / `--since`+`--until`,
  default last 7 IST days), filters (`--index`, `--reason`, `--entry-reason`,
  `--open`, `--limit`), and output modes (`--full`, `--csv`).
- Shows per-trade PnL, a win/loss summary and a per-day breakdown.
- Feed-gap logs now report an implausible bar count as "clock reset / long
  outage" instead of e.g. "5960011 bar(s) missed", which reads as corruption.

---

## 2026-08-29 — Execution hardening (pre-live)

Everything here targets the gap between paper and live. Paper fills instantly at
LTP and never rejects; none of the live order path had ever run.

- **Fills are confirmed, not assumed.** New `broker_orders.confirm_fill` polls the
  order book until terminal. Previously `execute_entry` placed an order and
  immediately logged a fill at LTP — a **rejected order became a phantom open
  position** that the TSL monitor would then try to sell. Nothing read the order
  book anywhere in the codebase.
  - rejected/cancelled → no position recorded
  - pending at timeout → UNKNOWN, not logged, `CRITICAL` (it may still fill)
  - partial entry → records filled qty; partial exit → leaves the trade OPEN
  - trades log the **broker's** average price, and target/SL are re-derived from
    the real fill instead of the intended price
- **`max_option_spread_pct` 3.0 → 1.5.** Measured edge is 2.09% of notional; a
  MARKET round trip at 3% spread costs ~2.9× that. This was the single largest
  threat to going live.
- **Execution quality recorded**: `intended_price`, `slippage`, `entry_bid`,
  `entry_ask`, `entry_spread_pct` on every trade.
- **`broker_health.SessionKeeper`** re-authenticates on auth errors only (not
  rate limits), probed each heartbeat. `generateSession` previously ran once at
  startup, so an expired token silently stopped all position management.
- **Reconciliation reports untracked broker positions** (at broker, absent from
  DB — a crash between `placeOrder` and the DB write). These carry no SL, target
  or EOD square-off. The old exchange filter also ignored every SENSEX/BFO row.
- **Rotating logs** (10MB × 5; was an unbounded `FileHandler`) and optional
  `CRITICAL` webhook alerting via `ALERT_WEBHOOK_URL`.

**Why now:** go-live is Sep 15 with ~10 sessions left. The paper edge is not yet
statistically significant (t = 1.32, needs ~90 trades), so these changes make
failure *visible and bounded* rather than proving the strategy works.

---

## 2026-08-25 — Target +30%, tiered trail ladder

- `trending_target_mult` **1.22 -> 1.30** (both indices). With the -10% stop that
  is **3:1** R:R.
- Trailing SL is now a config-driven ladder (`RISK["trail_tiers"]`), replacing the
  hardcoded if/elif chain:

  | Trigger | Lock |
  |---------|------|
  | +4% | breakeven *(unchanged)* |
  | +8% | entry x 1.02 *(unchanged)* |
  | +15% | 50% of peak gain *(unchanged)* |
  | +22% | **65% of peak gain** *(new)* |
  | +26% | **75% of peak gain** *(new)* |

- Behaviour below +15% is **byte-identical** to before. A trade peaking at +30%
  now locks **+22.5%** instead of +15% — ~+35% more kept across give-back
  scenarios.

**Why:** on last week's 39 trades, the +4%/+8% tiers turned 9 would-be -10%
losers into +2.8% average wins. Loosening them needed **40%** of those trades to
run to +22% just to break even, with a worst case of **-Rs5,453** — enough to
flip the week negative on its own. Raising the target is the far safer bet: a
miss still exits as a *win* on the trail. Break-even continuation is **53%** at
+30% but **77%** at +25%, so +25% is strictly worse than +30% — it gains too
little per hit to pay for the misses. The upper tiers only touch trades already
deep in profit, so they cannot convert a winner into a loser.

**Not changed:** `time_stop_minutes` stays 25. A +30% target needs more time than
+22%, so this is the most likely follow-up knob once DTE/runner data lands.

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
| — | Unmerged | `feat/seed-5min-history` — seeding, IST fixes, per-index cap, DTE instrumentation, +30% target + trail ladder |

---

## Current feature snapshot

| Area | Status |
|------|--------|
| Indices | NIFTY + SENSEX |
| Paper mode | Default ON |
| Signal bar | 5-min IST |
| Warmup | **Seeded from broker 5-min candles at startup** (entries live from 09:45) |
| Exit monitors | Tick / ~5s |
| Entries | VOLUME_BREAKOUT → TREND_CONT / RSI_HOOK |
| Choppy extreme RSI | OFF |
| Volume gate | Futures RVOL, sticky hold, tick dedupe, partial/gap bars discarded |
| Liquidity | Volume + real depth spread, cap **1.5%** (no fake 2%) |
| Contract | ATM, `min_dte = 0` (0-DTE allowed); expiry/DTE recorded per trade |
| Target / SL | **+30% / −10%** (3:1) |
| Trail | 5-tier ladder: +4% BE, +8% ×1.02, +15% 50% peak, +22% 65%, +26% 75% |
| Risk | Loss/streak halt, open caps, paper 12 / live 4 daily, **per-index 6/3**, trend soft-cap 4 |
| Session | 09:45–14:30 entries; 15:15 EOD |
| Timezone | All wall-clock via `ist_time.py` (host-local never used) |
| Scorecard | From 2026-08-21; adds `by DTE` + runner capture |
| Execution | Fills confirmed via order book; slippage + spread recorded |
| Session | Auto re-auth on token expiry; rotating logs; optional webhook alerts |
| Tests | 48 in `test_suite.py` |

---

## Measured baseline (7d to 2026-08-24, paper)

39 trades · 41% win rate · PnL ₹3,852 · profit factor 1.83 · expectancy ₹99/trade
· max DD ₹1,541.

Decomposed (the `STOP_LOSS_HIT` bucket splits, because a trailed stop fires above entry):

| Bucket | n | PnL | avg |
|--------|---|-----|-----|
| TARGET_HIT | 7 | +7,271 | **+1,039** |
| Trailed winners | 9 | +1,204 | **+134** |
| Real stop-outs | 20 | −4,314 | −216 |
| Breakeven | 1 | 0 | 0 |
| TIME_STOP | 2 | −309 | −154 |

Per index: NIFTY n=18 ₹605 (avg **₹33.6**) · SENSEX n=21 ₹3,248 (avg **₹154.7**).
Per reason: VOLUME_BREAKOUT n=32 ₹2,518 · TREND_CONT n=7 ₹1,335 · **RSI_HOOK n=0**.

Three things this baseline says:

1. **TARGET_HIT is 18% of trades but 189% of net PnL.** Everything else nets
   −₹3,418. Seven trades carry the week.
2. **Trailed winners average ₹134 vs ₹1,039 for target hits** — an 8× gap. Every
   one had run ≥ +4%. This drove the upper trail tiers.
3. **max DD ₹1,541 ≈ the ₹1,500 daily loss limit.** The circuit breaker is sitting
   at the observed worst drawdown and will start tripping.

---

## Open questions (not yet answerable from data)

| Question | Blocked on | Status |
|----------|-----------|--------|
| Does 0-DTE cause the NIFTY underperformance? | `dte` per trade | instrumented, **0 rows** |
| Are runners being captured? | `max_favorable_price` | instrumented, **0 rows** |
| Do +22% winners continue to +30%? | same | **assumed 53% break-even, unmeasured** |
| Is `time_stop_minutes = 25` too short for a +30% target? | above | unchanged, flagged |
| Why has `RSI_HOOK` never fired? | — | cross condition is narrow on 5-min bars |

The +30% target and the trail ladder are **reasoned bets, not backtested results.**
Worst case if no +22% winner continues is −₹2,974 against a ₹3,852 week — a flat
week, not a blow-up, and no trade converts from win to loss. Treat the first week
after merge as the experiment that validates or kills them.
