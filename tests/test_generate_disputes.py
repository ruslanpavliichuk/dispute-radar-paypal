import numpy as np
import pandas as pd
import pytest

from ml.generate_disputes import (
    API_REASON_TO_CLAIM_TYPE, FEATURES, OUTCOME_TO_WON, TOLERANCE, generate, load_rules, summarise, win_probability,
)


@pytest.fixture(scope="module")
def rules():
    return load_rules()


@pytest.fixture(scope="module")
def disputes(rules):
    return generate(rules)


def row(**kw):
    base = dict(claim_type="not_received", category="other", order_value=50.0, tracking_uploaded=False,
                delivery_confirmed=False, signature_confirmed=False, shipped_to_paypal_address=False,
                photos_supplied=False, response_days=3, seller_account_age_months=0, lifecycle_stage="INQUIRY")
    return pd.DataFrame([{**base, **kw}])


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


def test_win_probability_matches_a_hand_computed_case(rules):
    # not_received: base -2.0, INQUIRY +0.2, category "other" 0.0, delivery +2.0, tracking +0.8,
    # right address +1.2, signature +0.8, value 300 > 250 with signature +0.6, 12 months age +0.144
    df = row(order_value=300.0, tracking_uploaded=True, delivery_confirmed=True, signature_confirmed=True,
             shipped_to_paypal_address=True, seller_account_age_months=12)
    expected = sigmoid(-2.0 + 0.2 + 2.0 + 0.8 + 1.2 + 0.8 + 0.6 + 0.012 * 12)
    assert win_probability(df, rules)[0] == pytest.approx(expected)


def test_each_rule_moves_the_probability_the_right_way(rules):
    p = lambda **kw: win_probability(row(**kw), rules)[0]  # noqa: E731
    assert p(delivery_confirmed=True) > p() and p(shipped_to_paypal_address=True) > p()
    assert p(response_days=11) < p(response_days=10) == p(response_days=0)  # only past the 10 day deadline hurts
    assert p(photos_supplied=True, claim_type="not_as_described") > p(claim_type="not_as_described")
    assert p(lifecycle_stage="CHARGEBACK") < p()  # escalated disputes are harder for the seller
    assert p(seller_account_age_months=600) == p(seller_account_age_months=60)  # capped
    assert p(order_value=900.0, signature_confirmed=True) > p(order_value=100.0, signature_confirmed=True)
    assert p(order_value=900.0) == p(order_value=100.0)  # the high-value bonus needs a signature


def test_generated_data_is_consistent(disputes):
    assert len(disputes) == 20000 and disputes["dispute_id"].is_unique
    assert disputes["true_win_prob"].between(0, 1).all() and set(disputes["won"].dropna()) == {0, 1}
    assert not (disputes["delivery_confirmed"] & ~disputes["tracking_uploaded"]).any()  # delivery needs tracking
    assert not (disputes["signature_confirmed"] & ~disputes["delivery_confirmed"]).any()  # signature needs delivery
    assert set(FEATURES) <= set(disputes.columns)
    assert not {"won", "true_win_prob", "outcome_code", "dispute_id"} & set(FEATURES)  # no oracle, label or id as a feature
    assert "buyer_prior_disputes" not in disputes.columns  # the API gives us no buyer history


def test_outcome_codes_follow_the_approved_win_mapping(disputes):
    codes = set(disputes["outcome_code"])
    assert codes == {"RESOLVED_SELLER_FAVOUR", "RESOLVED_WITH_PAYOUT", "RESOLVED_BUYER_FAVOUR", "CANCELED_BY_BUYER"}
    for code, won in OUTCOME_TO_WON.items():
        assert (disputes[disputes["outcome_code"] == code]["won"] == won).all()
    assert disputes[disputes["outcome_code"] == "CANCELED_BY_BUYER"]["won"].isna().all()  # no winner
    assert disputes["won"].notna().sum() == (disputes["outcome_code"] != "CANCELED_BY_BUYER").sum()
    assert 0.03 < (disputes["outcome_code"] == "CANCELED_BY_BUYER").mean() < 0.09
    # payouts follow tracked, delivered, correctly addressed orders far more often than other wins
    won = disputes[disputes["won"] == 1]
    eligible = won["tracking_uploaded"] & won["delivery_confirmed"] & won["shipped_to_paypal_address"]
    paid = won["outcome_code"] == "RESOLVED_WITH_PAYOUT"
    assert paid[eligible].mean() > 0.6 > 0.2 > paid[~eligible].mean()


def test_api_reason_mapping_covers_the_three_named_reasons():
    assert API_REASON_TO_CLAIM_TYPE["MERCHANDISE_OR_SERVICE_NOT_RECEIVED"] == "not_received"
    assert API_REASON_TO_CLAIM_TYPE["SIGNIFICANTLY_NOT_AS_DESCRIBED"] == "not_as_described"
    assert API_REASON_TO_CLAIM_TYPE["UNAUTHORIZED_TRANSACTION"] == "unauthorised"


def test_win_rates_per_claim_type_match_the_rules_within_two_points(disputes):
    summary = summarise(disputes)
    assert set(summary.index) == {"not_received", "not_as_described", "unauthorised", "other"}
    assert summary["gap_points"].abs().max() < TOLERANCE * 100


def test_generation_is_deterministic_and_seed_dependent(rules):
    a, b, c = generate(rules, n=500), generate(rules, n=500), generate(rules, n=500, seed=99)
    pd.testing.assert_frame_equal(a, b)
    assert not a["won"].equals(c["won"])


def test_data_has_enough_spread_for_a_decision_layer(disputes):
    # a model and the fight/offer/accept layer need both clear wins and clear losses
    assert (disputes["true_win_prob"] < 0.35).mean() > 0.08 and (disputes["true_win_prob"] > 0.8).mean() > 0.2
    assert 0.5 < disputes["won"].mean() < 0.75
