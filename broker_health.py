"""Keep the broker session alive, and make failures visible.

Two gaps this closes:

1. `generateSession` was called once at startup. SmartAPI tokens expire, and an
   unattended EC2 run that loses its session stops managing OPEN positions
   silently -- every REST call just fails and gets logged.
2. Nothing outside the log file ever reported a problem. A CRITICAL event on a
   headless box was invisible until someone read the log.
"""
import logging
import logging.handlers
import os
import threading
import time
import urllib.request

# Substrings that mean "the session is gone", not "this one call failed".
AUTH_ERRORS = (
    "invalid token", "token expired", "session expired", "ag8001", "ag8002",
    "ag8003", "invalid session", "unauthorized", "401",
)


def looks_like_auth_failure(text):
    low = str(text or "").lower()
    return any(marker in low for marker in AUTH_ERRORS)


class SessionKeeper:
    """Re-authenticates on demand, at most once per `min_interval_sec`."""

    def __init__(self, login_fn, min_interval_sec=60.0):
        self.login_fn = login_fn
        self.min_interval_sec = float(min_interval_sec)
        self._last_attempt = 0.0
        self._lock = threading.Lock()
        self.api = None
        self.relogin_count = 0

    def ensure(self, force=False):
        """Return a live API handle, re-logging in if needed. None on failure."""
        with self._lock:
            if self.api is not None and not force:
                return self.api
            now = time.time()
            if now - self._last_attempt < self.min_interval_sec:
                return self.api
            self._last_attempt = now
            try:
                new_api = self.login_fn()
            except Exception as e:
                logging.error(f"[session] re-login raised: {e}")
                return self.api
            if new_api is None:
                logging.error("[session] re-login failed; keeping the old handle.")
                return self.api
            self.api = new_api
            self.relogin_count += 1
            logging.warning(f"[session] re-authenticated (#{self.relogin_count}).")
            return self.api

    def handle_error(self, error):
        """Call with any API exception/response. Re-logs in only for auth errors."""
        if looks_like_auth_failure(error):
            logging.error(f"[session] auth failure detected: {error}")
            return self.ensure(force=True)
        return self.api


def setup_logging(path="trading_bot.log", max_bytes=10 * 1024 * 1024, backups=5, level=logging.INFO):
    """Rotating file + console. The old plain FileHandler grew without bound."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    rotating = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    root.addHandler(rotating)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    return root


class AlertHandler(logging.Handler):
    """POSTs CRITICAL records to ALERT_WEBHOOK_URL (Slack/Telegram/whatever).

    Deliberately dependency-free and fail-open: an alerting outage must never
    take the trading process down, so every send is best-effort and swallowed.
    """

    def __init__(self, webhook_url=None, min_interval_sec=30.0):
        super().__init__(level=logging.CRITICAL)
        self.webhook_url = webhook_url or os.getenv("ALERT_WEBHOOK_URL")
        self.min_interval_sec = float(min_interval_sec)
        self._last_sent = 0.0

    def emit(self, record):
        if not self.webhook_url:
            return
        now = time.time()
        if now - self._last_sent < self.min_interval_sec:
            return
        self._last_sent = now
        try:
            payload = f'{{"text": {self.format(record)!r}}}'.encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass  # never let alerting break trading


def attach_alerting(webhook_url=None):
    handler = AlertHandler(webhook_url)
    if handler.webhook_url:
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.info("[alerts] CRITICAL alerts will POST to the configured webhook.")
    else:
        logging.info("[alerts] ALERT_WEBHOOK_URL not set; CRITICAL events log only.")
    return handler
