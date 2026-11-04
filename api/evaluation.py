"""Everything the Evaluation page draws, read from the saved model cards and the benchmark file
(CONTEXT.md: every chart reads from model cards). Public and read-only: no buyer or seller data in here."""
import json
from pathlib import Path

from fastapi import APIRouter

from agents.policies import decide
from api.config import settings

ART = Path(__file__).resolve().parent.parent / "ml" / "artifacts"
router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])
SAMPLE_VALUE_MINOR = 10_000  # the fight-vs-offer chart is drawn for a 100.00 dispute


def _json(name: str):
    return json.loads((ART / name).read_text())


def _decision_curve() -> list[dict]:
    rows = []
    for i in range(0, 101, 2):
        d = decide(i / 100, SAMPLE_VALUE_MINOR, "CHARGEBACK", settings.dispute_fee_minor, settings.offer_share_pct)
        rows.append({"p": i / 100, **{k: v / 100 for k, v in d["ev"].items()}, "action": d["action"]})
    return rows


@router.get("")
def evaluation():
    risk, outcome = _json("risk/model_card.json"), _json("outcome/model_card.json")
    return {
        "risk": {
            "calibration": _json("risk/calibration.json")["test"],
            "metrics": risk["test_calibrated"],
            "thresholds": _json("risk/threshold_table.json")["by_stage"],
        },
        "outcome": {"calibration": _json("outcome/calibration.json")["test"], "metrics": outcome["test"]["calibrated"]},
        "benchmark": _json("benchmark.json"),
        "decision": {"value": SAMPLE_VALUE_MINOR / 100, "fee": settings.dispute_fee_minor / 100,
                     "offer_share_pct": settings.offer_share_pct, "curve": _decision_curve()},
    }
