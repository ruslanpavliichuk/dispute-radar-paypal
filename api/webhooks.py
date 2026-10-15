import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from api.config import settings
from api.db import webhook_events
from api.paypal_client import PayPalClient

logger = logging.getLogger("dispute_radar.webhooks")

MAX_BODY_BYTES = 256 * 1024

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def get_paypal_client(request: Request) -> PayPalClient:
    return request.app.state.paypal_client


def _store_event(engine, event: dict) -> bool:
    """Record one verified event. False when this event id was already recorded."""
    resource = event.get("resource")
    resource = resource if isinstance(resource, dict) else {}
    resource_id = resource.get("dispute_id") or resource.get("id")
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(webhook_events).values(
                    event_id=event["id"],
                    event_type=event["event_type"][:100],
                    resource_id=str(resource_id)[:100] if resource_id else None,
                    received_at=datetime.now(timezone.utc),
                )
            )
    except IntegrityError:
        return False
    return True


@router.post("/paypal")
async def paypal_webhook(request: Request):
    if not settings.paypal_webhook_id:
        logger.error("PAYPAL_WEBHOOK_ID is not set; cannot verify webhooks")
        raise HTTPException(status_code=503, detail="webhook verification not configured")

    # stream so a chunked upload without Content-Length is cut off at the cap, not buffered
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
        chunks.append(chunk)

    try:
        body = b"".join(chunks).decode()
        event = json.loads(body)
    except (ValueError, RecursionError):  # RecursionError: deeply nested JSON
        raise HTTPException(status_code=400, detail="body is not valid JSON") from None
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="body is not a JSON object")

    try:
        verified = await get_paypal_client(request).verify_webhook_signature(
            headers=request.headers, body=body, webhook_id=settings.paypal_webhook_id
        )
    except (httpx.HTTPError, ValueError, KeyError):
        # includes a non-JSON or malformed 200 from PayPal; 503 makes PayPal retry the
        # delivery instead of dropping it as a bad signature
        logger.exception("PayPal verify-webhook-signature unavailable")
        raise HTTPException(status_code=503, detail="verification unavailable") from None
    if not verified:
        raise HTTPException(status_code=400, detail="invalid webhook signature")

    event_id = event.get("id")
    # validated up front so the primary key is the only thing _store_event can collide on
    if not isinstance(event_id, str) or not 0 < len(event_id) <= 100:
        raise HTTPException(status_code=400, detail="event has no usable id")
    if not isinstance(event.get("event_type"), str):
        event["event_type"] = "UNKNOWN"
    try:
        stored = await run_in_threadpool(_store_event, request.app.state.engine, event)
    except SQLAlchemyError:
        # 503 so PayPal redelivers once the database is back
        logger.exception("could not store webhook event %r", event_id)
        raise HTTPException(status_code=503, detail="could not record event") from None
    if not stored:
        logger.info("duplicate webhook event id=%r ignored", event_id)
        return {"status": "duplicate"}

    logger.info("verified webhook event_type=%r id=%r", event["event_type"], event_id)
    return {"status": "received"}
