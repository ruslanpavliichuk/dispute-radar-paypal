import httpx
import respx
from fastapi.testclient import TestClient

from api.config import settings
from api.main import app

WEBHOOK_HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
    "paypal-transmission-id": "abc",
    "paypal-transmission-sig": "sig",
    "paypal-transmission-time": "2026-10-12T00:00:00Z",
}
WEBHOOK_BODY = {"id": "WH-EVT-1", "event_type": "CUSTOMER.DISPUTE.CREATED"}


def test_webhook_route_accepts_valid_signature():
    with respx.mock(base_url=settings.paypal_api_base) as mock:
        mock.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        )
        mock.post("/v1/notifications/verify-webhook-signature").mock(
            return_value=httpx.Response(200, json={"verification_status": "SUCCESS"})
        )
        with TestClient(app) as client:
            response = client.post("/api/webhooks/paypal", json=WEBHOOK_BODY, headers=WEBHOOK_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "received"}


def test_webhook_route_rejects_invalid_signature():
    with respx.mock(base_url=settings.paypal_api_base) as mock:
        mock.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        )
        mock.post("/v1/notifications/verify-webhook-signature").mock(
            return_value=httpx.Response(200, json={"verification_status": "FAILURE"})
        )
        with TestClient(app) as client:
            response = client.post("/api/webhooks/paypal", json=WEBHOOK_BODY, headers=WEBHOOK_HEADERS)

    assert response.status_code == 400
