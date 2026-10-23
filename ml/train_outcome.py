"""Dispute-outcome model (T08): P(seller wins) with isotonic calibration and per-row SHAP.

    uv run python -m ml.train_outcome

Trains on resolved disputes only (`won` is <NA> when the buyer cancels). Random split, because the
synthetic rows have no time order: train 60%, validation A 10% (early stopping), validation B 15%
(isotonic calibrator), test 15% (scored once). Writes ml/artifacts/outcome/ and an MLflow run.
The oracle `true_win_prob` is only scored against, never a feature: it bounds what any model can reach.
"""
import hashlib
import json

import lightgbm as lgb
import mlflow
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

from ml.generate_disputes import FEATURES, OUT as DATA
from ml.outcome_model import ARTIFACTS, CATEGORICAL, OutcomeModel
from ml.risk_model import Calibrator
from ml.train_risk import ROOT, git_commit
from ml.train_risk_final import calibration_curve, ece, plot_calibration


def split(df: pd.DataFrame, seed: int = 0) -> dict[str, pd.DataFrame]:
    """Disjoint random split of resolved disputes."""
    df = df[df["won"].notna()].reset_index(drop=True)
    cut = (np.cumsum([0.60, 0.10, 0.15]) * len(df)).astype(int)
    parts = np.split(np.random.default_rng(seed).permutation(len(df)), cut)
    return {n: df.iloc[ix].reset_index(drop=True) for n, ix in zip(["train", "valA", "valB", "test"], parts)}


def prepare(df: pd.DataFrame, dtypes: dict | None = None) -> pd.DataFrame:
    X = df[FEATURES].copy()
    for c in CATEGORICAL:
        X[c] = X[c].astype(dtypes[c] if dtypes else "category")
    for c in X.columns.difference(CATEGORICAL):
        X[c] = X[c].astype(float)
    return X


def scores(y: np.ndarray, p: np.ndarray) -> dict:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return {"brier": float(brier_score_loss(y, p)), "log_loss": float(log_loss(y, p)),
            "ece": ece(y, p), "roc_auc": float(roc_auc_score(y, p)), "mean_predicted": float(p.mean())}


def main() -> None:
    parts = split(pd.read_parquet(DATA))
    train, val_a, val_b, test = (parts[k] for k in ("train", "valA", "valB", "test"))
    X_train = prepare(train)
    dtypes = {c: X_train[c].dtype for c in CATEGORICAL}
    y = {k: v["won"].astype(int).to_numpy() for k, v in parts.items()}

    model = lgb.LGBMClassifier(
        n_estimators=2000, learning_rate=0.03, num_leaves=15, min_child_samples=100,
        subsample=0.8, subsample_freq=1, colsample_bytree=0.8, random_state=0, n_jobs=-1, verbose=-1,
        deterministic=True, force_row_wise=True,
    )
    model.fit(X_train, y["train"], eval_set=[(prepare(val_a, dtypes), y["valA"])],
              eval_metric="binary_logloss", callbacks=[lgb.early_stopping(50, verbose=False)])
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(model.predict_proba(prepare(val_b, dtypes))[:, 1], y["valB"])
    calibrator = Calibrator.from_isotonic(iso)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    model.booster_.save_model(str(ARTIFACTS / "model.txt"), num_iteration=model.best_iteration_)
    (ARTIFACTS / "calibrator.json").write_text(calibrator.to_json())
    (ARTIFACTS / "features.json").write_text(json.dumps(
        {"features": FEATURES, "categories": {c: [str(v) for v in d.categories] for c, d in dtypes.items()}}))

    # everything below uses the artefacts exactly as the API will load them
    loaded = OutcomeModel.load(ARTIFACTS)
    p_test, p_raw = loaded.score(test), loaded.raw(test)
    assert np.allclose(p_raw, model.predict_proba(prepare(test, dtypes))[:, 1], atol=1e-9), "round trip changed scores"
    y_test = y["test"]
    results = {
        "calibrated": scores(y_test, p_test),
        "uncalibrated": scores(y_test, p_raw),
        "oracle (true_win_prob, the best any model can do)": scores(y_test, test["true_win_prob"].to_numpy()),
        "constant (train win rate)": scores(y_test, np.full(len(y_test), y["train"].mean())),
    }
    # how close the learned probabilities get to the generator's truth, in probability points
    mae_vs_oracle = float(np.mean(np.abs(p_test - test["true_win_prob"].to_numpy())))

    shap = np.abs(loaded.contributions(test)).mean(axis=0)
    data_hash = hashlib.sha256(DATA.read_bytes()).hexdigest()[:16]
    card = {
        "model": "LightGBM + isotonic calibration", "git_commit": git_commit(), "data_hash": data_hash,
        "rows": {k: int(len(v)) for k, v in parts.items()}, "win_rate": {k: float(v.mean()) for k, v in y.items()},
        "best_iteration": int(model.best_iteration_), "test": results, "mean_abs_error_vs_true_win_prob": mae_vs_oracle,
        "notes": [
            "Trained on resolved disputes only: CANCELED_BY_BUYER rows have no winner.",
            "Labels are drawn from our published rules (ml/generator_rules.yaml), not real PayPal outcomes: "
            "this shows the model recovers known structure, not that it predicts PayPal's decisions.",
            "The oracle row is the Brier/log loss of the true probabilities; the gap to it is the cost of learning from 20k noisy labels.",
            "SHAP values are exact TreeSHAP in raw log-odds, before calibration.",
        ],
    }
    (ARTIFACTS / "model_card.json").write_text(json.dumps(card, indent=2) + "\n")
    (ARTIFACTS / "shap_importance.json").write_text(json.dumps(
        dict(sorted(zip(FEATURES, map(float, shap)), key=lambda kv: -kv[1])), indent=2) + "\n")
    curve = calibration_curve(y_test, p_test)
    oracle_curve = calibration_curve(y_test, test["true_win_prob"].to_numpy())
    (ARTIFACTS / "calibration.json").write_text(json.dumps({"test": curve, "oracle": oracle_curve}, indent=2) + "\n")
    plot_calibration({"model (test)": curve, "true probability (test)": oracle_curve}, ARTIFACTS / "calibration.png",
                     title="Outcome model calibration", lim=1.0, ylabel="observed seller win rate")

    mlflow.set_tracking_uri(f"sqlite:///{ROOT.parent / 'mlflow.db'}")
    mlflow.set_experiment("outcome")
    with mlflow.start_run(run_name="outcome"):
        mlflow.log_params({"best_iteration": model.best_iteration_, "data_hash": data_hash})
        mlflow.log_metrics({f"test_{k}": v for k, v in results["calibrated"].items()})
        for f in ARTIFACTS.iterdir():
            mlflow.log_artifact(str(f))
    for name, r in results.items():
        print(f"{name:55s}", {k: round(v, 4) for k, v in r.items()})
    print("MAE vs true_win_prob:", round(mae_vs_oracle, 4), "| top SHAP:", list(dict(sorted(zip(FEATURES, shap), key=lambda kv: -kv[1])))[:4])


if __name__ == "__main__":
    main()
