"""Gold dimensions: the Kimball star schema, built from the raw vault.

Why build a star schema at all when the vault already holds everything? Because
the vault is optimised for loading and auditing, and a star schema is optimised
for being queried by people. "Turnover by sport by channel last Saturday" is one
join in a star and six joins plus two windows in a vault. Analysts and BI tools
will not do the six joins correctly every time, so you do it once, here.

The division of labour is worth stating plainly:

* the **raw vault** is the system of record - insert-only, auditable, no business
  rules, never rebuilt
* the **gold marts** are disposable - full of business rules, denormalised for
  speed, and safe to drop and rebuild from the vault whenever a definition changes

Which is exactly what these functions do: full rebuild, every run. At this size
that is seconds and it removes a whole class of incremental-load bug. Past roughly
a billion fact rows you switch the facts to incremental (partition overwrite by
date) and keep rebuilding the dimensions.

Surrogate keys here are **deterministic hashes** of the durable key, not identity
columns. The trade-off:

* hash: same input always gives the same key, so a full rebuild does not
  renumber the warehouse and any extract taken yesterday still joins. Costs 8
  bytes and is meaningless to read.
* identity: compact and monotonic, but a rebuild renumbers everything, which
  breaks anything that stored a key outside the warehouse.

For a mart that gets rebuilt, hashes win. Databricks does support
``GENERATED ALWAYS AS IDENTITY`` on Delta if you want the other trade-off.
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F

from ..config import Config
from ..io_utils import write_table
from ..vault.dv_helpers import satellite_current, satellite_scd2

UNKNOWN_KEY = -1
# Any fact whose dimension key cannot be resolved points here instead of being
# dropped or carrying a NULL. See the module docstring in facts.py for why.
UNKNOWN_LABEL = "Unknown"
LOW_DATE = "1900-01-01 00:00:00"
HIGH_DATE = "9999-12-31 23:59:59"


def surrogate_key(*cols: str | Column) -> Column:
    """Deterministic surrogate key from the durable business/hash key.

    ``abs(xxhash64(...))`` - xxhash64 is a fast non-cryptographic 64-bit hash, and
    unlike the SHA-256 hash keys in the vault it fits in a BIGINT, which keeps the
    fact tables narrow and the joins fast. abs() keeps it clear of the negative
    values reserved for unknown members.
    """
    parts = [F.col(c) if isinstance(c, str) else c for c in cols]
    return F.abs(F.xxhash64(*parts))


# ------------------------------------------------------------------- dim_date


def build_dim_date(spark: SparkSession, cfg: Config) -> int:
    """Calendar dimension, generated rather than derived from the data.

    Generated on purpose: a date dimension built from ``SELECT DISTINCT
    placed_date`` has holes on days with no betting, and then a time-series chart
    silently skips those days instead of showing a zero.
    """
    start = cfg.start_date.replace(day=1)
    end = cfg.end_date.replace(day=28)
    dates = spark.sql(
        f"SELECT explode(sequence(to_date('{start}') - INTERVAL 90 DAYS, "
        f"to_date('{end}') + INTERVAL 90 DAYS, INTERVAL 1 DAY)) AS full_date"
    )

    dim = dates.select(
        F.date_format("full_date", "yyyyMMdd").cast("int").alias("date_key"),
        F.col("full_date"),
        F.year("full_date").alias("calendar_year"),
        F.quarter("full_date").alias("calendar_quarter"),
        F.month("full_date").alias("month_number"),
        F.date_format("full_date", "MMMM").alias("month_name"),
        F.date_format("full_date", "yyyy-MM").alias("year_month"),
        F.dayofmonth("full_date").alias("day_of_month"),
        F.date_format("full_date", "EEEE").alias("day_name"),
        F.dayofweek("full_date").alias("day_of_week"),
        F.weekofyear("full_date").alias("iso_week"),
        F.dayofweek("full_date").isin(1, 7).alias("is_weekend"),
        # The Australian financial year runs 1 July to 30 June, and every internal
        # report at an ASX-listed operator is cut on it. Deriving it once here
        # stops fifteen analysts each writing their own CASE expression.
        F.when(F.month("full_date") >= 7, F.year("full_date"))
        .otherwise(F.year("full_date") - 1)
        .alias("financial_year_start"),
        F.concat(
            F.lit("FY"),
            F.when(F.month("full_date") >= 7, F.year("full_date"))
            .otherwise(F.year("full_date") - 1)
            .cast("string"),
            F.lit("/"),
            F.substring(
                F.when(F.month("full_date") >= 7, F.year("full_date") + 1)
                .otherwise(F.year("full_date"))
                .cast("string"),
                3, 2,
            ),
        ).alias("financial_year"),
    )

    unknown = spark.createDataFrame(
        [(UNKNOWN_KEY,)], "date_key int"
    ).select(
        "date_key",
        *[F.lit(None).cast(f.dataType).alias(f.name) for f in dim.schema.fields if f.name != "date_key"],
    )

    write_table(dim.unionByName(unknown), cfg.table("gold", "dim_date"), cfg)
    return dim.count() + 1


# --------------------------------------------------------------- dim_customer


def build_dim_customer(spark: SparkSession, cfg: Config) -> int:
    """Type 2 customer dimension, straight off ``sat_customer_details``.

    This is the join between the two modelling styles: an insert-only satellite
    plus one ``lead()`` window *is* an SCD2 dimension. Nothing else is needed.

    One deliberate business rule is applied here, and it belongs in gold rather
    than the vault: the earliest version of each customer is back-dated to 1900.
    The vault's ``load_date`` records when *we learned* something, not when it
    became true, so without back-dating, a bet placed before our first CRM extract
    would fail its as-at join and land on the unknown member. Back-dating version 1
    says "as far as this mart is concerned, this is what they always were", which
    is the correct reading and keeps the facts attributable.
    """
    hub = spark.table(cfg.table("vault", "hub_customer")).select("hk_customer", "customer_id")
    sat = spark.table(cfg.table("vault", "sat_customer_details"))
    versioned = satellite_scd2(sat, "hk_customer")

    dim = (
        versioned.join(hub, on="hk_customer", how="inner")
        .withColumn(
            "effective_from",
            F.when(F.col("version_number") == 1, F.lit(LOW_DATE).cast("timestamp")).otherwise(
                F.col("effective_from")
            ),
        )
        .select(
            surrogate_key("hk_customer", "effective_from").alias("customer_sk"),
            # The durable key: stable across all versions of a customer, which is
            # what you group by when you want "this customer" regardless of which
            # version of them a bet attached to.
            F.col("hk_customer").alias("customer_dk"),
            "customer_id",
            "first_name",
            "last_name",
            F.concat_ws(" ", "first_name", "last_name").alias("full_name"),
            "email",
            "birth_date",
            "signup_date",
            F.col("state_code"),
            "suburb",
            "postcode",
            "account_status",
            "verification_status",
            "is_self_excluded",
            "deposit_limit_weekly",
            "is_marketing_opt_in",
            "vip_tier",
            # Banding is a business rule, so it lives in the mart. The raw vault
            # keeps the birth date and nothing else, which means the bands can be
            # redefined next year without reloading history.
            F.when(F.col("birth_date").isNull(), F.lit(UNKNOWN_LABEL))
            .when(F.floor(F.months_between(F.current_date(), "birth_date") / 12) < 18, F.lit("Under 18"))
            .when(F.floor(F.months_between(F.current_date(), "birth_date") / 12) < 25, F.lit("18-24"))
            .when(F.floor(F.months_between(F.current_date(), "birth_date") / 12) < 35, F.lit("25-34"))
            .when(F.floor(F.months_between(F.current_date(), "birth_date") / 12) < 50, F.lit("35-49"))
            .when(F.floor(F.months_between(F.current_date(), "birth_date") / 12) < 65, F.lit("50-64"))
            .otherwise(F.lit("65+"))
            .alias("age_band"),
            "effective_from",
            "effective_to",
            "is_current",
            "version_number",
            F.col("record_source"),
        )
    )

    write_table(dim.unionByName(_unknown_row(spark, dim, "customer_sk")), cfg.table("gold", "dim_customer"), cfg)
    return dim.count() + 1


# ------------------------------------------------------------------ dim_event


def build_dim_event(spark: SparkSession, cfg: Config) -> int:
    """Event (fixture) dimension, type 1 - current state only.

    Type 1, not type 2, because nobody asks "what was this fixture's venue before
    it was rescheduled". Choosing type 1 where history has no business value is a
    real modelling decision, not laziness: an unnecessary type 2 dimension doubles
    its row count and forces every fact join to carry an as-at predicate.
    """
    hub = spark.table(cfg.table("vault", "hub_event")).select("hk_event", "event_id")
    sat = satellite_current(spark, cfg, "sat_event_details", "hk_event")

    dim = hub.join(sat, on="hk_event", how="left").select(
        surrogate_key("hk_event").alias("event_sk"),
        F.col("hk_event").alias("event_dk"),
        "event_id",
        F.coalesce("event_name", F.lit(UNKNOWN_LABEL)).alias("event_name"),
        "home_team",
        "away_team",
        F.coalesce("venue", F.lit(UNKNOWN_LABEL)).alias("venue"),
        "scheduled_start",
        "event_date",
        F.coalesce("event_status", F.lit(UNKNOWN_LABEL)).alias("event_status"),
        F.coalesce("is_live_betting_enabled", F.lit(False)).alias("is_live_betting_enabled"),
        "competition_id",
        F.coalesce("competition_name", F.lit(UNKNOWN_LABEL)).alias("competition_name"),
        F.coalesce("sport_code", F.lit(UNKNOWN_LABEL)).alias("sport_code"),
        F.coalesce("sport_name", F.lit(UNKNOWN_LABEL)).alias("sport_name"),
        F.col("record_source"),
    )

    write_table(dim.unionByName(_unknown_row(spark, dim, "event_sk")), cfg.table("gold", "dim_event"), cfg)
    return dim.count() + 1


# -------------------------------------------------------------- dim_selection


def build_dim_selection(spark: SparkSession, cfg: Config) -> int:
    """Selection dimension, with market and event attributes folded in.

    Deliberately denormalised: market type, market name and the parent event id
    all live on the selection row rather than in a ``dim_market`` the fact would
    have to join separately. Kimball's advice is to prefer few wide dimensions
    over many narrow ones, because every extra join is a chance for an analyst to
    get it wrong and a cost on every query. The vault keeps them properly
    separated (hub_market, sat_market_details, link_selection_market), so nothing
    is lost - this is a presentation choice, made once, here.

    Only selections we actually hold prices for get a row. The 8 selection ids
    that appear on bet legs but never in the pricing feed stay out, and their legs
    resolve to the unknown member - visible, countable, and not silently averaged
    into the margin numbers.
    """
    hub = spark.table(cfg.table("vault", "hub_selection")).select("hk_selection", "selection_id")
    sat = satellite_current(spark, cfg, "sat_selection_price", "hk_selection")
    link = spark.table(cfg.table("vault", "link_selection_market")).select(
        "hk_selection", "hk_market", "market_id"
    )
    market_sat = satellite_current(spark, cfg, "sat_market_details", "hk_market").select(
        "hk_market", "market_type", "market_name", "market_status"
    )
    market_event = spark.table(cfg.table("vault", "link_market_event")).select(
        "hk_market", "event_id"
    )

    dim = (
        hub.join(sat, on="hk_selection", how="inner")
        .join(link, on="hk_selection", how="left")
        .join(market_sat, on="hk_market", how="left")
        .join(market_event, on="hk_market", how="left")
        .select(
            surrogate_key("hk_selection").alias("selection_sk"),
            F.col("hk_selection").alias("selection_dk"),
            "selection_id",
            F.coalesce("selection_name", F.lit(UNKNOWN_LABEL)).alias("selection_name"),
            "runner_number",
            F.col("decimal_odds").alias("current_decimal_odds"),
            F.col("implied_probability").alias("current_implied_probability"),
            "selection_status",
            "market_id",
            F.coalesce("market_type", F.lit(UNKNOWN_LABEL)).alias("market_type"),
            F.coalesce("market_name", F.lit(UNKNOWN_LABEL)).alias("market_name"),
            "market_status",
            "event_id",
            F.col("record_source"),
        )
    )

    write_table(
        dim.unionByName(_unknown_row(spark, dim, "selection_sk")),
        cfg.table("gold", "dim_selection"), cfg,
    )
    return dim.count() + 1


# ---------------------------------------------------- small conformed dimensions

CHANNEL_ROWS = [
    ("IOS_APP", "iOS App", "APP", True),
    ("ANDROID_APP", "Android App", "APP", True),
    ("WEB_DESKTOP", "Web Desktop", "WEB", False),
    ("WEB_MOBILE", "Web Mobile", "WEB", True),
    ("RETAIL", "Retail Venue", "RETAIL", False),
]

BET_TYPE_ROWS = [
    ("SINGLE", "Single", 1, "One selection; stake returns at that selection's odds."),
    ("MULTI", "Multi", 2, "Several selections across different events; all must win, odds multiply."),
    ("SGM", "Same Game Multi", 2, "A multi whose legs are all within one event; legs are correlated."),
]


def build_dim_channel(spark: SparkSession, cfg: Config) -> int:
    """Channel dimension from a curated list, not from the data.

    Built from a hardcoded list on purpose. If it were built with SELECT DISTINCT
    over the facts, a typo in one source row would silently create a new channel,
    and a channel with no bets this month would vanish from the dimension and take
    its zero row off the report. Curated small dimensions are a feature.
    """
    rows = spark.createDataFrame(
        CHANNEL_ROWS, "channel_code string, channel_name string, channel_group string, is_mobile boolean"
    )
    dim = rows.select(surrogate_key("channel_code").alias("channel_sk"), "*")
    write_table(
        dim.unionByName(_unknown_row(spark, dim, "channel_sk")),
        cfg.table("gold", "dim_channel"), cfg,
    )
    return dim.count() + 1


def build_dim_bet_type(spark: SparkSession, cfg: Config) -> int:
    rows = spark.createDataFrame(
        BET_TYPE_ROWS,
        "bet_type_code string, bet_type_name string, min_legs int, bet_type_description string",
    )
    dim = rows.select(surrogate_key("bet_type_code").alias("bet_type_sk"), "*")
    write_table(
        dim.unionByName(_unknown_row(spark, dim, "bet_type_sk")),
        cfg.table("gold", "dim_bet_type"), cfg,
    )
    return dim.count() + 1


# ---------------------------------------------------------------------- helpers


def _unknown_row(spark: SparkSession, dim: DataFrame, sk_col: str) -> DataFrame:
    """Build the ``-1`` unknown member for a dimension.

    Every dimension gets one. It is what lets a fact with an unresolvable
    dimension key still be loaded, at the right grain, with its measures intact -
    so turnover still balances to the source system even when a fixture has not
    arrived yet. The alternative, dropping the fact row, makes the warehouse
    quietly disagree with the ledger, which is far worse than a row labelled
    "Unknown" that someone can go and investigate.
    """
    fields = []
    for field in dim.schema.fields:
        if field.name == sk_col:
            fields.append(F.lit(UNKNOWN_KEY).cast(field.dataType).alias(field.name))
        elif field.dataType.simpleString() == "string":
            fields.append(F.lit(UNKNOWN_LABEL).alias(field.name))
        elif field.name == "effective_from":
            fields.append(F.lit(LOW_DATE).cast(field.dataType).alias(field.name))
        elif field.name == "effective_to":
            fields.append(F.lit(HIGH_DATE).cast(field.dataType).alias(field.name))
        elif field.name == "is_current":
            fields.append(F.lit(True).alias(field.name))
        else:
            fields.append(F.lit(None).cast(field.dataType).alias(field.name))
    return spark.range(1).select(*fields)


def build_all_dimensions(spark: SparkSession, cfg: Config) -> dict[str, int]:
    """Rebuild every dimension. Order does not matter; they are independent."""
    return {
        "dim_date": build_dim_date(spark, cfg),
        "dim_customer": build_dim_customer(spark, cfg),
        "dim_event": build_dim_event(spark, cfg),
        "dim_selection": build_dim_selection(spark, cfg),
        "dim_channel": build_dim_channel(spark, cfg),
        "dim_bet_type": build_dim_bet_type(spark, cfg),
    }
