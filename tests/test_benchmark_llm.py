import json

import numpy as np
import pytest

from api.config import settings
from ml import benchmark_llm as b
from ml.generate_disputes import generate, load_rules
from ml.outcome_model import OutcomeModel
from ml.train_outcome import split


@pytest.fixture(scope="module")
def df():
    return generate(load_rules(), n=4000)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(b, "PAUSE", 0)
    monkeypatch.setattr(settings, "llm_provider", "gemini")


def one(doc):
    return next(iter(doc["runs"].values()))


def fake(monkeypatch, df, answer):
    """Fake LLM: `answer(true_win_prob)` for the dispute named in the prompt's facts."""
    calls = []
    lookup = {b.describe(r): r for r in df.to_dict("records")}

    def complete(text, schema, **kw):
        calls.append(text)
        r = next(r for d, r in lookup.items() if d in text)
        return answer(r["true_win_prob"])

    monkeypatch.setattr(b, "complete_json", complete)
    return calls


def test_describe_has_every_field_and_no_label():
    r = generate(load_rules(), n=5).iloc[0].to_dict()
    text = b.describe(r)
    for v in (r["order_value"], r["response_days"], r["seller_account_age_months"], r["category"]):
        assert (f"{v:.2f}" if isinstance(v, float) else str(v)) in text
    assert "INQUIRY" not in text or r["lifecycle_stage"] == "INQUIRY"
    for word in ("tracking", "delivery", "signature", "PayPal transaction address", "photos", "chargeback" if r["lifecycle_stage"] == "CHARGEBACK" else "inquiry"):
        assert word in text.lower() or word in text
    assert not {"won", "true_win_prob", "outcome_code"} & set(text.split())
    assert str(r["true_win_prob"]) not in b.prompt(r)


def test_rules_leak_nothing_about_the_generator():
    rules = load_rules()
    low = b.RULES.lower()
    assert "logit" not in low and "generator" not in low
    nums = {str(abs(t["weight"])) for c in rules["claim_types"].values() for t in c["terms"]} | {str(abs(c["base_logit"])) for c in rules["claim_types"].values()}
    assert not [n for n in nums if "." in n and n in b.RULES]
    assert not [c for c in rules["categories"] if c != "other" and c in low]
    assert 6 <= len(b.RULES.splitlines()) <= 10


def test_rows_are_the_models_held_out_rows_and_reproducible(df, monkeypatch, tmp_path):
    fake(monkeypatch, df, lambda p: {"win_probability": 0.5, "reasoning": ""})
    parts = split(df)
    seen = []
    orig = b.ask
    monkeypatch.setattr(b, "ask", lambda rows, cache: (seen.append([r["dispute_id"] for r in rows]), orig(rows, cache))[1])
    b.run(df, 40, 0, tmp_path), b.run(df, 40, 0, tmp_path / "again"), b.run(df, 40, 1, tmp_path / "other")
    assert seen[0] == seen[1] != seen[2] and len(set(seen[0])) == 40
    for k in ("train", "valA", "valB"):
        assert not set(seen[0]) & set(parts[k]["dispute_id"])
    assert set(seen[0]) <= set(parts["test"]["dispute_id"])


def test_oracle_llm_matches_oracle_and_constant_llm_scores_a_quarter(df, monkeypatch, tmp_path):
    fake(monkeypatch, df, lambda p: {"win_probability": float(p), "reasoning": ""})
    r = one(b.run(df, 300, 0, tmp_path))
    assert r["metrics"]["llm"]["brier"] == pytest.approx(r["metrics"]["oracle (true_win_prob)"]["brier"], abs=1e-9)
    fake(monkeypatch, df, lambda p: {"win_probability": 0.5, "reasoning": ""})
    r = one(b.run(df, 300, 0, tmp_path / "half"))
    assert r["metrics"]["llm"]["brier"] == pytest.approx(0.25)
    assert r["metrics"]["llm"]["brier"] > r["metrics"]["outcome_model"]["brier"]
    assert r["brier_diff_llm_minus_model"]["ci95"][0] > 0  # the 0.5 guesser is clearly worse


def test_percent_clipping_and_bad_answers_are_counted_and_non_dicts_not_cached(df, monkeypatch, tmp_path):
    answers = iter([{"win_probability": v} for v in (1.0, 0.0, 0.7, 70, 150, -0.2, float("nan"), "high")] + [["not a dict"]])
    calls = fake(monkeypatch, df, lambda p: next(answers, {"win_probability": 0.6}))
    r = one(b.run(df, 60, 0, tmp_path))
    assert (r["n_out_of_range_clipped"], r["n_rescaled_percent"], r["n_non_numeric"], r["n_failed"]) == (2, 1, 5, 0)
    assert r["n_scored"] == 55
    assert 0 < r["metrics"]["llm"]["log_loss"] < 10  # 1.0 and 0.0 were clipped
    cache = tmp_path / "benchmark" / "llm_cache_gemini_gemini-3.1-flash-lite.jsonl"
    assert len(calls) == 60 and len(cache.read_text().splitlines()) == 59  # the non-dict reply is retried next run


def test_cache_and_run_key_follow_the_model_name_and_runs_merge(df, monkeypatch, tmp_path):
    calls = fake(monkeypatch, df, lambda p: {"win_probability": 0.6, "reasoning": ""})
    monkeypatch.setattr(settings, "llm_model", "m-a")
    b.run(df, 30, 0, tmp_path)
    monkeypatch.setattr(settings, "llm_model", "org/m b")
    doc = b.run(df, 30, 0, tmp_path)
    assert len(calls) == 60  # the second model did not reuse the first model's answers
    assert sorted(f.name for f in (tmp_path / "benchmark").iterdir()) == ["llm_cache_gemini_m-a.jsonl", "llm_cache_gemini_org_m_b.jsonl"]
    assert set(doc["runs"]) == {"gemini:m-a", "gemini:org/m b"} == set(json.loads((tmp_path / "benchmark.json").read_text())["runs"])
    assert "LLM gemini:m-a" in b.table(doc) and "LLM gemini:org/m b" in b.table(doc)
    assert (tmp_path / "benchmark_calibration.png").stat().st_size > 0
    # re-running one model replaces only its own entry
    fake(monkeypatch, df, lambda p: {"win_probability": 0.9, "reasoning": ""})
    monkeypatch.setattr(settings, "llm_model", "m-a")
    (tmp_path / "benchmark" / "llm_cache_gemini_m-a.jsonl").unlink()
    again = b.run(df, 30, 0, tmp_path)
    assert again["runs"]["gemini:org/m b"] == doc["runs"]["gemini:org/m b"]
    assert again["runs"]["gemini:m-a"]["metrics"]["llm"]["mean_predicted"] == pytest.approx(0.9)
    # other rows (new seed) make the old runs incomparable, so they are dropped
    assert set(b.run(df, 30, 1, tmp_path)["runs"]) == {"gemini:m-a"}


def test_every_predictor_is_clipped_and_ece_uniform_by_hand(df, monkeypatch, tmp_path):
    fake(monkeypatch, df, lambda p: {"win_probability": 0.6})

    class Zero(OutcomeModel):
        def __init__(self): pass
        def score(self, d): return np.zeros(len(d))

    m = one(b.run(df, 40, 0, tmp_path, model=Zero()))["metrics"]
    assert m["outcome_model"]["mean_predicted"] == pytest.approx(0.01) and np.isfinite(m["outcome_model"]["log_loss"])
    o = b._metrics(np.array([1, 0]), np.array([1.0, 0.0]))  # oracle-style extremes
    assert o["mean_predicted"] == pytest.approx(0.5) and np.isfinite(o["log_loss"]) and o["brier"] == pytest.approx(0.0001)
    assert b.ece_uniform(np.array([1, 0, 1, 1]), np.array([0.05, 0.05, 0.95, 0.95])) == pytest.approx(0.25)


def test_failed_rows_are_excluded_and_counted_and_both_models_use_the_same_rows(df, monkeypatch, tmp_path):
    n = {"i": 0}

    def answer(p):
        n["i"] += 1
        if n["i"] % 4 == 0:
            raise RuntimeError("rate limited")
        return {"win_probability": 0.6, "reasoning": ""}

    fake(monkeypatch, df, answer)
    scored = []

    class Spy(OutcomeModel):
        def __init__(self): pass
        def score(self, d):
            scored.append(len(d))
            return np.full(len(d), 0.6)

    r = one(b.run(df, 40, 0, tmp_path, model=Spy()))
    assert (r["n_failed"], r["n_scored"]) == (10, 30) and scored == [30]
    assert r["metrics"]["outcome_model"]["mean_predicted"] == pytest.approx(0.6)


def test_cache_means_a_second_run_makes_no_calls_and_failures_are_retried(df, monkeypatch, tmp_path):
    calls = fake(monkeypatch, df, lambda p: {"win_probability": 0.6, "reasoning": ""})
    first = one(b.run(df, 30, 0, tmp_path))
    assert len(calls) == 30
    second = one(b.run(df, 30, 0, tmp_path))
    assert len(calls) == 30 and second["metrics"]["llm"] == first["metrics"]["llm"]


def test_outputs_have_expected_keys_and_the_table_prints(df, monkeypatch, tmp_path):
    fake(monkeypatch, df, lambda p: {"win_probability": float(p), "reasoning": ""})
    b.run(df, 50, 0, tmp_path)
    saved = json.loads((tmp_path / "benchmark.json").read_text())
    assert {"n", "seed", "git_commit", "data_hash", "rules_hash", "clip", "notes", "caveat", "runs"} <= set(saved)
    r = one(saved)
    assert {"provider", "model", "n_scored", "n_failed", "n_rescaled_percent", "metrics", "calibration", "brier_diff_llm_minus_model"} <= set(r)
    assert {"llm", "outcome_model", "constant (train win rate)", "oracle (true_win_prob)"} == set(r["metrics"])
    assert "ece_uniform" in r["metrics"]["llm"] and "general public knowledge" in saved["caveat"]
    assert (tmp_path / "benchmark_calibration.png").stat().st_size > 0
    t = b.table(saved)
    assert "| LLM gemini:" in t and "| oracle" in t and "not real PayPal" in t
