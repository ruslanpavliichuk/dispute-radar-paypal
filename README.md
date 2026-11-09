# Dispute Radar

Dispute Radar helps a PayPal seller **stop disputes before they happen and answer the ones that still arrive**. It watches orders after shipping, scores the chance each one turns into a dispute, and when a buyer does file one it scores the chance of winning and drafts a plain, evidence-based response. Every action waits for the seller's approval. Nothing reaches PayPal until they click.

Built for the PayPal AI Hackathon 2026. Sandbox only: no real money, no real buyers.

- Live demo: <https://dispute-radar-web.onrender.com> (hosted on Render)
- Licence: MIT

## What it does

| Stage | What happens | Who decides |
|---|---|---|
| **Prevention** | A shipping event arrives (`accepted`, `in_transit`, `stuck_at_depot`). A calibrated LightGBM risk model re-scores the order. Above the bar it proposes a **tracking upload** and, past the delivery estimate, a **goodwill refund** (10%). | Seller approves |
| **Response** | A verified PayPal dispute webhook arrives. An outcome model estimates the win chance with SHAP reasons. The agent picks the best expected value of **fight, offer or accept**, and drafts the message from facts in our own database. | Seller approves |
| **Safety** | A goodwill refund already sent means no second refund: the agent proposes evidence instead. PayPal is re-read at approval, and each approval makes one call with an idempotency key. | Code |

The LLM only writes draft text. It never decides and never calls PayPal. Buyer messages are untrusted and never reach the drafting LLM.

## Try it (for judges)

1. Open the live demo. The shop is the home page; the seller pages are **Dashboard, Timeline, Evaluation, Studio** in the top bar.
2. **Dashboard:** enter the admin token, given in the Devpost testing instructions (the token is not stored in this repo). You will see the proposals the agents made, the approval queue, and a side panel with the reasons, the draft and the approve or reject buttons.
3. **Buy something** in the shop with the sandbox buyer account from the Devpost testing instructions (PayPal sandbox only; never use a real account). The order appears in the database after capture.
4. **Shipping delays and disputes** are simulated, because there is no carrier feed and PayPal's sandbox cannot create disputes through the API. These are shell scripts (see *The agents and seller approval* below); the demo video shows the full run: buy, delay, approve the tracking upload and refund, file a dispute as the buyer, approve the response.
5. **Evaluation** shows calibration, precision and recall, and the model-vs-LLM benchmark. **Timeline** shows each dispute from opened to response-due, and clicking a dot or bar explains it. **Studio** lets you drag fields into your own charts and ask the small "Radar" assistant what is waiting.

Everything on the live demo uses the PayPal **sandbox**.

## Screenshots

| Dashboard | Dispute timeline |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Timeline](docs/screenshots/timeline.png) |
| **Evaluation** | **Studio** |
| ![Evaluation](docs/screenshots/evaluation.png) | ![Studio](docs/screenshots/studio.png) |

## Model results

All numbers are on held-out data, probabilities calibrated with isotonic regression. Full cards are in `ml/artifacts/*/model_card.json` and drawn live on `/evaluation`.

| Model | Trained on | Test result |
|---|---|---|
| Dispute risk (LightGBM, SHAP) | Olist orders, 899,370 daily snapshots | PR-AUC 0.311 (base rate 0.049), Brier 0.0407 |
| Win probability (LightGBM, SHAP) | 20,000 generated disputes (published rules) | Brier 0.183, ECE 0.017, ROC-AUC 0.783 (true-probability ceiling: Brier 0.181) |
| Claim type (embeddings + logistic regression) | 3,000 CFPB narratives + 1,497 synthetic messages | Macro F1 0.72 on CFPB labels, 0.74 on hand-checked rows |
| Model vs LLM guess | 300 held-out disputes | Model Brier 0.169 vs Gemini 0.207, interval +0.012 to +0.065 |

## Data assumptions (read these)

- **Risk model:** trained on Brazilian Olist e-commerce orders, applied to UK sandbox orders. Scores show how the system behaves, not real UK dispute rates. The model has no "stuck at depot" feature: it sees shipped, not delivered, time passing, and we assume a 14 day delivery window.
- **Win probability:** PayPal does not publish dispute outcomes, so the labels come from **our own published generator rules** (`ml/generator_rules.yaml`). It measures calibration on known structure, not how PayPal really decides.
- **Dispute fee** (14 GBP at chargeback) and the 30% offer share are assumptions, not PayPal figures.
- **Tracking events** are simulated by a script standing in for a carrier feed. Disputes are filed by hand in the sandbox because the create-dispute API is limited release.
- **Evidence** is tracking proof only. Document uploads (photos, receipts) are not implemented.

## Tools and services

PayPal Server SDK (orders and capture), PayPal Disputes, Payments and Tracking REST APIs over `httpx`, APIMatic Context Plugin, Gemini (default LLM; Claude or any OpenAI-compatible API by config), LightGBM, SHAP, fastembed, MLflow, FastAPI, SQLAlchemy, Postgres, Next.js 16, AG Grid, AG Charts, AG Studio, Bryntum Scheduler, Channel3 (product catalogue snapshot), GitHub Actions, Render.

## Licences and third-party notes

- **This project:** MIT (`LICENSE`).
- **AG Grid Community and AG Charts Community:** MIT.
- **AG Studio:** commercial product used under a free **evaluation licence** that expires on 19 December 2026. `/studio` needs `NEXT_PUBLIC_AG_STUDIO_LICENSE`; without a key it will not load.
- **Bryntum Scheduler:** commercial product used through the **trial package** (`@bryntum/scheduler-trial`, aliased in `web/package.json`). It shows a trial watermark; a production deployment needs a purchased licence.
- **Data:** Olist Brazilian E-Commerce (Kaggle, CC BY-NC-SA 4.0, non-commercial) and the CFPB consumer complaint narratives (public domain, read from a Hugging Face mirror). Neither is committed: `make data` downloads them. Check the Olist terms before any commercial use.
- No PayPal logo or wordmark is used; the colours are only inspired by PayPal's.

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

`GET /health` confirms the API is up. Start the database with `docker compose up -d db` first and set `DATABASE_URL` in `.env`. The web app (shop, dashboard, evaluation, timeline, Studio) runs separately:

```bash
cd web
cp .env.local.example .env.local   # PayPal sandbox client id; AG Studio licence for /studio
npm install
npm run dev                        # http://localhost:3000, forwards /api/* to the API on :8000
```

The trained models are committed under `ml/artifacts/`, so `make train` is only needed to retrain.

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

Checked live against the sandbox: a refund offer on a dispute, a tracking upload (appears on the PayPal order), two small goodwill refunds (the capture webhook then moved the order to `PARTIALLY_REFUNDED`), and a refusal to act on a disputed order. Subscribe the dashboard webhook to `PAYMENT.CAPTURE.COMPLETED`, `.DENIED`, `.REFUNDED` and `.REVERSED` as well as the dispute events. `provide_evidence` sends proof of fulfilment with the order's tracking number (PayPal refused a plain `OTHER` note with `EVIDENCE_TYPE_IS_NOT_ALLOWED`); if the order has no tracking number the approval is refused. Not checked live: the `send_message` and `accept_claim` bodies, and the new evidence body.

## Benchmark: trained model vs an LLM's guess

`make benchmark` (or `uv run python -m ml.benchmark_llm`) asks the configured LLM for a win probability on 300 held-out generated disputes, using the same facts the outcome model sees plus a short general summary of PayPal Seller Protection rules, and scores both on the same rows. It writes `ml/artifacts/benchmark.json` and `ml/artifacts/benchmark_calibration.png`. Each provider and model keeps its own entry, so you can run it again with another `LLM_PROVIDER` or `LLM_MODEL` and compare side by side.

Result with Gemini (`gemini-3.1-flash-lite`, free tier), 300 of 300 rows scored, lower is better:

| Predictor | Brier | Log loss | ECE (uniform bins) |
|---|---|---|---|
| Trained outcome model | 0.169 | 0.516 | 0.078 |
| LLM (Gemini) | 0.207 | 0.621 | 0.118 |
| Constant guess (train win rate) | 0.239 | 0.670 | 0.007 |
| Oracle (the true generator probability) | 0.166 | 0.504 | 0.046 |

The trained model beats the LLM by 0.038 Brier (95% bootstrap interval +0.012 to +0.065, paired over the same rows). The LLM beats a constant guess, so it does use the facts, but it is overconfident at the low end: where it says about 8%, sellers actually win about 36% of the time. The constant guess has the best ECE because it never varies; ECE alone does not reward telling disputes apart.

Read this with care. The labels come from our own published generator rules (`ml/generator_rules.yaml`), so the benchmark tests calibration on known structure, not how PayPal really decides. The rules summary given to the LLM was written from general public knowledge and still shares the direction of a few effects (the 10-day deadline, stricter chargebacks) with the generator. One model, one prompt and one run: do not read it as a ranking of LLM vendors.

## Seller dashboard, evaluation and AG Studio

`web/` has three pages next to the shop: `/dashboard` (AG Grid queues of proposals, approve or reject from a drawer, needs the `ADMIN_TOKEN`), `/evaluation` (AG Charts drawn from the saved model cards and benchmark file), `/timeline` (Bryntum Scheduler: each dispute from opened to response-due, with the agent's steps as points on the row) and `/studio` (AG Studio over the same data, with a small "Radar" agent that answers from the proposals table and cannot change anything). `/studio` needs `NEXT_PUBLIC_AG_STUDIO_LICENSE` in `web/.env.local`; it is an evaluation key, valid until 19 December 2026.

## Deploy on Render

`render.yaml` describes the whole stack: a Postgres database, the API (Docker, with the agents running inside it, so keep one instance) and the Next.js web app. The browser only talks to the web service; it forwards `/api/*` to the API over Render's private network, so there is no CORS to configure. Databases are reached over TLS (`sslmode=require` for any non-local host).

1. In Render choose **New, Blueprint**, pick this repo, and fill in the secrets it asks for: `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` (sandbox only), `GEMINI_API_KEY`, and for the web service `NEXT_PUBLIC_PAYPAL_CLIENT_ID` and `NEXT_PUBLIC_AG_STUDIO_LICENSE`. `ADMIN_TOKEN` is generated for you; read it in the API service's environment tab, it is the dashboard password.
2. When the API is live, register its webhook in the PayPal developer dashboard (sandbox app, Webhooks): `https://<your-api>.onrender.com/api/webhooks/paypal`, events `CUSTOMER.DISPUTE.*` and `PAYMENT.CAPTURE.*`. Copy the webhook ID into `PAYPAL_WEBHOOK_ID` on the API service and let it redeploy. Until then the route answers 503.
3. For a first-time visitor, open a shell on the API service and run `python -m scripts.seed_demo`: three labelled demo orders with shipment events, scored by the real model. Demo orders do not exist at PayPal, so approving them fails at the PayPal call; a real purchase in the shop gives a live approval.
4. The same shell runs `python -m scripts.simulate_tracking` and `python -m scripts.simulate_dispute` for the live demo.

Checkout is protected by a per-order secret: creating an order returns a one-time `capture_secret` that the same browser must send in `X-Capture-Secret` to capture it. `/score/*` needs the admin token too. The plans in `render.yaml` (`starter` for the API and web, `basic-256mb` for Postgres) are the cheapest that can work; if the API is killed for memory or the web build runs out of memory, switch that service to `standard`.

### Render deploy checklist

Before you create the Blueprint:

- [ ] The code, `render.yaml` and `Dockerfile` are pushed to the GitHub branch Render will watch, and CI is green.
- [ ] You have the sandbox app's client ID and secret, a Gemini API key and the AG Studio trial key.
- [ ] **New, Blueprint** (not New, Web Service: a manual service asks for start commands the Blueprint already has). The preview must list three resources: one database, `dispute-radar-api`, `dispute-radar-web`.

Values to enter (names are exact, mind the spelling of `LICENSE`):

| Service | Variable | Value |
|---|---|---|
| API | `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | sandbox app credentials |
| API | `GEMINI_API_KEY` | your key |
| API | `PAYPAL_WEBHOOK_ID` | blank for now, filled after the webhook exists |
| API | `ADMIN_TOKEN` | generated; this is the dashboard password, copy it from the **API** service, not from your local `.env` |
| web | `NEXT_PUBLIC_PAYPAL_CLIENT_ID` | the same public client ID (never the secret) |
| web | `NEXT_PUBLIC_AG_STUDIO_LICENSE` | the AG Studio key |

After the first deploy, in this order:

- [ ] The database and both services are in the **same region**, or the private link between web and API (`API_HOSTPORT`) does not resolve.
- [ ] API logs show `Application startup complete`; `https://<api>/health` returns `{"status":"ok"}`.
- [ ] `https://<api>/api/evaluation` returns JSON (models loaded); `https://<api>/api/proposals` returns 401 (protected).
- [ ] `https://<web>/` lists products (the proxy to the API works). `NEXT_PUBLIC_*` values are baked in at build time: after changing one, use **Clear build cache and deploy**.
- [ ] Register `https://<api>/api/webhooks/paypal` in the PayPal sandbox dashboard (events `CUSTOMER.DISPUTE.*`, `PAYMENT.CAPTURE.*`), copy the webhook ID into `PAYPAL_WEBHOOK_ID`, let the API redeploy.
- [ ] `/dashboard`: paste the API's `ADMIN_TOKEN`. Run `python -m scripts.seed_demo` in the API shell; demo proposals appear within a minute.
- [ ] Buy something with a sandbox buyer, then `python -m scripts.simulate_tracking <ORDER_ID> accepted --day 1` in the shell prints a success line.

| Symptom | Likely cause | Fix |
|---|---|---|
| Web build killed, exit 137 | not enough memory for the build | set the web service plan to `standard` |
| API restarts or "out of memory" | the models and embedding model need more than the plan has | set the API plan to `standard` |
| API log: `Missing required settings` | a variable above is empty | fill it in on the API service |
| API log: SSL or connection error | `DATABASE_URL` is not the database's connection string | use the Blueprint's `fromDatabase` value; do not paste an external URL |
| Shop shows "Could not load products" | API down, or `API_HOSTPORT` missing (different region) | check API health, then region |
| Shop shows "NEXT_PUBLIC_PAYPAL_CLIENT_ID is not set" | variable empty or no rebuild | set it, clear build cache and deploy |
| Dashboard says "bad admin token" | token copied from somewhere else | copy `ADMIN_TOKEN` from the API service |
| `/studio` or `/timeline` says to unlock first | the token is kept per browser tab | unlock on `/dashboard`, then use the nav links in the same tab |
| Webhook returns 503 | `PAYPAL_WEBHOOK_ID` unset | set it |
| Webhook returns 400 | ID does not match the registered webhook, or a simulator event | register the real URL and use a real dispute |
| Approve fails with `PayPal 4xx CODE` | PayPal refused the call; the code says why | read the code in the dashboard drawer |

### CI/CD

`.github/workflows/ci.yml` runs on every push and pull request: the Python tests (`uv run pytest`) and the web checks (`npm run lint`, `npm run build`, which also type-checks). No secrets are needed. `render.yaml` sets `autoDeployTrigger: commit` on both services, so Render deploys every push to the watched branch and CI runs next to it as a separate check (watch the Actions tab; a red run means fix before presenting). Setting it to `checksPass` makes Render wait for green CI, but that did not trigger reliably for us, so it is off; `off` means manual deploys only.
