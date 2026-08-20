import time
import sqlite3
import logging
from datetime import datetime
from db import migrate_database_schema
from broker_api import BrokerAPI

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class AIFNOBot:
    def __init__(self, db_path="trade_history.db", paper_trade=True):
        self.db_path = db_path
        
        # Initialize broker connection
        self.broker = BrokerAPI(
            api_key="YOUR_KEY", 
            api_secret="YOUR_SECRET", 
            paper_trade=paper_trade
        )
        
        self.open_positions = {}
        
        # --- NEW: Daily Trade Management ---
        self.max_daily_trades = 10
        self.trades_today = 0
        
        self._load_active_positions_from_db()
        self._sync_daily_trade_count()

    def _get_db_connection(self):
        return sqlite3.connect(self.db_path)

    def _sync_daily_trade_count(self):
        """Counts how many trades have already been executed today to survive reboots."""
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            conn = self._get_db_connection()
            cursor = conn.cursor()
            # Count any trade where the entry timestamp starts with today's date
            cursor.execute("SELECT COUNT(*) FROM trades WHERE entry_timestamp LIKE ?", (today_str + '%',))
            count = cursor.fetchone()[0]
            self.trades_today = count
            logging.info(f"📊 Daily Trade Limit Check: {self.trades_today}/{self.max_daily_trades} executed today.")
            conn.close()
        except Exception as e:
            logging.error(f"❌ Failed to sync daily trade count: {e}")

    def _load_active_positions_from_db(self):
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, entry_price FROM trades WHERE status = 'OPEN'")
            rows = cursor.fetchall()
            for symbol, entry_price in rows:
                self.open_positions[symbol] = {"entry_price": entry_price, "status": "OPEN"}
            if rows:
                logging.info(f"🔄 Restored {len(rows)} active positions from DB.")
            conn.close()
        except Exception as e:
            logging.error(f"❌ DB Load Error: {e}")

    def get_valid_contract_details(self, index_name, strike, option_type):
        """Resolves unexpired symbol, lot size, and exact expiry date."""
        today = datetime.now().date()
        exchange = "NFO" 
        
        try:
            instrument_list = self.broker.get_instruments(exchange)
            valid_contracts = []
            
            for inst in instrument_list:
                if (inst['name'] == index_name and 
                    inst['strike'] == strike and 
                    inst['instrument_type'] == option_type):
                    
                    expiry_date = datetime.strptime(str(inst['expiry']), "%Y-%m-%d").date()
                    if expiry_date >= today:
                        valid_contracts.append({
                            'symbol': inst['tradingsymbol'],
                            'expiry': expiry_date,
                            'lot_size': inst.get('lot_size', 65)
                        })
                        
            if not valid_contracts:
                return None, None, None
                
            valid_contracts.sort(key=lambda x: x['expiry'])
            best_contract = valid_contracts[0]
            
            return best_contract['symbol'], best_contract['lot_size'], best_contract['expiry']
        except Exception as e:
            return None, None, None

    def has_active_broker_position(self, symbol):
        try:
            for position in self.broker.get_positions():
                if position['symbol'] == symbol and position['quantity'] != 0:
                    return True
            return False
        except Exception:
            return True 

    def sync_locks_with_broker(self):
        try:
            active_symbols = [p['symbol'] for p in self.broker.get_positions() if p['quantity'] != 0]
            for symbol in list(self.open_positions.keys()):
                if symbol not in active_symbols:
                    logging.info(f"🔄 [SYNC] {symbol} closed externally. Clearing lock.")
                    self._update_trade_in_db(symbol, 0.0, "BROKER_AUTO_CLOSED")
                    self.open_positions.pop(symbol, None)
        except Exception as e:
            pass

    def _log_trade_entry_to_db(self, symbol, action, entry_price):
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO trades (entry_timestamp, symbol, action, entry_price, status) VALUES (?, ?, ?, ?, 'OPEN')", 
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, action, entry_price)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"❌ DB Entry Error: {e}")

    def _update_trade_in_db(self, symbol, exit_price, reason):
        try:
            conn = self._get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE trades SET exit_price = ?, exit_time = ?, exit_reason = ?, status = ? WHERE symbol = ? AND status = 'OPEN'",
                (exit_price, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason, f"CLOSED - {reason}", symbol)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logging.error(f"❌ DB Exit Error: {e}")

    def process_trading_signal(self, index_name, strike, option_type, previous_rsi, current_rsi, current_price):
        symbol, lot_size, expiry_date = self.get_valid_contract_details(index_name, strike, option_type)
        if not symbol:
            return

        # Circuit Breaker for expired options
        today = datetime.now().date()
        if expiry_date < today:
            return 

        if symbol in self.open_positions:
            return

        is_buy_signal = previous_rsi <= 70 and current_rsi > 70
        
        if is_buy_signal:
            # --- NEW: Daily Limit and Shadow Trade Logic ---
            if self.trades_today >= self.max_daily_trades:
                logging.info(f"👻 [SHADOW SIGNAL] Daily limit reached ({self.max_daily_trades}/{self.max_daily_trades}). Missed valid setup for {symbol} at ₹{current_price:.2f}.")
                return # Abort execution, just log it.

            if self.has_active_broker_position(symbol):
                self.open_positions[symbol] = {"entry_price": current_price, "status": "RECOVERED"}
                return

            logging.info(f"🟢 [ENTRY] Firing BUY order for {symbol} | Lot Size: {lot_size} | Price: ₹{current_price:.2f}")
            response = self.broker.place_order(symbol, "BUY", current_price, quantity=lot_size)

            if response.get("status") == "SUCCESS":
                self.open_positions[symbol] = {
                    "entry_price": current_price,
                    "lot_size": lot_size, 
                    "status": "OPEN"
                }
                self._log_trade_entry_to_db(symbol, option_type, current_price)
                
                # Increment the daily counter
                self.trades_today += 1
                logging.info(f"🔒 [LOCKED] {symbol} secured. (Trade {self.trades_today} of {self.max_daily_trades} today)")

    def check_exit_conditions(self, symbol, current_price):
        if symbol not in self.open_positions:
            return

        trade = self.open_positions[symbol]
        entry_price = trade["entry_price"]
        lot_size = trade.get("lot_size", 65) 

        target_price = entry_price + 20.0
        stop_loss_price = entry_price - 10.0

        reason = None
        if current_price >= target_price:
            reason = "TARGET_HIT"
        elif current_price <= stop_loss_price:
            reason = "STOP_LOSS_HIT"

        if reason:
            logging.info(f"🔴 [EXIT] Closing {symbol} | Reason: {reason}")
            self.broker.place_order(symbol, "SELL", current_price, quantity=lot_size)
            
            self._update_trade_in_db(symbol, current_price, reason)
            self.open_positions.pop(symbol, None)

    def run_market_loop(self):
        logging.info("🚀 Starting AI FNO Bot (STRICT NIFTY ONLY | 10 TRADES MAX)...")
        loop_count = 0

        # Replace with your actual live market feed
        market_ticks = [
            {"index": "NIFTY", "strike": 24250, "type": "CE", "prev_rsi": 68, "curr_rsi": 72, "price": 150.00},
            # Sensex and BankNifty data can come through, but the loop will ignore them
            {"index": "SENSEX", "strike": 77800, "type": "PE", "prev_rsi": 68, "curr_rsi": 72, "price": 400.00}, 
        ]

        while True:
            loop_count += 1
            if loop_count % 10 == 0:
                self.sync_locks_with_broker()

            for tick in market_ticks:
                # 🛑 STRICT INDEX FILTER: Hard-coded to only trade NIFTY
                if tick["index"] != "NIFTY":
                    continue
                    
                symbol, _, _ = self.get_valid_contract_details(tick["index"], tick["strike"], tick["type"])
                
                if symbol:
                    self.check_exit_conditions(symbol, tick["price"])

                self.process_trading_signal(
                    tick["index"], tick["strike"], tick["type"], 
                    tick["prev_rsi"], tick["curr_rsi"], tick["price"]
                )

            time.sleep(5)

if __name__ == "__main__":
    DB_NAME = "trade_history.db"
    
    migrate_database_schema(DB_NAME)
    
    # Running in Paper Trade mode for safety
    bot = AIFNOBot(db_path=DB_NAME, paper_trade=True)
    bot.run_market_loop()
