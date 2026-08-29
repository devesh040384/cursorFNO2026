"""Confirm what the broker actually did with an order.

The bot previously placed a live order and immediately logged a fill at LTP. A
rejected or pending order was therefore recorded as an open position that did
not exist, and the trailing-stop monitor would later try to sell it. Nothing in
the codebase ever read the order book.

This module polls the order book until the order reaches a terminal state and
reports the real status, filled quantity and average fill price.
"""
import logging
import time

# SmartAPI order statuses, lowercased. Anything not listed is treated as pending.
FILLED = {"complete", "completed", "filled"}
DEAD = {"rejected", "cancelled", "canceled"}


class FillResult:
    __slots__ = ("status", "filled_qty", "avg_price", "order_id", "raw")

    def __init__(self, status, filled_qty=0, avg_price=0.0, order_id=None, raw=None):
        self.status = status
        self.filled_qty = int(filled_qty or 0)
        self.avg_price = float(avg_price or 0.0)
        self.order_id = order_id
        self.raw = raw

    @property
    def is_filled(self):
        return self.status in FILLED and self.filled_qty > 0 and self.avg_price > 0

    @property
    def is_dead(self):
        return self.status in DEAD

    def __repr__(self):
        return (
            f"FillResult({self.status!r}, qty={self.filled_qty}, "
            f"avg={self.avg_price}, id={self.order_id})"
        )


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract(row):
    """Pull (status, filled_qty, avg_price) out of one order-book row."""
    status = str(
        row.get("orderstatus") or row.get("status") or row.get("order_status") or ""
    ).strip().lower()
    filled = row.get("filledshares")
    if filled in (None, ""):
        filled = row.get("filledQty") or row.get("quantity") or 0
    avg = row.get("averageprice")
    if avg in (None, "", 0, "0"):
        avg = row.get("avgPrice") or row.get("price") or 0
    return status, int(_as_float(filled)), _as_float(avg)


def _find_order(order_book, order_id):
    target = str(order_id).strip()
    for row in order_book or []:
        if not isinstance(row, dict):
            continue
        for key in ("orderid", "order_id", "orderId", "uniqueorderid"):
            if str(row.get(key) or "").strip() == target:
                return row
    return None


def confirm_fill(smart_api, order_id, timeout_sec=12.0, poll_sec=1.5):
    """Poll the order book until `order_id` is terminal, or timeout.

    Returns a FillResult. A timeout yields status 'timeout', which callers must
    treat as UNKNOWN -- never as a fill and never as a rejection, because the
    order may still execute.
    """
    if not smart_api or not order_id:
        return FillResult("unknown", order_id=order_id)

    deadline = time.time() + float(timeout_sec)
    last = FillResult("pending", order_id=order_id)
    while True:
        try:
            resp = smart_api.orderBook()
            rows = (resp or {}).get("data") or []
            row = _find_order(rows, order_id)
            if row is not None:
                status, filled, avg = _extract(row)
                last = FillResult(status, filled, avg, order_id, row)
                if last.is_filled or last.is_dead:
                    return last
        except Exception as e:
            logging.warning(f"[orders] order book poll failed for {order_id}: {e}")

        if time.time() >= deadline:
            logging.error(
                f"[orders] {order_id} still {last.status!r} after {timeout_sec}s; "
                "treating as UNKNOWN (not filled, not rejected)."
            )
            return FillResult("timeout", last.filled_qty, last.avg_price, order_id, last.raw)
        time.sleep(poll_sec)
