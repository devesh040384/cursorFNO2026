# AI FNO Trading Bot (NIFTY / SENSEX)

Paper-first Angel One SmartAPI bot for **index options**. It classifies index regime, gates entries with futures volume, buys ATM CE/PE, and manages exits with trailing stop, time-stop, and EOD square-off.

Keep `PAPER_TRADING = True` in `config.py` until paper results are stable.

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
| `config.py` | All live knobs |

**Startup seeding**

Building 22 x 5-min bars from live ticks takes ~110 minutes, so entries could not
start until ~11:35. On boot the bot now fetches closed `FIVE_MINUTE` candles
(`getCandleData`) for each index and its nearest future, filling regime/RSI history
and the futures RVOL window. The in-progress candle is dropped and no breakout
event is carried over. Entries are then gated only by `session_start_hhmm`
(**09:45**), which skips the opening-auction noise.

If the candle API fails or returns too few bars, the bot logs a warning and falls
back to the old live warmup — it does not trade on partial history.

**Hybrid timeframe**

- **5-min IST closed bars** → regime, `VOLUME_BREAKOUT`, `TREND_CONT`, `RSI_HOOK`, futures RVOL  
- **Tick / monitor loop** → LTP, trailing SL, time-stop, EOD  

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

- Nearest unexpired ATM CE/PE for the index
- Liquidity: min option volume 500; spread ≤ 3% when depth exists (no invented 2% spread)
- Targets / SL (trending): **+22% / −10%**

After a fill: **15-min** per-index cooldown.

---

## Risk & session

| Knob | Paper | Live (default) |
|------|-------|----------------|
| Daily entries | **12** | **4** |
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

### Exits

1. **TARGET_HIT** / **STOP_LOSS_HIT**  
2. **Trailing:** +4% → SL to entry; +8% → entry×1.02; +15% → lock 50% of peak gain  
3. **TIME_STOP** (25 min, gain &lt; 2%)  
4. **EOD_SQUAREOFF** (15:15) — refuses exit if LTP missing / ₹0  

---

## Key config (`config.py`)

```text
signal_bar_sec            = 300      # 5-min signals
breakout_max_age_sec      = 600
volume_mult               = 1.2      # breakout / TREND_CONT
volume_hook_mult          = 1.0      # RSI_HOOK
volume_sma_bars           = 8
volume_ok_hold_sec        = 600
trend_cont_requires_expansion = True
paper_max_daily_entries   = 12
max_daily_entries         = 4
max_trend_entries_per_day = 4
PAPER_TRADING             = True
ACTIVE_INDICES            = NIFTY, SENSEX
SCORECARD_SINCE           = 2026-08-21
```

---

## Ops notes

- `.env` is **gitignored**. Recreate on each host; do not re-commit secrets.  
- After upgrading from 1-min signals, old `rsi_state.json` is ignored (bar size mismatch).  
- Futures must resolve at startup or the volume gate blocks entries.
- Seeding needs historical-data permission on the SmartAPI key; BFO (SENSEX) candle
  support is broker-dependent — check the `[seed]` lines in the log.  
- Heartbeat logs spot, regime, volume warmup/`rvol`, and scorecard PnL.  

Full change history: see **[CHANGELOG.md](CHANGELOG.md)**.
