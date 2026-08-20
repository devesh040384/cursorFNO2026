def wilder_rsi(closes, period=14):
    """Wilder RSI: seed SMA of first `period` changes, then smooth (prev*(n-1)+chg)/n."""
    if closes is None or len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = float(closes[i]) - float(closes[i - 1])
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def sma_rsi(closes, period=14):
    """Last-window SMA RSI (not Wilder). Kept to prove the two differ in tests."""
    if closes is None or len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = float(closes[i]) - float(closes[i - 1])
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volume_expanded(closed_volumes, mult=1.5, sma_bars=20):
    """Last closed bar vs SMA of the prior bars in the window (standard RVOL)."""
    if not closed_volumes or len(closed_volumes) < sma_bars:
        return False
    window = [float(v) for v in closed_volumes[-sma_bars:]]
    prior = window[:-1]
    if not prior:
        return False
    avg = sum(prior) / len(prior)
    if avg <= 0:
        return False
    return window[-1] >= mult * avg


class TechnicalIndicators:
    @staticmethod
    def calculate_atr(highs, lows, closes, period=14):
        import numpy as np

        if len(closes) < period + 1:
            return 0.0
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            tr_list.append(tr)
        return float(np.mean(tr_list[-period:]))

    @staticmethod
    def calculate_rsi(closes, period=14):
        return wilder_rsi(closes, period=period)

    @staticmethod
    def calculate_vwap(prices, volumes):
        import numpy as np

        if len(prices) == 0 or len(volumes) == 0:
            return prices[-1] if prices else 0.0
        p = np.array(prices)
        v = np.array(volumes)
        return float(np.sum(p * v) / np.sum(v)) if np.sum(v) > 0 else float(p[-1])

    @staticmethod
    def calculate_bbw(closes, period=20, num_std=2):
        import numpy as np

        if len(closes) < period:
            return 0.0
        recent_closes = np.array(closes[-period:])
        sma = np.mean(recent_closes)
        std = np.std(recent_closes)
        upper = sma + (num_std * std)
        lower = sma - (num_std * std)
        if sma == 0:
            return 0.0
        return float((upper - lower) / sma)
