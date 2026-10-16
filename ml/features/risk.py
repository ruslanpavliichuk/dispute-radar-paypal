"""Olist -> dispute-risk snapshots (CONTEXT.md 7.3).

One row per (order, moment). Every feature uses only what was known at `snapshot_ts`.
The label is a property of the whole order and is never a feature.
"""
from pathlib import Path

import numpy as np
import pandas as pd

from ml.features.keywords import has_keyword

CUTOFF = pd.Timestamp("2018-08-31 23:59:59")  # later purchases are mostly undelivered/unreviewed
TRAIN_END = pd.Timestamp("2018-03-01")
VAL_END = pd.Timestamp("2018-06-01")
MAX_ROWS = 10
DAY = pd.Timedelta(days=1)

FEATURES = [
    "order_value",
    "freight_share",
    "category",
    "n_items",
    "est_window_days",
    "same_state",
    "seller_late_rate",
    "seller_low_review_rate",
    "days_since_purchase",
    "days_to_est",
    "delivered_yet",
    "days_late_so_far",
    "shipped_yet",
    "days_since_ship",
]
META = ["order_id", "snapshot_ts", "purchase_ts", "split", "label"]


def _ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).astype("datetime64[ns]")


def load_orders(raw: Path) -> pd.DataFrame:
    """One row per reviewed order with items, aggregated from the 9 Olist CSVs."""
    read = lambda name, **kw: pd.read_csv(raw / f"{name}.csv", **kw)  # noqa: E731
    orders = read("olist_orders_dataset")
    items = read("olist_order_items_dataset")
    reviews = read("olist_order_reviews_dataset")
    products = read("olist_products_dataset", usecols=["product_id", "product_category_name"])
    sellers = read("olist_sellers_dataset", usecols=["seller_id", "seller_state"])
    customers = read("olist_customers_dataset", usecols=["customer_id", "customer_state"])
    names = read("product_category_name_translation")

    # multi-seller orders (~1%): the seller and category of the highest-priced item stand in
    items = items.sort_values(["order_id", "price", "order_item_id"], ascending=[True, False, True])
    agg = items.groupby("order_id").agg(
        order_value=("price", "sum"), freight=("freight_value", "sum"), n_items=("order_item_id", "size")
    )
    top = items.drop_duplicates("order_id").set_index("order_id")[["seller_id", "product_id"]]

    translate = dict(zip(names["product_category_name"], names["product_category_name_english"]))
    products["category"] = (
        products["product_category_name"].map(translate).fillna(products["product_category_name"]).fillna("unknown")
    )

    # latest review per order (about 550 orders have several)
    reviews = reviews.assign(review_ts=_ts(reviews["review_answer_timestamp"]))
    reviews["review_text"] = reviews["review_comment_title"].fillna("") + " " + reviews["review_comment_message"].fillna("")
    reviews = reviews.sort_values(["review_ts", "review_id"]).drop_duplicates("order_id", keep="last")
    reviews = reviews[["order_id", "review_score", "review_text", "review_ts"]]

    df = (
        orders.rename(
            columns={
                "order_purchase_timestamp": "purchase_ts",
                "order_delivered_carrier_date": "carrier_ts",
                "order_delivered_customer_date": "delivered_ts",
                "order_estimated_delivery_date": "est_ts",
                "order_status": "status",
            }
        )[["order_id", "customer_id", "status", "purchase_ts", "carrier_ts", "delivered_ts", "est_ts"]]
        .merge(agg, on="order_id")
        .merge(top, on="order_id")
        .merge(reviews, on="order_id")  # orders without a review have no label: dropped
        .merge(sellers, on="seller_id", how="left")
        .merge(customers, on="customer_id", how="left")
        .merge(products[["product_id", "category"]], on="product_id", how="left")
    )
    for c in ("purchase_ts", "carrier_ts", "delivered_ts", "est_ts"):
        df[c] = _ts(df[c])
    df["category"] = df["category"].fillna("unknown")
    # impossible orderings are bad data: treat the later event as unknown
    df.loc[df["carrier_ts"] < df["purchase_ts"], "carrier_ts"] = pd.NaT
    df.loc[df["delivered_ts"] < df["purchase_ts"], "delivered_ts"] = pd.NaT
    df.loc[df["delivered_ts"] < df["carrier_ts"], "delivered_ts"] = pd.NaT
    df = df[df["purchase_ts"] <= CUTOFF]
    return df.drop(columns=["customer_id", "product_id"]).reset_index(drop=True)


def is_late(df: pd.DataFrame) -> pd.Series:
    """Delivered after the estimated date (calendar days), or never delivered and the
    buyer reviewed after the estimated date (developer-approved 2026-10-16)."""
    est = df["est_ts"].dt.normalize()
    delivered_late = df["delivered_ts"].dt.normalize() > est
    undelivered_late = df["delivered_ts"].isna() & (df["review_ts"].dt.normalize() > est)
    return delivered_late | undelivered_late


def label(df: pd.DataFrame) -> pd.Series:
    low = df["review_score"] <= 2
    keyword = df["review_text"].map(has_keyword)
    return (low & (is_late(df) | keyword)).astype(int)


def snapshot_times(orders: pd.DataFrame) -> pd.DataFrame:
    """Daily rows from purchase to est+7 days, at most 10, evenly spaced with the first
    (purchase) and last day included. The grid depends only on purchase and the estimated
    date, both known at purchase, so it carries no information about shipping or delivery."""
    span = ((orders["est_ts"] + 7 * DAY - orders["purchase_ts"]) // DAY).clip(lower=0).astype(int)
    k = (span + 1).clip(upper=MAX_ROWS)
    rep = orders.loc[orders.index.repeat(k)]
    j = rep.groupby(level=0).cumcount().to_numpy()
    last, kk = span.loc[rep.index].to_numpy(), k.loc[rep.index].to_numpy()
    offset = np.where(kk > 1, np.round(j * last / np.maximum(kk - 1, 1)), 0).astype(int)
    return rep.assign(snapshot_ts=rep["purchase_ts"] + offset * DAY).reset_index(drop=True)


def seller_history(snaps: pd.DataFrame, orders: pd.DataFrame) -> pd.DataFrame:
    """Seller late-delivery and low-review rates over outcomes known strictly before the
    snapshot. An outcome is known when the order is delivered / the review is answered.
    The order's own outcome is removed so it can never leak its own label."""
    own = orders.set_index("order_id")
    kinds = {
        "seller_late_rate": ("delivered_ts", lambda d: is_late(d).astype(int)),
        "seller_low_review_rate": ("review_ts", lambda d: (d["review_score"] <= 2).astype(int)),
    }
    out = snaps.sort_values("snapshot_ts").reset_index(drop=True)
    for col, (ts_col, bad_fn) in kinds.items():
        ev = orders.loc[orders[ts_col].notna(), ["seller_id", ts_col]].assign(bad=bad_fn(orders))
        ev = ev.rename(columns={ts_col: "event_ts"}).sort_values("event_ts")
        ev["n"] = ev.groupby("seller_id").cumcount() + 1
        ev["bad_n"] = ev.groupby("seller_id")["bad"].cumsum()
        merged = pd.merge_asof(
            out[["seller_id", "snapshot_ts"]],
            ev[["seller_id", "event_ts", "n", "bad_n"]],
            left_on="snapshot_ts",
            right_on="event_ts",
            by="seller_id",
            allow_exact_matches=False,
        )
        n, bad_n = merged["n"].fillna(0), merged["bad_n"].fillna(0)
        own_ts = out["order_id"].map(own[ts_col])
        own_bad = out["order_id"].map(bad_fn(orders).set_axis(orders["order_id"]))
        own_known = own_ts < out["snapshot_ts"]
        n = n - own_known.astype(int)
        bad_n = bad_n - (own_known * own_bad.fillna(0)).astype(int)
        out[col] = (bad_n / n.where(n > 0)).to_numpy()
    return out


def assign_split(purchase_ts: pd.Series) -> pd.Series:
    return pd.Series(
        np.select([purchase_ts < TRAIN_END, purchase_ts < VAL_END], ["train", "val"], "test"),
        index=purchase_ts.index,
    )


def resolved_in_train(orders: pd.DataFrame) -> pd.Series:
    """False for train orders whose review or last snapshot falls on or after the train
    cut-off: their labels resolve inside the validation window (developer-approved)."""
    unresolved = (orders["purchase_ts"] < TRAIN_END) & (
        (orders["review_ts"] >= TRAIN_END) | (orders["est_ts"] + 7 * DAY >= TRAIN_END)
    )
    return ~unresolved


def build_snapshots(orders: pd.DataFrame) -> pd.DataFrame:
    orders = orders.assign(label=label(orders))
    # all orders feed the seller history; only resolved ones become snapshot rows
    snaps = seller_history(snapshot_times(orders[resolved_in_train(orders)]), orders)
    t = snaps["snapshot_ts"]
    delivered = snaps["delivered_ts"].where(snaps["delivered_ts"] <= t)  # NaT while in transit
    snaps["freight_share"] = snaps["freight"] / snaps["order_value"].where(snaps["order_value"] > 0)
    snaps["est_window_days"] = (snaps["est_ts"] - snaps["purchase_ts"]) / DAY
    snaps["same_state"] = (snaps["seller_state"] == snaps["customer_state"]).astype(int)
    snaps["days_since_purchase"] = (t - snaps["purchase_ts"]) / DAY
    snaps["days_to_est"] = (snaps["est_ts"] - t) / DAY
    snaps["delivered_yet"] = delivered.notna().astype(int)
    end = delivered.fillna(t)  # calendar days, like the label
    snaps["days_late_so_far"] = ((end.dt.normalize() - snaps["est_ts"].dt.normalize()) / DAY).clip(lower=0)
    shipped = snaps["carrier_ts"].where(snaps["carrier_ts"] <= t)
    snaps["shipped_yet"] = shipped.notna().astype(int)
    snaps["days_since_ship"] = (t - shipped) / DAY
    snaps["split"] = assign_split(snaps["purchase_ts"])
    return snaps.sort_values(["order_id", "snapshot_ts"]).reset_index(drop=True)[META + FEATURES]
