"""Prevention agent core (T12 stage 2): one tracking event -> re-score the order -> maybe a stored proposal.
Nothing here calls PayPal; sending the tracking upload or the goodwill refund is stage 3, behind the seller's approval.

The risk model has no "stuck at depot" feature. It only sees shipped / delivered / time since purchase, ship and
estimate, so a stuck parcel raises the score only because time passes while it is not delivered."""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from agents.reconcile import aware
from agents.response import Skip, _fail, _stamp
from api.config import settings
from api.db import orders, proposals, tracking_events
from api.scoring import RiskRequest, score_tabular
from api.shop import money

logger = logging.getLogger("dispute_radar.prevention")

SHIPPED = ("accepted", "in_transit", "stuck_at_depot", "out_for_delivery", "delivered")
EARLY_DAYS = 8  # events dated before this many days after purchase use the early bar
DAY = 86400
# not knowable from our data: defaulted, and recorded on the proposal
DEFAULTED = ["est_window_days", "freight_share", "category", "n_items", "same_state", "seller_late_rate",
             "seller_low_review_rate"]


def to_risk_features(order, events: list[dict], event: dict, cfg=settings):
    """-> (RiskRequest fields, names we defaulted). State = the order's events dated up to this event's time."""
    t = aware(event["occurred_at"])
    seen = sorted((e for e in events if aware(e["occurred_at"]) <= t), key=lambda e: aware(e["occurred_at"]))
    shipped = [aware(e["occurred_at"]) for e in seen if e["kind"] in SHIPPED]
    delivered = [aware(e["occurred_at"]) for e in seen if e["kind"] == "delivered"]
    created = aware(order["created_at"])
    since_purchase = (t - created).total_seconds() / DAY
    est = created.timestamp() / DAY + cfg.est_window_days
    end = delivered[0] if delivered else t  # calendar days, like the training label
    late = int(end.timestamp() // DAY - est // 1)
    return {
        "order_value": order["amount_minor"] / 100, "freight_share": 0, "category": "unknown", "n_items": 1,
        "est_window_days": cfg.est_window_days, "same_state": False, "seller_late_rate": None,
        "seller_low_review_rate": None, "days_since_purchase": since_purchase,
        "days_to_est": cfg.est_window_days - since_purchase, "delivered_yet": bool(delivered),
        "days_late_so_far": float(max(late, 0)), "shipped_yet": bool(shipped),
        "days_since_ship": (t - shipped[0]).total_seconds() / DAY if shipped else None,
    }, list(DEFAULTED)


def _upload_note(first: dict) -> str:
    when = aware(first["occurred_at"])
    carrier = f" ({first['carrier']})" if first["carrier"] not in (None, "", "OTHER") else ""  # OTHER is PayPal's placeholder
    return (f"Your order was shipped on {when.day} {when:%B %Y}; tracking number {first['tracking_number']}{carrier}. "
            "It is later than expected and we are looking into it.")


def _goodwill_note(amount: int, currency: str) -> str:
    return (f"We are sorry your order is later than expected. As a goodwill gesture we would like to refund "
            f"{money(amount)} {currency}.")


def _add(engine, **values) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(insert(proposals).values(
                agent="prevention", created_at=datetime.now(timezone.utc), draft_source="template",
                **values))
    except IntegrityError:
        with engine.connect() as conn:  # only the unique key means "already proposed"; anything else is a real failure
            if conn.execute(select(proposals.c.id).where(proposals.c.agent == "prevention",
                                                         proposals.c.subject_id == values["subject_id"])).first() is None:
                raise


def _process(engine, models, event: dict) -> None:
    oid = event["paypal_order_id"]
    with engine.connect() as conn:
        order = conn.execute(select(orders).where(orders.c.paypal_order_id == oid)).mappings().first()
        events = [dict(r) for r in conn.execute(
            select(tracking_events).where(tracking_events.c.paypal_order_id == oid)).mappings()]
    if order is None:
        raise Skip("unknown order")
    if order["status"] != "COMPLETED":
        raise Skip("order is not completed")
    subjects = [f"{oid}:upload_tracking", f"{oid}:goodwill_refund"]
    if event["kind"] == "delivered":  # it arrived: whatever was proposed is moot
        with engine.begin() as conn:
            conn.execute(update(proposals).where(
                proposals.c.agent == "prevention", proposals.c.subject_id.in_(subjects), proposals.c.status == "PROPOSED",
            ).values(status="REJECTED", error="superseded", decided_at=datetime.now(timezone.utc)))
        return
    if any(e["kind"] == "delivered" for e in events):
        return  # delivered at any date: a backdated shipping event must not revive proposals
    feats, defaulted = to_risk_features(order, events, event, settings)
    if not feats["shipped_yet"]:
        return  # nothing to prevent before the first shipping event
    scored = score_tabular(models["risk"], RiskRequest(**feats), "risk")
    risk, value = scored.score, order["amount_minor"]
    bar = settings.prevention_min_risk_early if feats["days_since_purchase"] < EARLY_DAYS else settings.prevention_min_risk_late
    if risk < bar:
        return
    ev = {"risk": risk, "value_minor": value, "bar": bar, "expected_loss_minor": round(risk * value)}
    common = dict(currency=order["currency"], features=feats, defaulted=defaulted, ev=ev,
                  reasons=[r.model_dump() for r in scored.reasons])
    first = min((e for e in events if e["kind"] in SHIPPED and aware(e["occurred_at"]) <= aware(event["occurred_at"])),
                key=lambda e: aware(e["occurred_at"]))
    if not first["tracking_number"]:
        raise Skip("no tracking number")
    _add(engine, subject_id=subjects[0], action="upload_tracking", draft=_upload_note(first), **common)
    share = settings.goodwill_share_pct
    if feats["days_to_est"] < 0 and risk * 100 > share:
        amount = value * share // 100
        _add(engine, subject_id=subjects[1], action="goodwill_refund", amount_minor=amount,
             draft=_goodwill_note(amount, order["currency"]), **common)
    logger.info("prevention proposal for order %s (risk %.2f)", oid, risk)


async def handle_tracking_event(engine, models, event: dict) -> None:
    """Blocking work runs in worker threads; a Skip is stamped with its reason, anything else retries with backoff."""
    kw = {"table": tracking_events, "key": "id"}
    try:
        await asyncio.to_thread(_process, engine, models, event)
        await asyncio.to_thread(_stamp, engine, event["id"], None, **kw)
    except Skip as skip:
        await asyncio.to_thread(_stamp, engine, event["id"], str(skip), **kw)
    except Exception as exc:
        await asyncio.to_thread(_fail, engine, event, exc, **kw)
