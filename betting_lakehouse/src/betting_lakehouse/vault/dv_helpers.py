"""Generic Data Vault 2.0 loaders.

Data Vault splits every entity into three table types and nothing else:

**Hub** - the list of business keys that exist. One row per real-world thing,
ever. ``hub_customer`` is "these are the customers we have heard of". No
attributes, no dates other than when we first saw the key.

**Link** - the fact that two or more business keys are related. ``link_bet_selection``
is "this bet included this selection". Links are always many-to-many, even when
today's source system says one-to-many, which is the point: when the business
starts allowing something new, the link already supports it.

**Satellite** - the descriptive attributes, with history. Every time an attribute
changes, a new row is inserted. ``sat_customer_details`` holds what the CRM said
about a customer at each point in time.

Three properties fall out of that structure, and they are the whole reason a
regulated business chooses it:

* **Insert-only.** Nothing is ever updated or deleted, so the vault is auditable
  by construction - you can always show what you knew and when you knew it.
  For a bookmaker being asked by a regulator "what did you know about this
  customer's self-exclusion status on the 14th", that is not a nice-to-have.
* **Load-order independent.** Hubs, links and satellites can be loaded in
  parallel and in any order, because none of them depends on another's rows.
  Add a source system and you add satellites; you do not rewrite the model.
* **Replayable.** Loading the same batch twice inserts nothing the second time.

What it is *not* is queryable by humans. Answering "turnover by sport last
Saturday" from a vault means joining six tables and windowing two of them. That
is what the gold layer is for, and why both models exist in this repo rather than
one winning.

Hashing conventions used here (they matter, and shops differ):

* SHA-256 over the business key, upper-cased and trimmed, ``||`` separated
* NULL is replaced with ``^^`` so that ``(A, NULL)`` and ``(A, '')`` differ
* satellites carry a ``hash_diff`` over the payload, so change detection is one
  string comparison instead of a column-by-column diff
"""

from __future__ import annotations

from datetime import datetime

from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

from ..config import Config
from ..io_utils import append, insert_new_only, table_exists

# Data Vault standard column names.
HK = "hash_key"          # prefix for hash keys, e.g. hk_customer
LOAD_DATE = "load_date"
RECORD_SOURCE = "record_source"
HASH_DIFF = "hash_diff"

NULL_TOKEN = "^^"
DELIMITER = "||"


def _normalised(col: str | Column) -> Column:
    column = F.col(col) if isinstance(col, str) else col
    return F.coalesce(F.upper(F.trim(column.cast("string"))), F.lit(NULL_TOKEN))


def hash_key(*cols: str | Column) -> Column:
    """SHA-256 hash key over one or more business keys.

    Why hash at all instead of a sequence number? Because a hash can be computed
    independently by every job, on every source, in parallel, without a lookup.
    A sequence needs a central allocator, which serialises your loads and becomes
    the bottleneck the moment you have more than one source feeding a hub.

    The cost is that hash keys are wide (64 hex chars) and meaningless to read.
    Some shops use ``xxhash64`` for the size win; SHA-256 is used here because
    collision risk is effectively zero and it is the DV2.0 default.

    Order matters: ``hash_key("bet_id", "selection_id")`` and the reverse produce
    different values, so the column order in a link definition is part of its
    contract and must never be reshuffled.
    """
    parts = [_normalised(c) for c in cols]
    return F.sha2(F.concat_ws(DELIMITER, *parts), 256)


def hash_diff(*cols: str | Column) -> Column:
    """SHA-256 over a satellite's payload, used only for change detection.

    Comparing one hash beats comparing forty nullable columns with IS DISTINCT
    FROM, and it keeps the satellite loader generic. The trap: add a column to
    the payload and every hash_diff changes, so the next load inserts a new
    version of every row. That is not corruption, but it is a surprise if you did
    not expect the satellite to double in size - plan payload changes.
    """
    parts = [_normalised(c) for c in cols]
    return F.sha2(F.concat_ws(DELIMITER, *parts), 256)


# ---------------------------------------------------------------------- hubs


def load_hub(
    spark: SparkSession,
    cfg: Config,
    name: str,
    source: DataFrame,
    business_keys: list[str],
    hk_col: str,
    record_source: str | Column,
    load_date: datetime,
) -> int:
    """Insert any business keys not already in the hub. Returns rows inserted.

    Note what is absent: no update, no comparison of attributes, no end-dating.
    A hub row says only "this key exists", which is why it never needs to change.
    """
    rs = F.lit(record_source) if isinstance(record_source, str) else record_source
    candidates = (
        source.where(F.col(business_keys[0]).isNotNull())
        .select(
            hash_key(*business_keys).alias(hk_col),
            *[F.col(bk) for bk in business_keys],
            F.lit(load_date).cast("timestamp").alias(LOAD_DATE),
            rs.alias(RECORD_SOURCE),
        )
        # The same key can appear many times in one batch (one customer, fifty
        # bets). Collapse to one row before the anti-join.
        .dropDuplicates([hk_col])
    )
    return insert_new_only(spark, cfg.table("vault", name), candidates, cfg, keys=[hk_col])


# ---------------------------------------------------------------------- links


def load_link(
    spark: SparkSession,
    cfg: Config,
    name: str,
    source: DataFrame,
    link_hk_col: str,
    parents: dict[str, list[str]],
    record_source: str | Column,
    load_date: datetime,
    dependent_child_keys: list[str] | None = None,
) -> int:
    """Insert new relationships into a link table.

    ``parents`` maps each parent hash-key column to the business keys it is built
    from, e.g. ``{"hk_bet": ["bet_id"], "hk_selection": ["selection_id"]}``.

    ``dependent_child_keys`` are columns that are part of the relationship's
    identity but are not a business key in their own right - ``leg_number`` is the
    canonical example. Leg 1 and leg 2 of the same bet on the same selection are
    two distinct legs, so leg_number has to be in the link's hash or the second
    one silently disappears. This is the single most common Data Vault modelling
    mistake and it shows up as quietly undercounted rows.
    """
    dependent_child_keys = dependent_child_keys or []
    all_bks = [bk for bks in parents.values() for bk in bks] + dependent_child_keys
    rs = F.lit(record_source) if isinstance(record_source, str) else record_source

    candidates = (
        source.where(
            # A relationship needs both ends. Rows missing a parent key go
            # nowhere near the vault; the mart handles them with an unknown member.
            F.greatest(*[F.col(bk).isNull().cast("int") for bk in all_bks]) == 0
        )
        .select(
            hash_key(*all_bks).alias(link_hk_col),
            *[hash_key(*bks).alias(hk) for hk, bks in parents.items()],
            *[F.col(bk) for bk in all_bks],
            F.lit(load_date).cast("timestamp").alias(LOAD_DATE),
            rs.alias(RECORD_SOURCE),
        )
        .dropDuplicates([link_hk_col])
    )
    return insert_new_only(spark, cfg.table("vault", name), candidates, cfg, keys=[link_hk_col])


# ----------------------------------------------------------------- satellites


def load_satellite(
    spark: SparkSession,
    cfg: Config,
    name: str,
    source: DataFrame,
    hk_col: str,
    business_keys: list[str],
    payload: list[str],
    record_source: str | Column,
    load_date: datetime,
    version_col: str | None = None,
) -> int:
    """Insert a new satellite row only where the payload actually changed.

    The comparison is against the most recent existing row for that hash key
    *as of this batch's load_date* - not simply the latest row. That distinction
    matters when a batch is replayed out of order: comparing against a future row
    would either insert a spurious version or skip a real one.

    ``version_col`` is the source's own change timestamp. When several versions of
    the same key arrive in one batch (a price that moved three times before the
    extract ran), it decides which one is kept. Note the consequence: intermediate
    versions inside a single batch are lost. If you need every tick, the satellite
    has to be loaded from a stream, not a daily batch - a real trade-off, not an
    oversight.
    """
    rs = F.lit(record_source) if isinstance(record_source, str) else record_source
    target_name = cfg.table("vault", name)

    incoming = source.where(F.col(business_keys[0]).isNotNull()).select(
        hash_key(*business_keys).alias(hk_col),
        F.lit(load_date).cast("timestamp").alias(LOAD_DATE),
        rs.alias(RECORD_SOURCE),
        hash_diff(*payload).alias(HASH_DIFF),
        *[F.col(c) for c in payload],
        *([F.col(version_col)] if version_col and version_col not in payload else []),
    )

    if version_col:
        order = [F.col(version_col).desc()]
        window = Window.partitionBy(hk_col).orderBy(*order)
        incoming = (
            incoming.withColumn("_rn", F.row_number().over(window))
            .where(F.col("_rn") == 1)
            .drop("_rn")
        )
    else:
        incoming = incoming.dropDuplicates([hk_col])

    if not table_exists(spark, target_name):
        rows = incoming.count()
        if rows:
            append(incoming, target_name, cfg)
        return rows

    # Latest known payload per hash key, as at this batch.
    existing = spark.table(target_name).where(F.col(LOAD_DATE) <= F.lit(load_date))
    latest_window = Window.partitionBy(hk_col).orderBy(F.col(LOAD_DATE).desc())
    latest = (
        existing.withColumn("_rn", F.row_number().over(latest_window))
        .where(F.col("_rn") == 1)
        .select(F.col(hk_col), F.col(HASH_DIFF).alias("_prev_hash_diff"))
    )

    changed = (
        incoming.join(latest, on=hk_col, how="left")
        # NULL-safe inequality: a brand new key has no previous hash, and
        # `!=` against NULL would be NULL (i.e. not true) and drop the row.
        .where(~F.col(HASH_DIFF).eqNullSafe(F.col("_prev_hash_diff")))
        .drop("_prev_hash_diff")
    ).persist()

    inserted = changed.count()
    if inserted:
        append(changed, target_name, cfg)
    changed.unpersist()
    return inserted


def satellite_current(
    spark: SparkSession, cfg: Config, name: str, hk_col: str
) -> DataFrame:
    """The most recent version of each key in a satellite.

    Used everywhere a mart wants "what is true now" rather than "what was true
    then". A real deployment would materialise this as a view next to the
    satellite so that consumers cannot get the window wrong.
    """
    window = Window.partitionBy(hk_col).orderBy(F.col(LOAD_DATE).desc())
    return (
        spark.table(cfg.table("vault", name))
        .withColumn("_rn", F.row_number().over(window))
        .where(F.col("_rn") == 1)
        .drop("_rn")
    )


def satellite_scd2(
    df: DataFrame,
    hk_col: str,
    load_date_col: str = LOAD_DATE,
    high_date: str = "9999-12-31 23:59:59",
) -> DataFrame:
    """Turn an insert-only satellite into effective-dated SCD2 rows.

    Satellites deliberately store only ``load_date`` - the moment a version
    started. They do not store an end date, because writing one would mean
    updating the previous row, and that would break the insert-only guarantee
    that makes the vault auditable.

    So the end dates are derived at read time with ``lead()``: the next version's
    load_date is this version's end. That is the bridge between Data Vault and
    dimensional modelling - ``dim_customer`` is built by running exactly this
    function over ``sat_customer_details``.

    The open interval ends at 9999-12-31 rather than NULL so that
    ``BETWEEN effective_from AND effective_to`` works without a special case for
    the current row. Small thing; saves a NULL-handling bug in every join.
    """
    window = Window.partitionBy(hk_col).orderBy(F.col(load_date_col).asc())
    next_load = F.lead(F.col(load_date_col)).over(window)
    return (
        df.withColumn("effective_from", F.col(load_date_col))
        .withColumn(
            "effective_to",
            # One second before the next version starts, so intervals never
            # overlap and an as-at join can only ever match one row.
            F.when(next_load.isNotNull(), next_load - F.expr("INTERVAL 1 SECOND")).otherwise(
                F.lit(high_date).cast("timestamp")
            ),
        )
        .withColumn("is_current", next_load.isNull())
        .withColumn("version_number", F.row_number().over(window))
    )
