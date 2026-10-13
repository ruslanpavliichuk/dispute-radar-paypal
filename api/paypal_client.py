import json
import logging
import time

import httpx

from api.config import settings

logger = logging.getLogger("dispute_radar.paypal")


class PayPalClient:
    """Thin REST client for PayPal endpoints the Server SDK doesn't cover
    (disputes, tracking, webhook signature verification). Handles OAuth2
    client-credentials token fetch and caching.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(base_url=settings.paypal_api_base, timeout=10)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        response = await self._client.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(settings.paypal_client_id, settings.paypal_client_secret),
        )
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        self._token_expires_at = time.monotonic() + body["expires_in"] - 60
        return self._token

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        token = await self._get_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        return await self._client.request(method, path, headers=headers, **kwargs)

    async def verify_webhook_signature(
        self, headers: dict, body: str, webhook_id: str
    ) -> bool:
        payload = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": json.loads(body),
        }
        response = await self.request(
            "POST", "/v1/notifications/verify-webhook-signature", json=payload
        )
        if not response.is_success:
            # PayPal answers 400 for events it can't verify, e.g. simulator mock events
            logger.warning("verify-webhook-signature %s: %s", response.status_code, response.text)
            return False
        return response.json().get("verification_status") == "SUCCESS"

    async def list_disputes(self, page_size: int = 5) -> dict:
        response = await self.request(
            "GET", "/v1/customer/disputes", params={"page_size": page_size}
        )
        response.raise_for_status()
        return response.json()
