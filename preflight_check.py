import os
import sys
import sqlite3
import subprocess
from dotenv import load_dotenv

load_dotenv()

def run_preflight():
    print("=" * 70)
    print("         🔍 PRE-MARKET HEALTH & SANITY CHECK         ")
    print("=" * 70)
    all_passed = True

    required_env = ["SMARTAPI_KEY", "CLIENT_ID", "TRADING_PIN", "TOTP_SECRET"]
    missing_env = [var for var in required_env if not os.getenv(var)]
    if missing_env:
        print(f"[🔴 FAIL] Missing Env: {', '.join(missing_env)}")
        all_passed = False
    else:
        print("[🟢 PASS] Env Credentials Found")

    py_files = [f for f in os.listdir(".") if f.endswith(".py")]
    for py_file in py_files:
        res = subprocess.run([sys.executable, "-m", "py_compile", py_file], capture_output=True)
        if res.returncode != 0:
            print(f"[🔴 FAIL] Syntax error in {py_file}")
            all_passed = False
            
    if all_passed:
        print("[🟢 PASS] Python Syntax Verified")

    try:
        conn = sqlite3.connect("trade_history.db")
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM trade_history WHERE status='OPEN' OR exit_price IS NULL;")
        open_count = cursor.fetchone()[0]
        if open_count > 0:
            print(f"[⚠️ WARN] Found {open_count} stale 'OPEN' trade(s) from previous days.")
        else:
            print("[🟢 PASS] Database Healthy")
        conn.close()
    except Exception as e:
        print(f"[🔴 FAIL] DB Error: {e}")
        all_passed = False

    print("=" * 70)
    if all_passed:
        print("🟢 READY FOR MARKET OPEN")
    sys.exit(0 if all_passed else 1)

if __name__ == "__main__":
    run_preflight()
