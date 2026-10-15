from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    # blank until the webhook is registered; the route answers 503 while it is unset
    paypal_webhook_id: str = ""
    paypal_api_base: str = "https://api-m.sandbox.paypal.com"
    # /docs and /openapi.json are public through the dev tunnel, so off unless asked for
    enable_docs: bool = False


settings = Settings()


def require_paypal_credentials() -> None:
    missing = [
        name.upper()
        for name in ("paypal_client_id", "paypal_client_secret")
        if not getattr(settings, name)
    ]
    if missing:
        raise RuntimeError(f"Missing required settings in .env: {', '.join(missing)}")
