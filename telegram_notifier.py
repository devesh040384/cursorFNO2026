"""Telegram alerts and remote status for the FNO bot — standalone, zero coupling.

Runs as its OWN process alongside main.py and touches no trading file. It tails
the log the bot already writes and answers commands you send from your phone.
Nothing here can affect a trade: the bot is never imported for its behaviour,
and every database query is read-only.

Setup
-----
1. Talk to @BotFather on Telegram -> /newbot -> copy the token.
2. Send your new bot any message, then run:  python3 telegram_notifier.py --whoami
   That prints your chat id.
3. Put both in .env (same file the bot uses):
       TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
       TELEGRAM_CHAT_ID=987654321
4. Run it next to the bot, in its own tmux window:
       python3 telegram_notifier.py

Commands you can send the bot
-----------------------------
  /status   heartbeat: last spot, regime, entries today per index
  /open     currently OPEN trades
  /pnl      today's realised PnL and execution drag
  /trades   last 5 closed trades
  /log      last few WARNING/ERROR lines
  /mute /unmute   pause or resume pushed alerts
  /help

Only TELEGRAM_CHAT_ID is answered; messages from any other chat are ignored.
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

API = "https://api.telegram.org/bot{token}/{method}"
LOG_FILE = "trading_bot.log"
DB_FILE = "trade_history.db"

# What gets pushed. Order matters: first match wins.
# (regex, label, always_send) — always_send bypasses /mute for true emergencies.
RULES = [
    (r"UNTRACKED broker position", "🚨 UNTRACKED POSITION", True),
    (r"CIRCUIT BREAKER", "🛑 CIRCUIT BREAKER", True),
    (r"is (OPEN|PENDING|TIMEOUT) — not recorded|is \w+ — not recorded", "⚠️ ORDER UNCONFIRMED", True),
    (r"partial exit", "⚠️ PARTIAL EXIT", True),
    (r"Implausible spot", "🚨 BAD PRICE FEED", True),
    (r"\[KILL SWITCH\].*not squared", "⚠️ EOD SQUARE-OFF INCOMPLETE", True),
    (r"ENTRY (VOLUME_BREAKOUT|TREND_CONT|RSI_HOOK)", "📈 ENTRY", False),
    (r"\[TRADE CLOSED\]", "📉 EXIT", False),
    (r"Volume gate: no future", "⚠️ NO FUTURES — ENTRIES BLOCKED", True),
    (r"\[seed\].*only \d+ closed bars|\[seed\].*RVOL stays in warmup", "⚠️ SEEDING INCOMPLETE", True),
    (r"Signal bar gap|volume feed gap", "⚠️ FEED GAP", False),
    (r"\[session\] re-authenticated", "🔑 RE-AUTHENTICATED", False),
    (r"WebSocket Connection Closed", "⚠️ WEBSOCKET CLOSED", False),
    (r"Multi-Index Framework fully operational", "✅ BOT STARTED", True),
]
COMPILED = [(re.compile(p), label, urgent) for p, label, urgent in RULES]

# Telegram tolerates ~20 messages/minute to one chat comfortably.
MIN_SEND_INTERVAL = 1.2
MAX_MSG = 3500


class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self._last_send = 0.0

    def _call(self, method, params=None, timeout=15):
        url = API.format(token=self.token, method=method)
        data = urllib.parse.urlencode(params or {}).encode("utf-8")
        try:
            with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logging.warning("telegram %s failed: %s", method, e)
            return None

    def send(self, text):
        if not self.chat_id:
            return False
        gap = time.time() - self._last_send
        if gap < MIN_SEND_INTERVAL:
            time.sleep(MIN_SEND_INTERVAL - gap)
        self._last_send = time.time()
        body = text if len(text) <= MAX_MSG else text[:MAX_MSG] + "\n… truncated"
        res = self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": body,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        return bool(res and res.get("ok"))

    def updates(self, offset=None, timeout=25):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        res = self._call("getUpdates", params, timeout=timeout + 10)
        return (res or {}).get("result") or []


class LogTail:
    """Follows a file across RotatingFileHandler rotations."""

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.inode = None
        self._seek_end()

    def _stat(self):
        try:
            st = os.stat(self.path)
            return st.st_ino, st.st_size
        except OSError:
            return None, 0

    def _seek_end(self):
        self.inode, size = self._stat()
        self.pos = size

    def read_new(self):
        inode, size = self._stat()
        if inode is None:
            return []
        # Rotated (new inode) or truncated (smaller) -> start from the top.
        if inode != self.inode or size < self.pos:
            self.inode = inode
            self.pos = 0
        if size == self.pos:
            return []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.pos)
                chunk = f.read()
                self.pos = f.tell()
        except OSError:
            return []
        return [ln for ln in chunk.splitlines() if ln.strip()]


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def classify(line):
    for pattern, label, urgent in COMPILED:
        if pattern.search(line):
            return label, urgent
    return None, False


# ---------------------------------------------------------------- read-only DB

def _rows(query, params=()):
    if not os.path.exists(DB_FILE):
        return []
    try:
        # Read-only URI so this process can never write to the trading DB.
        conn = sqlite3.connect("file:%s?mode=ro" % DB_FILE, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logging.warning("db read failed: %s", e)
        return []


def _today():
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _qty(row):
    if row["qty"]:
        return int(row["qty"])
    sym = str(row["symbol"] or "").upper()
    if "SENSEX" in sym:
        return 20
    if "BANKNIFTY" in sym:
        return 30
    return 65


def cmd_open():
    rows = _rows(
        "SELECT symbol, index_name, qty, entry_price, target_price, stop_loss_price, entry_time"
        " FROM trades WHERE status = 'OPEN' ORDER BY id"
    )
    if not rows:
        return "No open trades."
    out = ["<b>Open trades</b>"]
    for r in rows:
        out.append(
            "• <code>%s</code>\n  entry ₹%.2f · T ₹%.2f · SL ₹%.2f · qty %s\n  since %s"
            % (esc(r["symbol"]), r["entry_price"] or 0, r["target_price"] or 0,
               r["stop_loss_price"] or 0, r["qty"], esc(str(r["entry_time"])[11:16]))
        )
    return "\n".join(out)


def cmd_pnl():
    rows = _rows(
        "SELECT symbol, qty, entry_price, exit_price, index_name, slippage"
        " FROM trades WHERE status = 'CLOSED'"
        " AND substr(COALESCE(entry_time, timestamp, ''), 1, 10) = ?",
        (_today(),),
    )
    if not rows:
        return "No closed trades today."
    total = 0.0
    wins = 0
    slip = 0.0
    by_index = {}
    for r in rows:
        if r["entry_price"] is None or r["exit_price"] is None:
            continue
        q = _qty(r)
        pnl = (float(r["exit_price"]) - float(r["entry_price"])) * q
        total += pnl
        wins += 1 if pnl > 0 else 0
        if r["slippage"] is not None:
            slip += float(r["slippage"]) * q
        key = r["index_name"] or "?"
        by_index[key] = by_index.get(key, 0.0) + pnl
    n = len(rows)
    lines = [
        "<b>Today</b> (%s)" % _today(),
        "%d closed · %dW/%dL · <b>₹%.2f</b>" % (n, wins, n - wins, total),
        " · ".join("%s ₹%.0f" % (k, v) for k, v in sorted(by_index.items())),
    ]
    if slip:
        drag = (100.0 * slip / abs(total)) if total else 0.0
        lines.append("execution drag ₹%.0f (%.0f%% of net)" % (slip, drag))
    return "\n".join(lines)


def cmd_trades():
    rows = _rows(
        "SELECT symbol, qty, entry_price, exit_price, exit_reason, entry_reason, exit_time"
        " FROM trades WHERE status = 'CLOSED'"
        " ORDER BY COALESCE(exit_time, entry_time, timestamp) DESC LIMIT 5"
    )
    if not rows:
        return "No closed trades yet."
    out = ["<b>Last 5 closed</b>"]
    for r in rows:
        if r["entry_price"] is None or r["exit_price"] is None:
            continue
        pnl = (float(r["exit_price"]) - float(r["entry_price"])) * _qty(r)
        out.append(
            "%s <code>%s</code>\n  ₹%.2f → ₹%.2f = <b>₹%.0f</b> · %s"
            % ("🟢" if pnl > 0 else "🔴", esc(r["symbol"]), r["entry_price"],
               r["exit_price"], pnl, esc(r["exit_reason"] or "?"))
        )
    return "\n".join(out)


def cmd_status():
    """Last heartbeat straight from the log — no bot import, no shared state."""
    line = None
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-400:]
        for candidate in reversed(tail):
            if "SYSTEM STATUS" in candidate:
                line = candidate.strip()
                break
    except OSError:
        pass
    if not line:
        return "No heartbeat found in the log yet. Is the bot running?"
    body = line.split("[SYSTEM STATUS]", 1)[-1].strip()
    stamp = line[:19]
    parts = [p.strip() for p in body.split("||")]
    return "<b>Status</b> <code>%s</code>\n%s" % (esc(stamp), esc("\n".join(parts)))


def cmd_log():
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-600:]
    except OSError:
        return "Log not readable."
    bad = [l.strip() for l in tail if " - ERROR - " in l or " - CRITICAL - " in l
           or " - WARNING - " in l]
    if not bad:
        return "No warnings or errors in the recent log. 👍"
    return "<b>Recent problems</b>\n<code>%s</code>" % esc("\n".join(bad[-8:]))


HELP = (
    "<b>FNO bot</b>\n"
    "/status — heartbeat\n"
    "/open — open trades\n"
    "/pnl — today's PnL\n"
    "/trades — last 5 closed\n"
    "/log — recent warnings/errors\n"
    "/mute /unmute — pause alerts\n"
    "/help"
)

HANDLERS = {
    "/status": cmd_status,
    "/open": cmd_open,
    "/pnl": cmd_pnl,
    "/trades": cmd_trades,
    "/log": cmd_log,
    "/help": lambda: HELP,
    "/start": lambda: HELP,
}


def whoami(token):
    tg = Telegram(token, None)
    updates = tg.updates(timeout=0)
    if not updates:
        print("No messages seen. Send your bot any message, then run this again.")
        return 1
    seen = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            seen[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not seen:
        print("No chat id found in recent updates.")
        return 1
    print("Add this to .env:\n")
    for cid, who in seen.items():
        print("    TELEGRAM_CHAT_ID=%s        # %s" % (cid, who))
    return 0


def run(tg, poll_sec=3.0, quiet=False):
    tail = LogTail(LOG_FILE)
    offset = None
    muted = False
    if not quiet:
        tg.send("🔔 <b>Notifier online</b>\nWatching <code>%s</code>. Send /help." % LOG_FILE)
    logging.info("watching %s -> chat %s", LOG_FILE, tg.chat_id)

    while True:
        # 1. push anything new in the log
        for line in tail.read_new():
            label, urgent = classify(line)
            if not label:
                continue
            if muted and not urgent:
                continue
            stamp = line[11:19] if len(line) > 19 else ""
            detail = line.split(" - ", 2)[-1]
            tg.send("%s <code>%s</code>\n%s" % (label, esc(stamp), esc(detail)))

        # 2. answer commands
        for update in tg.updates(offset=offset, timeout=int(poll_sec)):
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id") or "")
            if chat != tg.chat_id:
                continue  # ignore everyone else
            text = (msg.get("text") or "").strip().split()
            if not text:
                continue
            cmd = text[0].split("@")[0].lower()
            if cmd == "/mute":
                muted = True
                tg.send("🔕 Muted. Emergencies still come through. /unmute to restore.")
                continue
            if cmd == "/unmute":
                muted = False
                tg.send("🔔 Unmuted.")
                continue
            handler = HANDLERS.get(cmd)
            if handler:
                try:
                    tg.send(handler())
                except Exception as e:
                    tg.send("Command failed: %s" % esc(e))


def main(argv=None):
    global LOG_FILE
    parser = argparse.ArgumentParser(description="Telegram alerts for the FNO bot.")
    parser.add_argument("--whoami", action="store_true", help="print your chat id and exit")
    parser.add_argument("--test", action="store_true", help="send one test message and exit")
    parser.add_argument("--quiet", action="store_true", help="no startup message")
    parser.add_argument("--log", default=LOG_FILE, help="log file to follow")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG_FILE = args.log

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. See the setup notes at the top of this file.")
        return 1
    if args.whoami:
        return whoami(token)

    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not chat_id:
        print("TELEGRAM_CHAT_ID is not set. Run:  python3 telegram_notifier.py --whoami")
        return 1

    tg = Telegram(token, chat_id)
    if args.test:
        ok = tg.send("✅ Test message from the FNO bot notifier.")
        print("sent" if ok else "FAILED — check token and chat id")
        return 0 if ok else 1

    try:
        run(tg, quiet=args.quiet)
    except KeyboardInterrupt:
        logging.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
