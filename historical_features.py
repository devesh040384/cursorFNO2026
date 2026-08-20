import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class HistoricalFeatureExtractor:
    def __init__(self, smart_api_client):
        """
        :param smart_api_client: The active authenticated SmartConnect object from Step 1
        """
        self.client = smart_api_client

    def fetch_historical_candles(self, exchange: str, token: str, interval: str, days_back: int = 30) -> pd.DataFrame:
        """
        Queries Angel One REST API for historical OHLCV data.
        Interval options: 'ONE_MINUTE', 'FIVE_MINUTE', 'ONE_DAY', etc.
        """
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days_back)
        
        # Format strings matching Angel One standards: YYYY-MM-DD HH:MM
        params = {
            "exchange": exchange,
            "symboltoken": token,
            "interval": interval,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M")
        }
        
        try:
            logging.info(f"Fetching {interval} history for token {token} from {params['fromdate']}...")
            response = self.client.getCandleData(params)
            
            if response.get('status') == True and response.get('data') is not None:
                # Angel One returns a matrix list: [[Timestamp, Open, High, Low, Close, Volume], ...]
                raw_candles = response['data']
                
                df = pd.DataFrame(raw_candles, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
                df['Timestamp'] = pd.to_datetime(df['Timestamp'])
                df.set_index('Timestamp', inplace=True)
                
                # Convert object types to float currency representations
                for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                    df[col] = pd.to_numeric(df[col])
                    
                return df
            else:
                logging.error(f"API rejection on historical fetch: {response.get('message')}")
                return pd.DataFrame()
                
        except Exception as e:
            logging.error(f"Exception encountered during history fetch: {str(e)}")
            return pd.DataFrame()

    def engineer_ai_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Computes statistical and directional attributes for strategy inputs."""
        if df.empty or len(df) < 20:
            logging.warning("Insufficient data length to compute features.")
            return df
            
        # 1. Volatility Calculation (Crucial for Option Greek tracking)
        df['Log_Returns'] = np.log(df['Close'] / df['Close'].shift(1))
        # Annualized standard deviation rolling windows (Assuming intraday minutes, scale mapping accordingly)
        df['Historical_Volatility'] = df['Log_Returns'].rolling(window=20).std() * np.sqrt(252 * 375) # 375 minutes per NSE day
        
        # 2. Trend Feature: Exponential Moving Average (EMA) cross indicator
        df['EMA_Fast'] = df['Close'].ewm(span=9, adjust=False).mean()
        df['EMA_Slow'] = df['Close'].ewm(span=21, adjust=False).mean()
        df['Trend_Signal'] = np.where(df['EMA_Fast'] > df['EMA_Slow'], 1, -1) # 1 = Bullish, -1 = Bearish
        
        # 3. Momentum Feature: Basic Rate of Change
        df['Price_ROC'] = df['Close'].pct_change(periods=5)
        
        df.dropna(inplace=True)
        return df

# --- PIPELINE INTEGRATION RUNNER ---
if __name__ == "__main__":
    # Standard placeholder context to illustrate operation
    print("Historical Feature Engine initialized. Awaiting Step 1 operational client handle passing...")
