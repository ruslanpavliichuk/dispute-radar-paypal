"""Background loop for the response agent: picks up dispute-created webhook events and hands each to handle()."""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from agents.reconcile import aware, handle_capture_event, reconcile_pending
from agents.response import handle
from api.config import settings
from api.db import webhook_events
from api.orders_admin import attention_count
from api.webhooks import CAPTURE_EVENTS

logger = logging.getLogger("dispute_radar.agent")
BATCH = 10
BACKOFF_SECONDS = 60  # a failed event waits attempts x this after it was received, so a short PayPal outage is survived


RECONCILE_SECONDS = 60  # the PENDING backstop and the alert run this often, not on every 5s poll
_last_reconcile = float("-inf")


def _pending(engine, types=("CUSTOMER.DISPUTE.CREATED",)) -> list[dict]:
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            select(webhook_events)
            .where(webhook_events.c.event_type.in_(types), webhook_events.c.processed_at.is_(None))
            .order_by(webhook_events.c.received_at).limit(BATCH * 5)
        ).mappings()]
    now = datetime.now(timezone.utc)
    # shortcut: backoff counts from received_at (no last-attempt column), and 10 waiting events could crowd the batch
    # fetch more than BATCH so events still backing off cannot crowd out the ones that are due
    return [e for e in rows if now - aware(e["received_at"]) >= timedelta(seconds=BACKOFF_SECONDS * e["attempts"])][:BATCH]


# shortcut: single API instance assumed (two would only cost an extra draft, the proposals unique key blocks a
# duplicate proposal); switch to SELECT ... FOR UPDATE SKIP LOCKED when a second instance exists
async def run_once(engine, models, client) -> None:
    async def capture_events():
        for event in await asyncio.to_thread(_pending, engine, tuple(CAPTURE_EVENTS)):
            await handle_capture_event(engine, client, event)

    async def backstop():
        global _last_reconcile
        if time.monotonic() - _last_reconcile < RECONCILE_SECONDS:
            return
        _last_reconcile = time.monotonic()
        await reconcile_pending(engine, client)
        if count := await asyncio.to_thread(attention_count, engine):
            logger.critical("%d orders need attention (GET /api/orders/attention)", count)

    for event in await asyncio.to_thread(_pending, engine):
        await handle(engine, models, client, event)
    for step in (capture_events, backstop):  # one failing step must not stop the other
        try:
            await step()
        except Exception as exc:
            logger.error("%s failed: %s", step.__name__, type(exc).__name__)


async def loop(app) -> None:
    while True:
        try:
            await run_once(app.state.engine, app.state.models, app.state.paypal_client)
        except Exception as exc:  # never let one bad pass stop the agent; CancelledError still propagates
            logger.error("agent pass failed: %s", type(exc).__name__)
        await asyncio.sleep(settings.agent_poll_seconds)
