import numpy as np
from sklearn.linear_model import LogisticRegression

from ml.claim_model import ClaimClassifier
from ml.train_claims import is_test, report


def test_numpy_inference_matches_sklearn():
    rng = np.random.default_rng(0)
    X, y = rng.normal(size=(300, 8)), rng.choice(["a", "b", "c", "d"], 300)
    lr = LogisticRegression(max_iter=500).fit(X, y)
    clf = ClaimClassifier(list(lr.classes_), lr.coef_, lr.intercept_, embed=lambda texts: X[: len(texts)])
    np.testing.assert_allclose(clf.proba(["x"] * 300), lr.predict_proba(X), atol=1e-9)
    assert clf.predict(["x"] * 300) == list(lr.predict(X))


def test_is_test_is_deterministic_and_about_a_fifth():
    texts = [f"message {i}" for i in range(5000)]
    flags = [is_test(t) for t in texts]
    assert flags == [is_test(t) for t in texts] and 0.15 < np.mean(flags) < 0.25


def test_report_macro_f1_on_a_known_case():
    r = report(["not_received", "other", "other", "unauthorised"], ["not_received", "other", "unauthorised", "unauthorised"])
    assert r["n"] == 4 and r["confusion_matrix"]["rows_true_cols_pred"][3][3] == 1
    assert 0 < r["macro_f1"] < 1


def test_build_truth_prefers_human_and_skips_open_disagreements():
    import pandas as pd

    from ml.train_claims import build_truth

    weak = pd.Series({"1": "other", "2": "other", "3": "other", "4": "other"})
    reviewer = pd.DataFrame({"complaint_id": list("1234"), "text": list("abcd"),
                             "reviewer_label": ["other", "unauthorised", "unauthorised", "not_received"]})
    adjudicated = pd.DataFrame({"complaint_id": ["2", "3", "4"], "human_label": ["other", "unauthorised", ""]})
    t = build_truth(weak, reviewer, adjudicated).set_index("complaint_id")
    assert t["truth"].to_dict() == {"1": "other", "2": "other", "3": "unauthorised"}  # row 4: open disagreement left out
    assert t["source"].to_dict() == {"1": "both_llms", "2": "human", "3": "human"}
