import time
import logging

INTRADAY_PRODUCT = "INTRADAY"


class OrderExecutionEngine:
    def __init__(self, smart_api, db_manager, scrip_master=None, paper_trading=True, **kwargs):
        self.smart_api = smart_api
        self.db_manager = db_manager
        self.scrip_master = scrip_master
        self.paper_trading = paper_trading

    def _fetch_ltp(self, exchange, symbol, token, fallback=0.0):
        if not self.smart_api or not token:
            return float(fallback or 0.0)
        try:
            ltp_resp = self.smart_api.ltpData(exchange, symbol, str(token))
            if ltp_resp and ltp_resp.get("status") and ltp_resp.get("data"):
                return float(ltp_resp["data"]["ltp"])
        except Exception as e:
            logging.error(f"❌ Could not fetch LTP for {symbol}: {e}")
        return float(fallback or 0.0)

    def _place_live_order(self, symbol, token, qty, trans_type, exchange, order_type, price):
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": str(token),
            "transactiontype": trans_type,
            "exchange": exchange,
            "ordertype": order_type,
            "producttype": INTRADAY_PRODUCT,
            "duration": "DAY",
            "price": price if order_type == "LIMIT" else 0,
            "quantity": int(qty),
        }
        retries = 3
        for attempt in range(retries):
            try:
                order_id = self.smart_api.placeOrder(order_params)
                if order_id:
                    return order_id
            except Exception as e:
                if "rate limit" in str(e).lower():
                    logging.warning(f"⚠️ Rate limit hit. Retrying in {2 ** attempt}s...")
                    time.sleep(2 ** attempt)
                else:
                    logging.error(f"❌ Live order error for {symbol}: {e}")
                    return None
        logging.error(f"❌ Failed to place live order for {symbol} after {retries} attempts.")
        return None

    def execute_entry(
        self,
        symbol,
        token,
        qty,
        exchange="NFO",
        price=0.0,
        target_price=0.0,
        stop_loss_price=0.0,
        index_name=None,
        order_type="MARKET",
    ):
        qty = int(qty)
        if qty <= 0:
            logging.error(f"❌ Refusing entry for {symbol}: invalid qty {qty}")
            return None

        logging.info(
            f"⚙️ ENTRY BUY {qty}x {symbol} ({exchange}) | Paper={self.paper_trading}"
        )
        fill_price = price if price > 0 else self._fetch_ltp(exchange, symbol, token, price)

        if self.paper_trading:
            time.sleep(0.05)
            order_id = f"mock_buy_{int(time.time())}"
            trade_id = self.db_manager.log_trade(
                symbol, token, fill_price, target_price, stop_loss_price,
                qty=qty, exchange=exchange, index_name=index_name,
            )
            logging.info(f"✅ [PAPER] BUY {symbol} @ ₹{fill_price} | db#{trade_id} | {order_id}")
            return order_id

        order_id = self._place_live_order(symbol, token, qty, "BUY", exchange, order_type, fill_price)
        if not order_id:
            return None
        fill_price = self._fetch_ltp(exchange, symbol, token, fill_price)
        trade_id = self.db_manager.log_trade(
            symbol, token, fill_price, target_price, stop_loss_price,
            qty=qty, exchange=exchange, index_name=index_name,
        )
        logging.info(f"✅ [LIVE] BUY {symbol} @ ₹{fill_price} | db#{trade_id} | {order_id}")
        return order_id

    def execute_exit(
        self,
        trade_id,
        symbol,
        token,
        qty,
        exchange="NFO",
        price=0.0,
        reason="EXIT",
        order_type="MARKET",
    ):
        qty = int(qty) if qty else 0
        if qty <= 0:
            logging.error(f"❌ Refusing exit for {symbol}: invalid qty {qty}")
            return False

        logging.info(
            f"⚙️ EXIT SELL {qty}x {symbol} ({exchange}) | reason={reason} | Paper={self.paper_trading}"
        )
        fill_price = price if price > 0 else self._fetch_ltp(exchange, symbol, token, price)

        if self.paper_trading:
            time.sleep(0.05)
            closed = self.db_manager.close_trade(trade_id, fill_price, reason)
            logging.info(f"✅ [PAPER] SELL {symbol} @ ₹{fill_price} | closed={closed}")
            return closed

        order_id = self._place_live_order(symbol, token, qty, "SELL", exchange, order_type, fill_price)
        if not order_id:
            logging.error(f"❌ Broker SELL failed for {symbol}; leaving trade #{trade_id} OPEN.")
            return False
        fill_price = self._fetch_ltp(exchange, symbol, token, fill_price)
        closed = self.db_manager.close_trade(trade_id, fill_price, reason)
        logging.info(f"✅ [LIVE] SELL {symbol} @ ₹{fill_price} | {order_id} | closed={closed}")
        return closed

    def execute_order(self, symbol, token, qty, trans_type="BUY", exchange="NFO",
                      order_type="MARKET", product_type=None, price=0.0,
                      target_price=0.0, stop_loss_price=0.0, trade_id=None,
                      index_name=None, reason="MANUAL"):
        """Backward-compatible wrapper. SELL without trade_id is refused."""
        if str(trans_type).upper() == "BUY":
            return self.execute_entry(
                symbol, token, qty, exchange, price, target_price, stop_loss_price,
                index_name=index_name, order_type=order_type,
            )
        if trade_id is None:
            logging.error("❌ SELL refused: pass trade_id so the OPEN row is updated, not duplicated.")
            return None
        return self.execute_exit(
            trade_id, symbol, token, qty, exchange, price, reason=reason, order_type=order_type,
        )
