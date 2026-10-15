"""Drive a buyer-filed sandbox dispute through its stages. Sandbox only.

The buyer files the dispute by hand at sandbox.paypal.com ("Report a problem"); PayPal's
create-dispute API is limited release. This script then moves it along:

    uv run python -m scripts.simulate_dispute list
    uv run python -m scripts.simulate_dispute show PP-R-ABC-123
    uv run python -m scripts.simulate_dispute evidence PP-R-ABC-123 --party seller
    uv run python -m scripts.simulate_dispute adjudicate PP-R-ABC-123 --outcome seller

The dispute id is always explicit, and the script refuses any dispute that is not for a
capture in our own `orders` table.
"""

import argparse
import asyncio
import re
import sys

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from api.config import require_settings, settings
from api.db import make_engine, orders
from api.paypal_client import PayPalClient

DISPUTE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
EVIDENCE_ACTIONS = {"seller": "SELLER_EVIDENCE", "buyer": "BUYER_EVIDENCE"}
OUTCOMES = {"seller": "SELLER_FAVOR", "buyer": "BUYER_FAVOR"}
OPEN_STATUSES = {"OPEN", "WAITING_FOR_BUYER_RESPONSE", "WAITING_FOR_SELLER_RESPONSE", "UNDER_REVIEW"}


class ScriptError(Exception):
    pass


def describe_error(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    issues = ", ".join(str(d.get("issue", d)) for d in body.get("details", []) or [])
    return f"HTTP {response.status_code} {body.get('name', '')}: {body.get('message', '')} [{issues}]"


def linked_order(engine, dispute: dict):
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


def summary(dispute: dict, order) -> str:
    linked = f"order {order['paypal_order_id']} ({order['product_id']})" if order else "NOT ours"
    return (
        f"{dispute.get('dispute_id')}  status={dispute.get('status')}  "
        f"stage={dispute.get('dispute_life_cycle_stage')}  reason={dispute.get('reason')}  {linked}"
    )


async def load_our_dispute(client: PayPalClient, engine, dispute_id: str):
    dispute = await client.get_dispute(dispute_id)
    order = linked_order(engine, dispute)
    if order is None:
        raise ScriptError(f"{dispute_id} is not for any capture in our orders table; refusing")
    # fail closed: only act on a status we know is open, so an unexpected or missing value
    # is reported instead of being treated as "not resolved"
    if dispute.get("status") not in OPEN_STATUSES:
        raise ScriptError(
            f"{dispute_id} has status {dispute.get('status')!r}; "
            f"will only act on {sorted(OPEN_STATUSES)}"
        )
    return dispute, order


async def post_action(client: PayPalClient, dispute_id: str, action: str, body: dict) -> None:
    response = await client.request(
        "POST", f"/v1/customer/disputes/{dispute_id}/{action}", json=body
    )
    if not response.is_success:
        raise ScriptError(f"{action} failed: {describe_error(response)}")


async def run(args, client: PayPalClient, engine) -> int:
    try:
        if args.command == "list":
            items = (await client.list_disputes(page_size=20)).get("items", [])
            if not items:
                print("no disputes")
            # shortcut: first page only (20), enough for a sandbox account
            for item in items:
                dispute_id = item.get("dispute_id")
                if not dispute_id:
                    continue
                try:
                    dispute = await client.get_dispute(dispute_id)
                except httpx.HTTPStatusError as exc:
                    print(f"{dispute_id}  error: {describe_error(exc.response)}")
                    continue
                print(summary(dispute, linked_order(engine, dispute)))
            return 0

        dispute_id = args.dispute_id
        if args.command == "show":
            dispute = await client.get_dispute(dispute_id)
            print(summary(dispute, linked_order(engine, dispute)))
            actions = sorted(
                link["rel"] for link in dispute.get("links", []) if link.get("rel") not in (None, "self")
            )
            print("actions PayPal allows now:", ", ".join(actions) or "none")
            return 0

        before, _ = await load_our_dispute(client, engine, dispute_id)
        print("before:", before.get("status"), before.get("dispute_life_cycle_stage"))
        if args.command == "escalate":
            # an INQUIRY has no require-evidence/adjudicate link; escalating it to a claim
            # moves it to UNDER_REVIEW, where those sandbox actions become available
            await post_action(client, dispute_id, "escalate", {"note": args.note})
        elif args.command == "evidence":
            await post_action(
                client, dispute_id, "require-evidence", {"action": EVIDENCE_ACTIONS[args.party]}
            )
        else:
            await post_action(
                client, dispute_id, "adjudicate", {"adjudication_outcome": OUTCOMES[args.outcome]}
            )
        # trust the dispute resource, never the order webhooks arrive in
        after = await client.get_dispute(dispute_id)
        print("after: ", after.get("status"), after.get("dispute_life_cycle_stage"))
        return 0
    except ScriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"error: PayPal {describe_error(exc.response)}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"error: could not reach PayPal: {exc!r}", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"error: database problem (is `make db` running?): {type(exc).__name__}", file=sys.stderr)
        return 1
    except ValueError:
        print("error: PayPal returned a response that is not JSON", file=sys.stderr)
        return 1


def dispute_id_arg(value: str) -> str:
    if not DISPUTE_ID.match(value):
        raise argparse.ArgumentTypeError("dispute id must be letters, digits and dashes")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Sandbox dispute simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="our disputes and the orders they belong to")
    show = sub.add_parser("show", help="one dispute")
    show.add_argument("dispute_id", type=dispute_id_arg)
    escalate = sub.add_parser("escalate", help="escalate an inquiry to a claim (UNDER_REVIEW)")
    escalate.add_argument("dispute_id", type=dispute_id_arg)
    escalate.add_argument("--note", default="Escalating the sandbox test dispute for review.")
    evidence = sub.add_parser("evidence", help="require-evidence (sandbox only)")
    evidence.add_argument("dispute_id", type=dispute_id_arg)
    evidence.add_argument("--party", choices=EVIDENCE_ACTIONS, required=True)
    adjudicate = sub.add_parser("adjudicate", help="settle the dispute (sandbox only)")
    adjudicate.add_argument("dispute_id", type=dispute_id_arg)
    adjudicate.add_argument("--outcome", choices=OUTCOMES, required=True)
    return parser.parse_args(argv)


async def amain(argv=None) -> int:
    args = parse_args(argv)
    try:
        require_settings()  # also refuses a non-sandbox PAYPAL_API_BASE
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    engine = make_engine(settings.database_url)
    client = PayPalClient()
    try:
        return await run(args, client, engine)
    finally:
        await client.aclose()
        engine.dispose()


def main(argv=None) -> None:
    raise SystemExit(asyncio.run(amain(argv)))


if __name__ == "__main__":
    main()
