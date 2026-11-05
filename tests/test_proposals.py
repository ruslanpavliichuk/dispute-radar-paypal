import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from api.config import settings
from api.db import orders, proposals
from api.main import app

FIXTURES = Path(__file__).parent / "fixtures"
DISPUTE = json.loads((FIXTURES / "dispute_not_received_actionable.json").read_text())
DID = DISPUTE["dispute_id"]
URL = f"/v1/customer/disputes/{DID}"
TOKEN = "t0ken"
AUTH = {"X-Admin-Token": TOKEN}
MARKER = "SECRET-PAYPAL-BODY-MARKER"
T0 = datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_id", "client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "client-secret")
    monkeypatch.setattr(settings, "admin_token", TOKEN)


@pytest.fixture
def paypal():
    with respx.mock(base_url=settings.paypal_api_base, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        )
        mock.get_dispute = mock.get(URL).mock(return_value=httpx.Response(200, json=DISPUTE))
        mock.actions = {}
        for a in ("accept-claim", "make-offer", "send-message", "provide-evidence"):
            mock.actions[a] = mock.post(f"{URL}/{a}").mock(return_value=httpx.Response(200, json={}))
        yield mock


def posts(paypal):
    return [c for r in paypal.actions.values() for c in r.calls]


@pytest.fixture
def client():
    with TestClient(app) as c:
        with c.app.state.engine.begin() as conn:
            conn.execute(insert(orders).values(
                paypal_order_id="ORDER3", product_id="p", amount_minor=8000, currency="GBP",
                status="COMPLETED", capture_id="CAPTURE000000003", created_at=T0))
        yield c


def add(client, action="send_message", subject=DID, status="PROPOSED", amount=2400, draft="the draft"):
    with client.app.state.engine.begin() as conn:
        return conn.execute(insert(proposals).values(
            agent="response", subject_id=subject, status=status, action=action, amount_minor=amount,
            currency="GBP", draft=draft, created_at=T0)).inserted_primary_key[0]


def row(client, pid):
    with client.app.state.engine.connect() as conn:
        return conn.execute(select(proposals).where(proposals.c.id == pid)).mappings().one()


def approve(client, pid, **kw):
    return client.post(f"/api/proposals/{pid}/approve", headers=AUTH, **kw)


def test_auth(client, monkeypatch):
    pid = add(client)
    routes = [("get", "/api/proposals"), ("get", f"/api/proposals/{pid}"),
              ("post", f"/api/proposals/{pid}/reject"), ("post", f"/api/proposals/{pid}/approve")]
    for method, path in routes:
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, headers={"X-Admin-Token": "wrong"}).status_code == 401
    monkeypatch.setattr(settings, "admin_token", "")
    for method, path in routes:
        assert getattr(client, method)(path, headers=AUTH).status_code == 503
    assert row(client, pid)["status"] == "PROPOSED"


def test_list_and_get(client):
    a, b = add(client), add(client, subject="PP-R-OTHER")
    ids = [p["id"] for p in client.get("/api/proposals", headers=AUTH).json()]
    assert ids == [b, a]
    assert client.get(f"/api/proposals/{a}", headers=AUTH).json()["draft"] == "the draft"
    assert client.get("/api/proposals/999", headers=AUTH).status_code == 404


@pytest.mark.parametrize("action,path,body", [
    ("accept_claim", "accept-claim", {"note": "the draft", "accept_claim_reason": "REASON_NOT_PROVIDED"}),
    ("make_offer", "make-offer", {"note": "the draft", "offer_type": "REFUND",
                                  "offer_amount": {"currency_code": "GBP", "value": "24.00"}}),
    ("send_message", "send-message", {"message": "the draft"}),
    ("provide_evidence", "provide-evidence", {"evidences": [{"evidence_type": "OTHER", "notes": "the draft"}]}),
])
def test_approve_each_action(client, paypal, action, path, body):
    pid = add(client, action)
    r = approve(client, pid)
    assert r.status_code == 200 and r.json()["status"] == "SENT" and r.json()["sent_at"]
    calls = posts(paypal)
    assert len(calls) == 1 and paypal.actions[path].call_count == 1
    assert json.loads(calls[0].request.content) == body
    assert calls[0].request.headers["PayPal-Request-Id"] == f"proposal-{pid}"
    assert row(client, pid)["status"] == "SENT" and row(client, pid)["sent_at"]


def test_approve_note_overrides_draft_and_is_validated(client, paypal):
    pid = add(client)
    assert approve(client, pid, json={"note": "x" * 2001}).status_code == 422
    assert approve(client, pid, json={"note": "ok", "extra": 1}).status_code == 422
    assert approve(client, pid, json={"note": 5}).status_code == 422
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"
    assert approve(client, pid, json={"note": "edited"}).status_code == 200
    assert json.loads(posts(paypal)[0].request.content) == {"message": "edited"}


def test_approve_twice_and_already_sending(client, paypal):
    pid = add(client)
    assert approve(client, pid).status_code == 200
    assert approve(client, pid).status_code == 409
    assert len(posts(paypal)) == 1
    busy = add(client, status="SENDING", subject="PP-R-BUSY")
    assert approve(client, busy).status_code == 409
    assert len(posts(paypal)) == 1 and paypal.get_dispute.call_count == 1
    assert approve(client, 999).status_code == 404


def test_link_not_offered(client, paypal):
    paypal.get_dispute.mock(return_value=httpx.Response(200, json={**DISPUTE, "links": DISPUTE["links"][:1]}))
    pid = add(client)
    r = approve(client, pid)
    assert r.status_code == 409 and "does not offer send_message" in r.json()["detail"]
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


def test_dispute_closed_or_not_ours(client, paypal):
    pid = add(client)
    paypal.get_dispute.mock(return_value=httpx.Response(200, json={**DISPUTE, "status": "RESOLVED"}))
    assert approve(client, pid).status_code == 409
    other = [{**t, "seller_transaction_id": "NOTOURS"} for t in DISPUTE["disputed_transactions"]]
    paypal.get_dispute.mock(return_value=httpx.Response(200, json={**DISPUTE, "disputed_transactions": other}))
    assert approve(client, pid).status_code == 409
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


def test_get_dispute_fails(client, paypal):
    paypal.get_dispute.mock(return_value=httpx.Response(500, text=MARKER))
    pid = add(client)
    r = approve(client, pid)
    assert r.status_code == 502 and MARKER not in r.text
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


@pytest.mark.parametrize("code", [400, 500])
def test_post_fails_records_status_only(client, paypal, code):
    paypal.actions["send-message"].mock(return_value=httpx.Response(code, json={"message": MARKER}))
    pid = add(client, draft="private draft text")
    r = approve(client, pid)
    assert r.status_code == 502 and MARKER not in r.text
    stored = dict(row(client, pid))
    assert stored["status"] == "FAILED" and stored["error"] == f"PayPal {code}"
    assert MARKER not in json.dumps(stored, default=str)
    assert "private draft text" not in stored["error"]


def test_post_transport_error_records_type_only(client, paypal):
    paypal.actions["send-message"].mock(side_effect=httpx.ConnectError(MARKER))
    pid = add(client)
    assert approve(client, pid).status_code == 502
    stored = row(client, pid)
    assert stored["status"] == "FAILED" and stored["error"] == "ConnectError"


def test_offer_above_allowed_refund(client, paypal):
    pid = add(client, "make_offer", amount=8001)
    assert approve(client, pid).status_code == 409
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


def test_offer_without_amount(client, paypal):
    pid = add(client, "make_offer", amount=None)
    assert approve(client, pid).status_code == 409
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


def test_reject(client, paypal):
    pid = add(client)
    r = client.post(f"/api/proposals/{pid}/reject", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "REJECTED" and r.json()["decided_at"]
    assert client.post(f"/api/proposals/{pid}/reject", headers=AUTH).status_code == 409
    sent = add(client, status="SENT", subject="PP-R-SENT")
    assert client.post(f"/api/proposals/{sent}/reject", headers=AUTH).status_code == 409
    assert client.post("/api/proposals/999/reject", headers=AUTH).status_code == 404
    assert approve(client, pid).status_code == 409
    assert len(paypal.calls) == 0


def test_invalid_subject_makes_no_paypal_call(client, paypal):
    pid = add(client, subject="PP-R/../x")
    assert approve(client, pid).status_code == 409
    assert len(paypal.calls) == 0 and row(client, pid)["status"] == "PROPOSED"


def test_unsupported_action_goes_back(client, paypal):
    pid = add(client, "escalate")
    assert approve(client, pid).status_code == 409
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


@pytest.mark.parametrize("amount, currency", [(0, "GBP"), (-500, "GBP"), (2400, "USD")])
def test_offer_amount_must_be_positive_and_in_the_dispute_currency(client, paypal, amount, currency):
    pid = add(client, "make_offer", amount=amount)
    with client.app.state.engine.begin() as conn:
        conn.execute(update(proposals).where(proposals.c.id == pid).values(currency=currency))
    assert approve(client, pid).status_code == 409
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


def test_offer_is_capped_by_the_disputed_amount_when_no_refund_limit_is_given(client, paypal):
    d = {k: v for k, v in DISPUTE.items() if k != "refund_details"}
    paypal.get_dispute.mock(return_value=httpx.Response(200, json=d))
    pid = add(client, "make_offer", amount=int(float(DISPUTE["dispute_amount"]["value"]) * 100) + 1)
    assert approve(client, pid).status_code == 409 and not posts(paypal)


def test_unexpected_error_before_sending_reverts_the_claim(client, paypal, monkeypatch):
    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr("api.proposals.linked_order", boom)
    pid = add(client)
    assert approve(client, pid).status_code == 500
    assert not posts(paypal) and row(client, pid)["status"] == "PROPOSED"


def test_issue_keeps_only_a_strict_paypal_error_code():
    from api.proposals import _issue
    ok = httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSUFFICIENT_FUNDS"}]})
    assert _issue(ok) == " INSUFFICIENT_FUNDS"
    assert _issue(httpx.Response(400, json={"name": "INVALID_REQUEST"})) == " INVALID_REQUEST"
    assert _issue(httpx.Response(400, json={"details": [{"issue": "buyer said hello@example.com"}]})) == ""
    assert _issue(httpx.Response(500, text="<html>")) == ""
