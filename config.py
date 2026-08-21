# config.py — live knobs. Keep PAPER_TRADING True until paper results are stable.

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
    "max_daily_entries": 4,
    "session_start_hhmm": 945,
    "entry_cutoff_hhmm": 1430,
    "eod_squareoff_hhmm": 1515,
    "enable_choppy_entries": False,
    "min_option_premium": 25.0,
    "max_premium_risk_inr": 8000.0,
    "time_stop_minutes": 25,
    "time_stop_min_gain_mult": 1.02,
    "require_volume_expansion": True,
    "enable_volume_breakout": True,
    "volume_sma_bars": 20,
    # 1-min futures: 1.5x almost never prints; breakout uses volume_mult, RSI hook uses hook_mult
    "volume_mult": 1.2,
    "volume_hook_mult": 1.0,
    "volume_ok_hold_sec": 180,
}

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
        "vwap_buffer": 10.0,
        "ema_spread_min": 8.0,
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
        "vwap_buffer": 30.0,
        "ema_spread_min": 20.0,
        "trending_sl_mult": 0.90,
        "trending_target_mult": 1.22,
        "choppy_sl_mult": 0.95,
        "choppy_target_mult": 1.08,
    }
}
