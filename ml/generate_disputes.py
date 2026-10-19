"""Synthetic disputes with known win-probability rules (T07, CONTEXT.md 7.5).

    uv run python -m ml.generate_disputes

Reads ml/generator_rules.yaml, writes ml/data/processed/disputes.parquet and prints a summary
per claim type. `true_win_prob` is the rule-implied probability: it exists to check the
generator and to score models against the truth, and must never be a model feature.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

HERE = Path(__file__).parent
RULES = HERE / "generator_rules.yaml"
OUT = HERE / "data" / "processed" / "disputes.parquet"
TOLERANCE = 0.02  # empirical win rate must be within 2 points of the rule-implied mean

# model features (everything except ids, the oracle probability and the label)
FEATURES = [
    "claim_type", "order_value", "tracking_uploaded", "delivery_confirmed", "signature_confirmed",
    "shipped_to_paypal_address", "photos_supplied", "response_days", "seller_account_age_months",
    "buyer_prior_disputes", "category",
]


def load_rules(path: Path = RULES) -> dict:
    return yaml.safe_load(path.read_text())


def _lognormal(rng, n, spec) -> np.ndarray:
    return np.clip(rng.lognormal(np.log(spec["median"]), spec["sigma"], n), spec["min"], spec["max"])


def draw_fields(rules: dict, n: int, rng: np.random.Generator) -> pd.DataFrame:
    f = rules["fields"]
    claims, cats = rules["claim_types"], rules["categories"]
    value = np.round(_lognormal(rng, n, f["order_value"]), 2)
    tracking = rng.random(n) < f["tracking_uploaded"]
    delivered = tracking & (rng.random(n) < f["delivery_confirmed_given_tracking"])
    sign_p = np.where(value >= f["high_value_threshold"], f["signature_confirmed_given_delivery_high_value"],
                      f["signature_confirmed_given_delivery"])
    r = f["response_days"]
    return pd.DataFrame({
        "claim_type": rng.choice(list(claims), n, p=[c["share"] for c in claims.values()]),
        "order_value": value,
        "tracking_uploaded": tracking,
        "delivery_confirmed": delivered,
        "signature_confirmed": delivered & (rng.random(n) < sign_p),  # needs a delivery to sign for
        "shipped_to_paypal_address": rng.random(n) < f["shipped_to_paypal_address"],
        "photos_supplied": rng.random(n) < f["photos_supplied"],
        "response_days": np.minimum(np.round(rng.gamma(r["gamma_shape"], r["gamma_scale"], n)), r["max"]).astype(int),
        "seller_account_age_months": np.round(_lognormal(rng, n, f["seller_account_age_months"])).astype(int),
        "buyer_prior_disputes": rng.poisson(f["buyer_prior_disputes_poisson_mean"], n),
        "category": rng.choice(list(cats), n, p=[c["share"] for c in cats.values()]),
    })


def win_probability(df: pd.DataFrame, rules: dict) -> np.ndarray:
    """Rule-implied probability that the seller wins each dispute."""
    logit = df["category"].map({k: v["logit"] for k, v in rules["categories"].items()}).to_numpy(dtype=float)
    for claim, spec in rules["claim_types"].items():
        rows = (df["claim_type"] == claim).to_numpy()
        logit = logit + rows * spec["base_logit"]
        for t in spec["terms"]:
            x = df[t["feature"]].to_numpy()
            if t["kind"] == "flag":
                term = x.astype(bool)
            elif t["kind"] == "above":
                term = x > t["value"]
            elif t["kind"] == "per_unit":
                term = np.minimum(x, t["cap"])
            else:
                raise ValueError(f"unknown rule kind {t['kind']!r}")
            if "when_flag" in t:
                term = term * df[t["when_flag"]].to_numpy().astype(bool)
            logit = logit + rows * t["weight"] * term
    return 1 / (1 + np.exp(-logit))


def generate(rules: dict, n: int | None = None, seed: int | None = None) -> pd.DataFrame:
    n = n or rules["n_rows"]
    rng = np.random.default_rng(rules["seed"] if seed is None else seed)
    df = draw_fields(rules, n, rng)
    df["true_win_prob"] = win_probability(df, rules)
    df["won"] = (rng.random(n) < df["true_win_prob"]).astype(int)
    df.insert(0, "dispute_id", [f"SYN-{i:06d}" for i in range(n)])
    return df


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("claim_type").agg(n=("won", "size"), expected_win_rate=("true_win_prob", "mean"), win_rate=("won", "mean"))
    g["share"] = g["n"] / len(df)
    g["gap_points"] = (g["win_rate"] - g["expected_win_rate"]) * 100
    return g


def main() -> None:
    rules = load_rules()
    df = generate(rules)
    summary = summarise(df)
    print(summary.round(4).to_string())
    print(f"overall win rate {df['won'].mean():.3f}, rows {len(df):,}")
    worst = summary["gap_points"].abs().max()
    if worst > TOLERANCE * 100:
        raise SystemExit(f"win rate off the rules by {worst:.2f} points (limit {TOLERANCE * 100:.0f})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
