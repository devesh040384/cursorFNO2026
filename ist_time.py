"""Single source of truth for IST wall-clock.

The bot's session logic (09:45 start, 14:30 cutoff, 15:15 EOD) is IST, but the
DB stamps, scorecard and heartbeat used host-local `datetime.now()`. On any host
that is not Asia/Kolkata that mismatch silently breaks the daily entry cap and
the loss circuit breaker: they query "today" in the wrong timezone and see zero
trades. Everything date/time related must go through here.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ist_now():
    """Timezone-aware current time in IST."""
    return datetime.now(timezone.utc).astimezone(IST)


def ist_naive():
    """IST wall-clock as a naive datetime, for comparing against stored stamps."""
    return ist_now().replace(tzinfo=None)


def ist_today():
    """'YYYY-MM-DD' in IST."""
    return ist_now().strftime("%Y-%m-%d")


def ist_stamp():
    """'YYYY-MM-DD HH:MM:SS' in IST — the format stored in the trades table."""
    return ist_now().strftime("%Y-%m-%d %H:%M:%S")


def ist_hhmm():
    """Integer HHMM (e.g. 945) for session-window comparisons."""
    now = ist_now()
    return now.hour * 100 + now.minute
