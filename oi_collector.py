"""Collect option-chain snapshots so positioning signals can be screened later.

Why this exists as a collector rather than a screen
---------------------------------------------------
Price signals were screened over 248 cached sessions in an afternoon. Options
positioning cannot be: `getCandleData` returns OHLCV with **no open interest**,
and expired contracts are delisted, so no chain history is retrievable at any
price. Open interest has to be collected forward before any hypothesis about it
can be tested. There is no shortcut and no way to shorten the wait.

Design rule: **store raw, derive later.**
-----------------------------------------
Every snapshot is written as-is — strike, type, LTP, OI, volume, bid, ask. No
COI deltas, no PCR, no gamma walls are computed here. Those definitions will
change as we learn (which window? which strikes? OI-PCR or volume-PCR?), and a
premature aggregation would silently destroy the data needed to test the next
version. Metrics are computed from the raw table by the screen, not by this.

Runs as its own process alongside main.py, like telegram_notifier. It never
imports the trading path for behaviour and never writes trade_history.db.

  python3 oi_collector.py                 # run for the session
  python3 oi_collector.py --once          # one snapshot, to verify fields
  python3 oi_collector.py --report        # what has been collected so far
"""
import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import timedelta

from config import ACTIVE_INDICES, INDICES_CONFIG
from ist_time import ist_hhmm, ist_now, ist_stamp, ist_today

DB_FILE = "oi_history.db"
# Every 3 minutes: fast enough for a 15-minute rolling COI delta to have five
# observations, slow enough to stay far inside the rate limit.
INTERVAL_SEC = 180
# ATM +/- this many strikes. The action worth measuring is near the money.
STRIKE_DEPTH = 5
SESSION_START_HHMM = 900
SESSION_END_HHMM = 1530
# Angel One accepts a token list per call, but a very long list gets rejected.
MAX_TOKENS_PER_CALL = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at   TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    index_name    TEXT NOT NULL,
    expiry        TEXT,
    dte           INTEGER,
    strike        REAL NOT NULL,
    option_type   TEXT NOT NULL,
    spot          REAL,
    ltp           REAL,
    open_interest REAL,
    volume        REAL,
    bid           REAL,
    ask           REAL,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_lookup
    ON chain_snapshots (index_name, trade_date, captured_at);
CREATE INDEX IF NOT EXISTS idx_snap_strike
    ON chain_snapshots (index_name, trade_date, strike, option_type);
"""

# Field names vary across SmartAPI responses; try each in order.
OI_KEYS = ("opnInterest", "openInterest", "opninterest", "oi")
VOL_KEYS = ("tradeVolume", "volume", "totQtyTraded", "opVolume")
LTP_KEYS = ("ltp", "lastTradedPrice", "last_traded_price")


def _first(row, keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def connect(db_path=DB_FILE):
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def in_session():
    return SESSION_START_HHMM <= ist_hhmm() < SESSION_END_HHMM


def nearest_strikes(builder, spot, depth=STRIKE_DEPTH):
    """ATM +/- `depth` strikes of the nearest expiry, both CE and PE.

    Reuses the live chain builder's parsing so strike/expiry handling cannot
    drift from what the bot itself resolves.
    """
    contracts = builder.nfo_contracts or []
    if not contracts:
        builder.load_scrip_master()
        contracts = builder.nfo_contracts or []
    if not contracts:
        return []

    today = builder._ist_midnight()
    dated = []
    for c in contracts:
        parsed = builder._parse_expiry(c.get("expiry"))
        if parsed and parsed >= today:
            c["_expiry_dt"] = parsed
            dated.append(c)
    if not dated:
        return []

    nearest = min(d["_expiry_dt"] for d in dated)
    rows = []
    for c in dated:
        if c["_expiry_dt"] != nearest:
            continue
        symbol = str(c.get("symbol") or "").upper()
        opt_type = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else None
        if opt_type is None:
            continue
        try:
            raw = float(c.get("strike", 0))
        except (TypeError, ValueError):
            continue
        strike = raw / 100.0 if raw > 100000 else raw
        if strike <= 0:
            continue
        rows.append({
            "token": str(c.get("token") or c.get("symboltoken") or ""),
            "symbol": c.get("symbol"), "strike": strike, "option_type": opt_type,
            "expiry": c["_expiry_dt"].strftime("%d%b%Y").upper(),
            "dte": (c["_expiry_dt"] - today).days,
        })

    strikes = sorted({r["strike"] for r in rows})
    if not strikes:
        return []
    atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    wanted = set(strikes[max(0, i - depth): i + depth + 1])
    return [r for r in rows if r["strike"] in wanted and r["token"]]


def fetch_quotes(smart_api, exchange, tokens):
    """getMarketData FULL for a batch of tokens -> {token: row}."""
    out = {}
    for start in range(0, len(tokens), MAX_TOKENS_PER_CALL):
        batch = tokens[start:start + MAX_TOKENS_PER_CALL]
        try:
            resp = smart_api.getMarketData(mode="FULL", exchangeTokens={exchange: batch})
        except Exception as e:
            logging.warning("[oi] quote fetch failed for %s: %s", exchange, e)
            continue
        payload = (resp or {}).get("data") if isinstance(resp, dict) else None
        rows = []
        if isinstance(payload, dict):
            fetched = payload.get("fetched")
            rows = fetched if isinstance(fetched, list) else [payload]
        elif isinstance(payload, list):
            rows = payload
        for row in rows:
            if isinstance(row, dict):
                token = str(row.get("symbolToken") or row.get("symboltoken") or "")
                if token:
                    out[token] = row
        time.sleep(0.5)          # pace within a multi-batch snapshot
    return out


def snapshot(conn, smart_api, builders, spots):
    """One pass over every configured index. Returns rows written."""
    from options_chain_builder import DynamicOptionsChainBuilder  # noqa: F401

    stamp, today = ist_stamp(), ist_today()
    written = 0
    for index in ACTIVE_INDICES:
        builder = builders.get(index)
        spot = spots.get(index)
        if builder is None or not spot:
            continue
        contracts = nearest_strikes(builder, spot)
        if not contracts:
            logging.warning("[oi] %s: no strikes resolved", index)
            continue
        exchange = INDICES_CONFIG[index]["option_exchange"]
        quotes = fetch_quotes(smart_api, exchange, [c["token"] for c in contracts])
        rows = []
        for c in contracts:
            q = quotes.get(c["token"])
            if not q:
                continue
            depth = q.get("depth") if isinstance(q.get("depth"), dict) else {}

            def level(side):
                items = depth.get(side) or []
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    try:
                        return float(items[0].get("price"))
                    except (TypeError, ValueError):
                        return None
                return None

            rows.append((
                stamp, today, index, c["expiry"], c["dte"], c["strike"], c["option_type"],
                spot, _first(q, LTP_KEYS), _first(q, OI_KEYS), _first(q, VOL_KEYS),
                level("buy"), level("sell"), json.dumps(q, default=str)[:4000],
            ))
        if rows:
            conn.executemany(
                "INSERT INTO chain_snapshots (captured_at, trade_date, index_name, expiry,"
                " dte, strike, option_type, spot, ltp, open_interest, volume, bid, ask, raw)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            written += len(rows)
            missing_oi = sum(1 for r in rows if r[9] is None)
            if missing_oi:
                logging.warning(
                    "[oi] %s: %d/%d rows had no open-interest field. "
                    "Check the raw column for the real field name.",
                    index, missing_oi, len(rows))
    return written


def latest_spots(smart_api):
    """Index LTP per configured index, via the same API the bot uses."""
    spots = {}
    for index in ACTIVE_INDICES:
        cfg = INDICES_CONFIG[index]
        try:
            resp = smart_api.ltpData(cfg["exchange"], cfg["symbol"], str(cfg["index_token"]))
            if resp and resp.get("status") and resp.get("data"):
                spots[index] = float(resp["data"]["ltp"])
        except Exception as e:
            logging.warning("[oi] spot fetch failed for %s: %s", index, e)
        time.sleep(0.5)
    return spots


def report(db_path=DB_FILE):
    if not os.path.exists(db_path):
        return ["no snapshots yet — run: python3 oi_collector.py"]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT index_name, trade_date, COUNT(*), COUNT(open_interest),"
            " MIN(captured_at), MAX(captured_at) FROM chain_snapshots"
            " GROUP BY index_name, trade_date ORDER BY trade_date DESC, index_name"
        ).fetchall()
        if not rows:
            return ["database exists but holds no snapshots"]
        days = conn.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM chain_snapshots").fetchone()[0]
        out = ["  %-8s %-12s %8s %8s  %s .. %s"
               % ("index", "date", "rows", "with OI", "first", "last")]
        for idx, date, n, n_oi, first, last in rows[:20]:
            out.append("  %-8s %-12s %8d %8d  %s .. %s"
                       % (idx, date, n, n_oi, first[11:16], last[11:16]))
        out.append("")
        out.append("  %d session(s) collected. A first screen needs ~40;" % days)
        out.append("  an out-of-sample split needs roughly double that.")
        return out
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect option-chain snapshots.")
    parser.add_argument("--once", action="store_true", help="one snapshot, then exit")
    parser.add_argument("--report", action="store_true", help="show what is collected")
    parser.add_argument("--interval", type=int, default=INTERVAL_SEC)
    parser.add_argument("--depth", type=int, default=STRIKE_DEPTH)
    parser.add_argument("--db", default=DB_FILE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.report:
        print("\n".join(report(args.db)))
        return 0

    from main import authenticate_broker, load_scrip_master_cache
    from options_chain_builder import DynamicOptionsChainBuilder

    smart_api = authenticate_broker()
    if not smart_api:
        print("Could not authenticate.")
        return 1

    scrip = load_scrip_master_cache()
    builders = {}
    for index in ACTIVE_INDICES:
        b = DynamicOptionsChainBuilder(index_name=index, smart_api=smart_api)
        b.load_scrip_master(scrip)
        builders[index] = b

    conn = connect(args.db)
    try:
        if args.once:
            n = snapshot(conn, smart_api, builders, latest_spots(smart_api))
            print("wrote %d rows" % n)
            print("\n".join(report(args.db)))
            return 0

        logging.info("[oi] collecting every %ds, ATM +/- %d strikes", args.interval, args.depth)
        while True:
            if in_session():
                try:
                    n = snapshot(conn, smart_api, builders, latest_spots(smart_api))
                    logging.info("[oi] snapshot: %d rows", n)
                except Exception as e:
                    logging.error("[oi] snapshot failed: %s", e)
            else:
                logging.info("[oi] outside %d-%d IST; idling",
                             SESSION_START_HHMM, SESSION_END_HHMM)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logging.info("[oi] stopped")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
