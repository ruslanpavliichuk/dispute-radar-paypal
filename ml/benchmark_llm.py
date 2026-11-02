"""LLM benchmark (T13): the trained outcome model against an LLM's guess, on the same held-out disputes.

    uv run python -m ml.benchmark_llm [--n 300] [--seed 0]     # needs LLM_PROVIDER + key in .env, see ml/llm.py

Rows: the outcome model's own test split (ml.train_outcome.split, never trained or tuned on), n of them
drawn with numpy default_rng(seed).choice without replacement. Both scorers see exactly those rows.
Answers are cached per provider in ml/artifacts/benchmark/llm_cache_<provider>.jsonl (gitignored), keyed
by dispute id + prompt hash, so a rerun or a crash resumes. Writes ml/artifacts/benchmark.json and
benchmark_calibration.png. Labels come from our generator rules, so this tests calibration on known
structure, not real PayPal outcomes.
"""
import argparse
import hashlib
import json
import math
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

from api.config import settings
from ml.generate_disputes import OUT as DATA
from ml.llm import DEFAULT_MODEL, complete_json
from ml.outcome_model import OutcomeModel
from ml.train_outcome import scores, split
from ml.train_risk import git_commit
from ml.train_risk_final import calibration_curve, plot_calibration

ARTIFACTS = Path(__file__).parent / "artifacts"
CLIP, BOOTSTRAPS, PAUSE = (0.01, 0.99), 1000, 1.0
CAVEAT = ("Labels come from our published generator rules (ml/generator_rules.yaml), not real PayPal outcomes: "
          "this tests calibration on known structure, not how PayPal decides. The rules summary given to the LLM was written from general "
          "public knowledge of Seller Protection, yet it still shares the direction of some effects (the 10-day deadline, stricter "
          "chargebacks) with how the labels were generated.")
NOTES = {"ece": "equal-count bins (10); heavily tied scores, such as an LLM that says 0.7 often, are merged into one bin",
         "ece_uniform": "10 equal-width bins on [0, 1], empty bins skipped, count-weighted",
         "log_loss": "every predictor is clipped to [0.01, 0.99] first, so it can differ slightly from the T08 model card (clip 1e-6)"}
# General public knowledge of PayPal Seller Protection, written independently of the generator. It must NOT
# contain the generator's coefficients, category effects or anything about how the labels were drawn.
RULES = """PayPal Seller Protection, in short:
- It can cover claims that an item was not received and claims that a payment was not authorised by the account holder. It does not cover items that are significantly not as described.
- To be covered the seller needs proof of delivery, such as online tracking showing the item was delivered, and the item must be shipped to the address on the transaction details.
- Shipping to a different address than the one PayPal shows normally voids the protection.
- For high-value orders PayPal may also require delivery with signature confirmation.
- Sellers must respond to a dispute within the deadline PayPal sets, typically 10 days. A late response usually loses.
- In not-as-described cases, photos, listing details and messages with the buyer are the evidence PayPal weighs.
- A dispute that escalates from an inquiry to a chargeback is reviewed more strictly.
- Outcomes are never certain: strong evidence can still lose and thin evidence can still win."""
SCHEMA = {"type": "object", "required": ["win_probability", "reasoning"], "properties": {
    "win_probability": {"type": "number"}, "reasoning": {"type": "string", "maxLength": 200}}}


def describe(r: dict) -> str:
    yes = lambda k: "yes" if r[k] else "no"
    stage = {"INQUIRY": "an inquiry (the first stage)", "CHARGEBACK": "a chargeback (escalated)"}.get(r["lifecycle_stage"], r["lifecycle_stage"])
    return (f"Claim type: {r['claim_type'].replace('_', ' ')}. Stage: {stage}. Order value: {r['order_value']:.2f} GBP. "
            f"Item category: {r['category']}. Tracking uploaded: {yes('tracking_uploaded')}. Delivery confirmed: {yes('delivery_confirmed')}. "
            f"Signature confirmed: {yes('signature_confirmed')}. Shipped to the PayPal transaction address: {yes('shipped_to_paypal_address')}. "
            f"Photos supplied: {yes('photos_supplied')}. The seller took {r['response_days']} days to respond. "
            f"The seller account is {r['seller_account_age_months']} months old.")


def prompt(r: dict) -> str:
    return (f"{RULES}\n\nHere is one dispute from a UK online seller.\n{describe(r)}\n\n"
            "What is the probability that the seller wins this dispute? Give a number between 0 and 1 and a short reason.")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _model_name() -> str:
    return settings.llm_model or DEFAULT_MODEL.get(settings.llm_provider, "")


def ask(rows: list[dict], cache: Path) -> tuple[dict, int]:
    """dispute_id -> raw win_probability answer, plus the number of rows the LLM failed on after retries."""
    done = {json.loads(l)["key"]: json.loads(l)["value"] for l in cache.read_text().splitlines()} if cache.exists() else {}
    cache.parent.mkdir(parents=True, exist_ok=True)
    out, failed = {}, 0
    for r in rows:
        text = prompt(r)
        key = f"{r['dispute_id']}:{_digest(text)}"
        if key not in done:
            try:
                answer = complete_json(text, SCHEMA)
            except Exception as e:  # shortcut: any failure after ml.llm's retries drops the row (counted), a rerun retries it
                print(f"{r['dispute_id']}: {type(e).__name__}", flush=True)
                failed += 1
                continue
            if not isinstance(answer, dict):  # not cached, so a rerun asks again; counts as non-numeric this run
                out[r["dispute_id"]] = None
                continue
            done[key] = answer.get("win_probability")
            with cache.open("a") as f:
                f.write(json.dumps({"key": key, "value": done[key]}) + "\n")
            time.sleep(PAUSE)
        out[r["dispute_id"]] = done[key]
    return out, failed


def ece_uniform(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    b = np.minimum((p * bins).astype(int), bins - 1)
    return float(sum((b == k).mean() * abs(y[b == k].mean() - p[b == k].mean()) for k in np.unique(b)))


def _metrics(y: np.ndarray, p: np.ndarray) -> dict:
    p = np.clip(p, *CLIP)  # same clip for every predictor, so the comparison is like for like
    return {**scores(y, p), "ece_uniform": ece_uniform(y, p), "observed_win_rate": float(y.mean())}


def _percent(v):
    """(probability or None, rescaled?): 1 < v <= 100 is read as a percentage; anything else outside [0, 1] is unusable."""
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not 0 <= v <= 100:
        return None, False
    return (v / 100, True) if v > 1 else (float(v), False)


def run(df: pd.DataFrame, n: int, seed: int, out: Path = ARTIFACTS, model: OutcomeModel | None = None) -> dict:
    test = split(df)["test"]
    test = test.iloc[np.sort(np.random.default_rng(seed).choice(len(test), min(n, len(test)), replace=False))].reset_index(drop=True)
    provider, name = settings.llm_provider, _model_name()
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    answers, failed = ask(test.to_dict("records"), out / "benchmark" / f"llm_cache_{provider}_{slug}.jsonl")

    parsed = test["dispute_id"].map(lambda i: _percent(answers.get(i)))
    ok = parsed.map(lambda t: t[0] is not None).to_numpy()
    kept = test[ok].reset_index(drop=True)
    p_raw = np.array([t[0] for t in parsed[ok]])
    p_llm = np.clip(p_raw, *CLIP)  # log loss needs probabilities away from 0 and 1

    y = kept["won"].astype(int).to_numpy()
    p_model = np.clip((model or OutcomeModel.load()).score(kept), *CLIP)
    train_rate = float(split(df)["train"]["won"].astype(int).mean())
    preds = {"llm": p_llm, "outcome_model": p_model, "constant (train win rate)": np.full(len(y), train_rate),
             "oracle (true_win_prob)": np.clip(kept["true_win_prob"].to_numpy(), *CLIP)}

    d = (p_llm - y) ** 2 - (p_model - y) ** 2  # per-row Brier gap, LLM minus model
    idx = np.random.default_rng(seed).integers(0, len(d), (BOOTSTRAPS, len(d)))
    lo, hi = np.percentile(d[idx].mean(axis=1), [2.5, 97.5])
    run_result = {
        "provider": provider, "model": name, "n_scored": len(kept), "n_failed": failed,
        "n_non_numeric": len(answers) - len(kept), "n_rescaled_percent": int(sum(t[1] for t in parsed[ok])),
        "n_out_of_range_clipped": int(((p_raw < CLIP[0]) | (p_raw > CLIP[1])).sum()),
        "metrics": {k: _metrics(y, p) for k, p in preds.items()},
        "calibration": {k: calibration_curve(y, preds[k]) for k in ("llm", "outcome_model")},
        "brier_diff_llm_minus_model": {"mean": float(d.mean()), "ci95": [float(lo), float(hi)], "bootstraps": BOOTSTRAPS},
    }
    data_hash = _digest(pd.util.hash_pandas_object(test, index=False).values.tobytes().hex())
    path = out / "benchmark.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    runs = old["runs"] if old.get("data_hash") == data_hash and old.get("seed") == seed else {}  # other rows: old runs are not comparable
    runs[f"{provider}:{name}"] = run_result
    doc = {"n": len(test), "seed": seed, "git_commit": git_commit(), "data_hash": data_hash, "rules_hash": _digest(RULES),
           "clip": list(CLIP), "notes": NOTES, "caveat": CAVEAT, "runs": runs}
    path.write_text(json.dumps(doc, indent=2) + "\n")
    # shortcut: the trained-model curve is drawn from the first run; it differs between runs only if they scored different rows
    first = next(iter(runs.values()))
    plot_calibration({"outcome model": first["calibration"]["outcome_model"], **{f"LLM {k}": r["calibration"]["llm"] for k, r in runs.items()}},
                     out / "benchmark_calibration.png", title="Calibration on generator labels,\nnot real PayPal outcomes", lim=1.0,
                     ylabel="observed seller win rate", counts=True)
    return doc


def table(doc: dict) -> str:
    row = lambda k, m: f"| {k} | {m['brier']:.4f} | {m['log_loss']:.4f} | {m['ece']:.4f} | {m['ece_uniform']:.4f} | {m['mean_predicted']:.3f} | {m['observed_win_rate']:.3f} |"
    runs = doc["runs"]
    first_key, first = next(iter(runs.items()))
    lines = [f"LLM benchmark on {doc['n']} held-out disputes (scored: " + ", ".join(f"{k} {r['n_scored']}" for k, r in runs.items()) + ")",
             "", "| Predictor | Brier | Log loss | ECE (equal-count) | ECE (uniform) | Mean predicted | Observed |", "|---|---|---|---|---|---|---|"]
    lines += [row(f"LLM {k}", r["metrics"]["llm"]) for k, r in runs.items()]
    lines += [row(f"{k} (rows of {first_key})", m) for k, m in first["metrics"].items() if k != "llm"]
    lines.append("")
    for k, r in runs.items():
        b = r["brier_diff_llm_minus_model"]
        lines.append(f"{k}: Brier, LLM minus model {b['mean']:+.4f} (95% CI {b['ci95'][0]:+.4f} to {b['ci95'][1]:+.4f})")
    return "\n".join(lines + ["", doc["caveat"]])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    print(table(run(pd.read_parquet(DATA), a.n, a.seed)))
