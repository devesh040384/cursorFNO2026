"""Paper-session scorecard from the live trades table. PnL uses stored qty."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta

from database import DatabaseManager
from config import FALLBACK_LOT_SIZE


def _qty(row):
    raw = row["qty"] if "qty" in row.keys() else None
    if raw:
        return int(raw)
    symbol = str(row["symbol"] or "").upper()
    if symbol.startswith("SENSEX"):
        return FALLBACK_LOT_SIZE["SENSEX"]
    if "BANKNIFTY" in symbol:
        return FALLBACK_LOT_SIZE["BANKNIFTY"]
    return FALLBACK_LOT_SIZE["NIFTY"]


def trade_pnl(row):
    if row["entry_price"] is None or row["exit_price"] is None:
        return 0.0
    return (float(row["exit_price"]) - float(row["entry_price"])) * _qty(row)


def _parse_dt(value):
    if not value:
        return None
    text = str(value).split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if fmt != "%Y-%m-%d" else text[:10], fmt)
        except ValueError:
            continue
    return None


def _entry_dt(row):
    return _parse_dt(row["entry_time"] if "entry_time" in row.keys() else None) or _parse_dt(
        row["timestamp"] if "timestamp" in row.keys() else None
    )


def summarize_closed(rows):
    pnls = [trade_pnl(r) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    flats = [p for p in pnls if p == 0]
    total = sum(pnls)
    n = len(pnls)
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else (float("inf") if gross_win else 0.0)
    expectancy = (total / n) if n else 0.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    by_index = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for row, pnl in zip(rows, pnls):
        keys = row.keys()
        idx = row["index_name"] if "index_name" in keys and row["index_name"] else "UNKNOWN"
        reason = row["exit_reason"] if "exit_reason" in keys and row["exit_reason"] else "UNKNOWN"
        by_index[idx]["n"] += 1
        by_index[idx]["pnl"] += pnl
        by_reason[reason]["n"] += 1
        by_reason[reason]["pnl"] += pnl

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate": win_rate,
        "total_pnl": total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": max_dd,
        "by_index": dict(by_index),
        "by_reason": dict(by_reason),
    }


def load_trades(db_manager):
    return db_manager.fetch_all(
        """
        SELECT id, symbol, qty, index_name, entry_price, exit_price, status,
               exit_reason, timestamp, entry_time, exit_time
        FROM trades
        ORDER BY id ASC
        """
    )


def split_periods(all_rows, now=None):
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    closed = [r for r in all_rows if str(r["status"] or "").upper().startswith("CLOSED")]
    open_rows = [r for r in all_rows if str(r["status"] or "").upper() == "OPEN"]

    def in_range(row, start_prefix):
        dt = _entry_dt(row)
        if not dt:
            return False
        return dt.strftime("%Y-%m-%d") >= start_prefix

    today_closed = [r for r in closed if in_range(r, today)]
    week_closed = [r for r in closed if in_range(r, week_start)]
    return {
        "today": today_closed,
        "week": week_closed,
        "all": closed,
        "open": open_rows,
    }


def heartbeat_line(db_manager):
    rows = load_trades(db_manager)
    parts = split_periods(rows)
    stats = summarize_closed(parts["today"])
    pf = stats["profit_factor"]
    pf_s = "n/a" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"PnL ₹{stats['total_pnl']:.0f} | WR {stats['win_rate']:.0f}% "
        f"({stats['wins']}/{stats['trades']}) | PF {pf_s} | open {len(parts['open'])}"
    )


def _fmt_pf(value):
    if value == float("inf"):
        return "n/a (no losses)"
    return f"{value:.2f}"


def format_scorecard(db_manager, show_all=False):
    rows = load_trades(db_manager)
    parts = split_periods(rows)
    lines = ["=" * 64, "  PAPER SESSION SCORECARD  (qty * price change)", "=" * 64]
    periods = [("TODAY", parts["today"]), ("LAST 7 DAYS", parts["week"])]
    if show_all:
        periods.append(("ALL CLOSED", parts["all"]))
    for title, closed in periods:
        s = summarize_closed(closed)
        lines.append(f"\n{title}")
        lines.append(
            f"  trades {s['trades']}  wins {s['wins']}  losses {s['losses']}  "
            f"win rate {s['win_rate']:.1f}%"
        )
        lines.append(
            f"  PnL ₹{s['total_pnl']:.2f}  avg win ₹{s['avg_win']:.2f}  "
            f"avg loss ₹{s['avg_loss']:.2f}"
        )
        lines.append(
            f"  profit factor {_fmt_pf(s['profit_factor'])}  "
            f"expectancy ₹{s['expectancy']:.2f}  max DD ₹{s['max_drawdown']:.2f}"
        )
        if s["by_index"]:
            idx = ", ".join(f"{k} n={v['n']} ₹{v['pnl']:.0f}" for k, v in sorted(s["by_index"].items()))
            lines.append(f"  by index: {idx}")
        if s["by_reason"]:
            rs = ", ".join(f"{k} n={v['n']} ₹{v['pnl']:.0f}" for k, v in sorted(s["by_reason"].items()))
            lines.append(f"  by exit: {rs}")
    lines.append(f"\nOPEN now: {len(parts['open'])} (unrealized skipped; no LTP fetch)")
    lines.append("=" * 64)
    return "\n".join(lines)


def print_scorecard(db_path="trade_history.db", show_all=False):
    db = DatabaseManager(db_path)
    print(format_scorecard(db, show_all=show_all))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper session scorecard")
    parser.add_argument("--all", action="store_true", help="Include all-time closed trades")
    parser.add_argument("--db", default="trade_history.db")
    args = parser.parse_args()
    print_scorecard(args.db, show_all=args.all)
