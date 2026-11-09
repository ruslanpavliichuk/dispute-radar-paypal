import logging
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool

from api.config import settings
from api.db import orders, proposals, tracking_events
from api.disputes import DISPUTE_ID, OPEN_STATUSES, linked_order
from api.paypal_client import ORDER_ID, PayPalClient

logger = logging.getLogger("dispute_radar.proposals")

TEXT_MAX = 2000
REFUND_NOTE_MAX = 255  # PayPal's note_to_payer limit
SHIPPING_KINDS = ("accepted", "in_transit", "stuck_at_depot", "out_for_delivery")
TRACKING_NUMBER = re.compile(r"^[A-Z0-9]{6,32}$")  # fullmatch
CARRIERS = {"OTHER"}


def require_admin(x_admin_token: str | None = Header(None)) -> None:
    if not settings.admin_token:
        raise HTTPException(status_code=503, detail="approval is switched off: ADMIN_TOKEN is not set")
    if x_admin_token is None or not secrets.compare_digest(
        x_admin_token.encode(), settings.admin_token.encode()
    ):
        raise HTTPException(status_code=401, detail="bad admin token")


router = APIRouter(prefix="/api/proposals", tags=["proposals"], dependencies=[Depends(require_admin)])


class Approve(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    note: str | None = Field(None, max_length=TEXT_MAX)  # the seller's edit of the draft


def now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch(engine, proposal_id: int):
    with engine.connect() as conn:
        return conn.execute(select(proposals).where(proposals.c.id == proposal_id)).mappings().first()


def _move(engine, proposal_id: int, old: str, new: str, **values) -> bool:
    """Conditional status change; False when the row was not in `old`."""
    with engine.begin() as conn:
        result = conn.execute(
            update(proposals)
            .where(proposals.c.id == proposal_id, proposals.c.status == old)
            .values(status=new, **values)
        )
    return result.rowcount == 1


def _one(engine, proposal_id: int) -> dict:
    row = _fetch(engine, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown proposal")
    return dict(row)


def _public(row) -> dict:
    # features and claim_source stay internal; the table holds no buyer text
    return {k: row[k] for k in (
        "id", "agent", "subject_id", "status", "action", "amount_minor", "currency", "claim_type", "win_prob",
        "ev", "defaulted", "reasons", "evidence", "draft", "draft_source", "created_at", "decided_at",
        "sent_at", "error", "opened_at", "due_at")}


@router.get("")
def list_proposals(request: Request):
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(select(proposals).order_by(proposals.c.id.desc()).limit(50)).mappings().all()
    return [_public(r) for r in rows]


@router.get("/{proposal_id}")
def get_proposal(proposal_id: int, request: Request):
    return _public(_one(request.app.state.engine, proposal_id))


@router.post("/{proposal_id}/reject")
def reject(proposal_id: int, request: Request):
    engine = request.app.state.engine
    if not _move(engine, proposal_id, "PROPOSED", "REJECTED", decided_at=now()):
        _one(engine, proposal_id)  # 404 when absent
        raise HTTPException(status_code=409, detail="proposal is not waiting for a decision")
    return _public(_one(engine, proposal_id))


def _issue(response) -> str:
    """PayPal's short error code (like INSUFFICIENT_FUNDS) for the seller; only a strict enum, never free text."""
    try:
        body = response.json()
        code = ((body.get("details") or [{}])[0].get("issue")) or body.get("name")
    except Exception:
        return ""
    return f" {code}" if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,60}", code) else ""


def _body(action: str, row, text: str, dispute: dict, tracking: dict | None = None) -> dict:
    if action == "accept_claim":
        return {"note": text, "accept_claim_reason": "REASON_NOT_PROVIDED"}
    if action == "send_message":
        return {"message": text}
    if action == "provide_evidence":
        # PayPal rejects OTHER on not-received disputes (400 EVIDENCE_TYPE_IS_NOT_ALLOWED); proof of fulfilment with
        # the tracking number is the type it takes. shortcut: no document uploads (they need multipart), tracking only.
        if not tracking:
            raise HTTPException(status_code=409, detail="no tracking number on file to send as evidence")
        return {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "notes": text, "evidence_info": {"tracking_info": [{
            "carrier_name": "OTHER", "carrier_name_other": "Demo courier", "tracking_number": tracking["tracking_number"]}]}}]}
    if action == "make_offer":
        if row["amount_minor"] is None or not row["currency"]:
            raise HTTPException(status_code=409, detail="proposal has no offer amount")
        amount = Decimal(row["amount_minor"]).scaleb(-2)
        disputed = dispute.get("dispute_amount") or {}
        if amount <= 0 or row["currency"] != disputed.get("currency_code"):
            raise HTTPException(status_code=409, detail="offer amount or currency does not fit the dispute")
        cap = ((dispute.get("refund_details") or {}).get("allowed_refund_amount") or {}).get("value", disputed.get("value"))
        try:
            too_much = amount > Decimal(cap)
        except (InvalidOperation, TypeError):
            too_much = True
        if too_much:
            raise HTTPException(status_code=409, detail="offer is above the refund PayPal allows")
        return {
            "note": text,
            "offer_amount": {"currency_code": row["currency"], "value": f"{amount:.2f}"},
            "offer_type": "REFUND",
        }
    raise HTTPException(status_code=409, detail=f"unsupported action {action}")


def _refund_in_flight(engine, order_id: str, statuses) -> bool:
    with engine.connect() as conn:
        return conn.execute(select(proposals.c.id).where(
            proposals.c.agent == "prevention", proposals.c.subject_id == f"{order_id}:goodwill_refund",
            proposals.c.status.in_(statuses))).first() is not None


def _response_money_in_flight(engine, capture_id: str) -> bool:
    # shortcut: proposals have no capture_id column, so match the capture fact in the evidence JSON; upgrade with a column
    fact = f"PayPal capture id: {capture_id}."
    with engine.connect() as conn:
        rows = conn.execute(select(proposals.c.evidence).where(
            proposals.c.agent == "response", proposals.c.status.in_(("PROPOSED", "SENDING", "SENT")))).all()
    return any(fact in (e.get("fact") or "") for (ev,) in rows for e in ev or [] if isinstance(e, dict))


PAYMENT_STATES = {"COMPLETED", "PENDING", "REFUNDED", "PARTIALLY_REFUNDED", "DECLINED", "FAILED", "VOIDED"}
REFUND_CURRENCIES = {"GBP", "EUR", "USD"}  # shortcut: other currencies need currency-specific decimals


async def _prepare_response(engine, client, proposal_id: int, row, body, back):
    action, dispute_id = row["action"], row["subject_id"]
    if not DISPUTE_ID.fullmatch(dispute_id):
        await back(409, "proposal has an invalid dispute id")
    try:
        dispute = await client.get_dispute(dispute_id)
    except Exception:
        logger.warning("proposal %s: could not re-read dispute", proposal_id)
        await back(502, "could not read the dispute from PayPal; nothing was sent")
    order = await run_in_threadpool(linked_order, engine, dispute)
    if dispute.get("status") not in OPEN_STATUSES or not order:
        await back(409, "dispute is no longer open")
    if action in ("accept_claim", "make_offer") and await run_in_threadpool(_refund_in_flight, engine, order["paypal_order_id"], ("SENDING", "SENT")):
        await back(409, "a goodwill refund was already sent for this order")
    if not any(link.get("rel") == action for link in dispute.get("links") or []):
        await back(409, f"PayPal does not offer {action} on this dispute right now")
    tracking = None
    if action == "provide_evidence":
        _, events = await run_in_threadpool(_local_facts, engine, order["paypal_order_id"])
        tracking = next((e for e in events if e["kind"] in SHIPPING_KINDS and TRACKING_NUMBER.fullmatch(e["tracking_number"] or "")), None)
    try:
        payload = _body(action, row, ((body and body.note) or row["draft"] or "")[:TEXT_MAX], dispute, tracking)
    except HTTPException as exc:
        await back(exc.status_code, exc.detail)
    return f"/v1/customer/disputes/{dispute_id}/{action.replace('_', '-')}", payload


def _local_facts(engine, order_id: str):
    with engine.connect() as conn:
        order = conn.execute(select(orders).where(orders.c.paypal_order_id == order_id)).mappings().first()
        events = [dict(r) for r in conn.execute(
            select(tracking_events).where(tracking_events.c.paypal_order_id == order_id)
            .order_by(tracking_events.c.occurred_at, tracking_events.c.id)).mappings()]
    return order, events


async def _prepare_prevention(engine, client, proposal_id: int, row, body, back):
    order_id, _, suffix = row["subject_id"].partition(":")
    action = row["action"]
    if not ORDER_ID.fullmatch(order_id) or action not in ("upload_tracking", "goodwill_refund") \
            or suffix != action:
        await back(409, "proposal has an invalid order or action")
    order, events = await run_in_threadpool(_local_facts, engine, order_id)
    capture_id = order and order["capture_id"]
    if not capture_id or order["status"] != "COMPLETED" or not ORDER_ID.fullmatch(capture_id):
        await back(409, "the order is not a completed, captured payment")
    if any(e["kind"] == "delivered" for e in events):
        await back(409, "the order was already delivered")
    try:
        paypal_order = await client.get_order(order_id)
    except Exception:
        logger.warning("proposal %s: could not re-read order", proposal_id)
        await back(502, "could not read the order from PayPal; nothing was sent")
    captures = [c for u in paypal_order.get("purchase_units") or [] for c in (u.get("payments") or {}).get("captures") or []]
    capture = next((c for c in captures if c.get("id") == capture_id), None)
    if capture is None:
        await back(409, "PayPal does not show our payment on this order")
    if capture.get("status") != "COMPLETED":
        shown = capture.get("status") if capture.get("status") in PAYMENT_STATES else "in an unexpected state"
        await back(409, f"PayPal shows the payment as {shown}")
    note = body and body.note
    if action == "upload_tracking":
        first = next((e for e in events if e["kind"] in SHIPPING_KINDS), None)
        if first is None or not TRACKING_NUMBER.fullmatch(first["tracking_number"] or "") \
                or first["carrier"] not in CARRIERS:
            await back(409, "no usable tracking number or carrier on file")
        # shortcut: PayPal's tracker API has no free-text field; the draft is shown to the seller and reaches the
        # buyer only through PayPal's own notification. Sandbox behaviour unverified.
        return f"/v2/checkout/orders/{order_id}/track", {
            "capture_id": capture_id, "tracking_number": first["tracking_number"], "carrier": "OTHER",
            "carrier_name_other": "Demo courier", "notify_payer": True}
    # shortcut: concurrent approvals for one capture are not serialised; PayPal bounds the total refunded
    if order["currency"] not in REFUND_CURRENCIES:
        await back(409, "refunds are not supported in this currency")
    if await run_in_threadpool(_response_money_in_flight, engine, capture_id):
        await back(409, "a response proposal for this order's dispute is open or sent")
    amount = order["amount_minor"] * settings.goodwill_share_pct // 100  # recomputed; the stored amount only has to agree
    if amount != row["amount_minor"]:
        await back(409, "the suggested amount changed, reject this proposal")
    if not 0 < amount <= order["amount_minor"]:
        await back(409, "refund amount does not fit the order")
    text = (note or row["draft"] or "")[:REFUND_NOTE_MAX]
    return f"/v2/payments/captures/{capture_id}/refund", {
        "amount": {"currency_code": order["currency"], "value": f"{Decimal(amount).scaleb(-2):.2f}"},
        "note_to_payer": text}


@router.post("/{proposal_id}/approve")
async def approve(proposal_id: int, request: Request, body: Approve | None = None):
    engine = request.app.state.engine
    client: PayPalClient = request.app.state.paypal_client
    first = await run_in_threadpool(_one, engine, proposal_id)
    prepare = {"response": _prepare_response, "prevention": _prepare_prevention}.get(first["agent"])
    if prepare is None:
        raise HTTPException(status_code=409, detail="this proposal type cannot be sent")
    if first["agent"] == "prevention" and body and body.note is not None:  # before the claim: nothing changes
        if first["action"] == "upload_tracking":
            raise HTTPException(status_code=409, detail="a note is not used for tracking uploads")
        if len(body.note) > REFUND_NOTE_MAX:
            raise HTTPException(status_code=422, detail=f"note is longer than {REFUND_NOTE_MAX} characters")
    # the seller-approval gate: nothing below runs unless this claim succeeded
    if not await run_in_threadpool(_move, engine, proposal_id, "PROPOSED", "SENDING", decided_at=now()):
        raise HTTPException(status_code=409, detail="proposal is not waiting for a decision")
    row = await run_in_threadpool(_one, engine, proposal_id)
    action = row["action"]

    async def back(status_code: int, detail: str):
        await run_in_threadpool(_move, engine, proposal_id, "SENDING", "PROPOSED")
        raise HTTPException(status_code=status_code, detail=detail)

    try:
        path, payload = await prepare(engine, client, proposal_id, row, body, back)
    except HTTPException:
        raise  # already reverted by back()
    except Exception as exc:  # nothing was sent yet: never leave the row in SENDING (shortcut: a cancelled request still can)
        logger.error("proposal %s: checks failed before sending (%s)", proposal_id, type(exc).__name__)
        await back(500, "could not prepare the action; nothing was sent")

    try:
        response = await client.request(
            "POST", path, json=payload, headers={"PayPal-Request-Id": f"proposal-{proposal_id}"})
        failure = None if response.is_success else f"PayPal {response.status_code}{_issue(response)}"
        if not failure and action == "goodwill_refund":
            try:
                if response.json().get("status") in ("FAILED", "CANCELLED"):
                    failure = f"refund {response.json()['status']}"
            except Exception:
                pass  # a 2xx without a readable body still counts as sent
    except Exception as exc:
        failure = type(exc).__name__
    logger.info("proposal %s %s: %s", proposal_id, action, failure or "sent")
    if failure:
        await run_in_threadpool(_move, engine, proposal_id, "SENDING", "FAILED", error=failure)
        raise HTTPException(status_code=502, detail=f"PayPal did not accept the action ({failure})")
    await run_in_threadpool(_move, engine, proposal_id, "SENDING", "SENT", sent_at=now())
    return _public(await run_in_threadpool(_one, engine, proposal_id))
