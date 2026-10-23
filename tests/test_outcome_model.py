import numpy as np
import pandas as pd

from ml.generate_disputes import FEATURES, generate, load_rules
from ml.outcome_model import ARTIFACTS, OutcomeModel
from ml.train_outcome import split


def _disputes(n=2000):
    return generate(load_rules(), n=n)


def test_split_is_disjoint_and_drops_cancelled():
    df = _disputes()
    parts = split(df)
    ids = [set(p["dispute_id"]) for p in parts.values()]
    assert sum(len(i) for i in ids) == len(set().union(*ids)) == df["won"].notna().sum()
    assert all(p["won"].notna().all() for p in parts.values())


def test_saved_model_scores_explains_and_ignores_unseen_levels():
    m = OutcomeModel.load(ARTIFACTS)
    df = _disputes(50)
    p = m.score(df)
    assert p.shape == (50,) and ((p >= 0) & (p <= 1)).all()
    weak = df.assign(delivery_confirmed=False, tracking_uploaded=False, shipped_to_paypal_address=False)
    strong = df.assign(delivery_confirmed=True, tracking_uploaded=True, shipped_to_paypal_address=True)
    assert m.score(strong).mean() > m.score(weak).mean()
    top = m.explain(df.head(2))
    assert len(top) == 2 and len(top[0]) == 3 and top[0][0][0] in FEATURES
    assert np.isfinite(m.score(df.assign(claim_type="brand_new_claim"))).all()  # unseen level -> NaN, still scores
