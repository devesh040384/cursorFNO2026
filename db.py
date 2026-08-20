import sqlite3
import logging

DB_PATH = "trade_history.db"

def get_db_connection():
    """
    Creates a database connection with a 20-second timeout.
    This safely queues database writes to prevent 'database is locked' crashes.
    """
    conn = sqlite3.connect(DB_PATH, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """
    Initializes the database schema with the complete, modern blueprint.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                token TEXT,
                type TEXT,
                entry_timestamp TEXT,
                exit_time TEXT,
                entry_price REAL,
                exit_price REAL,
                status TEXT,
                exit_reason TEXT,
                target_spot REAL,
                stop_spot REAL
            )
        ''')
        
        conn.commit()
        logging.info("✅ Database initialized successfully with all modern schema columns.")
    except Exception as e:
        logging.error(f"❌ Database initialization error: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

def migrate_database_schema():
    """
    Checks for missing columns and adds them seamlessly if they don't exist.
    This acts as a safety net for future upgrades.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Pull current columns from the table
        cursor.execute("PRAGMA table_info(trades)")
        columns = [row['name'] for row in cursor.fetchall()]
        
        # Add new columns if they are missing
        if 'token' not in columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN token TEXT")
            logging.info("⚙️ DB Migration: Added 'token' column.")
            
        if 'target_spot' not in columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN target_spot REAL")
            logging.info("⚙️ DB Migration: Added 'target_spot' column.")
            
        if 'stop_spot' not in columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN stop_spot REAL")
            logging.info("⚙️ DB Migration: Added 'stop_spot' column.")
            
        conn.commit()
    except Exception as e:
        logging.error(f"❌ Database migration error: {e}")
    finally:
        if 'conn' in locals() and conn:
            conn.close()

if __name__ == "__main__":
    # If this file is run directly, it will just set up the database.
    init_db()
    migrate_database_schema()
