"""Inference side of the dispute-outcome model (T08): probability the seller wins a dispute.
Same artefact layout and dependencies as ml/risk_model.py, so the API loads both the same way."""
import json
from pathlib import Path

import lightgbm as lgb
import pandas as pd

from ml.risk_model import Calibrator, RiskModel

ARTIFACTS = Path(__file__).parent / "artifacts" / "outcome"
CATEGORICAL = ["claim_type", "lifecycle_stage", "category"]


class OutcomeModel(RiskModel):
    """`categories` maps each categorical feature to its training levels."""

    def __init__(self, booster: lgb.Booster, calibrator: Calibrator, features: list[str], categories: dict[str, list[str]]):
        self.booster, self.calibrator, self.features = booster, calibrator, features
        self.dtypes = {c: pd.CategoricalDtype(levels) for c, levels in categories.items()}

    @classmethod
    def load(cls, directory: Path = ARTIFACTS) -> "OutcomeModel":
        meta = json.loads((directory / "features.json").read_text())
        return cls(
            lgb.Booster(model_file=str(directory / "model.txt")),
            Calibrator.from_json((directory / "calibrator.json").read_text()),
            meta["features"],
            meta["categories"],
        )

    def _frame(self, df: pd.DataFrame) -> pd.DataFrame:
        X = df[self.features].copy()
        for c, dtype in self.dtypes.items():
            X[c] = X[c].astype(dtype)  # unseen levels become NaN
        for c in X.columns.difference(list(self.dtypes)):
            X[c] = X[c].astype(float)  # bool and int columns
        return X
