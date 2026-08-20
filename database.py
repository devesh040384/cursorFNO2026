import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime

SCHEMA_COLUMNS = {
    "qty": "INTEGER",
    "exchange": "TEXT",
    "index_name": "TEXT",
    "entry_time": "TEXT",
    "token": "TEXT",
    "target_price": "REAL",
    "stop_loss_price": "REAL",
    "peak_price": "REAL",
    "exit_price": "REAL",
    "exit_time": "TEXT",
    "exit_reason": "TEXT",
    "timestamp": "TEXT",
}


class DatabaseManager:
    def __init__(self, db_path='trade_history.db'):
        self.db_path = db_path
        self.init_database()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    @contextmanager
    def get_cursor(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            logging.error(f"❌ Database transaction error: {e}")
            raise
        finally:
            conn.close()

    def init_database(self):
        try:
            with self.get_cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        token TEXT,
                        qty INTEGER,
                        exchange TEXT,
                        index_name TEXT,
                        entry_price REAL,
                        target_price REAL,
                        stop_loss_price REAL,
                        peak_price REAL,
                        status TEXT,
                        exit_price REAL,
                        exit_time TEXT,
                        exit_reason TEXT,
                        timestamp TEXT,
                        entry_time TEXT
                    )
                """)
                cursor.execute("PRAGMA table_info(trades)")
                existing = {row[1] for row in cursor.fetchall()}
                for col, col_type in SCHEMA_COLUMNS.items():
                    if col not in existing:
                        cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
                        logging.info(f"⚙️ DB Migration: added '{col}'")
            logging.info("✅ Local DatabaseManager initialized (WAL Mode Active).")
        except Exception as e:
            logging.critical(f"❌ Fatal error initializing database schema: {e}")

    def log_trade(
        self,
        symbol,
        token,
        entry_price,
        target_price,
        stop_loss_price,
        status="OPEN",
        qty=None,
        exchange=None,
        index_name=None,
    ):
        """Logs a new entry. Returns trade id or None."""
        try:
            now_ist = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.get_cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO trades (
                        symbol, token, qty, exchange, index_name,
                        entry_price, target_price, stop_loss_price, peak_price,
                        status, timestamp, entry_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol, token, qty, exchange, index_name,
                        entry_price, target_price, stop_loss_price, entry_price,
                        status, now_ist, now_ist,
                    ),
                )
                trade_id = cursor.lastrowid
                logging.info(
                    f"💾 [DB] Logged OPEN #{trade_id} {symbol} qty={qty} @ ₹{entry_price}"
                )
                return trade_id
        except Exception as e:
            logging.error(f"❌ Failed to log trade for {symbol}: {e}")
            return None

    def update_trailing_stoploss(self, trade_id, new_sl_price, new_peak_price):
        try:
            with self.get_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE trades
                    SET stop_loss_price = ?, peak_price = ?
                    WHERE id = ? AND status = 'OPEN'
                    """,
                    (new_sl_price, new_peak_price, trade_id),
                )
        except Exception as e:
            logging.error(f"❌ Failed to update TSL for trade {trade_id}: {e}")

    def close_trade(self, trade_id, exit_price, exit_reason):
        try:
            now_ist = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.get_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE trades
                    SET status = 'CLOSED', exit_price = ?, exit_reason = ?, exit_time = ?
                    WHERE id = ? AND status = 'OPEN'
                    """,
                    (exit_price, exit_reason, now_ist, trade_id),
                )
                if cursor.rowcount:
                    logging.info(
                        f"🔒 [DB] Trade {trade_id} closed at ₹{exit_price}. Reason: {exit_reason}"
                    )
                    return True
                return False
        except Exception as e:
            logging.error(f"❌ Failed to close trade {trade_id}: {e}")
            return False

    def count_open_trades(self, index_name=None):
        try:
            if index_name:
                row = self.fetch_one(
                    "SELECT COUNT(*) AS n FROM trades WHERE status = 'OPEN' AND index_name = ?",
                    (index_name,),
                )
            else:
                row = self.fetch_one("SELECT COUNT(*) AS n FROM trades WHERE status = 'OPEN'")
            if row is None:
                return 0
            return int(row[0])
        except Exception:
            return 0

    def count_entries_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        row = self.fetch_one(
            """
            SELECT COUNT(*) AS n FROM trades
            WHERE COALESCE(entry_time, timestamp, '') LIKE ?
            """,
            (f"{today}%",),
        )
        if row is None:
            return 0
        return int(row[0])

    def fetch_closed_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        return self.fetch_all(
            """
            SELECT symbol, qty, entry_price, exit_price, COALESCE(entry_time, timestamp) AS t
            FROM trades
            WHERE status = 'CLOSED'
              AND COALESCE(entry_time, timestamp, '') LIKE ?
            ORDER BY COALESCE(exit_time, t) ASC
            """,
            (f"{today}%",),
        )

    def fetch_one(self, query, params=()):
        try:
            with self.get_cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchone()
        except Exception as e:
            logging.error(f"❌ Database fetch_one error: {e}")
            return None

    def fetch_all(self, query, params=()):
        try:
            with self.get_cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
        except Exception as e:
            logging.error(f"❌ Database fetch_all error: {e}")
            return []
