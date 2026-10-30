from contextlib import asynccontextmanager

from fastapi import FastAPI
from paypalserversdk.configuration import Environment
from paypalserversdk.http.auth.o_auth_2 import ClientCredentialsAuthCredentials
from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient

from api.config import require_settings, settings
from api.db import init_db, make_engine
from api.paypal_client import PayPalClient
from api.scoring import load_models
from api.scoring import router as scoring_router
from api.shop import router as shop_router
from api.webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    require_settings()
    app.state.models = load_models()
    app.state.paypal_client = PayPalClient()
    try:
        # sandbox only, hard-coded: this project never talks to live PayPal
        app.state.orders_api = PaypalServersdkClient(
            environment=Environment.SANDBOX,
            timeout=15,
            client_credentials_auth_credentials=ClientCredentialsAuthCredentials(
                o_auth_client_id=settings.paypal_client_id,
                o_auth_client_secret=settings.paypal_client_secret,
            ),
        ).orders
        app.state.engine = make_engine(settings.database_url)
        try:
            init_db(app.state.engine)
            yield
        finally:
            app.state.engine.dispose()
    finally:
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
app.include_router(shop_router)
app.include_router(scoring_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
