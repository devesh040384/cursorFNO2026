import sys
import logging
import socket
import time

# Configure Diagnostic Logger
logging.basicConfig(level=logging.INFO, format='[DIAGNOSTIC] %(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Diagnostics")

print("=" * 60)
print("        ANGEL ONE ALGO BOT: PRE-FLIGHT DIAGNOSTICS          ")
print("=" * 60)

# Test 1: Libraries
logger.info("Test 1/4: Scanning Python Virtual Environment library bindings...")
required_libs = ['SmartApi', 'pyotp', 'pandas', 'numpy', 'logzero']
for lib in required_libs:
    try:
        __import__(lib)
        logger.info(f"   ✅ Library check passed: '{lib}' is loaded.")
    except ImportError:
        logger.error(f"   ❌ Missing library: '{lib}'")

# Test 2: File Structure
logger.info("Test 2/4: Verifying module structures on EC2 disk...")
files = ['main.py', 'error_sentinel.py', 'strategy_brain.py', 'risk_manager.py', 'order_execution.py']
import os
for f in files:
    if os.path.exists(f):
        logger.info(f"   ✅ File signature matched: '{f}' located.")
    else:
        logger.error(f"   ❌ Missing file: '{f}'")

# Test 3: Ping Angel One Endpoint
logger.info("Test 3/4: Analyzing ping paths to Angel One routing server (smartapi.angelbroking.com)...")
try:
    start_time = time.time()
    s = socket.create_connection(("smartapi.angelbroking.com", 443), timeout=5)
    s.close()
    latency = (time.time() - start_time) * 1000
    logger.info(f"   ✅ Gateway connection established. TCP Handshake Latency: {latency:.2f}ms")
except Exception as e:
    logger.error(f"   ❌ Gateway Ping Failed: {e}")

# Test 4: Mock Pipeline Execution
logger.info("Test 4/4: Executing mock algorithmic payload pass through operational classes...")
try:
    from strategy_brain import StrategyBrain
    brain = StrategyBrain()
    
    mock_features = {'Trend_Signal': 1, 'Price_ROC': 0.003}
    market_direction = brain.analyze_market_state(mock_features)
    
    mock_chain = {
        "spot_price": 24200.0,
        "atm_strike": 24200,
        "calls": {
            24200: {"symbol": "NIFTY28JUL202624200CE", "strike": 24200, "token": "99999"}
        },
        "puts": {
            24200: {"symbol": "NIFTY28JUL202624200PE", "strike": 24200, "token": "88888"}
        }
    }
    
    signal = brain.select_optimal_contract(market_direction, mock_chain)
    if signal and "symbol" in signal:
        logger.info(f"   ✅ Pipeline Integrity Verified! Mock Signal Produced: {signal['symbol']}")
        print("-" * 60)
        logger.info("🟢 SYSTEM READY FOR DEPLOYMENT")
    else:
        raise ValueError("Strategy Brain produced empty signal output.")
except Exception as e:
    logger.error(f"   ❌ Pipeline Integrity Check Crashed: {e}")
    print("-" * 60)
    logger.critical("🔴 SYSTEM DEGRADED: Fix anomalies before deploying.")
