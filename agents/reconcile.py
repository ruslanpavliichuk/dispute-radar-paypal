"""Resolve orders stuck in PENDING from PayPal's own records, and apply capture webhook events to orders.
Reads from PayPal (GET order) only; nothing here moves money."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from agents.response import Skip, _fail, _stamp
from api.db import orders
from api.disputes import DISPUTE_ID
from api.shop import FAILED, PENDING_GRACE, _get, judge_capture

logger = logging.getLogger("dispute_radar.reconcile")
BATCH = 20
GIVE_UP_AFTER = timedelta(days=3)  # older PENDING orders are for the attention list, not the backstop
EVENT_GRACE = timedelta(minutes=10)  # an event can arrive before checkout has recorded the capture
COMPLETED_EVENT = "PAYMENT.CAPTURE.COMPLETED"
DENIED_EVENT = "PAYMENT.CAPTURE.DENIED"
# event type -> order statuses it may change; PayPal's own capture status decides the new one
MOVE_FROM = {
    "PAYMENT.CAPTURE.REFUNDED": ("COMPLETED", "PARTIALLY_REFUNDED"),  # a later full refund upgrades a partial one
    "PAYMENT.CAPTURE.REVERSED": ("COMPLETED", "PARTIALLY_REFUNDED"),
    DENIED_EVENT: ("PENDING",),
}
# capture statuses that count for each event. shortcut: PayPal documents no REVERSED capture status
# (reversals normally show up as disputes), so "REVERSED" is unverified and this branch may never fire.
ACCEPT = {
    "PAYMENT.CAPTURE.REFUNDED": {"REFUNDED", "PARTIALLY_REFUNDED"},
    "PAYMENT.CAPTURE.REVERSED": {"REVERSED"},
}


class NotYet(Exception):
    """The order is not in a state this event can be applied to yet; retry with backoff."""


def aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)  # SQLite (tests) drops the timezone


def _move(engine, order_id: str, old: tuple, new: str) -> bool:
    """Conditional status change: a row that is not in `old` (MISMATCH, UNKNOWN, already moved) is never touched."""
    with engine.begin() as conn:
        result = conn.execute(
            update(orders).where(orders.c.paypal_order_id == order_id, orders.c.status.in_(old)).values(status=new)
        )
    return result.rowcount == 1


def _capture(order: dict, capture_id: str | None):
    """The order's capture with this id, or the first one when we hold no id; None when absent."""
    units = _get(order, "purchase_units") or []
    captures = _get(units[0], "payments", "captures") if units else None
    if not captures:
        return None  # not captured at PayPal yet (CREATED / APPROVED)
    if capture_id is None:
        return captures[0]
    return next((c for c in captures if _get(c, "id") == capture_id), None)


def _apply(engine, row, order: dict) -> None:
    capture = _capture(order, row["capture_id"])
    if capture is None:
        return  # leave PENDING
    status = judge_capture(row["paypal_order_id"], row, capture)
    if status != "PENDING" and _move(engine, row["paypal_order_id"], ("PENDING",), status):
        logger.info("order %s: PENDING -> %s", row["paypal_order_id"], status)


async def _reconcile(engine, client, row) -> None:
    await asyncio.to_thread(_apply, engine, row, await client.get_order(row["paypal_order_id"]))


async def reconcile_pending(engine, client) -> None:
    """Backstop for webhooks that never arrive: look up orders PENDING for longer than the checkout grace."""
    def pending():
        now = datetime.now(timezone.utc)
        with engine.connect() as conn:
            # shortcut: no last-checked column, so the same oldest 20 are re-read every pass; the 3-day cut-off
            # keeps long-stuck orders from crowding out newer ones
            return [dict(r) for r in conn.execute(
                select(orders).where(orders.c.status == "PENDING", orders.c.created_at < now - PENDING_GRACE,
                                     orders.c.created_at > now - GIVE_UP_AFTER)
                .order_by(orders.c.created_at).limit(BATCH)).mappings()]

    for row in await asyncio.to_thread(pending):
        try:
            await _reconcile(engine, client, row)
        except Exception as exc:  # one bad order must not block the rest; type name only
            logger.error("order %s: reconcile failed: %s", row["paypal_order_id"], type(exc).__name__)


def _new_status(row, order: dict, etype: str) -> str | None:
    """The status PayPal's own capture record supports for this event, or None. The event type alone is not trusted."""
    capture = _capture(order, row["capture_id"])
    if capture is None:
        return None
    if etype == DENIED_EVENT:
        status = judge_capture(row["paypal_order_id"], row, capture)
        return status if status in FAILED else None
    status = str(_get(capture, "status"))
    return status if status in ACCEPT[etype] else None


async def handle_capture_event(engine, client, event: dict) -> None:
    try:
        capture_id, etype = event["resource_id"] or "", event["event_type"]
        if not DISPUTE_ID.fullmatch(capture_id):
            raise Skip("invalid capture id")

        def find():
            with engine.connect() as conn:
                return conn.execute(select(orders).where(orders.c.capture_id == capture_id)).mappings().first()

        row = await asyncio.to_thread(find)
        if row is None:
            if datetime.now(timezone.utc) - aware(event["received_at"]) < EVENT_GRACE:
                raise NotYet("unknown capture yet")
            raise Skip("unknown capture")
        if etype == COMPLETED_EVENT:
            if row["status"] == "PENDING":
                await _reconcile(engine, client, row)
        elif row["status"] not in MOVE_FROM[etype]:
            if row["status"] == "PENDING":
                raise NotYet("order not settled yet")  # refund event ahead of the order
            raise Skip(f"order is {row['status'].lower()}")
        else:
            new = await asyncio.to_thread(_new_status, row, await client.get_order(row["paypal_order_id"]), etype)
            if new and await asyncio.to_thread(_move, engine, row["paypal_order_id"], MOVE_FROM[etype], new):
                logger.info("order %s: -> %s", row["paypal_order_id"], new)
        await asyncio.to_thread(_stamp, engine, event["event_id"])
    except Skip as skip:
        await asyncio.to_thread(_stamp, engine, event["event_id"], str(skip))
    except Exception as exc:  # NotYet, PayPal 5xx, timeout, database: retry a few times
        await asyncio.to_thread(_fail, engine, event, exc)
