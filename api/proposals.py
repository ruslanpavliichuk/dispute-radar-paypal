import logging
import secrets
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool

from api.config import settings
from api.db import proposals
from api.disputes import DISPUTE_ID, OPEN_STATUSES, linked_order
from api.paypal_client import PayPalClient

logger = logging.getLogger("dispute_radar.proposals")

TEXT_MAX = 2000


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
        "sent_at", "error")}


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


def _body(action: str, row, text: str, dispute: dict) -> dict:
    if action == "accept_claim":
        return {"note": text, "accept_claim_reason": "REASON_NOT_PROVIDED"}
    if action == "send_message":
        return {"message": text}
    if action == "provide_evidence":
        # shortcut: real tracking evidence needs seller input and a multipart upload; this sends the
        # approved text as an OTHER note. Accepted body is unverified until checked live.
        return {"evidences": [{"evidence_type": "OTHER", "notes": text}]}
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


@router.post("/{proposal_id}/approve")
async def approve(proposal_id: int, request: Request, body: Approve | None = None):
    engine = request.app.state.engine
    client: PayPalClient = request.app.state.paypal_client
    # stage 3 adds the prevention actions; until then refuse before the claim so nothing changes state
    if (await run_in_threadpool(_one, engine, proposal_id))["agent"] != "response":
        raise HTTPException(status_code=409, detail="this proposal type cannot be sent yet")
    # the seller-approval gate: nothing below runs unless this claim succeeded
    if not await run_in_threadpool(_move, engine, proposal_id, "PROPOSED", "SENDING", decided_at=now()):
        await run_in_threadpool(_one, engine, proposal_id)  # 404 when absent
        raise HTTPException(status_code=409, detail="proposal is not waiting for a decision")
    row = await run_in_threadpool(_one, engine, proposal_id)
    action, dispute_id = row["action"], row["subject_id"]

    async def back(status_code: int, detail: str):
        await run_in_threadpool(_move, engine, proposal_id, "SENDING", "PROPOSED")
        raise HTTPException(status_code=status_code, detail=detail)

    try:
        if not DISPUTE_ID.fullmatch(dispute_id):
            await back(409, "proposal has an invalid dispute id")
        try:
            dispute = await client.get_dispute(dispute_id)
        except Exception:
            logger.warning("proposal %s: could not re-read dispute", proposal_id)
            await back(502, "could not read the dispute from PayPal; nothing was sent")
        if dispute.get("status") not in OPEN_STATUSES or not await run_in_threadpool(linked_order, engine, dispute):
            await back(409, "dispute is no longer open")
        if not any(link.get("rel") == action for link in dispute.get("links") or []):
            await back(409, f"PayPal does not offer {action} on this dispute right now")
        try:
            payload = _body(action, row, ((body and body.note) or row["draft"] or "")[:TEXT_MAX], dispute)
        except HTTPException as exc:
            await back(exc.status_code, exc.detail)
    except HTTPException:
        raise  # already reverted by back()
    except Exception as exc:  # nothing was sent yet: never leave the row in SENDING (shortcut: a cancelled request still can)
        logger.error("proposal %s: checks failed before sending (%s)", proposal_id, type(exc).__name__)
        await back(500, "could not prepare the action; nothing was sent")

    try:
        response = await client.request(
            "POST", f"/v1/customer/disputes/{dispute_id}/{action.replace('_', '-')}",
            json=payload, headers={"PayPal-Request-Id": f"proposal-{proposal_id}"},
        )
        failure = None if response.is_success else f"PayPal {response.status_code}"
    except Exception as exc:
        failure = type(exc).__name__
    logger.info("proposal %s %s: %s", proposal_id, action, failure or "sent")
    if failure:
        await run_in_threadpool(_move, engine, proposal_id, "SENDING", "FAILED", error=failure)
        raise HTTPException(status_code=502, detail=f"PayPal did not accept the action ({failure})")
    await run_in_threadpool(_move, engine, proposal_id, "SENDING", "SENT", sent_at=now())
    return _public(await run_in_threadpool(_one, engine, proposal_id))
