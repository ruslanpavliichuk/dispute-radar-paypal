import json
import logging
import time
from urllib.parse import urlparse

import httpx

from api.config import settings

logger = logging.getLogger("dispute_radar.paypal")

SIGNATURE_HEADERS = (
    "paypal-auth-algo",
    "paypal-cert-url",
    "paypal-transmission-id",
    "paypal-transmission-sig",
    "paypal-transmission-time",
)
CERT_HOSTS = {"api.paypal.com", "api.sandbox.paypal.com", "api-m.paypal.com", "api-m.sandbox.paypal.com"}


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
        # one retry: PayPal can revoke a token before our cached expiry
        for attempt in (1, 2):
            token = await self._get_token()
            headers = {**kwargs.get("headers", {}), "Authorization": f"Bearer {token}"}
            response = await self._client.request(method, path, **{**kwargs, "headers": headers})
            if response.status_code != 401 or attempt == 2:
                return response
            self._token = None
        return response

    async def verify_webhook_signature(
        self, headers: dict, body: str, webhook_id: str
    ) -> bool:
        """True only when PayPal confirms the signature.

        False for anything that can't be a valid delivery (missing or untrusted
        signature headers, PayPal's 400, FAILURE). Raises httpx.HTTPError when PayPal
        itself can't answer (5xx, 401 after retry, timeout), so the caller can ask
        PayPal to retry instead of reporting a bad signature.
        """
        values = {name: headers.get(name) for name in SIGNATURE_HEADERS}
        if not all(values.values()):
            return False
        cert = urlparse(values["paypal-cert-url"])
        # netloc, not hostname: rejects userinfo, ports and backslash tricks outright
        if cert.scheme != "https" or cert.netloc not in CERT_HOSTS:
            return False
        if values["paypal-auth-algo"] != "SHA256withRSA":
            return False

        fields = json.dumps(
            {
                "auth_algo": values["paypal-auth-algo"],
                "cert_url": values["paypal-cert-url"],
                "transmission_id": values["paypal-transmission-id"],
                "transmission_sig": values["paypal-transmission-sig"],
                "transmission_time": values["paypal-transmission-time"],
                "webhook_id": webhook_id,
            }
        )
        # splice the raw body in so PayPal sees the exact bytes it signed
        content = f'{fields[:-1]}, "webhook_event": {body}}}'
        response = await self.request(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            content=content,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code == 400:
            # PayPal answers 400 for events it can't verify, e.g. simulator mock events
            logger.warning("verify-webhook-signature 400: %.200r", response.text)
            return False
        response.raise_for_status()
        return response.json().get("verification_status") == "SUCCESS"

    async def list_disputes(self, page_size: int = 5) -> dict:
        response = await self.request(
            "GET", "/v1/customer/disputes", params={"page_size": page_size}
        )
        response.raise_for_status()
        return response.json()
