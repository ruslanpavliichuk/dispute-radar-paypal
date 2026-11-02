"""Background loop for the response agent: picks up dispute-created webhook events and hands each to handle()."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from agents.response import handle
from api.config import settings
from api.db import webhook_events

def aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)  # SQLite (tests) drops the timezone


logger = logging.getLogger("dispute_radar.agent")
BATCH = 10
BACKOFF_SECONDS = 60  # a failed event waits attempts x this after it was received, so a short PayPal outage is survived


def _pending(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            select(webhook_events)
            .where(webhook_events.c.event_type == "CUSTOMER.DISPUTE.CREATED", webhook_events.c.processed_at.is_(None))
            .order_by(webhook_events.c.received_at).limit(BATCH)
        ).mappings()]
    now = datetime.now(timezone.utc)
    # shortcut: backoff counts from received_at (no last-attempt column), and 10 waiting events could crowd the batch
    return [e for e in rows if now - aware(e["received_at"]) >= timedelta(seconds=BACKOFF_SECONDS * e["attempts"])]


# shortcut: single API instance assumed (two would only cost an extra draft, the proposals unique key blocks a
# duplicate proposal); switch to SELECT ... FOR UPDATE SKIP LOCKED when a second instance exists
async def run_once(engine, models, client) -> None:
    for event in await asyncio.to_thread(_pending, engine):
        await handle(engine, models, client, event)


async def loop(app) -> None:
    while True:
        try:
            await run_once(app.state.engine, app.state.models, app.state.paypal_client)
        except Exception as exc:  # never let one bad pass stop the agent; CancelledError still propagates
            logger.error("agent pass failed: %s", type(exc).__name__)
        await asyncio.sleep(settings.agent_poll_seconds)
