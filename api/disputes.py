"""Pieces shared by the dispute simulator script and the response agent."""
import re

from sqlalchemy import select

from api.db import orders

DISPUTE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")  # match with fullmatch; the id goes into PayPal URLs
OPEN_STATUSES = {"OPEN", "WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE", "UNDER_REVIEW"}


def linked_order(engine, dispute: dict):
    """Our orders row for the capture the dispute is about, or None when it is not ours."""
    capture_ids = [
        t.get("seller_transaction_id") for t in dispute.get("disputed_transactions") or []
    ]
    capture_ids = [c for c in capture_ids if c]
    if not capture_ids:
        return None
    with engine.connect() as conn:
        return conn.execute(
            select(orders).where(orders.c.capture_id.in_(capture_ids))
        ).mappings().first()
