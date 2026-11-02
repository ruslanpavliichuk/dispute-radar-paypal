"""Write simulated shipment events for a completed order straight to the database (no PayPal call, no endpoint).
The prevention agent in the running API picks them up and re-scores the order. Sandbox demo data only.

    uv run python -m scripts.simulate_tracking ORDER_ID accepted --day 1
    uv run python -m scripts.simulate_tracking ORDER_ID in_transit --day 3
    uv run python -m scripts.simulate_tracking ORDER_ID stuck_at_depot --day 16
    uv run python -m scripts.simulate_tracking list ORDER_ID

--day N dates the event N days after the order was placed. Suggested demo (the assumed delivery window is 14
days): accepted day 1, in_transit day 3 and stuck_at_depot day 5 stay below the bar; stuck_at_depot day 16 is
two days past the window and raises a tracking proposal and a goodwill refund; delivered afterwards withdraws them."""
import argparse
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select
from sqlalchemy.exc import SQLAlchemyError

from agents.prevention import SHIPPED
from api.config import settings
from api.db import make_engine, orders, tracking_events

ORDER_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TRACKING_NUMBER = re.compile(r"^[A-Z0-9]{6,32}$")
MAX_DAY = 60


class ScriptError(Exception):
    pass


def new_tracking_number() -> str:
    return f"DEMO{secrets.randbelow(10**10):010d}"


def order_events(engine, order_id: str) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(tracking_events).where(
            tracking_events.c.paypal_order_id == order_id).order_by(tracking_events.c.occurred_at, tracking_events.c.id)
        ).mappings()]


def add_event(engine, order_id: str, kind: str, day: int) -> dict:
    with engine.connect() as conn:
        order = conn.execute(select(orders).where(orders.c.paypal_order_id == order_id)).mappings().first()
    if order is None:
        raise ScriptError(f"no order {order_id} in our orders table")
    if order["status"] != "COMPLETED":
        raise ScriptError(f"order {order_id} is {order['status']}, not COMPLETED")
    existing = order_events(engine, order_id)
    if any(e["kind"] == "delivered" for e in existing):
        raise ScriptError(f"order {order_id} is already delivered; no more events")
    number = next((e["tracking_number"] for e in existing if e["tracking_number"]), None) or new_tracking_number()
    if not TRACKING_NUMBER.fullmatch(number):
        raise ScriptError("the tracking number on this order's earlier events is malformed; refusing")
    created = order["created_at"] if order["created_at"].tzinfo else order["created_at"].replace(tzinfo=timezone.utc)
    row = {"paypal_order_id": order_id, "kind": kind, "occurred_at": created + timedelta(days=day),
           "tracking_number": number, "carrier": "OTHER", "created_at": datetime.now(timezone.utc)}
    with engine.begin() as conn:
        conn.execute(insert(tracking_events).values(**row))
    return row


def run(args, engine) -> int:
    try:
        if args.command == "list":
            events = order_events(engine, args.order_id)
            for e in events:
                state = "done" if e["processed_at"] else f"waiting (attempts {e['attempts']})"
                print(f"{e['occurred_at']:%Y-%m-%d}  {e['kind']:16s} {e['tracking_number']}  {state}  {e['last_error'] or ''}")
            if not events:
                print("no events")
            return 0
        row = add_event(engine, args.order_id, args.kind, args.day)
        print(f"{row['kind']} for {row['paypal_order_id']} dated {row['occurred_at']:%Y-%m-%d} "
              f"(day {args.day}), tracking number {row['tracking_number']}")
        return 0
    except ScriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"error: database problem (is `make db` running?): {type(exc).__name__}", file=sys.stderr)
        return 1


def order_id_arg(value: str) -> str:
    if not ORDER_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("order id must be letters, digits and dashes (1 to 64)")
    return value


def day_arg(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,2}", value) or int(value) > MAX_DAY:
        raise argparse.ArgumentTypeError(f"day must be a whole number from 0 to {MAX_DAY}")
    return int(value)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first", help="an order id, or the word list")
    parser.add_argument("second", help=f"with list: the order id; otherwise the kind: {', '.join(SHIPPED)}")
    parser.add_argument("--day", type=day_arg, help="days after the order was placed (0 to 60); required except for list")
    args = parser.parse_args(argv)
    try:
        if args.first == "list":
            args.command, args.order_id = "list", order_id_arg(args.second)
        else:
            args.command, args.order_id, args.kind = "add", order_id_arg(args.first), args.second
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if args.command == "add":
        if args.kind not in SHIPPED:
            parser.error(f"kind must be one of {', '.join(SHIPPED)}")
        if args.day is None:
            parser.error("--day is required")
    return args


def main(argv=None) -> None:
    args = parse_args(argv)
    if not settings.database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        raise SystemExit(1)
    engine = make_engine(settings.database_url)
    try:
        raise SystemExit(run(args, engine))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
