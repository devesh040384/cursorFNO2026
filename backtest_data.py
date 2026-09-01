"""Candle fetch + on-disk cache for the backtest.

Angel One caps each getCandleData call to a short window, and the account is
rate-limited to ~1 request/sec, so pulling two years of 5-minute bars takes
hundreds of calls. Everything is cached to CSV: the slow pull happens once, and
every subsequent parameter sweep reads from disk.

  python3 backtest_data.py --days 365          # pull and cache
  python3 backtest_data.py --days 365 --report # show what is cached
"""
import argparse
import csv
import logging
import os
import time
from datetime import datetime, timedelta

from config import INDICES_CONFIG
from ist_time import IST, ist_now

CACHE_DIR = "backtest_cache"
# Angel One rejects long ranges for intraday intervals. One-minute data is 5x
# denser, so it needs a shorter window per request than five-minute does.
CHUNK_DAYS = 30
CHUNK_DAYS_BY_INTERVAL = {"ONE_MINUTE": 7, "THREE_MINUTE": 15}
INTERVAL = "FIVE_MINUTE"
INTERVALS = ("ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "TEN_MINUTE", "FIFTEEN_MINUTE")
FIELDS = ["timestamp", "open", "high", "low", "close", "volume"]


def cache_path(exchange, token, interval=INTERVAL):
    return os.path.join(CACHE_DIR, "%s_%s_%s.csv" % (exchange, token, interval))


def load_cache(exchange, token, interval=INTERVAL):
    """Return {iso_timestamp: row} so re-fetching a window merges, not duplicates."""
    path = cache_path(exchange, token, interval)
    if not os.path.exists(path):
        return {}
    rows = {}
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rows[row["timestamp"]] = {
                    "timestamp": row["timestamp"],
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
    except Exception as e:
        logging.warning("cache read failed for %s: %s", path, e)
    return rows


def save_cache(exchange, token, rows, interval=INTERVAL):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = cache_path(exchange, token, interval)
    ordered = sorted(rows.values(), key=lambda r: r["timestamp"])
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(ordered)
    return len(ordered)


def _fetch_chunk(smart_api, exchange, token, start, end, interval=INTERVAL, retries=2):
    params = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }
    for attempt in range(retries + 1):
        try:
            resp = smart_api.getCandleData(params)
            if resp and resp.get("status") and resp.get("data") is not None:
                return resp["data"]
            message = resp.get("message") if isinstance(resp, dict) else resp
            logging.warning("chunk rejected %s..%s: %s", params["fromdate"], params["todate"], message)
        except Exception as e:
            logging.warning("chunk error %s (%d): %s", params["fromdate"], attempt + 1, e)
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))
    return []


def chunk_days(interval):
    return CHUNK_DAYS_BY_INTERVAL.get(interval, CHUNK_DAYS)


def fetch_range(smart_api, exchange, token, days, interval=INTERVAL, pause=1.2):
    """Pull `days` of history in chunks, merging into the cache. Returns row count."""
    rows = load_cache(exchange, token, interval)
    before = len(rows)
    end = ist_now().replace(tzinfo=None)
    start = end - timedelta(days=days)

    cursor = start
    chunks = 0
    span = chunk_days(interval)
    while cursor < end:
        stop = min(cursor + timedelta(days=span), end)
        for candle in _fetch_chunk(smart_api, exchange, token, cursor, stop, interval):
            try:
                rows[str(candle[0])] = {
                    "timestamp": str(candle[0]),
                    "open": float(candle[1]), "high": float(candle[2]),
                    "low": float(candle[3]), "close": float(candle[4]),
                    "volume": float(candle[5]),
                }
            except (IndexError, TypeError, ValueError):
                continue
        chunks += 1
        cursor = stop
        time.sleep(pause)  # stay inside the account rate limit

    total = save_cache(exchange, token, rows, interval)
    logging.info("%s:%s  %d chunks, %d rows cached (+%d new)",
                 exchange, token, chunks, total, total - before)
    return total


def parse_ts(raw):
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).replace(tzinfo=None)


def load_series(exchange, token, interval=INTERVAL):
    """Cached bars as a list of dicts with a parsed `dt`, oldest first."""
    out = []
    for row in load_cache(exchange, token, interval).values():
        dt = parse_ts(row["timestamp"])
        if dt is None:
            continue
        item = dict(row)
        item["dt"] = dt
        out.append(item)
    out.sort(key=lambda r: r["dt"])
    return out


def group_by_session(bars):
    """Split a bar series into {date: [bars]} so the replay runs day by day."""
    sessions = {}
    for bar in bars:
        sessions.setdefault(bar["dt"].date(), []).append(bar)
    return sessions


def describe_cache():
    if not os.path.isdir(CACHE_DIR):
        return ["no cache yet — run: python3 backtest_data.py --days 365"]
    lines = []
    for name in sorted(os.listdir(CACHE_DIR)):
        if not name.endswith(".csv"):
            continue
        exchange, token, interval = name[:-4].split("_", 2)
        bars = load_series(exchange, token, interval)
        if not bars:
            lines.append("  %-28s empty" % name)
            continue
        days = len(group_by_session(bars))
        lines.append("  %-28s %6d bars  %4d sessions  %s .. %s"
                     % (name, len(bars), days,
                        bars[0]["dt"].date(), bars[-1]["dt"].date()))
    return lines or ["cache directory is empty"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fetch and cache backtest candles.")
    parser.add_argument("--days", type=int, default=365, help="history depth (default 365)")
    parser.add_argument("--report", action="store_true", help="only show what is cached")
    parser.add_argument("--indices", nargs="*", help="default: all configured")
    parser.add_argument("--interval", default=INTERVAL, choices=INTERVALS,
                        help="candle size (default %s)" % INTERVAL)
    parser.add_argument("--also-1min", action="store_true",
                        help="additionally cache ONE_MINUTE bars for entry confirmation")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.report:
        print("\n".join(describe_cache()))
        return 0

    from main import authenticate_broker
    from options_chain_builder import DynamicOptionsChainBuilder

    smart_api = authenticate_broker()
    if not smart_api:
        print("Could not authenticate; cannot fetch history.")
        return 1

    scrip = []
    if os.path.exists("scrip_master.json"):
        import json
        with open("scrip_master.json", "r", encoding="utf-8") as f:
            scrip = json.load(f)

    wanted = [args.interval]
    if args.also_1min and "ONE_MINUTE" not in wanted:
        wanted.append("ONE_MINUTE")

    for symbol in (args.indices or list(INDICES_CONFIG)):
        cfg = INDICES_CONFIG[symbol]
        for interval in wanted:
            # 1-min is only needed on the index: entry confirmation and the
            # structural stop both read the index, never the option.
            fetch_range(smart_api, cfg["exchange"], cfg["index_token"], args.days, interval)
        builder = DynamicOptionsChainBuilder(index_name=symbol, smart_api=smart_api)
        builder.load_scrip_master(scrip)
        fut = builder.get_nearest_expiry_future()
        if fut and fut.get("token"):
            # Futures volume drives the RVOL gate, so it must be cached too.
            fetch_range(smart_api, fut["exchange"], fut["token"], args.days, args.interval)
        else:
            logging.error("%s: no future resolved; RVOL gate cannot be replayed", symbol)

    print("\n".join(describe_cache()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
