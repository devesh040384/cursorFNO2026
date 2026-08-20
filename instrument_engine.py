import pandas as pd
import requests
import os
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class InstrumentMappingEngine:
    URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    CACHE_FILE = "scrip_master.parquet"

    def __init__(self):
        self.df = None

    def initialize_master_data(self, force_download: bool = False):
        """Downloads or loads the scrip master file and filters for F&O."""
        # 1. Check if we already downloaded it today to avoid wasting server bandwidth
        if not force_download and os.path.exists(self.CACHE_FILE):
            # Check file modification date
            file_time = datetime.fromtimestamp(os.path.getmtime(self.CACHE_FILE)).date()
            if file_time == datetime.today().date():
                logging.info("Loading Scrip Master from local cache...")
                self.df = pd.read_parquet(self.CACHE_FILE)
                return

        logging.info("Downloading fresh Scrip Master from Angel One...")
        try:
            response = requests.get(self.URL, timeout=15)
            data = response.json()
            
            # Convert to DataFrame
            full_df = pd.DataFrame(data)
            
            # Filter strictly for NSE Futures & Options (NFO) to keep memory small
            self.df = full_df[full_df['exch_seg'] == 'NFO'].copy()
            
            # Clean up types
            self.df['strike'] = pd.to_numeric(self.df['strike']) / 100.0 # Convert from paise if formatted abnormally
            
            # Save local copy using fast parquet format (requires: pip install pyarrow)
            self.df.to_parquet(self.CACHE_FILE)
            logging.info(f"Successfully cached {len(self.df)} active F&O contracts.")
            
        except Exception as e:
            logging.error(f"Failed to fetch instrument files: {str(e)}")
            if os.path.exists(self.CACHE_FILE):
                logging.warning("Falling back to stale local cache.")
                self.df = pd.read_parquet(self.CACHE_FILE)

    def search_option_token(self, name: str, expiry: str, strike: float, option_type: str) -> dict:
        """
        Quick lookup for an options token.
        :param name: 'NIFTY' or 'BANKNIFTY'
        :param expiry: Format 'DDMMMYYYY' (e.g., '30JUL2026')
        :param strike: Target Strike price (e.g., 24500.0)
        :param option_type: 'CE' or 'PE'
        """
        if self.df is None:
            raise ValueError("Engine not initialized. Run initialize_master_data() first.")

        # Filter criteria
        query_str = f"name == '{name}' and expiry == '{expiry}' and strike == {strike} and symbol.str.endswith('{option_type}')"
        result = self.df.query(query_str)
        
        if not result.empty:
            match = result.iloc[0]
            return {
                "token": match['token'],
                "symbol": match['symbol'],
                "lot_size": match['lotsize']
            }
        else:
            logging.warning(f"No contract matching criteria: {name} | {expiry} | {strike} | {option_type}")
            return {}

# --- LIVE TEST BLOCK ---
if __name__ == "__main__":
    # Ensure you run: pip install pandas pyarrow requests
    engine = InstrumentMappingEngine()
    engine.initialize_master_data()
    
    # Example search: Let's find an arbitrary contract token
    # (Note: Set expiry string matching your intended operational derivative contract)
    test_token = engine.search_option_token(name="NIFTY", expiry="23JUL2026", strike=24500.0, option_type="CE")
    print("\n--- Search Match Results ---")
    print(test_token)
