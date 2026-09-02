# AI FNO Trading Bot (NIFTY / SENSEX)

Paper-first Angel One SmartAPI bot for **index options**. It classifies index regime, gates entries with futures volume, buys ATM CE/PE, and manages exits with trailing stop, time-stop, and EOD square-off.

> ## Status: the strategy does not work. Do not trade it live.
>
> `VOLUME_BREAKOUT` — around 90% of all entries — does not predict index
> movement. Four independent methods agree, and the two that matter most use no
> option model at all:
>
> | Evidence | Result |
> |----------|--------|
> | Backtest, 1,146 trades over 248 sessions | expectancy **−₹88/trade**, t = −6.5 |
> | Live index excursion, 30 trades | median move after a signal: **0.08%** |
> | Forward excursion vs a random walk | **below diffusion** at every window |
> | Signal screen, 248 sessions | fails the multiple-testing bar; does not replicate across indices |
>
> It also loses **before costs** (−₹30.7/trade gross), and costs are ~₹60/round
> trip on a ~₹6,000 notional — roughly half the edge the strategy would need.
>
> The one candidate that looked real (`first_hour`, inverted) **failed
> out-of-sample**: the entire effect sits in one half of the year and is absent
> from the last 20 sessions.
>
> **What is worth keeping is the infrastructure** — fill confirmation, circuit
> breakers, IST correctness, seeding, and a research harness that can kill a bad
> signal in an afternoon rather than a quarter. Run the bot in paper as a free
> control while researching a different edge.

`PAPER_TRADING = True` in `config.py`. Live mode is gated behind
`preflight_check.py --live` and should stay unused.

---

## Quick start

```bash
# 1. Credentials (local only — never commit)
cat > .env <<'EOF'
SMART_API_KEY=...
CLIENT_ID=...
PASSWORD=...
TOTP_SECRET=...
FEED_TOKEN=
EOF
chmod 600 .env

# 2. Run
python3 main.py

# 3. Scorecard
python3 scorecard.py
python3 daily_summary.py
python3 view_completed.py --days 30      # trade history, any range
python3 preflight_check.py --capital 75000   # readiness gate (exit 1 = blocked)
python3 -m unittest test_suite.py -v
```

`FEED_TOKEN` may be blank; after login the bot calls `getfeedToken()` for the websocket.

---

## Architecture (what runs)

| Piece | Role |
|-------|------|
| `main.py` | Auth, websocket (index + futures), heartbeat, EOD kill switch |
| `strategy_brain.py` | 5-min signal bars: regime, entries, futures volume gate |
| `options_chain_builder.py` | ATM nearest-expiry contract + liquidity check |
| `order_execution.py` | Paper/live entry & exit; refuses ₹0 fills |
| `risk_manager.py` | Daily loss / streak / open / entry caps |
| `risk_monitors.py` | Trailing SL, target, time-stop (tick / ~5s loop) |
| `database.py` | SQLite `trade_history.db` (WAL) |
| `scorecard.py` | PnL / win-rate from stored qty |
| `history_seeder.py` | Seeds 5-min bar history from broker candles at startup |
| `timeframes.py` | 1-min entry confirmation + structural stops (**inert by default**) |
| `ist_time.py` | Single source of IST wall-clock (all dates/stamps) |
| `broker_orders.py` | Confirms real fills from the order book (never assumes) |
| `broker_health.py` | Session re-auth, log rotation, CRITICAL alerting |
| `backtest_engine.py` | **Separate.** Replays the live strategy over cached history |
| `backtest_options.py` | Black-Scholes ATM pricing + Angel One cost model |
| `backtest_data.py` | Candle fetch + CSV cache |
| `signal_lab.py` | **Screens signal hypotheses on index data — no options, no costs**; `--validate` for out-of-sample |
| `trade_analysis.py` | **Separate.** Index excursion per trade: signal failure vs exit failure |
| `telegram_notifier.py` | **Separate process.** Telegram alerts + remote status. Touches no trading file. |
| `config.py` | All live knobs |

**Startup seeding**

Building 22 x 5-min bars from live ticks takes ~110 minutes, so entries could not
start until ~11:35. On boot the bot now fetches closed `FIVE_MINUTE` candles
(`getCandleData`) for each index and its nearest future, filling regime/RSI history
and the futures RVOL window. The in-progress candle is dropped and no breakout
event is carried over. Entries are then gated only by `session_start_hhmm`
(**09:45**), which skips the opening-auction noise.

Candles are fetched with each index's `history_token`, **not** its websocket
token: `getCandleData` returns an empty series for the websocket form and does
so silently (`status=True`, empty list). That cost NIFTY the 09:45–10:50 window
every session until 2026-09-02.

If the candle API fails or returns too few bars, the bot logs an **ERROR**
naming the consequence and falls back to live warmup — it does not trade on
partial history. Check the `[seed]` lines every morning.

**Rate limits.** The historical endpoint is limited far below the order API and
replies in plain text, so the client raises a *parse error* rather than returning
a status — `Access denied because of exceeding access rate`. Seeding detects that
by message text and backs off exponentially (4s → 8s → 16s, capped at 30) rather
than retrying immediately, and paces its calls 1.5s apart. `RateLimitedAPI` is
per-process and knows nothing about this endpoint's tighter budget.

**The request window is computed, not fixed.** `required_price_bars()` (22 for
the regime gate plus margin) and `required_volume_bars()` (from
`volume_sma_bars`) drive `lookback_days()`. Asking for six days to fill thirty
5-minute bars is ~5× more payload than the job needs, and payload size is what
trips the limit.

**Hybrid timeframe**

- **5-min IST closed bars** → regime, `VOLUME_BREAKOUT`, `TREND_CONT`, `RSI_HOOK`, futures RVOL  
- **Tick / monitor loop** → LTP, trailing SL, time-stop, EOD  

**Feed integrity**

The strategy only trades on bars it believes are clean:

| Situation | Behaviour |
|-----------|-----------|
| Joined mid-bucket, no seed | First futures volume bar is **discarded** (a partial bar would drag the RVOL average down and fake a breakout on the next bar) |
| Feed gap over N bars (reconnect) | Volume: partial bar dropped, sticky expansion cleared. Price: entries **paused for N clean bars** (`stale_bars`, capped at 22) |
| Decoded spot outside `spot_min`/`spot_max` | Tick **dropped and logged**, never reaches the strategy |
| Duplicate futures tick | Deduped on `sequence_number`, else on `(volume_today, last_qty)` |

All wall-clock comes from `ist_time.py` — session windows, DB stamps, scorecard
buckets and expiry comparisons. Host-local time is never used, so a UTC VPS
behaves identically to an IST desktop.

---

## Strategies (entry logic)

Priority on each **closed 5-min** bar: **VOLUME_BREAKOUT** first, then trend paths.

### Regime

- EMA9 vs EMA21 with index `ema_spread_min`
- Last close vs EMA21 → `BULLISH` / `BEARISH` / `CHOPPY`
- Lookback mean uses last 20 closed bars (not full session)

### VOLUME_BREAKOUT

- Futures 5-min RVOL ≥ `volume_mult` (**1.2**) creates a breakout event (held ~600s)
- Up-bar → buy **CE** (BULLISH, or CHOPPY if allowed)
- Down-bar → buy **PE** (BEARISH, or CHOPPY if allowed)
- Event kept if fill fails (retry); consumed on success or direction mismatch

### TREND_CONT

- BULLISH + up-bar + RSI in [50, 68] → **CE**
- BEARISH + down-bar + RSI in [32, 50] → **PE**
- Requires **expansion** volume (RVOL ≥ 1.2), not just average

### RSI_HOOK

- Cross of 50 with recent dip (&lt;45) / spike (&gt;55) confirmation
- Softer volume: RVOL ≥ `volume_hook_mult` (**1.0**) or sticky post-breakout hold

### CHOPPY extreme RSI

- Config present but **disabled** (`enable_choppy_entries = False`)

### Contract selection

- Nearest ATM CE/PE with **DTE ≥ `min_dte`**
- `min_dte = 0` (default) allows **expiry-day / 0-DTE** contracts. At 0 DTE theta
  alone can walk a position into the −10% stop with no adverse spot move; set
  `1` to skip expiry day or `2` to force the next weekly. Trade-off: higher DTE
  means lower gamma, so **+30% is slower to reach**.
- Liquidity: min option volume 500; spread ≤ **1.5%** when depth exists (no invented 2% spread)
- Targets / SL (trending): **+30% / −10%** (3:1)
- `expiry`, `dte` and `max_favorable_price` are stored on every trade so the DTE
  question can be answered from data instead of assumption

After a fill: **15-min** per-index cooldown.

---

## Risk & session

| Knob | Paper | Live (default) |
|------|-------|----------------|
| Daily entries (total) | **12** | **4** |
| Daily entries **per index** | **6** | **3** |
| Trend soft-cap (`TREND_CONT` + `RSI_HOOK`) | **4**/day | **4**/day |
| Max open / index | 1 | 1 |
| Max open total | 2 | 2 |
| Max daily loss | ₹1500 | ₹1500 |
| Consecutive losses halt | 3 | 3 |
| Session entries | 09:45–14:30 IST | same |
| EOD square-off | 15:15 IST | same |
| Time-stop | 25 min if &lt; +2% | same |
| Min premium | ₹25 | ₹25 |
| Max notional | ₹8000 | ₹8000 |

`VOLUME_BREAKOUT` is not limited by the trend soft-cap (only by the daily entry budget).

### Circuit breaker (hard halt)

`RiskManager.refresh_from_db()` runs before **every** entry and recomputes the day
from closed trades. It latches `trading_halted = True` — no further entries for the
rest of the process — when either fires:

| Trip | Threshold |
|------|-----------|
| Realised daily loss | ≤ **−₹1500** (`max_daily_loss_inr`) |
| Consecutive losing trades | **3** in a row (`max_consecutive_losses`) |

It logs `CIRCUIT BREAKER: PnL ... | streak ...` at CRITICAL. It halts **entries only** —
open positions keep their trailing SL, time-stop and 15:15 square-off. It is *realised*
PnL: an open losing position does not trip it. Restarting the process clears the latch,
but `refresh_from_db()` re-trips it immediately from the same day's closed rows.

Separate, non-latching gates that also block entries: total daily cap, per-index daily
cap, trend soft-cap, max open per index / total, premium floor, notional ceiling.

### Exits

1. **TARGET_HIT** (+30%) / **STOP_LOSS_HIT**
2. **Trailing ladder** — `RISK["trail_tiers"]`, evaluated cheapest-first, best
   qualifying tier wins, and a tier can only ever **raise** the stop:

   | Trade reaches | Stop moves to | Effect |
   |---------------|---------------|--------|
   | +4% | entry | breakeven |
   | +8% | entry × 1.02 | +2% locked |
   | +15% | entry + 50% of peak gain | |
   | +22% | entry + 65% of peak gain | |
   | +26% | entry + 75% of peak gain | a +30% peak exits at **+22.5%** |

   The two lowest tiers are deliberately tight: they convert would-be −10%
   losers into small wins. The upper tiers only touch trades already deep in
   profit, so they can never turn a winner into a loser.
3. **TIME_STOP** (25 min, gain &lt; 2%)  
4. **EOD_SQUAREOFF** (15:15) — refuses exit if LTP missing / ₹0  

> **Reading the scorecard:** `STOP_LOSS_HIT` mixes two very different outcomes,
> because a trailed stop fires *above* entry. Split it by sign before drawing
> conclusions — trailed winners and real stop-outs behave nothing alike.

---

## Key config (`config.py`)

```text
# --- signal bars -------------------------------------------------------
signal_bar_sec                    = 300     # 5-min signals
breakout_max_age_sec              = 600     # ~2 signal bars

# --- futures volume gate ----------------------------------------------
require_volume_expansion          = True
volume_mult                       = 1.2     # VOLUME_BREAKOUT / TREND_CONT
volume_hook_mult                  = 1.0     # RSI_HOOK
volume_sma_bars                   = 8
volume_ok_hold_sec                = 600     # sticky post-breakout hold
trend_cont_requires_expansion     = True
trend_cont_rsi_max                = 68.0

# --- entry budget ------------------------------------------------------
max_daily_entries                 = 4       # live, all indices
paper_max_daily_entries           = 12
max_daily_entries_per_index       = 3       # live, per index
paper_max_daily_entries_per_index = 6
max_trend_entries_per_day         = 4       # TREND_CONT + RSI_HOOK soft cap
max_open_per_index                = 1
max_open_total                    = 2

# --- circuit breaker (latching) ---------------------------------------
max_daily_loss_inr                = 1500.0
max_consecutive_losses            = 3

# --- session (IST) -----------------------------------------------------
session_start_hhmm                = 945
entry_cutoff_hhmm                 = 1430
eod_squareoff_hhmm                = 1515

# --- contract ----------------------------------------------------------
min_dte                           = 0       # 1 skips expiry day, 2 = next weekly
min_option_premium                = 25.0
max_premium_risk_inr              = 8000.0
min_option_volume                 = 500.0
max_option_spread_pct             = 1.5     # was 3.0; see Execution below

# --- exits -------------------------------------------------------------
trending_target_mult              = 1.30    # per index, +30%
trending_sl_mult                  = 0.90    # per index, -10%
time_stop_minutes                 = 25
time_stop_min_gain_mult           = 1.02
trail_tiers                       = +4% breakeven | +8% x1.02
                                    | +15% 50% peak | +22% 65% | +26% 75%

# --- misc --------------------------------------------------------------
PAPER_TRADING                     = True
ACTIVE_INDICES                    = NIFTY, SENSEX
SCORECARD_SINCE                   = 2026-08-21
enable_choppy_entries             = False
```

---

## Concurrency

Ticks arrive on the SmartWebSocketV2 callback thread, and **more than one can be
in flight at once**. Three production bugs in two days came from that single
fact, so anything reached from a tick handler needs the same scrutiny:

| Symptom | Cause | Guard |
|---------|-------|-------|
| Two identical SENSEX positions at the same second, against a cap of 1 | `assess_order_safety` read the DB, `log_trade` wrote it, nothing held between | `ENTRY_LOCK` across check-and-write, **plus** one entry per index per signal bar |
| `rsi_state.json` spliced: *Extra data: line 1 column 2034* | two `open("w")` writers truncating over each other | `STATE_LOCK`, **plus** write-to-temp and `os.replace` so a crash cannot splice |
| Volume bars double-counted | duplicate ticks | dedupe on `sequence_number`, else `(volume_today, last_qty)` |
| **Two broker SELLs on one trade** (live only) | TSL (5s) and EOD (10s) loops both scan `OPEN`; the SELL is sent *before* `close_trade` is consulted, so its `AND status='OPEN'` guard protects the DB, not the broker | `claim_for_exit` moves `OPEN → EXITING` atomically **before** any order; `release_claim` on every failure path |

A claimed row still counts toward the open-position caps — otherwise claiming
one would free a slot and admit a new entry against a position not yet gone.

Each has two guards deliberately. A lock does not survive a kill signal
mid-write; an atomic swap does not stop two threads both deciding to trade.

---

## Execution (live mode)

Paper mode fills instantly at LTP. **Live crosses the bid-ask, and that cost is
the same order of magnitude as the entire measured edge:**

| Spread | MARKET round trip | vs measured edge (₹98.78/trade = 2.09% of notional) |
|--------|-------------------|------------------------------------------------|
| 1% | ~₹94 | **1.0×** |
| 2% | ~₹189 | **1.9×** |
| 3% | ~₹283 | **2.9×** |

Hence `max_option_spread_pct = 1.5`, and every trade now records `entry_bid`,
`entry_ask`, `entry_spread_pct`, `intended_price` and `slippage` so realised
execution cost is measurable rather than assumed.

**Fills are confirmed, never assumed.** After `placeOrder`, `broker_orders.confirm_fill`
polls the order book until the order is terminal:

| Outcome | Behaviour |
|---------|-----------|
| `complete` | Trade logged at the **broker's** average price; target/SL re-derived from the real fill |
| `rejected` / `cancelled` | **No position recorded** |
| Still pending at timeout | Treated as **UNKNOWN** — not logged, `CRITICAL` raised. May still fill, so check the broker terminal |
| Partial entry | Records the filled quantity only |
| Partial exit | Trade left **OPEN**; residual must be squared manually |

Reconciliation also reports **untracked broker positions** (held at broker, absent
from the DB — typically a crash between `placeOrder` and the DB write). Those carry
no SL, target or EOD square-off, so they raise `CRITICAL`.

**Session:** SmartAPI tokens expire. `SessionKeeper` re-authenticates on auth
errors only (not on rate limits), probed each heartbeat.

**Alerting:** set `ALERT_WEBHOOK_URL` in `.env` to POST `CRITICAL` events
(rate-limited to one per 30s). Unset = log only. Logs rotate at 10MB × 5.

---

## Monitoring

**Heartbeat** (every 60s) reports per index: spot, regime, RVOL warmup, and the
daily entry count against the per-index cap.

```text
[NIFTY] Spot: ₹24000.00 | Regime: BULLISH | Vol(5m): 24/8 1.31x OK |
        Entries: 2/6 (open 1, closed 1, PnL ₹1170)
```

**Scorecard** (`python3 scorecard.py`) adds two diagnostics beyond PnL/win-rate:

| Line | Answers |
|------|---------|
| `by DTE: DTE0 n=.. INR ..` | Does expiry-day (0-DTE) actually underperform? |
| `runner capture N% of INR X available` | What share of the gain that was *on the table* did we keep? `max_favorable_price` vs realised. |
| `execution: slippage INR .. \| avg entry spread ..%` | **The go/no-go metric.** Realised cost of crossing the spread. |
| `execution drag: N% of net PnL` | How much of the edge execution is eating. |
| `trades >= INR 1500` | How many trades cleared the runner threshold? |

Both are populated only for trades opened after the instrumentation landed;
older rows have no `dte` / `max_favorable_price` and are excluded.

---

## Index excursion (`trade_analysis.py`)

Trade rows record what the *option* did, not what the *index* did — so the key
post-mortem question was unanswerable: when a trade lost, was the **signal**
wrong (index never moved our way) or the **exit** wrong (index moved our way and
we still lost, to theta or a tight stop)?

```bash
python3 trade_analysis.py --backfill      # fill index_* columns from the candle cache
python3 trade_analysis.py --report --days 7
```

Backfills `index_at_entry / index_high / index_low / index_at_exit`,
`index_mfe_pct`, `index_mae_pct` and a verdict per trade:

| Verdict | Meaning |
|---------|---------|
| `WIN` | Profitable, index moved our way |
| `WIN_NO_INDEX_MOVE` | Profitable without a real index move — likely noise |
| `EXIT_WRONG` | **Lost while the index moved our way.** Exit problem. |
| `SIGNAL_WRONG` | Lost, index never moved our way. Signal problem. |

Post-hoc by design: it touches no trading file (safe during a frozen config
window), candle OHLC already carries the true intra-bar extremes so 5-min bars
give what a tick tracker would, and it works retroactively on closed trades.
Needs the candle cache current — run `backtest_data.py` after the session.

---

## Multi-timeframe (1-minute)

The 5-minute bar stays the signal timeframe. A 1-minute series is used for two
things, **both off by default**:

| Knob | Default | Alternatives |
|------|---------|--------------|
| `entry_timing` | `immediate` | `continuation`, `pullback` |
| `stop_mode` | `fixed_pct` | `structural_1m` |
| `confirm_window_min` | `3` | minutes a pending signal waits |
| `pullback_pct` | `0.15` | retracement required, pullback mode only |

**Entry timing.** A 5-min signal fires at the bar close, which on a vertical move
is the top of the move. `continuation` holds the signal until the index makes a
new 1-min extreme in that direction, so a move that peaks and fades is *never
filled*. `pullback` waits for a retracement instead: a better price, at the risk
of filling into a move that is already failing.

**Structural stops.** `structural_1m` adds an index-level invalidation — if the
index breaks the 1-min pivot that defined the setup, exit regardless of premium.
The fixed premium stop stays as a backstop. Against a +30% target this lifts R:R
from 3:1 toward 5-7:1, at the cost of more stop-outs; which wins is a backtest
question, not an argument.

Everything works on the **index**, never the option: the bot subscribes to index
ticks continuously but never to option ticks, so a 1-min option series does not
exist before entry.

Cache 1-minute candles with:

```bash
python3 backtest_data.py --days 30 --also-1min
```

> **Defaults reproduce today's behaviour exactly, and nothing in the live path
> reads these yet.** Do not move them off their defaults mid-experiment — they
> change which trades are taken, so the forward sample restarts.

---

## Signal screening (`signal_lab.py`)

A full backtest answers *"does this configuration make money"*, which mixes
signal quality with option pricing, costs, stops and caps. When the answer is
no, you cannot tell which part failed — that is exactly what happened to
`VOLUME_BREAKOUT`.

This asks the prior question: **does the signal predict index movement at all?**
It reads only index and futures candles. A signal that cannot beat its own
unconditional baseline here will not be rescued by any stop or target tuning,
and does not deserve a backtest.

```bash
python3 signal_lab.py                        # screen every hypothesis
python3 signal_lab.py --signals orb nr7
python3 signal_lab.py --index NIFTY --windows 15 30 60 90
```

Reports the **signed** forward index return in the direction each signal called.
Under no edge that mean is zero, so the test is a t-test against zero, with the
unconditional baseline printed above for comparison.

**Multiple testing is handled explicitly.** Screening N hypotheses across M
windows guarantees some will look good by chance — validation on a pure random
walk produced a `t = 2.03` false positive at the raw 1.96 bar. The report prints
the Bonferroni-corrected threshold (|t| > 3.20 for 36 tests) and only flags
signals that clear it.

Signals shipped: `volume_breakout` (the current live one, as a control), `orb`
(opening-range break), `mean_reversion`, `nr7` (range contraction then break),
`ema_trend`, `first_hour`. Adding one is a function in `SIGNALS`.

### Out-of-sample validation — do not skip this

```bash
python3 signal_lab.py --validate first_hour
```

Runs one signal over **disjoint** periods — first half, second half, last 60
sessions, last 20 — and prints each separately. A finding discovered on all the
data is not evidence until it survives on data it was not discovered on. **Signs
must agree across every period.**

This is not theoretical. `first_hour` screened at t = −6.44 (NIFTY) and −4.67
(SENSEX) on the full history, which looked like a real fade-the-open edge. Split
apart:

| Period | NIFTY 45m | SENSEX 45m |
|--------|-----------|------------|
| first half | t = −1.27 | **t = +0.65** |
| second half | t = −6.91 | t = −6.12 |
| last 60 | t = −5.34 | t = −2.46 |
| **last 20** | **t = −1.77** | **t = −0.70** |

The entire effect lives in the second half, SENSEX flips sign in the first, and
the recent period is null. A regime artifact, not an edge.

`--since` / `--until` screen a specific window directly.

### Judging an edge against an instrument

```bash
python3 signal_lab.py --instrument future --hold 45
```

A signal is only useful if its edge clears the cost of the instrument used to
express it. Comparing round-trip cost percentages across instruments is
meaningless without dividing by leverage — options are ~116× levered, which very
nearly cancels their much larger percentage cost:

| hold | option | future | wins |
|------|--------|--------|------|
| 5 min | 0.0154% | 0.0350% | option |
| 30 min | 0.0301% | 0.0350% | option |
| 45 min | 0.0389% | 0.0350% | **future** |
| 120 min | 0.0831% | 0.0350% | **future** |

**The instrument is not the problem — theta is, and only past ~20 minutes.**
Every screened signal falls short on both: `first_hour` inverted by 2.4×,
`ema_trend` by 3.9×, `volume_breakout` by 4.9×. Moving to futures would not have
rescued them.

---

## Backtesting

Forward paper testing yields 4-6 trades a day, so ~40 trades takes a fortnight
and still lands at t~1.5. The backtest replays the **same strategy code** over
cached history, turning that into thousands of signals.

```bash
python3 backtest_data.py --days 365      # pull + cache candles (slow, once)
python3 backtest_data.py --report        # what is cached
python3 backtest_engine.py --days 180
python3 backtest_engine.py --days 180 --set trending_target_mult=1.22
python3 backtest_engine.py --days 180 --no-costs
```

**It drives the real code.** There is no reimplementation of the regime
classifier, RSI, volume gate, entry rules or trailing ladder — the engine
constructs a real `StrategyBrain` and calls the real `risk_monitors._trailed_stop`
against a real (temporary) SQLite database, so risk caps, cooldowns and session
windows all apply. Change the bot, and the backtest changes with it.

Three things are substituted, because they cannot be replayed:

| Substituted | Why |
|-------------|-----|
| Clock | A controllable clock replaces `time` in `strategy_brain` and the IST helpers in `database`, so "today" follows the simulated date. Fully reverted afterwards (tested). |
| Broker | A fake order manager records fills instead of placing orders |
| Option premium | Expired contracts are delisted, so an ATM contract is repriced from the index path via Black-Scholes |

**Costs are modelled** (~Rs60/round trip: brokerage, STT, exchange, GST, stamp).
Flat brokerage is ~1% of a Rs6,000 notional — about half the strategy's measured
edge — so excluding it would flatter every result.

**IV is calibrated per index, not assumed.** Backed out of real fills, NIFTY
priced at ~13% and SENSEX at ~8%; a single shared default over-priced SENSEX by
71% and would have changed which parameters looked best.

> **Limits.** The option model has no IV smile, no IV crush around events, and no
> real bid-ask. Treat absolute rupee results as indicative; the trustworthy
> output is the *comparison between parameter settings*, where model error
> largely cancels.

---

## Telegram alerts (`telegram_notifier.py`)

Runs as its **own process** next to `main.py`. It tails the log the bot already
writes and answers commands from your phone. It imports nothing from the trading
path for behaviour and opens the database **read-only**, so it cannot affect a trade.

```bash
# 1. @BotFather -> /newbot -> copy the token into .env as TELEGRAM_BOT_TOKEN
# 2. message your bot once, then:
python3 telegram_notifier.py --whoami     # prints TELEGRAM_CHAT_ID
# 3. verify, then run it in its own tmux window
python3 telegram_notifier.py --test
python3 telegram_notifier.py
```

| Command | Returns |
|---------|---------|
| `/status` | Latest heartbeat: spot, regime, entries per index |
| `/open` | Open trades with entry, target, SL |
| `/pnl` | Today's realised PnL, per index, plus execution drag |
| `/trades` | Last 5 closed trades |
| `/log` | Recent WARNING/ERROR lines |
| `/mute` `/unmute` | Pause routine alerts — emergencies still get through |

Pushed automatically: entries, exits, and every abort condition
(`UNTRACKED`, `CIRCUIT BREAKER`, unconfirmed order, partial exit, bad price feed,
incomplete square-off). Only `TELEGRAM_CHAT_ID` is answered; other chats are ignored.

> **`ALERT_WEBHOOK_URL` is a different mechanism.** `broker_health.AlertHandler`
> posts `{"text": ...}`, which Telegram rejects — it needs `chat_id`. Pointing
> that variable at a Telegram URL will not work, and preflight's live gate still
> checks for it separately.

---

## Go-live checklist (`preflight_check.py`)

Run before every session; **exit code 1 means do not start.**

```bash
python3 preflight_check.py --capital 75000     # paper checks
python3 preflight_check.py --live --capital 75000
```

| Check | Blocks on |
|-------|-----------|
| credentials | any of `SMART_API_KEY`/`SMARTAPI_KEY`, `CLIENT_ID`, `PASSWORD`/`PIN`, `TOTP_SECRET` missing |
| module imports | any live-path module failing to import |
| db schema | a required column missing (incl. `dte`, `slippage`, `entry_spread_pct`) |
| stale open trades | an `OPEN` row from a previous day |
| scrip master | no usable `scrip_master.json` |
| risk:reward, trail vs target, session window | internally inconsistent config |
| capital | daily loss cap > 3% of capital, or peak deployment > capital |
| alerting *(live only)* | `ALERT_WEBHOOK_URL` unset |

Live mode also **warns** if the daily entry cap is above 2 — the first live week
should run at 1/day to exercise the order path cheaply.

---

## Trade history (`view_completed.py`)

Defaults to the **last 7 IST days**, newest first, with per-trade PnL and a
per-day breakdown.

```bash
python3 view_completed.py                    # last 7 days
python3 view_completed.py --days 30          # last 30 days
python3 view_completed.py --all              # everything
python3 view_completed.py --date 2026-08-25  # one day
python3 view_completed.py --since 2026-08-21 --until 2026-08-28
```

Filters: `--index NIFTY`, `--reason TARGET_HIT`, `--entry-reason VOLUME_BREAKOUT`,
`--open` (OPEN rows instead), `--limit N`.
Output: `--full` (every column), `--csv` (pipe to a file).

---

## Ops notes

- `.env` is **gitignored**. Recreate on each host; do not re-commit secrets.  
- After upgrading from 1-min signals, old `rsi_state.json` is ignored (bar size mismatch).  
- Futures must resolve at startup or the volume gate blocks entries.
- Seeding needs historical-data permission on the SmartAPI key; BFO (SENSEX) candle
  support is broker-dependent — check the `[seed]` lines in the log.  
- Heartbeat logs spot, regime, volume warmup/`rvol`, per-index entries and scorecard PnL.
- **API pacing:** `RateLimitedAPI` serialises *all* REST calls at 1/sec behind one
  lock — the TSL monitor (`ltpData` per open trade every 5s), the EOD monitor, the
  entry path and startup seeding all share it. 429s are unlikely at this rate; the
  real cost is that an entry's `ltpData` can queue ~1–2s behind monitor calls, which
  is slippage against the signal-bar close. Worth measuring before widening.
- **Verify the LTP decode on first run.** Websocket LTP is decoded as paise
  (`/100`) unconditionally. Check the first `📈 Tick Received` line against the
  real index level; an out-of-band value is dropped and logged, not traded.  

Full change history: see **[CHANGELOG.md](CHANGELOG.md)**.
