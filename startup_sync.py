import logging
import sqlite3

class TradeReconciler:
    def __init__(self, smart_api, db_path="trade_history.db"):
        self.smart_api = smart_api
        self.db_path = db_path

    def sync_open_positions(self):
        """Checks the broker for open positions and syncs them with the local database."""
        try:
            # 1. Fetch real open positions from Angel One
            response = self.smart_api.position()
            if not response or not response.get('status'):
                logging.error("❌ Failed to fetch positions from broker.")
                return False
            
            positions = response.get('data', [])
            if positions is None: 
                positions = []

            # Filter for currently open NFO (Options) positions
            open_broker_positions = [
                p for p in positions 
                if p.get('exchange') == 'NFO' and int(p.get('netqty', 0)) != 0
            ]

            # 2. Fetch local open positions from SQLite
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT symbol, token, entry_price FROM trades WHERE status='OPEN'")
            local_open_trades = cursor.fetchall()
            
            local_symbols = [trade[0] for trade in local_open_trades]

            # 3. Reconcile: If broker has an open trade not in local DB, log it/add it
            for bp in open_broker_positions:
                symbol = bp['tradingsymbol']
                if symbol not in local_symbols:
                    logging.warning(f"⚠️ ORPHANED TRADE FOUND: {symbol} is open on broker but missing in local DB. Syncing...")
                    # Calculate targets for the orphaned trade
                    entry_price = float(bp['netprice'])
                    target = round(entry_price * 1.10, 2)
                    stop = round(entry_price * 0.95, 2)
                    
                    cursor.execute("""
                        INSERT INTO trades (symbol, token, type, entry_timestamp, entry_price, status, target_spot, stop_spot) 
                        VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, 'OPEN', ?, ?)
                    """, (symbol, bp['symboltoken'], 'CE/PE', entry_price, target, stop))
                    
            conn.commit()
            conn.close()
            logging.info("✅ Broker reconciliation complete.")
            return True

        except Exception as e:
            logging.error(f"❌ Error during position reconciliation: {e}")
            return False
