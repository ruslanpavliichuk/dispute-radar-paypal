"""Claim classifier (T09): fastembed + logistic regression on weak-labelled CFPB narratives and synthetic messages.

    uv run python -m ml.train_claims

Splits each source 80/20 by a hash of the text (CFPB rows are held out as a group, never both sides).
Reports macro F1 and a confusion matrix on the CFPB test split (weak labels), the synthetic test split, and
a hand-checked sample of 200 CFPB test rows. That sample is checked in two steps (the developer's time is
the scarce part): a second, blind LLM labels all 200 (reviewer_labels.csv); the developer then labels only the rows
where it disagrees with the weak label (adjudicate.csv, `human_label` column). A row's truth is the human label when
there is one, otherwise the label both LLMs agree on. The model card counts each kind so nobody reads this as 200 human labels.
"""
import hashlib
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import mlflow  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import cohen_kappa_score, confusion_matrix, f1_score  # noqa: E402
from sklearn.model_selection import cross_val_predict  # noqa: E402

from ml.claim_model import ARTIFACTS, EMBEDDER, ClaimClassifier, embed_texts  # noqa: E402
from ml.label_claims import CLASSES, OUT as DATA  # noqa: E402
from ml.train_risk import ROOT, git_commit  # noqa: E402

REVIEWER = ARTIFACTS / "reviewer_labels.csv"  # complaint_id, text, reviewer_label (blind second LLM)
ADJUDICATE = ARTIFACTS / "adjudicate.csv"  # complaint_id, text, human_label: only rows where reviewer != weak label
N_HANDCHECK = 200
C_GRID = [0.3, 1, 3, 10, 30]


def is_test(text: str) -> bool:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16) % 5 == 0  # deterministic 20%


def handcheck_sample(df: pd.DataFrame) -> pd.DataFrame:
    """The 200 CFPB test rows that get checked; fixed seed so the sample never changes under the labels."""
    test = df[df["test"] & (df["source"] == "cfpb_weak")]
    return test.sample(min(N_HANDCHECK, len(test)), random_state=0)[["complaint_id", "text"]]


def build_truth(weak: pd.Series, reviewer: pd.DataFrame, adjudicated: pd.DataFrame | None) -> pd.DataFrame:
    """One row per checked text: its truth and where the truth came from ("human" or "both_llms").
    Rows where the LLMs disagree and the human has not answered yet are left out."""
    human = {}
    if adjudicated is not None:
        human = {i: h for i, h in zip(adjudicated["complaint_id"], adjudicated["human_label"]) if h in CLASSES}
    rows = []
    for i, text, rev in zip(reviewer["complaint_id"], reviewer["text"], reviewer["reviewer_label"]):
        if i in human:
            rows.append((i, text, human[i], "human"))
        elif rev == weak[i]:
            rows.append((i, text, rev, "both_llms"))
    return pd.DataFrame(rows, columns=["complaint_id", "text", "truth", "source"])


def report(y_true, y_pred) -> dict:
    return {
        "n": int(len(y_true)), "macro_f1": float(f1_score(y_true, y_pred, labels=CLASSES, average="macro", zero_division=0)),
        "per_class_f1": dict(zip(CLASSES, map(float, f1_score(y_true, y_pred, labels=CLASSES, average=None, zero_division=0)))),
        "confusion_matrix": {"labels": CLASSES, "rows_true_cols_pred": confusion_matrix(y_true, y_pred, labels=CLASSES).tolist()},
    }


def plot_confusion(cm: dict, title: str, path) -> None:
    m = np.array(cm["rows_true_cols_pred"])
    fig, ax = plt.subplots(figsize=(5.5, 5))
    ax.imshow(m, cmap="Blues")
    ax.set(xticks=range(len(CLASSES)), yticks=range(len(CLASSES)), xticklabels=CLASSES, yticklabels=CLASSES,
           xlabel="predicted", ylabel="true", title=title)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    for i, j in np.ndindex(m.shape):
        ax.text(j, i, m[i, j], ha="center", va="center", color="white" if m[i, j] > m.max() / 2 else "black")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main() -> None:
    df = pd.read_parquet(DATA)
    df["complaint_id"] = df["complaint_id"].astype("Int64").astype("string")  # stored as float; keep ids exact
    df["test"] = df["text"].map(is_test)
    X = embed_texts(df["text"].tolist())
    y = df["label"].to_numpy()
    tr = ~df["test"].to_numpy()

    # choose C by 5-fold cross-validated macro F1 on the training rows only
    def fit(C):
        return LogisticRegression(C=C, class_weight="balanced", max_iter=2000)

    cv = {C: f1_score(y[tr], cross_val_predict(fit(C), X[tr], y[tr], cv=5), average="macro") for C in C_GRID}
    best_C = max(cv, key=cv.get)
    clf = fit(best_C).fit(X[tr], y[tr])
    assert list(clf.classes_) == sorted(CLASSES)
    order = [list(clf.classes_).index(c) for c in CLASSES]  # store in CLASSES order
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "weights.json").write_text(json.dumps({
        "classes": CLASSES, "embedder": EMBEDDER, "coef": clf.coef_[order].tolist(), "intercept": clf.intercept_[order].tolist()}))

    loaded = ClaimClassifier.load(ARTIFACTS)
    te = df[df["test"]]
    pred = np.array(CLASSES)[loaded.proba(te["text"].tolist()).argmax(axis=1)]
    assert (pred == clf.predict(X[~tr])).mean() > 0.999, "round trip changed predictions"
    results = {}
    for src in ("cfpb_weak", "synthetic"):
        sel = (te["source"] == src).to_numpy()
        results[f"{src}_test"] = report(te["label"].to_numpy()[sel], pred[sel])

    weak = te[te["source"] == "cfpb_weak"].set_index("complaint_id")["label"]
    hand = None
    if REVIEWER.exists():
        reviewer = pd.read_csv(REVIEWER, dtype=str, keep_default_na=False)
        if not ADJUDICATE.exists():
            disagree = reviewer[reviewer["reviewer_label"] != reviewer["complaint_id"].map(weak)]
            disagree[["complaint_id", "text"]].assign(human_label="").to_csv(ADJUDICATE, index=False)
            print(f"wrote {ADJUDICATE}: {len(disagree)} rows; fill human_label with one of {CLASSES}, then rerun")
        adjudicated = pd.read_csv(ADJUDICATE, dtype=str, keep_default_na=False)
        open_rows = int((~adjudicated["human_label"].isin(CLASSES)).sum())
        if open_rows:
            print(f"adjudicate.csv: {open_rows}/{len(adjudicated)} rows still unlabelled; they are left out of the hand-checked score")
        truth = build_truth(weak, reviewer, adjudicated)
        if len(truth):
            w = truth["complaint_id"].map(weak)
            hand = {
                "n": int(len(truth)), "from_human": int((truth["source"] == "human").sum()),
                "from_two_llms_agreeing": int((truth["source"] == "both_llms").sum()), "unlabelled_disagreements": open_rows,
                "weak_label_agreement": float((w == truth["truth"]).mean()),
                "weak_label_kappa": float(cohen_kappa_score(w, truth["truth"])),
                "model_vs_truth": report(truth["truth"].to_numpy(), loaded.predict(truth["text"].tolist())),
            }
            results["hand_checked"] = hand["model_vs_truth"]

    data_hash = hashlib.sha256(DATA.read_bytes()).hexdigest()[:16]
    card = {
        "model": f"{EMBEDDER} + LogisticRegression(C={best_C}, balanced)", "git_commit": git_commit(), "data_hash": data_hash,
        "classes": CLASSES, "cv_macro_f1_by_C": {str(k): float(v) for k, v in cv.items()},
        "rows": df.groupby(["source", "label"]).size().unstack(fill_value=0).to_dict("index"),
        "test": results, "weak_label_check": hand,
        "notes": [
            "CFPB labels come from an LLM (weak labels), not a human. The cfpb_weak_test score measures agreement with that LLM, "
            "not correctness. `hand_checked` is against truth built from: the developer's label on every row where a second blind LLM "
            "disagreed with the weak label, and the shared label where both LLMs agreed. Rows both LLMs got wrong in the same way "
            "are not caught, so it can overstate accuracy."
            if hand else "NOT YET HAND-CHECKED: scores below are agreement with the LLM weak labels, not with a human.",
            "CFPB narratives are US credit-card complaints; the product sees UK marketplace buyer messages, so the synthetic "
            "messages (LLM-written, labelled by construction) are the closest proxy and are easier than real text.",
            "Narratives are truncated to the embedder's 512-token limit; CFPB redaction tokens (XXXX) are left in.",
        ],
    }
    (ARTIFACTS / "model_card.json").write_text(json.dumps(card, indent=2) + "\n")
    plot_confusion(results["cfpb_weak_test"]["confusion_matrix"], "Claim classifier, CFPB test (LLM labels)", ARTIFACTS / "confusion_matrix.png")

    mlflow.set_tracking_uri(f"sqlite:///{ROOT.parent / 'mlflow.db'}")
    mlflow.set_experiment("claims")
    with mlflow.start_run(run_name="claims"):
        mlflow.log_params({"C": best_C, "data_hash": data_hash})
        mlflow.log_metrics({f"{k}_macro_f1": v["macro_f1"] for k, v in results.items()})
        for f in ARTIFACTS.iterdir():
            mlflow.log_artifact(str(f))
    for k, v in results.items():
        print(k, "n", v["n"], "macro F1", round(v["macro_f1"], 3), {c: round(f, 2) for c, f in v["per_class_f1"].items()})
    if hand:
        print("weak-label agreement with human:", round(hand["weak_label_agreement"], 3), "kappa", round(hand["weak_label_kappa"], 3))


if __name__ == "__main__":
    main()
