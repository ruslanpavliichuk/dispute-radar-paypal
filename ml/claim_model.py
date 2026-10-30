"""Inference side of the claim classifier (T09): fastembed embeddings + a logistic regression stored as JSON.
Needs only fastembed and numpy (no scikit-learn or pickle), like the other two models."""
import json
from pathlib import Path

import numpy as np

ARTIFACTS = Path(__file__).parent / "artifacts" / "claims"
EMBEDDER = "BAAI/bge-small-en-v1.5"


def embed_texts(texts: list[str], model: str = EMBEDDER) -> np.ndarray:
    from fastembed import TextEmbedding  # imported here: loading it is slow and tests inject their own embedder

    return np.array(list(TextEmbedding(model).embed(texts)))


class ClaimClassifier:
    def __init__(self, classes: list[str], coef: np.ndarray, intercept: np.ndarray, embed=embed_texts):
        self.classes, self.coef, self.intercept, self.embed = classes, coef, intercept, embed

    @classmethod
    def load(cls, directory: Path = ARTIFACTS) -> "ClaimClassifier":
        w = json.loads((directory / "weights.json").read_text())
        return cls(w["classes"], np.asarray(w["coef"]), np.asarray(w["intercept"]), lambda t: embed_texts(t, w["embedder"]))

    def proba(self, texts: list[str]) -> np.ndarray:
        """Class probabilities, one row per text, columns in `self.classes` order."""
        z = self.embed(texts) @ self.coef.T + self.intercept
        e = np.exp(z - z.max(axis=1, keepdims=True))
        return e / e.sum(axis=1, keepdims=True)

    def predict(self, texts: list[str]) -> list[str]:
        return [self.classes[i] for i in self.proba(texts).argmax(axis=1)]
