"""1-minute timeframe: entry confirmation and structural stops.

The 5-minute bar stays the signal timeframe — regime, RVOL and the breakout test
are unchanged. This adds a faster timeframe used for two things only:

  1. ENTRY TIMING. A 5-minute signal fires at the bar close, which on a vertical
     move is the top of the move. Instead of filling immediately, the signal can
     be held pending until the 1-minute series confirms it.

  2. STRUCTURAL STOPS. A fixed -10% premium stop is arbitrary. The 1-minute
     pivot that defined the setup is not: if the index closes back through it,
     the reason for the trade is gone.

Everything here works on the INDEX, not the option. The bot subscribes to index
ticks continuously but never to option ticks, so a 1-minute option series does
not exist before entry — and the index is the signal source anyway; the option
is only the instrument.

All of it is inert unless `RISK["entry_timing"]` / `RISK["stop_mode"]` are moved
off their defaults, which reproduce today's behaviour exactly.
"""
import logging

from config import RISK

IMMEDIATE = "immediate"
CONTINUATION = "continuation"
PULLBACK = "pullback"

FIXED_PCT = "fixed_pct"
STRUCTURAL_1M = "structural_1m"


def entry_timing():
    return str(RISK.get("entry_timing", IMMEDIATE)).lower()


def stop_mode():
    return str(RISK.get("stop_mode", FIXED_PCT)).lower()


def confirm_window_min():
    return int(RISK.get("confirm_window_min", 3))


def pullback_pct():
    return float(RISK.get("pullback_pct", 0.15))


class MinuteBars:
    """Aggregates index ticks into 1-minute bars.

    Keeps only what the confirmation rules need: the forming bar and the last
    few closed ones. Bars roll on the wall-clock minute, matching how the
    5-minute signal bars bucket, so a 1-minute close is a real minute boundary
    rather than "60 seconds since the last tick".
    """

    __slots__ = ("closed", "cur_minute", "cur_open", "cur_high", "cur_low", "cur_close", "max_keep")

    def __init__(self, max_keep=20):
        self.closed = []
        self.cur_minute = None
        self.cur_open = self.cur_high = self.cur_low = self.cur_close = None
        self.max_keep = int(max_keep)

    def update(self, ts_minute, price):
        """Feed a tick. `ts_minute` is an integer minute index. Returns the bar
        that just closed, or None."""
        price = float(price)
        if self.cur_minute is None:
            self.cur_minute = ts_minute
            self.cur_open = self.cur_high = self.cur_low = self.cur_close = price
            return None

        if ts_minute == self.cur_minute:
            self.cur_high = max(self.cur_high, price)
            self.cur_low = min(self.cur_low, price)
            self.cur_close = price
            return None

        closed = {"minute": self.cur_minute, "open": self.cur_open, "high": self.cur_high,
                  "low": self.cur_low, "close": self.cur_close}
        self.closed.append(closed)
        if len(self.closed) > self.max_keep:
            self.closed.pop(0)
        self.cur_minute = ts_minute
        self.cur_open = self.cur_high = self.cur_low = self.cur_close = price
        return closed

    @property
    def last_closed(self):
        return self.closed[-1] if self.closed else None

    def pivot(self, is_call):
        """The level whose breach invalidates the setup.

        For a long call that is the previous 1-minute low; for a long put, the
        previous 1-minute high.
        """
        bar = self.last_closed
        if bar is None:
            return None
        return bar["low"] if is_call else bar["high"]


class PendingEntry:
    """A 5-minute signal held until the 1-minute series confirms it.

    Continuation: fill only when the index makes a new 1-minute extreme in the
    signal's direction — so a move that peaks and fades is never filled, which
    is the whole point.

    Pullback: fill on a retracement from the signal bar's close — a better
    price, at the risk of filling into a move that is already failing.
    """

    __slots__ = ("symbol", "side", "reason", "signal_price", "created_minute",
                 "trigger", "bars_seen")

    def __init__(self, symbol, side, reason, signal_price, created_minute, trigger):
        self.symbol = symbol
        self.side = side                    # "CE" or "PE"
        self.reason = reason
        self.signal_price = float(signal_price)
        self.created_minute = int(created_minute)
        self.trigger = float(trigger)
        self.bars_seen = 0

    @property
    def is_call(self):
        return self.side == "CE"

    def expired(self, now_minute):
        return (int(now_minute) - self.created_minute) > confirm_window_min()

    def confirmed(self, bar):
        """Does this closed 1-minute bar satisfy the confirmation rule?"""
        mode = entry_timing()
        if mode == CONTINUATION:
            # A new extreme in our direction: buyers (or sellers) followed through.
            return bar["close"] > self.trigger if self.is_call else bar["close"] < self.trigger
        if mode == PULLBACK:
            # Retraced far enough against the signal to be worth entering.
            return bar["low"] <= self.trigger if self.is_call else bar["high"] >= self.trigger
        return True


def make_trigger(side, signal_price, bars):
    """The price level that must be reached for the pending entry to fill.

    Returns None when the mode needs 1-minute history that does not exist yet;
    the caller then falls back to entering immediately rather than losing the
    signal.
    """
    mode = entry_timing()
    is_call = side == "CE"
    if mode == CONTINUATION:
        bar = bars.last_closed
        if bar is None:
            return None
        # Break the last completed minute's extreme in our direction.
        return bar["high"] if is_call else bar["low"]
    if mode == PULLBACK:
        pct = pullback_pct() / 100.0
        return signal_price * (1.0 - pct) if is_call else signal_price * (1.0 + pct)
    return None


def structural_stop_level(is_call, bars, entry_index_price):
    """Index level whose breach invalidates the trade, or None if unavailable.

    Never returns a level already breached at entry — that would stop the trade
    out on its first tick.
    """
    if stop_mode() != STRUCTURAL_1M:
        return None
    level = bars.pivot(is_call)
    if level is None:
        return None
    if is_call and level >= entry_index_price:
        return None
    if not is_call and level <= entry_index_price:
        return None
    return level


def structural_stop_hit(is_call, level, index_price):
    if level is None:
        return False
    return index_price <= level if is_call else index_price >= level


class PendingBook:
    """Per-symbol pending entries, with the timing policy applied."""

    def __init__(self):
        self.pending = {}

    def arm(self, symbol, side, reason, signal_price, now_minute, bars):
        """Hold a signal for confirmation. Returns True if it was held.

        False means the caller should enter immediately — either because the
        mode is `immediate`, or because the 1-minute history needed to build a
        trigger is not there yet.
        """
        if entry_timing() == IMMEDIATE:
            return False
        trigger = make_trigger(side, signal_price, bars)
        if trigger is None:
            logging.info("[%s] %s: no 1m history for a %s trigger; entering immediately.",
                         symbol, reason, entry_timing())
            return False
        self.pending[symbol] = PendingEntry(symbol, side, reason, signal_price, now_minute, trigger)
        logging.info("[%s] %s armed (%s): waiting for %.2f within %dm",
                     symbol, reason, entry_timing(), trigger, confirm_window_min())
        return True

    def on_minute_close(self, symbol, bar, now_minute):
        """Returns the PendingEntry to fill now, or None. Expired ones are dropped."""
        entry = self.pending.get(symbol)
        if entry is None:
            return None
        entry.bars_seen += 1
        if entry.confirmed(bar):
            self.pending.pop(symbol, None)
            logging.info("[%s] %s confirmed after %d 1m bar(s).",
                         symbol, entry.reason, entry.bars_seen)
            return entry
        if entry.expired(now_minute):
            self.pending.pop(symbol, None)
            logging.info("[%s] %s dropped: no confirmation within %dm.",
                         symbol, entry.reason, confirm_window_min())
        return None

    def clear(self, symbol=None):
        if symbol is None:
            self.pending.clear()
        else:
            self.pending.pop(symbol, None)
