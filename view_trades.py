import sqlite3
from tabulate import tabulate # Optional, standard python sqlite3 formatting works too

def show_trade_ledger():
    conn = sqlite3.connect('trade_history.db')
    cursor = conn.cursor()
    
    print("\n" + "="*80)
    print("📋 ACTIVE HOLDINGS (OPEN TRADES)")
    print("="*80)
    cursor.execute("SELECT id, symbol, type, qty, target_spot, stop_spot, timestamp FROM trades WHERE status='OPEN'")
    open_rows = cursor.fetchall()
    if open_rows:
        for r in open_rows:
            print(f"ID: {r[0]} | Symbol: {r[1]} | Type: {r[2]} | Qty: {r[3]} | Target Premium: ₹{r[4]} | SL Premium: ₹{r[5]} | Time: {r[6]}")
    else:
        print("No active holdings right now.")

    print("\n" + "="*80)
    print("🏁 COMPLETED TRADES HISTORY")
    print("="*80)
    cursor.execute("SELECT id, symbol, type, qty, status, timestamp FROM trades WHERE status LIKE 'CLOSED%'")
    closed_rows = cursor.fetchall()
    if closed_rows:
        for r in closed_rows:
            print(f"ID: {r[0]} | Symbol: {r[1]} | Type: {r[2]} | Qty: {r[3]} | Status: {r[4]} | Time: {r[5]}")
    else:
        print("No completed trades recorded yet.")
        
    print("="*80 + "\n")
    conn.close()

if __name__ == "__main__":
    show_trade_ledger()
