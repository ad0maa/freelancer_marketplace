"""Tests for the Data Vault loaders.

These are the tests that matter most in this repo, because ``dv_helpers`` is
generic: every hub, link and satellite in the vault is loaded by these four
functions, so a bug here is a bug in sixteen tables at once.
"""

from __future__ import annotations

from datetime import datetime

from pyspark.sql import functions as F

from betting_lakehouse.io_utils import table_exists
from betting_lakehouse.vault import dv_helpers as dv

LD1 = datetime(2026, 6, 27, 2, 0, 0)
LD2 = datetime(2026, 6, 28, 2, 0, 0)


def _one(spark, expr):
    return spark.range(1).select(expr.alias("v")).collect()[0]["v"]


# ------------------------------------------------------------------- hashing


def test_hash_key_matches_a_pinned_value(spark):
    """The hash of a given business key must never change.

    Pinned against a literal, not against another call to the same function - that
    would only prove the function is deterministic within one run, which is not the
    risk. The risk is somebody changing the delimiter, the null token or the case
    rule: every hash key in the vault would change, every satellite would look like
    a brand-new key, and the whole model would need reloading. This test is what
    makes that a failing build rather than a silent reload.

    The expected values are plain SHA-256 of the normalised, "||"-joined key:
        python -c "import hashlib; print(hashlib.sha256(b'C000123').hexdigest())"
    """
    assert _one(spark, dv.hash_key(F.lit("C000123"))) == (
        "233288a74f269b1d8d640f75e3e0a816dad2a0d4b0c42aabc06b6ef9e1843d7d"
    )
    assert _one(spark, dv.hash_key(F.lit("B1"), F.lit("S1"))) == (
        "b406ca5ab8b078aae4db037b3ecb794776217a64b0475f5b3d7963d853d3e6b6"
    )


def test_hash_key_normalises_case_and_whitespace(spark):
    """" c000123 " and "C000123" are the same customer.

    Getting this wrong is the classic Data Vault failure: two hubs, two satellites
    and two rows in every report for one person, undetectable by eye because the
    keys are opaque hashes.
    """
    assert _one(spark, dv.hash_key(F.lit("  c000123  "))) == _one(
        spark, dv.hash_key(F.lit("C000123"))
    )


def test_hash_key_is_order_sensitive(spark):
    """Column order is part of a link's contract and must not be reshuffled."""
    forward = _one(spark, dv.hash_key(F.lit("B1"), F.lit("S1")))
    backward = _one(spark, dv.hash_key(F.lit("S1"), F.lit("B1")))
    assert forward != backward


def test_hash_key_distinguishes_null_from_empty(spark):
    """(A, NULL) and (A, '') are different facts and must hash differently."""
    with_null = _one(spark, dv.hash_key(F.lit("A"), F.lit(None).cast("string")))
    with_empty = _one(spark, dv.hash_key(F.lit("A"), F.lit("")))
    assert with_null != with_empty


def test_hash_diff_changes_only_when_payload_changes(spark):
    same = _one(spark, dv.hash_diff(F.lit("VIC"), F.lit("ACTIVE")))
    again = _one(spark, dv.hash_diff(F.lit("VIC"), F.lit("ACTIVE")))
    different = _one(spark, dv.hash_diff(F.lit("NSW"), F.lit("ACTIVE")))
    assert same == again
    assert same != different


# ---------------------------------------------------------------------- hubs


def test_load_hub_inserts_once_then_never_again(spark, cfg):
    source = spark.createDataFrame(
        [("C1",), ("C2",), ("C2",)], "customer_id string"
    )
    name = "test_hub_customer"

    first = dv.load_hub(spark, cfg, name, source, ["customer_id"], "hk_customer",
                        "test.source", LD1)
    second = dv.load_hub(spark, cfg, name, source, ["customer_id"], "hk_customer",
                         "test.source", LD2)

    # Two distinct keys from three rows: the duplicate collapses before insert.
    assert first == 2
    # And the second load is a complete no-op, which is what makes a retry safe.
    assert second == 0
    assert spark.table(cfg.table("vault", name)).count() == 2


def test_load_hub_skips_null_business_keys(spark, cfg):
    source = spark.createDataFrame([("C9",), (None,)], "customer_id string")
    inserted = dv.load_hub(spark, cfg, "test_hub_nulls", source, ["customer_id"],
                           "hk_customer", "test.source", LD1)
    assert inserted == 1


# ---------------------------------------------------------------------- links


def test_load_link_uses_dependent_child_key(spark, cfg):
    """Two legs of one bet on the same selection are two distinct legs.

    Without leg_number in the hash they collapse into one row and a leg silently
    disappears from turnover. This is the single most common Data Vault modelling
    mistake, so it gets its own test.
    """
    source = spark.createDataFrame(
        [("B1", "S1", 1), ("B1", "S1", 2)],
        "bet_id string, selection_id string, leg_number int",
    )
    inserted = dv.load_link(
        spark, cfg, "test_link_bet_selection", source, "hk_bet_selection",
        {"hk_bet": ["bet_id"], "hk_selection": ["selection_id"]},
        "test.source", LD1, dependent_child_keys=["leg_number"],
    )
    assert inserted == 2

    # Without the dependent child key, the same input collapses to one row.
    collapsed = dv.load_link(
        spark, cfg, "test_link_no_dck", source, "hk_bet_selection",
        {"hk_bet": ["bet_id"], "hk_selection": ["selection_id"]},
        "test.source", LD1,
    )
    assert collapsed == 1


def test_load_link_requires_both_ends(spark, cfg):
    source = spark.createDataFrame(
        [("B1", "S1"), ("B2", None)], "bet_id string, selection_id string"
    )
    inserted = dv.load_link(
        spark, cfg, "test_link_partial", source, "hk_bet_selection",
        {"hk_bet": ["bet_id"], "hk_selection": ["selection_id"]}, "test.source", LD1,
    )
    assert inserted == 1


# ----------------------------------------------------------------- satellites


def _customer(customer_id: str, state: str, status: str, updated: datetime):
    return (customer_id, state, status, updated)


SAT_SCHEMA = "customer_id string, state_code string, account_status string, source_updated_at timestamp"


def test_satellite_inserts_nothing_when_payload_unchanged(spark, cfg):
    name = "test_sat_unchanged"
    rows = [_customer("C1", "VIC", "ACTIVE", datetime(2026, 6, 20))]
    source = spark.createDataFrame(rows, SAT_SCHEMA)

    first = dv.load_satellite(
        spark, cfg, name, source, "hk_customer", ["customer_id"],
        payload=["state_code", "account_status"], record_source="test.source",
        load_date=LD1, version_col="source_updated_at",
    )
    # Same payload, later batch: nothing to record, because nothing changed.
    second = dv.load_satellite(
        spark, cfg, name, source, "hk_customer", ["customer_id"],
        payload=["state_code", "account_status"], record_source="test.source",
        load_date=LD2, version_col="source_updated_at",
    )
    assert (first, second) == (1, 0)


def test_satellite_inserts_a_new_version_on_change(spark, cfg):
    name = "test_sat_changed"
    v1 = spark.createDataFrame(
        [_customer("C1", "VIC", "ACTIVE", datetime(2026, 6, 20))], SAT_SCHEMA
    )
    v2 = spark.createDataFrame(
        [_customer("C1", "NSW", "ACTIVE", datetime(2026, 6, 28))], SAT_SCHEMA
    )
    payload = ["state_code", "account_status"]

    dv.load_satellite(spark, cfg, name, v1, "hk_customer", ["customer_id"],
                      payload=payload, record_source="t", load_date=LD1,
                      version_col="source_updated_at")
    inserted = dv.load_satellite(spark, cfg, name, v2, "hk_customer", ["customer_id"],
                                 payload=payload, record_source="t", load_date=LD2,
                                 version_col="source_updated_at")
    assert inserted == 1

    history = spark.table(cfg.table("vault", name)).orderBy("load_date").collect()
    assert [r["state_code"] for r in history] == ["VIC", "NSW"]
    # Insert-only: the first row is untouched, not end-dated in place.
    assert history[0]["load_date"] == LD1


def test_satellite_keeps_latest_version_within_a_batch(spark, cfg):
    """Three price moves in one extract: the newest one wins.

    Note what this also documents - the two intermediate versions are lost. If
    every tick matters, the satellite has to be fed by a stream, not a daily batch.
    """
    name = "test_sat_within_batch"
    source = spark.createDataFrame(
        [
            _customer("C1", "VIC", "ACTIVE", datetime(2026, 6, 20, 9)),
            _customer("C1", "NSW", "ACTIVE", datetime(2026, 6, 20, 12)),
            _customer("C1", "QLD", "ACTIVE", datetime(2026, 6, 20, 15)),
        ],
        SAT_SCHEMA,
    )
    inserted = dv.load_satellite(
        spark, cfg, name, source, "hk_customer", ["customer_id"],
        payload=["state_code", "account_status"], record_source="t", load_date=LD1,
        version_col="source_updated_at",
    )
    assert inserted == 1
    assert spark.table(cfg.table("vault", name)).collect()[0]["state_code"] == "QLD"


def test_satellite_scd2_produces_non_overlapping_intervals(spark, cfg):
    name = "test_sat_scd2"
    payload = ["state_code", "account_status"]
    for load_date, state in ((LD1, "VIC"), (LD2, "NSW")):
        dv.load_satellite(
            spark, cfg, name,
            spark.createDataFrame([_customer("C1", state, "ACTIVE", load_date)], SAT_SCHEMA),
            "hk_customer", ["customer_id"], payload=payload, record_source="t",
            load_date=load_date, version_col="source_updated_at",
        )

    scd2 = dv.satellite_scd2(spark.table(cfg.table("vault", name)), "hk_customer")
    rows = scd2.orderBy("version_number").collect()

    assert [r["version_number"] for r in rows] == [1, 2]
    assert [r["is_current"] for r in rows] == [False, True]
    # Version 1 must end strictly before version 2 begins, or an as-at join can
    # match both and silently double the fact row.
    assert rows[0]["effective_to"] < rows[1]["effective_from"]
    # The open interval uses a high date rather than NULL, so BETWEEN just works.
    assert rows[1]["effective_to"].year == 9999


def test_satellite_current_returns_one_row_per_key(spark, cfg, lakehouse):
    current = dv.satellite_current(spark, cfg, "sat_customer_details", "hk_customer")
    assert current.count() == current.select("hk_customer").distinct().count()
    assert table_exists(spark, cfg.table("vault", "sat_customer_details"))
