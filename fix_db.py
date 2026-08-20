import sqlite3

def clean_bugged_trades():
    try:
        # Connect to your SQLite database
        conn = sqlite3.connect('trade_history.db')
        cursor = conn.cursor()
        
        # Delete any open trade that has an entry price of 0.0
        cursor.execute("DELETE FROM trades WHERE entry_price = 0.0 OR entry_price IS NULL")
        
        deleted_count = cursor.rowcount
        conn.commit()
        
        print(f"✅ Successfully deleted {deleted_count} bugged trades from the database.")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    clean_bugged_trades()
