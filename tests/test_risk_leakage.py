import pandas as pd
import pytest

from ml.features.keywords import has_keyword
from ml.features.risk import DAY, FEATURES, MAX_ROWS, build_snapshots, label

T0 = pd.Timestamp("2017-06-01")  # well before the 2018-03-01 train cut-off
COLUMNS = [
    "order_id", "status", "purchase_ts", "carrier_ts", "delivered_ts", "est_ts", "order_value",
    "freight", "n_items", "seller_id", "review_score", "review_text", "review_ts",
    "seller_state", "customer_state", "category",
]


def order(order_id, purchase=T0, ship=1, deliver=5, est=7, score=5, text="", review=6, seller="s1"):
    """Day offsets from `purchase`; deliver/review None means never."""
    at = lambda d: None if d is None else purchase + d * DAY  # noqa: E731
    return dict(
        order_id=order_id, status="delivered", purchase_ts=purchase, carrier_ts=at(ship),
        delivered_ts=at(deliver), est_ts=at(est), order_value=100.0, freight=10.0, n_items=1,
        seller_id=seller, review_score=score, review_text=text, review_ts=at(review),
        seller_state="SP", customer_state="SP", category="toys",
    )


def frame(*rows):
    df = pd.DataFrame(list(rows), columns=COLUMNS)
    for c in ("purchase_ts", "carrier_ts", "delivered_ts", "est_ts", "review_ts"):
        df[c] = pd.to_datetime(df[c]).astype("datetime64[ns]")
    return df


def features_up_to(df, cutoff):
    snaps = build_snapshots(df)
    return snaps[snaps["snapshot_ts"] <= cutoff].reset_index(drop=True)[["order_id", "snapshot_ts"] + FEATURES]


def world():
    return frame(
        order("a", purchase=T0, deliver=12, est=7, score=1, review=13),
        order("b", purchase=T0 + 3 * DAY, deliver=9, est=15, score=2, review=11, seller="s2"),
        order("x", purchase=T0 + 20 * DAY, ship=21, deliver=26, est=27, score=5, review=28),
        order("y", purchase=T0 + 20 * DAY, ship=24, deliver=26, est=27, score=5, review=28, seller="s2"),
        order("c", purchase=T0 + 21 * DAY, ship=22, deliver=40, est=25, score=1, review=41),
        order("d", purchase=T0 + 5 * DAY, ship=None, deliver=None, est=10, score=1, review=20, seller="s2"),
    )


@pytest.mark.parametrize("cutoff_day", range(0, 45, 3))
def test_features_do_not_change_when_the_future_changes(cutoff_day):
    """Rewrite everything that happens after `cutoff`: rows at or before it must not move."""
    base = world()
    cutoff = T0 + cutoff_day * DAY
    mutated = base.copy()
    for col in ("carrier_ts", "delivered_ts", "review_ts"):
        later = mutated[col] > cutoff
        mutated.loc[later, col] += 30 * DAY
    later_review = base["review_ts"] > cutoff
    mutated.loc[later_review, "review_score"] = 6 - mutated.loc[later_review, "review_score"]
    mutated.loc[later_review, "review_text"] = "nao recebi"

    pd.testing.assert_frame_equal(features_up_to(base, cutoff), features_up_to(mutated, cutoff))


def test_seller_history_never_mixes_sellers():
    both = frame(
        order("s1_late", seller="s1", deliver=12, est=7, score=1, review=13),
        order("s2_ok", seller="s2", deliver=5, est=7, score=5, review=6),
        order("s1_x", seller="s1", purchase=T0 + 30 * DAY, ship=31, deliver=32, est=37),
        order("s2_x", seller="s2", purchase=T0 + 30 * DAY, ship=31, deliver=32, est=37),
    )
    snaps = build_snapshots(both)
    first = snaps[snaps["snapshot_ts"] == snaps["purchase_ts"] + 0 * DAY]
    first = first[first["order_id"].isin(["s1_x", "s2_x"])].set_index("order_id")
    assert first.loc["s1_x", "seller_late_rate"] == 1.0 and first.loc["s1_x", "seller_low_review_rate"] == 1.0
    assert first.loc["s2_x", "seller_late_rate"] == 0.0 and first.loc["s2_x", "seller_low_review_rate"] == 0.0


def test_an_orders_own_review_never_reaches_its_own_seller_rate():
    def seller_rate(score):
        w = frame(
            order("a", purchase=T0, deliver=5, est=7, score=5, review=6),
            # x's review is answered on day 27, before its later snapshots (up to day 34)
            order("x", purchase=T0 + 20 * DAY, ship=21, deliver=22, est=27, score=score, review=7),
        )
        snaps = build_snapshots(w)
        return snaps[snaps["order_id"] == "x"]["seller_low_review_rate"].tolist()

    assert seller_rate(5) == seller_rate(1)


def test_an_orders_own_lateness_never_reaches_its_own_seller_rate():
    def late_rates(deliver):  # est is day 21 of x: deliver 22 is late, deliver 21 is on time
        w = frame(
            order("a", purchase=T0, deliver=9, est=7, score=5, review=40),  # the one late order
            order("x", purchase=T0 + 10 * DAY, ship=11, deliver=deliver, est=11, score=5, review=40),
        )
        snaps = build_snapshots(w)
        rows = snaps[(snaps["order_id"] == "x") & (snaps["snapshot_ts"] > T0 + 12 * DAY)]
        return rows["seller_late_rate"].tolist()

    # a is the only other order and was late: exactly 1 of 1, whatever x's own delivery was
    assert late_rates(12) == late_rates(11) == [1.0] * len(late_rates(12)) != []


def test_history_counts_an_outcome_only_strictly_before_the_snapshot():
    t = T0 + 20 * DAY  # x's purchase row
    w = frame(
        order("on_time", purchase=T0, deliver=19, est=25, score=5, review=19.5),
        order("late_before", purchase=T0, ship=1, deliver=19.99, est=10, score=1, review=20.5),
        order("late_at", purchase=T0, ship=1, deliver=20, est=10, score=1, review=21),
        order("x", purchase=t, ship=21, deliver=22, est=27),
    )
    snaps = build_snapshots(w)
    row = snaps[(snaps["order_id"] == "x") & (snaps["snapshot_ts"] == t)].iloc[0]

    # delivered strictly before t: on_time (not late) and late_before (late); late_at is excluded
    assert row["seller_late_rate"] == pytest.approx(0.5)
    # only on_time's review (score 5) was answered before t; the two later 1-star reviews are not counted
    assert row["seller_low_review_rate"] == 0.0


def test_features_exclude_outcome_and_review_columns():
    raw = {"review_score", "review_text", "review_ts", "status", "label", "carrier_ts", "delivered_ts", "approved"}
    assert not raw & set(FEATURES)


def test_all_snapshots_of_an_order_share_a_split_matching_purchase_date():
    world = frame(
        order("tr", purchase=pd.Timestamp("2018-01-10")),
        order("va", purchase=pd.Timestamp("2018-03-01")),
        order("va2", purchase=pd.Timestamp("2018-05-31 23:00")),
        order("te", purchase=pd.Timestamp("2018-06-01")),
    )
    snaps = build_snapshots(world)
    assert (snaps.groupby("order_id")["split"].nunique() == 1).all()
    assert snaps.groupby("order_id")["split"].first().to_dict() == {
        "tr": "train", "va": "val", "va2": "val", "te": "test",
    }


def test_snapshot_grid_depends_only_on_purchase_and_estimate():
    w = frame(
        order("long", ship=1, est=60),  # many more than 10 days -> capped
        order("short", ship=1, est=2),  # purchase..est+7 = day 0..9 -> 10 rows
        order("unshipped", ship=None, deliver=None, est=60),
        order("late_ship", ship=30, est=7),
    )
    snaps = build_snapshots(w)
    counts = snaps.groupby("order_id").size()

    assert counts.to_dict() == {"long": MAX_ROWS, "short": 10, "unshipped": MAX_ROWS, "late_ship": 10}
    first = snaps.groupby("order_id").first()
    assert (first["snapshot_ts"] == first["purchase_ts"]).all()
    # same grid whether or not, or when, the order shipped
    grid = lambda oid: snaps[snaps["order_id"] == oid]["snapshot_ts"].tolist()  # noqa: E731
    assert grid("long") == grid("unshipped")
    assert grid("long")[0] == T0 and grid("long")[-1] == T0 + 67 * DAY
    assert pd.Series(grid("long")).is_monotonic_increasing and pd.Series(grid("long")).is_unique


def test_shipped_features_follow_the_snapshot_time():
    snaps = build_snapshots(frame(order("o", ship=3, deliver=8, est=2))).set_index("snapshot_ts")
    assert snaps.loc[T0 + 2 * DAY, "shipped_yet"] == 0 and pd.isna(snaps.loc[T0 + 2 * DAY, "days_since_ship"])
    assert snaps.loc[T0 + 3 * DAY, "shipped_yet"] == 1 and snaps.loc[T0 + 3 * DAY, "days_since_ship"] == 0
    assert snaps.loc[T0 + 6 * DAY, "days_since_ship"] == pytest.approx(3)


def test_train_orders_resolving_inside_the_validation_window_are_purged():
    end = pd.Timestamp("2018-03-01")
    w = frame(
        order("ok", purchase=end - 40 * DAY, est=7, review=10),
        order("late_review", purchase=end - 12 * DAY, est=7, review=15),
        order("late_est", purchase=end - 10 * DAY, est=7, review=5),  # est + 7 days is after the cut-off
        order("val_ok", purchase=end + 2 * DAY, est=7, review=20),
    )
    assert set(build_snapshots(w)["order_id"]) == {"ok", "val_ok"}


def test_delivered_yet_and_days_late_follow_the_snapshot_time():
    world = frame(order("o", ship=1, deliver=5, est=2))  # est + 7 = day 9: every day is a row
    snaps = build_snapshots(world).set_index("snapshot_ts")
    day = lambda d: snaps.loc[T0 + d * DAY]  # noqa: E731

    assert day(0)["delivered_yet"] == 0 and day(0)["days_late_so_far"] == 0
    assert day(3)["delivered_yet"] == 0 and day(3)["days_late_so_far"] == pytest.approx(1)
    assert day(5)["delivered_yet"] == 1 and day(5)["days_late_so_far"] == pytest.approx(3)
    assert day(8)["days_late_so_far"] == pytest.approx(3)  # frozen at delivery
    assert day(5)["days_to_est"] == pytest.approx(-3)


def test_label_rule():
    world = frame(
        order("late_low", deliver=10, est=7, score=1),
        order("ontime_low_plain", deliver=5, est=7, score=1, text="ok"),
        order("ontime_low_keyword", deliver=5, est=7, score=2, text="Produto quebrado, NÃO recebi tudo"),
        order("late_high", deliver=10, est=7, score=5),
        order("keyword_high", deliver=5, est=7, score=4, text="defeito"),
        order("undelivered_late", deliver=None, est=7, score=1, review=10),
        order("undelivered_early", deliver=None, est=7, score=1, review=5),
        order("same_day", deliver=7.5, est=7, score=1),  # same calendar day as the estimate
    )
    got = dict(zip(world["order_id"], label(world)))
    assert got == {
        "late_low": 1, "ontime_low_plain": 0, "ontime_low_keyword": 1, "late_high": 0,
        "keyword_high": 0, "undelivered_late": 1, "undelivered_early": 0, "same_day": 0,
    }


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Não recebi o produto", True),
        ("NAO CHEGOU", True),
        ("entrega atrasada", True),
        ("veio com defeito", True),
        ("produto diferente do anunciado", True),
        ("tudo certo, recomendo", False),
        ("", False),
        (None, False),
        (float("nan"), False),
    ],
)
def test_keywords(text, expected):
    assert has_keyword(text) is expected
