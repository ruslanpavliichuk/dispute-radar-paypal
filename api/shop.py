import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi import Path as PathParam
from paypalserversdk.exceptions.api_exception import ApiException
from paypalserversdk.models.amount_with_breakdown import AmountWithBreakdown
from paypalserversdk.models.checkout_payment_intent import CheckoutPaymentIntent
from paypalserversdk.models.order_request import OrderRequest
from paypalserversdk.models.purchase_unit_request import PurchaseUnitRequest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from api.db import orders

logger = logging.getLogger("dispute_radar.shop")

# The server's own price list is the only source of amounts: the browser sends a
# product id and nothing else.
CATALOGUE = {
    p["id"]: p for p in json.loads(Path(__file__).with_name("catalogue.json").read_text())
}
PAID = {"COMPLETED", "PENDING", "PARTIALLY_REFUNDED", "REFUNDED"}
FAILED = {"DECLINED", "FAILED"}

router = APIRouter(prefix="/api/shop", tags=["shop"])


class CreateOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str


def money(minor: int) -> str:
    """Pence as PayPal's decimal string, with no floats involved."""
    return f"{minor // 100}.{minor % 100:02d}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _get(obj, *names):
    """Walk optional SDK fields. The SDK leaves a field it did not receive unset
    (no attribute at all), so plain attribute access raises after money has moved."""
    for name in names:
        obj = getattr(obj, name, None)
        if obj is None:
            return None
    return obj


def _fetch(engine, order_id: str):
    with engine.connect() as conn:
        return conn.execute(
            select(orders).where(orders.c.paypal_order_id == order_id)
        ).mappings().first()


def _record(engine, order_id: str, **values) -> bool:
    """Write the capture result once. False means another request already did."""
    try:
        with engine.begin() as conn:
            result = conn.execute(
                update(orders)
                .where(orders.c.paypal_order_id == order_id, orders.c.status == "CREATED")
                .values(**values)
            )
        return result.rowcount == 1
    except SQLAlchemyError:
        # PayPal has already moved the money, so make this impossible to miss in the logs
        logger.critical("ORDER %s captured at PayPal but DB write failed: %s", order_id, values, exc_info=True)
        raise HTTPException(status_code=500, detail="order recorded incorrectly") from None


def _stored(row) -> dict:
    if row["status"] in PAID:
        return {"status": row["status"], "capture_id": row["capture_id"]}
    # DECLINED / FAILED / MISMATCH / UNKNOWN are terminal: a retry replays the same
    # PayPal answer, so the buyer has to start a new order
    raise HTTPException(status_code=409, detail=f"order is {row['status'].lower()}; start a new order")


@router.get("/products")
def list_products():
    return [
        {
            "id": p["id"],
            "title": p["title"],
            "brand": p["brand"],
            "image_url": p["image_url"],
            "price": money(p["price_minor"]),
            "currency": p["currency"],
        }
        for p in CATALOGUE.values()
    ]


@router.post("/orders")
def create_order(body: CreateOrder, request: Request):
    product = CATALOGUE.get(body.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="unknown product")

    order_request = OrderRequest(
        intent=CheckoutPaymentIntent.CAPTURE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=product["id"],
                amount=AmountWithBreakdown(
                    currency_code=product["currency"], value=money(product["price_minor"])
                ),
            )
        ],
    )
    try:
        result = request.app.state.orders_api.create_order(
            {
                "body": order_request,
                "prefer": "return=minimal",
                "paypal_request_id": str(uuid.uuid4()),
            }
        )
        order_id = result.body.id
    except Exception:
        logger.exception("PayPal create_order failed")
        raise HTTPException(status_code=502, detail="could not create order") from None

    try:
        with request.app.state.engine.begin() as conn:
            conn.execute(
                insert(orders).values(
                    paypal_order_id=order_id,
                    product_id=product["id"],
                    amount_minor=product["price_minor"],
                    currency=product["currency"],
                    status="CREATED",
                    created_at=now(),
                )
            )
    except SQLAlchemyError:
        logger.critical("PayPal order %s created but DB insert failed", order_id, exc_info=True)
        raise HTTPException(status_code=500, detail="could not save order") from None
    return {"order_id": order_id}


@router.post("/orders/{order_id}/capture")
def capture_order(request: Request, order_id: str = PathParam(pattern=r"^[A-Z0-9]{1,36}$")):
    engine = request.app.state.engine
    row = _fetch(engine, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown order")
    if row["status"] != "CREATED":
        return _stored(row)

    api = request.app.state.orders_api
    already_captured_or_declined = False
    try:
        order = api.capture_order(
            {
                "id": order_id,
                "prefer": "return=representation",
                # PayPal replays the original response for a repeated id, so a double
                # click or retry can never capture twice
                "paypal_request_id": f"capture-{order_id}",
            }
        ).body
    except ApiException as exc:
        if exc.response_code != 422:
            logger.error("PayPal capture_order failed for %s: %s", order_id, exc)
            raise HTTPException(status_code=502, detail="capture failed") from None
        # 422 covers ORDER_ALREADY_CAPTURED as well as declined instruments: read the order
        already_captured_or_declined = True
        try:
            order = api.get_order({"id": order_id}).body
        except Exception:
            logger.exception("PayPal get_order failed for %s", order_id)
            raise HTTPException(status_code=502, detail="capture failed") from None
    except Exception:
        # timeouts and malformed responses: state at PayPal is unknown, same request id is safe to retry
        logger.exception("PayPal capture_order errored for %s", order_id)
        raise HTTPException(status_code=502, detail="capture failed") from None

    units = _get(order, "purchase_units") or []
    captures = _get(units[0], "payments", "captures") if units else None
    capture = captures[0] if captures else None
    if capture is None:
        if already_captured_or_declined:
            raise HTTPException(status_code=402, detail="payment was not completed")
        logger.critical("PayPal order %s has no capture after a successful capture call", order_id)
        raise HTTPException(status_code=502, detail="no capture on order")

    capture_id = _get(capture, "id")
    status = _get(capture, "status")
    currency = _get(capture, "amount", "currency_code")
    value = _get(capture, "amount", "value")
    if capture_id is None or currency != row["currency"] or value != money(row["amount_minor"]):
        logger.critical("ORDER %s capture mismatch: expected %s %s, got %s %s (capture %s)",
                        order_id, money(row["amount_minor"]), row["currency"], value, currency, capture_id)
        _record(engine, order_id, status="MISMATCH", capture_id=capture_id, captured_at=now())
        raise HTTPException(status_code=502, detail="capture does not match order")

    status = str(status)
    if status not in PAID | FAILED:
        logger.critical("ORDER %s capture %s has unexpected status %r", order_id, capture_id, status)
        _record(engine, order_id, status="UNKNOWN", capture_id=capture_id, captured_at=now())
        raise HTTPException(status_code=502, detail="unexpected payment status")

    payer_id = _get(order, "payer", "payer_id") or _get(order, "payment_source", "paypal", "account_id")
    if not _record(
        engine, order_id, status=status, capture_id=capture_id, payer_id=payer_id, captured_at=now()
    ):
        # a concurrent request recorded the same capture first; report what it stored
        return _stored(_fetch(engine, order_id))
    if status in FAILED:
        raise HTTPException(status_code=402, detail=f"payment {status.lower()}")
    return {"status": status, "capture_id": capture_id}
