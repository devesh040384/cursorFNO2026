import os
import sys
import logging
import pyotp

try:
    from SmartApi.smartConnect import SmartConnect
except ModuleNotFoundError:
    from smartapi.smartConnect import SmartConnect

def init_broker_session():
    """Authenticates with SmartAPI using environment credentials and returns (smart_api, feed_token)."""
    try:
        api_key = os.getenv('SMARTAPI_KEY') or os.getenv('SMART_API_KEY') or os.getenv('API_KEY') or os.getenv('ANGEL_API_KEY')
        client_id = os.getenv('CLIENT_ID') or os.getenv('SMART_CLIENT_ID') or os.getenv('USER_ID')
        password = os.getenv('PIN') or os.getenv('SMART_PASSWORD') or os.getenv('PASSWORD')
        totp_secret = os.getenv('TOTP_SECRET') or os.getenv('TOTP')
        
        if not api_key or not client_id or not password or not totp_secret:
            logging.error("❌ CREDENTIAL ERROR: Missing credentials in .env file.")
            sys.exit(1)

        totp_code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
        obj = SmartConnect(api_key=api_key)
        data = obj.generateSession(client_id, password, totp_code)
        
        if data and data.get('status'):
            feed_token = obj.getfeedToken()
            logging.info("🔐 Successfully authenticated with SmartAPI using TOTP.")
            return obj, feed_token
        else:
            logging.error(f"❌ Broker authentication failed: {data}")
            sys.exit(1)
    except Exception as e:
        logging.error(f"❌ Exception during broker session initialization: {e}")
        sys.exit(1)
