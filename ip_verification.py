import urllib.request
import logging
import sys

# Setup structured logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# REPLACE THIS: Type the exact Elastic IP assigned to your AWS EC2 instance
REGISTERED_STATIC_IP = "13.200.51.50" 

def verify_server_environment():
    logging.info("Checking server infrastructure...")
    try:
        # Query a clean, fast API to check the outbound IPv4 address
        current_ip = urllib.request.urlopen('https://api.ipify.org', timeout=5).read().decode('utf8')
        
        logging.info(f"EC2 Outbound Public IP detected as: {current_ip}")
        
        if current_ip == REGISTERED_STATIC_IP:
            logging.info("✅ IP Validation Successful. Server matches Angel One App Whitelist.")
            return True
        else:
            logging.critical(
                f"🚨 IP MISMATCH DETECTED!\n"
                f"Detected: {current_ip}\n"
                f"Expected: {REGISTERED_STATIC_IP}\n"
                f"Orders will be rejected by SmartAPI. Aborting startup."
            )
            return False
            
    except Exception as e:
        logging.error(f"Failed to fetch public IP from instance: {str(e)}")
        return False

# Self-test block
if __name__ == "__main__":
    if not verify_server_environment():
        sys.exit(1) # Exit execution with error code if mismatch occurs
