import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from api.config import settings
from api.db import orders, proposals, tracking_events
from api.main import app

TOKEN = "t0ken"
AUTH = {"X-Admin-Token": TOKEN}
MARKER = "SECRET-PAYPAL-BODY-MARKER"
T0 = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)
OID, CAP = "ORDER-P1", "CAPTURE000000001"
ORDER_URL = f"/v2/checkout/orders/{OID}"
TRACK_URL = f"{ORDER_URL}/track"
REFUND_URL = f"/v2/payments/captures/{CAP}/refund"


def pp_order(status="COMPLETED", cap=CAP):
    return {"id": OID, "purchase_units": [{"payments": {"captures": [{"id": cap, "status": status}]}}]}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_id", "client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "client-secret")
    monkeypatch.setattr(settings, "admin_token", TOKEN)


@pytest.fixture
def paypal():
    with respx.mock(base_url=settings.paypal_api_base, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600}))
        mock.get_order = mock.get(ORDER_URL).mock(return_value=httpx.Response(200, json=pp_order()))
        mock.track = mock.post(TRACK_URL).mock(return_value=httpx.Response(200, json={}))
        mock.refund = mock.post(REFUND_URL).mock(return_value=httpx.Response(201, json={"status": "COMPLETED"}))
        yield mock


@pytest.fixture
def client():
    with TestClient(app) as c:
        with c.app.state.engine.begin() as conn:
            conn.execute(insert(orders).values(
                paypal_order_id=OID, product_id="p", amount_minor=6000, currency="GBP",
                status="COMPLETED", capture_id=CAP, created_at=T0))
            conn.execute(insert(tracking_events).values(
                paypal_order_id=OID, kind="accepted", occurred_at=T0, tracking_number="TRK123456",
                carrier="OTHER", created_at=T0))
            conn.execute(insert(tracking_events).values(
                paypal_order_id=OID, kind="in_transit", occurred_at=T0 + timedelta(days=1),
                tracking_number="LATER99999", carrier="OTHER", created_at=T0))
        yield c


def add(client, action="upload_tracking", amount=None, status="PROPOSED", subject=None, draft="the draft"):
    with client.app.state.engine.begin() as conn:
        return conn.execute(insert(proposals).values(
            agent="prevention", subject_id=subject or f"{OID}:{action}", status=status, action=action,
            amount_minor=amount, currency="GBP", draft=draft, created_at=T0)).inserted_primary_key[0]


def refund(client, **kw):
    return add(client, "goodwill_refund", amount=600, **kw)  # 10% of 6000


def row(client, pid):
    with client.app.state.engine.connect() as conn:
        return conn.execute(select(proposals).where(proposals.c.id == pid)).mappings().one()


def approve(client, pid, **kw):
    return client.post(f"/api/proposals/{pid}/approve", headers=AUTH, **kw)


def sql(client, stmt):
    with client.app.state.engine.begin() as conn:
        conn.execute(stmt)


def sent(paypal):
    return paypal.track.call_count + paypal.refund.call_count


def test_upload_happy_path(client, paypal):
    pid = add(client)
    r = approve(client, pid)
    assert r.status_code == 200 and r.json()["status"] == "SENT" and r.json()["sent_at"]
    assert sent(paypal) == 1
    call = paypal.track.calls[0].request
    assert json.loads(call.content) == {
        "capture_id": CAP, "tracking_number": "TRK123456", "carrier": "OTHER",
        "carrier_name_other": "Demo courier", "notify_payer": True}
    assert call.headers["PayPal-Request-Id"] == f"proposal-{pid}"
    assert row(client, pid)["status"] == "SENT" and row(client, pid)["sent_at"]


def test_refund_happy_path_and_pending(client, paypal):
    pid = refund(client)
    r = approve(client, pid)
    assert r.status_code == 200 and r.json()["status"] == "SENT"
    call = paypal.refund.calls[0].request
    assert json.loads(call.content) == {
        "amount": {"currency_code": "GBP", "value": "6.00"}, "note_to_payer": "the draft"}
    assert call.headers["PayPal-Request-Id"] == f"proposal-{pid}" and sent(paypal) == 1
    paypal.refund.mock(return_value=httpx.Response(201, json={"status": "PENDING"}))
    sql(client, update(proposals).values(status="PROPOSED"))
    assert approve(client, pid, json={"note": "edited"}).status_code == 200
    assert json.loads(paypal.refund.calls[1].request.content)["note_to_payer"] == "edited"
    assert row(client, pid)["status"] == "SENT"


def test_refund_status_failed(client, paypal):
    paypal.refund.mock(return_value=httpx.Response(201, json={"status": "FAILED", "x": MARKER}))
    pid = refund(client)
    r = approve(client, pid)
    assert r.status_code == 502 and MARKER not in r.text
    stored = row(client, pid)
    assert stored["status"] == "FAILED" and stored["error"] == "refund FAILED"


def test_refund_reply_without_json_is_sent(client, paypal):
    paypal.refund.mock(return_value=httpx.Response(201, text="not json"))
    pid = refund(client)
    assert approve(client, pid).status_code == 200 and row(client, pid)["status"] == "SENT"


@pytest.mark.parametrize("status", ["PENDING", "REFUNDED", "PARTIALLY_REFUNDED"])
@pytest.mark.parametrize("make", [add, refund])
def test_capture_not_completed_refused(client, paypal, status, make):
    paypal.get_order.mock(return_value=httpx.Response(200, json=pp_order(status)))
    pid = make(client)
    r = approve(client, pid)
    assert r.status_code == 409 and f"shows the payment as {status}" in r.json()["detail"]
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


def test_capture_missing_at_paypal(client, paypal):
    paypal.get_order.mock(return_value=httpx.Response(200, json=pp_order(cap="OTHERCAP")))
    pid = add(client)
    assert approve(client, pid).status_code == 409
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


@pytest.mark.parametrize("make", [add, refund])
def test_local_order_not_completed_or_delivered(client, paypal, make):
    pid = make(client)
    sql(client, update(orders).values(status="REFUNDED"))
    assert approve(client, pid).status_code == 409
    sql(client, update(orders).values(status="COMPLETED"))
    sql(client, insert(tracking_events).values(
        paypal_order_id=OID, kind="delivered", occurred_at=T0 + timedelta(days=2), created_at=T0))
    assert approve(client, pid).status_code == 409
    assert paypal.get_order.call_count == 0 and sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


def test_order_without_capture_or_unknown(client, paypal):
    pid = add(client, subject="NOSUCH:upload_tracking")
    assert approve(client, pid).status_code == 409
    sql(client, update(orders).values(capture_id=None))
    assert approve(client, add(client)).status_code == 409
    assert paypal.calls.call_count == 0


@pytest.mark.parametrize("stored, order_amount", [(601, 6000), (0, 5)])  # stored differs; recomputed is 0
def test_refund_amount_changed_or_not_positive(client, paypal, stored, order_amount):
    sql(client, update(orders).values(amount_minor=order_amount))
    pid = add(client, "goodwill_refund", amount=stored)
    assert approve(client, pid).status_code == 409
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


def test_refund_currency_allowlist(client, paypal):
    sql(client, update(orders).values(currency="JPY"))
    pid = refund(client)
    assert approve(client, pid).status_code == 409
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


def test_unknown_capture_status_is_not_echoed(client, paypal):
    paypal.get_order.mock(return_value=httpx.Response(200, json=pp_order(MARKER)))
    r = approve(client, add(client))
    assert r.status_code == 409 and MARKER not in r.text and "unexpected state" in r.json()["detail"]


def test_refund_status_cancelled(client, paypal):
    paypal.refund.mock(return_value=httpx.Response(201, json={"status": "CANCELLED"}))
    pid = refund(client)
    assert approve(client, pid).status_code == 502
    assert row(client, pid)["status"] == "FAILED" and row(client, pid)["error"] == "refund CANCELLED"


def add_response(client, status, capture=CAP, action="make_offer", subject="PP-D-1"):
    ev = [{"id": "E3", "kind": "capture", "fact": f"PayPal capture id: {capture}."}]
    with client.app.state.engine.begin() as conn:
        conn.execute(insert(proposals).values(
            agent="response", subject_id=subject, status=status, action=action, evidence=ev, created_at=T0))


@pytest.mark.parametrize("status, blocked", [("PROPOSED", True), ("SENDING", True), ("SENT", True),
                                             ("REJECTED", False), ("FAILED", False)])
def test_refund_blocked_by_response_proposal_for_same_capture(client, paypal, status, blocked):
    add_response(client, status)
    pid = refund(client)
    assert approve(client, pid).status_code == (409 if blocked else 200)
    assert sent(paypal) == (0 if blocked else 1)


def test_refund_not_blocked_by_response_proposal_for_other_capture(client, paypal):
    add_response(client, "SENT", capture="OTHERCAPTURE")
    assert approve(client, refund(client)).status_code == 200


@pytest.mark.parametrize("status, blocked", [("SENDING", True), ("SENT", True), ("PROPOSED", False),
                                             ("REJECTED", False), ("FAILED", False)])
@pytest.mark.parametrize("action", ["make_offer", "accept_claim", "send_message"])
def test_response_money_blocked_by_goodwill_refund(client, action, status, blocked):
    from tests import test_proposals as tp  # same dispute fixtures as the response-flow tests
    sql(client, update(orders).values(paypal_order_id="ORDER3", capture_id="CAPTURE000000003"))
    sql(client, update(tracking_events).values(paypal_order_id="ORDER3"))
    with respx.mock(base_url=settings.paypal_api_base, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}))
        mock.get(tp.URL).mock(return_value=httpx.Response(200, json=tp.DISPUTE))
        post = mock.post(f"{tp.URL}/{action.replace('_', '-')}").mock(return_value=httpx.Response(200, json={}))
        add(client, "goodwill_refund", amount=600, status=status, subject="ORDER3:goodwill_refund")
        with client.app.state.engine.begin() as conn:
            pid = conn.execute(insert(proposals).values(
                agent="response", subject_id=tp.DID, status="PROPOSED", action=action, amount_minor=2400,
                currency="GBP", draft="d", created_at=T0)).inserted_primary_key[0]
        r = approve(client, pid)
    money = action != "send_message"
    assert (r.status_code == 409) == (blocked and money)
    assert post.call_count == (0 if blocked and money else 1)


@pytest.mark.parametrize("tn, carrier", [("trk123456", "OTHER"), ("TRK/12345", "OTHER"), ("SHORT", "OTHER"),
                                         ("TRK123456", "DHL"), (None, "OTHER")])
def test_upload_refused_for_bad_tracking(client, paypal, tn, carrier):
    sql(client, update(tracking_events).where(tracking_events.c.kind == "accepted")
        .values(tracking_number=tn, carrier=carrier))
    pid = add(client)
    assert approve(client, pid).status_code == 409
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


def test_upload_refused_without_shipping_event(client, paypal):
    sql(client, tracking_events.delete())
    pid = add(client)
    assert approve(client, pid).status_code == 409
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


@pytest.mark.parametrize("subject, action", [("bad/id:upload_tracking", "upload_tracking"),
                                             (f"{OID}:upload_tracking", "escalate"),
                                             (f"{OID}:goodwill_refund", "upload_tracking")])
def test_bad_subject_or_action(client, paypal, subject, action):
    pid = add(client, action, subject=subject)
    assert approve(client, pid).status_code == 409
    assert paypal.calls.call_count == 0 and row(client, pid)["status"] == "PROPOSED"


def test_unknown_agent_refused_before_claim(client, paypal):
    sql(client, insert(proposals).values(agent="x", subject_id="s", status="PROPOSED", action="a", created_at=T0))
    assert approve(client, 1).status_code == 409 and paypal.calls.call_count == 0


@pytest.mark.parametrize("make", [add, refund])
def test_get_order_failure(client, paypal, make):
    paypal.get_order.mock(return_value=httpx.Response(500, text=MARKER))
    pid = make(client)
    r = approve(client, pid)
    assert r.status_code == 502 and "could not read the order" in r.json()["detail"] and MARKER not in r.text
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"


@pytest.mark.parametrize("make, route", [(add, "track"), (refund, "refund")])
@pytest.mark.parametrize("code", [400, 500])
def test_post_fails_records_status_only(client, paypal, make, route, code):
    getattr(paypal, route).mock(return_value=httpx.Response(code, json={"message": MARKER}))
    pid = make(client, draft="private draft text")
    r = approve(client, pid)
    assert r.status_code == 502 and MARKER not in r.text
    stored = dict(row(client, pid))
    assert stored["status"] == "FAILED" and stored["error"] == f"PayPal {code}"
    assert MARKER not in json.dumps(stored, default=str) and "TRK123456" not in json.dumps(stored, default=str)


def test_post_transport_error(client, paypal):
    paypal.track.mock(side_effect=httpx.ConnectError(MARKER))
    pid = add(client)
    assert approve(client, pid).status_code == 502
    assert row(client, pid)["status"] == "FAILED" and row(client, pid)["error"] == "ConnectError"


def test_twice_and_already_sending(client, paypal):
    pid = refund(client)
    assert approve(client, pid).status_code == 200
    assert approve(client, pid).status_code == 409 and sent(paypal) == 1
    busy = add(client, status="SENDING", subject=f"{OID}:x")
    assert approve(client, busy).status_code == 409 and sent(paypal) == 1


def test_reject_makes_no_call(client, paypal):
    pid = add(client)
    r = client.post(f"/api/proposals/{pid}/reject", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert paypal.calls.call_count == 0


def test_note_rules(client, paypal):
    up, rf = add(client), refund(client)
    r = approve(client, up, json={"note": "x"})
    assert r.status_code == 409 and "note is not used" in r.json()["detail"]
    assert approve(client, rf, json={"note": "x" * 256}).status_code == 422
    assert sent(paypal) == 0 and row(client, up)["status"] == "PROPOSED" and row(client, rf)["status"] == "PROPOSED"
    assert approve(client, rf, json={"note": "x" * 255}).status_code == 200


def test_unexpected_error_reverts(client, paypal, monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr("api.proposals._local_facts", boom)
    pid = add(client)
    assert approve(client, pid).status_code == 500
    assert sent(paypal) == 0 and row(client, pid)["status"] == "PROPOSED"
