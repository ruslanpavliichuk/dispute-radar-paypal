"""Build ml/data/processed/risk_snapshots.parquet from the raw Olist CSVs.

    uv run python -m ml.build_risk_dataset
"""
from pathlib import Path

from ml.features.risk import build_snapshots, label, load_orders

DATA = Path(__file__).parent / "data"
OUT = DATA / "processed" / "risk_snapshots.parquet"


def main() -> None:
    orders = load_orders(DATA / "raw" / "olist")
    orders["label"] = label(orders)
    print(f"orders: {len(orders):,}  base rate: {orders['label'].mean():.2%}")
    snaps = build_snapshots(orders)
    summary = snaps.groupby("split").agg(orders=("order_id", "nunique"), rows=("label", "size"), rate=("label", "mean"))
    print(summary.to_string(float_format=lambda v: f"{v:.4f}"))
    print(f"snapshots: {len(snaps):,}  snapshot-level base rate: {snaps['label'].mean():.2%}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    snaps.to_parquet(OUT, index=False)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
