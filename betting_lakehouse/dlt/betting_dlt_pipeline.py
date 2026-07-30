# Databricks notebook source
"""The same silver and gold layers, expressed declaratively in Delta Live Tables.

Deploy with the `betting_dlt` pipeline in databricks.yml. This file cannot run
locally - `import dlt` only resolves on a Databricks DLT cluster - and it is here
because the imperative-vs-declarative choice is one you will be asked to make.

WHAT DLT DOES FOR YOU
---------------------
You declare tables as functions and DLT works out the rest:

* **the DAG** - it reads your `dlt.read()` calls and derives the dependency graph,
  so there is no task ordering to maintain. Add a table in the middle and nothing
  else changes.
* **expectations as first-class metrics** - `@dlt.expect` records pass rates in the
  event log per run, with no results table to write yourself. `dq/expectations.py`
  in this repo is largely a hand-rolled version of this.
* **incremental streaming tables** - `@dlt.table` over `spark.readStream` keeps
  checkpoints and does exactly-once appends without a checkpoint path to manage.
* **CDC / SCD2** - `dlt.create_auto_cdc_flow(stored_as_scd_type=2)` maintains an
  effective-dated table from a change feed. That is a type 2 dimension in four
  lines instead of a `MERGE` with end-dating logic.
* **recovery** - a failed update leaves the previous version of every table intact.

WHAT YOU GIVE UP
----------------
* **Control of the write.** No `MERGE` you wrote, no partition overwrite you tuned,
  no arbitrary Python between reading and writing. When you need those, you are
  back to the imperative jobs in ``src/``.
* **Testability off-cluster.** These functions cannot be imported without the `dlt`
  module, so the unit tests in ``tests/`` could not exist in this form. That is the
  strongest argument for keeping core transformations in plain functions and using
  DLT as the shell around them.
* **Portability.** This file is Databricks-only. The rest of the repo runs on any
  Spark.

WHEN TO USE WHICH
-----------------
DLT for ingest and cleansing, where the work is repetitive and the value is in
expectations and automatic incrementalisation. Imperative jobs for the vault and the
marts, where load semantics *are* the design - insert-only hubs, hash-diff
satellites and allocated measures are not expressible as "declare a table".

That split is what this repo does: DLT here, plain PySpark in ``src/``.
"""

import dlt
from pyspark.sql import functions as F

CATALOG = spark.conf.get("catalog", "sportsbet_demo")  # noqa: F821 - provided by DLT
LANDING = spark.conf.get("landing_root", "/Volumes/sportsbet_demo/landing/wagering")  # noqa: F821


# ---------------------------------------------------------------- bronze (stream)


def _autoloader(dataset: str, fmt: str = "json"):
    """Streaming read of a landing directory via Auto Loader."""
    return (
        spark.readStream.format("cloudFiles")  # noqa: F821
        .option("cloudFiles.format", fmt)
        .option("cloudFiles.inferColumnTypes", "false")
        # Rescue any unexpected column into _rescued_data instead of dropping it.
        # In bronze this is always what you want: an upstream team adding a field
        # should never be a data loss event.
        .option("cloudFiles.schemaEvolutionMode", "rescue")
        .option("header", "true")
        .load(f"{LANDING}/{dataset}")
    )


@dlt.table(
    name="bronze_bets",
    comment="Raw bet slips exactly as the betting engine sent them.",
    table_properties={"quality": "bronze", "delta.enableChangeDataFeed": "true"},
)
def bronze_bets():
    return (
        _autoloader("bets")
        .withColumn("_ingested_at", F.current_timestamp())
        .withColumn("_source_file", F.col("_metadata.file_path"))
        .withColumn("_record_source", F.lit("betting_engine.bets"))
    )


@dlt.table(name="bronze_customers", comment="Raw CRM customer extract.",
           table_properties={"quality": "bronze"})
def bronze_customers():
    return (
        _autoloader("customers", fmt="csv")
        .withColumn("_ingested_at", F.current_timestamp())
        .withColumn("_source_file", F.col("_metadata.file_path"))
        .withColumn("_record_source", F.lit("crm.customers"))
    )


@dlt.table(name="bronze_settlements", comment="Raw settlement events.",
           table_properties={"quality": "bronze"})
def bronze_settlements():
    return (
        _autoloader("settlements")
        .withColumn("_ingested_at", F.current_timestamp())
        .withColumn("_record_source", F.lit("betting_engine.settlements"))
    )


# ------------------------------------------------------------------------ silver


@dlt.table(
    name="silver_bets",
    comment="Cleansed, typed, deduplicated bet slips.",
    table_properties={"quality": "silver"},
)
# These four decorators replace the whole of dq/expectations.py for this table.
# expect_or_drop quarantines the row and keeps the pipeline green; expect records
# the failure and keeps the row; expect_or_fail stops the update.
@dlt.expect_or_drop("bet_id_present", "bet_id IS NOT NULL")
@dlt.expect_or_drop("stake_positive", "stake_amount > 0")
@dlt.expect_or_drop("placed_at_parsed", "placed_at IS NOT NULL")
@dlt.expect("odds_at_least_one", "combined_odds >= 1")
def silver_bets():
    from pyspark.sql.window import Window

    # dlt.read_stream() on a streaming table gives you only the new rows, with
    # checkpointing handled. The equivalent imperative code needs an explicit
    # checkpoint location and a watermark column.
    raw = dlt.read_stream("bronze_bets")
    typed = raw.select(
        F.upper(F.trim("bet_id")).alias("bet_id"),
        F.upper(F.trim("customer_id")).alias("customer_id"),
        F.upper(F.trim("bet_type")).alias("bet_type"),
        F.upper(F.trim("channel")).alias("channel_code"),
        F.regexp_replace(F.col("stake_amount"), r"[$,\s]", "").cast("decimal(18,2)").alias("stake_amount"),
        F.col("combined_odds").cast("decimal(12,3)").alias("combined_odds"),
        F.to_timestamp("placed_at").alias("placed_at"),
        (F.upper(F.trim("in_play_flag")) == "Y").alias("is_in_play"),
        F.col("_record_source"),
        F.col("_ingested_at"),
    ).withColumn("placed_date", F.to_date("placed_at"))

    # Deduplication inside a stream needs care: row_number() over an unbounded
    # window is not supported on a streaming DataFrame. dropDuplicatesWithinWatermark
    # is the streaming-safe form - it bounds the state store by the watermark
    # rather than remembering every key forever.
    return (
        typed.withWatermark("placed_at", "2 days")
        .dropDuplicatesWithinWatermark(["bet_id"])
    )


@dlt.view(name="silver_customers_changes", comment="Typed CRM change feed for SCD2.")
@dlt.expect("customer_is_adult", "age_years >= 18")
def silver_customers_changes():
    return (
        dlt.read_stream("bronze_customers")
        .select(
            F.upper(F.trim("customer_id")).alias("customer_id"),
            F.initcap(F.trim("first_name")).alias("first_name"),
            F.initcap(F.trim("last_name")).alias("last_name"),
            F.upper(F.nullif(F.trim("state"), F.lit(""))).alias("state_code"),
            F.upper(F.trim("account_status")).alias("account_status"),
            F.upper(F.trim("vip_tier")).alias("vip_tier"),
            (F.upper(F.trim("self_excluded_flag")) == "Y").alias("is_self_excluded"),
            F.to_date("birth_date", "yyyy-MM-dd").alias("birth_date"),
            F.to_timestamp("updated_at").alias("source_updated_at"),
            F.col("_record_source"),
        )
        .withColumn(
            "age_years",
            F.floor(F.months_between(F.current_date(), F.col("birth_date")) / 12).cast("int"),
        )
    )


# ------------------------------------------------------- SCD2 without writing it

dlt.create_streaming_table(
    name="dim_customer",
    comment="Type 2 customer dimension, maintained by DLT from the CRM change feed.",
    table_properties={"quality": "gold"},
)

# This is the shortest honest comparison in the repo. Everything
# gold/dimensions.build_dim_customer does by hand - hash_diff change detection,
# lead() end-dating, is_current, one row per version - DLT does from this
# declaration. What you lose is the ability to back-date version 1 to 1900, which
# that function does deliberately so that bets predating the first extract still
# resolve. Getting that behaviour here means a downstream view instead.
dlt.create_auto_cdc_flow(
    target="dim_customer",
    source="silver_customers_changes",
    keys=["customer_id"],
    sequence_by=F.col("source_updated_at"),
    stored_as_scd_type=2,
    track_history_except_column_list=["source_updated_at", "_record_source"],
)


# -------------------------------------------------------------------------- gold


@dlt.table(
    name="gold_daily_turnover",
    comment="Daily turnover, payouts and hold % by channel.",
    table_properties={"quality": "gold"},
)
@dlt.expect_or_fail("turnover_not_negative", "turnover_amount >= 0")
def gold_daily_turnover():
    bets = dlt.read("silver_bets")
    settlements = (
        dlt.read("bronze_settlements")
        .select(
            F.upper(F.trim("bet_id")).alias("bet_id"),
            F.upper(F.trim("settlement_status")).alias("settlement_status"),
            F.col("payout_amount").cast("decimal(18,2)").alias("payout_amount"),
        )
        .dropDuplicates(["bet_id"])
    )
    return (
        bets.join(settlements, on="bet_id", how="left")
        .groupBy("placed_date", "channel_code")
        .agg(
            F.sum("stake_amount").alias("turnover_amount"),
            F.coalesce(F.sum("payout_amount"), F.lit(0)).alias("payout_amount"),
            (F.sum("stake_amount") - F.coalesce(F.sum("payout_amount"), F.lit(0)))
            .alias("gross_win_amount"),
            F.count(F.lit(1)).alias("bet_count"),
            F.countDistinct("customer_id").alias("active_customers"),
        )
        .withColumn(
            "hold_pct",
            F.round(100 * F.col("gross_win_amount") / F.nullif(F.col("turnover_amount"), F.lit(0)), 4),
        )
    )
