"""Count why entries are rejected, so the funnel can be measured not guessed.

The signal fires ~14 times a session per index, and ~2 trades result. Six out of
seven candidate entries are stopped somewhere between the trigger and the fill,
and until now nothing recorded where. "Raise the daily cap" was never going to
help, because the cap is not what is rejecting them.

Pure observation: a counter increment and nothing else. Importing this changes no
behaviour, and every call site is a place that was already returning False.
"""
import threading
from collections import Counter

_LOCK = threading.Lock()
_COUNTS = Counter()

# Two different denominators, kept apart on purpose. Bar-level gates are counted
# once per closed signal bar; signal-level gates only once a breakout actually
# fired. Mixing them produced a table where stages read "207% of fired", which
# invites exactly the wrong conclusion.
BAR_STAGES = (
    "outside_session",
    "cooldown_active",
    "stale_bars_pause",
)

SIGNAL_STAGES = (
    "signal_fired",
    "direction_mismatch",
    "volume_gate_hook",
    "volume_gate_expansion",
    "same_bar_duplicate",
    "no_contract_for_dte",
    "illiquid_contract",
    "no_ltp",
    "trading_halted",
    "max_open_total",
    "max_open_per_index",
    "daily_cap",
    "per_index_daily_cap",
    "trend_soft_cap",
    "premium_below_min",
    "notional_above_cap",
    "entry_placed",
)

STAGES = BAR_STAGES + SIGNAL_STAGES


def bump(stage, n=1):
    """Record one rejection (or one success). Never raises."""
    try:
        with _LOCK:
            _COUNTS[stage] += n
    except Exception:
        pass


def snapshot():
    with _LOCK:
        return dict(_COUNTS)


def reset():
    with _LOCK:
        _COUNTS.clear()


def funnel(counts=None):
    """Render the funnel. Signal-level stages share one denominator; bar-level
    ones are reported separately because they do not."""
    counts = snapshot() if counts is None else counts
    fired = counts.get("signal_fired", 0)
    placed = counts.get("entry_placed", 0)
    lines = []

    bar_rows = [(st, counts.get(st, 0)) for st in BAR_STAGES if counts.get(st)]
    if bar_rows:
        lines.append("  bar-level gates (counted per closed signal bar)")
        for stage, n in bar_rows:
            lines.append("    %-24s %8d" % (stage, n))
        lines.append("")

    lines.append("  signal-level funnel (% of breakouts that fired)")
    lines.append("    %-24s %8s %8s" % ("stage", "count", "% fired"))
    lines.append("    " + "-" * 42)
    for stage in SIGNAL_STAGES:
        n = counts.get(stage, 0)
        if not n:
            continue
        share = (100.0 * n / fired) if fired else 0.0
        lines.append("    %-24s %8d %7.1f%%" % (stage, n, share))
    if fired:
        lines.append("    " + "-" * 42)
        lines.append("    %-24s %8d %7.1f%%"
                     % ("CONVERSION", placed, 100.0 * placed / fired))
        biggest = max(((s_, counts.get(s_, 0)) for s_ in SIGNAL_STAGES
                       if s_ not in ("signal_fired", "entry_placed")),
                      key=lambda kv: kv[1], default=(None, 0))
        if biggest[0]:
            lines.append("")
            lines.append("    largest single rejector: %s (%d, %.0f%% of fired)"
                         % (biggest[0], biggest[1], 100.0 * biggest[1] / fired))
    return lines
