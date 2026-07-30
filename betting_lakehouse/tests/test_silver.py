"""Tests for the silver layer: typing, parsing, standardising, deduplicating."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pyspark.sql import functions as F

from betting_lakehouse import silver
from betting_lakehouse.bronze import BATCH_ID
from betting_lakehouse.dq.expectations import Expectation, apply_expectations


def _values(df, col):
    return [r[col] for r in df.collect()]


# ------------------------------------------------------------------- parsing


def test_money_parses_formatted_currency_exactly(spark):
    df = spark.createDataFrame(
        [("$1,250.00",), ("25.50",), ("$0.00",), ("",), (None,)], "raw string"
    )
    parsed = _values(df.select(silver.money("raw").alias("v")), "v")
    assert parsed == [Decimal("1250.00"), Decimal("25.50"), Decimal("0.00"), None, None]


def test_money_is_decimal_not_double(spark):
    """Ten cents, a hundred thousand times, must be exactly $10,000.00.

    Summed as doubles this drifts, and a finance team reconciling to the cent will
    find the drift. Decimal arithmetic in Spark is exact.
    """
    df = spark.range(100_000).select(F.lit("0.10").alias("raw"))
    total = df.select(F.sum(silver.money("raw")).alias("t")).collect()[0]["t"]
    assert total == Decimal("10000.00")


def test_naive_cast_of_formatted_money_aborts_the_job(spark):
    """Documents *why* the helpers use try_cast rather than cast.

    Spark 4 turns ANSI SQL mode on by default, so a plain cast of "$1,250.00"
    raises and takes the whole batch down. One malformed row would stop five
    million good ones from loading.

    ANSI mode is the right default - silent NULLs from failed casts have hidden
    more data bugs than anything else in Spark - but it means a cleansing layer has
    to parse with `try_*` and pair each one with a DQ rule, which is exactly what
    `money()` and `EXPECTATIONS` do.
    """
    import pytest

    assert spark.conf.get("spark.sql.ansi.enabled") == "true"
    df = spark.createDataFrame([("$1,250.00",)], "raw string")
    with pytest.raises(Exception, match="CAST_INVALID_INPUT"):
        df.select(F.col("raw").cast("double")).collect()

    # The ANSI-safe form used throughout silver: NULL instead of an aborted run.
    assert _values(df.select(silver.money("raw").alias("v")), "v") == [Decimal("1250.00")]


def test_parse_helpers_do_not_raise_on_unparseable_input(spark):
    """The property that keeps one bad row from killing a batch."""
    df = spark.createDataFrame([("garbage",), (None,)], "raw string")
    assert _values(df.select(silver.parse_date("raw", "dd/MM/yyyy").alias("v")), "v") == [None, None]
    assert _values(df.select(silver.parse_timestamp("raw").alias("v")), "v") == [None, None]
    assert _values(df.select(silver.money("raw").alias("v")), "v") == [None, None]
    assert _values(df.select(silver.safe_int("raw").alias("v")), "v") == [None, None]


def test_parse_date_handles_australian_and_iso_formats(spark):
    df = spark.createDataFrame([("11/12/2025",), ("2025-12-11",), ("garbage",)], "raw string")
    parsed = _values(
        df.select(silver.parse_date("raw", "dd/MM/yyyy", "yyyy-MM-dd").alias("v")), "v"
    )
    # 11/12/2025 is the 11th of December, not the 12th of November.
    assert parsed == [date(2025, 12, 11), date(2025, 12, 11), None]


def test_parse_timestamp_handles_offset_and_naive_forms(spark):
    """Both source forms parse, and both land on the right local wall-clock time.

    Asserted with ``date_format`` rather than by comparing collected datetimes,
    because those are two different questions. ``date_format`` renders using
    ``spark.sql.session.timeZone`` (Australia/Melbourne here), which is what every
    downstream ``to_date`` and daily aggregate uses. Collecting to Python converts
    using the *driver JVM's* default timezone instead, which in a container is
    usually UTC - so the same correct value comes back as 05:56 rather than 15:56.

    Worth knowing before it costs you an afternoon: if a timestamp looks ten hours
    out in a notebook but the daily totals are right, this asymmetry is why.
    """
    assert spark.conf.get("spark.sql.session.timeZone") == "Australia/Melbourne"
    df = spark.createDataFrame(
        [("2026-06-23T15:56:30+10:00",), ("2026-06-13 23:50:00",)], "raw string"
    )
    rendered = _values(
        df.select(
            F.date_format(silver.parse_timestamp("raw"), "yyyy-MM-dd HH:mm:ss").alias("v")
        ),
        "v",
    )
    # +10:00 is Melbourne's June offset, so the offset-bearing form keeps its
    # wall-clock time; the naive form is read as already being local.
    assert rendered == ["2026-06-23 15:56:30", "2026-06-13 23:50:00"]


# ----------------------------------------------------------- standardisation


def test_business_key_normalises_and_nulls_blanks(spark):
    df = spark.createDataFrame([("  c000123 ",), ("C000123",), ("",), (None,)], "raw string")
    assert _values(df.select(silver.business_key("raw").alias("v")), "v") == [
        "C000123", "C000123", None, None
    ]


def test_flag_converts_source_boolean_spellings_and_keeps_null(spark):
    """NULL in, NULL out.

    "The CRM did not tell us whether this customer self-excluded" is a different
    statement from "this customer has not self-excluded", and collapsing them to
    False loses the distinction exactly where it matters most.
    """
    df = spark.createDataFrame([("Y",), ("y",), ("YES",), ("N",), ("",), (None,)], "raw string")
    assert _values(df.select(silver.flag("raw").alias("v")), "v") == [
        True, True, True, False, False, None
    ]


# --------------------------------------------------------------- deduplication


def test_deduplicate_keeps_the_latest_version(spark):
    df = spark.createDataFrame(
        [
            ("B1", "OPEN", datetime(2026, 6, 1, 10)),
            ("B1", "SETTLED", datetime(2026, 6, 1, 22)),
            ("B2", "OPEN", datetime(2026, 6, 1, 11)),
        ],
        "bet_id string, bet_status string, updated_at timestamp",
    )
    deduped = silver.deduplicate(df, ["bet_id"], [F.col("updated_at").desc()])
    rows = {r["bet_id"]: r["bet_status"] for r in deduped.collect()}
    # Latest wins, deliberately - not whichever row Spark happened to see first.
    assert rows == {"B1": "SETTLED", "B2": "OPEN"}


def test_silver_bets_has_no_duplicate_keys(spark, lakehouse):
    cfg = lakehouse
    bets = spark.table(cfg.table("silver", "bets"))
    per_batch = bets.select("bet_id", BATCH_ID).distinct().count()
    assert bets.count() == per_batch, "silver.bets contains duplicate bet_id per batch"


def test_silver_preserves_history_across_batches(spark, lakehouse):
    """A customer who changed must have two rows in silver, not one.

    This is the property that makes the vault satellites and the type 2 dimension
    possible. If silver overwrote in place, the previous version would be gone
    before any modelling saw it.
    """
    cfg = lakehouse
    customers = spark.table(cfg.table("silver", "customers"))
    changed = (
        customers.groupBy("customer_id")
        .agg(F.countDistinct(BATCH_ID).alias("batches"))
        .where(F.col("batches") > 1)
    )
    assert changed.count() > 0, "no customers changed between batches - test data is wrong"

    # And the _current companion holds exactly one row per customer.
    current = spark.table(cfg.table("silver", "customers_current"))
    assert current.count() == current.select("customer_id").distinct().count()


# ------------------------------------------------------------- data quality


def test_expectations_quarantine_instead_of_dropping_silently(spark, cfg):
    df = spark.createDataFrame(
        [("B1", Decimal("10.00")), ("B2", Decimal("0.00")), ("B3", None)],
        "bet_id string, stake_amount decimal(18,2)",
    )
    expectations = [
        Expectation("stake_positive", "stake_amount IS NOT NULL AND stake_amount > 0",
                    "zero or missing stake"),
    ]
    clean = apply_expectations(spark, df, expectations, "test.bets", "1", cfg)

    assert _values(clean, "bet_id") == ["B1"]
    quarantined = spark.table(cfg.table("dq", "quarantine")).where(
        F.col("table_name") == "test.bets"
    )
    # Both bad rows are kept, with the reason attached - not deleted.
    assert quarantined.count() == 2
    assert _values(quarantined.select(F.col("failed_expectations")[0].alias("v")), "v") == [
        "stake_positive", "stake_positive"
    ]


def test_expectation_results_are_recorded_for_every_rule(spark, cfg):
    df = spark.createDataFrame([("B1", Decimal("10.00"))], "bet_id string, stake_amount decimal(18,2)")
    expectations = [
        Expectation("always_true", "stake_amount > 0", "sanity", severity="warn"),
        Expectation("always_false", "stake_amount > 1000", "will fail", severity="warn"),
    ]
    apply_expectations(spark, df, expectations, "test.results", "1", cfg)

    results = spark.table(cfg.table("dq", "dq_results")).where(
        F.col("table_name") == "test.results"
    )
    by_name = {r["expectation"]: r for r in results.collect()}
    assert by_name["always_true"]["rows_failed"] == 0
    assert by_name["always_false"]["rows_failed"] == 1
    # A 'warn' failure keeps the row.
    assert by_name["always_false"]["rows_checked"] == 1
