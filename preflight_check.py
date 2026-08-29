"""Go-live readiness gate. Run before every session, and before Sep 15 live.

The previous version could never pass: it required an env var named TRADING_PIN
that nothing reads, and queried a table called `trade_history` when the table is
`trades`. It also checked nothing that actually matters for live trading.

  python3 preflight_check.py            # paper-mode checks
  python3 preflight_check.py --live     # adds the live-only gates
  python3 preflight_check.py --capital 75000

Exit code 0 = ready, 1 = blocked.
"""
import argparse
import os
import sqlite3
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from config import (
    ACTIVE_INDICES,
    INDICES_CONFIG,
    PAPER_TRADING,
    RISK,
    daily_entry_cap,
    index_daily_entry_cap,
)
from ist_time import ist_now, ist_today

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
DB_FILE = "trade_history.db"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, check, detail):
        self.rows.append((level, check, detail))

    def ok(self, check, detail=""):
        self.add(PASS, check, detail)

    def warn(self, check, detail=""):
        self.add(WARN, check, detail)

    def fail(self, check, detail=""):
        self.add(FAIL, check, detail)

    @property
    def failures(self):
        return [r for r in self.rows if r[0] == FAIL]

    @property
    def warnings(self):
        return [r for r in self.rows if r[0] == WARN]

    def render(self):
        width = max(len(c) for _, c, _ in self.rows) + 2
        icons = {PASS: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
        print("=" * 78)
        print(f"  PRE-FLIGHT / GO-LIVE READINESS  ({ist_now():%Y-%m-%d %H:%M:%S} IST)")
        print("=" * 78)
        for level, check, detail in self.rows:
            print(f"{icons[level]} {check.ljust(width)} {detail}")
        print("=" * 78)
        if self.failures:
            print(f"  BLOCKED: {len(self.failures)} failure(s). Do not start.")
        elif self.warnings:
            print(f"  READY, with {len(self.warnings)} warning(s) to review.")
        else:
            print("  READY.")
        print("=" * 78)


def check_credentials(rep):
    """Names must match what main.py actually reads."""
    api_key = os.getenv("SMART_API_KEY") or os.getenv("SMARTAPI_KEY")
    pwd = os.getenv("PASSWORD") or os.getenv("PIN")
    missing = []
    if not api_key:
        missing.append("SMART_API_KEY (or SMARTAPI_KEY)")
    if not os.getenv("CLIENT_ID"):
        missing.append("CLIENT_ID")
    if not pwd:
        missing.append("PASSWORD (or PIN)")
    if not os.getenv("TOTP_SECRET"):
        missing.append("TOTP_SECRET")
    if missing:
        rep.fail("credentials", "missing: " + ", ".join(missing))
    else:
        rep.ok("credentials", "all four present in env/.env")


def check_imports(rep):
    """Import the live path; a syntax/import error must not surface at 09:15."""
    modules = [
        "config", "ist_time", "database", "indicators", "strategy_brain",
        "options_chain_builder", "order_execution", "risk_manager",
        "risk_monitors", "scorecard", "history_seeder", "broker_orders",
        "broker_health",
    ]
    broken = []
    for name in modules:
        try:
            __import__(name)
        except Exception as e:
            broken.append(f"{name}: {e}")
    if broken:
        rep.fail("module imports", "; ".join(broken))
    else:
        rep.ok("module imports", f"{len(modules)} modules import cleanly")


def check_database(rep):
    if not os.path.exists(DB_FILE):
        rep.warn("database", f"{DB_FILE} not found (will be created on first run)")
        return
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        required = {
            "qty", "index_name", "entry_reason", "entry_time",
            "expiry", "dte", "max_favorable_price",
            "intended_price", "slippage", "entry_spread_pct",
        }
        missing = required - cols
        if missing:
            rep.fail("db schema", "missing columns: " + ", ".join(sorted(missing)))
        else:
            rep.ok("db schema", f"{len(cols)} columns, all required present")

        stale = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'OPEN' "
            "AND substr(COALESCE(entry_time, timestamp, ''), 1, 10) < ?",
            (ist_today(),),
        ).fetchone()[0]
        if stale:
            rep.fail(
                "stale open trades",
                f"{stale} OPEN row(s) from a previous day - square off or close them first",
            )
        else:
            rep.ok("stale open trades", "none")
        conn.close()
    except Exception as e:
        rep.fail("database", str(e))


def check_scrip_master(rep):
    for name in ("scrip_master.json", "OpenAPIScripMaster.json"):
        if os.path.exists(name) and os.path.getsize(name) > 1_000_000:
            rep.ok("scrip master", f"{name} ({os.path.getsize(name) // 1_048_576} MB)")
            return
    rep.fail("scrip master", "no usable scrip_master.json - contracts cannot resolve")


def check_risk_config(rep, capital=None):
    """Internal consistency of the knobs, independent of any account."""
    reward = {}
    for symbol in ACTIVE_INDICES:
        cfg = INDICES_CONFIG.get(symbol, {})
        rr = (cfg.get("trending_target_mult", 1) - 1) / max(
            1e-9, 1 - cfg.get("trending_sl_mult", 1)
        )
        reward[symbol] = rr
    worst = min(reward.values()) if reward else 0
    if worst < 2.0:
        rep.warn("risk:reward", f"lowest R:R is {worst:.1f}:1")
    else:
        rep.ok("risk:reward", f"{worst:.1f}:1 across {', '.join(ACTIVE_INDICES)}")

    top_tier = max(float(t["at"]) for t in RISK["trail_tiers"])
    min_target = min(
        INDICES_CONFIG[s].get("trending_target_mult", 1) for s in ACTIVE_INDICES
    )
    if top_tier >= min_target:
        rep.fail("trail vs target", f"top tier {top_tier} >= target {min_target} (dead tier)")
    else:
        rep.ok("trail ladder", f"{len(RISK['trail_tiers'])} tiers, top {top_tier} < target {min_target}")

    spread = RISK.get("max_option_spread_pct", 3.0)
    if spread > 2.0:
        rep.warn(
            "spread cap",
            f"{spread}% - a MARKET round trip costs ~{spread * 2:.0f}% of notional",
        )
    else:
        rep.ok("spread cap", f"{spread}%")

    if RISK["session_start_hhmm"] >= RISK["entry_cutoff_hhmm"]:
        rep.fail("session window", "start is not before cutoff")
    elif RISK["entry_cutoff_hhmm"] >= RISK["eod_squareoff_hhmm"]:
        rep.fail("session window", "cutoff is not before EOD square-off")
    else:
        rep.ok(
            "session window",
            f"{RISK['session_start_hhmm']} entries, {RISK['entry_cutoff_hhmm']} cutoff, "
            f"{RISK['eod_squareoff_hhmm']} EOD (IST)",
        )

    peak = RISK["max_premium_risk_inr"] * RISK["max_open_total"]
    if capital:
        loss_pct = 100.0 * RISK["max_daily_loss_inr"] / capital
        detail = (
            f"daily loss cap Rs{RISK['max_daily_loss_inr']:.0f} = {loss_pct:.1f}% "
            f"of Rs{capital:,.0f}; peak deployment Rs{peak:,.0f}"
        )
        if peak > capital:
            rep.fail("capital", f"peak deployment Rs{peak:,.0f} exceeds capital Rs{capital:,.0f}")
        elif loss_pct > 3.0:
            rep.fail("capital", detail + " - above 3%/day is not survivable")
        elif loss_pct > 2.0:
            rep.warn("capital", detail + " - aggressive")
        else:
            rep.ok("capital", detail)
    else:
        rep.warn(
            "capital",
            f"pass --capital to validate; peak deployment Rs{peak:,.0f}, "
            f"daily loss cap Rs{RISK['max_daily_loss_inr']:.0f}",
        )


def check_live_gates(rep):
    """Only meaningful once PAPER_TRADING is False."""
    if PAPER_TRADING:
        rep.ok("mode", "PAPER (no live orders will be placed)")
        return
    rep.warn("mode", "LIVE - real orders will be placed")

    cap = daily_entry_cap()
    if cap > 2:
        rep.warn(
            "live entry cap",
            f"{cap}/day. First live week should be 1/day to exercise the order path cheaply.",
        )
    else:
        rep.ok("live entry cap", f"{cap}/day, {index_daily_entry_cap()}/index")

    if os.getenv("ALERT_WEBHOOK_URL"):
        rep.ok("alerting", "ALERT_WEBHOOK_URL set; CRITICAL events will be pushed")
    else:
        rep.fail(
            "alerting",
            "ALERT_WEBHOOK_URL unset - an unattended live box would fail silently",
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="Go-live readiness gate.")
    parser.add_argument("--live", action="store_true", help="run live-only gates too")
    parser.add_argument("--capital", type=float, help="account capital in INR, to size-check risk")
    args = parser.parse_args(argv)

    rep = Report()
    check_credentials(rep)
    check_imports(rep)
    check_database(rep)
    check_scrip_master(rep)
    check_risk_config(rep, capital=args.capital)
    if args.live or not PAPER_TRADING:
        check_live_gates(rep)
    else:
        rep.ok("mode", "PAPER (no live orders will be placed)")
    rep.render()
    return 1 if rep.failures else 0


if __name__ == "__main__":
    sys.exit(main())
