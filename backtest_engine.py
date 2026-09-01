"""Replay the live strategy over cached history.

The point of this file is what it does NOT contain: there is no reimplementation
of the regime classifier, the RSI, the volume gate, the entry rules or the
trailing ladder. It drives the real `StrategyBrain` and calls the real
`risk_monitors._trailed_stop`. If the bot's logic changes, the backtest changes
with it — a reimplementation would quietly validate the copy instead.

To make live code run over history, three things are substituted:

  * the clock      — a controllable clock replaces `time` inside strategy_brain
                     and the IST helpers inside database, so "today" follows the
                     simulated date
  * the broker     — a fake order manager records fills instead of placing them
  * the option     — `backtest_options.SyntheticContract`, since historical
                     premiums for expired contracts are not retrievable

Everything else — risk caps, cooldowns, session windows, the entry budget — is
the production code path, running against a real (temporary) SQLite database.

  python3 backtest_engine.py --days 180
  python3 backtest_engine.py --days 180 --set trending_target_mult=1.22
"""
import argparse
import logging
import os
import tempfile
from datetime import datetime, timedelta

import backtest_data as bd
import backtest_options as bo
from config import FALLBACK_LOT_SIZE, INDICES_CONFIG, RISK, history_token

MARKET_OPEN_MIN = 9 * 60 + 15


class Clock:
    """Simulated wall clock. Substituted for `time` inside strategy_brain."""

    def __init__(self, dt=None):
        self.dt = dt or datetime(2026, 1, 1, 9, 15)

    def set(self, dt):
        self.dt = dt

    def time(self):
        return self.dt.timestamp()

    def sleep(self, _seconds):  # strategy code never sleeps, but keep the API
        return None

    def hhmm(self):
        return self.dt.hour * 100 + self.dt.minute

    def today(self):
        return self.dt.strftime("%Y-%m-%d")

    def stamp(self):
        return self.dt.strftime("%Y-%m-%d %H:%M:%S")

    def minutes_elapsed(self):
        return max(0.0, (self.dt.hour * 60 + self.dt.minute) - MARKET_OPEN_MIN)


class FakeAPI:
    """Serves option LTP from the synthetic contract the strategy just asked for."""

    def __init__(self, engine):
        self.engine = engine

    def ltpData(self, exchange, symbol, token):
        contract = self.engine.pending.get(str(token))
        if contract is None:
            return None
        price = contract.price(self.engine.spot[contract.index], self.engine.clock.minutes_elapsed())
        if price <= 0:
            return None
        return {"status": True, "data": {"ltp": round(price, 2)}}


class FakeOrderManager:
    """Stands in for OrderExecutionEngine. Records the fill, opens no order."""

    def __init__(self, engine, db):
        self.engine = engine
        self.db_manager = db
        self.smart_api = FakeAPI(engine)

    def execute_entry(self, symbol, token, qty, exchange="NFO", price=0.0,
                      target_price=0.0, stop_loss_price=0.0, index_name=None,
                      entry_reason=None, expiry=None, dte=None, **kwargs):
        contract = self.engine.pending.get(str(token))
        if contract is None or price <= 0:
            return None
        return self.engine.open_position(
            contract, qty, price, target_price, stop_loss_price, index_name, entry_reason, dte)


class FakeBuilder:
    """Stands in for DynamicOptionsChainBuilder, returning a synthetic ATM contract."""

    def __init__(self, engine, index):
        self.engine = engine
        self.index = index

    def get_nearest_expiry_contract(self, spot_price, instrument_type="CE"):
        engine = self.engine
        dte = engine.dte_for(self.index)
        if dte < int(RISK.get("min_dte", 0)):
            return None
        contract = bo.SyntheticContract(
            index=self.index,
            spot=engine.spot[self.index],
            is_call=(str(instrument_type).upper() == "CE"),
            iv=engine.iv[self.index],
            dte=dte,
            lot_size=FALLBACK_LOT_SIZE.get(self.index, 65),
            minutes_elapsed=engine.clock.minutes_elapsed(),
        )
        if contract.entry_price <= 0:
            return None
        token = "SYN%d" % engine.token_seq
        engine.token_seq += 1
        engine.pending[token] = contract
        return {
            "symbol": contract.symbol, "token": token, "strike": contract.strike,
            "expiry": "SYN", "dte": dte, "lotsize": contract.lot_size,
            "exchange": INDICES_CONFIG[self.index]["option_exchange"],
            # Observed live spreads were 0.09-0.36%; use the midpoint so the
            # liquidity gate behaves as it does in production.
            "bid": None, "ask": None, "spread_pct": 0.21,
        }


class Position:
    __slots__ = ("contract", "qty", "entry", "target", "stop", "peak",
                 "index", "reason", "dte", "opened_at", "trade_id")

    def __init__(self, contract, qty, entry, target, stop, index, reason, dte, opened_at, trade_id):
        self.contract, self.qty, self.entry = contract, qty, entry
        self.target, self.stop, self.peak = target, stop, entry
        self.index, self.reason, self.dte = index, reason, dte
        self.opened_at, self.trade_id = opened_at, trade_id


class Backtest:
    def __init__(self, expiry_weekday=None, cost_model=True, verbose=False):
        self.clock = Clock()
        self.spot = {}
        self.iv = {}
        self.pending = {}
        self.open_positions = {}
        self.trades = []
        self.token_seq = 0
        self.cost_model = cost_model
        self.verbose = verbose
        # NIFTY weekly expires Tue(1), SENSEX Thu(3). Override if the exchange
        # calendar changes — this drives the whole DTE analysis.
        self.expiry_weekday = expiry_weekday or {"NIFTY": 1, "SENSEX": 3}
        self._db_path = None
        self._patched = []

    # ---------------------------------------------------------------- lifecycle

    def _patch(self, module, name, value):
        self._patched.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def _install(self):
        """Swap the clock into the live modules. Reversed in _restore()."""
        import database
        import strategy_brain

        self._patch(strategy_brain, "time", self.clock)
        self._patch(strategy_brain, "ist_hhmm", self.clock.hhmm)
        self._patch(strategy_brain, "ist_today", self.clock.today)
        self._patch(database, "ist_today", self.clock.today)
        self._patch(database, "ist_stamp", self.clock.stamp)

    def _restore(self):
        for module, name, original in reversed(self._patched):
            setattr(module, name, original)
        self._patched = []

    def dte_for(self, index):
        weekday = self.expiry_weekday.get(index, 3)
        days = (weekday - self.clock.dt.weekday()) % 7
        return days

    # ---------------------------------------------------------------- positions

    def open_position(self, contract, qty, entry, target, stop, index, reason, dte):
        trade_id = self.db.log_trade(
            contract.symbol, "SYN", entry, target, stop, qty=qty,
            exchange=INDICES_CONFIG[index]["option_exchange"], index_name=index,
            entry_reason=reason, expiry="SYN", dte=dte,
        )
        if trade_id is None:
            return None
        self.open_positions[index] = Position(
            contract, qty, entry, target, stop, index, reason, dte, self.clock.dt, trade_id)
        if self.verbose:
            logging.info("  %s ENTRY %s %s @ %.2f (dte %d)",
                         self.clock.stamp(), index, reason, entry, dte)
        return "bt_%d" % trade_id

    def close_position(self, index, price, why):
        pos = self.open_positions.pop(index, None)
        if pos is None:
            return
        gross = (price - pos.entry) * pos.qty
        cost = bo.round_trip_cost(pos.entry * pos.qty) if self.cost_model else 0.0
        self.db.close_trade(pos.trade_id, price, why)
        self.trades.append({
            "date": pos.opened_at.date(), "index": index, "reason": pos.reason,
            "exit_reason": why, "dte": pos.dte, "qty": pos.qty,
            "entry": pos.entry, "exit": price, "peak": pos.peak,
            "gross": gross, "cost": cost, "net": gross - cost,
            "held_min": (self.clock.dt - pos.opened_at).total_seconds() / 60.0,
        })
        if self.verbose:
            logging.info("  %s EXIT  %s %s @ %.2f  net %+.0f",
                         self.clock.stamp(), index, why, price, gross - cost)

    def manage_exits(self, index):
        """Target, stop, trailing ladder and time-stop — using the live rules."""
        from risk_monitors import _trailed_stop

        pos = self.open_positions.get(index)
        if pos is None:
            return
        price = pos.contract.price(self.spot[index], self.clock.minutes_elapsed())
        if price <= 0:
            return
        if pos.target and price >= pos.target:
            return self.close_position(index, price, "TARGET_HIT")
        if pos.stop and price <= pos.stop:
            return self.close_position(index, price, "STOP_LOSS_HIT")

        held = (self.clock.dt - pos.opened_at).total_seconds() / 60.0
        if held >= RISK["time_stop_minutes"] and price < pos.entry * RISK["time_stop_min_gain_mult"]:
            return self.close_position(index, price, "TIME_STOP")

        pos.peak = max(pos.peak, price)
        pos.stop = _trailed_stop(pos.entry, pos.peak, price, pos.stop)

    # ---------------------------------------------------------------- the replay

    def run(self, days=180, indices=None):
        from database import DatabaseManager
        from strategy_brain import StrategyBrain

        indices = indices or [s for s in INDICES_CONFIG]
        series, futures = {}, {}
        for symbol in indices:
            cfg = INDICES_CONFIG[symbol]
            bars = bd.load_series(cfg["exchange"], history_token(symbol))
            if not bars:
                logging.error("%s: no cached index bars. Run backtest_data.py first.", symbol)
                continue
            series[symbol] = bd.group_by_session(bars)
            self.iv[symbol] = bo.realised_iv([b["close"] for b in bars[-2000:]])
        if not series:
            return []

        for symbol in indices:
            fut_bars = self._load_futures(symbol)
            futures[symbol] = bd.group_by_session(fut_bars) if fut_bars else {}
            if not fut_bars:
                logging.warning("%s: no cached futures bars — RVOL gate will block entries.", symbol)

        cutoff = (bd.ist_now().replace(tzinfo=None) - timedelta(days=days)).date()
        all_days = sorted({d for s in series.values() for d in s if d >= cutoff})
        logging.info("replaying %d sessions, IV %s", len(all_days),
                     {k: "%.1f%%" % (100 * v) for k, v in self.iv.items()})

        fd, self._db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._install()
        try:
            self.db = DatabaseManager(self._db_path)
            for day in all_days:
                self._run_session(day, series, futures, indices, StrategyBrain)
        finally:
            self._restore()
            if self._db_path and os.path.exists(self._db_path):
                os.remove(self._db_path)
        return self.trades

    def _load_futures(self, symbol):
        """Futures bars are cached under whichever exchange/token was pulled."""
        cfg = INDICES_CONFIG[symbol]
        for exchange in (cfg["option_exchange"], cfg["exchange"]):
            if not os.path.isdir(bd.CACHE_DIR):
                return []
            for name in os.listdir(bd.CACHE_DIR):
                if name.startswith(exchange + "_") and name.endswith(".csv"):
                    token = name[:-4].split("_")[1]
                    if str(token) in (str(cfg["index_token"]), str(history_token(symbol))):
                        continue  # that is the index itself, not a future
                    bars = bd.load_series(exchange, token)
                    if bars:
                        return bars
        return []

    def _run_session(self, day, series, futures, indices, StrategyBrain):
        # A fresh brain per session mirrors production: the bot restarts daily and
        # seeds history, so state must not leak across sessions.
        brain = StrategyBrain(order_engine=None, options_builders={}, db_manager=self.db)
        brain.order_manager = FakeOrderManager(self, self.db)
        for symbol in indices:
            token = str(INDICES_CONFIG[symbol]["index_token"])
            brain.options_builders[token] = FakeBuilder(self, symbol)
            brain.volume_gate.mark_subscribed(symbol, bool(futures.get(symbol, {}).get(day)))

        # Seed the gate with the prior session's futures volume, as the live
        # seeder does, so entries are possible from 09:45 rather than after warmup.
        for symbol in indices:
            prior = self._prior_session_volumes(futures.get(symbol, {}), day)
            if prior:
                brain.volume_gate.closed_volumes[symbol] = prior[-40:]

        merged = {}
        for symbol in indices:
            for bar in series.get(symbol, {}).get(day, []):
                merged.setdefault(bar["dt"], {})[symbol] = bar
            for bar in futures.get(symbol, {}).get(day, []):
                merged.setdefault(bar["dt"], {})["FUT_" + symbol] = bar

        for stamp in sorted(merged):
            self.clock.set(stamp)
            slot = merged[stamp]
            for symbol in indices:
                fut = slot.get("FUT_" + symbol)
                if fut:
                    brain.volume_gate.on_fut_tick(symbol, last_traded_qty=fut["volume"])
                bar = slot.get(symbol)
                if not bar:
                    continue
                self.spot[symbol] = bar["close"]
                self.manage_exits(symbol)
                brain.evaluate_tick(symbol, bar["close"])

        # Square off whatever is still open, as the 15:15 kill switch does.
        for symbol in list(self.open_positions):
            price = self.open_positions[symbol].contract.price(
                self.spot.get(symbol, 0), self.clock.minutes_elapsed())
            if price > 0:
                self.close_position(symbol, price, "EOD_SQUAREOFF")
            else:
                self.open_positions.pop(symbol, None)

    @staticmethod
    def _prior_session_volumes(fut_sessions, day):
        prior_days = sorted(d for d in fut_sessions if d < day)
        if not prior_days:
            return []
        return [b["volume"] for b in fut_sessions[prior_days[-1]]]


def summarise(trades, label="BACKTEST"):
    if not trades:
        return ["no trades"]
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gross_w, gross_l = sum(wins), abs(sum(losses))
    equity = peak = maxdd = 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        maxdd = max(maxdd, peak - equity)
    lines = [
        "=" * 68, "  %s" % label, "=" * 68,
        "  trades %d  wins %d  losses %d  win rate %.1f%%"
        % (len(nets), len(wins), len(losses), 100.0 * len(wins) / len(nets)),
        "  net INR %.0f  gross INR %.0f  costs INR %.0f"
        % (sum(nets), sum(t["gross"] for t in trades), sum(t["cost"] for t in trades)),
        "  avg win INR %.0f  avg loss INR %.0f  expectancy INR %.2f"
        % (gross_w / len(wins) if wins else 0, -gross_l / len(losses) if losses else 0,
           sum(nets) / len(nets)),
        "  profit factor %.2f  max DD INR %.0f"
        % ((gross_w / gross_l) if gross_l else float("inf"), maxdd),
    ]
    for key, title in (("index", "by index"), ("reason", "by entry"),
                       ("exit_reason", "by exit"), ("dte", "by DTE")):
        buckets = {}
        for t in trades:
            b = buckets.setdefault(t[key], [0, 0.0])
            b[0] += 1
            b[1] += t["net"]
        lines.append("  %s: %s" % (title, ", ".join(
            "%s n=%d INR %.0f" % (k, v[0], v[1]) for k, v in sorted(buckets.items(), key=str))))
    return lines


def apply_overrides(pairs):
    """--set key=value, so a sweep can vary config without editing config.py."""
    for pair in pairs or []:
        key, _, raw = pair.partition("=")
        key = key.strip()
        try:
            value = float(raw)
            if value.is_integer() and "." not in raw:
                value = int(value)
        except ValueError:
            value = raw.strip()
        if key in RISK:
            RISK[key] = value
        else:
            for cfg in INDICES_CONFIG.values():
                if key in cfg:
                    cfg[key] = value
        logging.info("override %s = %r", key, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Replay the live strategy over cached history.")
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--indices", nargs="*")
    parser.add_argument("--set", dest="overrides", nargs="*", help="key=value config overrides")
    parser.add_argument("--no-costs", action="store_true", help="exclude brokerage/taxes")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(message)s")
    apply_overrides(args.overrides)
    bt = Backtest(cost_model=not args.no_costs, verbose=args.verbose)
    trades = bt.run(days=args.days, indices=args.indices)
    print("\n".join(summarise(trades, "BACKTEST  %d sessions requested" % args.days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
