"""Download CFPB credit-card dispute narratives (T09).

    uv run python -m ml.data.download_cfpb

The CFPB's own API and bulk CSV no longer include narrative text, so this reads a public-domain (CC0)
Hugging Face mirror of the complaints that have text (3 files, about 300 MB each, downloaded one at a
time and deleted after filtering). Writes ml/data/processed/cfpb_narratives.parquet (complaint_id, narrative).
"""
import sys
from pathlib import Path

import httpx
import pandas as pd

BASE = "https://huggingface.co/datasets/BEE-spoke-data/consumer-finance-complaints/resolve/main/has-text/"
FILES = [f"train-0000{i}-of-00003.parquet" for i in range(3)]
RAW = Path(__file__).parent / "raw"
OUT = Path(__file__).parent / "processed" / "cfpb_narratives.parquet"
ISSUE = "Problem with a purchase shown on your statement"  # the card-dispute complaints
NARRATIVE = "Consumer complaint narrative"
COLUMNS = ["Complaint ID", "Product", "Issue", NARRATIVE]


def fetch(name: str) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    path, tmp = RAW / name, RAW / (name + ".part")
    try:
        with httpx.stream("GET", BASE + name, follow_redirects=True, timeout=120) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    except httpx.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        sys.exit(f"download of {name} failed ({type(exc).__name__}); rerun")
    tmp.replace(path)
    return path


def main() -> None:
    if OUT.exists():
        print(f"{OUT} already exists")
        return
    kept = []
    for name in FILES:
        path = fetch(name)
        df = pd.read_parquet(path, columns=COLUMNS)
        kept.append(df[(df["Issue"] == ISSUE) & df["Product"].str.startswith("Credit card") & df[NARRATIVE].notna()])
        path.unlink()
    df = pd.concat(kept).rename(columns={"Complaint ID": "complaint_id", NARRATIVE: "narrative"})
    df = df.drop_duplicates("complaint_id")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df[["complaint_id", "narrative"]].reset_index(drop=True).to_parquet(OUT, index=False)
    print(f"wrote {len(df):,} narratives to {OUT}")


if __name__ == "__main__":
    main()
