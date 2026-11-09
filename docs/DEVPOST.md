# Devpost submission text

## Tagline
Stop PayPal disputes before they happen, and answer the ones that still arrive, with a human approving every step.

## Description
Dispute Radar is an assistant for PayPal sellers. After an order ships, a calibrated LightGBM model re-scores its chance of turning into a dispute each time tracking changes. When the score crosses a bar, an agent proposes a tracking upload and, if the parcel is past its delivery estimate, a goodwill refund. When a buyer files a dispute anyway, PayPal's webhook reaches the app, an outcome model estimates the win chance with SHAP reasons, and the agent picks the best expected value of fighting, offering a partial refund or accepting. It drafts a plain message from facts in our own database.

The seller stays in control. Nothing is sent to PayPal until they click approve; every approval re-reads PayPal, makes one call with an idempotency key, and refuses a second refund on the same order. The LLM only writes draft text. It never decides and never calls PayPal, and buyer messages never reach it.

The seller dashboard (AG Grid), a dispute timeline (Bryntum Scheduler), an evaluation page (AG Charts: calibration, precision and recall, a model-vs-LLM benchmark) and AG Studio (self-service charts and a small "Radar" assistant) make the models inspectable.

## Honest limits
The risk model is trained on Brazilian Olist orders and applied to UK sandbox orders. PayPal does not publish dispute outcomes, so win probabilities come from our own published generator rules and measure calibration on known structure. Shipping delays are simulated by a script; disputes are filed by hand in the sandbox. Sandbox only.

## Built with
PayPal Server SDK, PayPal Disputes / Payments / Tracking REST APIs, APIMatic Context Plugin, Gemini (configurable LLM), LightGBM, SHAP, fastembed, FastAPI, Postgres, Next.js, AG Grid, AG Charts, AG Studio, Bryntum Scheduler, Channel3, GitHub Actions, Render.

## Links
- Repo: https://github.com/ruslanpavliichuk/dispute-radar-paypal
- Demo: https://dispute-radar-web.onrender.com
- Video: <paste the YouTube link>

## Testing instructions (private field, do NOT commit real values)
1. Open the demo URL. Seller pages are in the top bar: Dashboard, Timeline, Evaluation, Studio.
2. Dashboard: unlock with the admin token: `<ADMIN_TOKEN>`.
3. Buy something in the shop (home page) at the PayPal sandbox with this personal account: `<SANDBOX_BUYER_EMAIL>` / `<SANDBOX_BUYER_PASSWORD>`. Sandbox only, no real money.
4. Shipping delays and disputes are simulated with scripts, so the demo video shows that part; the dashboard, timeline, evaluation and Studio can all be explored live.
