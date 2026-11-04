"""Give a fresh deployment something to look at: three clearly labelled DEMO orders with shipment events, so the
running prevention agent scores them with the real model and the dashboard, timeline and Studio are not empty.
Safe to run twice. Demo orders do not exist at PayPal, so approving their proposals will fail at the PayPal call;
buy something in the shop for a live approval.

    python -m scripts.seed_demo
"""
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select

from api.config import settings
from api.db import init_db, make_engine, orders
from api.shop import CATALOGUE
from scripts.simulate_tracking import add_event

# (order id, days since purchase, shipment steps as (kind, day))
DEMO = [
    ("DEMO-LATE-1", 17, [("accepted", 1), ("in_transit", 3), ("stuck_at_depot", 16)]),
    ("DEMO-LATE-2", 18, [("accepted", 1), ("in_transit", 4), ("stuck_at_depot", 17)]),
    ("DEMO-FINE-1", 6, [("accepted", 1), ("in_transit", 3)]),  # on schedule: the agent proposes nothing
]


def seed(engine) -> list[str]:
    products = list(CATALOGUE.values())
    made = []
    for i, (order_id, age, steps) in enumerate(DEMO):
        with engine.connect() as conn:
            if conn.execute(select(orders.c.paypal_order_id).where(orders.c.paypal_order_id == order_id)).first():
                continue
        product = products[i % len(products)]
        placed = datetime.now(timezone.utc) - timedelta(days=age)
        with engine.begin() as conn:
            conn.execute(insert(orders).values(
                paypal_order_id=order_id, product_id=product["id"], amount_minor=product["price_minor"],
                currency=product["currency"], status="COMPLETED", capture_id=f"{order_id}-CAPTURE",
                payer_id="DEMO-BUYER", created_at=placed, captured_at=placed))
        for kind, day in steps:
            add_event(engine, order_id, kind, day)
        made.append(order_id)
    return made


def main() -> None:
    if not settings.database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        raise SystemExit(1)
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        made = seed(engine)
        print(f"added {', '.join(made)}" if made else "demo orders already there")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
