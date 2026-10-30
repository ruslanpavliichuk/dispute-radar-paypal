"""Inference side of the claim classifier (T09): fastembed embeddings + a logistic regression stored as JSON.
Needs only fastembed and numpy (no scikit-learn or pickle), like the other two models."""
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np

ARTIFACTS = Path(__file__).parent / "artifacts" / "claims"
EMBEDDER = "BAAI/bge-small-en-v1.5"


@lru_cache(maxsize=2)
def _embedder(model: str):
    from fastembed import TextEmbedding  # imported here: loading it is slow and tests inject their own embedder

    return TextEmbedding(model)


def embed_texts(texts: list[str], model: str = EMBEDDER) -> np.ndarray:
    return np.array(list(_embedder(model).embed(texts)))


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

    def explain(self, text: str, top: int = 3, chunks: int = 12) -> list[dict]:
        """Which stretches of `text` carry the predicted label: drop each of up to `chunks` consecutive word groups
        and measure how far the predicted class's probability falls. A logistic regression on embeddings has no
        per-word weights, so this occlusion is the explanation (one batched embed call, not one per word)."""
        words = text.split()
        if len(words) < 2:
            return []
        size = math.ceil(len(words) / min(chunks, len(words)))
        groups = [words[i : i + size] for i in range(0, len(words), size)]
        without = [" ".join(w for j, g in enumerate(groups) if j != i for w in g) for i in range(len(groups))]
        base = self.proba([text])[0]
        label = int(base.argmax())
        drops = base[label] - self.proba(without)[:, label]
        return [{"text": " ".join(groups[i]), "contribution": float(drops[i])} for i in np.argsort(-drops, kind="stable")[:top]]
