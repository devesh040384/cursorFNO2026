import sqlite3
import pandas as pd
from datetime import datetime
from ist_time import ist_today
import os

from config import FALLBACK_LOT_SIZE

DB_PATH = 'trade_history.db'

def generate_daily_summary():
    if not os.path.exists(DB_PATH):
        print("❌ Database file 'trade_history.db' not found.")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        query = """
            SELECT 
                id, symbol, token, qty, entry_price, exit_price, target_price,
                stop_loss_price, peak_price, status, exit_reason,
                timestamp AS entry_time, exit_time
            FROM trades
            ORDER BY id ASC
        """
        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty:
            print("📊 No trades found in database.")
            return

        def parse_time_to_ist(val):
            if pd.isnull(val) or not val or str(val).strip() == "": return None
            dt = pd.to_datetime(val)
            if dt.hour < 6: dt = dt + pd.Timedelta(hours=5, minutes=30)
            return dt

        df['entry_dt'] = df['entry_time'].apply(parse_time_to_ist)
        df['exit_dt'] = df['exit_time'].apply(parse_time_to_ist)

        today_str = ist_today()
        df['date_str'] = df['entry_dt'].dt.strftime('%Y-%m-%d')
        today_df = df[df['date_str'] == today_str].copy()

        if today_df.empty:
            print(f"📊 No trades executed today ({today_str}).")
            return

        print("\n" + "="*125)
        print(f"                               DAILY TRADING SUMMARY - {today_str}")
        print("="*125)

        total_trades = len(today_df)
        closed_trades = today_df[today_df['status'] == 'CLOSED']
        open_trades = today_df[today_df['status'] == 'OPEN']

        print(f"Total Trades Today : {total_trades}")
        print(f"Open Positions     : {len(open_trades)}")
        print(f"Closed Positions   : {len(closed_trades)}")
        print("-" * 125)

        summary_list = []
        total_realized_pnl_rs = 0.0

        for idx, row in today_df.iterrows():
            entry_p = row['entry_price'] or 0.0
            exit_p = row['exit_price'] or 0.0
            status = row['status']
            reason = row['exit_reason'] or 'OPEN'
            symbol = str(row['symbol'])
            
            qty_raw = row['qty'] if 'qty' in today_df.columns else None
            if pd.notnull(qty_raw) and qty_raw:
                lot_size = int(qty_raw)
            elif symbol.startswith('BANKNIFTY'):
                lot_size = FALLBACK_LOT_SIZE["BANKNIFTY"]
            elif symbol.startswith('NIFTY'):
                lot_size = FALLBACK_LOT_SIZE["NIFTY"]
            elif symbol.startswith('SENSEX'):
                lot_size = FALLBACK_LOT_SIZE["SENSEX"]
            else:
                lot_size = FALLBACK_LOT_SIZE["NIFTY"]
            
            if status == 'CLOSED' and entry_p > 0:
                # Percentage PnL
                pnl_pct = ((exit_p - entry_p) / entry_p) * 100
                pnl_pct_str = f"{pnl_pct:+.2f}%"
                
                # Rupee PnL
                pnl_rs = (exit_p - entry_p) * lot_size
                pnl_rs_str = f"₹ {pnl_rs:+.2f}"
                total_realized_pnl_rs += pnl_rs
            else:
                pnl_pct_str = "N/A"
                pnl_rs_str = "—"

            date_fmt = row['entry_dt'].strftime('%Y-%m-%d') if pd.notnull(row['entry_dt']) else today_str
            entry_time_fmt = row['entry_dt'].strftime('%H:%M:%S') if pd.notnull(row['entry_dt']) else "—"
            exit_time_fmt = row['exit_dt'].strftime('%H:%M:%S') if pd.notnull(row['exit_dt']) and status == 'CLOSED' else "—"

            summary_list.append({
                "ID": row['id'],
                "Date": date_fmt,
                "Symbol": symbol,
                "Status": status,
                "Entry ₹": f"{entry_p:.2f}",
                "Exit ₹": f"{exit_p:.2f}" if status == 'CLOSED' else "—",
                "PnL %": pnl_pct_str,
                "PnL ₹": pnl_rs_str,
                "Reason": reason,
                "Entry Time": entry_time_fmt,
                "Exit Time": exit_time_fmt
            })

        summary_df = pd.DataFrame(summary_list)
        print(summary_df.to_string(index=False))
        print("="*125)
        
        if not closed_trades.empty:
            wins = closed_trades[closed_trades['exit_price'] > closed_trades['entry_price']]
            losses = closed_trades[closed_trades['exit_price'] <= closed_trades['entry_price']]
            win_rate = (len(wins) / len(closed_trades) * 100)
            print(f"Win Rate          : {win_rate:.1f}% ({len(wins)} Wins / {len(losses)} Losses)")
            print(f"Net Realized PnL  : ₹ {total_realized_pnl_rs:+.2f}")
            print("="*125 + "\n")

    except Exception as e:
        print(f"❌ Error generating summary: {e}")

if __name__ == "__main__":
    generate_daily_summary()
