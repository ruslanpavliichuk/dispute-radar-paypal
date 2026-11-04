"""Response agent core (T11b): one verified dispute-created event -> one stored proposal (an action and a plain
draft note). It only reads from PayPal; sending anything is T11c, behind the seller's approval.

Buyer text is untrusted: it reaches the claim classifier (only when the reason code is unmapped) and nothing
else. It is never stored, logged, or put into the drafting prompt."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx
from pydantic import ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from agents.policies import decide, paypal_action
from api.config import settings
from api.db import proposals, webhook_events
from api.disputes import DISPUTE_ID, OPEN_STATUSES, linked_order
from api.scoring import OutcomeRequest, classify, score_tabular
from api.shop import CATALOGUE, money
from ml.generate_disputes import API_REASON_TO_CLAIM_TYPE
from ml.llm import complete_json

logger = logging.getLogger("dispute_radar.agent")

MAX_ATTEMPTS = 3
MAX_DRAFT_CHARS = 600
NUMBER = re.compile(r"\d+(?:\.\d+)?")
# shortcut: catches the obvious link and address shapes, not every obfuscation; the seller reads every draft before it is sent
LINK = re.compile(r"://|www\.|@|\b\w+\.(?:com|net|org|uk|io)\b", re.I)
# not knowable from the dispute or our database: defaulted, and recorded on the proposal
NOT_KNOWN = ("tracking_uploaded", "delivery_confirmed", "signature_confirmed", "shipped_to_paypal_address", "photos_supplied")


class Skip(ValueError):
    """The event can never become a proposal; the message is stored as the reason."""


def to_features(dispute: dict, cfg=settings, clf=None, now: datetime | None = None):
    """-> (OutcomeRequest fields, names we defaulted, claim_type, claim_source)."""
    stage = dispute.get("dispute_life_cycle_stage")
    if stage not in ("INQUIRY", "CHARGEBACK"):
        raise Skip("unknown lifecycle stage")
    amount = dispute.get("dispute_amount") or {}
    if amount.get("currency_code") != "GBP":
        raise Skip("not a GBP dispute")
    try:
        value = float(amount["value"])
        created = datetime.fromisoformat(dispute["create_time"])
    except (KeyError, TypeError, ValueError):
        raise Skip("dispute amount or create_time unreadable") from None

    claim_type = API_REASON_TO_CLAIM_TYPE.get(dispute.get("reason"))
    claim_source = "reason"
    if claim_type is None:  # reason code we do not map: let the classifier read the longest buyer message
        texts = [m.get("content") for m in dispute.get("messages") or [] if m.get("posted_by") == "BUYER"]
        text = max((t.strip() for t in texts if isinstance(t, str)), key=len, default="")[:5000]
        claim_type = classify(clf, text).label if text else "other"
        claim_source = "classifier" if text else "reason"

    days = (now or datetime.now(timezone.utc)) - created
    features = {
        "claim_type": claim_type, "lifecycle_stage": stage, "order_value": value,
        **dict.fromkeys(NOT_KNOWN, False),
        "response_days": min(max(days.days, 0), 365),
        "seller_account_age_months": cfg.seller_account_age_months,
        "category": "home",  # a valid generator category; our catalogue has no category field
    }
    try:
        features = OutcomeRequest(**features).model_dump()
    except ValidationError:
        raise Skip("dispute fields outside the model's range") from None
    return features, [*NOT_KNOWN, "seller_account_age_months", "category"], claim_type, claim_source


def build_evidence(dispute: dict, order) -> list[dict]:
    """Facts from our own order and the dispute's reason and amount. No buyer text."""
    paid = order["captured_at"] or order["created_at"]
    amount = dispute["dispute_amount"]
    product = CATALOGUE.get(order["product_id"], {}).get("title", order["product_id"])
    facts = [
        ("order", f"Order paid: {money(order['amount_minor'])} {order['currency']} on {paid.day} {paid:%B %Y}."),
        ("product", f"Product ordered: {product}."),
        ("capture", f"PayPal capture id: {order['capture_id']}."),
        ("dispute_reason", f"Reason given on the dispute: {dispute.get('reason')}."),
        ("dispute_amount", f"Amount in dispute: {amount['value']} {amount['currency_code']}."),
    ]
    return [{"id": f"E{i}", "kind": kind, "fact": fact} for i, (kind, fact) in enumerate(facts, 1)]


def _template(evidence: list[dict], action: str, offer: str | None) -> str:
    lead = {
        "make_offer": f"We offer a partial refund of {offer}. ",
        "accept_claim": "We accept this claim and will refund the buyer in full. ",
    }.get(action, "Our records for this order show the following. ")
    return lead + " ".join(e["fact"] for e in evidence)


def _valid(reply, evidence: list[dict], offer: str | None) -> bool:
    if not isinstance(reply, dict) or not isinstance(reply.get("message"), str):
        return False
    message, cited = reply["message"], reply.get("cited_evidence_ids")
    if not isinstance(cited, list) or not cited or not set(cited) <= {e["id"] for e in evidence}:
        return False
    known = set(NUMBER.findall(" ".join([e["fact"] for e in evidence] + [offer or ""])))
    return len(message) <= MAX_DRAFT_CHARS and not LINK.search(message) and set(NUMBER.findall(message)) <= known


def draft(evidence: list[dict], claim_type: str, action: str, amount_minor: int | None, currency: str):
    """-> (note, 'llm' | 'template'). The LLM sees the evidence, the claim label and the action name only."""
    offer = f"{money(amount_minor)} {currency}" if action == "make_offer" else None
    ids = [e["id"] for e in evidence]
    schema = {"type": "object", "required": ["message", "cited_evidence_ids"], "properties": {
        "message": {"type": "string"},
        "cited_evidence_ids": {"type": "array", "items": {"type": "string", "enum": ids}}}}
    prompt = (
        "Write a short note from an online seller to PayPal about a buyer dispute. Plain and factual, British "
        "spelling, at most 80 words, no greeting or sign-off, no filler, no marketing tone. State only what the "
        "evidence says. Promise nothing. Mention no amount that is not in the evidence"
        f"{' or the offer amount' if offer else ''}. Cite the evidence ids you used.\n"
        f"Claim type: {claim_type}\nAction: {action}\n"
        + (f"Offer amount: {offer}\n" if offer else "")
        + f"Evidence: {json.dumps(evidence)}"
    )
    try:
        reply = complete_json(prompt, schema, tries=2)  # sync: handle() runs this in a worker thread
        if _valid(reply, evidence, offer):
            return reply["message"], "llm"
        logger.warning("draft failed the post-check; using the template")
    except Exception as exc:  # no key, rate limit, bad JSON: the draft must never block the proposal
        logger.warning("draft LLM unavailable (%s); using the template", type(exc).__name__)
    return _template(evidence, action, offer), "template"


def _stamp(engine, event_id, error: str | None = None, table=webhook_events, key: str = "event_id") -> None:
    values = {"processed_at": datetime.now(timezone.utc), "last_error": error}  # success clears an earlier attempt's error
    with engine.begin() as conn:
        conn.execute(update(table).where(table.c[key] == event_id).values(**values))


def _fail(engine, event: dict, exc: Exception, table=webhook_events, key: str = "event_id") -> None:
    """Transient failure: count it, keep only the exception type, give up after MAX_ATTEMPTS."""
    attempts = event["attempts"] + 1
    values = {"attempts": attempts, "last_error": type(exc).__name__}
    if attempts >= MAX_ATTEMPTS:
        values["processed_at"] = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(update(table).where(table.c[key] == event[key]).values(**values))
    logger.error("event attempt %d failed: %s", attempts, type(exc).__name__)


def _when(dispute: dict, key: str) -> datetime | None:
    try:
        return datetime.fromisoformat(dispute[key])
    except (KeyError, TypeError, ValueError):
        return None  # the timeline just shows no bar for this dispute


def _propose(engine, models, event: dict, dispute: dict) -> None:
    order = linked_order(engine, dispute)
    if order is None:
        raise Skip("dispute is not for a capture in our orders")
    if dispute.get("status") not in OPEN_STATUSES:
        raise Skip("dispute is not open")
    features, defaulted, claim_type, claim_source = to_features(dispute, settings, models["claim"])
    scored = score_tabular(models["outcome"], OutcomeRequest(**features), "outcome")
    value = round(features["order_value"] * 100)
    decision = decide(scored.score, value, features["lifecycle_stage"], settings.dispute_fee_minor,
                      settings.offer_share_pct)
    action = paypal_action(decision["action"], dispute.get("reason"))
    currency = dispute["dispute_amount"]["currency_code"]
    amount = {"make_offer": decision["offer_amount_minor"], "accept_claim": value}.get(action)
    evidence = build_evidence(dispute, order)
    note, source = draft(evidence, claim_type, action, amount, currency)
    try:
        with engine.begin() as conn:
            conn.execute(insert(proposals).values(
                agent="response", subject_id=event["resource_id"], source_event_id=event["event_id"],
                action=action, amount_minor=amount, currency=currency if amount is not None else None,
                claim_type=claim_type, claim_source=claim_source, features=features, defaulted=defaulted,
                win_prob=scored.score, reasons=[r.model_dump() for r in scored.reasons], ev=decision["ev"],
                evidence=evidence, draft=note, draft_source=source, created_at=datetime.now(timezone.utc),
                opened_at=_when(dispute, "create_time"), due_at=_when(dispute, "seller_response_due_date"),
            ))
    except IntegrityError:
        with engine.connect() as conn:  # only the unique key means "already proposed"; anything else is a real failure
            if conn.execute(select(proposals.c.id).where(proposals.c.agent == "response",
                                                         proposals.c.subject_id == event["resource_id"])).first() is None:
                raise
    logger.info("proposal for dispute %s: %s", event["resource_id"], action)


async def handle(engine, models, client, event: dict) -> None:
    """Turn one event into a proposal, or stamp it with why not. Blocking work runs in worker threads."""
    try:
        dispute_id = event["resource_id"] or ""
        if not DISPUTE_ID.fullmatch(dispute_id):
            raise Skip("invalid dispute id")  # checked before the id goes into a PayPal URL
        try:
            dispute = await client.get_dispute(dispute_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            raise Skip("dispute not found at PayPal") from None
        await asyncio.to_thread(_propose, engine, models, event, dispute)
        await asyncio.to_thread(_stamp, engine, event["event_id"])
    except Skip as skip:
        await asyncio.to_thread(_stamp, engine, event["event_id"], str(skip))
    except Exception as exc:  # PayPal 5xx or timeout, database, anything unexpected: retry a few times
        await asyncio.to_thread(_fail, engine, event, exc)
