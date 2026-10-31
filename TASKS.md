# Tasks

Live, frequently-updated tracker. CONTEXT.md §9 is the spec for each task (scope, acceptance criteria, cut order); this file is the checklist view of where we actually are. Update it as tasks move, don't let it drift from CONTEXT.md §12 (Progress log).

Legend: `[ ]` not started · `[~]` in progress · `[x]` done

## Setup (pre-Week 1)

- [x] Dev tooling: installed `ecc`, `ponytail`, `pstack` Claude Code skills/plugins
- [x] Gitignore pstack's project-local files and `.env`

## Week 1 — Foundations (Oct 10–16, 10h)

- [x] **T01** Scaffold and sandbox (2h)
  - [x] Repo layout, `uv` env, Makefile, `.env.example`, MIT license
  - [x] Sandbox app: Disputes + Transaction search features enabled
  - [x] FastAPI `/api/webhooks/paypal` with signature verification (code done, tests pass)
  - [x] Disputes REST smoke test (`list_disputes` equivalent; see §5 decision — no longer the Agent Toolkit)
  - [x] APIMatic Context Plugin installed
  - [x] Real sandbox credentials in `.env`, tunnel + webhook registered, live smoke test passes
  - [x] Simulator mock event reaches `/api/webhooks/paypal` and is rejected cleanly (400); PayPal can't postback-verify mock events
  - Done when: `make dev` starts the API and a test webhook from the PayPal dashboard verifies
  - Note: a real `SUCCESS` verification needs a real signed event; it is exercised in T03 (real dispute)
- [x] **T02** Demo shop (2h, two sessions)
  - [x] Backend: Postgres via `docker-compose.yml`, `orders` table, Channel3 catalogue snapshot (8 products, price cap), `GET /api/shop/products`, `POST /api/shop/orders`, `POST /api/shop/orders/{id}/capture` with server-side pricing, amount/currency verification and request-id idempotency, 63 tests
  - [x] Backend reviewed by Reviewer + Security agents; findings fixed
  - [x] Frontend: `web/` Next.js 16 page, `@paypal/react-paypal-js` v10 (PayPal V6 SDK) button per product, `/api/shop/*` same-origin rewrite
  - [x] Verified live: sandbox buyer purchase, green notice, `COMPLETED` row with `capture_id`, `payer_id` and timestamp in Postgres
  - Done when: a sandbox buyer completes a purchase and the order row appears with its capture ID
- [x] **T03** Sandbox dispute script (2h)
  - [x] `scripts/simulate_dispute.py` (`list` / `show` / `escalate` / `evidence` / `adjudicate`; refuses disputes not in our `orders`, fails closed on unknown statuses, sandbox host allowlist)
  - [x] `webhook_events` table: verified events recorded (id, type, resource id, time; no body), redelivery is a no-op, DB error returns 503
  - [x] README guide: tunnel, webhook registration, filing the dispute, running the script
  - [x] Reviewer + Security agents ran on the diff; findings fixed; 87 tests
  - [x] **Verified live:** first real signed webhooks verify (raw-body splice works); 6 events for one dispute (CREATED + 5 UPDATED) stored once each; escalate took the dispute INQUIRY → UNDER_REVIEW → CHARGEBACK claim
  - [ ] Not reachable (closed on the developer's decision): `adjudicate` / `require-evidence` were refused in every state (PayPal never offered the links on a Resolution-Center-created dispute) and the sandbox create-dispute body is undocumented. **RESOLVED is demonstrated through `accept-claim` in T11** (the link was available on a fresh INQUIRY).
  - Done when: one dispute goes created → resolved, each webhook logged (created → updated proven; resolved moves to T11 via accept-claim)
- [x] **T04** Olist pipeline (2h)
  - [x] Download script, joins across orders/items/products/sellers/customers/reviews
  - [x] Label (proxy) + base rate printed (7.61% of orders; snapshot level 7.16%)
  - [x] Snapshot builder (daily from purchase to est. date + 7, cap 10 rows; grid independent of shipping)
  - [x] Features (adds shipped_yet, days_since_ship), leakage tests (mutation-checked)
  - [x] Done when: `ml/data/processed/risk_snapshots.parquet` exists, base rate printed, leakage tests pass
  - Findings for T05/T06: base rate drifts by split (train 6.9%, val 9.7%, test 4.9% of snapshots), so expect test PR-AUC to differ from validation; `days_late_so_far` tops out at 7; `seller_late_rate` ignores never-delivered orders (a seller whose orders never arrive looks clean); keyword stems add some label noise ("errad" matches "enterrada"); val labels resolve partly inside the test window (only train is purged).
- [x] **T05** Baseline risk model (2h)
  - [x] LightGBM default params, date split (`ml/train_risk.py`, `make train`)
  - [x] MLflow run (experiment `risk`, SQLite store), PR-AUC, recall@top-10%
  - [x] Done when: test metrics logged in MLflow and saved to a draft model card (`ml/artifacts/risk_baseline/model_card.json`)
  - Baseline (snapshot level, test): PR-AUC 0.299 (base rate 0.049), ROC-AUC 0.766, recall@top10% 0.460, Brier 0.040. Order level (max snapshot score): PR-AUC 0.404, recall@top10% 0.580. Validation PR-AUC 0.426, train 0.568, so the default model overfits and drifts; T06 tunes it and calibrates.
  - For T06: `delivered_yet` carries 27.5% of gain, then `days_to_est`, `days_late_so_far`; the proxy label is partly defined by lateness, so this is expected. Raw probabilities are uncalibrated (default LightGBM, no class weights).

## Week 2 — ML core (Oct 17–23, 10h)

- [x] **T06** Risk model final (2h)
  - [x] 12-candidate sweep (leaves, learning rate, min_child_samples, class weight), picked on val half A by order-level PR-AUC; isotonic on val half B; test scored once
  - [x] SHAP (LightGBM `pred_contrib`), threshold analysis by stage, calibration plot (`ml/train_risk_final.py`, `ml/risk_model.py`)
  - [x] Done when: `ml/artifacts/risk/` holds model, calibrator, model card with test Brier, PR-AUC and the calibration plot
  - Test: snapshot PR-AUC 0.311 (baseline 0.299), ROC-AUC 0.770, recall@top10% 0.465, Brier 0.0407 (constant guess 0.0487, uncalibrated baseline 0.0403), ECE 0.026. Order level (best snapshot, retrospective): PR-AUC 0.449, recall@top10% 0.584. The sweep barely mattered (valA 0.635 to 0.656).
  - **Finding: the model has no skill at purchase time.** At day 0, flagging at risk >= 10% flags 80% of orders at 5.3% precision (base rate 4.9%), and the calibrator over-predicts there (mean 0.118 vs 0.049). Skill appears once shipping and delivery signals exist (day 8 onwards: 12% flagged, 22% precision, 57% recall). `delivered_yet` carries about 86% of mean |SHAP|. The Evaluation page must use `threshold_table.json` `by_stage`; `retrospective_any_time` is an upper bound.
  - Goodwill refund (10% of order value) reduces to risk > 10%: flags most orders at day 0 at low precision. Show as a finding.
  - Open for the developer (T10/T12): per-stage calibration, and whether the demo claims prevention at checkout or from first shipping onwards.
  - Reviewer findings fixed: decision-unit framing, `deterministic=True`, pure `select_winner` with tests, tie-aware ECE bins, `diag_` prefix on the test-label diagnostic. Not fixed: unseen categories are scored silently (log in T10).

- [x] **T07** Dispute generator (2h)
  - [x] `ml/generator_rules.yaml` (published rules), `ml/generate_disputes.py`, 20k rows, `make disputes`
  - [x] Summary stats per claim type (expected vs observed win rate, gap in points)
  - [x] Done when: generated file exists, win rates per claim type match rules within 2 points (worst gap 1.1 points; the check fails the run otherwise)
  - Mix: not_received 38%, not_as_described 28%, unauthorised 15%, other 20% (other raised from 10%: at 10% its sampling noise alone (about 1.1 points) failed the 2-point check on some seeds; now about 1.5% of 200 seeds fail). Overall win rate 62%, claim types 42% to 75%; 13.5% of disputes are weak (p < 0.35), 31% strong (p > 0.8). Base log-odds were lowered by 1.0 from a first draft (80% overall win rate would make "fight" dominate the decision layer).
  - `true_win_prob` is the oracle probability for scoring models in T08/T13; never a feature (`FEATURES` in `generate_disputes.py` excludes it).
  - **Follow-up (aligned to the PayPal dispute structure, developer-approved):** added `lifecycle_stage` (INQUIRY 65% / CHARGEBACK 35%, chargebacks harder: win rate 52% vs 65%) and `outcome_code` (RESOLVED_SELLER_FAVOUR, RESOLVED_WITH_PAYOUT, RESOLVED_BUYER_FAVOUR, CANCELED_BY_BUYER 6%); `won` is `<NA>` when cancelled, so T08 must train on `won.notna()`. Dropped `buyer_prior_disputes` (not in the API). `API_REASON_TO_CLAIM_TYPE` is in `ml/generate_disputes.py`; verify its values against live API responses in T10. Worst gap now 0.2 points; the 2-point check trips on about 5% of arbitrary seeds (sampling noise), the error message says so.
  - Caveat to repeat in README/Evaluation page: the weights are our judgement, not PayPal's numbers.

- [x] **T08** Outcome model (2h)
  - [x] LightGBM, isotonic calibration, SHAP per row (`ml/train_outcome.py`, `ml/outcome_model.py`, added to `make train`)
  - [x] Done when: `ml/artifacts/outcome/` written with Brier, log loss, ECE, calibration plot
  - Test (resolved disputes, random 60/10/15/15 split): Brier 0.1827, log loss 0.5427, ECE 0.0165, ROC-AUC 0.783. The true probabilities score Brier 0.1807, log loss 0.5342, ROC-AUC 0.787 (the ceiling); a constant guess scores Brier 0.2368. Mean |p - true_win_prob| is 0.043. Isotonic improved ECE (0.0201 raw) but slightly worsened log loss (0.5384 raw); kept for consistency with the risk model.
  - No tuning sweep: the data is iid and synthetic and the model is within 0.002 Brier of the ceiling. Top SHAP: claim_type, delivery_confirmed, lifecycle_stage, photos_supplied.
  - Scores are against the generator's own rules: this shows the model recovers known structure, not PayPal's real decisions (stated in the model card).
- [x] **T09** Claim classifier (2h)
  - [x] CFPB narratives (`ml/data/download_cfpb.py`: the CFPB API and bulk CSV no longer carry narrative text, so it reads the CC0 Hugging Face mirror; 28,898 card-dispute rows), `make claims`
  - [x] Weak labels on 3,000 rows and 1,497 synthetic buyer messages from **Gemini** (`ml/label_claims.py`, `ml/llm.py` with `LLM_PROVIDER=claude` as the switch; CONTEXT.md still says Claude, needs the developer-approved rewording)
  - [x] fastembed `BAAI/bge-small-en-v1.5` + logistic regression (C=10 by CV), JSON weights, `make train`
  - [x] Done when: `ml/artifacts/claims/` has macro F1 and the confusion matrix. Test macro F1: CFPB (vs Gemini labels, 591 rows) 0.717, synthetic (283 rows) 0.952. Weak-label mix: unauthorised 1,277, other 1,156, not_as_described 334, not_received 233. Weakest classes: not_received 0.61 and not_as_described 0.60 F1.
  - [x] Blind second-LLM labels for the 200 hand-check rows (`ml/artifacts/claims/reviewer_labels.csv`): agrees with the weak label on 171/200 (85.5%), disagrees on 29.
  - [x] Adjudicated 29 rows (`adjudicate.csv`, labelled by the developer from `adjudicate.md`; the developer agreed with the blind LLM on 12 of 29). Hand-checked truth = 29 human labels + 171 rows where both LLMs agreed.
  - Hand-checked results (200 rows): Gemini weak labels agree with the truth on 94.0% (Cohen's kappa 0.907); the model scores macro F1 0.738 (not_received 0.71, not_as_described 0.53, unauthorised 0.92, other 0.79). not_as_described is the weak class. This can overstate accuracy: 171 of the 200 truths are two LLMs agreeing, and shared mistakes are not caught.
  - Caveat: CFPB is US card complaints, the product sees UK marketplace messages; the synthetic score is on easy LLM-written text.
- [x] **T10** Model service (2h)
  - [x] `POST /score/risk`, `/score/outcome`, `/score/claim` (`api/scoring.py`), models loaded once in the lifespan; each returns the score plus the top 3 reasons. Risk and outcome reasons are exact TreeSHAP in raw log-odds; claim reasons are occlusion (drop each word group, measure the fall in the predicted class's probability), because a logistic regression on embeddings has no per-word weights. Claim probabilities are uncalibrated.
  - [x] Strict request models (`extra="forbid"`, bounds, enums, 5,000-char message cap); unseen categories are scored as missing and logged (T06 carry-over). 11 new tests; `make train` rebuilds all three models.
  - [x] Done when: all three answer well under 300 ms locally (median 4 ms risk, 6 ms outcome, 25 ms claim) and tests pass (159).
  - Open for T11: the endpoints take model features, not PayPal dispute JSON. The agent has to map a dispute to `OutcomeRequest` (and `API_REASON_TO_CLAIM_TYPE` is still unverified against live API responses). No auth on `/score/*`: put it behind the proxy before T16.

## Week 3 — Agents and dashboard (Oct 24–30, 10h)

- [ ] **T11** Response agent (2h)
  - [ ] On `CUSTOMER.DISPUTE.CREATED`: fetch dispute + order, classify, score
  - [ ] Channel3 attribute check for "not as described" claims
  - [ ] Claude draft (cites only backend-passed evidence), decision layer, store proposal
  - [ ] On approval: `provide-evidence` / `make-offer` / `send-message` / `accept-claim`
  - [ ] Add `accept-claim` (and `make-offer`) to `scripts/simulate_dispute.py`; accepting a fresh INQUIRY dispute should produce the first `CUSTOMER.DISPUTE.RESOLVED` webhook (buyer favour). Settle via `adjudicate` is not available (T03 finding). **CONTEXT.md §11 demo step "adjudicate shows a win" needs developer-approved rewording** (e.g. accept / offer).
  - [ ] Webhook replay protection: event-id dedupe exists (T03 `webhook_events`); still to add: `paypal-transmission-time` skew check
  - [ ] Acting on events: add a nullable `processed_at` to `webhook_events` and treat a duplicate as done **only when it is set**, otherwise a crash after the insert makes PayPal's retry a skipped duplicate (T03 review). Note `resource_id` is a dispute id only for `CUSTOMER.DISPUTE.*`; key on event type.
  - [ ] Rate-limit the public webhook and `POST /api/shop/orders` at the proxy; purge stale `CREATED` orders (deferred from T01/T02 reviews)
  - Done when: a sandbox dispute gets a proposal and an approved proposal reaches PayPal
- [ ] **T12** Prevention agent (2h)
  - [ ] Reconcile `PENDING` orders (webhook `PAYMENT.CAPTURE.COMPLETED` or `get_order`) and alert on `MISMATCH`/`UNKNOWN` orders; refunds (deferred from T02 review)
  - [ ] `scripts/simulate_tracking.py` (accepted, in transit, stuck at depot, out for delivery, delivered)
  - [ ] Worker re-scores on each event, proposes tracking upload / buyer email / goodwill refund at threshold
  - [ ] On approval: tracking or refund REST call via `httpx`
  - Done when: a "stuck at depot" event raises score and an approved tracking upload appears on the PayPal order
- [ ] **T13** LLM benchmark (1h)
  - [ ] `benchmark_llm.py` on 300 held-out disputes
  - [ ] Writes `ml/artifacts/benchmark.json` + calibration plot for both
  - Done when: JSON + plot exist, README has results table
- [ ] **T14** AG Grid dashboard + Evaluation page (3h, two sessions)
  - [ ] Orders queue + disputes queue in AG Grid, risk-score cell renderers, SHAP reasons, approval drawer
  - [ ] Evaluation page: AG Charts calibration curves, precision/recall, LLM benchmark, fight-vs-offer chart
  - Done when: seller can approve a proposal from the grid, every chart reads from model cards
- [ ] **T15** Bryntum timeline (2h)
  - [ ] Bryntum Scheduler: dispute opened → response-due date, agent actions as events
  - Done when: timeline renders live data from the API

## Week 4 — Ship (Oct 31–Nov 6, 10h)

- [ ] **T16** Render deploy (2h)
  - [ ] Before real users: per-order secret returned by create and required on capture, `sslmode=require` on the managed DB, no permissive CORS (deferred from T02 review)
  - [ ] `render.yaml`: web, api, worker, Postgres
  - [ ] Register production webhook URL, seed data for first-time visitor
  - Done when: hosted URL runs the full demo
- [ ] **T17** End-to-end test (2h)
  - [ ] Fresh sandbox accounts, clean browser, full prevention + response flows
  - [ ] Fix what breaks
  - Done when: demo script (CONTEXT.md §11) runs start to finish without manual fixes
- [ ] **T18** README and docs (2h)
  - [ ] Setup, run steps, sandbox setup, judge login, data assumptions, model results, tools section, screenshots, license check
  - Done when: someone new can clone and run the project from the README alone
- [ ] **T19** Demo video (3h, two sessions)
  - [ ] Record + edit to <3 min per CONTEXT.md §11
  - [ ] Upload to YouTube, public
  - Done when: video link plays in a logged-out browser
- [ ] **T20** Submit (1h)
  - [ ] Devpost form: description, tools, repo link, demo URL, video link
  - Done when: submission shows complete on Devpost

## Buffer

- [ ] Nov 7–11: up to 8h for missed sessions
- Cut order if behind: T15 → Channel3 check in T11 → goodwill refund in T12. Never cut T04–T10 or T13.
