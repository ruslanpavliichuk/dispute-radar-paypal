# Dispute Radar

Predicts which PayPal orders will turn into disputes and acts before the buyer files one. Calibrated LightGBM models with SHAP explanations, and an evidence-backed response when a dispute still arrives. Built for the PayPal AI Hackathon 2026.

**Status:** early build (T01 in progress). This README covers setup for what exists today; a full run guide lands at T18.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
make setup   # uv sync
cp .env.example .env
```

Fill in `.env` with your PayPal **sandbox** app's Client ID and Secret (Disputes + Transaction search features must be enabled on that app in the PayPal Developer Dashboard). Leave `PAYPAL_WEBHOOK_ID` blank until you've registered a webhook — see below.

## Run

```bash
make dev    # uvicorn api.main:app --reload, http://127.0.0.1:8000
make test   # pytest
```

`GET /health` confirms the API is up.

## Testing the webhook locally

PayPal can't reach `localhost`, so expose the dev server with a tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

Then, in the PayPal Developer Dashboard, on your sandbox app:
1. Add a webhook pointing at `https://<your-tunnel-subdomain>.trycloudflare.com/api/webhooks/paypal`.
2. Subscribe to at least `CUSTOMER.DISPUTE.CREATED`.
3. Copy the webhook's ID into `.env` as `PAYPAL_WEBHOOK_ID`.
4. Use the dashboard's "Send test webhook" button — the request should return `200 {"status": "received"}` and show up in `make dev`'s logs.

The cloudflared quick-tunnel URL changes every time you restart it, so you'll need to update the webhook URL in the dashboard after each restart.
