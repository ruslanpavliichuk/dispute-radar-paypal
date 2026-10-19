import numpy as np
import pandas as pd
import pytest

from ml.train_risk import metrics, recall_at_top


def test_recall_at_top_counts_positives_in_the_highest_scored_rows():
    y = np.array([1, 0, 1, 0, 0, 0, 0, 0, 0, 0])
    score = np.array([0.9, 0.8, 0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0])
    # top 10% of 10 rows is one row (score 0.9, a positive) out of 2 positives
    assert recall_at_top(y, score) == pytest.approx(0.5)
    assert recall_at_top(y, score, fraction=0.3) == pytest.approx(0.5)  # adds 0.8 and 0.3: both negatives
    assert recall_at_top(y, score, fraction=0.5) == pytest.approx(1.0)


def test_metrics_for_a_perfect_score_and_order_level_max():
    df = pd.DataFrame({"order_id": list("aabbcc"), "label": [1, 1, 0, 0, 0, 0]})
    out = metrics(df, np.array([0.9, 0.7, 0.2, 0.1, 0.3, 0.0]))
    assert out["pr_auc"] == pytest.approx(1.0) and out["roc_auc"] == pytest.approx(1.0)
    assert out["order_pr_auc"] == pytest.approx(1.0)  # order a's best snapshot outranks b and c
    assert out["base_rate"] == pytest.approx(1 / 3)
