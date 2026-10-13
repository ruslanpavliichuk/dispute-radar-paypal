import httpx
import pytest
import respx

from api.config import settings
from api.paypal_client import PayPalClient


@pytest.mark.asyncio
async def test_get_token_fetches_and_caches():
    client = PayPalClient()
    try:
        with respx.mock(base_url=settings.paypal_api_base) as mock:
            route = mock.post("/v1/oauth2/token").mock(
                return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
            )
            token_a = await client._get_token()
            token_b = await client._get_token()
            assert token_a == token_b == "tok-1"
            assert route.call_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_verify_webhook_signature_success():
    client = PayPalClient()
    try:
        with respx.mock(base_url=settings.paypal_api_base) as mock:
            mock.post("/v1/oauth2/token").mock(
                return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
            )
            mock.post("/v1/notifications/verify-webhook-signature").mock(
                return_value=httpx.Response(200, json={"verification_status": "SUCCESS"})
            )
            verified = await client.verify_webhook_signature(
                headers={
                    "paypal-auth-algo": "SHA256withRSA",
                    "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
                    "paypal-transmission-id": "abc",
                    "paypal-transmission-sig": "sig",
                    "paypal-transmission-time": "2026-10-12T00:00:00Z",
                },
                body='{"event_type": "CUSTOMER.DISPUTE.CREATED"}',
                webhook_id="WH-123",
            )
            assert verified is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_verify_webhook_signature_paypal_400_is_unverified_not_a_crash():
    client = PayPalClient()
    try:
        with respx.mock(base_url=settings.paypal_api_base) as mock:
            mock.post("/v1/oauth2/token").mock(
                return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
            )
            mock.post("/v1/notifications/verify-webhook-signature").mock(
                return_value=httpx.Response(400, json={"name": "VALIDATION_ERROR"})
            )
            verified = await client.verify_webhook_signature(
                headers={}, body="{}", webhook_id="WH-123"
            )
            assert verified is False
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_verify_webhook_signature_failure():
    client = PayPalClient()
    try:
        with respx.mock(base_url=settings.paypal_api_base) as mock:
            mock.post("/v1/oauth2/token").mock(
                return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
            )
            mock.post("/v1/notifications/verify-webhook-signature").mock(
                return_value=httpx.Response(200, json={"verification_status": "FAILURE"})
            )
            verified = await client.verify_webhook_signature(
                headers={}, body="{}", webhook_id="WH-123"
            )
            assert verified is False
    finally:
        await client.aclose()
