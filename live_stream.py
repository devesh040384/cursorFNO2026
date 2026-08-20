import time
import logging
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- SET UP CORRESPONDING CREDENTIALS FROM STEP 1 ---
CLIENT_ID = "YOUR_CLIENT_ID"
FEED_TOKEN = "YOUR_LIVE_FEED_TOKEN" # This is the feed_token returned by generateSession()

# 1. Choose a list of tokens you want to stream live.
# For testing, we use token '26000' (Nifty 50 Index Spot) or an option token from your query.
TOKEN_LIST = ["26000"] 
EXCHANGE_SEGMENT = 1 # 1 represents NSE Equity/Index, 2 represents NFO (Futures & Options)

def on_data_received(ws, message):
    """Callback function triggered automatically whenever a new price tick arrives."""
    logging.info(f"🟢 Live Tick Received: {message}")
    # Later, Step 4 will route this structured message right into our live options chain array.

def on_connection_established(ws):
    """Callback function triggered when the secure WebSocket channel opens successfully."""
    logging.info("WebSocket Connected! Subscribing to target instrument tokens...")
    
    # Subscription payload parameters
    correlation_id = "FO_Bot_Stream_01"
    action = 1 # 1 = Subscribe, 2 = Unsubscribe
    mode = 3   # 1 = LTP, 2 = Quote, 3 = Full Snap Quote (Includes Depth)

    ws.subscribe(correlation_id, action, [{
        "exchangeType": EXCHANGE_SEGMENT,
        "tokens": TOKEN_LIST
    }], mode)

def on_connection_closed(ws, close_status_code, close_msg):
    logging.warning(f"🔴 WebSocket disconnected: {close_status_code} - {close_msg}")

def on_error_encountered(ws, error):
    logging.error(f"⚠️ WebSocket Error: {str(error)}")

def start_data_stream():
    # Initialize the official Angel One SmartAPI WebSocket client
    sws = SmartWebSocketV2(AUTH_TOKEN=FEED_TOKEN, CLIENT_CODE=CLIENT_ID)

    # Assign event routing endpoints
    sws.on_open = on_connection_established
    sws.on_data = on_data_received
    sws.on_error = on_error_encountered
    sws.on_close = on_connection_closed

    logging.info("Initializing WebSocket handshake loop...")
    # Launch connection in a background non-blocking thread loop
    sws.connect()

if __name__ == "__main__":
    # Ensure you are running this during market hours (9:15 AM - 3:30 PM) to see active shifting values
    start_data_stream()
