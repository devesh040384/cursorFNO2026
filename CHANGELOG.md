# Changelog

All notable bot / strategy changes. Format: newest first.

---

## 2026-09-03 — Concurrency audit + instrument cost model

**Audit finding: double-SELL exposure (live only).** The trailing-stop loop (5s)
and the EOD loop (10s) both scan for `OPEN` rows, so at 15:15 they can pick up
the same trade. `execute_exit` sends the broker SELL **before** `close_trade` is
consulted, and `close_trade`'s `AND status = 'OPEN'` guard protects the database,
not the broker — two callers would send two SELLs and leave a **net short
position**. Paper mode hid it, because there `close_trade` is the only effect.

Fixed by making the database the arbiter *before* anything irreversible:
`claim_for_exit` atomically moves `OPEN → EXITING`, and only the winner proceeds.
Every failure path calls `release_claim` so the trade retries. A claimed row
still counts as open for risk caps, or a claim would free a slot for a new entry.

That is the fourth bug from one root cause: shared state touched by more than one
thread without an atomic guard.

**Instrument cost model, and a correction.** I suggested futures would have a
"~15x lower hurdle" than options. That was wrong: it compared round-trip cost as
a percentage of notional without dividing by leverage. Options are ~116x levered,
which very nearly cancels their much larger percentage cost.

Break-even **index** move by hold time:

| hold | option | future | wins |
|------|--------|--------|------|
| 5 min | 0.0154% | 0.0350% | option |
| 30 min | 0.0301% | 0.0350% | option |
| 45 min | 0.0389% | 0.0350% | **future** |
| 120 min | 0.0831% | 0.0350% | **future** |

The instrument is not the problem; **theta is, and only past ~20 minutes.** Every
screened signal falls short on both: `first_hour` inverted is 2.4x short,
`ema_trend` 3.9x, `volume_breakout` 4.9x.

`signal_lab --instrument future --hold N` reports the hurdle beside each signal
and flags any that clears it.

---

## 2026-09-03 — Atomic state file (same root cause as the entry race)

`❌ Error loading StrategyBrain state: Extra data: line 1 column 2034` — a JSON
splice, not a truncation. `_save_state` runs on the websocket callback thread and
used a bare `open("w")` with no lock, so two writers both truncated and the
shorter write landed inside the longer one.

Reproduced in 60 attempts, then fixed two ways:

- `STATE_LOCK` serialises writers.
- The write goes to `rsi_state.json.tmp` and is `os.replace`d into position —
  atomic on POSIX and Windows — so a reader or a crash never sees half a file.

A corrupt file is now moved to `.corrupt` on load rather than left to fail on
every restart, and seeding rebuilds the history anyway.

**This is the third instance of the same root cause in two days:** duplicate
entries (unlocked check-then-act on the DB), and now spliced state writes.
Anything touched from the websocket callback thread needs the same scrutiny.

**Live impact was small.** State failed to load, so the bot started cold — and
seeding covers exactly that. It would have mattered more before the seeding fix.

---

## 2026-09-03 — Duplicate-entry race + seeding rate limits (LIVE)

**Duplicate positions (serious).** Trades 136 and 137 on 2026-09-02 are the same
contract, same second (13:35:03), same fill price — two SENSEX positions against
`max_open_per_index = 1`, i.e. double the intended risk.

Entries run on the websocket callback thread. `assess_order_safety` reads the DB,
`log_trade` writes it, and nothing held in between, so two ticks arriving together
both passed the cap check before either wrote a row. Reproduced: six concurrent
callers opened **six** positions.

Two guards, both needed:

- `ENTRY_LOCK` held from the risk check through the DB write, closing the
  check-then-act window.
- One entry per index per signal bar (`last_entry_bucket`), which also survives a
  retry path and does not depend on lock scope.

Six concurrent callers now open exactly one position.

**Seeding rate limits.** `[seed] candle fetch error BFO:844615: Access denied
because of exceeding access rate`. The historical endpoint is limited far below
the order API and replies in plain text, so the client raises a parse error rather
than returning a status — the old generic handler retried after 1.5s and kept
hitting it.

- Rate-limit responses are now detected by message text and backed off
  exponentially (4s → 8s → 16s, capped at 30) instead of retried immediately.
- Calls are paced 1.5s apart; the account limiter is per-process and knows
  nothing about this endpoint's tighter budget.
- **The window is now computed, not guessed.** `required_price_bars()` (22 for
  the regime gate + margin = 30) and `required_volume_bars()` (from
  `volume_sma_bars`) drive `lookback_days()`, cutting the request from a fixed 6
  days to 5 — and it now shrinks automatically if the indicators do.

---

## 2026-09-02 — Signal screening lab

`VOLUME_BREAKOUT` failed, but a full backtest could not say *which part* failed:
signal, option model, costs, stops and caps all move together in it.

`signal_lab.py` isolates the prior question — does a signal predict index
movement at all? — using only index and futures candles. No option model, no
costs, no execution. It measures the **signed** forward index return in the
direction the signal called, which is zero under no edge, and t-tests it against
zero with the unconditional distribution printed for comparison.

**Validated against known answers before use:**

| Data | Signal | t | Correct? |
|------|--------|---|----------|
| pure random walk | volume_breakout | 0.26 | null, as required |
| pure random walk | mean_reversion | −1.31 | null, as required |
| pure random walk | ema_trend | **2.03** | **false positive at the raw bar** |
| momentum injected | volume_breakout | 5.58 | detects it |
| momentum injected | mean_reversion | −7.07 | correctly negative |

That false positive on data with zero edge by construction is why the report
prints a Bonferroni-corrected threshold (|t| > 3.20 across 36 tests) and flags
only signals clearing it.

Six hypotheses ship, including the current live signal as a control.

---

## 2026-09-02 — Random-walk baseline (the signal does not predict)

The forward-excursion table looked encouraging — reach rose 10% → 16.7% → 26.7%
→ 30% across held/15/30/45 min. It is not encouraging. **A running maximum grows
as √t for any series**, so a rising column proves nothing on its own.

Calibrating a driftless random walk to the 15-min point (σ 0.0302%/min,
~9.3% annualised):

| window | reach observed | reach random |
|--------|----------------|--------------|
| 15 min | 16.7% | 20.0% |
| 30 min | 26.7% | 36.5% |
| 45 min | 30.0% | 46.0% |

**Observed sits below diffusion at every window, and the gap widens.** Median
excursion grows 1.51× from 15 to 45 min where a coin flip gives 1.73×. The
entries are not better timed than picking a moment at random.

The report now prints this baseline alongside the observations so the table
cannot be misread the same way again.

Also fixed a reporting bug: the verdict line asserted *"Those are exit problems,
not signal problems"* unconditionally — including when zero losing trades
qualified, which stated the exact opposite of the finding.

**Three independent lines now agree the signal has no edge:** the 1,146-trade
backtest (t = −6.5), live index excursion (median 0.08% move after a signal),
and this baseline comparison. The first depends on a synthetic option model; the
other two do not.

---

## 2026-09-02 — Forward index excursion

The first excursion report read *"of 16 losing trades, 0 had the index move our
way first"* — apparently proving exits were fine and the signal was dead. That
conclusion was not safe.

MFE was measured over the **holding window only**, and Sep 1's median hold was
4.1 minutes (one trade lasted 1.6). Such a window cannot show a 0.15% index move
even if one arrived at minute 10, so `SIGNAL_WRONG` was conflating *"the signal
did not predict a move"* with *"we exited before the move appeared"*.

Adds `fwd_mfe_15m / 30m / 45m`: best favourable index move within a fixed window
of entry, **regardless of when we actually exited**. The report contrasts them
with the held-window figure.

- longer windows reach 0.15% far more often → the signal works, the stop is too tight
- they do not → the signal genuinely does not predict movement, matching the backtest

Verified on a constructed case: a trade stopped out at minute 4, into a move
starting at minute 10, shows held-MFE 0.09% but forward-30m 0.51%. The old tool
called that `SIGNAL_WRONG`.

---

## 2026-09-02 — Backtest: intra-bar exits + IV override

First 365-session run showed all three configs losing (~−₹112k, PF 0.75, win
rate 29–30%) and the three landing within 7% of each other. Two engine issues
surfaced.

**Intra-bar exit resolution (bug).** `manage_exits` ran once per 5-minute bar
while the live monitor polls every 5 seconds, so a target or stop touched inside
a bar was invisible. Now repriced at the bar's extremes — a call is worth most
at the bar high, a put at the bar low. Where both extremes would trigger in one
bar the ordering is unknowable, so the adverse one is assumed first.

Note this made results **worse**, not better: with a −10% stop against a +30%
target, intra-bar noise reaches the stop far more often than the target. The
correction is still right — it matches live polling — but it did not explain the
gap between the backtest and live paper results.

**IV is estimated, and results are very sensitive to it.** The engine used
*realised* vol; implied normally trades above realised, and under-stating IV
makes options cheap, so the same index move becomes a larger percentage swing
and trips the −10% stop on noise. A 0.30% adverse NIFTY move costs −47% of
premium at 8% IV but −26% at 16%. Added `--iv NIFTY=13.1 SENSEX=8.2` so the
values calibrated from real fills can be used instead of an estimate.

**Correction to earlier analysis:** I read the live-vs-backtest win rate gap
(50% vs 29.5%) as evidence the backtest was wrong. With n=16, P(≥ 8 wins) under
a true 29.5% rate is 6.8% — not rare. Sixteen trades cannot distinguish 30% from
50%, and the live sample may simply have started lucky.

---

## 2026-09-02 — Fix silent NIFTY seeding failure (LIVE)

**This is a behavioural fix, not tooling.** `history_seeder.py` asked for candles
with `index_token`, which for NIFTY is the *websocket* token `26000`. That
returns `status=True` with an **empty list** — no error, no rejection — so
seeding failed silently and NIFTY fell back to cold live warmup every session.

Evidence, all three agreeing:

| | |
|---|---|
| `getCandleData` with `26000` | 0 rows (twice) |
| `getCandleData` with `99926000` | 23,336 rows |
| Cold warmup from pre-open ticks | 22nd bar at **10:50** |
| NIFTY first trade, Sep 1 | **10:50:02** |
| SENSEX first trade, Aug 31 | 09:45:03 — only possible if seeded |

SENSEX's `index_token` already *is* its AMXIDX token, which is why only NIFTY
was affected and why the failure went unnoticed.

- Seeder now uses `history_token()`.
- The failure is logged at **ERROR** and states the consequence
  (*"NO ENTRIES until ~10:50 IST"*) rather than a quiet warning. The old warning
  was present in the log for two sessions and nobody read it.
- Fixed a latent backtest bug where futures discovery skipped only the websocket
  token, so an index cached under its history token could be read as futures.

**Expect NIFTY behaviour to change from the next session:** entries become
possible from 09:45 instead of ~10:50, so NIFTY will take more trades per day
and may reach its 6/day per-index cap earlier. The NIFTY forward sample restarts
here; SENSEX is unaffected.

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
| **Strategy** | **Does not work — see the top of the README. Paper only.** |
| Indices | NIFTY + SENSEX |
| Signal bar | 5-min IST; 1-min scaffolding present but inert |
| Warmup | Seeded from broker candles (computed window, rate-limit aware) |
| Entries | VOLUME_BREAKOUT → TREND_CONT / RSI_HOOK |
| Target / SL | +30% / −10% (3:1) |
| Trail | 5 tiers: +4% BE, +8% ×1.02, +15% 50% peak, +22% 65%, +26% 75% |
| Risk | Loss/streak halt, per-index 6/3 daily, trend soft-cap 4 |
| Concurrency | `ENTRY_LOCK` + per-bar dedupe; `STATE_LOCK` + atomic state write |
| Execution | Fills confirmed via order book; slippage + spread recorded |
| Session | 09:45–14:30 entries; 15:15 EOD |
| Timezone | All wall-clock via `ist_time.py` |
| Research | `backtest_engine`, `signal_lab` (+`--validate`), `trade_analysis` |
| Tests | 142 in `test_suite.py` |

---

## Verdict (2026-09-03)

Four independent methods agree `VOLUME_BREAKOUT` has no edge. Two of them use no
option model, so the conclusion does not rest on the synthetic pricing:

| Method | Result |
|--------|--------|
| Backtest, 1,146 trades | expectancy −₹88/trade, **t = −6.5** |
| Live index excursion, 30 trades | median move after a signal **0.08%** |
| Forward excursion vs random walk | **below diffusion** at every window |
| Signal screen, 248 sessions | below the corrected bar; no cross-index replication |

It loses **before costs** (−₹30.7/trade gross). The one candidate that looked
real, `first_hour` inverted, **failed out-of-sample**.

Go-live is off. The infrastructure stands; the signal does not.

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
