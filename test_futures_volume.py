import os
import time
import json
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pyotp
import websocket
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

websocket.enableTrace(False)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

# Helper: Find current month futures contract
def get_current_month_futures(scrip_data, underlying="NIFTY"):
    now = datetime.now()
    exchange = "BFO" if underlying == "SENSEX" else "NFO"
    instrument_type = "FUTIDX"
    
    matching_contracts = []
    
    for item in scrip_data:
        if (item.get("exch_seg") == exchange and 
            item.get("instrumenttype") == instrument_type and 
            item.get("name") == underlying):
            
            # Format expiry usually: DDMMMYYYY (e.g., 28AUG2026)
            expiry_str = item.get("expiry")
            if not expiry_str:
                continue
            
            try:
                # Handle expiry format variations
                exp_date = datetime.strptime(expiry_str, "%d%b%Y")
                if exp_date.date() >= now.date():
                    matching_contracts.append((exp_date, item))
            except Exception:
                continue

    if not matching_contracts:
        return None
        
    # Sort by nearest expiry
    matching_contracts.sort(key=lambda x: x[0])
    return matching_contracts[0][1]

# In-memory Volume & Candle Tracker
class FuturesVolumeTracker:
    def __init__(self, symbol, token):
        self.symbol = symbol
        self.token = str(token)
        self.volume_history = []  # Last 20 closed 1-min volumes
        self.current_candle_vol = 0
        self.last_candle_time = time.time()
        self.open_price = None
        self.close_price = None

    def on_tick(self, ltp, volume_traded):
        current_time = time.time()
        
        # 1-minute aggregation interval
        if current_time - self.last_candle_time >= 60:
            if self.current_candle_vol > 0:
                self.volume_history.append(self.current_candle_vol)
                if len(self.volume_history) > 20:
                    self.volume_history.pop(0)

                avg_vol = sum(self.volume_history) / len(self.volume_history) if self.volume_history else 1
                rvol = self.current_candle_vol / avg_vol if avg_vol > 0 else 1.0
                
                logging.info(
                    f"📊 [{self.symbol} 1M CLOSE] LTP: ₹{ltp:.2f} | "
                    f"Vol: {self.current_candle_vol} | "
                    f"SMA20(Vol): {avg_vol:.0f} | "
                    f"RVOL: {rvol:.2f}x"
                )
                
                if rvol >= 3.5:
                    logging.warning(f"⚡⚡ [VOLUME BREAKOUT DETECTED] {self.symbol} RVOL: {rvol:.2f}x >= 3.5x!")

            self.current_candle_vol = 0
            self.last_candle_time = current_time
            self.open_price = ltp

        self.current_candle_vol += volume_traded
        self.close_price = ltp


def authenticate():
    api_key = os.getenv("SMART_API_KEY") or os.getenv("SMARTAPI_KEY")
    client_id = os.getenv("CLIENT_ID")
    pwd = os.getenv("PASSWORD") or os.getenv("PIN")
    totp_key = os.getenv("TOTP_SECRET")

    smart_api = SmartConnect(api_key=api_key)
    totp_gen = pyotp.TOTP(totp_key).now()
    data = smart_api.generateSession(client_id, pwd, totp_gen)
    return smart_api if data and data.get("status") else None


def main():
    if not os.path.exists("scrip_master.json"):
        logging.error("❌ scrip_master.json not found!")
        return

    with open("scrip_master.json", "r", encoding="utf-8") as f:
        scrip_data = json.load(f)

    # 1. Resolve Active Month Futures
    nifty_fut = get_current_month_futures(scrip_data, "NIFTY")
    sensex_fut = get_current_month_futures(scrip_data, "SENSEX")

    if not nifty_fut or not sensex_fut:
        logging.error("❌ Could not resolve Futures tokens from scrip master.")
        return

    logging.info(f"✅ Found NIFTY Futures: {nifty_fut['symbol']} (Token: {nifty_fut['token']}) Expiry: {nifty_fut['expiry']}")
    logging.info(f"✅ Found SENSEX Futures: {sensex_fut['symbol']} (Token: {sensex_fut['token']}) Expiry: {sensex_fut['expiry']}")

    trackers = {
        str(nifty_fut["token"]): FuturesVolumeTracker(nifty_fut["symbol"], nifty_fut["token"]),
        str(sensex_fut["token"]): FuturesVolumeTracker(sensex_fut["symbol"], sensex_fut["token"]),
    }

    # 2. Authenticate
    smart_api = authenticate()
    if not smart_api:
        logging.error("❌ Authentication failed.")
        return

    raw_api = smart_api.api if hasattr(smart_api, "api") else smart_api
    feed_resp = raw_api.getfeedToken()
    feed_token = feed_resp.get("data") if isinstance(feed_resp, dict) else feed_resp

    # 3. Stream Live Futures WebSocket Data
    sws = SmartWebSocketV2(
        auth_token=raw_api.access_token,
        api_key=os.getenv("SMART_API_KEY") or os.getenv("SMARTAPI_KEY"),
        client_code=os.getenv("CLIENT_ID"),
        feed_token=feed_token
    )

    def on_data(*args):
        message = args[1] if len(args) > 1 else args[0]
        if isinstance(message, str):
            try: message = json.loads(message)
            except Exception: pass
        if not isinstance(message, dict): return

        token = str(message.get("token") or message.get("exchangeToken") or "").replace("\x00", "").strip()
        ltp_raw = message.get("last_traded_price") or message.get("ltp")
        vol_raw = message.get("last_traded_quantity") or message.get("vol_traded_today") or 1

        if token in trackers and ltp_raw is not None:
            ltp = float(ltp_raw) / 100.0 if float(ltp_raw) > 1000000 else float(ltp_raw)
            trackers[token].on_tick(ltp, int(vol_raw))

    def on_open(ws):
        logging.info("🔌 Live WebSocket Connected for Futures Tokens. Subscribing...")
        token_list = [
            {"exchangeType": 2, "tokens": [str(nifty_fut["token"])]}, # NFO = 2
            {"exchangeType": 4, "tokens": [str(sensex_fut["token"])]}  # BFO = 4
        ]
        sws.subscribe("corrid_futures", 2, token_list) # Quote mode for volume details

    sws.on_open = on_open
    sws.on_data = on_data
    sws.connect()

if __name__ == "__main__":
    main()
