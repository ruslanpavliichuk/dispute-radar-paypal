from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.config import require_paypal_credentials, settings
from api.paypal_client import PayPalClient
from api.webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    require_paypal_credentials()
    app.state.paypal_client = PayPalClient()
    yield
    await app.state.paypal_client.aclose()


docs = settings.enable_docs
app = FastAPI(
    title="Dispute Radar API",
    lifespan=lifespan,
    docs_url="/docs" if docs else None,
    redoc_url="/redoc" if docs else None,
    openapi_url="/openapi.json" if docs else None,
)
app.include_router(webhooks_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
