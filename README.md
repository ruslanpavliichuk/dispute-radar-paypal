# Dispute Radar

Predicts which PayPal orders will turn into disputes and acts before the buyer files one. Calibrated LightGBM models with SHAP explanations, and an evidence-backed response when a dispute still arrives. Built for the PayPal AI Hackathon 2026.

**Status:** early build (T01 and T02 done: webhook intake and the sandbox demo shop). This README covers setup for what exists today; a full run guide lands at T18.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+. On macOS, LightGBM also needs the OpenMP runtime: `brew install libomp`.

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

## Risk-model data (Olist)

```bash
make data    # downloads Olist (needs KAGGLE_API_KEY in .env), builds ml/data/processed/risk_snapshots.parquet
```

If the download fails, put the Kaggle zip at `ml/data/raw/olist.zip` and run `make data` again. The label is a **proxy** (a 1 or 2 star review plus a late delivery or a delivery/product-condition keyword), not real PayPal disputes; the exact rule is in CONTEXT.md section 7.3. Known limits: the base rate drifts across the date splits, and review text is Portuguese.

`make disputes` generates 20,000 synthetic disputes into `ml/data/processed/disputes.parquet` from the published rules in `ml/generator_rules.yaml` (seeded, deterministic) and prints win rates per claim type. **The labels follow our own rules, which model PayPal Seller Protection; they are not real PayPal outcomes.**

`make train` runs a 12-candidate LightGBM sweep, fits an isotonic calibrator, and writes the model, calibrator, model card, calibration plot, SHAP importance and threshold tables to `ml/artifacts/risk/` (about 30 seconds). Runs are logged to MLflow (`mlflow ui --backend-store-uri sqlite:///mlflow.db`). The T05 baseline stays in `ml/artifacts/risk_baseline/` for comparison. Read `model_card.json` before trusting the numbers: risk is only informative once shipping signals exist, and the calibrator over-predicts on the later test period.

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

Then, in the PayPal Developer Dashboard, open your sandbox **app** (Apps & Credentials → your app → Webhooks, *not* the Webhooks Simulator):
1. Add a webhook pointing at `https://<your-tunnel-subdomain>.trycloudflare.com/api/webhooks/paypal`. Delete any webhook that points at an old tunnel.
2. Subscribe it to `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED` and `CUSTOMER.DISPUTE.RESOLVED`.
3. Copy the webhook's ID into `.env` as `PAYPAL_WEBHOOK_ID`, then **restart `make dev`** (`--reload` does not re-read `.env`).

The cloudflared quick-tunnel URL changes every time you restart it, so you'll need to update the webhook URL in the dashboard after each restart.

> The Webhooks Simulator sends mock events that PayPal cannot verify, so they are rejected with `400`. Use a real dispute (below) to see a verified event.

## Simulating a dispute (sandbox)

Disputes can't be created through the API, so the buyer files one by hand and `scripts/simulate_dispute.py` moves it along. Sandbox only: the script refuses a non-sandbox `PAYPAL_API_BASE`, refuses disputes that aren't for one of our `orders`, and always needs an explicit dispute id.

1. Make a purchase in the demo shop with your sandbox **Personal** account; confirm the order is `COMPLETED` in `orders`.
2. Log in at <https://www.sandbox.paypal.com> as that buyer, open the payment, choose **Report a problem**, and pick "item not received".
3. A `CUSTOMER.DISPUTE.CREATED` delivery should arrive. Check it was verified and recorded:
   ```bash
   docker compose exec db psql -U dispute -d dispute_radar -c \
     "select event_type, resource_id, received_at from webhook_events order by received_at;"
   ```
4. Find the dispute, see what PayPal currently allows, and escalate it:
   ```bash
   uv run python -m scripts.simulate_dispute list
   uv run python -m scripts.simulate_dispute show <DISPUTE_ID>      # prints "actions PayPal allows now"
   uv run python -m scripts.simulate_dispute escalate <DISPUTE_ID>  # INQUIRY -> claim, status UNDER_REVIEW
   ```
5. Re-run the `select` above: you should see `CREATED` and several `UPDATED` events, each verified and stored once.

`evidence` (require-evidence) and `adjudicate` are in the script too, but PayPal only accepts them when the dispute's own links include them. **In practice the sandbox does not offer those links on disputes filed through the Resolution Center**, so both return `ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE`. The script prints that reason and exits 1. The sandbox create-dispute API is limited-release and its request body is not public.

`webhook_events` stores only the event id, type, resource id and time (never the body, which holds the buyer's email and messages). A redelivered event id is ignored.
