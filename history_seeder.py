"""Seed signal-bar history from broker candles so entries can start at 09:45.

Without this, the bot builds its 22 spot bars and 8 futures volume bars from live
ticks only: 22 x 5min = 110 minutes, so the first possible entry was ~11:35.
We pull closed FIVE_MINUTE candles at startup instead and hand them to
StrategyBrain / VolumeExpansionGate in the exact shape the live tick path uses.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from config import INDICES_CONFIG, RISK, history_token, signal_bar_bucket, signal_bar_sec
from indicators import wilder_rsi

IST = timezone(timedelta(hours=5, minutes=30))

# Angel One interval names keyed by signal bar seconds.
_INTERVALS = {
    60: "ONE_MINUTE",
    180: "THREE_MINUTE",
    300: "FIVE_MINUTE",
    600: "TEN_MINUTE",
    900: "FIFTEEN_MINUTE",
}

# Enough closed bars for EMA21 + Wilder RSI(14) warmup with headroom.
SEED_BARS = 60
# Calendar days to look back; NSE holidays/weekends make this deliberately loose.
LOOKBACK_DAYS = 6


def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)


def interval_name(bar_sec=None):
    bar_sec = signal_bar_sec() if bar_sec is None else int(bar_sec)
    return _INTERVALS.get(bar_sec)


def _parse_ts(raw):
    """Angel candle timestamps look like '2026-08-25T09:20:00+05:30'."""
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def fetch_candles(smart_api, exchange, token, interval, days_back=LOOKBACK_DAYS, retries=2):
    """Return [(ist_dt, open, high, low, close, volume), ...] oldest first, or []."""
    if not smart_api or not token or not interval:
        return []
    to_dt = now_ist()
    from_dt = to_dt - timedelta(days=days_back)
    params = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
    }
    for attempt in range(retries + 1):
        try:
            resp = smart_api.getCandleData(params)
            if resp and resp.get("status") and resp.get("data"):
                rows = []
                for c in resp["data"]:
                    ts = _parse_ts(c[0])
                    if ts is None:
                        continue
                    rows.append((ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])))
                rows.sort(key=lambda r: r[0])
                return rows
            msg = resp.get("message") if isinstance(resp, dict) else resp
            logging.warning(f"[seed] candle fetch rejected {exchange}:{token} -> {msg}")
        except Exception as e:
            logging.warning(f"[seed] candle fetch error {exchange}:{token} ({attempt + 1}): {e}")
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return []


def _drop_forming_bar(rows, now_ts=None):
    """Angel returns the in-progress bar last; it is not a closed bar yet."""
    if not rows:
        return rows
    current = signal_bar_bucket(now_ts)
    out = list(rows)
    while out and signal_bar_bucket(out[-1][0].timestamp()) == current:
        out.pop()
    return out


def seed_price_history(smart_api, brain, symbol, bars=SEED_BARS):
    """Fill brain.price_histories/closed_rsi/last_closed_rsi for one index."""
    cfg = INDICES_CONFIG.get(symbol) or {}
    interval = interval_name()
    if interval is None:
        logging.warning(f"[seed] no candle interval for bar={signal_bar_sec()}s; {symbol} not seeded")
        return 0

    # getCandleData needs the AMXIDX token, not the websocket one. NIFTY's
    # index_token (26000) returns an EMPTY series with status=True, so seeding
    # failed silently and NIFTY ran on cold warmup — no entries until ~10:50.
    token = history_token(symbol)
    rows = _drop_forming_bar(
        fetch_candles(smart_api, cfg.get("exchange", "NSE"), token, interval)
    )
    if len(rows) < 22:
        logging.error(
            f"[seed] {symbol}: only {len(rows)} closed bars from {cfg.get('exchange')}:{token}. "
            f"Falling back to live warmup — NO ENTRIES until ~22 bars have built "
            f"(roughly 10:50 IST). Check the candle token for this index."
        )
        return 0

    closes = [r[4] for r in rows[-bars:]]
    # evaluate_tick() treats history[-1] as the forming bar and overwrites it on
    # every tick, so append a placeholder that live ticks may safely clobber.
    brain.price_histories[symbol] = closes + [closes[-1]]

    # Prior closed-bar RSI so an RSI_HOOK 50-cross can trigger on the first live bar.
    brain.last_closed_rsi[symbol] = wilder_rsi(closes[:-1])
    # Recent RSI ring buffer feeds the dip(<45)/spike(>55) hook confirmation.
    ring = []
    for i in range(max(1, len(closes) - 5), len(closes) + 1):
        ring.append(wilder_rsi(closes[:i]))
    brain.closed_rsi[symbol] = ring[-5:]

    now = time.time()
    brain.last_candle_times[symbol] = now
    brain.last_signal_buckets[symbol] = signal_bar_bucket(now)
    logging.info(
        f"[seed] {symbol}: {len(closes)} closed {signal_bar_sec() // 60}m bars "
        f"(last close {closes[-1]:.2f}, rsi {wilder_rsi(closes):.1f})"
    )
    return len(closes)


def seed_volume_history(smart_api, gate, symbol, fut, bars=None):
    """Fill VolumeExpansionGate.closed_volumes from futures candles."""
    if not fut or not fut.get("token"):
        return 0
    need = int(RISK.get("volume_sma_bars", 20))
    bars = max(need * 3, need + 4) if bars is None else bars
    interval = interval_name()
    if interval is None:
        return 0

    rows = _drop_forming_bar(
        fetch_candles(smart_api, fut.get("exchange", "NFO"), fut.get("token"), interval)
    )
    vols = [r[5] for r in rows[-bars:] if r[5] > 0]
    if len(vols) < need:
        logging.warning(
            f"[seed] {symbol} futures volume: {len(vols)}/{need} bars; RVOL stays in warmup"
        )
        return 0

    gate.closed_volumes[symbol] = vols
    gate.forming_vol[symbol] = 0.0
    now = time.time()
    gate.last_bar_time[symbol] = now
    gate.last_bar_bucket[symbol] = signal_bar_bucket(now)
    # Seeded bars are historical: never let them fire a stale breakout event.
    gate.breakout_event[symbol] = None
    gate.volume_ok[symbol] = False
    gate.volume_ok_until[symbol] = 0.0
    # last_session_vol stays None so the first live tick establishes the baseline
    # instead of booking the whole session cumulative as one bar's increment.
    gate.last_session_vol[symbol] = None
    rv = gate.rvol(symbol)
    logging.info(
        f"[seed] {symbol} futures: {len(vols)} volume bars, rvol={rv:.2f}x" if rv is not None
        else f"[seed] {symbol} futures: {len(vols)} volume bars"
    )
    return len(vols)


def seed_all(smart_api, brain, futures_by_symbol, symbols=None):
    """Seed price + futures volume history for every active index."""
    if not smart_api:
        logging.warning("[seed] no broker session; skipping history seed (live warmup applies)")
        return {}
    symbols = list(symbols or INDICES_CONFIG.keys())
    result = {}
    for symbol in symbols:
        try:
            px = seed_price_history(smart_api, brain, symbol)
            vol = seed_volume_history(smart_api, brain.volume_gate, symbol, futures_by_symbol.get(symbol))
            result[symbol] = {"price_bars": px, "volume_bars": vol}
        except Exception as e:
            logging.error(f"[seed] {symbol} failed: {e}")
            result[symbol] = {"price_bars": 0, "volume_bars": 0}
    ready = [s for s, r in result.items() if r["price_bars"] and r["volume_bars"]]
    logging.info(
        f"[seed] ready at open: {ready or 'none'} | entries gated by session_start "
        f"{RISK.get('session_start_hhmm', 945)} IST"
    )
    return result
