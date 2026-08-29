import time
import json
import os
import logging
from config import FALLBACK_LOT_SIZE, INDICES_CONFIG, RISK, signal_bar_bucket, signal_bar_sec
from ist_time import ist_hhmm, ist_today
from risk_manager import RiskManager
from indicators import wilder_rsi, volume_expanded

# Upper bound on the entry pause after a feed gap: EMA21 needs ~21 clean bars,
# but pausing longer than that just wastes the session.
MAX_STALE_BARS = 22


class VolumeExpansionGate:
    """Signal-bar futures volume vs SMA (default 5-min). Fail closed until sma_bars + subscribe."""

    def __init__(self):
        self.closed_volumes = {symbol: [] for symbol in INDICES_CONFIG.keys()}
        self.forming_vol = {symbol: 0.0 for symbol in INDICES_CONFIG.keys()}
        self.last_bar_time = {symbol: 0.0 for symbol in INDICES_CONFIG.keys()}
        self.last_bar_bucket = {symbol: None for symbol in INDICES_CONFIG.keys()}
        self.last_session_vol = {symbol: None for symbol in INDICES_CONFIG.keys()}
        self.subscribed = {symbol: False for symbol in INDICES_CONFIG.keys()}
        self.volume_ok = {symbol: False for symbol in INDICES_CONFIG.keys()}
        self.volume_ok_until = {symbol: 0.0 for symbol in INDICES_CONFIG.keys()}
        self.breakout_event = {symbol: None for symbol in INDICES_CONFIG.keys()}
        self.zero_bar_streak = {symbol: 0 for symbol in INDICES_CONFIG.keys()}
        self.last_seq = {symbol: None for symbol in INDICES_CONFIG.keys()}
        self.last_tick_sig = {symbol: None for symbol in INDICES_CONFIG.keys()}
        # First live bar starts mid-bucket: its partial volume would drag the SMA
        # down and fake an expansion on the next bar. Discard it once.
        self.partial_first_bar = {symbol: False for symbol in INDICES_CONFIG.keys()}

    def _breakout_age(self):
        return float(RISK.get("breakout_max_age_sec", max(signal_bar_sec() * 2, 600)))

    def mark_subscribed(self, symbol, ok=True):
        self.subscribed[symbol] = bool(ok)
        if not ok:
            self.volume_ok[symbol] = False
            self.volume_ok_until[symbol] = 0.0
            self.breakout_event[symbol] = None

    def rvol(self, symbol):
        hist = self.closed_volumes.get(symbol) or []
        sma_bars = int(RISK.get("volume_sma_bars", 20))
        if len(hist) < sma_bars:
            return None
        window = [float(v) for v in hist[-sma_bars:]]
        prior = window[:-1]
        if not prior:
            return None
        avg = sum(prior) / len(prior)
        if avg <= 0:
            return 0.0
        return window[-1] / avg

    def snapshot(self, symbol):
        hist = self.closed_volumes.get(symbol) or []
        need = int(RISK.get("volume_sma_bars", 20))
        rv = self.rvol(symbol)
        ok = self.allows_entry(symbol)
        bar_m = max(1, signal_bar_sec() // 60)
        if not self.subscribed.get(symbol):
            reason = "no-fut"
        elif len(hist) < need:
            reason = f"warmup {len(hist)}/{need}"
        elif rv is not None and rv <= 0:
            reason = "vol-feed-zero"
        elif not ok:
            reason = f"rvol {rv:.2f}x" if rv is not None else "low-vol"
        else:
            reason = "ready"
        return {
            "bars": len(hist),
            "need": need,
            "rvol": rv,
            "ok": ok,
            "reason": reason,
            "bar_min": bar_m,
        }

    def allows_entry(self, symbol):
        """Hook path: average-or-better futures volume (or sticky post-breakout hold)."""
        if not RISK.get("require_volume_expansion", True):
            return True
        if not self.subscribed.get(symbol):
            return False
        if time.time() < float(self.volume_ok_until.get(symbol) or 0.0):
            return True
        rv = self.rvol(symbol)
        if rv is None:
            return False
        return rv >= float(RISK.get("volume_hook_mult", 1.0))

    def allows_expansion(self, symbol):
        """TREND_CONT / breakout-grade: sticky hold or rvol >= volume_mult."""
        if not RISK.get("require_volume_expansion", True):
            return True
        if not self.subscribed.get(symbol):
            return False
        if time.time() < float(self.volume_ok_until.get(symbol) or 0.0):
            return True
        rv = self.rvol(symbol)
        if rv is None:
            return False
        return rv >= float(RISK.get("volume_mult", 1.2))

    def has_fresh_breakout(self, symbol, max_age_sec=None):
        max_age_sec = self._breakout_age() if max_age_sec is None else max_age_sec
        ts = self.breakout_event.get(symbol)
        if ts is None:
            return False
        if (time.time() - float(ts)) > max_age_sec:
            self.breakout_event[symbol] = None
            return False
        return True

    def consume_breakout(self, symbol, max_age_sec=None):
        """Return True once for a fresh expansion; clears the event."""
        max_age_sec = self._breakout_age() if max_age_sec is None else max_age_sec
        ts = self.breakout_event.get(symbol)
        self.breakout_event[symbol] = None
        if ts is None:
            return False
        return (time.time() - float(ts)) <= max_age_sec

    def _close_volume_bar(self, symbol, now, increment):
        closed = self.forming_vol[symbol]
        if self.partial_first_bar.get(symbol):
            self.partial_first_bar[symbol] = False
            self.forming_vol[symbol] = increment
            self.last_bar_time[symbol] = now
            logging.info(f"[{symbol}] Discarded partial first futures volume bar ({closed:.0f}).")
            return
        hist = self.closed_volumes[symbol]
        hist.append(closed)
        if len(hist) > 80:
            hist.pop(0)
        self.closed_volumes[symbol] = hist

        bar_m = max(1, signal_bar_sec() // 60)
        if closed <= 0:
            self.zero_bar_streak[symbol] = self.zero_bar_streak.get(symbol, 0) + 1
            if self.zero_bar_streak[symbol] == 5:
                logging.warning(
                    f"[{symbol}] {bar_m}-min futures volume is 0 for 5 bars. "
                    "Quote feed may be missing volume fields; entries stay blocked."
                )
        else:
            self.zero_bar_streak[symbol] = 0

        expanded = volume_expanded(
            hist,
            mult=float(RISK.get("volume_mult", 1.2)),
            sma_bars=int(RISK.get("volume_sma_bars", 20)),
        )
        self.volume_ok[symbol] = expanded
        if expanded:
            self.breakout_event[symbol] = now
            hold = float(RISK.get("volume_ok_hold_sec", self._breakout_age()))
            self.volume_ok_until[symbol] = now + hold
        rv = self.rvol(symbol)
        rv_s = f"{rv:.2f}x" if rv is not None else "n/a"
        logging.info(
            f"📊 [{symbol} {bar_m}M VOL] last={closed:.0f} rvol={rv_s} "
            f"bars={len(hist)}/{int(RISK.get('volume_sma_bars', 20))} "
            f"breakout={expanded} hook={self.allows_entry(symbol)}"
        )
        self.forming_vol[symbol] = increment
        self.last_bar_time[symbol] = now

    def on_fut_tick(self, symbol, volume_traded_today=None, last_traded_qty=None, sequence_number=None):
        if symbol not in INDICES_CONFIG:
            return
        now = time.time()
        bucket = signal_bar_bucket(now)
        bar_sec = signal_bar_sec()
        if self.last_bar_time[symbol] == 0.0:
            self.last_bar_time[symbol] = now
            self.last_bar_bucket[symbol] = bucket
            # Only partial if we joined mid-bucket with no seeded history.
            self.partial_first_bar[symbol] = not self.closed_volumes.get(symbol)

        if sequence_number is not None:
            if sequence_number == self.last_seq.get(symbol):
                return
            self.last_seq[symbol] = sequence_number
        else:
            sig = (volume_traded_today, last_traded_qty)
            if sig == self.last_tick_sig.get(symbol) and sig != (None, None):
                return
        self.last_tick_sig[symbol] = (volume_traded_today, last_traded_qty)

        increment = 0.0
        if volume_traded_today is not None:
            prev = self.last_session_vol[symbol]
            current = float(volume_traded_today)
            if prev is None:
                increment = 0.0
            else:
                increment = max(0.0, current - prev)
            self.last_session_vol[symbol] = current
            # Session cumulative often stalls on a tick; last trade qty still moves.
            if increment == 0.0 and last_traded_qty is not None:
                increment = max(0.0, float(last_traded_qty))
        elif last_traded_qty is not None:
            increment = max(0.0, float(last_traded_qty))

        last_bucket = self.last_bar_bucket[symbol]
        clock_rolled = last_bucket is not None and bucket != last_bucket
        elapsed_rolled = now - self.last_bar_time[symbol] >= bar_sec
        if clock_rolled or elapsed_rolled:
            # A feed gap (reconnect) spans several buckets but only closes one bar,
            # so the "bar" holds a fraction of the traded volume. Padding with zeros
            # would crater the SMA and fake an expansion on the next bar, so drop the
            # stale sticky state and let RVOL rebuild from clean bars instead.
            missed = int((now - self.last_bar_time[symbol]) // bar_sec) - 1
            if missed > 0:
                logging.warning(
                    f"[{symbol}] Futures volume feed gap: {missed} bar(s) missed. "
                    "Dropping partial bar and clearing sticky expansion."
                )
                self.volume_ok[symbol] = False
                self.volume_ok_until[symbol] = 0.0
                self.breakout_event[symbol] = None
                self.forming_vol[symbol] = increment
                self.last_bar_time[symbol] = now
                self.last_bar_bucket[symbol] = bucket
                return
            self._close_volume_bar(symbol, now, increment)
            self.last_bar_bucket[symbol] = bucket
        else:
            self.forming_vol[symbol] += increment


class StrategyBrain:
    def __init__(self, order_manager=None, order_engine=None, **kwargs):
        self.order_manager = order_manager or order_engine
        self.options_builders = kwargs.get("options_builders", {})
        self.db = kwargs.get("db_manager")
        if self.db is None and self.order_manager is not None:
            self.db = getattr(self.order_manager, "db_manager", None)
        self.risk = kwargs.get("risk_manager") or RiskManager(db_manager=self.db)

        self.price_histories = {symbol: [] for symbol in INDICES_CONFIG.keys()}
        self.closed_rsi = {symbol: [] for symbol in INDICES_CONFIG.keys()}
        self.last_candle_times = {symbol: 0.0 for symbol in INDICES_CONFIG.keys()}
        self.last_signal_buckets = {symbol: None for symbol in INDICES_CONFIG.keys()}
        self.cooldown_until = {symbol: 0.0 for symbol in INDICES_CONFIG.keys()}
        self.last_closed_rsi = {symbol: 50.0 for symbol in INDICES_CONFIG.keys()}
        self.current_regimes = {symbol: "INITIALIZING" for symbol in INDICES_CONFIG.keys()}
        # Set when a tick gap distorts the bar series; blocks entries until enough
        # clean bars have been rebuilt (see _bar_gap_bars).
        self.stale_bars = {symbol: 0 for symbol in INDICES_CONFIG.keys()}
        self.volume_gate = kwargs.get("volume_gate") or VolumeExpansionGate()

        self.state_file = "rsi_state.json"
        self._load_state()

    def _calculate_rsi(self, prices, period=14):
        return wilder_rsi(prices, period=period)

    def _calculate_ema(self, history, period):
        if len(history) < period:
            return sum(history) / len(history) if history else 0.0
        multiplier = 2 / (period + 1)
        ema = sum(history[:period]) / period
        for price in history[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def _save_state(self):
        try:
            today_str = ist_today()
            with open(self.state_file, "w") as f:
                json.dump({
                    "date": today_str,
                    "price_histories": self.price_histories,
                    "last_closed_rsi": self.last_closed_rsi,
                    "last_candle_times": self.last_candle_times,
                    "closed_rsi": self.closed_rsi,
                    "signal_bar_sec": signal_bar_sec(),
                }, f)
        except Exception as e:
            logging.error(f"❌ Error saving StrategyBrain state: {e}")

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        try:
            today_str = ist_today()
            with open(self.state_file, "r") as f:
                state = json.load(f)
            if state.get("date", "") != today_str:
                return
            # Drop histories built on a different / unknown bar size (e.g. old 1-min state).
            saved_bar = state.get("signal_bar_sec")
            if saved_bar is None or int(saved_bar) != signal_bar_sec():
                logging.info(
                    f"Ignoring rsi_state.json: bar size {saved_bar} != current {signal_bar_sec()}s"
                )
                return
            loaded = state.get("price_histories", {})
            for symbol in INDICES_CONFIG.keys():
                if symbol in loaded:
                    self.price_histories[symbol] = loaded[symbol]
            self.last_closed_rsi = state.get("last_closed_rsi", self.last_closed_rsi)
            loaded_rsi = state.get("closed_rsi") or {}
            for symbol in INDICES_CONFIG.keys():
                if symbol in loaded_rsi:
                    self.closed_rsi[symbol] = list(loaded_rsi[symbol])[-5:]
            # backward compat with older state key
            if "last_arsis" in state and "last_closed_rsi" not in state:
                self.last_closed_rsi = state.get("last_arsis", self.last_closed_rsi)
            # Restart must not treat the first tick as a closed bar.
            now = time.time()
            for symbol in INDICES_CONFIG.keys():
                self.last_candle_times[symbol] = now
                self.last_signal_buckets[symbol] = signal_bar_bucket(now)
        except Exception as e:
            logging.error(f"❌ Error loading StrategyBrain state: {e}")

    def _trigger_entry(self, symbol, spot_price, option_type, target_mult, sl_mult, entry_reason="RSI_HOOK"):
        try:
            if not self.order_manager:
                return False
            self.risk.refresh_from_db()
            if self.risk.trading_halted:
                return False

            config = INDICES_CONFIG.get(symbol, {})
            index_token = str(config.get("index_token"))
            builder = self.options_builders.get(index_token)
            if not builder:
                return False

            contract = builder.get_nearest_expiry_contract(spot_price, instrument_type=option_type)
            if not contract:
                return False

            opt_symbol = contract.get("symbol")
            opt_token = str(contract.get("token"))
            exchange = contract.get("exchange") or config.get("option_exchange") or (
                "BFO" if symbol == "SENSEX" else "NFO"
            )
            qty = int(contract.get("lotsize") or FALLBACK_LOT_SIZE.get(symbol, 0))
            if qty <= 0 or not opt_symbol or not opt_token:
                logging.error(f"[{symbol}] Missing lot/token for {opt_symbol}")
                return False

            ltp_resp = self.order_manager.smart_api.ltpData(exchange, opt_symbol, opt_token)
            if not (ltp_resp and ltp_resp.get("status") and ltp_resp.get("data")):
                return False
            opt_ltp = float(ltp_resp["data"]["ltp"])

            if not self.risk.assess_order_safety(
                {
                    "qty": qty,
                    "index_name": symbol,
                    "symbol": opt_symbol,
                    "entry_reason": entry_reason,
                },
                estimated_premium=opt_ltp,
            ):
                return False

            spread = contract.get("spread_pct")
            spread_s = f"{spread:.2f}%" if spread is not None else "n/a"
            target_price = round(opt_ltp * target_mult, 1)
            sl_price = round(opt_ltp * sl_mult, 1)
            logging.info(
                f"[{symbol}] ENTRY {entry_reason} {option_type} {opt_symbol} qty={qty} @ ₹{opt_ltp} "
                f"| T ₹{target_price} | SL ₹{sl_price} | DTE {contract.get('dte')} | spread {spread_s}"
            )
            order_id = self.order_manager.execute_entry(
                symbol=opt_symbol,
                token=opt_token,
                qty=qty,
                exchange=exchange,
                price=opt_ltp,
                target_price=target_price,
                stop_loss_price=sl_price,
                index_name=symbol,
                entry_reason=entry_reason,
                expiry=contract.get("expiry"),
                dte=contract.get("dte"),
                bid=contract.get("bid"),
                ask=contract.get("ask"),
                spread_pct=contract.get("spread_pct"),
            )
            return bool(order_id)
        except Exception as e:
            logging.error(f"❌ Entry failed for {symbol}: {e}")
            return False

    def _classify_regime(self, closed, config):
        """EMA + 20-bar mean. Full-session mean kept labels CHOPPY all morning."""
        ema_9 = self._calculate_ema(closed, 9)
        ema_21 = self._calculate_ema(closed, 21)
        lookback = int(config.get("regime_mean_bars", 20))
        window = closed[-lookback:] if len(closed) >= 2 else closed
        loc_mean = sum(window) / len(window) if window else 0.0
        ema_spread_min = float(config.get("ema_spread_min", 1.0))
        last_close = closed[-1]
        ema_up = ema_9 > (ema_21 + ema_spread_min)
        ema_dn = ema_9 < (ema_21 - ema_spread_min)
        # Hold the slow EMA; requiring last >= ema9 kept mild grinds in CHOPPY.
        if ema_up and last_close >= ema_21:
            trend = "BULLISH"
        elif ema_dn and last_close <= ema_21:
            trend = "BEARISH"
        else:
            trend = "CHOPPY"
        return trend, ema_9, ema_21, loc_mean

    def _try_volume_breakout(self, symbol, last_close, prev_close, macro_trend, config):
        if not RISK.get("enable_volume_breakout", True):
            return False
        if not self.volume_gate.has_fresh_breakout(symbol):
            return False
        up_bar = last_close > prev_close
        down_bar = last_close < prev_close
        allow_chop = RISK.get("enable_volume_breakout_in_chop", True)
        want_ce = (macro_trend == "BULLISH" or (macro_trend == "CHOPPY" and allow_chop)) and up_bar
        want_pe = (macro_trend == "BEARISH" or (macro_trend == "CHOPPY" and allow_chop)) and down_bar
        if not want_ce and not want_pe:
            self.volume_gate.consume_breakout(symbol)
            logging.info(
                f"[{symbol}] VOLUME_BREAKOUT skipped: bar direction does not match {macro_trend}."
            )
            return False
        side = "CE" if want_ce else "PE"
        logging.info(f"[{symbol}] VOLUME_BREAKOUT {'up' if want_ce else 'down'}-bar ({macro_trend}). Buying {side}.")
        ok = self._trigger_entry(
            symbol, last_close, side,
            config["trending_target_mult"], config["trending_sl_mult"],
            entry_reason="VOLUME_BREAKOUT",
        )
        if ok:
            self.volume_gate.consume_breakout(symbol)
        else:
            logging.warning(
                f"[{symbol}] VOLUME_BREAKOUT {side} not filled; keeping event for retry."
            )
        return bool(ok)

    def _volume_ok_for_reason(self, symbol, reason):
        """TREND_CONT needs expansion; RSI_HOOK may use the softer hook gate."""
        if reason == "TREND_CONT" and RISK.get("trend_cont_requires_expansion", True):
            return self.volume_gate.allows_expansion(symbol)
        return self.volume_gate.allows_entry(symbol)

    def _try_trend_entries(self, symbol, last_close, prev_close, macro_trend, config, last_rsi, current_rsi):
        recent_rsis = self.closed_rsi.get(symbol, [])
        rsi_dipped_bullish = any(r < 45 for r in recent_rsis)
        rsi_spiked_bearish = any(r > 55 for r in recent_rsis)
        cont_max = float(RISK.get("trend_cont_rsi_max", 68.0))

        if macro_trend == "BULLISH":
            hook = last_rsi < 50 and current_rsi >= 50 and rsi_dipped_bullish
            cont = last_close > prev_close and 50.0 <= current_rsi <= cont_max
            if not hook and not cont:
                return False
            reason = "RSI_HOOK" if hook else "TREND_CONT"
            if not self._volume_ok_for_reason(symbol, reason):
                snap = self.volume_gate.snapshot(symbol)
                need = "expansion" if reason == "TREND_CONT" else "hook"
                logging.info(f"[{symbol}] CE {reason} skipped ({need}): {snap['reason']}")
                return False
            logging.info(f"[{symbol}] Closed-bar CE ({reason}): trend up rsi={current_rsi:.1f}")
            return self._trigger_entry(
                symbol, last_close, "CE",
                config["trending_target_mult"], config["trending_sl_mult"],
                entry_reason=reason,
            )

        if macro_trend == "BEARISH":
            hook = last_rsi > 50 and current_rsi <= 50 and rsi_spiked_bearish
            cont = last_close < prev_close and (100.0 - cont_max) <= current_rsi <= 50.0
            if not hook and not cont:
                return False
            reason = "RSI_HOOK" if hook else "TREND_CONT"
            if not self._volume_ok_for_reason(symbol, reason):
                snap = self.volume_gate.snapshot(symbol)
                need = "expansion" if reason == "TREND_CONT" else "hook"
                logging.info(f"[{symbol}] PE {reason} skipped ({need}): {snap['reason']}")
                return False
            logging.info(f"[{symbol}] Closed-bar PE ({reason}): trend down rsi={current_rsi:.1f}")
            return self._trigger_entry(
                symbol, last_close, "PE",
                config["trending_target_mult"], config["trending_sl_mult"],
                entry_reason=reason,
            )

        if RISK.get("enable_choppy_entries") and macro_trend == "CHOPPY":
            if not self.volume_gate.allows_entry(symbol):
                return False
            if last_rsi < 80 and current_rsi >= 80:
                return self._trigger_entry(
                    symbol, last_close, "PE",
                    config["choppy_target_mult"], config["choppy_sl_mult"],
                    entry_reason="RSI_HOOK",
                )
            if last_rsi > 20 and current_rsi <= 20:
                return self._trigger_entry(
                    symbol, last_close, "CE",
                    config["choppy_target_mult"], config["choppy_sl_mult"],
                    entry_reason="RSI_HOOK",
                )
        return False

    def evaluate_tick(self, symbol, spot_price, option_volume=None):
        """Update forming signal bar on every tick; entries only on closed signal bars (5-min)."""
        if symbol not in INDICES_CONFIG:
            return
        current_time = time.time()
        current_hour_min = ist_hhmm()
        bar_sec = signal_bar_sec()
        bucket = signal_bar_bucket(current_time)

        history = self.price_histories.get(symbol, [])
        state_changed = False
        closed_bar = False

        last_candle_time = self.last_candle_times.get(symbol, 0.0)
        last_bucket = self.last_signal_buckets.get(symbol)
        bucket_rolled = last_bucket is not None and bucket != last_bucket
        elapsed_rolled = current_time - last_candle_time >= bar_sec
        if bucket_rolled or elapsed_rolled:
            # Missed buckets mean the appended close is not one bar after the last
            # one; EMA/RSI spacing is distorted until clean bars replace the gap.
            missed = int((current_time - last_candle_time) // bar_sec) - 1
            if missed > 0 and last_candle_time > 0.0:
                self.stale_bars[symbol] = min(
                    MAX_STALE_BARS, self.stale_bars.get(symbol, 0) + missed
                )
                logging.warning(
                    f"[{symbol}] Signal bar gap: {missed} bar(s) missed. "
                    f"Entries paused for {self.stale_bars[symbol]} clean bar(s)."
                )
            if history:
                closed_bar = True
            history.append(spot_price)
            # ~1 trading day of signal bars with headroom
            max_bars = max(80, 375 * 60 // bar_sec)
            if len(history) > max_bars:
                history.pop(0)
            self.last_candle_times[symbol] = current_time
            self.last_signal_buckets[symbol] = bucket
            state_changed = True
        elif len(history) == 0:
            history.append(spot_price)
            self.last_candle_times[symbol] = current_time
            self.last_signal_buckets[symbol] = bucket
            state_changed = True
        else:
            history[-1] = spot_price

        self.price_histories[symbol] = history
        if len(history) < 22:
            if state_changed:
                self._save_state()
            return

        # Regime uses closed bars only so the forming candle cannot flicker entries
        closed = history[:-1]
        current_rsi = self._calculate_rsi(closed)
        last_rsi = self.last_closed_rsi.get(symbol, 50.0)

        config = INDICES_CONFIG[symbol]
        last_close = closed[-1]
        prev_close = closed[-2] if len(closed) >= 2 else last_close
        macro_trend, ema_9, ema_21, loc_mean = self._classify_regime(closed, config)
        self.current_regimes[symbol] = macro_trend

        if closed_bar:
            rsi_bucket = self.closed_rsi.setdefault(symbol, [])
            rsi_bucket.append(current_rsi)
            if len(rsi_bucket) > 5:
                rsi_bucket.pop(0)

        if current_hour_min < RISK["session_start_hhmm"] or current_hour_min >= RISK["entry_cutoff_hhmm"]:
            if closed_bar:
                self.last_closed_rsi[symbol] = current_rsi
                self._save_state()
            return

        if current_time < self.cooldown_until.get(symbol, 0.0):
            if closed_bar:
                self.last_closed_rsi[symbol] = current_rsi
            return

        if not closed_bar:
            return

        if self.stale_bars.get(symbol, 0) > 0:
            self.stale_bars[symbol] -= 1
            logging.info(
                f"[{symbol}] Entry paused: rebuilding after feed gap "
                f"({self.stale_bars[symbol]} bar(s) left)."
            )
            self.last_closed_rsi[symbol] = current_rsi
            self._save_state()
            return

        bar_m = max(1, bar_sec // 60)
        logging.info(
            f"[{symbol}] Closed {bar_m}m bar {macro_trend} px={last_close:.2f} "
            f"ema9={ema_9:.1f} ema21={ema_21:.1f} mean20={loc_mean:.1f} rsi={current_rsi:.1f} "
            f"| Vol {self.volume_gate.snapshot(symbol)['reason']}"
        )
        fired = self._try_volume_breakout(symbol, last_close, prev_close, macro_trend, config)
        if not fired:
            fired = self._try_trend_entries(
                symbol, last_close, prev_close, macro_trend, config,
                last_rsi, current_rsi,
            )

        if fired:
            self.cooldown_until[symbol] = time.time() + 900

        self.last_closed_rsi[symbol] = current_rsi
        if state_changed:
            self._save_state()
