"""Orders that need a human: capture mismatch, unknown status, or PENDING for too long."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, func, or_, select

from api.db import orders
from api.proposals import require_admin
from api.shop import PENDING_GRACE


def needs_attention():
    return or_(
        orders.c.status.in_(("MISMATCH", "UNKNOWN")),
        and_(orders.c.status == "PENDING", orders.c.created_at < datetime.now(timezone.utc) - PENDING_GRACE),
    )


def attention_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(orders).where(needs_attention())).scalar_one()


router = APIRouter(prefix="/api/orders", tags=["orders"], dependencies=[Depends(require_admin)])


@router.get("/attention")
def attention(request: Request):
    with request.app.state.engine.connect() as conn:
        rows = conn.execute(
            select(orders.c.paypal_order_id, orders.c.status, orders.c.amount_minor, orders.c.currency,
                   orders.c.created_at, orders.c.capture_id)
            .where(needs_attention()).order_by(orders.c.created_at.desc()).limit(50)
        ).mappings().all()
    return [dict(r) for r in rows]
