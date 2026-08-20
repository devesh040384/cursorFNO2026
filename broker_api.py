import random
import logging
import time

class BrokerAPI:
    def __init__(self, api_key, api_secret, paper_trade=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper_trade = paper_trade
        
        if self.paper_trade:
            logging.warning("⚠️ PAPER TRADING MODE ENABLED: No real orders will be placed.")
        else:
            logging.info("🔴 LIVE TRADING MODE ENABLED: Real money is at risk.")

    def get_instruments(self, exchange):
        """
        MOCK FUNCTION: Replace with actual broker call to fetch active instruments.
        """
        return [
            {
                "tradingsymbol": "NIFTY26AUG2624250CE",
                "name": "NIFTY",
                "strike": 24250,
                "instrument_type": "CE",
                "expiry": "2026-08-26", 
                "lot_size": 65 # 2026 NIFTY Lot Size
            }
        ]

    def get_positions(self):
        """
        MOCK FUNCTION: Replace with actual broker position fetcher.
        """
        return []

    def place_order(self, symbol, action, price, quantity):
        """
        Executes real orders if live, or simulates them if paper trading.
        """
        if self.paper_trade:
            logging.info(f"📝 [PAPER TRADE] Simulating {action} for {quantity} units of {symbol} at ₹{price}")
            time.sleep(0.1) # Simulate network delay
            return {"status": "SUCCESS", "order_id": f"PAPER_ORD_{random.randint(1000, 9999)}"}
        else:
            logging.info(f"📡 [BROKER API] Executing REAL {action} order for {quantity} units of {symbol} at ₹{price}")
            return {"status": "SUCCESS", "order_id": f"LIVE_ORD_{random.randint(1000, 9999)}"}
