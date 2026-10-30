import logging
import time

import pytest
from fastapi.testclient import TestClient

from api.config import settings
from api.main import app

RISK = {"order_value": 120.0, "freight_share": 0.1, "category": "toys", "n_items": 1, "est_window_days": 20.0,
        "same_state": False, "seller_late_rate": 0.05, "seller_low_review_rate": None, "days_since_purchase": 12.0,
        "days_to_est": 8.0, "delivered_yet": False, "days_late_so_far": 0.0, "shipped_yet": True, "days_since_ship": 3.0}
OUTCOME = {"claim_type": "not_received", "lifecycle_stage": "INQUIRY", "order_value": 80.0, "tracking_uploaded": True,
           "delivery_confirmed": True, "signature_confirmed": False, "shipped_to_paypal_address": True,
           "photos_supplied": False, "response_days": 3, "seller_account_age_months": 24, "category": "toys"}
CLAIM = {"text": "The parcel never arrived and the tracking number shows nothing since last week."}


@pytest.fixture(scope="module")
def client():
    # module scope so the saved artefacts load once; the autouse conftest fixture is per-test, so patch settings here
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings, "database_url", "sqlite://")
        mp.setattr(settings, "paypal_client_id", "id")  # lifespan checks these but never calls PayPal at startup
        mp.setattr(settings, "paypal_client_secret", "secret")
        with TestClient(app) as c:
            yield c


def test_risk_returns_probability_and_three_reasons(client):
    r = client.post("/score/risk", json=RISK)
    assert r.status_code == 200
    body = r.json()
    assert 0 <= body["score"] <= 1 and len(body["reasons"]) == 3
    assert {"feature", "value", "contribution"} <= set(body["reasons"][0])


def test_outcome_rewards_evidence(client):
    strong = client.post("/score/outcome", json=OUTCOME).json()["score"]
    weak = client.post("/score/outcome", json={**OUTCOME, "tracking_uploaded": False, "delivery_confirmed": False,
                                               "shipped_to_paypal_address": False}).json()["score"]
    assert strong > weak


def test_claim_labels_a_clear_message_and_explains_it(client):
    body = client.post("/score/claim", json=CLAIM).json()
    assert body["label"] == "not_received"
    assert abs(sum(body["scores"].values()) - 1) < 1e-6
    assert 1 <= len(body["reasons"]) <= 3 and all(r["text"] for r in body["reasons"])


@pytest.mark.parametrize("path,bad", [
    ("/score/risk", {**RISK, "order_value": -1}),
    ("/score/risk", {**RISK, "surprise": 1}),
    ("/score/outcome", {**OUTCOME, "claim_type": "refund"}),
    ("/score/claim", {"text": ""}),
    ("/score/claim", {"text": "x" * 5001}),
])
def test_rejects_invalid_input(client, path, bad):
    assert client.post(path, json=bad).status_code == 422


def test_unseen_category_is_scored_and_logged(client, caplog):
    with caplog.at_level(logging.WARNING, logger="dispute_radar.scoring"):
        r = client.post("/score/risk", json={**RISK, "category": "space_yachts"})
    assert r.status_code == 200 and "unseen category" in caplog.text


def test_every_endpoint_answers_under_300ms(client):
    for path, body in [("/score/risk", RISK), ("/score/outcome", OUTCOME), ("/score/claim", CLAIM)]:
        client.post(path, json=body)  # warm up
        t = time.perf_counter()
        client.post(path, json=body)
        assert time.perf_counter() - t < 0.3, path
