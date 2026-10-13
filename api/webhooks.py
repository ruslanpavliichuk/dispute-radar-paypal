import logging

from fastapi import APIRouter, HTTPException, Request

from api.config import settings
from api.paypal_client import PayPalClient

logger = logging.getLogger("dispute_radar.webhooks")

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def get_paypal_client(request: Request) -> PayPalClient:
    return request.app.state.paypal_client


@router.post("/paypal")
async def paypal_webhook(request: Request):
    paypal_client = get_paypal_client(request)
    body = await request.body()
    verified = await paypal_client.verify_webhook_signature(
        headers=request.headers, body=body.decode(), webhook_id=settings.paypal_webhook_id
    )
    if not verified:
        raise HTTPException(status_code=400, detail="invalid webhook signature")

    event = await request.json()
    logger.info("verified webhook event_type=%s id=%s", event.get("event_type"), event.get("id"))
    return {"status": "received"}
