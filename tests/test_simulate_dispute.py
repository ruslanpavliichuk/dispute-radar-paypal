import json
from datetime import datetime, timezone

import httpx
import pytest
import respx
from sqlalchemy import insert

from api.config import settings
from api.db import init_db, make_engine, orders
from api.paypal_client import PayPalClient
from scripts import simulate_dispute as sim

DISPUTE = "PP-R-ABC-123"
BASE = "https://api-m.sandbox.paypal.com"


def dispute_json(status="UNDER_REVIEW", capture="CAP1", stage="INQUIRY"):
    return {
        "dispute_id": DISPUTE,
        "status": status,
        "dispute_life_cycle_stage": stage,
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "disputed_transactions": [{"seller_transaction_id": capture}],
    }


@pytest.fixture
def engine():
    e = make_engine("sqlite://")
    init_db(e)
    with e.begin() as conn:
        conn.execute(
            insert(orders).values(
                paypal_order_id="ORDER1", product_id="wool-beanie", amount_minor=1800,
                currency="GBP", status="COMPLETED", capture_id="CAP1",
                created_at=datetime.now(timezone.utc),
            )
        )
    return e


@pytest.fixture
def paypal():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/v1/oauth2/token").mock(
            return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        )
        mock.get_route = mock.get(f"/v1/customer/disputes/{DISPUTE}")
        mock.evidence_route = mock.post(f"/v1/customer/disputes/{DISPUTE}/require-evidence")
        mock.adjudicate_route = mock.post(f"/v1/customer/disputes/{DISPUTE}/adjudicate")
        yield mock


async def run(argv, engine):
    client = PayPalClient()
    try:
        return await sim.run(sim.parse_args(argv), client, engine)
    finally:
        await client.aclose()


async def test_evidence_sends_the_exact_body_and_rereads_the_dispute(paypal, engine, capsys):
    paypal.get_route.side_effect = [
        httpx.Response(200, json=dispute_json("WAITING_FOR_SELLER_RESPONSE")),
        httpx.Response(200, json=dispute_json("UNDER_REVIEW")),
    ]
    paypal.evidence_route.mock(return_value=httpx.Response(202, json={}))

    code = await run(["evidence", DISPUTE, "--party", "seller"], engine)

    assert code == 0
    assert json.loads(paypal.evidence_route.calls.last.request.content) == {"action": "SELLER_EVIDENCE"}
    out = capsys.readouterr().out
    assert "before: WAITING_FOR_SELLER_RESPONSE" in out and "after:  UNDER_REVIEW" in out


async def test_adjudicate_sends_the_exact_body(paypal, engine):
    paypal.get_route.mock(return_value=httpx.Response(200, json=dispute_json()))
    paypal.adjudicate_route.mock(return_value=httpx.Response(202, json={}))

    assert await run(["adjudicate", DISPUTE, "--outcome", "seller"], engine) == 0
    assert json.loads(paypal.adjudicate_route.calls.last.request.content) == {
        "adjudication_outcome": "SELLER_FAVOR"
    }


async def test_buyer_outcome_maps_to_buyer_favor(paypal, engine):
    paypal.get_route.mock(return_value=httpx.Response(200, json=dispute_json()))
    paypal.adjudicate_route.mock(return_value=httpx.Response(202, json={}))

    await run(["adjudicate", DISPUTE, "--outcome", "buyer"], engine)
    assert json.loads(paypal.adjudicate_route.calls.last.request.content) == {
        "adjudication_outcome": "BUYER_FAVOR"
    }


async def test_refuses_a_dispute_that_is_not_for_our_orders(paypal, engine, capsys):
    paypal.get_route.mock(return_value=httpx.Response(200, json=dispute_json(capture="SOMEONE-ELSE")))

    assert await run(["adjudicate", DISPUTE, "--outcome", "seller"], engine) == 1
    assert not paypal.adjudicate_route.called
    assert "not for any capture in our orders" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["RESOLVED", "SOMETHING_NEW", None])
async def test_only_acts_on_known_open_statuses(paypal, engine, capsys, status):
    body = dispute_json()
    if status is None:
        del body["status"]
    else:
        body["status"] = status
    paypal.get_route.mock(return_value=httpx.Response(200, json=body))

    assert await run(["evidence", DISPUTE, "--party", "seller"], engine) == 1
    assert not paypal.evidence_route.called
    assert "will only act on" in capsys.readouterr().err


async def test_list_survives_one_bad_item(paypal, engine, capsys):
    paypal.get("/v1/customer/disputes").mock(
        return_value=httpx.Response(
            200, json={"items": [{"dispute_id": "PP-BAD"}, {"nothing": 1}, {"dispute_id": DISPUTE}]}
        )
    )
    paypal.get("/v1/customer/disputes/PP-BAD").mock(
        return_value=httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "gone"})
    )
    paypal.get_route.mock(return_value=httpx.Response(200, json=dispute_json()))

    assert await run(["list"], engine) == 0
    out = capsys.readouterr().out
    assert "PP-BAD  error:" in out and f"{DISPUTE}  status=UNDER_REVIEW" in out


async def test_database_down_is_a_clean_error(paypal, capsys):
    paypal.get_route.mock(return_value=httpx.Response(200, json=dispute_json()))
    bad_engine = make_engine("sqlite://")  # no tables created: the orders lookup fails

    assert await run(["show", DISPUTE], bad_engine) == 1
    assert "database problem" in capsys.readouterr().err


async def test_paypal_422_prints_the_reason_and_fails(paypal, engine, capsys):
    paypal.get_route.mock(return_value=httpx.Response(200, json=dispute_json()))
    paypal.adjudicate_route.mock(
        return_value=httpx.Response(
            422,
            json={"name": "UNPROCESSABLE_ENTITY", "message": "bad state",
                  "details": [{"issue": "DISPUTE_NOT_IN_VALID_STATE"}]},
        )
    )

    assert await run(["adjudicate", DISPUTE, "--outcome", "seller"], engine) == 1
    err = capsys.readouterr().err
    assert "DISPUTE_NOT_IN_VALID_STATE" in err and "422" in err


async def test_show_does_not_modify_anything_and_lists_allowed_actions(paypal, engine, capsys):
    body = dispute_json()
    body["links"] = [{"rel": "self"}, {"rel": "escalate"}, {"rel": "make_offer"}]
    paypal.get_route.mock(return_value=httpx.Response(200, json=body))

    assert await run(["show", DISPUTE], engine) == 0
    out = capsys.readouterr().out
    assert "order ORDER1" in out
    assert "actions PayPal allows now: escalate, make_offer" in out
    assert not paypal.evidence_route.called and not paypal.adjudicate_route.called


async def test_escalate_posts_the_note_and_rereads_the_dispute(paypal, engine, capsys):
    escalate_route = paypal.post(f"/v1/customer/disputes/{DISPUTE}/escalate").mock(
        return_value=httpx.Response(202, json={})
    )
    paypal.get_route.side_effect = [
        httpx.Response(200, json=dispute_json("WAITING_FOR_SELLER_RESPONSE")),
        httpx.Response(200, json=dispute_json("UNDER_REVIEW", stage="CHARGEBACK")),
    ]

    assert await run(["escalate", DISPUTE, "--note", "please review"], engine) == 0
    assert json.loads(escalate_route.calls.last.request.content) == {"note": "please review"}
    assert "after:  UNDER_REVIEW" in capsys.readouterr().out


async def test_escalate_refuses_a_dispute_that_is_not_ours(paypal, engine):
    escalate_route = paypal.post(f"/v1/customer/disputes/{DISPUTE}/escalate")
    paypal.get_route.mock(return_value=httpx.Response(200, json=dispute_json(capture="SOMEONE-ELSE")))

    assert await run(["escalate", DISPUTE], engine) == 1
    assert not escalate_route.called


async def test_unknown_dispute_is_a_clean_error_not_a_traceback(paypal, engine, capsys):
    paypal.get_route.mock(return_value=httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"}))

    assert await run(["show", DISPUTE], engine) == 1
    assert "RESOURCE_NOT_FOUND" in capsys.readouterr().err


def test_dispute_id_must_be_plain():
    with pytest.raises(SystemExit):
        sim.parse_args(["show", "../webhooks/paypal"])


@pytest.mark.parametrize(
    "base",
    [
        "https://api-m.paypal.com",
        "https://evil.example/sandbox",
        "https://api-m.paypal.com/?sandbox",
        "https://api-m.sandbox.paypal.com.evil.example",
    ],
)
async def test_non_sandbox_base_aborts_before_any_http_call(monkeypatch, capsys, base):
    monkeypatch.setattr(settings, "paypal_client_id", "id")
    monkeypatch.setattr(settings, "paypal_client_secret", "secret")
    monkeypatch.setattr(settings, "paypal_api_base", base)
    with respx.mock(assert_all_called=False) as mock:
        assert await sim.amain(["list"]) == 1
        assert not mock.calls
    assert "sandbox" in capsys.readouterr().err
