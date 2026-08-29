"""Browse completed trades over any date range.

Was hardcoded to today, used host-local time (so a UTC box showed the wrong
day), and `SELECT *` now dumps 20+ columns unreadably. Defaults to the last 7
IST days; every window is selectable.

  python3 view_completed.py                     # last 7 days
  python3 view_completed.py --days 30           # last 30 days
  python3 view_completed.py --all               # everything
  python3 view_completed.py --date 2026-08-25   # one day
  python3 view_completed.py --since 2026-08-21 --until 2026-08-28
  python3 view_completed.py --index NIFTY --reason TARGET_HIT
  python3 view_completed.py --open              # OPEN rows instead
  python3 view_completed.py --full              # every column
  python3 view_completed.py --csv               # CSV to stdout
"""
import argparse
import csv
import os
import sqlite3
import sys
from datetime import timedelta

from ist_time import ist_now, ist_today

DB_FILE = "trade_history.db"

# Readable default; --full shows everything.
DEFAULT_COLUMNS = [
    "id", "entry_time", "exit_time", "symbol", "index_name", "qty",
    "entry_price", "exit_price", "pnl", "entry_reason", "exit_reason",
    "dte", "slippage", "entry_spread_pct",
]
FALLBACK_LOTS = {"SENSEX": 20, "BANKNIFTY": 30, "NIFTY": 65}


def _qty(row):
    if row["qty"]:
        return int(row["qty"])
    sym = str(row["symbol"] or "").upper()
    for key in ("SENSEX", "BANKNIFTY"):
        if key in sym:
            return FALLBACK_LOTS[key]
    return FALLBACK_LOTS["NIFTY"]


def _pnl(row):
    if row["entry_price"] is None or row["exit_price"] is None:
        return None
    return (float(row["exit_price"]) - float(row["entry_price"])) * _qty(row)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="View completed trades over a date range.")
    win = p.add_mutually_exclusive_group()
    win.add_argument("--days", type=int, metavar="N", help="last N IST days (default 7)")
    win.add_argument("--all", action="store_true", help="no date filter")
    win.add_argument("--date", metavar="YYYY-MM-DD", help="a single day")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="start date (inclusive)")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="end date (inclusive)")
    p.add_argument("--index", help="filter by index_name, e.g. NIFTY")
    p.add_argument("--reason", help="filter by exit_reason, e.g. TARGET_HIT")
    p.add_argument("--entry-reason", dest="entry_reason", help="filter by entry_reason")
    p.add_argument("--open", action="store_true", help="show OPEN trades instead of completed")
    p.add_argument("--full", action="store_true", help="show every column")
    p.add_argument("--limit", type=int, help="max rows (most recent first)")
    p.add_argument("--csv", action="store_true", help="emit CSV instead of a table")
    p.add_argument("--db", default=DB_FILE, help="database path (default %s)" % DB_FILE)
    return p.parse_args(argv)


def build_query(args):
    """Return (where_sql, params, window_label). Dates are IST."""
    where, params = [], []
    where.append("status = 'OPEN'" if args.open else "status != 'OPEN'")

    since = until = None
    if args.all:
        label = "all time"
    elif args.date:
        since = until = args.date
        label = args.date
    elif args.since or args.until:
        since = args.since
        until = args.until
        label = "%s to %s" % (since or "start", until or "today")
    else:
        days = args.days if args.days else 7
        since = (ist_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        until = ist_today()
        label = "last %d day(s): %s to %s" % (days, since, until)

    # entry_time is the reliable column; timestamp is the legacy fallback.
    stamp = "COALESCE(entry_time, timestamp, '')"
    if since:
        where.append("substr(%s, 1, 10) >= ?" % stamp)
        params.append(since)
    if until:
        where.append("substr(%s, 1, 10) <= ?" % stamp)
        params.append(until)
    if args.index:
        where.append("(index_name = ? OR (index_name IS NULL AND UPPER(symbol) LIKE ?))")
        params.extend([args.index, args.index.upper() + "%"])
    if args.reason:
        where.append("UPPER(COALESCE(exit_reason, '')) = ?")
        params.append(args.reason.upper())
    if args.entry_reason:
        where.append("UPPER(COALESCE(entry_reason, '')) = ?")
        params.append(args.entry_reason.upper())
    return " AND ".join(where), params, label


def fetch(conn, args):
    where, params, label = build_query(args)
    sql = (
        "SELECT * FROM trades WHERE " + where
        + " ORDER BY COALESCE(entry_time, timestamp, '') DESC, id DESC"
    )
    if args.limit:
        sql += " LIMIT ?"
        params = params + [int(args.limit)]
    return conn.execute(sql, params).fetchall(), label


def pick_columns(rows, args):
    available = rows[0].keys()
    if args.full:
        return list(available) + ["pnl"]
    return [c for c in DEFAULT_COLUMNS if c == "pnl" or c in available]


def cell(row, col):
    if col == "pnl":
        value = _pnl(row)
        return "-" if value is None else "%.2f" % value
    value = row[col]
    if value is None:
        return "-"
    if isinstance(value, float):
        return "%.2f" % value
    return str(value)


def render(rows, columns, label, args):
    table = [[cell(r, c) for c in columns] for r in rows]
    widths = []
    for i, name in enumerate(columns):
        widths.append(max([len(name)] + [len(r[i]) for r in table]))
    header = " | ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    kind = "OPEN" if args.open else "COMPLETED"
    print("\n" + "=" * len(header))
    print(" %s TRADES (%s) in '%s' - Total: %d" % (kind, label, args.db, len(rows)))
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for line in table:
        print(" | ".join(v.ljust(widths[i]) for i, v in enumerate(line)))
    print("=" * len(header))

    pnls = [p for p in (_pnl(r) for r in rows) if p is not None]
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        rate = 100.0 * len(wins) / len(pnls)
        print(" net INR %.2f | %dW/%dL (%.1f%%) | best %.2f | worst %.2f" % (
            sum(pnls), len(wins), len(losses), rate, max(pnls), min(pnls)))
        by_day = {}
        for row in rows:
            pnl = _pnl(row)
            if pnl is None:
                continue
            day = str(row["entry_time"] or row["timestamp"] or "")[:10]
            agg = by_day.setdefault(day, [0, 0.0])
            agg[0] += 1
            agg[1] += pnl
        if len(by_day) > 1:
            print(" by day: " + ", ".join(
                "%s n=%d INR %.0f" % (d, v[0], v[1]) for d, v in sorted(by_day.items())))
    print()


def main(argv=None):
    args = parse_args(argv)
    if not os.path.exists(args.db):
        print("Database '%s' not found." % args.db)
        return 1
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        rows, label = fetch(conn, args)
        if not rows:
            kind = "OPEN" if args.open else "completed"
            print("\nNo %s trades found for %s in '%s'.\n" % (kind, label, args.db))
            return 0
        columns = pick_columns(rows, args)
        if args.csv:
            writer = csv.writer(sys.stdout, lineterminator="\n")
            writer.writerow(columns)
            for row in rows:
                writer.writerow([cell(row, c) for c in columns])
            return 0
        render(rows, columns, label, args)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
