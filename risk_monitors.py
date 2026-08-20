import logging
import time
import threading
from datetime import datetime
from config import FALLBACK_LOT_SIZE, PAPER_TRADING, RISK


def _row_get(row, key, index=None, default=None):
    try:
        if hasattr(row, "keys") and key in row.keys():
            val = row[key]
            return default if val is None else val
    except Exception:
        pass
    if index is not None:
        try:
            val = row[index]
            return default if val is None else val
        except Exception:
            return default
    return default


def _qty_and_exchange(row):
    symbol = str(_row_get(row, "symbol", 1, "") or "")
    qty = _row_get(row, "qty", None, None)
    exchange = _row_get(row, "exchange", None, None)
    if not qty:
        qty = FALLBACK_LOT_SIZE["SENSEX"] if symbol.startswith("SENSEX") else FALLBACK_LOT_SIZE["NIFTY"]
        if "BANKNIFTY" in symbol.upper():
            qty = FALLBACK_LOT_SIZE["BANKNIFTY"]
    if not exchange:
        exchange = "BFO" if symbol.startswith("SENSEX") else "NFO"
    return int(qty), exchange


class TrailingStopLossMonitor(threading.Thread):
    def __init__(self, db_manager, smart_api=None, order_engine=None, interval=5):
        super().__init__()
        self.db = db_manager
        self.smart_api = smart_api
        self.order_engine = order_engine
        self.interval = interval
        self.daemon = True
        self._running = True

    def run(self):
        logging.info("[TrailingStopLossMonitor] Background thread started.")
        while self._running:
            try:
                self.check_and_update_stops()
            except Exception as e:
                logging.error(f"❌ Error in TrailingStopLossMonitor loop: {e}")
            time.sleep(self.interval)

    def check_and_update_stops(self):
        try:
            open_trades = self.db.fetch_all(
                """
                SELECT id, symbol, token, entry_price, target_price, stop_loss_price,
                       peak_price, qty, exchange, timestamp, entry_time
                FROM trades WHERE status = 'OPEN'
                """
            )
            if not open_trades:
                return

            time_stop_min = RISK.get("time_stop_minutes", 25)
            min_gain = RISK.get("time_stop_min_gain_mult", 1.02)

            for trade in open_trades:
                trade_id = _row_get(trade, "id", 0)
                symbol = _row_get(trade, "symbol", 1)
                token = _row_get(trade, "token", 2)
                entry_price = float(_row_get(trade, "entry_price", 3, 0) or 0)
                target_price = float(_row_get(trade, "target_price", 4, 0) or 0)
                sl_price = float(_row_get(trade, "stop_loss_price", 5, 0) or 0)
                peak_price = float(_row_get(trade, "peak_price", 6, entry_price) or entry_price)
                qty, exchange = _qty_and_exchange(trade)
                entry_ts = str(_row_get(trade, "entry_time", None, "") or _row_get(trade, "timestamp", None, "") or "")

                current_price = entry_price
                if self.smart_api and token:
                    try:
                        resp = self.smart_api.ltpData(exchange, symbol, token)
                        if resp and resp.get("status"):
                            current_price = float(resp["data"]["ltp"])
                    except Exception:
                        continue

                if target_price and current_price >= target_price:
                    self.close_trade(trade_id, symbol, token, qty, exchange, current_price, "TARGET_HIT")
                    continue
                if sl_price and current_price <= sl_price:
                    self.close_trade(trade_id, symbol, token, qty, exchange, current_price, "STOP_LOSS_HIT")
                    continue

                if self._time_stop_hit(entry_ts, current_price, entry_price, min_gain, time_stop_min):
                    self.close_trade(trade_id, symbol, token, qty, exchange, current_price, "TIME_STOP")
                    continue

                if current_price > peak_price or current_price >= entry_price * 1.04:
                    new_peak = max(peak_price, current_price)
                    new_sl = sl_price
                    if current_price >= entry_price * 1.15:
                        locked = entry_price + 0.50 * (new_peak - entry_price)
                        new_sl = max(sl_price, locked)
                    elif current_price >= entry_price * 1.08:
                        new_sl = max(sl_price, entry_price * 1.02)
                    elif current_price >= entry_price * 1.04:
                        new_sl = max(sl_price, entry_price)
                    self.db.update_trailing_stoploss(trade_id, new_sl, new_peak)
        except Exception as e:
            logging.error(f"❌ Failed checking/updating stop losses: {e}")

    def _time_stop_hit(self, entry_ts, current_price, entry_price, min_gain, minutes):
        if not entry_ts or not entry_price:
            return False
        try:
            entered = datetime.strptime(entry_ts[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        age = (datetime.now() - entered).total_seconds() / 60.0
        return age >= minutes and current_price < entry_price * min_gain

    def close_trade(self, trade_id, symbol, token, qty, exchange, exit_price, reason):
        if self.order_engine:
            ok = self.order_engine.execute_exit(
                trade_id, symbol, token, qty, exchange, exit_price, reason=reason
            )
            if ok:
                logging.info(f"[TRADE CLOSED] {symbol} | Exit ₹{exit_price:.2f} | {reason}")
            return
        self.db.close_trade(trade_id, exit_price, reason)
        logging.warning(f"[TRADE CLOSED DB-ONLY] {symbol} | {reason} — no order_engine wired")

    def stop(self):
        self._running = False


class TradeReconciler:
    def __init__(self, smart_api, db_manager, paper_trading=None):
        self.smart_api = smart_api
        self.db = db_manager
        self.paper_trading = PAPER_TRADING if paper_trading is None else paper_trading

    def reconcile(self):
        try:
            if self.paper_trading:
                logging.info("Skipping broker reconcile in paper mode (no live netqty expected).")
                return
            if not self.smart_api:
                return

            positions_resp = self.smart_api.position()
            if not positions_resp or not positions_resp.get("status"):
                return

            broker_data = positions_resp.get("data")
            if not broker_data:
                broker_data = []

            active_symbols = {
                p.get("tradingsymbol") for p in broker_data if float(p.get("netqty", 0) or 0) != 0
            }

            db_trades = self.db.fetch_all("SELECT id, symbol FROM trades WHERE status = 'OPEN'")
            for trade in db_trades:
                trade_id = _row_get(trade, "id", 0)
                symbol = _row_get(trade, "symbol", 1)
                if symbol not in active_symbols:
                    self.db.close_trade(trade_id, 0.0, "RECONCILED_CLOSED")
                    logging.warning(
                        f"[RECONCILIATION] {symbol} (ID: {trade_id}) closed externally at broker."
                    )
        except Exception as e:
            logging.error(f"❌ Error during trade reconciliation: {e}")
