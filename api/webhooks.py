import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from api.config import settings
from api.paypal_client import PayPalClient

logger = logging.getLogger("dispute_radar.webhooks")

MAX_BODY_BYTES = 256 * 1024

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def get_paypal_client(request: Request) -> PayPalClient:
    return request.app.state.paypal_client


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

    logger.info("verified webhook event_type=%s id=%s", event.get("event_type"), event.get("id"))
    return {"status": "received"}
