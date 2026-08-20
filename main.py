# main.py - AI_FNOBot (Index F&O Framework)
import os
import time
import json
import logging
import threading
import traceback
from datetime import datetime, timedelta
from dotenv import load_dotenv

import websocket
from SmartApi import SmartConnect
import pyotp

from config import ACTIVE_INDICES, INDICES_CONFIG, PAPER_TRADING, RISK
from database import DatabaseManager
from options_chain_builder import DynamicOptionsChainBuilder
from order_execution import OrderExecutionEngine
from strategy_brain import StrategyBrain
from risk_monitors import TrailingStopLossMonitor, TradeReconciler
from rate_limiter import RateLimitedAPI

# Disable raw binary frame tracing to prevent terminal flooding
websocket.enableTrace(False)

# Setup unified logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("trading_bot.log"),
        logging.StreamHandler()
    ]
)

load_dotenv()

# Global state tracker for Heartbeat Monitor
latest_market_state = {
    index: {"spot_price": None, "last_tick_time": None}
    for index in ACTIVE_INDICES
}

class SystemHeartbeatMonitor(threading.Thread):
    """Background thread to print system health, spot prices, market regime, and trade counts."""
    def __init__(self, db_manager, strategy_brain, interval=60):
        super().__init__()
        self.db_manager = db_manager
        self.strategy_brain = strategy_brain
        self.interval = interval
        self.running = True
        self.daemon = True

    def run(self):
        while self.running:
            for _ in range(self.interval):
                if not self.running:
                    return
                time.sleep(1)
                
            try:
                today_str = datetime.now().strftime("%Y-%m-%d")
                status_summary = []

                for index in ACTIVE_INDICES:
                    trades = self.get_trade_count_today(index, today_str)
                    spot = latest_market_state[index].get("spot_price")
                    spot_str = f"₹{spot:.2f}" if spot else "Waiting/Offline..."
                    
                    try:
                        if hasattr(self.strategy_brain, 'current_regimes'):
                            regime = self.strategy_brain.current_regimes.get(index, "INITIALIZING")
                        else:
                            regime = "INITIALIZING"
                    except Exception:
                        regime = "INITIALIZING"

                    status_summary.append(f"[{index}] Spot: {spot_str} | Regime: {regime} | Trades Today: {trades}")

                from scorecard import heartbeat_line
                logging.info("💓 [SYSTEM STATUS] " + " || ".join(status_summary) + " || " + heartbeat_line(self.db_manager))
            except Exception as e:
                logging.error(f"❌ Error in Heartbeat Monitor: {e}")

    def get_trade_count_today(self, index_symbol, date_str):
        try:
            query = "SELECT COUNT(*) FROM trades WHERE symbol LIKE ? AND DATE(timestamp) = ?"
            result = self.db_manager.fetch_one(query, (f"%{index_symbol}%", date_str))
            return result[0] if result else 0
        except Exception:
            return 0

    def stop(self):
        self.running = False


class EndOfDaySquareOffMonitor(threading.Thread):
    """Force square-off of OPEN rows at 3:15 PM via the same exit path as TSL."""
    def __init__(self, smart_api, order_engine, db_manager, interval=10):
        super().__init__()
        self.smart_api = smart_api
        self.order_engine = order_engine
        self.db_manager = db_manager
        self.interval = interval
        self.running = True
        self.daemon = True
        self.has_squared_off_today = False

    def run(self):
        from risk_monitors import _qty_and_exchange, _row_get

        while self.running:
            for _ in range(self.interval):
                if not self.running:
                    return
                time.sleep(1)

            try:
                now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
                current_hour_min = now_ist.hour * 100 + now_ist.minute

                if current_hour_min < 900:
                    self.has_squared_off_today = False

                eod_hhmm = RISK.get("eod_squareoff_hhmm", 1515)
                if current_hour_min >= eod_hhmm and not self.has_squared_off_today:
                    logging.warning("⏰ [KILL SWITCH] EOD reached. Squaring off OPEN trades.")
                    open_trades = self.db_manager.fetch_all(
                        """
                        SELECT id, symbol, token, qty, exchange
                        FROM trades WHERE status = 'OPEN'
                        """
                    )
                    if not open_trades:
                        logging.info("✅ [KILL SWITCH] No open trades to square off.")
                        self.has_squared_off_today = True
                        continue

                    for trade in open_trades:
                        trade_id = _row_get(trade, "id", 0)
                        symbol = _row_get(trade, "symbol", 1)
                        token = _row_get(trade, "token", 2)
                        qty, exchange = _qty_and_exchange(trade)
                        exit_price = 0.0
                        try:
                            ltp_resp = self.smart_api.ltpData(exchange, symbol, token)
                            if ltp_resp and ltp_resp.get("status") and ltp_resp.get("data"):
                                exit_price = float(ltp_resp["data"]["ltp"])
                        except Exception as e:
                            logging.error(f"❌ [KILL SWITCH] Could not fetch LTP for {symbol}: {e}")

                        ok = self.order_engine.execute_exit(
                            trade_id, symbol, token, qty, exchange, exit_price, reason="EOD_SQUAREOFF"
                        )
                        if ok:
                            logging.info(f"🛑 [KILL SWITCH] Closed {symbol} at ~₹{exit_price}")
                        else:
                            logging.error(f"❌ [KILL SWITCH] Exit failed for {symbol}; left OPEN")

                    self.has_squared_off_today = True
            except Exception as e:
                logging.error(f"❌ Error in EOD Square Off Monitor: {e}")

    def stop(self):
        self.running = False


def authenticate_broker():
    try:
        api_key = os.getenv("SMART_API_KEY") or os.getenv("SMARTAPI_KEY")
        client_id = os.getenv("CLIENT_ID")
        pwd = os.getenv("PASSWORD") or os.getenv("PIN")
        totp_key = os.getenv("TOTP_SECRET")

        if not all([api_key, client_id, pwd, totp_key]):
            logging.error("❌ Missing broker credentials in .env file.")
            return None

        smart_api = SmartConnect(api_key=api_key)
        totp_gen = pyotp.TOTP(totp_key).now()
        
        data = smart_api.generateSession(client_id, pwd, totp_gen)
        if data and data.get('status'):
            logging.info("🔐 Successfully authenticated with SmartAPI using TOTP.")
            rate_limited_api = RateLimitedAPI(smart_api, max_calls_per_sec=1)
            logging.info("🛡️ [FNO Bot] API Rate Limiter engaged (Max 1 call/sec).")
            return rate_limited_api
        else:
            logging.error(f"❌ Authentication failed: {data}")
            return None
    except Exception as e:
        logging.error(f"❌ Critical error during broker authentication: {e}")
        return None


def load_scrip_master_cache():
    if os.path.exists('scrip_master.json'):
        try:
            with open('scrip_master.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                logging.info(f"📁 Loaded scrip master cache ({len(data)} tokens).")
                return data
        except Exception as e:
            logging.error(f"❌ Failed to load scrip_master.json: {e}")
    return []


def main():
    logging.info(f"Initializing Multi-Index Framework for: {ACTIVE_INDICES}")
    
    db_manager = DatabaseManager('trade_history.db')
    smart_api = authenticate_broker()
    scrip_master = load_scrip_master_cache()
    
    order_engine = OrderExecutionEngine(
        smart_api=smart_api,
        db_manager=db_manager,
        scrip_master=scrip_master,
        paper_trading=PAPER_TRADING
    )
    
    options_builders = {}
    for symbol in ACTIVE_INDICES:
        cfg = INDICES_CONFIG[symbol]
        token = cfg["index_token"]
        builder = DynamicOptionsChainBuilder(index_name=symbol, smart_api=smart_api)
        builder.load_scrip_master(scrip_master)
        options_builders[token] = builder

    strategy_brain = StrategyBrain(
        order_engine=order_engine,
        options_builders=options_builders,
        scrip_master_data=scrip_master,
        db_manager=db_manager,
    )

    fut_token_to_index = {}
    fut_subscriptions = []
    for symbol in ACTIVE_INDICES:
        cfg = INDICES_CONFIG[symbol]
        builder = options_builders.get(str(cfg["index_token"]))
        fut = builder.get_nearest_expiry_future() if builder else None
        if fut and fut.get("token"):
            fut_token_to_index[str(fut["token"])] = symbol
            fut_subscriptions.append({
                "exchangeType": int(cfg.get("fut_exchange_type") or fut.get("exchange_type") or 2),
                "tokens": [str(fut["token"])],
            })
            strategy_brain.volume_gate.mark_subscribed(symbol, True)
            logging.info(f"Volume gate subscribed {symbol} via {fut['symbol']} token={fut['token']}")
        else:
            strategy_brain.volume_gate.mark_subscribed(symbol, False)
            if RISK.get("require_volume_expansion", True):
                logging.error(f"Volume gate: no future for {symbol}. New entries blocked until resolved.")
    
    tsl_monitor = TrailingStopLossMonitor(
        db_manager=db_manager,
        smart_api=smart_api,
        order_engine=order_engine,
        interval=5,
    )
    tsl_monitor.start()
    logging.info("🛡️ Trailing Stop-Loss Exit Monitor active.")

    heartbeat_monitor = SystemHeartbeatMonitor(
        db_manager=db_manager, 
        strategy_brain=strategy_brain, 
        interval=60
    )
    heartbeat_monitor.start()
    logging.info("💓 System Heartbeat Monitor active.")

    eod_monitor = EndOfDaySquareOffMonitor(
        smart_api=smart_api,
        order_engine=order_engine,
        db_manager=db_manager,
        interval=10,
    )
    eod_monitor.start()
    logging.info("⏰ 3:15 PM EOD Kill Switch Monitor active.")

    if smart_api:
        logging.info("Starting broker reconciliation...")
        try:
            reconciler = TradeReconciler(
                smart_api=smart_api,
                db_manager=db_manager,
                paper_trading=PAPER_TRADING,
            )
            reconciler.reconcile()
            logging.info("✅ Broker reconciliation complete.")
        except Exception as e:
            logging.error(f"❌ Error during trade reconciliation block: {e}")

    logging.info("✅ Multi-Index Framework fully operational.")

    if smart_api:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        
        client_code = os.getenv("CLIENT_ID")
        feed_token = os.getenv("FEED_TOKEN")
        api_key = os.getenv("SMART_API_KEY") or os.getenv("SMARTAPI_KEY")
        
        raw_api = smart_api.api if hasattr(smart_api, 'api') else smart_api
        
        if not feed_token or feed_token in ["your_feed_token_here", "placeholder"]:
            try:
                feed_resp = raw_api.getfeedToken()
                if isinstance(feed_resp, dict):
                    feed_token = feed_resp.get('data') or feed_resp.get('feedToken')
                else:
                    feed_token = feed_resp
                logging.info("🔑 Feed token generated dynamically from active session.")
            except Exception as e:
                logging.error(f"❌ Failed to fetch feed token: {e}")
        
        if not feed_token and hasattr(raw_api, 'feedToken'):
            feed_token = raw_api.feedToken

        if client_code and feed_token:
            access_token = raw_api.access_token

            sws = SmartWebSocketV2(
                auth_token=access_token,
                api_key=api_key,
                client_code=client_code,
                feed_token=feed_token
            )
            
            last_log_per_token = {}

            def on_data(*args):
                try:
                    message = args[1] if len(args) > 1 else args[0]

                    if isinstance(message, str):
                        try:
                            message = json.loads(message)
                        except Exception:
                            pass

                    if not isinstance(message, dict):
                        return

                    raw_token = str(message.get('token') or message.get('exchangeToken') or '')
                    token = raw_token.replace('\x00', '').strip()

                    ltp_raw = message.get('last_traded_price') or message.get('ltp')
                    vol_today = message.get("volume_trade_for_the_day") or message.get("vol_traded_today")
                    last_qty = message.get("last_traded_quantity") or message.get("last_trade_qty")

                    if token in fut_token_to_index:
                        idx = fut_token_to_index[token]
                        vt = float(vol_today) if vol_today is not None else None
                        lq = float(last_qty) if last_qty is not None else None
                        strategy_brain.volume_gate.on_fut_tick(idx, volume_traded_today=vt, last_traded_qty=lq)
                    
                    if ltp_raw is not None:
                        spot_price = float(ltp_raw) / 100.0 if float(ltp_raw) > 1000000 else float(ltp_raw)
                        current_time = time.time()
                        
                        for sym, cfg in INDICES_CONFIG.items():
                            if str(cfg["index_token"]) == token:
                                latest_market_state[sym]["spot_price"] = spot_price
                                latest_market_state[sym]["last_tick_time"] = current_time

                                if token not in last_log_per_token or (current_time - last_log_per_token[token] > 30):
                                    logging.info(f"📈 Tick Received - [{sym}] Token: {token} | Spot LTP: ₹{spot_price:.2f}")
                                    last_log_per_token[token] = current_time

                                strategy_brain.evaluate_tick(sym, spot_price)
                                
                except Exception as ex:
                    logging.error(f"❌ Error processing websocket tick: {ex}")

            def on_open(ws):
                logging.info("🔌 Live WebSocket Connection Established. Subscribing tokens...")
                token_list = []
                for sym in ACTIVE_INDICES:
                    cfg = INDICES_CONFIG[sym]
                    exch_type = cfg.get("exchange_type")
                    if exch_type is None:
                        exch_str = str(cfg.get("exchange", "NSE")).upper()
                        exch_type = 3 if exch_str == "BSE" else 1
                    token_list.append({"exchangeType": exch_type, "tokens": [str(cfg["index_token"])]})
                
                sws.subscribe("corrid_multindex", 1, token_list)
                logging.info(f"📡 Subscribed to indices: {ACTIVE_INDICES} with payload: {token_list}")
                if fut_subscriptions:
                    sws.subscribe("corrid_futures_vol", 2, fut_subscriptions)
                    logging.info(f"📡 Subscribed to index futures (quote/volume): {fut_subscriptions}")
                elif RISK.get("require_volume_expansion", True):
                    logging.error("No futures subscriptions; volume gate will block entries.")

            def on_error(ws, error=None):
                err_msg = error if error is not None else ws
                logging.error(f"❌ WebSocket Error: {err_msg}")

            def on_close(ws, close_code=None, close_reason=None):
                logging.warning(f"⚠️ WebSocket Connection Closed. Code: {close_code}, Reason: {close_reason}")

            sws.on_open = on_open
            sws.on_data = on_data
            sws.on_error = on_error
            sws.on_close = on_close

            logging.info("Launching core live WebSocket market data stream...")
            
            ws_thread = threading.Thread(target=sws.connect)
            ws_thread.daemon = True
            ws_thread.start()

            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                tsl_monitor.stop()
                heartbeat_monitor.stop()
                eod_monitor.stop()
                logging.info("🛑 Bot stopped manually by user.")
        else:
            logging.error("❌ FEED_TOKEN could not be obtained. Cannot start WebSocket stream.")
    else:
        logging.warning("⚠️ Running without active broker SmartAPI connection.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("🛑 Bot stopped manually by user.")
    except Exception as e:
        logging.critical(f"❌ Fatal crash in main loop: {e}", exc_info=True)
