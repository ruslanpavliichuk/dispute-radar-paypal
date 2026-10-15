import json

import httpx
import pytest
import respx

from api.config import settings
from api.paypal_client import PayPalClient

HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "paypal-transmission-id": "abc",
    "paypal-transmission-sig": "sig",
    "paypal-transmission-time": "2026-10-12T00:00:00Z",
}
TOKEN = httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
VERIFY_PATH = "/v1/notifications/verify-webhook-signature"


@pytest.fixture
async def client():
    c = PayPalClient()
    yield c
    await c.aclose()


@pytest.fixture
def paypal():
    with respx.mock(base_url=settings.paypal_api_base, assert_all_called=False) as mock:
        mock.token_route = mock.post("/v1/oauth2/token").mock(return_value=TOKEN)
        mock.verify_route = mock.post(VERIFY_PATH)
        yield mock


async def test_get_token_fetches_and_caches(client, paypal):
    assert await client._get_token() == await client._get_token() == "tok-1"
    assert paypal.token_route.call_count == 1


async def test_verify_success_sends_raw_body_verbatim(client, paypal):
    paypal.verify_route.mock(return_value=httpx.Response(200, json={"verification_status": "SUCCESS"}))
    body = '{"id":"WH-1",  "amount": 1.10, "name": "caf\\u00e9"}'

    assert await client.verify_webhook_signature(HEADERS, body, "WH-123") is True

    sent = paypal.verify_route.calls.last.request.content.decode()
    assert f'"webhook_event": {body}' in sent
    parsed = json.loads(sent)
    assert parsed["webhook_id"] == "WH-123"
    assert parsed["transmission_id"] == "abc"
    assert parsed["cert_url"] == HEADERS["paypal-cert-url"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"name": "VALIDATION_ERROR"}),
        httpx.Response(200, json={"verification_status": "FAILURE"}),
    ],
)
async def test_verify_unverified_is_false(client, paypal, response):
    paypal.verify_route.mock(return_value=response)
    assert await client.verify_webhook_signature(HEADERS, "{}", "WH-123") is False


@pytest.mark.parametrize(
    "bad_headers",
    [
        {},
        {k: v for k, v in HEADERS.items() if k != "paypal-transmission-sig"},
        {**HEADERS, "paypal-cert-url": "https://evil.example.com/cert"},
        {**HEADERS, "paypal-cert-url": "http://api.sandbox.paypal.com/cert"},
        {**HEADERS, "paypal-cert-url": "https://api.paypal.com.evil.example.com/cert"},
        {**HEADERS, "paypal-auth-algo": "none"},
        {**HEADERS, "paypal-cert-url": "https://evil.example.com\\@api.paypal.com/cert"},
        {**HEADERS, "paypal-cert-url": "https://api.paypal.com:8443/cert"},
        {**HEADERS, "paypal-cert-url": "https://user@api.paypal.com/cert"},
    ],
)
async def test_verify_rejects_bad_headers_without_calling_paypal(client, paypal, bad_headers):
    assert await client.verify_webhook_signature(bad_headers, "{}", "WH-123") is False
    assert not paypal.verify_route.called
    assert not paypal.token_route.called


async def test_verify_raises_when_paypal_is_down(client, paypal):
    paypal.verify_route.mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await client.verify_webhook_signature(HEADERS, "{}", "WH-123")


async def test_request_refreshes_token_once_on_401(client, paypal):
    paypal.verify_route.side_effect = [
        httpx.Response(401),
        httpx.Response(200, json={"verification_status": "SUCCESS"}),
    ]
    assert await client.verify_webhook_signature(HEADERS, "{}", "WH-123") is True
    assert paypal.token_route.call_count == 2


async def test_request_does_not_loop_on_persistent_401(client, paypal):
    paypal.verify_route.mock(return_value=httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        await client.verify_webhook_signature(HEADERS, "{}", "WH-123")
    assert paypal.verify_route.call_count == 2
