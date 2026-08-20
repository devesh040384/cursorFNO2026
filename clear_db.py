import sqlite3

def clear_trades():
    conn = sqlite3.connect('trade_history.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM trades")
    conn.commit()
    conn.close()
    print("🧹 All database entries cleared successfully!")

if __name__ == "__main__":
    clear_trades()
