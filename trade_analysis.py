"""Where did the INDEX go while each trade was open?

The trade rows record what the option did. They do not record what the index
did, so the most important post-mortem question is unanswerable: when a trade
lost, was the signal wrong (index never moved our way) or was the exit wrong
(index moved our way and we still lost, to theta or a tight stop)?

This backfills index open/high/low/close over each trade's holding window from
the candle cache, then classifies every trade on that axis.

Deliberately a SEPARATE, POST-HOC tool rather than live tick tracking:

  * it touches no trading file, so it is safe to run during a frozen config
    window
  * candle OHLC already carries the true intra-bar extremes, so 5-minute bars
    give essentially the same high/low a tick tracker would
  * it works retroactively on trades that are already closed

  python3 trade_analysis.py --backfill        # fill index_* columns
  python3 trade_analysis.py --report --days 7
"""
import argparse
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta

import backtest_data as bd
from config import INDICES_CONFIG, history_token
from database import DatabaseManager
from ist_time import ist_today

DB_FILE = "trade_history.db"

# Added by DatabaseManager's migration path; listed here so --backfill can run
# against a database that has not seen the bot since the migration.
COLUMNS = {
    "index_at_entry": "REAL",
    "index_high": "REAL",
    "index_low": "REAL",
    "index_at_exit": "REAL",
    "index_mfe_pct": "REAL",   # best favourable index move, signed for direction
    "index_mae_pct": "REAL",   # worst adverse index move
    "signal_verdict": "TEXT",  # see classify()
    # Forward excursion: measured from entry over a FIXED window, ignoring when
    # we actually exited. Holding-window MFE cannot distinguish "the signal was
    # wrong" from "we exited before the move arrived" -- a 1.6-minute hold has
    # no chance to show a 0.15% move even if one landed at minute 10.
    "fwd_mfe_15m": "REAL",
    "fwd_mfe_30m": "REAL",
    "fwd_mfe_45m": "REAL",
}

FORWARD_WINDOWS = (15, 30, 45)


def ensure_columns(db_path=DB_FILE):
    conn = sqlite3.connect(db_path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        added = []
        for name, coltype in COLUMNS.items():
            if name not in existing:
                conn.execute("ALTER TABLE trades ADD COLUMN %s %s" % (name, coltype))
                added.append(name)
        conn.commit()
        return added
    finally:
        conn.close()


def _parse(stamp):
    if not stamp:
        return None
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def is_call(symbol):
    return str(symbol or "").upper().endswith("CE")


def window_extremes(bars, start, end):
    """Index O/H/L/C across [start, end], using each bar's own high and low.

    A 5-minute bar's high/low are the true extremes inside it, so this is not a
    close-only approximation — it is what a tick tracker would have seen, minus
    sub-bar ordering.
    """
    if not bars or start is None or end is None:
        return None
    # Include the bar containing `start`: it is the bar the entry happened in.
    window = [b for b in bars if start - timedelta(minutes=5) < b["dt"] <= end + timedelta(minutes=5)]
    if not window:
        return None
    return {
        "open": window[0]["open"],
        "high": max(b["high"] for b in window),
        "low": min(b["low"] for b in window),
        "close": window[-1]["close"],
        "bars": len(window),
    }


def forward_excursion(bars, start, minutes, call):
    """Best favourable index move within `minutes` of entry, however long we held.

    This is the test that separates a dead signal from a stop that is too tight:
    if the move shows up at minute 20 but we were stopped out at minute 4, the
    signal worked and the exit did not.
    """
    if not bars or start is None:
        return None
    end = start + timedelta(minutes=minutes)
    window = [b for b in bars if start - timedelta(minutes=5) < b["dt"] <= end]
    if not window:
        return None
    entry = float(window[0]["open"])
    if entry <= 0:
        return None
    if call:
        return 100.0 * (max(b["high"] for b in window) - entry) / entry
    return 100.0 * (entry - min(b["low"] for b in window)) / entry


def excursions(extremes, call):
    """Favourable/adverse index moves as % of the index at entry.

    Signed by option direction: for a PE, a FALLING index is favourable.
    """
    entry = float(extremes["open"])
    if entry <= 0:
        return 0.0, 0.0
    up = 100.0 * (extremes["high"] - entry) / entry
    down = 100.0 * (extremes["low"] - entry) / entry
    if call:
        return up, down          # favourable = up, adverse = down
    return -down, -up            # for a put the signs invert


def classify(pnl, mfe_pct, mae_pct, favourable_threshold=0.15):
    """Split losses into 'signal was wrong' vs 'exit was wrong'.

    favourable_threshold is in index percent. ~0.15% of NIFTY is ~36 points,
    which on an ATM option is roughly a 20% premium move — comfortably enough
    to have reached the target had it been held.
    """
    moved_our_way = mfe_pct >= favourable_threshold
    if pnl > 0:
        return "WIN" if moved_our_way else "WIN_NO_INDEX_MOVE"
    if moved_our_way:
        return "EXIT_WRONG"      # index went our way, we still lost
    return "SIGNAL_WRONG"        # index never went our way


# Median of a driftless Brownian running maximum is 0.6745 * sigma * sqrt(t).
BROWNIAN_MEDIAN_FACTOR = 0.6745


def _norm_sf(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def random_walk_baseline(sigma_per_min, minutes, threshold=0.15):
    """What a coin flip would produce over the same window.

    Without this, a forward-excursion table reads as encouraging purely because
    a running maximum grows as sqrt(t) for ANY series. The question is never
    "does the move get bigger with more time" — it always does — but "does it
    get bigger than diffusion alone would give".
    """
    scale = sigma_per_min * math.sqrt(minutes)
    if scale <= 0:
        return 0.0, 0.0
    return BROWNIAN_MEDIAN_FACTOR * scale, 100.0 * 2.0 * _norm_sf(threshold / scale)


def implied_sigma_per_min(median_mfe, minutes):
    """Back out per-minute vol from an observed median excursion."""
    if minutes <= 0 or median_mfe <= 0:
        return 0.0
    return median_mfe / (BROWNIAN_MEDIAN_FACTOR * math.sqrt(minutes))


def load_index_bars(symbol, needed_days):
    cfg = INDICES_CONFIG.get(symbol)
    if not cfg:
        return []
    bars = bd.load_series(cfg["exchange"], history_token(symbol))
    if not bars:
        logging.warning("%s: no cached index candles. Run backtest_data.py first.", symbol)
    elif needed_days:
        have = {b["dt"].date() for b in bars}
        missing = sorted(d for d in needed_days if d not in have)
        if missing:
            logging.warning("%s: no candles cached for %s", symbol, ", ".join(map(str, missing[:5])))
    return bars


def backfill(db_path=DB_FILE, only_missing=True):
    added = ensure_columns(db_path)
    if added:
        logging.info("added columns: %s", ", ".join(added))

    db = DatabaseManager(db_path)
    where = "status = 'CLOSED' AND exit_time IS NOT NULL"
    if only_missing:
        where += " AND index_at_entry IS NULL"
    rows = db.fetch_all(
        "SELECT id, symbol, index_name, entry_time, exit_time, entry_price, exit_price, qty"
        " FROM trades WHERE " + where + " ORDER BY id"
    )
    if not rows:
        return 0, 0

    by_index = {}
    for r in rows:
        idx = r["index_name"] or ("SENSEX" if "SENSEX" in str(r["symbol"]).upper() else "NIFTY")
        by_index.setdefault(idx, []).append(r)

    filled = skipped = 0
    conn = sqlite3.connect(db_path)
    try:
        for idx, trades in by_index.items():
            days = {_parse(t["entry_time"]).date() for t in trades if _parse(t["entry_time"])}
            bars = load_index_bars(idx, days)
            if not bars:
                skipped += len(trades)
                continue
            for t in trades:
                start, end = _parse(t["entry_time"]), _parse(t["exit_time"])
                ext = window_extremes(bars, start, end)
                if not ext:
                    skipped += 1
                    continue
                call = is_call(t["symbol"])
                mfe, mae = excursions(ext, call)
                qty = int(t["qty"] or 0)
                pnl = ((t["exit_price"] or 0) - (t["entry_price"] or 0)) * qty
                fwd = [forward_excursion(bars, start, m, call) for m in FORWARD_WINDOWS]
                conn.execute(
                    "UPDATE trades SET index_at_entry=?, index_high=?, index_low=?,"
                    " index_at_exit=?, index_mfe_pct=?, index_mae_pct=?, signal_verdict=?,"
                    " fwd_mfe_15m=?, fwd_mfe_30m=?, fwd_mfe_45m=? WHERE id=?",
                    (ext["open"], ext["high"], ext["low"], ext["close"],
                     mfe, mae, classify(pnl, mfe, mae), fwd[0], fwd[1], fwd[2], t["id"]),
                )
                filled += 1
        conn.commit()
    finally:
        conn.close()
    return filled, skipped


def report(db_path=DB_FILE, days=7):
    since = (datetime.strptime(ist_today(), "%Y-%m-%d") - timedelta(days=days - 1)).date()
    db = DatabaseManager(db_path)
    rows = db.fetch_all(
        "SELECT id, symbol, index_name, entry_reason, exit_reason, entry_price, exit_price,"
        " qty, index_at_entry, index_high, index_low, index_mfe_pct, index_mae_pct,"
        " signal_verdict, entry_time, fwd_mfe_15m, fwd_mfe_30m, fwd_mfe_45m FROM trades"
        " WHERE status='CLOSED' AND index_at_entry IS NOT NULL"
        " AND substr(COALESCE(entry_time, timestamp,''),1,10) >= ? ORDER BY id",
        (str(since),),
    )
    if not rows:
        return ["no enriched trades in the window — run --backfill first"]

    lines = ["=" * 72, "  INDEX EXCURSION  (last %d days)" % days, "=" * 72]
    buckets = {}
    for r in rows:
        pnl = ((r["exit_price"] or 0) - (r["entry_price"] or 0)) * int(r["qty"] or 0)
        b = buckets.setdefault(r["signal_verdict"], [0, 0.0])
        b[0] += 1
        b[1] += pnl
    total = sum(v[0] for v in buckets.values())
    lines.append("  verdict split (n=%d):" % total)
    for verdict in ("WIN", "WIN_NO_INDEX_MOVE", "EXIT_WRONG", "SIGNAL_WRONG"):
        if verdict in buckets:
            n, pnl = buckets[verdict]
            lines.append("    %-20s n=%-3d %5.0f%%  INR %+8.0f" % (verdict, n, 100.0 * n / total, pnl))

    lines.append("")
    lines.append("  %-5s %-8s %-16s %-9s %8s %8s %8s" %
                 ("id", "index", "entry", "exit", "pnl", "idx MFE", "idx MAE"))
    for r in rows:
        pnl = ((r["exit_price"] or 0) - (r["entry_price"] or 0)) * int(r["qty"] or 0)
        lines.append("  %-5d %-8s %-16s %-9s %8.0f %7.2f%% %7.2f%%" % (
            r["id"], r["index_name"] or "?", r["entry_reason"] or "?",
            (r["exit_reason"] or "?")[:9], pnl,
            r["index_mfe_pct"] or 0.0, r["index_mae_pct"] or 0.0))

    have_fwd = [r for r in rows if r["fwd_mfe_30m"] is not None]
    if have_fwd:
        lines += ["", "  FORWARD INDEX MOVE FROM ENTRY (ignores when we exited)"]
        held_key = "index_mfe_pct"
        lines.append("    %-14s %s" % ("window", "share of trades reaching a tradeable move"))
        for label, key in (("held only", held_key), ("15 min", "fwd_mfe_15m"),
                           ("30 min", "fwd_mfe_30m"), ("45 min", "fwd_mfe_45m")):
            vals = [r[key] for r in have_fwd if r[key] is not None]
            if not vals:
                continue
            reach = 100.0 * sum(1 for v in vals if v >= 0.15) / len(vals)
            lines.append("    %-14s %5.1f%%  (median %.3f%%)"
                         % (label, reach, sorted(vals)[len(vals) // 2]))
        anchor_med = None
        vals15 = sorted(v for v in (r["fwd_mfe_15m"] for r in have_fwd) if v is not None)
        if vals15:
            anchor_med = vals15[len(vals15) // 2]
        sigma = implied_sigma_per_min(anchor_med, 15) if anchor_med else 0.0
        if sigma > 0:
            lines += ["", "    vs a RANDOM WALK calibrated to the 15-min point",
                      "    (a running maximum grows as sqrt(t) for ANY series, so a rising",
                      "     column proves nothing on its own)", "",
                      "    %-9s %10s %10s %9s %9s" % ("window", "med obs", "med rand", "reach", "rand")]
            beat = False
            for label, key, minutes in (("15 min", "fwd_mfe_15m", 15),
                                        ("30 min", "fwd_mfe_30m", 30),
                                        ("45 min", "fwd_mfe_45m", 45)):
                vals = sorted(v for v in (r[key] for r in have_fwd) if v is not None)
                if not vals:
                    continue
                med = vals[len(vals) // 2]
                reach = 100.0 * sum(1 for v in vals if v >= 0.15) / len(vals)
                med_r, reach_r = random_walk_baseline(sigma, minutes)
                beat = beat or reach > reach_r
                lines.append("    %-9s %9.3f%% %9.3f%% %8.1f%% %8.1f%%"
                             % (label, med, med_r, reach, reach_r))
            lines.append("")
            if beat:
                lines.append("    Beats the baseline somewhere — worth investigating.")
            else:
                lines += ["    Does NOT beat a random walk at any window. The growth across",
                          "    columns is diffusion, not prediction: these entries are no",
                          "    better timed than picking a moment at random."]

    losses = [r for r in rows if ((r["exit_price"] or 0) - (r["entry_price"] or 0)) < 0]
    if losses:
        exit_wrong = [r for r in losses if r["signal_verdict"] == "EXIT_WRONG"]
        pct = 100.0 * len(exit_wrong) / len(losses)
        lines += ["", "  Of %d losing trades, %d had the index move our way first (%.0f%%)."
                  % (len(losses), len(exit_wrong), pct)]
        # This asserted "exit problems" unconditionally, including when the count
        # was zero — which stated the exact opposite of the finding.
        if pct >= 50:
            lines.append("  Most losses are EXIT problems: the move was there and we lost anyway.")
        elif len(exit_wrong):
            lines.append("  A minority are exit problems; the rest never had a move to catch.")
        else:
            lines.append("  None are exit problems: no losing trade ever had a move to catch.")
    lines.append("=" * 72)
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index excursion analysis for closed trades.")
    parser.add_argument("--backfill", action="store_true", help="fill index_* columns")
    parser.add_argument("--all", action="store_true", help="re-fill trades already enriched")
    parser.add_argument("--report", action="store_true", help="print the analysis")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--db", default=DB_FILE)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not os.path.exists(args.db):
        print("Database '%s' not found." % args.db)
        return 1

    if args.backfill or not args.report:
        filled, skipped = backfill(args.db, only_missing=not args.all)
        print("enriched %d trade(s), skipped %d (no candles cached)" % (filled, skipped))
    if args.report or not args.backfill:
        print("\n".join(report(args.db, args.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
