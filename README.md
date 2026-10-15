# Dispute Radar

Predicts which PayPal orders will turn into disputes and acts before the buyer files one. Calibrated LightGBM models with SHAP explanations, and an evidence-backed response when a dispute still arrives. Built for the PayPal AI Hackathon 2026.

**Status:** early build (T01 and T02 done: webhook intake and the sandbox demo shop). This README covers setup for what exists today; a full run guide lands at T18.

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

## Demo shop (sandbox)

Needs Docker (for Postgres) and Node 20+.

```bash
make db                                  # Postgres 16 on 127.0.0.1:5432
cp web/.env.local.example web/.env.local # set NEXT_PUBLIC_PAYPAL_CLIENT_ID (public sandbox client ID)
cd web && npm install && cd ..
make dev                                 # API on :8000
make web                                 # shop on http://localhost:3000
```

In `.env` also set `DATABASE_URL` (see `.env.example`). Buy with a **sandbox Personal** account; the order appears in the `orders` table with its capture ID.

- Prices come from `api/catalogue.json`, never from the browser. `make catalogue` refreshes it from Channel3 (needs `CHANNEL3_API_KEY`); the shop itself never calls Channel3.
- Channel3 prices are in USD and are **relabelled as GBP at face value** for the demo (no FX conversion).

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
