import json

import lightgbm as lgb
import numpy as np
import pandas as pd
import pytest
from sklearn.isotonic import IsotonicRegression

from ml.features.risk import FEATURES
from ml.risk_model import ARTIFACTS, Calibrator, RiskModel, decide
from ml.train_risk_final import ece, per_order, select_winner, split_val, threshold_table


def test_calibrator_matches_sklearn_isotonic_including_out_of_range():
    rng = np.random.default_rng(0)
    raw = rng.uniform(0.1, 0.9, 500)
    y = (rng.uniform(size=500) < raw**2).astype(int)
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, y)
    cal = Calibrator.from_json(Calibrator.from_isotonic(iso).to_json())  # JSON round trip

    probe = np.array([-5.0, 0.0, 0.1, 0.33, 0.5, 0.9, 1.0, 7.0])  # includes out-of-range
    np.testing.assert_allclose(cal(probe), iso.predict(probe))
    out = cal(np.linspace(-1, 2, 200))
    assert np.all(np.diff(out) >= 0) and out.min() >= 0 and out.max() <= 1


def test_decide_rule():
    assert decide(0.0, 100, 0)  # free actions (email, tracking) always run
    assert decide(0.5, 100, 40)  # expected loss 50 > cost 40
    assert not decide(0.4, 100, 40)  # 40 > 40 is false: strict
    assert decide(0.11, 100, 0.10 * 100) and not decide(0.10, 100, 0.10 * 100)  # goodwill share 10% == risk > 10%


def test_validation_halves_never_split_an_order():
    val = pd.DataFrame({"order_id": np.repeat([f"o{i}" for i in range(200)], 5), "label": 0})
    a, b = split_val(val)
    assert set(a["order_id"]).isdisjoint(b["order_id"])
    assert len(a) + len(b) == len(val)
    assert 60 < a["order_id"].nunique() < 140  # roughly half
    a2, _ = split_val(val)
    assert a["order_id"].tolist() == a2["order_id"].tolist()  # deterministic


def test_ece():
    y = np.array([0, 0, 0, 1] * 25)
    assert ece(y, np.full(100, 0.25)) == pytest.approx(0.0)
    assert ece(y, np.full(100, 0.75)) == pytest.approx(0.5)


def test_threshold_table_is_monotone_and_starts_at_the_base_rate():
    rng = np.random.default_rng(1)
    p = rng.uniform(0, 1, 2000)
    orders = pd.DataFrame({"label": (rng.uniform(size=2000) < p).astype(int), "value": rng.uniform(10, 200, 2000), "p": p})
    table = threshold_table(orders, "test")
    rows = table["by_threshold"]

    recalls = [r["recall"] for r in rows]
    shares = [r["flagged_share"] for r in rows]
    assert recalls == sorted(recalls, reverse=True) and shares == sorted(shares, reverse=True)
    assert rows[0]["precision"] == pytest.approx(orders["label"].mean(), abs=0.02)  # t=0.01 flags nearly everyone
    assert table["goodwill_refund"]["n_flagged"] == int((p > 0.10).sum())  # risk * v > 0.1 * v  <=>  risk > 0.1


def test_select_winner_prefers_simpler_within_the_band_and_ignores_nothing_else():
    def r(leaves, child, score):
        return {"num_leaves": leaves, "min_child_samples": child, "valA_order_pr_auc": score}

    sweep = [r(63, 50, 0.660), r(31, 200, 0.655), r(15, 50, 0.652), r(15, 1000, 0.651), r(7, 50, 0.600)]
    # 0.660 - 0.01 = 0.650 band holds the first four; fewest leaves wins, then the larger min_child_samples
    assert select_winner(sweep) == r(15, 1000, 0.651)
    assert select_winner([r(63, 50, 0.7), r(15, 50, 0.6)]) == r(63, 50, 0.7)  # outside the band: best score wins


def test_per_order_takes_the_highest_risk_snapshot_and_keeps_the_label():
    df = pd.DataFrame({"order_id": ["a", "a", "b"], "label": [1, 1, 0], "order_value": [50.0, 50.0, 20.0]})
    out = per_order(df, np.array([0.2, 0.7, 0.4]))
    assert out.loc["a", "p"] == 0.7 and out.loc["b", "p"] == 0.4 and out.loc["a", "label"] == 1


def test_ece_handles_tied_scores_like_isotonic_plateaus():
    rng = np.random.default_rng(3)
    p = np.repeat([0.05, 0.2, 0.5], 400)  # three plateaus
    y = (rng.uniform(size=1200) < p).astype(int)
    assert ece(y, p) < 0.05  # honest rates per plateau: small error, not an artefact of slicing ties
    assert ece(np.zeros(1200, dtype=int), p) == pytest.approx(p.mean(), abs=1e-9)  # nothing positive: error = mean score


def tiny_model():
    rng = np.random.default_rng(2)
    n = 400
    df = pd.DataFrame({f: rng.normal(size=n) for f in FEATURES})
    df["category"] = rng.choice(["a", "b"], n)
    y = (df["order_value"] + rng.normal(size=n) > 1).astype(int)
    X = df.copy()
    X["category"] = X["category"].astype(pd.CategoricalDtype(["a", "b"]))
    booster = lgb.LGBMClassifier(n_estimators=8, num_leaves=4, min_child_samples=5, verbose=-1).fit(X, y).booster_
    return RiskModel(booster, Calibrator(np.array([0.0, 1.0]), np.array([0.0, 1.0])), FEATURES, ["a", "b"]), df


def test_explanations_add_up_to_the_raw_score():
    model, df = tiny_model()
    full = model.booster.predict(model._frame(df), pred_contrib=True)
    margin = model.booster.predict(model._frame(df), raw_score=True)
    np.testing.assert_allclose(full.sum(axis=1), margin, atol=1e-6)  # contributions + bias = raw log-odds
    np.testing.assert_allclose(model.contributions(df), full[:, :-1])
    top = model.explain(df.head(3), top=2)
    assert len(top) == 3 and all(len(r) == 2 for r in top)
    assert all(abs(r[0][2]) >= abs(r[1][2]) for r in top)


def test_unseen_category_is_scored_not_rejected():
    model, df = tiny_model()
    df.loc[df.index[0], "category"] = "never-seen"
    nan_row = df.head(1).assign(category=np.nan)
    assert np.isfinite(model.score(df.head(1))).all()
    np.testing.assert_allclose(model.score(df.head(1)), model.score(nan_row))  # unseen == missing


ARTEFACTS_PRESENT = (ARTIFACTS / "model_card.json").exists()


@pytest.mark.skipif(not ARTEFACTS_PRESENT, reason="run `make train` first")
def test_committed_artefacts_are_small_complete_and_loadable():
    files = {p.name: p.stat().st_size for p in ARTIFACTS.iterdir()}
    expected = {"model.txt", "calibrator.json", "features.json", "model_card.json",
                "calibration.png", "calibration.json", "threshold_table.json", "shap_importance.json"}
    assert expected <= set(files)
    assert max(files.values()) < 10 * 1024 * 1024 and sum(files.values()) < 10 * 1024 * 1024

    card = json.loads((ARTIFACTS / "model_card.json").read_text())
    assert {"brier", "pr_auc"} <= set(card["test_calibrated"])
    model = RiskModel.load()
    assert model.features == FEATURES
    assert card["n_distinct_calibrator_steps"] > 20  # a degenerate calibrator would be a flat line
    table = json.loads((ARTIFACTS / "threshold_table.json").read_text())
    assert set(table) == {"by_stage", "retrospective_any_time"} and len(table["by_stage"]) == 3
    assert set(card["test_by_stage"]) == set(table["by_stage"])
