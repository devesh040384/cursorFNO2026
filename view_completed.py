import sqlite3
import os
from datetime import datetime

DB_FILE = 'trade_history.db' 

def show_completed_trades():
    if not os.path.exists(DB_FILE):
        print(f"❌ Database '{DB_FILE}' not found.")
        return

    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        query = f"SELECT * FROM trades WHERE status != 'OPEN' AND timestamp LIKE '{today_str}%' ORDER BY timestamp DESC"
        cursor.execute(query)
        completed_trades = cursor.fetchall()
        
        if not completed_trades:
            print("\n" + "="*80)
            print(f" 📉 NO COMPLETED TRADES FOUND FOR TODAY ({today_str}) in '{DB_FILE}'.")
            print("="*80 + "\n")
            return

        col_names = [description[0] for description in cursor.description]

        col_widths = [len(name) for name in col_names]
        for row in completed_trades:
            for i, item in enumerate(row):
                col_widths[i] = max(col_widths[i], len(str(item)))
        
        col_widths = [w + 2 for w in col_widths]

        header = " | ".join([f"{name:<{col_widths[i]}}" for i, name in enumerate(col_names)])
        
        print("\n" + "="*len(header))
        print(f" ✅ COMPLETED TRADES FOR TODAY ({today_str}) in '{DB_FILE}' - Total: {len(completed_trades)}")
        print("="*len(header))
        print(header)
        print("-" * len(header))
        
        for row in completed_trades:
            row_str = " | ".join([f"{str(item):<{col_widths[i]}}" for i, item in enumerate(row)])
            print(row_str)
            
        print("="*len(header) + "\n")

    except Exception as e:
        print(f"❌ Error reading database: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    show_completed_trades()
