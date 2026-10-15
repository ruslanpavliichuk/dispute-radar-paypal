from pydantic_settings import BaseSettings, SettingsConfigDict


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
    if "sandbox" not in settings.paypal_api_base:
        raise RuntimeError("PAYPAL_API_BASE must be a sandbox URL; this project is sandbox-only")
