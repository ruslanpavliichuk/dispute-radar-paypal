"""Model service (T10): POST /score/risk, /score/outcome, /score/claim. Each returns the score plus the top 3
reasons. Artefacts load once at startup (api.main lifespan) and the endpoints only read them."""
import logging
from typing import Literal

import pandas as pd
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ml.claim_model import ClaimClassifier
from ml.outcome_model import OutcomeModel
from ml.risk_model import RiskModel

logger = logging.getLogger("dispute_radar.scoring")
router = APIRouter(prefix="/score", tags=["score"])
Nonneg = Field(ge=0, le=1e7)
Days = Field(ge=-1000, le=1000)


def load_models() -> dict:
    return {"risk": RiskModel.load(), "outcome": OutcomeModel.load(), "claim": ClaimClassifier.load()}


class Request_(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskRequest(Request_):
    """One order at one moment, in the units of ml/features/risk.py. Fields the model handles as missing are optional."""
    order_value: float = Nonneg
    freight_share: float = Field(ge=0, le=100)
    category: str = Field(max_length=100)
    n_items: int = Field(ge=1, le=1000)
    est_window_days: float = Days
    same_state: bool
    seller_late_rate: float | None = Field(None, ge=0, le=1)  # None: no seller history yet
    seller_low_review_rate: float | None = Field(None, ge=0, le=1)
    days_since_purchase: float = Days
    days_to_est: float = Days
    delivered_yet: bool
    days_late_so_far: float = Days
    shipped_yet: bool
    days_since_ship: float | None = Field(None, ge=0, le=1000)  # None: not shipped yet


class OutcomeRequest(Request_):
    """One dispute, in the units of ml/generate_disputes.py. Mirrors the PayPal dispute fields the agent can fill."""
    claim_type: Literal["not_received", "not_as_described", "unauthorised", "other"]
    lifecycle_stage: Literal["INQUIRY", "CHARGEBACK"]
    order_value: float = Field(ge=0, le=1e7)
    tracking_uploaded: bool
    delivery_confirmed: bool
    signature_confirmed: bool
    shipped_to_paypal_address: bool
    photos_supplied: bool
    response_days: int = Field(ge=0, le=365)
    seller_account_age_months: int = Field(ge=0, le=1200)
    category: str = Field(max_length=100)


class ClaimRequest(Request_):
    text: str = Field(min_length=1, max_length=5000)


class Reason(BaseModel):
    feature: str
    value: float | str | bool | None
    contribution: float  # raw log-odds, before calibration: only its sign and size relative to the others mean something


class TabularScore(BaseModel):
    score: float
    reasons: list[Reason]


class ClaimScore(BaseModel):
    label: str
    scores: dict[str, float]
    reasons: list[dict]  # {"text": the stretch of message, "contribution": probability drop when removed}


def _tabular(model, body: BaseModel, name: str) -> TabularScore:
    df = pd.DataFrame([body.model_dump()])
    numeric = df.columns.difference(list(model.dtypes))
    df[numeric] = df[numeric].astype(float)  # None becomes NaN (an object column would be rejected), flags become 0/1
    for col, dtype in model.dtypes.items():
        if df[col][0] not in dtype.categories:  # the model scores unseen levels as missing; make that visible
            logger.warning("%s: unseen %s %r scored as missing", name, col, df[col][0])
    reasons = [Reason(feature=f, value=None if pd.isna(v) else (v if isinstance(v, str) else float(v)), contribution=c)
               for f, v, c in model.explain(df)[0]]
    return TabularScore(score=float(model.score(df)[0]), reasons=reasons)


@router.post("/risk", response_model=TabularScore)
def score_risk(body: RiskRequest, request: Request) -> TabularScore:
    return _tabular(request.app.state.models["risk"], body, "risk")


@router.post("/outcome", response_model=TabularScore)
def score_outcome(body: OutcomeRequest, request: Request) -> TabularScore:
    return _tabular(request.app.state.models["outcome"], body, "outcome")


@router.post("/claim", response_model=ClaimScore)
def score_claim(body: ClaimRequest, request: Request) -> ClaimScore:
    clf: ClaimClassifier = request.app.state.models["claim"]
    p = clf.proba([body.text])[0]
    return ClaimScore(label=clf.classes[int(p.argmax())], scores=dict(zip(clf.classes, map(float, p))),
                      reasons=clf.explain(body.text))
