import pytest

from api.config import settings
from api.paypal_client import PayPalClient

requires_sandbox_credentials = pytest.mark.skipif(
    not (settings.paypal_client_id and settings.paypal_client_secret),
    reason="PAYPAL_CLIENT_ID/PAYPAL_CLIENT_SECRET not set in .env",
)


@requires_sandbox_credentials
async def test_list_disputes_against_live_sandbox():
    client = PayPalClient()
    try:
        result = await client.list_disputes()
        assert isinstance(result, dict)
    finally:
        await client.aclose()
