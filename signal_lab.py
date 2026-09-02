"""Screen signal hypotheses against cached index data. No options, no costs.

A full backtest answers "does this configuration make money", which mixes signal
quality with option pricing, costs, stops and caps. When the answer is no, you
cannot tell which part failed — that is what happened to VOLUME_BREAKOUT.

This asks the prior question: **does the signal predict index movement at all?**
It reads only index and futures candles. If a signal cannot beat its own
unconditional baseline here, no amount of stop or target tuning will rescue it,
and there is no point spending a backtest on it.

Method
------
For every trigger the signal produces, measure the SIGNED forward index return
in the direction the signal called, over 15/30/45 minutes. Under no edge that
mean is zero. The test is a one-sample t-test against zero, plus a comparison
against the unconditional distribution over the same bars.

Multiple testing is handled explicitly: screening N hypotheses guarantees some
will look good by chance, so the Bonferroni threshold is printed next to the raw
one and the verdict uses the corrected bar.

  python3 signal_lab.py                       # screen every signal
  python3 signal_lab.py --signals orb nr7     # just these
  python3 signal_lab.py --index NIFTY --windows 15 30 60 90
"""
import argparse
import logging
import math
import statistics

import backtest_data as bd
from config import INDICES_CONFIG, history_token

DEFAULT_WINDOWS = (15, 30, 45)
BAR_MINUTES = 5
# The move an ATM option needs to clear costs and reach a worthwhile target.
TRADEABLE_PCT = 0.15
SESSION_START_MIN = 9 * 60 + 45     # entries begin 09:45, as live
SESSION_END_MIN = 14 * 60 + 30      # and stop at 14:30


# --------------------------------------------------------------------- helpers

def _minute_of_day(bar):
    return bar["dt"].hour * 60 + bar["dt"].minute


def _in_session(bar):
    return SESSION_START_MIN <= _minute_of_day(bar) < SESSION_END_MIN


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = (v - ema) * k + ema
    return ema


def _norm_sf(z):
    return 0.5 * math.erfc(z / math.sqrt(2.0))


# ------------------------------------------------------------------- economics
# A signal is only useful if its edge clears the cost of the instrument used to
# express it. The same index move that is worthless in ATM options can be
# comfortably profitable in futures, because the hurdle differs by ~15x.

INSTRUMENTS = {
    # cost_pct is the round-trip cost as a percentage of NOTIONAL.
    # leverage converts an index % move into an instrument % move.
    "option": {
        # 0.21% observed spread crossed twice, plus ~1.02% brokerage/STT/GST on a
        # ~Rs6,000 notional where the flat Rs40 dominates.
        "cost_pct": 1.44,
        "leverage": 116.0,      # measured on ATM 1-DTE: +0.5% index -> +58% premium
        "theta_pct_per_hour": 4.1,
        "lot_margin": {"NIFTY": 6500.0, "SENSEX": 6000.0},
        "label": "ATM option (buy)",
    },
    "future": {
        # Measured on one NIFTY lot (notional ~Rs15.6L): Rs40 brokerage + Rs312
        # STT + Rs59 exchange + GST + stamp = ~Rs460 = 0.0295%. Spread is a tick
        # or two on top. No decay.
        "cost_pct": 0.035,
        "leverage": 1.0,
        "theta_pct_per_hour": 0.0,
        "lot_margin": {"NIFTY": 175000.0, "SENSEX": 160000.0},
        "label": "index future",
    },
}


def net_edge_pct(index_move_pct, instrument="option", hold_minutes=0.0):
    """Instrument-level return after costs, for a given index move.

    Returns (gross %, cost %, net %) of the instrument's own notional.
    """
    spec = INSTRUMENTS[instrument]
    gross = index_move_pct * spec["leverage"]
    cost = spec["cost_pct"] + spec["theta_pct_per_hour"] * (hold_minutes / 60.0)
    return gross, cost, gross - cost


def required_index_move_pct(instrument="option", hold_minutes=0.0):
    """Smallest index move that breaks even on this instrument."""
    spec = INSTRUMENTS[instrument]
    cost = spec["cost_pct"] + spec["theta_pct_per_hour"] * (hold_minutes / 60.0)
    return cost / spec["leverage"]


# --------------------------------------------------------------------- signals
# Each takes (bars, i, fut) and returns "CE", "PE" or None for the bar at i.
# `fut` is the aligned futures series (for volume), or None.

def sig_volume_breakout(bars, i, fut):
    """The current live signal, reproduced. Included as the control."""
    if fut is None or i < 9:
        return None
    vols = [b["volume"] for b in fut[i - 8:i + 1]]
    if len(vols) < 9 or any(v <= 0 for v in vols[:-1]):
        return None
    avg = sum(vols[:-1]) / len(vols[:-1])
    if avg <= 0 or vols[-1] < 1.2 * avg:
        return None
    return "CE" if bars[i]["close"] > bars[i - 1]["close"] else "PE"


def sig_opening_range_break(bars, i, fut):
    """Break of the first 60 minutes' range — a classic that this bot never tries."""
    day = bars[i]["dt"].date()
    opening = [b for b in bars[max(0, i - 80):i]
               if b["dt"].date() == day and _minute_of_day(b) < 9 * 60 + 15 + 60]
    if len(opening) < 10:
        return None
    hi = max(b["high"] for b in opening)
    lo = min(b["low"] for b in opening)
    close = bars[i]["close"]
    if close > hi:
        return "CE"
    if close < lo:
        return "PE"
    return None


def sig_mean_reversion(bars, i, fut):
    """Fade a sharp move — the opposite premise to the current strategy."""
    if i < 3:
        return None
    move = (bars[i]["close"] - bars[i - 3]["close"]) / bars[i - 3]["close"]
    if move > 0.0025:
        return "PE"
    if move < -0.0025:
        return "CE"
    return None


def sig_range_contraction(bars, i, fut):
    """NR7-style: the narrowest range in 7 bars, then a break of it.

    Volatility clusters, so compression often precedes expansion. Tests whether
    the bot is entering on expansion that has already happened.
    """
    if i < 8:
        return None
    ranges = [b["high"] - b["low"] for b in bars[i - 7:i]]
    if not ranges or min(ranges) <= 0:
        return None
    narrow = bars[i - 1]
    if (narrow["high"] - narrow["low"]) > min(ranges):
        return None
    if bars[i]["close"] > narrow["high"]:
        return "CE"
    if bars[i]["close"] < narrow["low"]:
        return "PE"
    return None


def sig_ema_trend(bars, i, fut):
    """Pure trend alignment on a slower view: EMA9 vs EMA21 with separation."""
    if i < 30:
        return None
    closes = [b["close"] for b in bars[i - 29:i + 1]]
    fast, slow = _ema(closes, 9), _ema(closes, 21)
    if fast is None or slow is None or slow <= 0:
        return None
    spread = (fast - slow) / slow
    if spread > 0.0006:
        return "CE"
    if spread < -0.0006:
        return "PE"
    return None


def sig_first_hour_momentum(bars, i, fut):
    """Direction of the first hour, traded for the rest of the session."""
    day = bars[i]["dt"].date()
    if _minute_of_day(bars[i]) < 10 * 60 + 15:
        return None
    opening = [b for b in bars[max(0, i - 80):i]
               if b["dt"].date() == day and _minute_of_day(b) < 10 * 60 + 15]
    if len(opening) < 10:
        return None
    change = opening[-1]["close"] - opening[0]["open"]
    if opening[0]["open"] <= 0:
        return None
    pct = change / opening[0]["open"]
    if pct > 0.002:
        return "CE"
    if pct < -0.002:
        return "PE"
    return None


SIGNALS = {
    "volume_breakout": sig_volume_breakout,
    "orb": sig_opening_range_break,
    "mean_reversion": sig_mean_reversion,
    "nr7": sig_range_contraction,
    "ema_trend": sig_ema_trend,
    "first_hour": sig_first_hour_momentum,
}


# ------------------------------------------------------------------ evaluation

def forward_return(bars, i, minutes, direction):
    """Signed index return over `minutes`, in the direction the signal called."""
    steps = max(1, minutes // BAR_MINUTES)
    j = i + steps
    if j >= len(bars):
        return None
    if bars[j]["dt"].date() != bars[i]["dt"].date():
        return None                       # never measure across a session boundary
    entry = bars[i]["close"]
    if entry <= 0:
        return None
    pct = 100.0 * (bars[j]["close"] - entry) / entry
    return pct if direction == "CE" else -pct


def forward_mfe(bars, i, minutes, direction):
    """Best favourable excursion — what a target would have captured."""
    steps = max(1, minutes // BAR_MINUTES)
    window = bars[i + 1:i + 1 + steps]
    window = [b for b in window if b["dt"].date() == bars[i]["dt"].date()]
    if not window:
        return None
    entry = bars[i]["close"]
    if entry <= 0:
        return None
    if direction == "CE":
        return 100.0 * (max(b["high"] for b in window) - entry) / entry
    return 100.0 * (entry - min(b["low"] for b in window)) / entry


def t_stat(values):
    if len(values) < 2:
        return 0.0
    sd = statistics.stdev(values)
    if sd <= 0:
        return 0.0
    return statistics.mean(values) / (sd / math.sqrt(len(values)))


def evaluate(bars, fut, fn, windows):
    """Run one signal over the whole series. Returns per-window statistics."""
    triggers = []
    for i in range(1, len(bars) - 1):
        if not _in_session(bars[i]):
            continue
        direction = fn(bars, i, fut)
        if direction:
            triggers.append((i, direction))

    out = {"n": len(triggers), "windows": {}}
    for w in windows:
        rets, mfes = [], []
        for i, direction in triggers:
            r = forward_return(bars, i, w, direction)
            m = forward_mfe(bars, i, w, direction)
            if r is not None:
                rets.append(r)
            if m is not None:
                mfes.append(m)
        if not rets:
            continue
        out["windows"][w] = {
            "n": len(rets),
            "mean": statistics.mean(rets),
            "t": t_stat(rets),
            "reach": 100.0 * sum(1 for m in mfes if m >= TRADEABLE_PCT) / len(mfes) if mfes else 0.0,
            "median_mfe": statistics.median(mfes) if mfes else 0.0,
        }
    return out


def unconditional(bars, windows):
    """Baseline: every in-session bar, direction chosen by the bar's own sign.

    This is the honest comparison — the same index, same hours, no selection.
    A signal that cannot beat it is selecting bars at random.
    """
    triggers = [(i, "CE" if bars[i]["close"] >= bars[i - 1]["close"] else "PE")
                for i in range(1, len(bars) - 1) if _in_session(bars[i])]
    out = {}
    for w in windows:
        rets = [forward_return(bars, i, w, d) for i, d in triggers]
        rets = [r for r in rets if r is not None]
        if rets:
            out[w] = {"n": len(rets), "mean": statistics.mean(rets), "t": t_stat(rets)}
    return out


def slice_sessions(bars, since=None, until=None, first_fraction=None, last_n=None):
    """Restrict bars to a date range or a share of the sessions.

    Out-of-sample validation is the only defence against data mining. A finding
    discovered on the full history has to survive on periods it was not found
    on, or it is a coincidence dressed as an edge.
    """
    if not bars:
        return bars
    days = sorted({b["dt"].date() for b in bars})
    if first_fraction is not None:
        cut = max(1, int(len(days) * first_fraction))
        keep = set(days[:cut]) if first_fraction > 0 else set()
        if first_fraction < 0:                       # negative means the tail
            keep = set(days[int(len(days) * (1 + first_fraction)):])
        return [b for b in bars if b["dt"].date() in keep]
    if last_n is not None:
        keep = set(days[-last_n:])
        return [b for b in bars if b["dt"].date() in keep]
    out = bars
    if since:
        out = [b for b in out if str(b["dt"].date()) >= since]
    if until:
        out = [b for b in out if str(b["dt"].date()) <= until]
    return out


def stability_t(mean_a, t_a, mean_b, t_b):
    """Are two periods' means significantly different from each other?

    A signal can be significant in both halves and still be unstable, or -- as
    with first_hour -- significant in one half and absent in the other. Reading
    each period in isolation misses that; this is the test that catches it.
    """
    if not t_a or not t_b:
        return 0.0
    se_a, se_b = abs(mean_a / t_a), abs(mean_b / t_b)
    denom = math.sqrt(se_a * se_a + se_b * se_b)
    return (mean_b - mean_a) / denom if denom else 0.0


def validate(symbol, name, windows, folds=None):
    """Run one signal across disjoint periods and report each separately."""
    bars, fut = load(symbol)
    if not bars:
        return []
    days = sorted({b["dt"].date() for b in bars})
    folds = folds or [
        ("first half", dict(first_fraction=0.5)),
        ("second half", dict(first_fraction=-0.5)),
        ("last 60 sessions", dict(last_n=60)),
        ("last 20 sessions", dict(last_n=20)),
    ]
    rows = []
    for label, kwargs in folds:
        subset = slice_sessions(bars, **kwargs)
        if not subset:
            continue
        sub_fut = None
        if fut is not None:
            by_dt = {b["dt"]: f for b, f in zip(bars, fut)}
            sub_fut = [by_dt.get(b["dt"], {"volume": 0.0}) for b in subset]
        res = evaluate(subset, sub_fut, SIGNALS[name], windows)
        sdays = sorted({b["dt"].date() for b in subset})
        rows.append((label, len(sdays), sdays[0], sdays[-1], res))
    return rows


def load(symbol):
    cfg = INDICES_CONFIG[symbol]
    bars = bd.load_series(cfg["exchange"], history_token(symbol))
    fut = None
    for name in sorted(__import__("os").listdir(bd.CACHE_DIR)):
        if not name.startswith(cfg["option_exchange"] + "_") or not name.endswith("FIVE_MINUTE.csv"):
            continue
        token = name[:-4].split("_")[1]
        fut = bd.load_series(cfg["option_exchange"], token)
        break
    if fut is not None and len(fut) != len(bars):
        # Align futures to index timestamps so index i means the same bar in both.
        by_dt = {b["dt"]: b for b in fut}
        fut = [by_dt.get(b["dt"], {"volume": 0.0}) for b in bars]
    return bars, fut


def main(argv=None):
    parser = argparse.ArgumentParser(description="Screen signal hypotheses on index data.")
    parser.add_argument("--index", default=None, help="default: every configured index")
    parser.add_argument("--signals", nargs="*", help="default: all")
    parser.add_argument("--windows", nargs="*", type=int, default=list(DEFAULT_WINDOWS))
    parser.add_argument("--instrument", default="option", choices=sorted(INSTRUMENTS),
                        help="cost model to judge the edge against (default option)")
    parser.add_argument("--hold", type=float, default=30.0,
                        help="assumed hold in minutes, for theta (default 30)")
    parser.add_argument("--validate", metavar="SIGNAL",
                        help="out-of-sample check: run one signal across disjoint periods")
    parser.add_argument("--since", metavar="YYYY-MM-DD")
    parser.add_argument("--until", metavar="YYYY-MM-DD")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    names = args.signals or list(SIGNALS)
    symbols = [args.index] if args.index else list(INDICES_CONFIG)
    # Screening N hypotheses over M windows means N*M looks at the data.
    tests = max(1, len(names) * len(args.windows) * len(symbols))
    bar = 1.96
    corrected = -_inv_norm(0.025 / tests)

    if args.validate:
        if args.validate not in SIGNALS:
            print("unknown signal %r. known: %s" % (args.validate, ", ".join(SIGNALS)))
            return 1
        for symbol in symbols:
            rows = validate(symbol, args.validate, args.windows)
            if not rows:
                print("%s: no cached candles." % symbol)
                continue
            print()
            print("=" * 78)
            print("  OUT-OF-SAMPLE VALIDATION — %s / %s" % (symbol, args.validate))
            print("  A real edge holds on periods it was not discovered on.")
            print("=" * 78)
            print("  %-18s %5s %-11s %5s %6s %10s %7s"
                  % ("period", "days", "from", "win", "n", "mean fwd%", "t"))
            print("  " + "-" * 72)
            by_label = {}
            for label, ndays, d0, d1, res in rows:
                by_label[label] = res
                for w in args.windows:
                    st = res["windows"].get(w)
                    if not st:
                        continue
                    print("  %-18s %5d %-11s %4dm %6d %+10.4f %7.2f"
                          % (label, ndays, str(d0), w, st["n"], st["mean"], st["t"]))
                print()

            # The halves are the only genuinely disjoint pair; last-60 and
            # last-20 overlap the second half and cannot confirm it.
            a = by_label.get("first half", {}).get("windows", {})
            b = by_label.get("second half", {}).get("windows", {})
            if a and b:
                print("  STABILITY — are the two halves the same effect?")
                unstable = False
                for w in args.windows:
                    sa, sb = a.get(w), b.get(w)
                    if not sa or not sb:
                        continue
                    td = stability_t(sa["mean"], sa["t"], sb["mean"], sb["t"])
                    flag = "UNSTABLE" if abs(td) > 1.96 else "consistent"
                    unstable = unstable or abs(td) > 1.96
                    print("    %4dm  halves differ t=%+6.2f   %s" % (w, td, flag))
                print()
                if unstable:
                    print("    The halves are significantly DIFFERENT from each other.")
                    print("    That is one effect in one period, not an edge measured twice.")
                else:
                    print("    Halves agree — the effect is at least stable across the sample.")
                print()
        print("  Signs must agree across every period. A flip means the finding")
        print("  was a coincidence of the window it was discovered in.")
        print()
        return 0

    for symbol in symbols:
        bars, fut = load(symbol)
        bars = slice_sessions(bars, since=args.since, until=args.until)
        if fut is not None and args.__dict__.get("since") or args.__dict__.get("until"):
            keep = {b["dt"] for b in bars}
            full, ffut = load(symbol)
            by_dt = {b["dt"]: f for b, f in zip(full, ffut or [])}
            fut = [by_dt.get(b["dt"], {"volume": 0.0}) for b in bars] if ffut else None
        if not bars:
            print("%s: no cached candles. Run backtest_data.py first." % symbol)
            continue
        base = unconditional(bars, args.windows)
        print("\n" + "=" * 78)
        print("  SIGNAL SCREEN — %s   %d bars, %d sessions"
              % (symbol, len(bars), len(bd.group_by_session(bars))))
        print("  significance bar: |t| > %.2f raw, > %.2f after correcting for %d tests"
              % (bar, corrected, tests))
        spec = INSTRUMENTS[args.instrument]
        hurdle = required_index_move_pct(args.instrument, args.hold)
        print("  instrument: %s  |  round-trip cost %.2f%% + theta %.1f%%/h"
              % (spec["label"], spec["cost_pct"], spec["theta_pct_per_hour"]))
        print("  break-even index move over a %.0f-min hold: %.4f%%"
              % (args.hold, hurdle))
        print("=" * 78)
        print("  %-16s %6s %5s %10s %7s %8s %8s" %
              ("signal", "n", "win", "mean fwd%", "t", "reach%", "medMFE%"))
        print("  " + "-" * 74)
        for w in args.windows:
            b = base.get(w)
            if b:
                print("  %-16s %6d %5dm %+10.4f %7.2f %8s %8s"
                      % ("(unconditional)", b["n"], w, b["mean"], b["t"], "-", "-"))
        for name in names:
            fn = SIGNALS.get(name)
            if fn is None:
                continue
            res = evaluate(bars, fut, fn, args.windows)
            if not res["windows"]:
                print("  %-16s %6d  no measurable forward windows" % (name, res["n"]))
                continue
            for w in args.windows:
                s = res["windows"].get(w)
                if not s:
                    continue
                flag = ""
                if abs(s["t"]) > corrected:
                    flag = "  <== beats corrected bar"
                elif abs(s["t"]) > bar:
                    flag = "  (raw only)"
                if abs(s["mean"]) >= hurdle:
                    flag += "  [clears cost]"
                print("  %-16s %6d %5dm %+10.4f %7.2f %8.1f %8.3f%s"
                      % (name, s["n"], w, s["mean"], s["t"], s["reach"], s["median_mfe"], flag))
    print("\n  mean fwd%% is the SIGNED index move in the signal's own direction.")
    print("  Under no edge it is zero. Reach%% is the share clearing %.2f%%.\n" % TRADEABLE_PCT)
    return 0


def _inv_norm(p):
    """Inverse standard normal CDF (Acklam's approximation) for the corrected bar."""
    if p <= 0 or p >= 1:
        return 0.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


if __name__ == "__main__":
    raise SystemExit(main())
