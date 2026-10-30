"""Build the claim-classifier dataset (T09): weak labels for CFPB narratives and synthetic buyer messages.

    uv run python -m ml.label_claims            # needs LLM_PROVIDER + key in .env, see ml/llm.py

Writes ml/data/processed/claims.parquet (text, label, source, complaint_id). Both steps cache every
answer in a jsonl file next to it, so a rerun (or a rate-limit crash) resumes instead of paying again.
Weak labels come from the LLM, not a human: ml/train_claims.py reports agreement on a hand-checked sample.
"""
import json
import random
import time
from pathlib import Path

import pandas as pd

from ml.data.download_cfpb import OUT as CFPB
from ml.llm import complete_json

PROCESSED = Path(__file__).parent / "data" / "processed"
OUT = PROCESSED / "claims.parquet"
WEAK_CACHE, SYNTH_CACHE = PROCESSED / "claims_weak.jsonl", PROCESSED / "claims_synthetic.jsonl"
CLASSES = ["not_received", "not_as_described", "unauthorised", "other"]
N_WEAK, N_SYNTH_PER_CLASS = 3000, 375  # 1,500 synthetic messages in total
WEAK_BATCH, SYNTH_BATCH, MAX_CHARS, PAUSE = 10, 15, 1200, 1.0

MEANING = """not_received: the buyer says the item or service never arrived or was never delivered.
not_as_described: the buyer received something that differs from the description (wrong, faulty, damaged, counterfeit, wrong size).
unauthorised: the buyer says they did not make or authorise the purchase (fraud, stolen card, unknown charge).
other: anything else (refund delays, billing errors, cancelled orders, subscription or service disputes, unclear)."""

STYLES = ["short and angry", "polite and detailed", "confused and rambling", "brief, one line, lowercase",
          "formal, quotes dates and order numbers", "worried, mentions a family member", "sarcastic", "matter-of-fact"]


def _cached(path: Path) -> dict:
    return {json.loads(l)["key"]: json.loads(l)["value"] for l in path.read_text().splitlines()} if path.exists() else {}


def _append(path: Path, key, value) -> None:
    with path.open("a") as f:
        f.write(json.dumps({"key": key, "value": value}) + "\n")


def weak_label(df: pd.DataFrame) -> pd.DataFrame:
    schema = {"type": "array", "items": {"type": "object", "required": ["id", "label"], "properties": {
        "id": {"type": "integer"}, "label": {"type": "string", "enum": CLASSES}}}}
    done = _cached(WEAK_CACHE)
    todo = [r for r in df.itertuples() if r.complaint_id not in done]
    for i in range(0, len(todo), WEAK_BATCH):
        batch = todo[i : i + WEAK_BATCH]
        items = "\n".join(f"[{n}] {r.narrative[:MAX_CHARS]}" for n, r in enumerate(batch))
        answer = complete_json(
            f"A buyer disputed a card purchase. For each numbered complaint pick the single best claim type.\n{MEANING}\n\n{items}",
            schema)
        for a in answer:
            if 0 <= a["id"] < len(batch):  # ignore ids the model invented
                _append(WEAK_CACHE, batch[a["id"]].complaint_id, a["label"])
        print(f"weak labels {min(i + WEAK_BATCH, len(todo))}/{len(todo)}", flush=True)
        time.sleep(PAUSE)
    done = _cached(WEAK_CACHE)
    out = df[df["complaint_id"].isin(done)].copy()
    out["label"] = out["complaint_id"].map(done)
    return out.rename(columns={"narrative": "text"}).assign(source="cfpb_weak")


def synthesise() -> pd.DataFrame:
    schema = {"type": "array", "items": {"type": "string"}}
    done = _cached(SYNTH_CACHE)  # key "label:n" -> list of messages
    rng = random.Random(0)
    for label in CLASSES:
        for n in range(-(-N_SYNTH_PER_CLASS // SYNTH_BATCH)):
            key = f"{label}:{n}"
            style = rng.choice(STYLES)
            if key in done:
                continue
            msgs = complete_json(
                f"Write {SYNTH_BATCH} different messages that UK online-marketplace buyers send when opening a payment dispute.\n"
                f"Every message must be a clear case of this claim type, with no hint of the others.\n{MEANING}\n"
                f"Claim type: {label}. Style this time: {style}. Vary products, wording and length (1 to 4 sentences). "
                f"Use British spelling and pounds. Return only the messages.", schema, temperature=1.0)
            _append(SYNTH_CACHE, key, msgs[:SYNTH_BATCH])
            print(f"synthetic {key}", flush=True)
            time.sleep(PAUSE)
    done = _cached(SYNTH_CACHE)
    rows = [(m.strip(), key.split(":")[0]) for key, ms in done.items() for m in ms if m.strip()]
    df = pd.DataFrame(rows, columns=["text", "label"]).drop_duplicates("text")
    df = df.groupby("label").head(N_SYNTH_PER_CLASS)  # a class that overshot the quota is trimmed
    return df.assign(source="synthetic", complaint_id=None)


def main() -> None:
    cfpb = pd.read_parquet(CFPB)
    cfpb = cfpb[cfpb["narrative"].str.len() >= 40].sample(N_WEAK, random_state=0).reset_index(drop=True)
    weak = weak_label(cfpb)
    synth = synthesise()
    out = pd.concat([weak, synth], ignore_index=True)[["text", "label", "source", "complaint_id"]]
    out.to_parquet(OUT, index=False)
    print(out.groupby(["source", "label"]).size().unstack().to_string())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
