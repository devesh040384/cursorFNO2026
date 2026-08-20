import logging
import sys
import time
import os
import json
import pyotp
import sqlite3
import threading
from datetime import datetime, timedelta

# Import the Reconciler
from startup_sync import TradeReconciler 

from dotenv import load_dotenv, find_dotenv

from order_execution import OrderExecutionEngine
from options_chain_builder import DynamicOptionsChainBuilder
from strategy_brain import StrategyBrain

# Load Environment Variables for Angel One Credentials
dotenv_path = find_dotenv(filename='.env', raise_error_if_not_found=False)
if dotenv_path:
    load_dotenv(dotenv_path=dotenv_path, override=True)
else:
    load_dotenv(override=True)

try:
    from SmartApi.smartConnect import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
except ModuleNotFoundError:
    from smartapi.smartConnect import SmartConnect
    from smartapi.smartWebSocketV2 import SmartWebSocketV2

# =======================================================================
# 🛠️ DatabaseManager
# =======================================================================
try:
    from database import migrate_database_schema
except ImportError:
    migrate_database_schema = None

class DatabaseManager:
    """A lightweight bridge to keep OrderExecutionEngine and Study Logs happy."""
    def __init__(self, db_path='trade_history.db'):
        self.db_path = db_path
        if migrate_database_schema:
            try:
                migrate_database_schema(self.db_path)
            except TypeError:
                migrate_database_schema()
        
        # Ensure study_signals table exists for post-limit analysis
        self._init_study_table()
        logging.info("✅ Local DatabaseManager initialized.")
        
    def get_connection(self):
        # timeout=20 prevents "database is locked" crashes
        return sqlite3.connect(self.db_path, check_same_thread=False, timeout=20.0)

    def _init_study_table(self):
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS study_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    instrument_type TEXT,
                    spot_price REAL,
                    reason TEXT
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"❌ Failed to initialize study_signals table: {e}")

    def log_study_signal(self, symbol, spot_price, instrument_type, reason="STUDY_TRIGGER"):
        """Logs post-limit signals for study/backtesting purposes without executing orders."""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            # Store in clean IST format
            ist_time = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("""
                INSERT INTO study_signals (timestamp, symbol, instrument_type, spot_price, reason)
                VALUES (?, ?, ?, ?, ?)
            """, (ist_time, symbol, instrument_type, spot_price, reason))
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"❌ Failed to log study signal: {e}")
# =======================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class AIFNOBot:
    def __init__(self):
        logging.info("Initializing F&O Framework (🛑 RESTRICTED TO NIFTY ONLY 🛑)")
        
        self.db = DatabaseManager()
        
        # 1. Load JSON Scrip Master Cache
        self.scrip_master_data = []
        try:
            if os.path.exists('scrip_master.json'):
                with open('scrip_master.json', 'r') as f:
                    self.scrip_master_data = json.load(f)
                logging.info(f"📁 Successfully loaded scrip master from local cache ({len(self.scrip_master_data)} tokens).")
        except Exception as e:
            logging.warning(f"⚠️ Could not load local scrip master cache: {e}")

        self.smart_api = None
        self.feed_token = None
        self.sws = None  
        self.tick_counter =  0  
        self.last_heartbeat = {}  
        
        # 2. Authenticate via .env credentials
        self._init_broker_session()

        # 3. Options Chain Builders (Feed Loaded JSON Data)
        self.options_builders = {}
        for token, index_name in [('26000', 'NIFTY')]:
            builder = DynamicOptionsChainBuilder(index_name=index_name, smart_api=self.smart_api)
            
            builder.scrip_master_data = self.scrip_master_data
            builder.scrip_data = self.scrip_master_data
            builder.scrip_master = self.scrip_master_data
            
            try:
                builder.load_scrip_master(self.scrip_master_data)
            except TypeError:
                builder.load_scrip_master()
                
            self.options_builders[token] = builder

        # 4. Order Execution Engine
        self.order_engine = OrderExecutionEngine(
            smart_api=self.smart_api, 
            db_manager=self.db, 
            scrip_master=self.scrip_master_data, 
            paper_trading=True
        )
        
        # 🛡️ DAILY LIMIT & SHADOW STUDY LOGGING PATCH
        self._apply_shadow_logging_patch()
        
        # 5. Strategy Brain (Feed Loaded JSON Data & Builders)
        try:
            self.strategy = StrategyBrain(
                order_engine=self.order_engine, 
                options_builders=self.options_builders,
                scrip_master_data=self.scrip_master_data
            )
        except TypeError:
            self.strategy = StrategyBrain(
                order_engine=self.order_engine, 
                options_builders=self.options_builders
            )
            setattr(self.strategy, 'scrip_master_data', self.scrip_master_data)
            setattr(self.strategy, 'scrip_data', self.scrip_master_data)
            setattr(self.strategy, 'scrip_master', self.scrip_master_data)
        
        # 🚀 Start Background Threads for Monitoring Exits and EOD
        threading.Thread(target=self._continuous_exit_monitor, daemon=True).start()
        threading.Thread(target=self._continuous_eod_monitor, daemon=True).start()
        
        logging.info("✅ Framework fully loaded and ready for NIFTY live feeds.")

    def _apply_shadow_logging_patch(self):
        """Intercepts order execution if daily limit of 10 trades is reached, switching to Study Mode."""
        execution_method_name = 'execute_options_order'
        original_execute = getattr(self.order_engine, execution_method_name, None)

        if original_execute:
            def shadow_wrapper(*args, **kwargs):
                count = self._get_daily_trade_count()
                if count >= 10:
                    symbol = kwargs.get('symbol', 'UNKNOWN')
                    spot = kwargs.get('entry_spot', 0.0)
                    inst_type = kwargs.get('instrument_type', 'CE')
                    
                    logging.info(f"📚 [STUDY SIGNAL CAPTURED] Daily limit (10) reached. Valid {inst_type} signal recorded for analysis @ Spot: {spot}")
                    self.db.log_study_signal(symbol, spot, inst_type, reason="DAILY_LIMIT_REACHED_STUDY")
                    return {"status": "STUDY_LOGGED"}
                return original_execute(*args, **kwargs)
                
            setattr(self.order_engine, execution_method_name, shadow_wrapper)
            logging.info("🛡️ Shadow Study Logging & Daily Limit interceptor active.")

    def _get_daily_trade_count(self):
        """Counts how many trades have been executed today in IST."""
        try:
            today_str = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
            conn = self.db.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades WHERE entry_timestamp LIKE ?", (today_str + '%',))
            count = cursor.fetchone()[0]
            conn.close()
            return count
        except Exception:
            return 0

    def _init_broker_session(self):
        try:
            api_key = os.getenv('SMARTAPI_KEY') or os.getenv('SMART_API_KEY') or os.getenv('API_KEY') or os.getenv('ANGEL_API_KEY')
            client_id = os.getenv('CLIENT_ID') or os.getenv('SMART_CLIENT_ID') or os.getenv('USER_ID')
            password = os.getenv('PIN') or os.getenv('SMART_PASSWORD') or os.getenv('PASSWORD')
            totp_secret = os.getenv('TOTP_SECRET') or os.getenv('TOTP')
            
            if not api_key or not client_id or not password or not totp_secret:
                logging.error(f"❌ CREDENTIAL ERROR: Missing credentials in .env file.")
                sys.exit(1)

            totp_code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            obj = SmartConnect(api_key=api_key)
            data = obj.generateSession(client_id, password, totp_code)
            
            if data and data.get('status'):
                self.smart_api = obj
                self.feed_token = obj.getfeedToken()
                logging.info("🔐 Successfully authenticated with SmartAPI using TOTP.")
            else:
                logging.error(f"❌ Broker authentication failed: {data}")
                sys.exit(1)
        except Exception as e:
            logging.error(f"❌ Exception during broker session initialization: {e}")
            sys.exit(1)

    def _continuous_exit_monitor(self):
        """🎯 TRAILING STOP-LOSS (TSL) EXIT MONITOR: Dynamically locks in profit as price rises."""
        logging.info("🛡️ Trailing Stop-Loss Exit Monitor active.")
        while True:
            try:
                conn = self.db.get_connection()
                cursor = conn.cursor()
                cursor.execute("SELECT id, symbol, token, entry_price, stop_spot, peak_price FROM trades WHERE status='OPEN'")
                open_trades = cursor.fetchall()
                conn.close()
                
                if not open_trades:
                    time.sleep(10)
                    continue

                for trade in open_trades:
                    trade_id, symbol, token, entry_price, stop_spot, peak_price = trade
                    
                    if not entry_price or entry_price <= 0:
                        continue
                        
                    if not peak_price or peak_price < entry_price:
                        peak_price = entry_price
                        
                    if not stop_spot or stop_spot <= 0:
                        stop_spot = round(entry_price * 0.95, 2)

                    try:
                        exchange = "BFO" if "SENSEX" in symbol else "NFO"
                        response = self.smart_api.ltpData(exchange, symbol, token)
                        
                        if response and response.get('status'):
                            live_ltp = float(response['data']['ltp'])
                            
                            if live_ltp > peak_price:
                                peak_price = live_ltp
                                activation_price = entry_price * 1.10
                                if peak_price >= activation_price:
                                    new_sl = round(peak_price * 0.95, 2)
                                    if new_sl > stop_spot:
                                        stop_spot = new_sl
                                        locked_pnl = ((stop_spot - entry_price) / entry_price) * 100
                                        logging.info(
                                            f"📈 [TSL TRAILED] {symbol} | New Peak: ₹{peak_price:.2f} | "
                                            f"Updated Trailing SL: ₹{stop_spot:.2f} (Locked Profit: {locked_pnl:+.1f}%)"
                                        )

                            conn = self.db.get_connection()
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE trades SET peak_price=?, stop_spot=? WHERE id=?",
                                (peak_price, stop_spot, trade_id)
                            )
                            conn.commit()
                            conn.close()

                            if live_ltp <= stop_spot:
                                exit_reason = "TRAILING SL HIT" if stop_spot >= entry_price else "STOPLOSS HIT"
                                pnl_pct = ((live_ltp - entry_price) / entry_price) * 100
                                
                                ist_exit_time = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
                                
                                conn = self.db.get_connection()
                                cursor = conn.cursor()
                                cursor.execute(
                                    "UPDATE trades SET status=?, exit_price=?, exit_time=?, exit_reason=? WHERE id=?", 
                                    (f"CLOSED - {exit_reason}", live_ltp, ist_exit_time, exit_reason, trade_id)
                                )
                                conn.commit()
                                conn.close()
                                logging.info(
                                    f"🚨 [EXIT EXECUTED] {symbol} | Reason: {exit_reason} | "
                                    f"Exit Price: ₹{live_ltp:.2f} | Realized PnL: {pnl_pct:+.2f}%"
                                )

                    except Exception as e:
                        logging.error(f"❌ Exception fetching live LTP for {symbol}: {e}")

                    time.sleep(1.0)

            except Exception as e:
                logging.error(f"❌ Error in auto-exit monitor: {e}")

            time.sleep(10)

    def _continuous_eod_monitor(self):
        """🎯 EOD GUARD: Force-closes all open positions at 15:20 (3:20 PM) daily."""
        while True:
            now = datetime.utcnow() + timedelta(hours=5, minutes=30) # IST check
            if now.hour == 15 and now.minute >= 20:
                try:
                    conn = self.db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, symbol FROM trades WHERE status='OPEN'")
                    open_trades = cursor.fetchall()
                    
                    if open_trades:
                        ist_exit_time = now.strftime('%Y-%m-%d %H:%M:%S')
                        for trade in open_trades:
                            trade_id, symbol = trade
                            logging.info(f"🚨 [EOD SQUARE-OFF] Force closing intraday position: {symbol}")
                            cursor.execute("UPDATE trades SET status=?, exit_time=? WHERE id=?", ("CLOSED - EOD SQUARE OFF", ist_exit_time, trade_id))
                            conn.commit()
                            time.sleep(0.5)
                    conn.close()
                except Exception as e:
                    logging.error(f"❌ Error during EOD square-off execution: {e}")
            
            time.sleep(30)

    def _on_data_feed(self, ws, message):
        """Live WebSocket Stream Processing"""
        try:
            self.tick_counter += 1
            token = str(message.get('token', ''))
            ltp_raw = message.get('last_traded_price')
            
            if not ltp_raw:
                return
                
            ltp = float(ltp_raw) / 100.0
            
            symbol_map = {'26000': 'NIFTY'}
            symbol = symbol_map.get(token)
            
            if symbol and ltp > 0:
                current_time = time.time()
                last_time = self.last_heartbeat.get(symbol, 0)
                
                if current_time - last_time >= 20:
                    trades_today = self._get_daily_trade_count()
                    logging.info(f"💓 [HEARTBEAT] NIFTY @ {ltp:.2f} | Trades Today: {trades_today}/10")
                    self.last_heartbeat[symbol] = current_time

                self.strategy.evaluate_tick(symbol=symbol, spot_price=ltp)
        except Exception as e:
            logging.error(f"❌ Error processing live data feed tick: {e}")

    def _on_open(self, ws):
        logging.info("🔌 Live WebSocket Connection Established. Subscribing to NIFTY token...")
        token_list = [
            {"exchangeType": 1, "tokens": ["26000"]}
        ]
        if self.sws: 
            self.sws.subscribe("aifno_live_feed", 1, token_list)

    def _on_close(self, ws, close_status_code, close_msg):
        logging.critical("🚨 [FATAL] Live WebSocket Connection closed.")

    def _on_error(self, ws, error, *args, **kwargs):
        logging.warning(f"⚠️ [WEBSOCKET WARNING] Connection dropped or interrupted: {error}")

    def run(self):
        logging.info("Starting broker reconciliation...")
        
        reconciler = TradeReconciler(smart_api=self.smart_api, db_path="trade_history.db")
        sync_success = reconciler.sync_open_positions()
        
        if not sync_success:
            logging.critical("🛑 Halting startup: Broker sync failed. Check API connection.")
            return 
            
        logging.info("Launching core live WebSocket market data stream...")
        try:
            if self.smart_api and self.feed_token:
                client_id = os.getenv('CLIENT_ID') or os.getenv('SMART_CLIENT_ID', '')
                api_key = os.getenv('SMARTAPI_KEY') or os.getenv('SMART_API_KEY', '')
                
                self.sws = SmartWebSocketV2(
                    auth_token=self.smart_api.access_token, 
                    api_key=api_key, 
                    client_code=client_id, 
                    feed_token=self.feed_token
                )
                self.sws.on_open = self._on_open
                self.sws.on_data = self._on_data_feed
                self.sws.on_error = self._on_error
                self.sws.on_close = self._on_close
                self.sws.connect()
            else:
                logging.error("❌ Cannot launch WebSocket: Broker session is not authenticated.")
                while True: 
                    time.sleep(1)
        except KeyboardInterrupt:
            logging.info("🛑 Bot stopped gracefully by user.")

if __name__ == "__main__":
    bot = AIFNOBot()
    bot.run()
