"""Table read/write helpers.

Four write patterns cover essentially every batch pipeline you will ever build,
and each layer of this lakehouse uses a different one:

===============  ==========================  ==================================
pattern          used by                     why
===============  ==========================  ==================================
replace_batch    bronze                      append, but replayable: re-running
                                             a batch replaces it instead of
                                             duplicating it
merge_into       silver, dim_* (SCD1)        upsert on a primary key
insert_new_only  hubs, links                 insert-only, never update
append           satellites, facts           new rows only, history preserved
===============  ==========================  ==================================

The Delta path uses real MERGE. The parquet fallback emulates it with an
anti-join and a full rewrite, which is what everyone did before Delta existed
and is worth seeing once so you understand what Delta actually bought us.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from .config import Config


# --------------------------------------------------------------------- schemas


def ensure_schemas(spark: SparkSession, cfg: Config) -> None:
    """Create the catalog/schemas if they do not exist.

    On Databricks with Unity Catalog the CREATE CATALOG usually happens once by
    an admin (see sql/00_unity_catalog_setup.sql) and jobs only create schemas.
    """
    for layer in cfg.layers:
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {cfg.schema(layer)}")


def table_exists(spark: SparkSession, name: str) -> bool:
    return spark.catalog.tableExists(name)


def read_table(spark: SparkSession, name: str) -> DataFrame:
    return spark.table(name)


def row_count(spark: SparkSession, name: str) -> int:
    return spark.table(name).count() if table_exists(spark, name) else 0


def drop_table(spark: SparkSession, name: str) -> None:
    spark.sql(f"DROP TABLE IF EXISTS {name}")


# ---------------------------------------------------------------------- writes


def write_table(
    df: DataFrame,
    name: str,
    cfg: Config,
    mode: str = "overwrite",
    partition_by: list[str] | None = None,
    comment: str | None = None,
) -> None:
    """Write a DataFrame as a managed table."""
    writer = df.write.format(cfg.table_format).mode(mode)
    if partition_by:
        writer = writer.partitionBy(*partition_by)
    if comment:
        writer = writer.option("comment", comment)
    if mode == "overwrite":
        # Schema evolution on a full overwrite is safe and saves you from having
        # to drop tables by hand every time you add a column during development.
        writer = writer.option("overwriteSchema", "true")
    writer.saveAsTable(name)


def append(df: DataFrame, name: str, cfg: Config, partition_by: list[str] | None = None) -> None:
    """Append rows, allowing new columns to appear (Delta schema evolution)."""
    writer = df.write.format(cfg.table_format).mode("append")
    if cfg.table_format == "delta":
        writer = writer.option("mergeSchema", "true")
    if partition_by:
        writer = writer.partitionBy(*partition_by)
    writer.saveAsTable(name)


def replace_batch(
    spark: SparkSession,
    df: DataFrame,
    name: str,
    cfg: Config,
    batch_col: str,
    batch_value,
    partition_by: list[str] | None = None,
) -> None:
    """Append a batch idempotently: delete any existing rows for that batch first.

    This is the single cheapest way to make an append-only ingest re-runnable.
    Without it, the first time a job fails halfway and gets retried you get
    duplicate raw rows, and every count downstream is quietly wrong.

    ``replaceWhere`` does the same thing atomically in one write on Delta; the
    explicit delete-then-append is used here because it also works on parquet.
    """
    if table_exists(spark, name):
        if cfg.table_format == "delta":
            spark.sql(f"DELETE FROM {name} WHERE {batch_col} = '{batch_value}'")
            append(df, name, cfg, partition_by)
            return
        keep = spark.table(name).where(F.col(batch_col) != F.lit(batch_value)).persist()
        keep.count()  # materialise before we overwrite the files we are reading
        write_table(keep.unionByName(df, allowMissingColumns=True), name, cfg,
                    mode="overwrite", partition_by=partition_by)
        keep.unpersist()
        return
    write_table(df, name, cfg, mode="overwrite", partition_by=partition_by)


def merge_into(
    spark: SparkSession,
    name: str,
    source: DataFrame,
    cfg: Config,
    keys: list[str],
    update: bool = True,
) -> None:
    """Upsert ``source`` into table ``name`` matching on ``keys``.

    The workhorse of the silver layer. Note the source must already be
    deduplicated on the merge keys - Delta raises an error if one target row
    matches multiple source rows, which is a feature, not an inconvenience:
    it catches the grain bugs that would otherwise silently pick a random row.
    """
    if not table_exists(spark, name):
        write_table(source, name, cfg)
        return

    condition = " AND ".join(f"t.{k} = s.{k}" for k in keys)

    if cfg.table_format == "delta":
        from delta.tables import DeltaTable

        builder = (
            DeltaTable.forName(spark, name)
            .alias("t")
            .merge(source.alias("s"), condition)
            .whenNotMatchedInsertAll()
        )
        if update:
            builder = builder.whenMatchedUpdateAll()
        builder.execute()
        return

    # Parquet fallback: anti-join out the rows being replaced, then rewrite.
    target = spark.table(name)
    if update:
        keep = target.join(source.select(*keys), on=keys, how="left_anti")
        result = keep.unionByName(source, allowMissingColumns=True)
    else:
        new_rows = source.join(target.select(*keys), on=keys, how="left_anti")
        result = target.unionByName(new_rows, allowMissingColumns=True)
    result = result.persist()
    result.count()
    write_table(result, name, cfg, mode="overwrite")
    result.unpersist()


def insert_new_only(
    spark: SparkSession,
    name: str,
    source: DataFrame,
    cfg: Config,
    keys: list[str],
) -> int:
    """Insert only rows whose ``keys`` are not already present. Never updates.

    This is the load pattern for Data Vault hubs and links, and it is why a
    vault is so replay-friendly: loading the same source file twice is a no-op,
    and two jobs loading the same hub concurrently cannot corrupt each other
    because neither one ever rewrites an existing row.

    Returns the number of rows actually inserted, which is a useful thing to log.
    """
    if not table_exists(spark, name):
        write_table(source, name, cfg)
        return source.count()

    new_rows = source.join(spark.table(name).select(*keys), on=keys, how="left_anti").persist()
    inserted = new_rows.count()
    if inserted:
        append(new_rows, name, cfg)
    new_rows.unpersist()
    return inserted


# ------------------------------------------------------------- maintenance ops


def optimize(spark: SparkSession, name: str, cfg: Config, zorder_by: list[str] | None = None) -> None:
    """Compact small files, optionally clustering by the columns you filter on.

    Small files are the number one performance problem in a real lakehouse: a
    streaming ingest that commits every 30 seconds produces 2,880 files a day
    per table, and every downstream query pays to list them. OPTIMIZE rewrites
    them into ~1GB files; ZORDER co-locates rows so that a filter on
    ``event_date`` skips whole files instead of reading them.

    On Databricks, predictive optimization does this for you on managed tables.
    """
    if cfg.table_format != "delta":
        return
    sql = f"OPTIMIZE {name}"
    if zorder_by:
        sql += f" ZORDER BY ({', '.join(zorder_by)})"
    try:
        spark.sql(sql)
    except Exception as exc:  # pragma: no cover - depends on Delta build
        print(f"  (skipped OPTIMIZE on {name}: {exc})")
