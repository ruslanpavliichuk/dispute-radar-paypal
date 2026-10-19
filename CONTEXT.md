# Dispute Radar: project context

Read this file at the start of every session. It holds the goal, the constraints, the architecture, the ML design and the task list. Section 9 is the plan of action.

## 1. What we are building

Dispute Radar predicts which PayPal orders will turn into disputes and acts before the buyer files one. When a dispute still arrives, it classifies the claim, estimates the seller's chance of winning with a calibrated model, drafts an evidence-backed response, and recommends fight, partial refund or accept by expected money kept.

Target user: UK small sellers who ship physical goods and take payment through PayPal.

Two flows:

1. **Prevention.** Every paid order gets a dispute-risk score. The score updates on each tracking event. When risk times order value crosses a cost threshold, the prevention agent proposes an action: upload tracking to PayPal, email the buyer a delivery update, or offer a goodwill refund.
2. **Response.** A `CUSTOMER.DISPUTE.CREATED` webhook starts the response agent. It classifies the claim, scores the win probability, drafts the evidence letter with Claude, and proposes the PayPal action.

The seller approves every action before anything reaches PayPal.

**What makes this project different:** trained, calibrated ML models with a visible evaluation, and a benchmark showing the trained outcome model beats an LLM asked to guess the win probability. Other entries in this hackathon already ship LLM-only dispute assistants. Our ML is the story.

## 2. Hard constraints

- **Deadline:** submissions close Nov 12, 2026, 3:00 PM EST (8:00 PM UK). Internal target: submit by Nov 11.
- **Developer time:** one 2-hour session per day, about 5 days a week, 40 hours in total. Each task fits one session, or two when marked. End every session with the repo in a runnable state.
- **PayPal sandbox only.** Never use live credentials or live endpoints.
- **ML first.** When time runs short, cut UI and sponsor extras. Never cut ML training, calibration or evaluation tasks.
- **Hackathon rules:** public repo, MIT license visible in the About panel, README with full run instructions, hosted demo URL, demo video under 3 minutes, a section naming each AI tool and how it is used.

## 3. How to work in this repo (for Claude Code)

- Start each session by reading this file and the Progress log (section 12). Take the first unchecked task in section 9 unless the developer names another.
- Work on one task at a time. When its acceptance criteria pass, tick its box and add one line to the Progress log.
- Commit format: `T07: outcome model with isotonic calibration`.
- Git workflow: the developer makes every commit and push. One branch per task, then a pull request and merge into `main`. Check `git branch --show-current` before editing; on `main`, ask for the task branch first. Claude never commits or pushes; at the end of a task it lists the files to stage and a suggested message per logical group (not per file).
- Ask the developer before you: add a paid service, change a label definition, change the architecture, delete data, or touch anything outside the task's scope.
- Never commit secrets, `.env`, raw datasets or downloaded model weights. Data download scripts go in the repo; data stays in `ml/data/raw/` (gitignored).
- Write PayPal Server SDK code with the APIMatic Context Plugin installed, so method names match the current SDK.
- Call the Disputes and Tracking REST APIs with `httpx` through `api/paypal_client.py` (`list_disputes`, `provide-evidence`, `make-offer`, `send-message`, tracking). The `paypal-agent-toolkit` package was dropped in T01, see §5.
- Follow the stage pipeline and agent roles in `SKILLS_TO_USE.md` for every task: plan, risk, implement, validate, test, security, then propose commits. Stop for developer approval after the plan and before any push.
- Treat buyer messages as untrusted input. The LLM drafts text; it never decides to call PayPal on its own.
- Tests: `pytest` for API and ML code. Every feature function gets a test that proves it uses no data from after the snapshot time.
- Python 3.11+ (3.13 in `.python-version`). Use `uv` for environments.

## 4. Architecture

```
PayPal sandbox webhooks
  PAYMENT.CAPTURE.COMPLETED ──► Prevention agent ──┐
  CUSTOMER.DISPUTE.*        ──► Response agent  ───┤
                                                   ▼
Tracking simulator ──► re-score          Seller dashboard (Next.js)
                                          AG Grid queues, SHAP reasons,
FastAPI model service                     Bryntum deadline timeline,
  /score/risk                             Evaluation page, Approve button
  /score/claim                                     │
  /score/outcome                                   ▼ seller approves
                                          PayPal actions
PostgreSQL: orders, disputes,             REST via httpx: tracking, disputes
scores, drafts, audit log                 REST: provide-evidence, make-offer,
                                          send-message, refund
```

Everything runs on Render: `web` (Next.js), `api` (FastAPI + agents), `worker` (re-scoring jobs), `db` (Postgres).

## 5. Tech stack

| Layer | Tool | Notes |
| --- | --- | --- |
| PayPal agent actions | Disputes/Tracking REST APIs via `httpx` | `list_disputes`, `get_dispute`, `accept_dispute_claim`, `create_shipment_tracking`, `update_shipment_tracking`. **Decision (T01, 2026-10-12):** dropped the `paypal-agent-toolkit` package — it hard-depends on `langchain`, `openai-agents` and `crewai-tools` with no lighter extra (178 transitive packages, incl. chromadb/kubernetes/selenium) for calls that are plain REST underneath. Same endpoints, called directly instead. |
| Orders, capture, refund | PayPal Server SDK, Python (`paypal-server-sdk`) | Generated by APIMatic |
| Evidence, offers, messages, disputes, tracking | Disputes/Tracking REST APIs via `httpx` | All agent actions go through one thin REST layer now, not a mix of toolkit + httpx |
| Coding assistant context | APIMatic Context Plugin for PayPal Server SDKs | Install in Claude Code |
| Buyer checkout | PayPal JS SDK | Demo shop |
| LLM | Claude API, structured JSON output | Drafts, buyer emails, weak labels for the claim classifier, benchmark baseline |
| ML | pandas, LightGBM, scikit-learn, SHAP, fastembed, MLflow | fastembed runs ONNX models, no PyTorch |
| Backend | FastAPI, SQLAlchemy, PostgreSQL, APScheduler | |
| Frontend | Next.js, Tailwind, AG Grid Community, AG Charts, Bryntum Scheduler | Bryntum: use the trial or sponsor licence |
| Product data | Channel3 API | Demo shop catalogue and "not as described" checks |
| Hosting | Render (`render.yaml` blueprint) | |

## 6. PayPal integration

Enable **Disputes** and **Transaction search** in the sandbox app's features.

| Purpose | Endpoint or event |
| --- | --- |
| Create and capture demo orders | `POST /v2/checkout/orders`, `POST /v2/checkout/orders/{id}/capture` |
| New paid order | Webhook `PAYMENT.CAPTURE.COMPLETED` |
| Upload or fix tracking | `POST /v2/checkout/orders/{id}/track`, `PATCH /v2/checkout/orders/{id}/trackers/{tracker_id}` (via `httpx`, see §5) |
| Goodwill refund | `POST /v2/payments/captures/{id}/refund` |
| Dispute events | Webhooks `CUSTOMER.DISPUTE.CREATED`, `.UPDATED`, `.RESOLVED` |
| Read disputes | `GET /v1/customer/disputes`, `GET /v1/customer/disputes/{id}` |
| Respond | `POST /v1/customer/disputes/{id}/provide-evidence`, `/make-offer`, `/send-message`, `/accept-claim` |
| Sandbox only | `POST /v1/customer/disputes/{id}/require-evidence`, `/adjudicate` |
| Webhook security | `POST /v1/notifications/verify-webhook-signature` |

**Creating test disputes:** the create-dispute endpoint is limited release. Log in to sandbox.paypal.com as the Personal sandbox account, open a paid order, choose Report a problem. `escalate` then moves it to a claim (`UNDER_REVIEW`, stage `CHARGEBACK`). **T03 finding:** PayPal only accepts `require-evidence` and `adjudicate` when the dispute's own `links` include them, and they were never offered on a Resolution-Center-created dispute, so do not plan on them. `accept-claim` and `make-offer` are offered on a fresh `INQUIRY` and are the way to reach `RESOLVED`.

**Pre-dispute buyer emails:** PayPal has no API for messaging a buyer before a dispute exists. Send these from the app with a free email API (Resend).

## 7. ML design

### 7.1 Where training happens

- Train on the developer's laptop. Olist has about 100k orders; LightGBM trains in minutes on a CPU.
- If embedding the claim texts takes longer than 15 minutes locally, run `ml/notebooks/embed_claims.ipynb` in a free Kaggle Notebook and download the embeddings file.
- Track every run in MLflow (`mlflow ui --backend-store-uri sqlite:///mlflow.db`, local `mlflow.db` and `mlruns/`, both gitignored; MLflow 3 refuses the plain `mlruns/` file store). Record params, metrics, plots and the data hash.
- Each training script writes its final artefacts to `ml/artifacts/<model>/`: the model file, the calibrator, the feature list and `model_card.json` (metrics, data hash, train/test date ranges, git commit).
- Artefacts are small (under 10 MB), so commit them to the repo. Judges can then run the app without retraining. `make train` must rebuild them from scratch.

### 7.2 Where models run

- The FastAPI service loads all artefacts at startup and serves `/score/risk`, `/score/claim`, `/score/outcome`.
- Use fastembed (ONNX) for embeddings, in training and at inference, so the API stays inside Render's memory limit without PyTorch.
- Render free web services sleep after 15 minutes idle. Use Render credits or the Starter plan from week 4 onward so judges don't hit a cold start.

### 7.3 Model A: dispute risk (prevention)

- **Data:** Olist Brazilian E-Commerce (Kaggle, `olistbr/brazilian-ecommerce`), joined across orders, items, products, sellers, customers and reviews.
- **Label (proxy):** an order is dispute-prone when the review score is 1 or 2 and either the order was late or the review contains a delivery or product-condition keyword. Report the base rate. The README states this proxy plainly. Details fixed with the developer on 2026-10-16: "late" compares calendar dates (delivery on the estimated date is on time), and an order never delivered counts as late when the review was answered after the estimated date. Keywords are accent-folded stems in `ml/features/keywords.py` (nao recebi, nao chegou, atras, defeit, quebrad, errad, diferente) searched in review title and message. Orders without a review are dropped, the latest review wins, and purchases after 2018-08-31 are dropped (immature orders). Multi-seller orders use the highest-priced item's seller and category.
- **Snapshot design:** score the same order at several moments. For each order, build one row at purchase and then daily rows until the estimated date plus 7 days, capped at 10 rows (evenly spaced, purchase and last day included). The grid depends only on purchase and estimated date, never on shipping or delivery (changed 2026-10-16 from "daily from shipping": rows only for shipped orders taught the model that any later row means already shipped). Each row holds only what was known on that day.
- **Features:** order value, freight as share of price, product category, number of items, estimated delivery window, same-region flag, seller's past late-delivery rate and past low-review rate (computed only from earlier orders), days since purchase, days to estimated date (negative once past), delivered yet, days late so far (calendar days), shipped yet, days since shipped.
- **Split:** by purchase date. Train before 2018-03-01, validate 2018-03-01 to 2018-05-31, test from 2018-06-01. Group all snapshots of one order into the same split. Train orders whose review or estimated date + 7 days falls on or after 2018-03-01 are purged, because their labels resolve inside the validation window.
- **Model:** LightGBM, class weights for imbalance, isotonic calibration fitted on the validation set.
- **Metrics:** PR-AUC, recall at the top 10% riskiest orders, Brier score, calibration curve, all on the test set.
- **Threshold:** act when `risk × order_value > action_cost` (email: 0, tracking upload: 0, goodwill refund: its value). Plot the precision/recall trade-off for the Evaluation page.
- **Demo mapping:** sandbox orders map to the same features (UK regions replace Brazilian states, categories map to a short list). Note the domain shift in the README.

### 7.4 Model B: claim classifier (response)

- **Classes:** `not_received`, `not_as_described`, `unauthorised`, `other`.
- **Data:** CFPB consumer complaint narratives about credit card purchase disputes, downloaded through the CFPB search API (credit card product, narratives only, about 30k rows), plus about 1,500 synthetic buyer messages written by Claude in UK marketplace style.
- **Labels:** Claude labels a random 3,000 CFPB narratives (weak labels). The developer hand-checks 200 of them; report agreement. Synthetic messages carry their generation label.
- **Model:** fastembed embeddings (`BAAI/bge-small-en-v1.5`) + logistic regression. Optional upgrade only if time allows: fine-tuned small transformer.
- **Metrics:** macro F1 and a confusion matrix on a hand-checked test set.

### 7.5 Model C: outcome predictor (response)

- **Generator (`ml/generate_disputes.py`):** sample 20,000 disputes with fields: claim type, order value (the API's `dispute_amount`), lifecycle stage (INQUIRY or CHARGEBACK), tracking uploaded, delivery confirmed, signature confirmed, shipped to the PayPal address, photos supplied, days the seller took to respond, seller account age, item category. Outcomes use the PayPal API's `dispute_outcome` codes: RESOLVED_SELLER_FAVOUR and RESOLVED_WITH_PAYOUT count as a win, RESOLVED_BUYER_FAVOUR as a loss, CANCELED_BY_BUYER has no winner and is excluded from win rates and training (developer-approved 2026-10-19; the buyer's prior dispute count was dropped because the API does not provide it). Rules modelled on PayPal Seller Protection set a win probability for each dispute. Draw each label from a Bernoulli with that probability, so the data carries real uncertainty. Keep the rules in `ml/generator_rules.yaml` and publish them.
- **Model:** LightGBM + isotonic calibration + SHAP explanations per dispute.
- **Metrics:** Brier score, log loss, ECE, calibration curve.

### 7.6 Benchmark: trained model vs LLM guess

- Take 300 held-out generated disputes. Describe each one in text, give Claude the same facts plus a summary of the Seller Protection rules, and ask for a win probability.
- Compare Brier score, log loss and calibration curves for the LLM and Model C.
- Show the result on the Evaluation page and in the README. State the caveat: the labels come from our generator rules, so the benchmark tests calibration on known structure, not real PayPal outcomes.

### 7.7 Decision layer

```
E[fight] = p × v − (1 − p) × f        E[offer] = (1 − r) × v        E[accept] = 0
```

`p` = calibrated win probability, `v` = order value, `f` = dispute fee, `r` = refund share offered. Recommend the action with the highest expected value and show the numbers to the seller.

## 8. Repo layout

```
dispute-radar-paypal/
├── CONTEXT.md
├── README.md
├── LICENSE
├── Makefile               # make setup | train | test | dev
├── render.yaml
├── docker-compose.yml
├── .env.example           # PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_WEBHOOK_ID,
│                          # ANTHROPIC_API_KEY, CHANNEL3_API_KEY, RESEND_API_KEY, DATABASE_URL
├── api/                   # FastAPI app, webhooks, PayPal clients, model service
├── agents/                # prevention.py, response.py, policies.py
├── ml/
│   ├── data/              # download scripts; raw/ is gitignored
│   ├── features/
│   ├── notebooks/
│   ├── train_risk.py
│   ├── train_claims.py
│   ├── generate_disputes.py
│   ├── generator_rules.yaml
│   ├── train_outcome.py
│   ├── benchmark_llm.py
│   └── artifacts/         # committed model files + model_card.json
├── web/                   # Next.js dashboard and demo shop
├── scripts/               # simulate_dispute.py, simulate_tracking.py, register_webhook.py
└── tests/
```

## 9. Task plan

Hours are estimates. One session is 2 hours.

### Week 1: Foundations (Oct 10 to 16, 10 h)

- [x] **T01 Scaffold and sandbox (2 h).** Repo layout, `uv` env, Makefile, `.env.example`, MIT license. Sandbox app with Disputes and Transaction search enabled. FastAPI `/api/webhooks/paypal` with signature verification. Agent Toolkit smoke test (`list_disputes` returns). APIMatic Context Plugin installed.
  *Done when:* `make dev` starts the API and a test webhook from the PayPal dashboard verifies.
- [x] **T02 Demo shop (2 h).** Next.js page with 6 to 10 products from Channel3, PayPal JS SDK checkout, server-side create and capture with the Server SDK. Orders saved to Postgres.
  *Done when:* a sandbox buyer completes a purchase and the order row appears with its capture ID.
- [ ] **T03 Sandbox dispute script (2 h).** `scripts/simulate_dispute.py` plus a written guide in the README: file a dispute as the buyer, receive the webhook, call `require-evidence`, call `adjudicate`.
  *Done when:* one dispute goes from created to resolved and each webhook is logged.
- [ ] **T04 Olist pipeline (2 h).** Download script, joins, label, snapshot builder, features, leakage tests.
  *Done when:* `ml/data/processed/risk_snapshots.parquet` exists, the base rate is printed, leakage tests pass.
- [ ] **T05 Baseline risk model (2 h).** LightGBM with default params, date split, MLflow run, PR-AUC and recall at top 10%.
  *Done when:* test metrics logged in MLflow and saved to a draft model card.

### Week 2: ML core (Oct 17 to 23, 10 h)

- [ ] **T06 Risk model final (2 h).** Small tuning sweep (num_leaves, learning rate, min_child_samples), isotonic calibration, SHAP, threshold analysis.
  *Done when:* `ml/artifacts/risk/` holds the model, calibrator and model card with test Brier, PR-AUC and the calibration plot.
- [ ] **T07 Dispute generator (2 h).** `generator_rules.yaml`, `generate_disputes.py`, 20k rows, summary stats per claim type.
  *Done when:* the generated file exists and win rates per claim type match the rules within 2 points.
- [ ] **T08 Outcome model (2 h).** LightGBM, isotonic calibration, SHAP per row.
  *Done when:* `ml/artifacts/outcome/` is written with Brier, log loss, ECE and the calibration plot.
- [ ] **T09 Claim classifier (2 h).** CFPB download, Claude weak labels on 3,000 rows, 1,500 synthetic messages, fastembed + logistic regression, hand-check 200.
  *Done when:* `ml/artifacts/claims/` is written with macro F1 and the confusion matrix.
- [ ] **T10 Model service (2 h).** `/score/risk`, `/score/claim`, `/score/outcome` in FastAPI, returning score plus top 3 SHAP reasons. Unit tests. `make train` rebuilds all artefacts.
  *Done when:* all three endpoints answer in under 300 ms locally and tests pass.

### Week 3: Agents and dashboard (Oct 24 to 30, 10 h)

- [ ] **T11 Response agent (2 h).** On `CUSTOMER.DISPUTE.CREATED`: fetch dispute and order, classify, score, check Channel3 product attributes for "not as described" claims, draft with Claude (it may cite only evidence objects the backend passes in), compute the decision layer, store a proposal. On approval: call `provide-evidence`, `make-offer`, `send-message` or `accept-claim`.
  *Done when:* a sandbox dispute gets a proposal and an approved proposal reaches PayPal.
- [ ] **T12 Prevention agent (2 h).** `scripts/simulate_tracking.py` emits carrier-style events (accepted, in transit, stuck at depot, out for delivery, delivered). The worker re-scores on each event and proposes tracking upload, buyer email or goodwill refund when the threshold is crossed. On approval: call the Agent Toolkit or refund endpoint.
  *Done when:* a "stuck at depot" event raises the score and an approved tracking upload appears on the PayPal order.
  Note (T01): "On approval: call the Agent Toolkit" is now "call the tracking/refund REST endpoints via httpx" — see §5.
- [ ] **T13 LLM benchmark (1 h).** `benchmark_llm.py` on 300 held-out disputes, writes `ml/artifacts/benchmark.json` and a calibration plot for both.
  *Done when:* the JSON and plot exist and the README has a results table.
- [ ] **T14 AG Grid dashboard and Evaluation page (3 h, two sessions).** Orders queue and disputes queue in AG Grid with risk-score cell renderers, SHAP reasons per row, an approval drawer. Evaluation page with AG Charts: calibration curves, precision/recall, LLM benchmark, fight-vs-offer chart.
  *Done when:* the seller can approve a proposal from the grid and every chart reads from the model cards.
- [ ] **T15 Bryntum timeline (2 h).** Bryntum Scheduler showing each dispute from opened to response-due date, with agent actions as events.
  *Done when:* the timeline renders live data from the API.

### Week 4: Ship (Oct 31 to Nov 6, 10 h)

- [ ] **T16 Render deploy (2 h).** `render.yaml` with web, api, worker and Postgres. Register the production webhook URL. Seed data for a first-time visitor.
  *Done when:* the hosted URL runs the full demo.
- [ ] **T17 End-to-end test (2 h).** Fresh sandbox accounts, a clean browser, the full prevention and response flows. Fix what breaks.
  *Done when:* the demo script in section 11 runs start to finish without manual fixes.
- [ ] **T18 README and docs (2 h).** Setup, run steps, sandbox setup, judge login, data assumptions, model results, tools section, screenshots, license check.
  *Done when:* someone new can clone and run the project from the README alone.
- [ ] **T19 Demo video (3 h, two sessions).** Record and edit to under 3 minutes following section 11. Upload to YouTube as public.
  *Done when:* the video link plays in a logged-out browser.
- [ ] **T20 Submit (1 h).** Devpost form: description, tools, repo link, demo URL, video link.
  *Done when:* the submission shows as complete on Devpost.

**Buffer:** Nov 7 to 11, up to 8 hours for missed sessions.

**Cut order if behind:** T15 Bryntum timeline, then the Channel3 attribute check in T11, then the goodwill refund action in T12. Never cut T04 to T10 or T13.

## 10. Prize targets

Main prizes and Best Use of PayPal + AI, Most Impactful, Most Creative and Best Use of Agentic Commerce come from the core build. Best Demo Delivery comes from T19. Sponsor prizes: AG Grid (T14), APIMatic (T01, described in README), Bryntum (T15), Channel3 (T02, T11), Render (T16). Check the Official Rules for how many prizes one project can win.

## 11. Demo script and submission checklist

Video, under 3 minutes:

1. 0:00 to 0:20. The problem: a seller loses a dispute they could have won.
2. 0:20 to 1:00. Prevention: buy in the demo shop, tracking goes "stuck at depot", risk jumps, the agent proposes a tracking upload and buyer email, the seller approves.
3. 1:00 to 2:00. Response: file a dispute, webhook fires, claim classified, win probability with SHAP reasons, draft approved, `adjudicate` shows a win.
4. 2:00 to 2:40. Evaluation page: calibration, precision/recall, the LLM benchmark.
5. 2:40 to 2:55. What this means for PayPal at scale.

Checklist:

- [ ] Public GitHub repo with all source code
- [ ] MIT license visible in the About panel
- [ ] README with setup, run steps and data assumptions
- [ ] Hosted demo on Render with a sandbox buyer login for judges
- [ ] Text description of features
- [ ] Tools section: PayPal Server SDK, PayPal Disputes/Tracking REST APIs, APIMatic Context Plugin, Claude API, LightGBM, SHAP, fastembed, AG Grid, Bryntum, Channel3, Render
- [ ] Demo video under 3 minutes, public on YouTube
- [ ] Commits spread across the hackathon period

## 12. Progress log

Add one line per finished task: date, task ID, result, anything the next session needs to know.

- 2026-10-09: Plan written. No code yet.
- 2026-10-12: T01 code complete (repo scaffold, FastAPI app, webhook signature verification, disputes REST smoke test, MIT license, Makefile, APIMatic context plugin). Architecture change: dropped `paypal-agent-toolkit` for a direct `httpx` REST layer — see §5. Caught and fixed a real bug during review: a module-level `PayPalClient` singleton was closed by the app's shutdown hook, breaking on reuse; moved it to `app.state`, owned per app instance. Live check done: simulator mock event reaches the endpoint and gets a clean 400 (PayPal can't postback-verify mock events). Real SUCCESS verification is exercised in T03.
- 2026-10-15: Pipeline test on T01 webhook intake (Reviewer + Security agents, two rounds). Hardened: required credentials at startup, cert-url netloc allowlist, raw-body verify, streamed 256 KB cap, 503 on PayPal outage so PayPal retries, 401 token refresh, docs off by default. 33 tests. Deferred to T11: replay dedupe; rate limiting at the proxy. Unverified until T03: real signed events verifying with the spliced raw body.
- 2026-10-15: T02 session 1 (backend) done: Postgres compose, orders table, catalogue, create/capture endpoints with server-side pricing and amount verification, 63 tests. Reviewer + Security agents found 4 high issues in my first cut (SDK leaves absent fields unset so attribute access raised after money moved; non-terminal failure states; capture race; uncaught SDK/network errors), all fixed. Installed paypal-server-sdk is 2.4.0 (dict options style), not the shape in the APIMatic skill text. Not yet verified: real Postgres, Channel3 snapshot (needs key), a live sandbox create/capture. Next: session 2 = web/ frontend.
- 2026-10-15: T02 done. `web/` Next.js 16 + `@paypal/react-paypal-js` v10 (V6 SDK: `PayPalProvider` + `PayPalOneTimePaymentButton`, `createOrder` returns `{orderId}`), same-origin rewrite to FastAPI. Live sandbox purchase verified end to end: COMPLETED row with capture_id/payer_id in Postgres. Channel3 snapshot has 8 products (USD prices relabelled GBP, capped at 150). Next: T03 (sandbox dispute script) which also gives the first real signed webhook to verify.
- 2026-10-15: T03 code complete on branch T03-sandbox-dispute-script (live run pending the developer): `scripts/simulate_dispute.py`, `webhook_events` table (verified events only, no body, PK dedupe), `get_dispute`, README guide, host-allowlist sandbox guard (replaces a substring check). Reviewer + Security agents found 2 real issues in my first cut (unknown dispute status treated as open; substring sandbox check). 85 tests. Verified on real Postgres: table created by init_db, duplicate insert raises IntegrityError. Still unproven: that a real signed webhook returns SUCCESS with the spliced raw body, and PayPal's exact dispute statuses and field layout; both are settled by the live run.
- 2026-10-15: T03 closed. Verified live: real PayPal-signed dispute webhooks verify with the spliced raw body (T01 risk closed); 6 events stored once each for one dispute; `escalate` moves INQUIRY to UNDER_REVIEW/CHARGEBACK. Finding: sandbox `adjudicate` and `require-evidence` are refused (no link offered) on a Resolution-Center-created dispute in every state, and the sandbox create-dispute API (limited release) has no public request body (4 attempts: MISSING_OR_INVALID_REQUEST_BODY / UNSUPPORTED_FIELDS). Decision: reach RESOLVED via `accept-claim` in T11; CONTEXT.md §11 demo wording to be rewritten with developer approval. Reviewer + Security agents caught two real defects in the first cut (unknown status treated as open; substring sandbox check). 87 tests.
- 2026-10-16: T04 done. `make data` downloads Olist (Kaggle KGAT token over httpx), builds `ml/data/processed/risk_snapshots.parquet`: 97,916 orders, 899,370 snapshots, base rate 7.61% of orders. Developer decisions: undelivered-and-past-estimate counts as late, accent-folded keyword stems incl. title, same-state flag, cut purchases at 2018-08-31, daily grid from purchase (adds shipped_yet, days_since_ship), purge train orders resolving after 2018-03-01 (7,979 of 58,305). Reviewers (MLE + Security) found no direct temporal leak; they found a grid selection effect (rows only for shipped orders), a train/val resolution overlap, weak tests (all fixed; each planted leak now fails a test) and a zip-extraction hardening (fixed). 122 tests.
- 2026-10-19: T05 done. `ml/train_risk.py` fits default LightGBM on the T04 snapshots, logs to MLflow (SQLite store: MLflow 3 refuses `mlruns/`), writes `ml/artifacts/risk_baseline/` (model.txt, draft model_card.json). Test: PR-AUC 0.299 (base rate 0.049), ROC-AUC 0.766, recall@top10% 0.46; validation 0.426 PR-AUC, train 0.568 (overfits, split drift). macOS needs `brew install libomp`. 124 tests.
- 2026-10-19: T06 done. `make train` writes `ml/artifacts/risk/` (290 KB): LightGBM (15 leaves, lr 0.03, 84 trees) + isotonic calibrator (JSON, np.interp), SHAP, threshold tables by stage, calibration plot. Test snapshot PR-AUC 0.311, Brier 0.0407 (val-to-test base-rate shift 9.4% to 4.9% makes it over-predict; documented, not corrected). Key finding: no skill at purchase time (day 0: 80% flagged at 5.3% precision); skill comes from shipping/delivery signals. Decide in T10/T12 whether to calibrate per stage and how the demo frames prevention. MLE reviewer: no leakage; fixed the decision-unit framing and four smaller issues. 135 tests.
- 2026-10-19: T07 done. `ml/generator_rules.yaml` (published, seed 7) + `ml/generate_disputes.py`: 20,000 disputes, win probability = sigmoid(base + category + evidence terms), label ~ Bernoulli. Worst per-claim-type gap 1.1 points vs rule-implied mean (limit 2); overall win rate 62%. Rebalanced claim mix (other 20%) and lowered base rates after the first draft gave 80% wins and a noisy check. Oracle `true_win_prob` kept out of features. 141 tests.
- 2026-10-19: T07 follow-up. Generator aligned to the PayPal dispute structure: added lifecycle stage, real outcome codes (win = seller favour or payout; cancelled excluded), dropped buyer_prior_disputes. Worst per-claim-type gap 0.2 points, overall win rate 60% of resolved disputes. T08 trains on resolved rows only. 143 tests.
