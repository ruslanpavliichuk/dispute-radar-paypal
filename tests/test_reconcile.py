import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from agents import reconcile, runner
from agents.reconcile import handle_capture_event, reconcile_pending
from api.config import settings
from api.db import init_db, make_engine, orders, webhook_events
from api.main import app
from api.paypal_client import PayPalClient

BASE = "https://api-m.sandbox.paypal.com"
OLD = datetime.now(timezone.utc) - timedelta(minutes=10)
TOKEN = "t0ken"


@pytest.fixture
def engine():
    e = make_engine("sqlite://")
    init_db(e)
    return e


@pytest.fixture
def paypal():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").mock(return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        mock.order = mock.get(url__regex=r"/v2/checkout/orders/.+")
        yield mock


@pytest.fixture
async def client():
    c = PayPalClient()
    yield c
    await c.aclose()


def add_order(engine, oid="ORD1", status="PENDING", created=OLD, cap="CAP1", amount=8000):
    with engine.begin() as conn:
        conn.execute(insert(orders).values(
            paypal_order_id=oid, product_id="p", amount_minor=amount, currency="GBP", status=status,
            capture_id=cap, created_at=created))


def status(engine, oid="ORD1"):
    with engine.connect() as conn:
        return conn.execute(select(orders.c.status).where(orders.c.paypal_order_id == oid)).scalar_one()


def body(cap_status="COMPLETED", value="80.00", currency="GBP", captures=True):
    cap = {"id": "CAP1", "status": cap_status, "amount": {"currency_code": currency, "value": value}}
    return {"purchase_units": [{"payments": {"captures": [cap]} if captures else {}}]}


def add_event(engine, etype="PAYMENT.CAPTURE.COMPLETED", cap="CAP1", eid="E1", attempts=0):
    with engine.begin() as conn:
        conn.execute(insert(webhook_events).values(
            event_id=eid, event_type=etype, resource_id=cap, received_at=datetime.now(timezone.utc) - timedelta(hours=1),
            processed_at=None, attempts=attempts))


def event(engine, eid="E1"):
    with engine.connect() as conn:
        return dict(conn.execute(select(webhook_events).where(webhook_events.c.event_id == eid)).mappings().one())


# ---- reconcile_pending ----
async def test_pending_becomes_completed(engine, paypal, client):
    add_order(engine)
    paypal.order.mock(return_value=httpx.Response(200, json=body()))
    await reconcile_pending(engine, client)
    assert status(engine) == "COMPLETED"


async def test_amount_mismatch_becomes_mismatch(engine, paypal, client):
    add_order(engine)
    paypal.order.mock(return_value=httpx.Response(200, json=body(value="1.00")))
    await reconcile_pending(engine, client)
    assert status(engine) == "MISMATCH"


async def test_unknown_status_becomes_unknown(engine, paypal, client):
    add_order(engine)
    paypal.order.mock(return_value=httpx.Response(200, json=body(cap_status="WEIRD")))
    await reconcile_pending(engine, client)
    assert status(engine) == "UNKNOWN"


@pytest.mark.parametrize("payload", [body(captures=False), body(cap_status="PENDING")])
async def test_not_yet_captured_stays_pending(engine, paypal, client, payload):
    add_order(engine)
    paypal.order.mock(return_value=httpx.Response(200, json=payload))
    await reconcile_pending(engine, client)
    assert status(engine) == "PENDING"


@pytest.mark.parametrize("st", ["MISMATCH", "UNKNOWN", "COMPLETED"])
async def test_settled_orders_are_never_overwritten(engine, paypal, client, st):
    add_order(engine, status=st)
    paypal.order.mock(return_value=httpx.Response(200, json=body()))
    await reconcile_pending(engine, client)
    assert status(engine) == st and not paypal.order.called
    # and a race: the row changed after it was selected
    assert reconcile._move(engine, "ORD1", ("PENDING",), "COMPLETED") is False


async def test_pending_older_than_three_days_is_left_to_the_attention_list(engine, paypal, client):
    add_order(engine, created=datetime.now(timezone.utc) - timedelta(days=4))
    await reconcile_pending(engine, client)
    assert status(engine) == "PENDING" and not paypal.order.called


async def test_young_pending_is_not_touched(engine, paypal, client):
    add_order(engine, created=datetime.now(timezone.utc) - timedelta(minutes=1))
    await reconcile_pending(engine, client)
    assert status(engine) == "PENDING" and not paypal.order.called


async def test_one_failing_order_does_not_stop_the_rest(engine, paypal, client):
    add_order(engine, "ORD1", created=OLD - timedelta(hours=1), cap="CAPX")
    add_order(engine, "ORD2")
    paypal.order.side_effect = [httpx.Response(500), httpx.Response(200, json=body())]
    await reconcile_pending(engine, client)
    assert (status(engine, "ORD1"), status(engine, "ORD2")) == ("PENDING", "COMPLETED")


async def test_bad_order_id_never_reaches_http(paypal, client):
    for bad in ("../x", "a/b", "", "x" * 65, "ORD 1"):
        with pytest.raises(ValueError):
            await client.get_order(bad)
    assert not paypal.order.called


# ---- capture events ----
async def test_unknown_capture_is_stamped_and_makes_no_call(engine, paypal, client):
    add_event(engine, cap="NOPE")
    await handle_capture_event(engine, client, event(engine))
    e = event(engine)
    assert e["processed_at"] is not None and e["last_error"] == "unknown capture" and not paypal.order.called


async def test_recent_unknown_capture_retries_instead_of_stamping(engine, paypal, client):
    add_event(engine, cap="NOPE")
    with engine.begin() as conn:
        conn.execute(webhook_events.update().values(received_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await handle_capture_event(engine, client, event(engine))
    e = event(engine)
    assert e["processed_at"] is None and e["attempts"] == 1 and e["last_error"] == "NotYet"


async def test_invalid_capture_id_is_stamped(engine, paypal, client):
    add_event(engine, cap="../x")
    await handle_capture_event(engine, client, event(engine))
    assert event(engine)["last_error"] == "invalid capture id" and not paypal.order.called


async def test_completed_event_reconciles_a_pending_order_even_if_young(engine, paypal, client):
    add_order(engine, created=datetime.now(timezone.utc))
    add_event(engine)
    paypal.order.mock(return_value=httpx.Response(200, json=body()))
    await handle_capture_event(engine, client, event(engine))
    assert status(engine) == "COMPLETED" and event(engine)["processed_at"] is not None


def refunded_body(cap_status="REFUNDED"):
    return body(cap_status=cap_status)


async def test_refunded_moves_completed_by_paypals_capture_status(engine, paypal, client):
    add_order(engine, status="COMPLETED")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED")
    paypal.order.mock(return_value=httpx.Response(200, json=refunded_body()))
    await handle_capture_event(engine, client, event(engine))
    assert status(engine) == "REFUNDED" and event(engine)["processed_at"] is not None


async def test_partial_refund_then_full_refund_upgrades(engine, paypal, client):
    add_order(engine, status="COMPLETED")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED", eid="E1")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED", eid="E2")
    paypal.order.mock(return_value=httpx.Response(200, json=refunded_body("PARTIALLY_REFUNDED")))
    await handle_capture_event(engine, client, event(engine, "E1"))
    assert status(engine) == "PARTIALLY_REFUNDED"
    paypal.order.mock(return_value=httpx.Response(200, json=refunded_body("REFUNDED")))
    await handle_capture_event(engine, client, event(engine, "E2"))
    assert status(engine) == "REFUNDED"


async def test_event_type_alone_is_not_trusted(engine, paypal, client):
    add_order(engine, status="COMPLETED")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED", eid="E1")
    add_event(engine, "PAYMENT.CAPTURE.REVERSED", eid="E2")
    paypal.order.mock(return_value=httpx.Response(200, json=body()))  # PayPal still shows COMPLETED
    for eid in ("E1", "E2"):
        await handle_capture_event(engine, client, event(engine, eid))
    assert status(engine) == "COMPLETED" and event(engine, "E2")["processed_at"] is not None


async def test_reversed_applies_only_when_paypal_shows_reversed(engine, paypal, client):
    add_order(engine, status="COMPLETED")
    add_event(engine, "PAYMENT.CAPTURE.REVERSED")
    paypal.order.mock(return_value=httpx.Response(200, json=body(cap_status="REVERSED")))
    await handle_capture_event(engine, client, event(engine))
    assert status(engine) == "REVERSED"


async def test_refund_on_a_pending_order_retries_instead_of_stamping(engine, paypal, client):
    add_order(engine)
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED")
    await handle_capture_event(engine, client, event(engine))
    e = event(engine)
    assert status(engine) == "PENDING" and e["processed_at"] is None and e["attempts"] == 1
    assert e["last_error"] == "NotYet" and not paypal.order.called


async def test_refund_on_a_settled_other_state_is_stamped_with_a_note(engine, paypal, client):
    add_order(engine, status="MISMATCH")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED")
    await handle_capture_event(engine, client, event(engine))
    e = event(engine)
    assert status(engine) == "MISMATCH" and e["processed_at"] is not None and e["last_error"] == "order is mismatch"
    assert not paypal.order.called


async def test_capture_picked_by_id_not_position(engine, paypal, client):
    add_order(engine, status="COMPLETED")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED")
    other = {"id": "OTHER", "status": "COMPLETED", "amount": {"currency_code": "GBP", "value": "80.00"}}
    mine = body(cap_status="REFUNDED")["purchase_units"][0]["payments"]["captures"][0]
    payload = {"purchase_units": [{"payments": {"captures": [other, mine]}}]}
    paypal.order.mock(return_value=httpx.Response(200, json=payload))
    await handle_capture_event(engine, client, event(engine))
    assert status(engine) == "REFUNDED"


async def test_pending_order_with_no_matching_capture_stays_pending(engine, paypal, client):
    add_order(engine, cap="MISSING")
    paypal.order.mock(return_value=httpx.Response(200, json=body()))  # its capture id is CAP1
    await reconcile_pending(engine, client)
    assert status(engine) == "PENDING"


async def test_refund_event_found_through_the_up_link(engine, paypal, client):
    add_order(engine, status="COMPLETED")
    refund = {"id": "EV-R", "event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": {
        "id": "REFUND-ID-1", "links": [
            {"rel": "self", "href": f"{BASE}/v2/payments/refunds/REFUND-ID-1"},
            {"rel": "up", "href": f"{BASE}/v2/payments/captures/CAP1"}]}}
    reconcile_store(engine, refund)
    assert event(engine, "EV-R")["resource_id"] == "CAP1"
    paypal.order.mock(return_value=httpx.Response(200, json=refunded_body()))
    await handle_capture_event(engine, client, event(engine, "EV-R"))
    assert status(engine) == "REFUNDED"


def reconcile_store(engine, ev):
    from api.webhooks import _store_event
    assert _store_event(engine, ev)


@pytest.mark.parametrize("links", [None, [], [{"rel": "up", "href": "https://x/captures/../a b"}]])
def test_refund_event_without_a_usable_up_link_is_not_actionable(engine, links):
    reconcile_store(engine, {"id": "EV-X", "event_type": "PAYMENT.CAPTURE.REFUNDED",
                             "resource": {"id": "REFUND-ID-1", "links": links}})
    assert event(engine, "EV-X")["processed_at"] is not None  # the refund id is never taken for a capture id


async def test_denied_moves_pending_to_declined_only_if_paypal_says_so(engine, paypal, client):
    add_order(engine, "ORD1")
    add_order(engine, "ORD2", status="COMPLETED", cap="CAP2")
    add_event(engine, "PAYMENT.CAPTURE.DENIED", "CAP1", "E1")
    add_event(engine, "PAYMENT.CAPTURE.DENIED", "CAP2", "E2")
    paypal.order.mock(return_value=httpx.Response(200, json=body(cap_status="PENDING")))
    await handle_capture_event(engine, client, event(engine, "E1"))
    assert status(engine, "ORD1") == "PENDING"  # PayPal does not show a decline
    paypal.order.mock(return_value=httpx.Response(200, json=body(cap_status="DECLINED")))
    for eid in ("E1", "E2"):
        await handle_capture_event(engine, client, event(engine, eid))
    assert (status(engine, "ORD1"), status(engine, "ORD2")) == ("DECLINED", "COMPLETED")


async def test_duplicate_event_processing_is_harmless(engine, paypal, client):
    add_order(engine, status="COMPLETED")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED")
    paypal.order.mock(return_value=httpx.Response(200, json=refunded_body()))
    for _ in range(2):
        await handle_capture_event(engine, client, event(engine))
    assert status(engine) == "REFUNDED"


async def test_paypal_500_counts_attempts_then_gives_up(engine, paypal, client):
    add_order(engine)
    add_event(engine)
    paypal.order.mock(return_value=httpx.Response(500, text="SECRET-BODY"))
    for n in (1, 2):
        await handle_capture_event(engine, client, event(engine))
        e = event(engine)
        assert e["attempts"] == n and e["processed_at"] is None and e["last_error"] == "HTTPStatusError"
    await handle_capture_event(engine, client, event(engine))
    e = event(engine)
    assert e["attempts"] == 3 and e["processed_at"] is not None and "SECRET" not in str(e)
    assert status(engine) == "PENDING"


# ---- run_once ----
async def test_run_once_handles_events_and_backstop(engine, paypal, client, monkeypatch, caplog):
    monkeypatch.setattr(runner, "BACKOFF_SECONDS", 0)
    add_order(engine, "ORD1", created=OLD)
    add_order(engine, "ORD2", status="MISMATCH", cap="CAP2")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED", "CAP2")
    paypal.order.mock(return_value=httpx.Response(200, json=body()))
    with caplog.at_level(logging.CRITICAL):
        await runner.run_once(engine, {}, client)
    assert status(engine, "ORD1") == "COMPLETED" and status(engine, "ORD2") == "MISMATCH"
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL] == [
        "1 orders need attention (GET /api/orders/attention)"]
    # throttled: an immediate second pass does not look again
    paypal.order.reset()
    add_order(engine, "ORD3", cap="CAP3")
    await runner.run_once(engine, {}, client)
    assert not paypal.order.called


async def test_run_once_survives_a_failing_reconcile(engine, paypal, client, monkeypatch):
    monkeypatch.setattr(runner, "BACKOFF_SECONDS", 0)

    async def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(runner, "reconcile_pending", boom)
    add_order(engine, status="COMPLETED")
    add_event(engine, "PAYMENT.CAPTURE.REFUNDED")
    paypal.order.mock(return_value=httpx.Response(200, json=refunded_body()))
    await runner.run_once(engine, {}, client)
    assert status(engine) == "REFUNDED"


async def test_run_once_survives_failing_capture_pass(engine, paypal, client, monkeypatch):
    add_order(engine)
    paypal.order.mock(return_value=httpx.Response(200, json=body()))

    async def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(runner, "handle_capture_event", boom)
    add_event(engine)
    await runner.run_once(engine, {}, client)
    assert status(engine) == "COMPLETED"  # backstop still ran


# ---- attention endpoint ----
@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_id", "client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "client-secret")
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    with TestClient(app) as c:
        yield c


def test_attention_auth(web, monkeypatch):
    assert web.get("/api/orders/attention").status_code == 401
    assert web.get("/api/orders/attention", headers={"X-Admin-Token": "bad"}).status_code == 401
    monkeypatch.setattr(settings, "admin_token", "")
    assert web.get("/api/orders/attention", headers={"X-Admin-Token": TOKEN}).status_code == 503


def test_attention_lists_only_the_three_kinds(web):
    young = datetime.now(timezone.utc) - timedelta(minutes=1)
    for oid, st, created in [("A", "MISMATCH", OLD), ("B", "UNKNOWN", OLD + timedelta(minutes=1)), ("C", "PENDING", OLD + timedelta(minutes=2)),
                             ("D", "PENDING", young), ("E", "COMPLETED", OLD), ("F", "CREATED", OLD)]:
        with web.app.state.engine.begin() as conn:
            conn.execute(insert(orders).values(paypal_order_id=oid, product_id="p", amount_minor=100, currency="GBP",
                                               status=st, capture_id=f"CAP-{oid}", payer_id="PAYER", created_at=created))
    r = web.get("/api/orders/attention", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200
    assert [o["paypal_order_id"] for o in r.json()] == ["C", "B", "A"]  # newest first
    assert set(r.json()[0]) == {"paypal_order_id", "status", "amount_minor", "currency", "created_at", "capture_id"}
