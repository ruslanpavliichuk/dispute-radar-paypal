"""Baseline dispute-risk model: LightGBM with default parameters (T05).

    uv run python -m ml.train_risk

Logs one MLflow run (local ./mlruns) and writes a draft model card to
ml/artifacts/risk_baseline/. T06 replaces this with the tuned, calibrated model.
"""
import hashlib
import json
import subprocess
from pathlib import Path

import lightgbm as lgb
import mlflow
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from ml.features.risk import FEATURES

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "processed" / "risk_snapshots.parquet"
OUT = ROOT / "artifacts" / "risk_baseline"
TOP_FRACTION = 0.10


def recall_at_top(y: np.ndarray, score: np.ndarray, fraction: float = TOP_FRACTION) -> float:
    """Share of all positives found among the `fraction` highest-scored rows."""
    k = max(1, int(round(len(y) * fraction)))
    top = np.argsort(-score, kind="stable")[:k]
    return float(y[top].sum() / y.sum())


def metrics(df: pd.DataFrame, score: np.ndarray) -> dict:
    y = df["label"].to_numpy()
    # an order counts as flagged when its highest-scored snapshot is flagged
    per_order = df.assign(score=score).groupby("order_id").agg(label=("label", "first"), score=("score", "max"))
    return {
        "base_rate": float(y.mean()),
        "pr_auc": float(average_precision_score(y, score)),
        "roc_auc": float(roc_auc_score(y, score)),
        "brier": float(brier_score_loss(y, score)),
        f"recall_top{int(TOP_FRACTION * 100)}": recall_at_top(y, score),
        "order_pr_auc": float(average_precision_score(per_order["label"], per_order["score"])),
        f"order_recall_top{int(TOP_FRACTION * 100)}": recall_at_top(
            per_order["label"].to_numpy(), per_order["score"].to_numpy()
        ),
    }


def prepare(df: pd.DataFrame, categories: pd.CategoricalDtype | None = None):
    X = df[FEATURES].copy()
    X["category"] = X["category"].astype(categories or "category")
    return X


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> None:
    df = pd.read_parquet(DATA)
    parts = {name: df[df["split"] == name].reset_index(drop=True) for name in ("train", "val", "test")}
    X_train = prepare(parts["train"])
    dtype = X_train["category"].dtype  # categories fixed on train; unseen ones become NaN
    X = {name: prepare(part, dtype) for name, part in parts.items()}

    params = {"random_state": 0, "n_jobs": -1, "verbose": -1}  # everything else is LightGBM's default
    model = lgb.LGBMClassifier(**params).fit(X_train, parts["train"]["label"])

    # MLflow 3 refuses the plain ./mlruns file store; a local SQLite file is the supported one
    mlflow.set_tracking_uri(f"sqlite:///{ROOT.parent / 'mlflow.db'}")
    mlflow.set_experiment("risk")
    data_hash = hashlib.sha256(DATA.read_bytes()).hexdigest()[:16]
    with mlflow.start_run(run_name="baseline-lightgbm-default"):
        mlflow.log_params({**params, "features": ",".join(FEATURES), "data_hash": data_hash})
        card = {"model": "lightgbm default parameters", "status": "draft baseline (T05)", "data_hash": data_hash,
                "git_commit": git_commit(), "splits": {}}
        for name, part in parts.items():
            m = metrics(part, model.predict_proba(X[name])[:, 1])
            card["splits"][name] = {
                "orders": int(part["order_id"].nunique()), "rows": len(part),
                "purchase_range": [str(part["purchase_ts"].min().date()), str(part["purchase_ts"].max().date())],
                **m,
            }
            mlflow.log_metrics({f"{name}_{k}": v for k, v in m.items()})
            print(name, {k: round(v, 4) for k, v in m.items()})
        OUT.mkdir(parents=True, exist_ok=True)
        model.booster_.save_model(str(OUT / "model.txt"))
        (OUT / "model_card.json").write_text(json.dumps(card, indent=2) + "\n")
        mlflow.log_artifact(str(OUT / "model_card.json"))

    importance = pd.Series(model.booster_.feature_importance("gain"), index=FEATURES).sort_values(ascending=False)
    print("gain importance:", (importance / importance.sum()).round(3).to_dict())


if __name__ == "__main__":
    main()
