"""Final dispute-risk model (T06): tuning sweep, isotonic calibration, SHAP, threshold analysis.

    uv run python -m ml.train_risk_final

Scheme (no row is used twice for conflicting jobs):
  train        fits every candidate
  val half A   early stopping and picking the winner (by order-level PR-AUC)
  val half B   fits the isotonic calibrator (halves split by order, deterministically)
  test         scored once, at the end
Writes ml/artifacts/risk/ and logs a parent MLflow run with one nested run per candidate.
"""
import hashlib
import itertools
import json
import math
import random
from pathlib import Path

import lightgbm as lgb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import mlflow  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.isotonic import IsotonicRegression  # noqa: E402
from sklearn.metrics import average_precision_score  # noqa: E402

from ml.features.risk import FEATURES  # noqa: E402
from ml.risk_model import ARTIFACTS, Calibrator, RiskModel, decide  # noqa: E402
from ml.train_risk import DATA, ROOT, git_commit, metrics, prepare  # noqa: E402

N_CONFIGS = 12
SIMPLER_IF_WITHIN = 0.01  # prefer fewer leaves when PR-AUC is this close to the best
GOODWILL_SHARE = 0.10  # a goodwill refund costs this share of the order value (developer-approved)
THRESHOLDS = [round(t, 2) for t in np.arange(0.01, 0.91, 0.01)]
GRID = {
    "num_leaves": [15, 31, 63],
    "learning_rate": [0.03, 0.05, 0.1],
    "min_child_samples": [50, 200, 1000],
    "weight": ["none", "sqrt", "full"],
}


def split_val(val: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deterministic split of validation by order: an order never lands in both halves."""
    half = pd.util.hash_pandas_object(val["order_id"], index=False).to_numpy() % 2
    return val[half == 0].reset_index(drop=True), val[half == 1].reset_index(drop=True)


def per_order(df: pd.DataFrame, p: np.ndarray) -> pd.DataFrame:
    """One row per order: its label, value and highest-risk snapshot."""
    return (
        df.assign(p=p)
        .groupby("order_id")
        .agg(label=("label", "first"), value=("order_value", "first"), p=("p", "max"))
    )


def order_pr_auc(df: pd.DataFrame, score: np.ndarray) -> float:
    o = per_order(df, score)
    return float(average_precision_score(o["label"], o["p"]))


def _bins(p: np.ndarray, bins: int) -> list[np.ndarray]:
    """Row indices per equal-count bin; tied scores (isotonic plateaus) always share a bin."""
    codes = pd.qcut(p, bins, labels=False, duplicates="drop")
    if np.isnan(codes).any():  # constant scores: every edge collapsed
        return [np.arange(len(p))]
    return [np.flatnonzero(codes == c) for c in np.unique(codes)]


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """Expected calibration error over equal-count bins."""
    return float(sum(len(b) / len(y) * abs(y[b].mean() - p[b].mean()) for b in _bins(p, bins)))


def calibration_curve(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list[dict]:
    return [
        {"mean_pred": float(p[b].mean()), "obs_rate": float(y[b].mean()), "n": int(len(b))}
        for b in _bins(p, bins)
    ]


def select_winner(results: list[dict]) -> dict:
    """Best validation-A order PR-AUC; among candidates within SIMPLER_IF_WITHIN of it, the
    simplest (fewest leaves, then largest min_child_samples). Uses no test information."""
    best = max(r["valA_order_pr_auc"] for r in results)
    near = [r for r in results if r["valA_order_pr_auc"] >= best - SIMPLER_IF_WITHIN]
    return min(near, key=lambda r: (r["num_leaves"], -r["min_child_samples"]))


STAGES = {
    "purchase (day 0, what a seller sees at checkout)": lambda d: d["days_since_purchase"] < 1,
    "days 1 to 7": lambda d: (d["days_since_purchase"] >= 1) & (d["days_since_purchase"] < 8),
    "day 8 onwards": lambda d: d["days_since_purchase"] >= 8,
}


def threshold_table(rows: pd.DataFrame, unit: str) -> dict:
    y, p, v = rows["label"].to_numpy(), rows["p"].to_numpy(), rows["value"].to_numpy()

    def row(flag: np.ndarray) -> dict:
        n = int(flag.sum())
        return {
            "n_flagged": n,
            "flagged_share": float(n / len(y)),
            "precision": float(y[flag].mean()) if n else None,
            "recall": float(y[flag].sum() / y.sum()),
            "dispute_value_recall": float((v * y)[flag].sum() / (v * y).sum()),
        }

    goodwill = np.array([decide(pi, vi, GOODWILL_SHARE * vi) for pi, vi in zip(p, v)])
    return {
        "unit": unit,
        "rows": int(len(y)),
        "base_rate": float(y.mean()),
        "by_threshold": [{"threshold": t, **row(p >= t)} for t in THRESHOLDS],
        "goodwill_refund": {"cost_share_of_value": GOODWILL_SHARE, **row(goodwill)},
    }


def prior_shift_brier(y: np.ndarray, p: np.ndarray, from_rate: float) -> float:
    """Diagnostic only: Brier after rescaling to the test base rate (uses test labels)."""
    k = (y.mean() / (1 - y.mean())) / (from_rate / (1 - from_rate))
    odds = p / (1 - p).clip(1e-12) * k
    return float(np.mean((odds / (1 + odds) - y) ** 2))


def plot_calibration(curves: dict[str, list[dict]], path: Path, title: str = "Risk model calibration",
                     lim: float = 0.6, ylabel: str = "observed dispute rate") -> None:
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, lim], [0, lim], "k--", lw=1, label="perfect")
    for name, c in curves.items():
        ax.plot([b["mean_pred"] for b in c], [b["obs_rate"] for b in c], "o-", label=name)
    ax.set(xlabel="predicted probability", ylabel=ylabel, title=title)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def fit_candidate(cfg: dict, X_train, y_train, X_a, y_a, pos_weight: dict):
    model = lgb.LGBMClassifier(
        n_estimators=2000, learning_rate=cfg["learning_rate"], num_leaves=cfg["num_leaves"],
        min_child_samples=cfg["min_child_samples"], scale_pos_weight=pos_weight[cfg["weight"]],
        subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
        metric="average_precision", random_state=0, n_jobs=-1, verbose=-1,
        deterministic=True, force_row_wise=True,
    )
    model.fit(X_train, y_train, eval_set=[(X_a, y_a)], callbacks=[lgb.early_stopping(50, verbose=False)])
    return model


def main() -> None:
    df = pd.read_parquet(DATA)
    train, val, test = (df[df["split"] == s].reset_index(drop=True) for s in ("train", "val", "test"))
    val_a, val_b = split_val(val)
    X_train = prepare(train)
    dtype = X_train["category"].dtype
    X_a, X_b, X_test = prepare(val_a, dtype), prepare(val_b, dtype), prepare(test, dtype)
    neg_over_pos = float((train["label"] == 0).sum() / train["label"].sum())
    pos_weight = {"none": 1.0, "sqrt": math.sqrt(neg_over_pos), "full": neg_over_pos}

    candidates = random.Random(0).sample(
        [dict(zip(GRID, values)) for values in itertools.product(*GRID.values())], N_CONFIGS
    )
    mlflow.set_tracking_uri(f"sqlite:///{ROOT.parent / 'mlflow.db'}")
    mlflow.set_experiment("risk")
    data_hash = hashlib.sha256(DATA.read_bytes()).hexdigest()[:16]

    with mlflow.start_run(run_name="risk-final"):
        sweep = []
        for i, cfg in enumerate(candidates):
            with mlflow.start_run(run_name=f"candidate-{i}", nested=True):
                model = fit_candidate(cfg, X_train, train["label"], X_a, val_a["label"], pos_weight)
                score_a = model.predict_proba(X_a)[:, 1]
                result = {
                    **cfg, "best_iteration": int(model.best_iteration_),
                    "valA_order_pr_auc": order_pr_auc(val_a, score_a),
                    "valA_pr_auc": float(average_precision_score(val_a["label"], score_a)),
                }
                mlflow.log_params({**cfg, "best_iteration": result["best_iteration"]})
                mlflow.log_metrics({k: v for k, v in result.items() if k.startswith("valA")})
                sweep.append((result, model))
                print(i, {k: (round(v, 4) if isinstance(v, float) else v) for k, v in result.items()})

        winner = select_winner([r for r, _ in sweep])
        model = next(m for r, m in sweep if r is winner)
        print("winner:", winner)

        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        iso.fit(model.predict_proba(X_b)[:, 1], val_b["label"])  # unweighted labels
        calibrator = Calibrator.from_isotonic(iso)

        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        model.booster_.save_model(str(ARTIFACTS / "model.txt"), num_iteration=model.best_iteration_)
        (ARTIFACTS / "calibrator.json").write_text(calibrator.to_json())
        categories = [str(c) for c in dtype.categories]
        (ARTIFACTS / "features.json").write_text(json.dumps({"features": FEATURES, "categories": categories}))

        # everything below uses the artefacts exactly as the API will load them
        loaded = RiskModel.load(ARTIFACTS)
        raw_test, cal_test = loaded.raw(test), loaded.score(test)
        assert np.allclose(cal_test, calibrator(model.predict_proba(X_test)[:, 1]), atol=1e-9), "round trip changed scores"
        raw_b, cal_b = loaded.raw(val_b), loaded.score(val_b)

        y_test, y_b = test["label"].to_numpy(), val_b["label"].to_numpy()
        rate_b = float(y_b.mean())
        test_cal = {
            **metrics(test, cal_test), "ece": ece(y_test, cal_test), "mean_predicted": float(cal_test.mean()),
            "brier_constant_val_rate": float(np.mean((y_test - rate_b) ** 2)),
            "diag_brier_prior_shift_oracle": prior_shift_brier(y_test, cal_test.clip(1e-9, 1 - 1e-9), rate_b),
            "distinct_calibrated_values": int(len(np.unique(cal_test))),
        }
        card = {
            "model": "LightGBM + isotonic calibration", "git_commit": git_commit(), "data_hash": data_hash,
            "winner": winner, "sweep": [r for r, _ in sweep], "n_distinct_calibrator_steps": int(len(calibrator.y)),
            "ranges": {n: [str(d.purchase_ts.min().date()), str(d.purchase_ts.max().date())]
                       for n, d in (("train", train), ("val", val), ("test", test))},
            "valB_calibrated (in-sample for the calibrator)": {**metrics(val_b, cal_b), "ece": ece(y_b, cal_b)},
            "test_calibrated": test_cal,
            "test_raw_ranking (before isotonic ties)": metrics(test, raw_test),
            "notes": [
                "Calibrator fitted on validation half B, which has a higher base rate than test "
                f"({rate_b:.3f} vs {float(y_test.mean()):.3f}), so test probabilities over-predict on average. "
                "Not corrected: production has no future labels. diag_brier_prior_shift_oracle is a diagnostic that peeks at test labels.",
                "Label is a proxy (CONTEXT.md 7.3), not real PayPal disputes.",
                "SHAP values are exact TreeSHAP in raw log-odds, before calibration.",
                "Isotonic creates tied scores, so calibrated ranking metrics can differ slightly from raw ones.",
            ],
        }
        (ARTIFACTS / "model_card.json").write_text(json.dumps(card, indent=2) + "\n")

        scored = test.assign(p=cal_test, value=test["order_value"])
        by_stage = {name: threshold_table(scored[sel(scored)], "snapshot row at this stage") for name, sel in STAGES.items()}
        card["test_by_stage"] = {
            name: {"rows": t["rows"], "base_rate": t["base_rate"], "mean_predicted": float(scored[STAGES[name](scored)]["p"].mean()),
                   "ece": ece(scored[STAGES[name](scored)]["label"].to_numpy(), scored[STAGES[name](scored)]["p"].to_numpy())}
            for name, t in by_stage.items()
        }
        card["notes"].append(
            "Use threshold_table.json by_stage for decisions: a seller scores an order at a given moment, and at checkout only the "
            "day-0 row exists. retrospective_any_time takes each order's highest-risk snapshot, which uses delivery information "
            "from after purchase and is an upper bound, not purchase-time performance."
        )
        card["notes"].append(
            "Goodwill refund (cost 10% of order value) reduces to risk > 10%, which flags most orders at low precision: "
            "a finding about the cost assumption, not a recommendation."
        )
        (ARTIFACTS / "model_card.json").write_text(json.dumps(card, indent=2) + "\n")
        table = {
            "by_stage": by_stage,
            "retrospective_any_time": threshold_table(
                per_order(test, cal_test), "order, highest-risk snapshot (retrospective upper bound)"
            ),
        }
        (ARTIFACTS / "threshold_table.json").write_text(json.dumps(table, indent=2) + "\n")

        curve_test, curve_b = calibration_curve(y_test, cal_test), calibration_curve(y_b, cal_b)
        (ARTIFACTS / "calibration.json").write_text(json.dumps({"test": curve_test, "valB": curve_b}, indent=2) + "\n")
        plot_calibration({"test": curve_test, "validation B (in-sample)": curve_b}, ARTIFACTS / "calibration.png")

        sample = test.sample(n=min(20000, len(test)), random_state=0)
        importance = np.abs(loaded.contributions(sample)).mean(axis=0)
        (ARTIFACTS / "shap_importance.json").write_text(
            json.dumps(dict(sorted(zip(FEATURES, map(float, importance)), key=lambda kv: -kv[1])), indent=2) + "\n"
        )

        mlflow.log_params({**winner, "data_hash": data_hash})
        mlflow.log_metrics({f"test_{k}": v for k, v in test_cal.items()})
        for f in ARTIFACTS.iterdir():
            mlflow.log_artifact(str(f))

    print("test calibrated:", {k: round(v, 4) for k, v in test_cal.items()})
    print("test raw ranking:", {k: round(v, 4) for k, v in metrics(test, raw_test).items() if k != "brier"})
    print("size KB:", sum(f.stat().st_size for f in ARTIFACTS.iterdir()) // 1024)


if __name__ == "__main__":
    main()
