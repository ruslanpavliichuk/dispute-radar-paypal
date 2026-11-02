from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from agents import prevention, runner
from agents.prevention import handle_tracking_event, to_risk_features
from api.config import settings
from api.db import init_db, make_engine, orders, proposals, tracking_events
from api.main import app
from api.scoring import RiskRequest, TabularScore, load_models, score_tabular
from scripts import simulate_tracking as sim

C = datetime(2026, 10, 1, tzinfo=timezone.utc)  # order placed at midnight UTC, so calendar days are exact
CFG = SimpleNamespace(est_window_days=14.0)
NUMBER = "DEMO0123456789"
TOKEN = "t0ken"
STUB = {"risk": None}  # tests that stub score_tabular need no model


@pytest.fixture(scope="module")
def models():
    return {"risk": load_models()["risk"]}


@pytest.fixture
def engine():
    e = make_engine("sqlite://")
    init_db(e)
    return e


def add_order(engine, oid="ORD1", status="COMPLETED", amount=6000):
    with engine.begin() as conn:
        conn.execute(insert(orders).values(
            paypal_order_id=oid, product_id="p", amount_minor=amount, currency="GBP", status=status,
            capture_id=f"CAP-{oid}", created_at=C))


def add_ev(engine, kind, day, oid="ORD1", processed=False):
    values = dict(paypal_order_id=oid, kind=kind, occurred_at=C + timedelta(days=day), tracking_number=NUMBER,
                  carrier="OTHER", created_at=C, processed_at=C if processed else None)
    with engine.begin() as conn:
        pk = conn.execute(insert(tracking_events).values(**values)).inserted_primary_key[0]
    return get_ev(engine, pk)


def get_ev(engine, pk):
    with engine.connect() as conn:
        return dict(conn.execute(select(tracking_events).where(tracking_events.c.id == pk)).mappings().one())


def props(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(proposals).order_by(proposals.c.id)).mappings()]


def all_events(engine):
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(tracking_events)).mappings()]


def order_row(engine):
    with engine.connect() as conn:
        return dict(conn.execute(select(orders)).mappings().first())


def risk_is(monkeypatch, value):
    seen = []

    def fake(model, body, name):
        seen.append(body)
        return TabularScore(score=value, reasons=[])
    monkeypatch.setattr(prevention, "score_tabular", fake)
    return seen


async def handle(engine, models, kind, day):
    ev = add_ev(engine, kind, day)
    await handle_tracking_event(engine, models, ev)
    return get_ev(engine, ev["id"])


# ---- features ----
def feats(kind, day, events):
    evs = [{"kind": k, "occurred_at": C + timedelta(days=d)} for k, d in events]
    ev = {"kind": kind, "occurred_at": C + timedelta(days=day)}
    f, defaulted = to_risk_features({"created_at": C, "amount_minor": 6000}, evs, ev, CFG)
    RiskRequest(**f)  # valid for the model service
    return f, defaulted


TIMELINE = [("accepted", 1), ("in_transit", 3), ("stuck_at_depot", 5), ("stuck_at_depot", 16), ("delivered", 18)]


def test_features_each_state():
    f, defaulted = feats("accepted", 1, TIMELINE)
    assert (f["order_value"], f["days_since_purchase"], f["days_to_est"]) == (60.0, 1.0, 13.0)
    assert (f["shipped_yet"], f["days_since_ship"], f["delivered_yet"], f["days_late_so_far"]) == (True, 0.0, False, 0.0)
    assert f["est_window_days"] == 14.0 and f["freight_share"] == 0 and f["category"] == "unknown"
    assert f["n_items"] == 1 and f["same_state"] is False
    assert f["seller_late_rate"] is None and f["seller_low_review_rate"] is None
    assert set(defaulted) == {"est_window_days", "freight_share", "category", "n_items", "same_state",
                              "seller_late_rate", "seller_low_review_rate"}
    f, _ = feats("in_transit", 3, TIMELINE)
    assert (f["days_since_ship"], f["days_to_est"], f["days_late_so_far"]) == (2.0, 11.0, 0.0)
    f, _ = feats("stuck_at_depot", 16, TIMELINE)  # two days past the estimate; the day-18 delivery is still in the future
    assert (f["days_to_est"], f["days_late_so_far"], f["days_since_ship"], f["delivered_yet"]) == (-2.0, 2.0, 15.0, False)
    f, _ = feats("delivered", 18, TIMELINE)
    assert (f["delivered_yet"], f["days_late_so_far"], f["days_to_est"]) == (True, 4.0, -4.0)


def test_features_before_any_shipment_and_fractional_day():
    f, _ = feats("bogus", 0, [("bogus", 0)])
    assert f["shipped_yet"] is False and f["days_since_ship"] is None
    ev = {"kind": "in_transit", "occurred_at": C + timedelta(days=2, hours=12)}
    f, _ = to_risk_features({"created_at": C, "amount_minor": 6000}, [ev], ev, CFG)
    assert f["days_since_purchase"] == 2.5 and f["days_since_ship"] == 0.0


def test_features_naive_datetimes_from_sqlite():
    ev = {"kind": "accepted", "occurred_at": datetime(2026, 10, 2)}
    f, _ = to_risk_features({"created_at": datetime(2026, 10, 1), "amount_minor": 100}, [ev], ev, CFG)
    assert f["days_since_purchase"] == 1.0


# ---- the agent ----
async def test_bar_is_early_before_day_8_and_late_after(engine, monkeypatch):
    add_order(engine)
    seen = risk_is(monkeypatch, 0.19)
    await handle(engine, STUB, "in_transit", 7)
    assert props(engine) == []  # 0.19 < early bar 0.20
    await handle(engine, STUB, "in_transit", 8)  # the same score clears the late bar 0.10
    assert [p["ev"]["bar"] for p in props(engine)] == [0.10]
    assert seen[0].days_since_purchase == 7.0
    risk_is(monkeypatch, 0.20)
    add_order(engine, "ORD2")
    ev = add_ev(engine, "in_transit", 7, "ORD2")
    await handle_tracking_event(engine, STUB, ev)
    assert [(p["subject_id"], p["ev"]["bar"]) for p in props(engine)][-1] == ("ORD2:upload_tracking", 0.20)


async def test_below_late_bar_creates_nothing(engine, monkeypatch):
    add_order(engine)
    risk_is(monkeypatch, 0.09)
    done = await handle(engine, STUB, "stuck_at_depot", 12)
    assert props(engine) == [] and done["processed_at"] is not None and done["last_error"] is None


async def test_event_without_shipment_creates_nothing(engine, monkeypatch):
    add_order(engine)
    seen = risk_is(monkeypatch, 0.9)
    done = await handle(engine, STUB, "bogus", 0)
    assert props(engine) == [] and seen == [] and done["processed_at"] is not None


async def test_upload_proposal_over_the_bar(engine, monkeypatch):
    add_order(engine)
    monkeypatch.setattr(prevention, "score_tabular", lambda *a: TabularScore(score=0.3, reasons=[
        {"feature": "days_since_ship", "value": 4.0, "contribution": 0.5}]))
    await handle(engine, STUB, "in_transit", 5)
    [p] = props(engine)
    assert (p["agent"], p["subject_id"], p["action"], p["status"]) == ("prevention", "ORD1:upload_tracking", "upload_tracking", "PROPOSED")
    assert p["amount_minor"] is None and p["currency"] == "GBP" and p["draft_source"] == "template"
    assert p["ev"] == {"risk": 0.3, "value_minor": 6000, "bar": 0.2, "expected_loss_minor": 1800}
    assert p["features"]["days_since_purchase"] == 5.0 and "est_window_days" in p["defaulted"]
    assert p["reasons"][0]["feature"] == "days_since_ship"
    assert p["draft"] == ("Your order was shipped on 6 October 2026; tracking number DEMO0123456789. "
                          "It is later than expected and we are looking into it.")
    assert p["claim_type"] is None and p["win_prob"] is None  # no buyer data, no dispute fields


async def test_goodwill_only_past_the_estimate_and_over_the_share(engine, monkeypatch):
    add_order(engine)
    risk_is(monkeypatch, 0.5)
    await handle(engine, STUB, "stuck_at_depot", 12)  # not past the estimate (day 14)
    assert [p["action"] for p in props(engine)] == ["upload_tracking"]
    risk_is(monkeypatch, 0.10)
    await handle(engine, STUB, "stuck_at_depot", 16)  # past it, but 0.10 is not over the 10% share
    assert [p["action"] for p in props(engine)] == ["upload_tracking"]
    risk_is(monkeypatch, 0.11)
    await handle(engine, STUB, "stuck_at_depot", 17)
    up, goodwill = props(engine)
    assert goodwill["action"] == "goodwill_refund" and goodwill["subject_id"] == "ORD1:goodwill_refund"
    assert goodwill["amount_minor"] == 600 and goodwill["currency"] == "GBP" and goodwill["status"] == "PROPOSED"
    assert goodwill["draft"] == ("We are sorry your order is later than expected. As a goodwill gesture we would "
                                 "like to refund 6.00 GBP.")
    assert goodwill["ev"]["risk"] == 0.11 and goodwill["features"]["days_to_est"] == -3.0


async def test_reprocessing_does_not_duplicate(engine, monkeypatch):
    add_order(engine)
    risk_is(monkeypatch, 0.5)
    ev = add_ev(engine, "stuck_at_depot", 16)
    await handle_tracking_event(engine, STUB, ev)
    await handle_tracking_event(engine, STUB, ev)
    assert sorted(p["action"] for p in props(engine)) == ["goodwill_refund", "upload_tracking"]


async def test_delivered_supersedes_only_proposed_rows(engine, monkeypatch):
    add_order(engine)
    add_order(engine, "ORD2")
    risk_is(monkeypatch, 0.5)
    await handle(engine, STUB, "stuck_at_depot", 16)
    for status in ("SENT", "REJECTED"):  # rows already decided keep their state
        with engine.begin() as conn:
            conn.execute(update(proposals).where(proposals.c.action == "goodwill_refund").values(status=status))
        await handle(engine, STUB, "delivered", 17)
        assert {p["action"]: p["status"] for p in props(engine)}["goodwill_refund"] == status
    rows = {p["action"]: p for p in props(engine)}
    assert rows["upload_tracking"]["status"] == "REJECTED" and rows["upload_tracking"]["error"] == "superseded"
    assert rows["upload_tracking"]["decided_at"] is not None and rows["goodwill_refund"]["error"] is None
    await handle(engine, STUB, "delivered", 17)  # and again: nothing new is created
    assert len(props(engine)) == 2
    add_ev(engine, "stuck_at_depot", 16, "ORD2", processed=True)
    await handle(engine, STUB, "delivered", 18)  # an order with no proposals: nothing happens
    assert len(props(engine)) == 2


@pytest.mark.parametrize("status", ["PENDING", "CREATED", "MISMATCH", "REFUNDED"])
async def test_order_not_completed_is_skipped(engine, monkeypatch, status):
    add_order(engine, status=status)
    seen = risk_is(monkeypatch, 0.9)
    done = await handle(engine, STUB, "stuck_at_depot", 16)
    assert done["last_error"] == "order is not completed" and done["processed_at"] is not None
    assert props(engine) == [] and seen == []


async def test_unknown_order_is_skipped(engine, monkeypatch):
    risk_is(monkeypatch, 0.9)
    done = await handle(engine, STUB, "stuck_at_depot", 16)
    assert done["last_error"] == "unknown order" and done["processed_at"] is not None and props(engine) == []


async def test_crash_leaves_event_unprocessed_with_type_only(engine, monkeypatch):
    add_order(engine)

    def boom(*a):
        raise RuntimeError("SECRET-DETAIL")
    monkeypatch.setattr(prevention, "score_tabular", boom)
    done = await handle(engine, STUB, "stuck_at_depot", 16)
    assert done["processed_at"] is None and done["attempts"] == 1 and done["last_error"] == "RuntimeError"
    assert "SECRET" not in str(all_events(engine)) and props(engine) == []
    for _ in range(2):
        await handle_tracking_event(engine, STUB, get_ev(engine, done["id"]))
    assert get_ev(engine, done["id"])["processed_at"] is not None  # gives up after MAX_ATTEMPTS


async def test_success_clears_an_earlier_error(engine, monkeypatch):
    add_order(engine)
    risk_is(monkeypatch, 0.0)
    ev = add_ev(engine, "accepted", 1)
    with engine.begin() as conn:
        conn.execute(update(tracking_events).values(attempts=1, last_error="RuntimeError"))
    await handle_tracking_event(engine, STUB, get_ev(engine, ev["id"]))
    assert get_ev(engine, ev["id"])["last_error"] is None


async def test_backdated_event_after_delivery_creates_nothing(engine, monkeypatch):
    add_order(engine)
    risk_is(monkeypatch, 0.9)
    await handle(engine, STUB, "delivered", 18)
    late = await handle(engine, STUB, "stuck_at_depot", 16)
    assert props(engine) == [] and late["processed_at"] is not None and late["last_error"] is None


async def test_missing_tracking_number_creates_no_proposal(engine, monkeypatch):
    add_order(engine)
    risk_is(monkeypatch, 0.9)
    ev = add_ev(engine, "stuck_at_depot", 16)
    with engine.begin() as conn:
        conn.execute(update(tracking_events).values(tracking_number=""))
    await handle_tracking_event(engine, STUB, get_ev(engine, ev["id"]))
    done = get_ev(engine, ev["id"])
    assert props(engine) == [] and done["last_error"] == "no tracking number" and done["processed_at"] is not None


@pytest.mark.parametrize("value", [0, 51, -5])
def test_goodwill_share_is_bounded(value):
    from pydantic import ValidationError
    from api.config import Settings
    with pytest.raises(ValidationError):
        Settings(goodwill_share_pct=value)


# ---- default window: the real model on the demo timeline ----
DEMO = [("accepted", 1), ("in_transit", 3), ("stuck_at_depot", 5), ("stuck_at_depot", 9), ("stuck_at_depot", 12),
        ("stuck_at_depot", 16), ("out_for_delivery", 17), ("delivered", 18)]


def demo_scores(models, window):
    evs = [{"kind": k, "occurred_at": C + timedelta(days=d)} for k, d in DEMO]
    out = {}
    for (kind, day), ev in zip(DEMO, evs):
        f, _ = to_risk_features({"created_at": C, "amount_minor": 6000}, evs, ev, SimpleNamespace(est_window_days=window))
        out[(kind, day)] = score_tabular(models["risk"], RiskRequest(**f), "risk").score
    return out


def test_default_window_separates_stuck_from_on_time(models):
    s = demo_scores(models, settings.est_window_days)
    assert settings.est_window_days == 14.0
    early = [s[("accepted", 1)], s[("in_transit", 3)], s[("stuck_at_depot", 5)]]
    assert max(early) < settings.prevention_min_risk_early
    assert s[("stuck_at_depot", 16)] > settings.prevention_min_risk_late
    assert s[("stuck_at_depot", 16)] > max(early[:2])
    # chosen from 10, 14, 21: 10 puts the day-5 event over the early bar, so a parcel on time would be flagged
    assert demo_scores(models, 10)[("stuck_at_depot", 5)] >= settings.prevention_min_risk_early
    # 21 is also valid but separates less: the gap from the highest early score to the day-16 stuck score is wider at 14
    def gap(w):
        d = demo_scores(models, w)
        return d[("stuck_at_depot", 16)] - max(d[("accepted", 1)], d[("in_transit", 3)], d[("stuck_at_depot", 5)])
    assert gap(14) > gap(21)


# ---- runner, end to end with the real model ----
async def test_run_once_processes_tracking_events_in_date_order(engine, models, monkeypatch):
    monkeypatch.setattr(runner, "BACKOFF_SECONDS", 0)
    add_order(engine)
    late = add_ev(engine, "stuck_at_depot", 16)  # inserted first, dated later
    early = add_ev(engine, "accepted", 1)
    order = []
    real = runner.handle_tracking_event

    async def spy(eng, mod, event):
        order.append(event["id"])
        await real(eng, mod, event)
    monkeypatch.setattr(runner, "handle_tracking_event", spy)
    await runner.run_once(engine, models, None)
    assert order == [early["id"], late["id"]]
    assert all(e["processed_at"] is not None and e["last_error"] is None for e in all_events(engine))
    assert [p["action"] for p in props(engine)] == ["upload_tracking", "goodwill_refund"]
    assert props(engine)[0]["ev"]["risk"] > 0.10


async def test_run_once_survives_a_failing_tracking_pass(engine, models, monkeypatch):
    add_order(engine)
    add_ev(engine, "stuck_at_depot", 16)
    called = []

    async def boom(*a):
        raise RuntimeError("x")

    async def noop(*a):
        called.append(1)
    monkeypatch.setattr(runner, "handle_tracking_event", boom)
    monkeypatch.setattr(runner, "reconcile_pending", noop)
    await runner.run_once(engine, models, None)
    assert called == [1]  # the backstop still ran


async def test_run_once_waits_out_the_backoff(engine, models):
    add_order(engine)
    with engine.begin() as conn:
        conn.execute(insert(tracking_events).values(
            paypal_order_id="ORD1", kind="stuck_at_depot", occurred_at=C + timedelta(days=16), attempts=1,
            created_at=datetime.now(timezone.utc)))
    await runner.run_once(engine, models, None)
    assert props(engine) == [] and all_events(engine)[0]["processed_at"] is None


# ---- simulator ----
def sim_run(engine, argv, capsys):
    code = sim.run(sim.parse_args(argv), engine)
    return code, capsys.readouterr()


def test_simulator_adds_event_dated_from_order(engine, capsys):
    add_order(engine)
    code, out = sim_run(engine, ["ORD1", "accepted", "--day", "3"], capsys)
    assert code == 0
    [e] = all_events(engine)
    assert e["occurred_at"].replace(tzinfo=timezone.utc) == C + timedelta(days=3) and e["kind"] == "accepted"
    assert sim.TRACKING_NUMBER.fullmatch(e["tracking_number"]) and e["tracking_number"].startswith("DEMO")
    assert len(e["tracking_number"]) == 14 and e["carrier"] == "OTHER" and e["processed_at"] is None
    assert e["tracking_number"] in out.out


def test_simulator_reuses_the_tracking_number_per_order(engine, capsys):
    add_order(engine)
    add_order(engine, "ORD2")
    for oid, kind in (("ORD1", "accepted"), ("ORD1", "in_transit"), ("ORD2", "accepted")):
        sim_run(engine, [oid, kind, "--day", "1"], capsys)
    numbers = {(e["paypal_order_id"], e["tracking_number"]) for e in all_events(engine)}
    assert len(numbers) == 2


def test_simulator_refuses_unknown_and_unfinished_orders(engine, capsys):
    add_order(engine, status="PENDING")
    code, out = sim_run(engine, ["NOPE", "accepted", "--day", "1"], capsys)
    assert code == 1 and "no order NOPE" in out.err
    code, out = sim_run(engine, ["ORD1", "accepted", "--day", "1"], capsys)
    assert code == 1 and "not COMPLETED" in out.err and all_events(engine) == []


def test_simulator_refuses_events_after_delivery(engine, capsys):
    add_order(engine)
    assert sim_run(engine, ["ORD1", "delivered", "--day", "18"], capsys)[0] == 0
    for kind in ("delivered", "stuck_at_depot"):
        code, out = sim_run(engine, ["ORD1", kind, "--day", "16"], capsys)
        assert code == 1 and "already delivered" in out.err
    assert len(all_events(engine)) == 1


def test_simulator_refuses_a_malformed_stored_number(engine, capsys):
    add_order(engine)
    ev = add_ev(engine, "accepted", 1)
    with engine.begin() as conn:
        conn.execute(update(tracking_events).values(tracking_number="bad number"))
    code, out = sim_run(engine, ["ORD1", "in_transit", "--day", "2"], capsys)
    assert code == 1 and "malformed" in out.err and len(all_events(engine)) == 1


@pytest.mark.parametrize("argv", [
    ["ORD1", "teleported", "--day", "1"], ["ORD1", "accepted"], ["ORD1", "accepted", "--day", "61"],
    ["ORD1", "accepted", "--day", "-1"], ["ORD1", "accepted", "--day", "1.5"], ["ORD1", "accepted", "--day", "x"],
    ["ORD/1", "accepted", "--day", "1"], ["a" * 65, "accepted", "--day", "1"], ["ORD1\n", "accepted", "--day", "1"],
    ["ORD1", "accepted", "--day", "١"], ["list", "bad id"],
])
def test_simulator_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        sim.parse_args(argv)
    assert exc.value.code == 2


def test_simulator_day_bounds_and_list(engine, capsys):
    add_order(engine)
    assert sim_run(engine, ["list", "ORD1"], capsys)[1].out == "no events\n"
    for day in ("0", "60"):
        assert sim_run(engine, ["ORD1", "accepted", "--day", day], capsys)[0] == 0
    code, out = sim_run(engine, ["list", "ORD1"], capsys)
    assert code == 0 and out.out.count("accepted") == 2 and "waiting" in out.out


def test_simulator_main_needs_a_database_url(monkeypatch, capsys):
    monkeypatch.setattr(settings, "database_url", "")
    with pytest.raises(SystemExit) as exc:
        sim.main(["ORD1", "accepted", "--day", "1"])
    assert exc.value.code == 1 and "DATABASE_URL" in capsys.readouterr().err


# ---- prevention proposals cannot be approved yet ----
def test_prevention_proposal_is_listed_but_cannot_be_approved(monkeypatch):
    monkeypatch.setattr(settings, "paypal_client_id", "client-id")
    monkeypatch.setattr(settings, "paypal_client_secret", "client-secret")
    monkeypatch.setattr(settings, "admin_token", TOKEN)
    auth = {"X-Admin-Token": TOKEN}
    with TestClient(app) as c:  # no PayPal mock: any outgoing call would fail the test
        with c.app.state.engine.begin() as conn:
            pid = conn.execute(insert(proposals).values(
                agent="prevention", subject_id="ORD1:upload_tracking", action="upload_tracking", draft="d",
                created_at=C)).inserted_primary_key[0]
        r = c.post(f"/api/proposals/{pid}/approve", headers=auth)
        assert r.status_code == 409 and r.json()["detail"] == "this proposal type cannot be sent yet"
        assert c.get(f"/api/proposals/{pid}", headers=auth).json()["status"] == "PROPOSED"
        assert c.get("/api/proposals", headers=auth).json()[0]["agent"] == "prevention"
        assert c.post("/api/proposals/999/approve", headers=auth).status_code == 404
        assert c.post(f"/api/proposals/{pid}/reject", headers=auth).json()["status"] == "REJECTED"
