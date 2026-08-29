# config.py — live knobs. Keep PAPER_TRADING True until paper results are stable.
from datetime import datetime, timedelta

# Scorecard / eval: ignore trades before this date (bugfix + volume strategy deploy)
SCORECARD_SINCE = "2026-08-21"

ACTIVE_INDICES = ["NIFTY", "SENSEX"]

# Paper Trading Mode (Set to False for Live Broker Execution)
PAPER_TRADING = True

# Risk / session (min-loss posture: fewer trades, hard halt, no late entries)
RISK = {
    "max_daily_loss_inr": 1500.0,
    "max_consecutive_losses": 3,
    "max_open_per_index": 1,
    "max_open_total": 2,
    # Live stays tight; paper must not burn the day on early TREND_CONT noise (Aug 24 miss).
    "max_daily_entries": 4,
    "paper_max_daily_entries": 12,
    # Soft cap on TREND_CONT/RSI_HOOK so VOLUME_BREAKOUT still has room under the daily budget.
    "max_trend_entries_per_day": 4,
    # Per-index daily entry cap. max_open_per_index is only a CONCURRENT cap, so
    # one index could previously churn the entire daily budget by itself.
    "max_daily_entries_per_index": 3,
    "paper_max_daily_entries_per_index": 6,
    "session_start_hhmm": 945,
    "entry_cutoff_hhmm": 1430,
    "eod_squareoff_hhmm": 1515,
    "enable_choppy_entries": False,
    "min_option_premium": 25.0,
    "max_premium_risk_inr": 8000.0,
    "time_stop_minutes": 25,
    "time_stop_min_gain_mult": 1.02,
    # Hybrid: 5-min closed bars for regime/entries/RVOL; exits stay tick/1-min monitors.
    "signal_bar_sec": 300,
    "breakout_max_age_sec": 600,
    "require_volume_expansion": True,
    "enable_volume_breakout": True,
    "enable_volume_breakout_in_chop": True,
    "volume_sma_bars": 8,
    # 5-min futures: breakout uses volume_mult; RSI hook may use hook_mult
    "volume_mult": 1.2,
    "volume_hook_mult": 1.0,
    # Hold expansion ~2 signal bars so TREND_CONT can use a fresh breakout.
    "volume_ok_hold_sec": 600,
    # Aug 24: TREND_CONT with only rvol>=1.0 fired into chop; require real expansion.
    "trend_cont_requires_expansion": True,
    "trend_cont_rsi_max": 68.0,
    # Days-to-expiry floor for the option we buy. 0 = allow expiry-day (0-DTE),
    # which is what the bot did implicitly. On 0-DTE, theta alone can hit a -10%
    # stop with no adverse spot move. Set 1 to skip expiry day, 2 to force the
    # next weekly. Trade-off: higher DTE = lower gamma, so +22% is slower to reach.
    "min_dte": 0,
    "min_option_volume": 500.0,
    "max_option_spread_pct": 3.0,
}


def daily_entry_cap():
    """Paper uses a higher budget so early noise cannot exhaust the session."""
    if PAPER_TRADING:
        return int(RISK.get("paper_max_daily_entries", RISK["max_daily_entries"]))
    return int(RISK["max_daily_entries"])


def index_daily_entry_cap():
    """Per-index daily entry budget (paper gets the looser one)."""
    if PAPER_TRADING:
        return int(RISK.get("paper_max_daily_entries_per_index",
                            RISK.get("max_daily_entries_per_index", 3)))
    return int(RISK.get("max_daily_entries_per_index", 3))


def signal_bar_sec():
    return max(60, int(RISK.get("signal_bar_sec", 300)))


def signal_bar_bucket(now_ts=None):
    """IST wall-clock bucket for signal bars (aligns to :00/:05/:10 ... when bar=300)."""
    from datetime import timezone

    bar = signal_bar_sec()
    if now_ts is not None:
        now_ist = datetime.fromtimestamp(now_ts, tz=timezone.utc) + timedelta(hours=5, minutes=30)
    else:
        now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    secs = now_ist.hour * 3600 + now_ist.minute * 60 + now_ist.second
    return secs // bar


# Fallback lot sizes if scrip master omits lotsize (must match current NSE/BSE lots)
FALLBACK_LOT_SIZE = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}

INDICES_CONFIG = {
    "NIFTY": {
        "token": "26000",
        "index_token": "26000",
        "exchange": "NSE",
        "option_exchange": "NFO",
        "fut_exchange_type": 2,
        "symbol": "NIFTY",
        # Sanity band for decoded websocket LTP (paise -> rupees). Out-of-band
        # ticks are dropped instead of being fed to the strategy.
        "spot_min": 5000.0,
        "spot_max": 100000.0,
        "vwap_buffer": 2.0,
        "ema_spread_min": 1.0,
        "regime_mean_bars": 20,
        # Tighter than original 15%/50%: smaller loss, realistic option target
        "trending_sl_mult": 0.90,       # 10% stop
        "trending_target_mult": 1.22,   # 22% target
        "choppy_sl_mult": 0.95,
        "choppy_target_mult": 1.08,
    },
    "SENSEX": {
        "token": "99919000",
        "index_token": "99919000",
        "exchange": "BSE",
        "option_exchange": "BFO",
        "fut_exchange_type": 4,
        "symbol": "SENSEX",
        "spot_min": 20000.0,
        "spot_max": 300000.0,
        "vwap_buffer": 8.0,
        "ema_spread_min": 6.0,
        "regime_mean_bars": 20,
        "trending_sl_mult": 0.90,
        "trending_target_mult": 1.22,
        "choppy_sl_mult": 0.95,
        "choppy_target_mult": 1.08,
    }
}
