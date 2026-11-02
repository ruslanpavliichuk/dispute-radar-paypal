import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from api.config import settings
from api.db import webhook_events
from api.main import app
from api.webhooks import MAX_BODY_BYTES

URL = "/api/webhooks/paypal"
HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "paypal-transmission-id": "abc",
    "paypal-transmission-sig": "sig",
    "paypal-transmission-time": datetime.now(timezone.utc).isoformat(),
}
EVENT = {"id": "WH-EVT-1", "event_type": "CUSTOMER.DISPUTE.CREATED"}
SUCCESS = httpx.Response(200, json={"verification_status": "SUCCESS"})


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_id", "client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "client-secret")
    monkeypatch.setattr(settings, "paypal_webhook_id", "WH-123")


@pytest.fixture
def paypal():
    with respx.mock(base_url=settings.paypal_api_base, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        )
        mock.verify_route = mock.post("/v1/notifications/verify-webhook-signature")
        yield mock


def post(content=None, headers=HEADERS):
    with TestClient(app) as client:
        return client.post(URL, content=json.dumps(EVENT) if content is None else content, headers=headers)


def test_accepts_valid_signature_and_forwards_headers_and_body(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)

    response = post()

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    sent = json.loads(paypal.verify_route.calls.last.request.content)
    assert sent["transmission_id"] == "abc"
    assert sent["webhook_id"] == "WH-123"
    assert sent["webhook_event"] == EVENT


def test_rejects_invalid_signature(paypal):
    paypal.verify_route.mock(return_value=httpx.Response(200, json={"verification_status": "FAILURE"}))
    assert post().status_code == 400


@pytest.mark.parametrize(
    "content", [b"not json", b"\xff\xfe\x00", b"[1, 2]", b'"text"', b"[" * 100_000]
)
def test_rejects_bad_body_without_calling_paypal(paypal, content):
    assert post(content=content).status_code == 400
    assert not paypal.verify_route.called


def test_oversized_chunked_body_without_content_length_is_cut_off(paypal):
    chunks = iter([b"x" * (MAX_BODY_BYTES // 2 + 1)] * 3)  # no Content-Length, chunked
    assert post(content=chunks).status_code == 413
    assert not paypal.verify_route.called


def test_non_json_200_from_paypal_is_503(paypal):
    paypal.verify_route.mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    assert post().status_code == 503


def test_forwards_body_bytes_verbatim(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)
    raw = '{"id":"WH-1",  "amount": 1.10}'
    assert post(content=raw).status_code == 200
    assert f'"webhook_event": {raw}' in paypal.verify_route.calls.last.request.content.decode()


def test_rejects_missing_signature_headers_without_calling_paypal(paypal):
    assert post(headers={}).status_code == 400
    assert not paypal.verify_route.called


def test_rejects_oversized_body(paypal):
    assert post(content=b"x" * (MAX_BODY_BYTES + 1)).status_code == 413
    assert not paypal.verify_route.called


def test_unconfigured_webhook_id_is_503_not_invalid_signature(paypal, monkeypatch):
    monkeypatch.setattr(settings, "paypal_webhook_id", "")
    assert post().status_code == 503
    assert not paypal.verify_route.called


@pytest.mark.parametrize(
    "effect",
    [httpx.Response(500), httpx.ConnectTimeout("timed out")],
)
def test_paypal_outage_is_503_so_paypal_retries(paypal, effect):
    paypal.verify_route.mock(side_effect=[effect])
    assert post().status_code == 503


DISPUTE_EVENT = {
    "id": "WH-EVT-9",
    "event_type": "CUSTOMER.DISPUTE.CREATED",
    "resource": {"dispute_id": "PP-R-1", "buyer_email": "buyer@example.com"},
}


def stored_events():
    with app.state.engine.connect() as conn:
        return conn.execute(select(webhook_events)).mappings().all()


def test_verified_event_is_persisted_without_its_body(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)

    with TestClient(app) as client:
        response = client.post(URL, content=json.dumps(DISPUTE_EVENT), headers=HEADERS)
        rows = stored_events()

    assert response.status_code == 200
    assert len(rows) == 1
    assert (rows[0]["event_id"], rows[0]["event_type"], rows[0]["resource_id"]) == (
        "WH-EVT-9", "CUSTOMER.DISPUTE.CREATED", "PP-R-1",
    )
    assert "buyer@example.com" not in str(dict(rows[0]))


def test_only_dispute_created_is_left_unprocessed(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)
    other = {"id": "WH-EVT-10", "event_type": "CUSTOMER.DISPUTE.UPDATED"}

    with TestClient(app) as client:
        client.post(URL, content=json.dumps(DISPUTE_EVENT), headers=HEADERS)
        client.post(URL, content=json.dumps(other), headers=HEADERS)
        rows = {r["event_id"]: r["processed_at"] for r in stored_events()}

    assert rows["WH-EVT-9"] is None and rows["WH-EVT-10"] is not None


def test_redelivered_processed_event_is_200_and_one_row(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)
    other = json.dumps({"id": "WH-EVT-11", "event_type": "PAYMENT.CAPTURE.COMPLETED"})

    with TestClient(app) as client:
        first = client.post(URL, content=other, headers=HEADERS)
        second = client.post(URL, content=other, headers=HEADERS)
        rows = stored_events()

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == {"status": "duplicate"} and len(rows) == 1


def iso(delta):
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.mark.parametrize(
    "sent",
    [iso(-timedelta(hours=73)), iso(timedelta(minutes=6)), None, "yesterday", "2026-10-12T00:00:00"],
)
def test_bad_transmission_time_is_400_without_calling_paypal(paypal, sent):
    headers = {k: v for k, v in HEADERS.items() if k != "paypal-transmission-time"}
    if sent:
        headers["paypal-transmission-time"] = sent

    assert post(headers=headers).status_code == 400
    assert not paypal.verify_route.called


@pytest.mark.parametrize("sent", [iso(-timedelta(hours=71)), iso(timedelta(minutes=4))])
def test_transmission_time_inside_the_window_passes(paypal, sent):
    paypal.verify_route.mock(return_value=SUCCESS)
    assert post(headers={**HEADERS, "paypal-transmission-time": sent}).status_code == 200


def test_unverified_event_is_not_persisted(paypal):
    paypal.verify_route.mock(return_value=httpx.Response(200, json={"verification_status": "FAILURE"}))

    with TestClient(app) as client:
        response = client.post(URL, content=json.dumps(DISPUTE_EVENT), headers=HEADERS)
        rows = stored_events()

    assert response.status_code == 400
    assert rows == []


def test_redelivered_event_is_a_noop(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)

    with TestClient(app) as client:
        first = client.post(URL, content=json.dumps(DISPUTE_EVENT), headers=HEADERS)
        second = client.post(URL, content=json.dumps(DISPUTE_EVENT), headers=HEADERS)
        rows = stored_events()

    assert first.json() == {"status": "received"}
    assert second.status_code == 200 and second.json() == {"status": "duplicate"}
    assert len(rows) == 1


def test_event_without_type_is_stored_as_unknown_and_long_id_is_rejected(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)

    with TestClient(app) as client:
        ok = client.post(URL, content=json.dumps({"id": "WH-NO-TYPE"}), headers=HEADERS)
        too_long = client.post(URL, content=json.dumps({"id": "x" * 101}), headers=HEADERS)
        rows = stored_events()

    assert ok.status_code == 200 and too_long.status_code == 400
    assert [(r["event_id"], r["event_type"]) for r in rows] == [("WH-NO-TYPE", "UNKNOWN")]


def test_database_error_while_recording_is_503_so_paypal_redelivers(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)

    class BrokenEngine:
        def __init__(self, real):
            self.real = real

        def begin(self):
            raise OperationalError("INSERT", {}, Exception("db down"))

        def dispose(self):
            self.real.dispose()

    with TestClient(app) as client:
        app.state.engine = BrokenEngine(app.state.engine)
        response = client.post(URL, content=json.dumps(DISPUTE_EVENT), headers=HEADERS)

    assert response.status_code == 503


def test_startup_fails_without_client_credentials(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_secret", "")
    with pytest.raises(RuntimeError, match="PAYPAL_CLIENT_SECRET"):
        with TestClient(app):
            pass


def test_dispute_created_without_a_dispute_id_is_marked_processed(paypal):
    paypal.verify_route.mock(return_value=SUCCESS)
    event = {"id": "WH-EVT-12", "event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {}}

    with TestClient(app) as client:
        client.post(URL, content=json.dumps(event), headers=HEADERS)
        rows = {r["event_id"]: r["processed_at"] for r in stored_events()}

    assert rows["WH-EVT-12"] is not None  # nothing for the agent to fetch, so it must not wait forever
