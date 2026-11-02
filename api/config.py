from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_HOSTS = {"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    # blank until the webhook is registered; the route answers 503 while it is unset
    paypal_webhook_id: str = ""
    paypal_api_base: str = "https://api-m.sandbox.paypal.com"
    database_url: str = ""
    # only read by scripts/fetch_catalogue.py; the shop never calls Channel3 at runtime
    channel3_api_key: str = ""
    # only read by ml/data/download_olist.py; a KGAT_ token needs no username
    kaggle_api_key: str = ""
    kaggle_username: str = ""
    # LLM provider for ml/llm.py: "gemini" (free tier, default), "claude" or "openai" (OpenAI-compatible); blank model = default
    llm_provider: str = "gemini"
    llm_model: str = ""
    # "openai" provider: any OpenAI-compatible chat API (OpenAI, xAI Grok https://api.x.ai/v1, local servers...)
    llm_base_url: str = ""
    llm_api_key: str = ""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    # response agent (T11b): the background loop that turns dispute-created events into proposals
    agent_enabled: bool = True
    agent_poll_seconds: float = 5.0
    seller_account_age_months: int = 18  # the dispute does not carry it
    dispute_fee_minor: int = 1400  # pence, CHARGEBACK only; our assumption, not checked against PayPal
    offer_share_pct: int = 30  # share of the order value offered back as a partial refund
    # prevention agent (T12): risk bars by days since purchase, the assumed delivery window, and the goodwill refund share
    prevention_min_risk_early: float = 0.20  # events dated under 8 days after purchase
    prevention_min_risk_late: float = 0.10  # 8 days or more
    est_window_days: float = 14.0  # our shop has no delivery estimate; see tests/test_prevention.py for how it was chosen
    goodwill_share_pct: int = Field(10, ge=1, le=50)  # keeps the suggested refund well under the order value
    # header X-Admin-Token for /api/proposals; blank = approval switched off (503), never open
    admin_token: str = ""
    # /docs and /openapi.json are public through the dev tunnel, so off unless asked for
    enable_docs: bool = False


settings = Settings()


def require_settings() -> None:
    missing = [
        name.upper()
        for name in ("paypal_client_id", "paypal_client_secret", "database_url")
        if not getattr(settings, name)
    ]
    if missing:
        raise RuntimeError(f"Missing required settings in .env: {', '.join(missing)}")
    if urlparse(settings.paypal_api_base).hostname not in SANDBOX_HOSTS:
        raise RuntimeError("PAYPAL_API_BASE must be a PayPal sandbox host; this project is sandbox-only")
