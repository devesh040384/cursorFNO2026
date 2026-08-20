import sys
import pyotp
import logging
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

# 1. Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 2. Hardcoded Authentication Credentials (Keep these updated for your new EC2 instance)
API_KEY = "TOOPDila"
CLIENT_ID = "D65235"       
PASSWORD = "1204"       
TOTP_SECRET = "SJY4JK4LERJNTX3YVFSTAA465I"

def run_trading_pipeline():
    logging.info("Step 1: Commencing Angel One login authentication sequence...")
    
    # Initialize connection client
    smart_api_client = SmartConnect(api_key=API_KEY)
    
    # Generate instantaneous 2FA token
    totp = pyotp.TOTP(TOTP_SECRET).now()
    
    try:
        # Request interactive session authorization
        session_data = smart_api_client.generateSession(CLIENT_ID, PASSWORD, totp)
        
        if session_data.get('status') == True:
            logging.info("✅ Login successful! Extracting connection keys...")
            
            # --- FIXED ENTRIES ---
            jwt_token = session_data['data']['jwtToken'] # Extracted token variable safely
            live_feed_token = smart_api_client.getfeedToken()
            
            logging.info("Step 3: Initializing live streaming connection handshake...")
            
            # Instantiate WebSocket feed via corrected lowercase parameters
            sws = SmartWebSocketV2(
                auth_token=jwt_token,
                api_key=API_KEY,
                client_code=CLIENT_ID,
                feed_token=live_feed_token
            )
            
            # 3. Define Internal WebSocket Event Triggers
            def on_data(wsapp, message):
                logging.info(f"📈 Raw Tick Arrived: {message}")

            def on_open(wsapp):
                logging.info("🎯 WebSocket Data Tunnel Successfully Opened!")
                
                # Test Target Subscription: Nifty Index LTP (Token 99926009)
                correlation_id = "fno_pipeline_test"
                mode = 1 # 1 = LTP (Last Traded Price) feed
                token_list = [{"exchangeType": 1, "tokens": ["26009"]}]
                
                sws.subscribe(correlation_id, mode, token_list)
                logging.info("Subscription packet submitted for Nifty Index.")

            def on_error(wsapp, error):
                logging.error(f"❌ WebSocket Intercepted Error: {error}")

            def on_close(wsapp, close_status_code, close_msg):
                logging.warning(f"🔒 Connection Severed: {close_status_code} - {close_msg}")

            # Assign runtime callbacks to engine
            sws.on_open = on_open
            sws.on_data = on_data
            sws.on_error = on_error
            sws.on_close = on_close
            
            # Fire the active execution thread
            sws.connect()
            
        else:
            logging.critical(f"🛑 Login rejection returned by SmartAPI: {session_data.get('message')}")
            
    except Exception as e:
        logging.critical(f"💥 Critical crash in pipeline execution: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    run_trading_pipeline()
