import time
import logging

from broker_orders import confirm_fill

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
        entry_reason=None,
        expiry=None,
        dte=None,
        bid=None,
        ask=None,
        spread_pct=None,
    ):
        qty = int(qty)
        if qty <= 0:
            logging.error(f"❌ Refusing entry for {symbol}: invalid qty {qty}")
            return None

        logging.info(
            f"⚙️ ENTRY BUY {qty}x {symbol} ({exchange}) | Paper={self.paper_trading}"
            f"{' | ' + entry_reason if entry_reason else ''}"
        )
        fill_price = price if price > 0 else self._fetch_ltp(exchange, symbol, token, price)

        if self.paper_trading:
            time.sleep(0.05)
            order_id = f"mock_buy_{int(time.time())}"
            trade_id = self.db_manager.log_trade(
                symbol, token, fill_price, target_price, stop_loss_price,
                qty=qty, exchange=exchange, index_name=index_name,
                entry_reason=entry_reason, expiry=expiry, dte=dte,
                intended_price=price, slippage=0.0,
                bid=bid, ask=ask, spread_pct=spread_pct,
            )
            logging.info(f"✅ [PAPER] BUY {symbol} @ ₹{fill_price} | db#{trade_id} | {order_id}")
            return order_id

        intended = fill_price
        order_id = self._place_live_order(symbol, token, qty, "BUY", exchange, order_type, fill_price)
        if not order_id:
            return None

        # Never log a position the broker did not actually give us.
        fill = confirm_fill(self.smart_api, order_id)
        if not fill.is_filled:
            if fill.is_dead:
                logging.error(f"❌ [LIVE] BUY {symbol} {fill.status.upper()} ({order_id}); no position opened.")
            else:
                logging.critical(
                    f"⚠️ [LIVE] BUY {symbol} order {order_id} is {fill.status.upper()} — "
                    "not recorded. If it fills later you will hold an UNTRACKED position. "
                    "Check the broker terminal now."
                )
            return None

        fill_price = fill.avg_price
        if fill.filled_qty != qty:
            logging.warning(
                f"⚠️ [LIVE] BUY {symbol} partial fill {fill.filled_qty}/{qty}; "
                "recording the filled quantity."
            )
            qty = fill.filled_qty
        slippage = fill_price - intended
        # Targets were computed off the intended price; re-derive from the real fill.
        if intended > 0:
            target_price = round(target_price / intended * fill_price, 1)
            stop_loss_price = round(stop_loss_price / intended * fill_price, 1)

        trade_id = self.db_manager.log_trade(
            symbol, token, fill_price, target_price, stop_loss_price,
            qty=qty, exchange=exchange, index_name=index_name,
            entry_reason=entry_reason, expiry=expiry, dte=dte,
            intended_price=intended, slippage=slippage,
            bid=bid, ask=ask, spread_pct=spread_pct,
        )
        logging.info(
            f"✅ [LIVE] BUY {symbol} @ ₹{fill_price} (intended ₹{intended}, "
            f"slip ₹{slippage:+.2f}) | db#{trade_id} | {order_id}"
        )
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
        if fill_price <= 0:
            logging.error(
                f"❌ Refusing exit for {symbol} at ₹{fill_price} ({reason}); leaving OPEN."
            )
            return False

        if self.paper_trading:
            time.sleep(0.05)
            closed = self.db_manager.close_trade(trade_id, fill_price, reason)
            logging.info(f"✅ [PAPER] SELL {symbol} @ ₹{fill_price} | closed={closed}")
            return closed

        intended = fill_price
        order_id = self._place_live_order(symbol, token, qty, "SELL", exchange, order_type, fill_price)
        if not order_id:
            logging.error(f"❌ Broker SELL failed for {symbol}; leaving trade #{trade_id} OPEN.")
            return False

        fill = confirm_fill(self.smart_api, order_id)
        if not fill.is_filled:
            # Leaving the row OPEN is deliberate: the monitors will retry, and EOD
            # square-off is the backstop. Booking a close we did not get is worse.
            logging.critical(
                f"⚠️ [LIVE] SELL {symbol} order {order_id} is {fill.status.upper()}; "
                f"trade #{trade_id} left OPEN for retry."
            )
            return False

        fill_price = fill.avg_price
        if fill.filled_qty != qty:
            logging.critical(
                f"⚠️ [LIVE] SELL {symbol} partial exit {fill.filled_qty}/{qty}; "
                f"trade #{trade_id} left OPEN — residual position must be squared manually."
            )
            return False

        closed = self.db_manager.close_trade(trade_id, fill_price, reason)
        logging.info(
            f"✅ [LIVE] SELL {symbol} @ ₹{fill_price} (intended ₹{intended}, "
            f"slip ₹{fill_price - intended:+.2f}) | {order_id} | closed={closed}"
        )
        return closed

    def execute_order(self, symbol, token, qty, trans_type="BUY", exchange="NFO",
                      order_type="MARKET", product_type=None, price=0.0,
                      target_price=0.0, stop_loss_price=0.0, trade_id=None,
                      index_name=None, reason="MANUAL"):
        """Backward-compatible wrapper. SELL without trade_id is refused."""
        if str(trans_type).upper() == "BUY":
            return self.execute_entry(
                symbol, token, qty, exchange, price, target_price, stop_loss_price,
                index_name=index_name, order_type=order_type, entry_reason=reason,
            )
        if trade_id is None:
            logging.error("❌ SELL refused: pass trade_id so the OPEN row is updated, not duplicated.")
            return None
        return self.execute_exit(
            trade_id, symbol, token, qty, exchange, price, reason=reason, order_type=order_type,
        )
