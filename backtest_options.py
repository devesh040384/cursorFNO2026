"""Synthetic option pricing for the backtest.

Historical option premiums are the hard part of backtesting this strategy: past
expiries are delisted and the scrip master only carries live contracts. So the
backtest reprices a synthetic ATM contract from the index path instead, using
Black-Scholes with zero rate.

This is a MODEL, not market data. What it does capture, which a naive
"premium moves with delta" approximation does not:

  * delta   — premium responds to the index move
  * gamma   — that response accelerates as the option goes ITM
  * theta   — premium bleeds as expiry approaches, hardest at 0 DTE

Those three are exactly what the open questions turn on (is 0-DTE hurting us,
does a +30% target get reached often enough), so the model has to have them.

What it does NOT capture: IV smile, IV crush/expansion around events, real
bid-ask, and the fact that a real ATM strike is rounded to the strike grid
rather than sitting exactly at spot. Treat absolute rupee results as indicative
and comparisons between parameter settings as the real output.
"""
import math

# Indian index options: cash-settled, no dividend, and at intraday horizons the
# risk-free rate is immaterial next to theta. r = 0 keeps the model honest.
TRADING_DAYS = 252
MINUTES_PER_SESSION = 375

# Strike grid, so the synthetic ATM lands where a real one would.
STRIKE_STEP = {"NIFTY": 50.0, "SENSEX": 100.0, "BANKNIFTY": 100.0}
DEFAULT_STEP = 50.0

# Fallback annualised IV when realised vol is unusable. India VIX has spent most
# of its life in the 10-20 band; 14 is a neutral mid.
DEFAULT_IV = 0.14
MIN_IV, MAX_IV = 0.06, 0.60


def _norm_cdf(x):
    """Standard normal CDF via erf — avoids a scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot, strike, years, iv, is_call=True):
    """Undiscounted Black-Scholes (r=0). Returns the option premium."""
    spot = float(spot)
    strike = float(strike)
    if spot <= 0 or strike <= 0:
        return 0.0
    intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    # At/after expiry, or with no vol, the option is worth exactly its intrinsic.
    if years <= 0 or iv <= 0:
        return intrinsic
    vol_t = iv * math.sqrt(years)
    if vol_t < 1e-9:
        return intrinsic
    d1 = (math.log(spot / strike) + 0.5 * vol_t * vol_t) / vol_t
    d2 = d1 - vol_t
    if is_call:
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def delta(spot, strike, years, iv, is_call=True):
    """Option delta — used for sanity checks and reporting, not pricing."""
    if years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        if is_call:
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    vol_t = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * vol_t * vol_t) / vol_t
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def atm_strike(spot, symbol="NIFTY"):
    """Round to the strike grid the exchange actually lists."""
    step = STRIKE_STEP.get(str(symbol).upper(), DEFAULT_STEP)
    return round(float(spot) / step) * step


def years_to_expiry(dte, minutes_elapsed=0.0):
    """Time to expiry in years, decaying within the session.

    dte is whole days to expiry. On expiry day (dte=0) the option still has the
    rest of the session left, which is what makes 0-DTE theta so violent — and
    what the DTE question needs the model to reproduce.
    """
    remaining_today = max(0.0, MINUTES_PER_SESSION - float(minutes_elapsed))
    sessions = float(max(0, int(dte))) + remaining_today / MINUTES_PER_SESSION
    return sessions / TRADING_DAYS


def realised_iv(closes, bars_per_session=75, floor=MIN_IV, cap=MAX_IV):
    """Annualised realised vol from index closes, as an IV proxy.

    Implied trades above realised most of the time, but the premium level mainly
    scales results rather than changing which parameter setting wins, so the
    simple estimator is the honest choice over an invented multiplier.
    """
    closes = [float(c) for c in closes if c and c > 0]
    if len(closes) < 20:
        return DEFAULT_IV
    rets = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev > 0:
            rets.append(math.log(closes[i] / prev))
    if len(rets) < 20:
        return DEFAULT_IV
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    per_bar = math.sqrt(var)
    annual = per_bar * math.sqrt(bars_per_session * TRADING_DAYS)
    return min(max(annual, floor), cap)


class SyntheticContract:
    """One ATM CE/PE, repriced from the index path.

    Created when the strategy asks for a contract, then repriced bar by bar so
    the exit monitors see a premium series with real delta, gamma and theta.
    """

    __slots__ = ("symbol", "index", "strike", "is_call", "iv", "dte",
                 "lot_size", "entry_minutes", "entry_spot", "entry_price")

    def __init__(self, index, spot, is_call, iv, dte, lot_size, minutes_elapsed=0.0):
        self.index = index
        self.strike = atm_strike(spot, index)
        self.is_call = is_call
        self.iv = float(iv)
        self.dte = int(dte)
        self.lot_size = int(lot_size)
        self.entry_minutes = float(minutes_elapsed)
        self.entry_spot = float(spot)
        self.symbol = "%s%dSYN%s" % (index, int(self.strike), "CE" if is_call else "PE")
        self.entry_price = self.price(spot, minutes_elapsed)

    def price(self, spot, minutes_elapsed):
        years = years_to_expiry(self.dte, minutes_elapsed)
        return black_scholes(spot, self.strike, years, self.iv, self.is_call)

    def delta(self, spot, minutes_elapsed):
        years = years_to_expiry(self.dte, minutes_elapsed)
        return delta(spot, self.strike, years, self.iv, self.is_call)

    def __repr__(self):
        return "<%s strike=%.0f dte=%d iv=%.1f%%>" % (
            self.symbol, self.strike, self.dte, 100 * self.iv)


def round_trip_cost(notional, brokerage_per_leg=20.0):
    """Approximate Angel One F&O cost for one buy+sell round trip.

    Rates are approximate and change — verify against a real contract note.
    Modelled because flat brokerage is ~1% of a Rs6,000 notional, which is half
    the strategy's measured edge and therefore cannot be left out.
    """
    notional = abs(float(notional))
    brokerage = 2.0 * float(brokerage_per_leg)
    stt = 0.001 * notional            # sell side of premium
    exchange = 0.0005 * notional * 2  # both legs
    gst = 0.18 * (brokerage + exchange)
    stamp = 0.00003 * notional        # buy side
    return brokerage + stt + exchange + gst + stamp


def implied_iv(premium, spot, strike, years, is_call=True, lo=0.01, hi=2.0, tol=1e-6):
    """Back out IV from an observed premium by bisection.

    Used to calibrate the model against real fills rather than assuming a level:
    NIFTY and SENSEX do not trade at the same IV, and a single default misprices
    one of them badly enough to change which parameter setting looks best.
    """
    premium = float(premium)
    intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    if premium <= intrinsic or years <= 0:
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        price = black_scholes(spot, strike, years, mid, is_call)
        if abs(price - premium) < tol:
            return mid
        if price < premium:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def calibrate_iv_from_fills(fills):
    """Fit a per-index IV from real trades.

    `fills` items: (index, spot, strike, dte, minutes_elapsed, premium, is_call).
    Returns {index: median_iv}. Median, not mean, so one odd fill cannot drag
    the whole calibration.
    """
    per_index = {}
    for index, spot, strike, dte, minutes, premium, is_call in fills:
        years = years_to_expiry(dte, minutes)
        iv = implied_iv(premium, spot, strike, years, is_call)
        if iv is not None and MIN_IV <= iv <= MAX_IV:
            per_index.setdefault(str(index).upper(), []).append(iv)
    out = {}
    for index, values in per_index.items():
        values.sort()
        n = len(values)
        out[index] = values[n // 2] if n % 2 else 0.5 * (values[n // 2 - 1] + values[n // 2])
    return out
