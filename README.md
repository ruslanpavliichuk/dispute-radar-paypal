# Dispute Radar

Predicts which PayPal orders will turn into disputes and acts before the buyer files one. Calibrated LightGBM models with SHAP explanations, and an evidence-backed response when a dispute still arrives. Built for the PayPal AI Hackathon 2026.

**Status:** early build (T01 to T12 done: webhook intake, demo shop, risk, outcome and claim models, scoring API, response agent, prevention agent, seller approval API). The dashboard (T14) is not built yet, so approvals go through the API. This README covers setup for what exists today; a full run guide lands at T18.

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

`make disputes` generates 20,000 synthetic disputes into `ml/data/processed/disputes.parquet` from the published rules in `ml/generator_rules.yaml` (seeded, deterministic) and prints win rates per claim type. Outcomes use PayPal's `dispute_outcome` codes (seller wins on RESOLVED_SELLER_FAVOUR and RESOLVED_WITH_PAYOUT; CANCELED_BY_BUYER is kept but never trained on). **The labels follow our own rules, which model PayPal Seller Protection; they are not real PayPal outcomes.**

`make train` runs a 12-candidate LightGBM sweep, fits an isotonic calibrator, and writes the model, calibrator, model card, calibration plot, SHAP importance and threshold tables to `ml/artifacts/risk/` (about 30 seconds). Runs are logged to MLflow (`mlflow ui --backend-store-uri sqlite:///mlflow.db`). The T05 baseline stays in `ml/artifacts/risk_baseline/` for comparison. Read `model_card.json` before trusting the numbers: risk is only informative once shipping signals exist, and the calibrator over-predicts on the later test period.

## Choosing an LLM (no vendor lock-in)

An LLM is used in three places: weak-labelling CFPB narratives and writing synthetic buyer messages (claim classifier training), drafting dispute responses, and the benchmark against the trained model. All of it goes through one function, `ml/llm.py: complete_json(prompt, schema)`, and the provider is a setting in `.env`. **The shipped training run used Gemini's free tier, because the author chose not to pay for API credits. Nothing in the project requires a particular vendor.**

| Provider | `.env` |
|---|---|
| Gemini (default, free tier at [aistudio.google.com](https://aistudio.google.com)) | `LLM_PROVIDER=gemini`, `GEMINI_API_KEY=...` |
| Claude | `LLM_PROVIDER=claude`, `ANTHROPIC_API_KEY=...` |
| Any OpenAI-compatible API (OpenAI, xAI Grok, ...) | `LLM_PROVIDER=openai`, `LLM_API_KEY=...`, `LLM_BASE_URL=https://api.x.ai/v1`, `LLM_MODEL=<model name>` |

Set `LLM_MODEL` to override the default model (required for `openai`). Gemini was run end to end; the Claude and OpenAI-compatible paths are covered by mocked tests only and have not been run against the live APIs. A model that cannot return JSON matching a schema will not work. Free tiers are rate-limited and may use prompts to improve the vendor's products: fine for public CFPB text and synthetic data, not for real customer data.

## Training the models

The trained models are committed under `ml/artifacts/` (risk, outcome, claims), so the API runs without training. To rebuild them:

```bash
make data      # Olist for the risk model (see above)
make disputes  # synthetic disputes for the outcome model
make claims    # claim classifier data: CFPB narratives (about 900 MB download, deleted after filtering) + LLM labels. NEEDS an LLM key.
make train     # risk model, outcome model, claim classifier
```

`make claims` makes about 400 LLM calls and takes 30 to 40 minutes on Gemini's free tier. Answers are cached in `ml/data/processed/claims_*.jsonl`, so a crash or rate limit resumes instead of starting over; delete those files to relabel from scratch with another provider. `make train` takes a few minutes; if your machine struggles, prefix it with `OMP_NUM_THREADS=2`.

**Claim classifier honesty note.** The 3,000 CFPB labels come from an LLM, not a human. A second blind LLM labelled a 200-row sample and a human labelled every row where they disagreed (`ml/artifacts/claims/`). On that sample the model scores macro F1 0.74 (weakest class: `not_as_described`, 0.53), and the weak labels agree with the checked truth 94% of the time. 171 of the 200 truths are two LLMs agreeing, so shared mistakes are not caught. CFPB complaints are US card disputes; the product sees UK marketplace messages.

## Scoring API

`make dev` also serves `POST /score/risk`, `/score/outcome` and `/score/claim` (request fields are in `api/scoring.py`). Each returns a score and the top 3 reasons: SHAP values for the risk and outcome models, and for claims the stretches of the message that most support the label. The claim probabilities are not calibrated. There is no authentication yet; it goes behind the proxy before deployment.

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

## The agents and seller approval

Both agents run as a background loop inside the API process (`AGENT_ENABLED=true`, the default). They **propose**; nothing is sent to PayPal until the seller approves. The LLM only writes draft text; it never decides to call PayPal.

**Response agent.** A verified `CUSTOMER.DISPUTE.CREATED` webhook becomes one proposal: it reads the dispute from PayPal, takes the claim type from the dispute's `reason`, scores the win probability with the outcome model, and picks the action with the best expected value (fight, offer 30% back, or accept; the fee is assumed to be 14 GBP at chargeback stage, unchecked against PayPal). Fighting means `provide_evidence` on item-not-received disputes and a plain message otherwise; it never proposes `escalate`. The draft is written from facts in our own database and cited by id, with a plain template if the LLM fails. The buyer's message text is never sent to the drafting LLM. Win probabilities are crude: the agent cannot see tracking or delivery evidence for a sandbox dispute, so those inputs default to "no" (the defaults are stored on each proposal).

**Prevention agent.** `scripts/simulate_tracking.py` stands in for a carrier feed:

```bash
uv run python -m scripts.simulate_tracking <PAYPAL_ORDER_ID> accepted --day 1
uv run python -m scripts.simulate_tracking <PAYPAL_ORDER_ID> in_transit --day 3
uv run python -m scripts.simulate_tracking <PAYPAL_ORDER_ID> stuck_at_depot --day 16
```

Each event re-scores the order with the risk model (event time is the clock, so a demo does not have to wait). From the first shipping event onwards, a score above the bar (0.20 in the first week, 0.10 after) proposes a tracking upload; an order past its delivery estimate also gets a goodwill refund proposal (10% of the order). A `delivered` event cancels open proposals. Before shipping the agent does nothing, because the risk model has no skill at checkout (see the risk model card).

Know the limits: the model has **no "stuck at depot" feature**; it sees "shipped, not delivered, time passing", so the score rises only because the event is dated past the 14-day delivery window we assume. It was trained on Brazilian Olist orders and is applied to UK sandbox orders, so scores show how the system behaves, not real UK dispute rates. There is no email service: the buyer is told through PayPal's own notification when tracking is uploaded.

**Approving.** Set `ADMIN_TOKEN` in `.env` (generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`); without it the routes answer `503`. Send it as the `X-Admin-Token` header:

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/api/proposals            # list
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/api/proposals/<ID>/approve
curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/api/proposals/<ID>/reject
curl -H "X-Admin-Token: $ADMIN_TOKEN" localhost:8000/api/orders/attention     # MISMATCH, UNKNOWN, stale PENDING orders
```

Approval makes exactly one PayPal call, after re-reading the dispute or order: it is refused if PayPal no longer offers the action, if the payment is held (for example because of a dispute) or already refunded, if the order has been delivered, or if a refund and a dispute response would pay out twice. Refund amounts are recomputed from the database at approval, never taken from the proposal or the request. Approve may carry a `{"note": "..."}` to replace the draft text (refunds only, up to 255 characters).

Checked live against the sandbox: a refund offer on a dispute, a tracking upload (appears on the PayPal order), and a 1.96 GBP goodwill refund. Not checked live: the `send_message`, `provide_evidence` and `accept_claim` bodies sent through the API.
