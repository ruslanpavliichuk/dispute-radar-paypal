from contextlib import asynccontextmanager

from fastapi import FastAPI

from api.paypal_client import PayPalClient
from api.webhooks import router as webhooks_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.paypal_client = PayPalClient()
    yield
    await app.state.paypal_client.aclose()


app = FastAPI(title="Dispute Radar API", lifespan=lifespan)
app.include_router(webhooks_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
