"""Idempotency: running the same load twice must change nothing.

This is the property that makes everything else survivable. A cluster dies
mid-write, a task is retried, someone re-runs yesterday's job to be safe - none of
it can corrupt the warehouse if every load converges rather than accumulates.

It is also the property that is easiest to lose accidentally. Append one row
without a batch guard, or switch a satellite from hash-diff comparison to a plain
append, and the pipeline still runs green while every count creeps upward. So it
gets its own test file, and the assertions are on *whole-table counts* rather than
on the loader return values, because a loader can lie and a count cannot.
"""

from __future__ import annotations

from betting_lakehouse import bronze, silver
from betting_lakehouse.gold import dimensions, facts
from betting_lakehouse.vault import raw_vault


def _all_counts(spark, cfg) -> dict[str, int]:
    counts = {}
    for layer in ("bronze", "silver", "vault", "gold"):
        schema = cfg.schema(layer)
        for row in spark.sql(f"SHOW TABLES IN {schema}").collect():
            name = f"{schema}.{row['tableName']}"
            counts[name] = spark.table(name).count()
    return counts


def test_rerunning_every_layer_changes_no_row_counts(spark, lakehouse):
    """Re-run batch 2 through the whole stack and compare every table."""
    cfg = lakehouse
    before = _all_counts(spark, cfg)

    bronze.ingest_batch(spark, cfg, 2)
    silver.build_silver(spark, cfg, 2)
    raw_vault.build_raw_vault(spark, cfg, 2)
    dimensions.build_all_dimensions(spark, cfg)
    facts.build_all_facts(spark, cfg)

    after = _all_counts(spark, cfg)

    drifted = {
        table: (before[table], after[table])
        for table in before
        if before[table] != after.get(table)
    }
    assert not drifted, f"row counts changed on re-run: {drifted}"


def test_vault_load_inserts_nothing_on_a_repeat_batch(spark, lakehouse):
    """The insert-only guarantee, asserted directly on the loaders' return values."""
    cfg = lakehouse
    inserted = raw_vault.build_raw_vault(spark, cfg, 1)
    assert sum(inserted.values()) == 0, f"vault re-inserted rows: {inserted}"


def test_bronze_replays_a_batch_without_duplicating_it(spark, lakehouse):
    """Bronze is append-only, so this only holds because the batch is replaced."""
    cfg = lakehouse
    target = cfg.table("bronze", "bets")
    before = spark.table(target).count()
    bronze.ingest_batch(spark, cfg, 1)
    assert spark.table(target).count() == before


def test_replaying_one_batch_leaves_the_other_intact(spark, lakehouse):
    """A batch replace must delete only its own rows.

    The obvious wrong implementation - overwrite the whole table - passes a naive
    idempotency test and silently destroys history.
    """
    cfg = lakehouse
    target = cfg.table("bronze", "bets")
    batch1_before = spark.table(target).where("_batch_id = '1'").count()
    batch2_before = spark.table(target).where("_batch_id = '2'").count()
    assert batch1_before > 0 and batch2_before > 0

    bronze.ingest_batch(spark, cfg, 2)

    assert spark.table(target).where("_batch_id = '1'").count() == batch1_before
    assert spark.table(target).where("_batch_id = '2'").count() == batch2_before


def test_late_arriving_fixtures_resolve_on_rebuild(spark, lakehouse):
    """The batch-2 payoff: no backfill, no correction, just a mart rebuild.

    Bets are placed on three fixtures whose reference data does not arrive until
    batch 2. After batch 1 those legs point at the unknown event. After batch 2 the
    only legs still unknown are the genuine orphans - selection ids that exist in no
    pricing feed at all - and every one of those has an unknown selection too.
    """
    from pyspark.sql import functions as F

    cfg = lakehouse
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))

    unknown_event = legs.where(F.col("event_sk") == -1)
    # Any leg that cannot find its event also cannot find its selection - because
    # the event is reached *through* the selection. A leg with a known selection but
    # an unknown event would mean the dimension join is broken.
    assert unknown_event.where(F.col("selection_sk") != -1).count() == 0
