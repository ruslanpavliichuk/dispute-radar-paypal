"""Inference side of the dispute-risk model (T06). Needs only lightgbm, numpy and pandas:
no scikit-learn, MLflow or pickle, so the API can load it inside Render's memory limit.
"""
import json
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ARTIFACTS = Path(__file__).parent / "artifacts" / "risk"


@dataclass(frozen=True)
class Calibrator:
    """Isotonic regression stored as its step thresholds; np.interp reproduces
    sklearn's IsotonicRegression(out_of_bounds="clip") exactly."""

    x: np.ndarray
    y: np.ndarray

    @classmethod
    def from_isotonic(cls, iso) -> "Calibrator":
        return cls(np.asarray(iso.X_thresholds_, dtype=float), np.asarray(iso.y_thresholds_, dtype=float))

    def __call__(self, raw) -> np.ndarray:
        return np.interp(raw, self.x, self.y)

    def to_json(self) -> str:
        return json.dumps({"x": self.x.tolist(), "y": self.y.tolist()})

    @classmethod
    def from_json(cls, text: str) -> "Calibrator":
        d = json.loads(text)
        return cls(np.asarray(d["x"], dtype=float), np.asarray(d["y"], dtype=float))


def decide(risk: float, order_value: float, action_cost: float) -> bool:
    """Act when the expected loss avoided exceeds the action's cost (CONTEXT.md 7.3).
    Email and tracking upload cost 0, so they always act; a goodwill refund costs its value."""
    return action_cost == 0 or risk * order_value > action_cost


class RiskModel:
    def __init__(self, booster: lgb.Booster, calibrator: Calibrator, features: list[str], categories: list[str]):
        self.booster, self.calibrator, self.features = booster, calibrator, features
        self.category_dtype = pd.CategoricalDtype(categories)

    @classmethod
    def load(cls, directory: Path = ARTIFACTS) -> "RiskModel":
        meta = json.loads((directory / "features.json").read_text())
        return cls(
            lgb.Booster(model_file=str(directory / "model.txt")),
            Calibrator.from_json((directory / "calibrator.json").read_text()),
            meta["features"],
            meta["categories"],
        )

    def _frame(self, df: pd.DataFrame) -> pd.DataFrame:
        X = df[self.features].copy()
        X["category"] = X["category"].astype(self.category_dtype)  # unseen categories become NaN
        return X

    def raw(self, df: pd.DataFrame) -> np.ndarray:
        """Uncalibrated score: only its order is meaningful."""
        return self.booster.predict(self._frame(df))

    def score(self, df: pd.DataFrame) -> np.ndarray:
        """Calibrated probability that each snapshot's order becomes a dispute."""
        return self.calibrator(self.raw(df))

    def contributions(self, df: pd.DataFrame) -> np.ndarray:
        """Exact TreeSHAP per row and feature, in raw log-odds (before calibration)."""
        return self.booster.predict(self._frame(df), pred_contrib=True)[:, :-1]  # last column is the bias

    def explain(self, df: pd.DataFrame, top: int = 3) -> list[list[tuple[str, object, float]]]:
        """Per row: the `top` features pushing the raw score most, as (feature, value, contribution)."""
        X = self._frame(df)
        contrib = self.contributions(df)
        rows = []
        for i in range(len(X)):
            order = np.argsort(-np.abs(contrib[i]), kind="stable")[:top]
            rows.append([(self.features[j], X.iloc[i, j], float(contrib[i, j])) for j in order])
        return rows
