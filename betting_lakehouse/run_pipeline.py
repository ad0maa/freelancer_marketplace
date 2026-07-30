#!/usr/bin/env python3
"""Run the whole betting lakehouse locally, end to end.

    python run_pipeline.py --fresh      # wipe, generate two batches, run everything
    python run_pipeline.py --batch 2    # run batch 2 again (proves idempotency)
    python run_pipeline.py --show-only  # just print the results of the last run

On Databricks this file does not exist. Its job is done by a Workflow with one
task per layer (see databricks.yml), and each task calls the same functions this
script calls. That is the point of keeping the layers as importable functions
rather than scripts: the orchestrator is swappable.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pyspark.sql import functions as F  # noqa: E402

from betting_lakehouse import bronze, generate_source_data, silver  # noqa: E402
from betting_lakehouse.config import Config  # noqa: E402
from betting_lakehouse.gold import dimensions, facts  # noqa: E402
from betting_lakehouse.io_utils import ensure_schemas  # noqa: E402
from betting_lakehouse.spark import get_spark  # noqa: E402
from betting_lakehouse.vault import raw_vault  # noqa: E402

WIDTH = 78


def header(text: str) -> None:
    print(f"\n\033[1m{'=' * WIDTH}\n {text}\n{'=' * WIDTH}\033[0m")


def step(text: str) -> None:
    print(f"\n\033[1m-- {text} {'-' * max(0, WIDTH - len(text) - 4)}\033[0m")


def counts_table(counts: dict[str, int], label: str = "rows") -> None:
    if not counts:
        print("  (nothing to load)")
        return
    for name, value in counts.items():
        print(f"  {name:<32} {value:>10,} {label}")


# ---------------------------------------------------------------------- layers


def run_batch(spark, cfg: Config, batch: int) -> None:
    """Bronze -> silver -> raw vault for one landing batch."""
    step(f"BRONZE  landing -> bronze  (batch {batch})")
    started = time.time()
    counts_table(bronze.ingest_batch(spark, cfg, batch))
    print(f"  ({time.time() - started:.1f}s)")

    step(f"SILVER  bronze -> silver  (batch {batch})")
    started = time.time()
    counts_table(silver.build_silver(spark, cfg, batch))
    print(f"  ({time.time() - started:.1f}s)")

    step(f"RAW VAULT  silver -> hubs, links, satellites  (batch {batch})")
    started = time.time()
    inserted = raw_vault.build_raw_vault(spark, cfg, batch)
    counts_table(inserted, "inserted")
    total = sum(inserted.values())
    print(f"  ({time.time() - started:.1f}s)")
    if total == 0:
        print("\n  Nothing was inserted: every row in this batch was already in the vault.")
        print("  That is the insert-only design working - re-running a load is a no-op.")


def rebuild_marts(spark, cfg: Config) -> None:
    """Full rebuild of the dimensional marts from the vault."""
    step("GOLD  raw vault -> dimensions")
    started = time.time()
    counts_table(dimensions.build_all_dimensions(spark, cfg))
    print(f"  ({time.time() - started:.1f}s)")

    step("GOLD  raw vault -> facts and aggregates")
    started = time.time()
    counts_table(facts.build_all_facts(spark, cfg))
    print(f"  ({time.time() - started:.1f}s)")


# ---------------------------------------------------------------------- output


def show_warehouse(spark, cfg: Config) -> None:
    header("WHAT IS IN THE WAREHOUSE")
    for layer in ("bronze", "silver", "vault", "gold", "dq"):
        schema = cfg.schema(layer)
        try:
            tables = [r["tableName"] for r in spark.sql(f"SHOW TABLES IN {schema}").collect()]
        except Exception:
            continue
        print(f"\n  \033[1m{schema}\033[0m")
        for table in sorted(tables):
            n = spark.table(f"{schema}.{table}").count()
            print(f"    {table:<34} {n:>10,} rows")


def show_data_quality(spark, cfg: Config) -> None:
    header("DATA QUALITY")
    results = spark.table(cfg.table("dq", "dq_results"))
    failing = (
        results.groupBy("table_name", "expectation", "severity", "description")
        .agg(F.sum("rows_failed").alias("rows_failed"), F.sum("rows_checked").alias("rows_checked"))
        .where(F.col("rows_failed") > 0)
        .orderBy(F.col("rows_failed").desc())
    )
    if failing.isEmpty():
        print("\n  Every expectation passed.")
    else:
        print("\n  Expectations with failures:")
        for row in failing.collect():
            print(f"    [{row['severity']:<4}] {row['expectation']:<24} "
                  f"{row['rows_failed']:>4} / {row['rows_checked']:,} rows")
            print(f"           {row['description']}")
    quarantined = spark.table(cfg.table("dq", "quarantine")).count()
    print(f"\n  Rows quarantined (kept, not silently dropped): {quarantined:,}")


def show_scd2(spark, cfg: Config) -> None:
    header("TYPE 2 HISTORY: WHAT dim_customer LOOKS LIKE WHEN SOMEONE CHANGES")
    dim = spark.table(cfg.table("gold", "dim_customer"))
    changed = (
        dim.where(F.col("customer_sk") != -1)
        .groupBy("customer_id")
        .agg(F.count(F.lit(1)).alias("versions"))
        .where(F.col("versions") > 1)
    )
    total_changed = changed.count()
    print(f"\n  {total_changed} customers have more than one version in the dimension.")
    if total_changed:
        # Prefer a self-exclusion, because it is the change that matters most here.
        excluded = (
            dim.join(changed.select("customer_id"), on="customer_id")
            .where(F.col("is_self_excluded") & F.col("is_current"))
            .select("customer_id")
            .limit(1)
        )
        pick = excluded.collect() or changed.select("customer_id").limit(1).collect()
        customer_id = pick[0]["customer_id"]
        print(f"  Full history for {customer_id}:\n")
        (
            dim.where(F.col("customer_id") == customer_id)
            .orderBy("version_number")
            .select(
                "version_number", "state_code", "account_status", "vip_tier",
                "is_self_excluded", "effective_from", "effective_to", "is_current",
            )
            .show(truncate=False)
        )
        print("  Version 1 starts at 1900-01-01 so that bets placed before our first")
        print("  extract still resolve to a customer. See dimensions.build_dim_customer.")


def show_gold(spark, cfg: Config) -> None:
    header("THE NUMBERS THE BUSINESS ACTUALLY ASKS FOR")

    print("\n  Turnover, gross win and hold % by sport (all channels):\n")
    spark.sql(f"""
        SELECT sport_name,
               SUM(turnover_amount)                                          AS turnover,
               SUM(gross_win_amount)                                         AS gross_win,
               ROUND(100 * SUM(gross_win_amount) / SUM(turnover_amount), 2)   AS hold_pct,
               SUM(bet_count)                                                AS bets
        FROM {cfg.table("gold", "agg_daily_sport_channel")}
        GROUP BY sport_name
        ORDER BY turnover DESC
    """).show(truncate=False)

    print("\n  Turnover by channel group:\n")
    spark.sql(f"""
        SELECT channel_group,
               SUM(turnover_amount)                                        AS turnover,
               ROUND(100 * SUM(gross_win_amount) / SUM(turnover_amount), 2) AS hold_pct,
               SUM(leg_count)                                              AS legs
        FROM {cfg.table("gold", "agg_daily_sport_channel")}
        GROUP BY channel_group ORDER BY turnover DESC
    """).show(truncate=False)

    print("\n  A few rows of fact_bet_leg - the star schema's centre:\n")
    spark.table(cfg.table("gold", "fact_bet_leg")).select(
        "bet_id", "leg_number", "placed_date_key", "customer_sk", "event_sk",
        "selection_sk", "legs_in_bet", "odds_taken", "bet_stake_amount", "stake_allocated",
    ).orderBy(F.col("legs_in_bet").desc(), "bet_id").show(6, truncate=False)


def unknown_member_counts(spark, cfg: Config) -> dict[str, int]:
    """How many fact rows currently point at an unknown dimension member."""
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    return {
        "selection_sk": legs.where(F.col("selection_sk") == -1).count(),
        "event_sk": legs.where(F.col("event_sk") == -1).count(),
        "customer_sk": legs.where(F.col("customer_sk") == -1).count(),
    }


def show_late_arrivals(snapshots: list[tuple[int, dict[str, int]]]) -> None:
    """Show the late-arriving fixtures resolving themselves between batches."""
    if len(snapshots) < 2:
        return
    header("LATE-ARRIVING DIMENSIONS, RESOLVING THEMSELVES")
    print("\n  Legs pointing at an unknown dimension member, after each batch:\n")
    print(f"    {'':<16}" + "".join(f"after batch {b:<6}" for b, _ in snapshots))
    for key in ("event_sk", "selection_sk", "customer_sk"):
        row = "".join(f"{counts[key]:<19}" for _, counts in snapshots)
        print(f"    {key:<16}{row}")
    first, last = snapshots[0][1], snapshots[-1][1]
    resolved = first["event_sk"] - last["event_sk"]
    print(f"\n  {resolved} legs were placed on fixtures that had not reached the warehouse yet.")
    print("  They were loaded anyway, against the unknown member, so turnover was")
    print("  never understated. When batch 2 brought the fixtures in, the mart rebuild")
    print("  attached them to the real event with no correction or backfill needed.")
    print(f"  The {last['event_sk']} still unresolved are the genuine orphans: selection ids")
    print("  that appear on bet legs but exist in no pricing feed at all.")


def show_integrity(spark, cfg: Config) -> None:
    header("DOES IT ADD UP?")
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    slips = spark.table(cfg.table("gold", "fact_bet_settlement"))

    leg_turnover = legs.agg(F.sum("stake_allocated")).collect()[0][0] or 0
    slip_turnover = slips.agg(F.sum("stake_amount")).collect()[0][0] or 0
    diff = abs(float(leg_turnover) - float(slip_turnover))
    print(f"\n  Turnover from the leg fact (allocated) : ${float(leg_turnover):,.2f}")
    print(f"  Turnover from the slip fact (exact)    : ${float(slip_turnover):,.2f}")
    print(f"  Difference                             : ${diff:,.2f}"
          f"  {'OK' if diff < 1 else 'MISMATCH'}")
    print("  Allocated measures must sum back to the true total. This is the check")
    print("  that catches a fan-out bug before finance does.")

    unknown_selection = legs.where(F.col("selection_sk") == -1).count()
    unknown_event = legs.where(F.col("event_sk") == -1).count()
    unknown_customer = legs.where(F.col("customer_sk") == -1).count()
    print(f"\n  Legs pointing at the unknown member:")
    print(f"    selection_sk = -1 : {unknown_selection:>5}  (selection never appeared in the pricing feed)")
    print(f"    event_sk     = -1 : {unknown_event:>5}  (fixture had not arrived when the bet was placed)")
    print(f"    customer_sk  = -1 : {unknown_customer:>5}")
    print("  These rows are still counted in turnover. Dropping them would make the")
    print("  warehouse disagree with the betting engine, which is the worse failure.")


def show_all(spark, cfg: Config, snapshots: list[tuple[int, dict[str, int]]] | None = None) -> None:
    show_warehouse(spark, cfg)
    show_data_quality(spark, cfg)
    show_scd2(spark, cfg)
    show_gold(spark, cfg)
    show_integrity(spark, cfg)
    show_late_arrivals(snapshots or [])


# ------------------------------------------------------------------------ main


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true",
                        help="delete all local data and run both batches from scratch")
    parser.add_argument("--batch", type=int, choices=(1, 2),
                        help="run a single batch instead of both")
    parser.add_argument("--show-only", action="store_true", help="print the last run's results")
    parser.add_argument("--skip-generate", action="store_true",
                        help="do not regenerate landing files")
    args = parser.parse_args()

    cfg = Config.load()

    if args.fresh:
        for path in (cfg.landing_dir.parent, Path("derby.log")):
            if path.exists():
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        print("Wiped local data. Starting from an empty lakehouse.")

    batches = [args.batch] if args.batch else [1, 2]

    if not args.show_only and not args.skip_generate:
        header("GENERATE SOURCE DATA (standing in for CRM, betting engine, pricing, payments)")
        for batch in batches:
            counts = generate_source_data.write_batch(cfg, batch)
            print(f"\n  batch {batch}:")
            counts_table(counts)

    spark = get_spark("betting-lakehouse-local", cfg)
    snapshots: list[tuple[int, dict[str, int]]] = []
    try:
        if not args.show_only:
            ensure_schemas(spark, cfg)
            for batch in batches:
                header(
                    f"BATCH {batch}  "
                    + ("initial load" if batch == 1 else "next day's incremental load")
                )
                run_batch(spark, cfg, batch)
                # The marts are rebuilt after every batch. Batch 2 is what makes
                # the late-arriving fixtures resolve: legs that pointed at the
                # unknown member after batch 1 get their real event_sk here.
                rebuild_marts(spark, cfg)
                snapshots.append((batch, unknown_member_counts(spark, cfg)))

        show_all(spark, cfg, snapshots)

        header("NEXT")
        print("\n  Run it again and watch nothing change:")
        print("      python run_pipeline.py --batch 2 --skip-generate")
        print("  Every vault load should insert 0 rows. That is idempotency, and it is")
        print("  the property that lets you re-run a failed job without thinking.\n")
        print("  Then read, in this order:")
        print("      docs/01_pyspark_primer.md          what Spark is doing underneath")
        print("      docs/02_databricks_primer.md       what Databricks adds on top")
        print("      docs/03_data_vault_vs_dimensional.md   why both models exist here")
        print("      src/betting_lakehouse/vault/dv_helpers.py   the code worth reading twice\n")
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
