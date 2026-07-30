"""Bronze layer: land the source data exactly as it arrived, plus provenance.

The rule for bronze is: **do not fix anything**. Every column is read as a
string, nothing is cast, nothing is filtered, nothing is deduplicated. If the
source sent you "$1,250.00" then bronze stores "$1,250.00".

That feels wrong the first time you see it. The reason is replay: when someone
finds a bug in the silver logic eighteen months from now, you want to be able to
rebuild silver and gold from bronze without going back to the source systems -
which by then have already aged out their history, changed their schema, or been
decommissioned. The moment bronze applies business logic, it stops being a
faithful record and you have lost that ability.

What bronze *does* add is provenance - four audit columns that let you answer
"where did this row come from and when did we get it", which is the first
question asked in every single data incident.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StringType, StructField, StructType

from .config import Config
from .io_utils import replace_batch

# Audit columns prefixed with "_" so they never collide with a source column.
INGEST_TS = "_ingested_at"
SOURCE_FILE = "_source_file"
BATCH_ID = "_batch_id"
RECORD_SOURCE = "_record_source"


@dataclass(frozen=True)
class SourceDataset:
    """One source extract and where it lands.

    ``record_source`` follows the Data Vault convention of ``system.entity``. It
    is carried all the way through to the satellites, so in the mart you can
    still answer "which system told us this customer lives in Geelong".
    """

    name: str
    fmt: str
    columns: list[str]
    record_source: str
    landing_dataset: str | None = None
    file_pattern: str = "*"

    @property
    def source_dir(self) -> str:
        return self.landing_dataset or self.name

    @property
    def schema(self) -> StructType:
        # Everything is a string in bronze. An explicit schema (rather than
        # inferSchema) matters for three reasons: it never triggers an extra
        # inference pass over the data, it cannot change shape between runs
        # because one file happened to have an integer-looking column, and a
        # column the source stops sending shows up as NULL instead of vanishing.
        return StructType([StructField(c, StringType(), True) for c in self.columns])


SOURCES: list[SourceDataset] = [
    SourceDataset(
        "customers", "csv",
        ["customer_id", "first_name", "last_name", "email", "birth_date", "signup_date",
         "state", "suburb", "postcode", "account_status", "verification_status",
         "self_excluded_flag", "deposit_limit_weekly", "marketing_opt_in", "vip_tier",
         "updated_at"],
        "crm.customers",
    ),
    SourceDataset(
        "events", "json",
        ["event_id", "competition_id", "event_name", "home_team", "away_team", "venue",
         "scheduled_start", "event_status", "live_betting_enabled"],
        "fixtures.events",
    ),
    SourceDataset(
        "markets", "json",
        ["market_id", "event_id", "market_type", "market_name", "market_status"],
        "trading.markets",
    ),
    SourceDataset(
        "selections", "json",
        ["selection_id", "market_id", "selection_name", "runner_number", "decimal_odds",
         "selection_status", "price_updated_at"],
        "pricing.selections",
    ),
    SourceDataset(
        "bets", "json",
        ["bet_id", "customer_id", "bet_type", "channel", "stake_amount", "combined_odds",
         "potential_payout", "currency_code", "placed_at", "in_play_flag", "promo_code",
         "bet_status"],
        "betting_engine.bets",
    ),
    SourceDataset(
        "bet_legs", "json",
        ["leg_id", "bet_id", "leg_number", "selection_id", "odds_taken"],
        "betting_engine.bet_legs",
    ),
    SourceDataset(
        "settlements", "json",
        ["settlement_id", "bet_id", "settlement_status", "payout_amount", "settled_at",
         "settled_by"],
        "betting_engine.settlements",
    ),
    SourceDataset(
        "payments", "csv",
        ["payment_id", "customer_id", "payment_type", "amount", "payment_method",
         "payment_status", "created_at"],
        "payments.transactions",
    ),
    # Reference data arrives as two small files in one directory.
    SourceDataset(
        "sports", "json", ["sport_id", "sport_name", "sport_code"],
        "fixtures.sports", landing_dataset="reference", file_pattern="sports.json",
    ),
    SourceDataset(
        "competitions", "json", ["competition_id", "sport_id", "competition_name"],
        "fixtures.competitions", landing_dataset="reference", file_pattern="competitions.json",
    ),
]


def _read_landing(spark: SparkSession, cfg: Config, source: SourceDataset, batch: int) -> DataFrame | None:
    path = Path(cfg.landing_path(source.source_dir, batch))
    if not path.exists():
        return None  # e.g. reference data only lands in batch 1
    location = str(path / source.file_pattern) if source.file_pattern != "*" else str(path)

    reader = spark.read.schema(source.schema)
    if source.fmt == "csv":
        # mode=PERMISSIVE is the default and is what you want in bronze: a
        # malformed line becomes NULLs rather than killing the job. FAILFAST
        # belongs in silver, where you have already decided what "valid" means.
        return reader.option("header", "true").csv(location)
    return reader.json(location)


def ingest_batch(spark: SparkSession, cfg: Config, batch: int) -> dict[str, int]:
    """Land one batch of every source dataset into bronze.

    Re-running the same batch replaces it rather than duplicating it, so a
    half-failed run can simply be retried - see ``io_utils.replace_batch``.
    """
    counts: dict[str, int] = {}
    for source in SOURCES:
        df = _read_landing(spark, cfg, source, batch)
        if df is None:
            continue

        enriched = (
            df.withColumn(INGEST_TS, F.current_timestamp())
            # input_file_name() is how you trace a bad row back to the exact
            # file it came from. Worth its weight in gold at 2am.
            .withColumn(SOURCE_FILE, F.input_file_name())
            .withColumn(BATCH_ID, F.lit(str(batch)))
            .withColumn(RECORD_SOURCE, F.lit(source.record_source))
        )

        target = cfg.table("bronze", source.name)
        replace_batch(
            spark, enriched, target, cfg,
            batch_col=BATCH_ID, batch_value=str(batch), partition_by=[BATCH_ID],
        )
        counts[source.name] = enriched.count()
    return counts


# ---------------------------------------------------------------------------
# The production version of the same thing.
# ---------------------------------------------------------------------------
def ingest_stream_autoloader(spark: SparkSession, cfg: Config, source: SourceDataset):
    """Auto Loader equivalent of ``ingest_batch`` - the Databricks-native path.

    This is how bronze ingest is actually written on Databricks, and it is worth
    understanding what it replaces. The batch version above has to be told which
    batch to load. Auto Loader instead tracks which files it has already seen in
    a RocksDB checkpoint, so you point it at a directory once and it picks up new
    files forever - no watermark table, no "load yesterday" parameter, no gap
    when a file lands late.

    ``availableNow=True`` makes it behave like a batch job that catches up on
    everything new and then stops, which is what you want on a scheduled
    Workflow. Drop it and the same code becomes a continuous stream.

    Not executed in the local demo because ``cloudFiles`` is a Databricks
    feature; kept here because reading it is the point.
    """
    checkpoint = f"{cfg.checkpoint_dir}/bronze/{source.name}"
    stream = (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", source.fmt)
        .option("cloudFiles.schemaLocation", f"{checkpoint}/schema")
        # Rescue any column the source starts sending that we did not expect,
        # instead of silently dropping it.
        .option("cloudFiles.schemaEvolutionMode", "rescue")
        .option("header", "true")
        .schema(source.schema)
        .load(cfg.landing_path(source.source_dir))
        .withColumn(INGEST_TS, F.current_timestamp())
        .withColumn(SOURCE_FILE, F.col("_metadata.file_path"))
        .withColumn(RECORD_SOURCE, F.lit(source.record_source))
    )
    return (
        stream.writeStream.format("delta")
        .option("checkpointLocation", f"{checkpoint}/commits")
        .outputMode("append")
        .trigger(availableNow=True)
        .toTable(cfg.table("bronze", source.name))
    )
