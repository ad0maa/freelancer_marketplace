"""Silver layer: cleansed, typed, deduplicated, conformed.

Silver is where the data becomes trustworthy. Four jobs, in this order:

1. **Type it.** Strings become dates, timestamps and decimals. Money is
   ``decimal``, never ``double`` - see ``money()`` for why.
2. **Deduplicate it.** Source systems deliver at-least-once. A window function
   keeps the latest version of each business key and throws the rest away.
3. **Standardise it.** Business keys trimmed and upper-cased, blank strings
   turned into real NULLs, Y/N flags turned into booleans.
4. **Conform it.** Light joins so that downstream code does not have to know
   that "sport" lives three tables away from "event".

Silver here is **append-only history plus a current view**, not overwrite-in-
place. Each batch's cleansed rows are appended with their ``_batch_id``, so when
a customer moves house in batch 2 we still hold what they were in batch 1. That
history is what makes the Data Vault satellites and the SCD2 dimension possible;
if silver overwrote, the history would be gone before modelling ever saw it.

The ``*_current`` tables are the exception: they are maintained with a Delta
MERGE and hold exactly one row per business key, which is what most consumers
actually want.
"""

from __future__ import annotations

from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

from .bronze import BATCH_ID, INGEST_TS, RECORD_SOURCE
from .config import Config
from .dq.expectations import Expectation, apply_expectations
from .io_utils import merge_into, replace_batch

AUDIT_COLS = [INGEST_TS, BATCH_ID, RECORD_SOURCE]


# --------------------------------------------------------- column-level helpers


def business_key(col: str) -> Column:
    """Standardise a business key: trimmed, upper-cased, blanks to NULL.

    Sounds trivial; is the single most common cause of duplicated customers in a
    warehouse. ``" c000123 "`` and ``"C000123"`` are the same person, and if you
    hash them into a Data Vault hub without normalising first you get two hubs,
    two satellites, and two rows in every report about them - and because the
    hash is opaque you will not notice until someone asks why the customer count
    went up by 4%.
    """
    return F.nullif(F.upper(F.trim(F.col(col))), F.lit(""))


def blank_to_null(col: str) -> Column:
    """Empty string is not a value. It is a missing value wearing a disguise."""
    return F.nullif(F.trim(F.col(col)), F.lit(""))


def money(col: str, precision: int = 18, scale: int = 2) -> Column:
    """Parse "$1,250.00" into decimal(18,2).

    Two deliberate choices here.

    **Decimal, not double.** Floating point cannot represent 0.10 exactly, so
    summing a few million stakes as doubles drifts by cents, and a finance team
    that reconciles turnover to the cent will find it. Spark's decimal arithmetic
    is exact and the cost is negligible at this scale.

    **try_cast, not cast.** Spark 4 turns ANSI SQL mode on by default, and under
    ANSI a bad cast *raises* instead of returning NULL - so a single malformed row
    would abort the whole run. ``try_cast`` returns NULL instead, which lets the
    DQ expectations catch and quarantine that one row while the other five million
    load normally. See the note at the bottom of this module.
    """
    cleaned = F.regexp_replace(F.trim(F.col(col)), r"[$,\s]", "")
    return F.nullif(cleaned, F.lit("")).try_cast(f"decimal({precision},{scale})")


def flag(col: str) -> Column:
    """Y/N/true/1 -> boolean, preserving NULL.

    NULL in gives NULL out rather than False. A missing flag is not the same
    statement as a flag set to "no", and collapsing the two throws away the
    distinction between "this customer has not self-excluded" and "the CRM did not
    tell us". Consumers that need a concrete answer coalesce explicitly - see the
    row filter in sql/00_unity_catalog_setup.sql.
    """
    return F.when(
        F.col(col).isNotNull(), F.upper(F.trim(F.col(col))).isin("Y", "YES", "TRUE", "1")
    )


def parse_date(col: str, *formats: str) -> Column:
    """Try each date format in order, returning NULL if none match.

    Needed because the CRM sends dd/MM/yyyy and everything else sends ISO.

    Built on ``try_to_timestamp`` rather than ``to_date`` because of ANSI mode:
    ``to_date('garbage', 'dd/MM/yyyy')`` raises under Spark 4 and would take the
    run down with it. The ``try_*`` variants are the ANSI-safe family, and the
    pattern to reach for whenever you are parsing data you do not control.

    A NULL result is not swallowed - each parsed column has a DQ expectation
    asserting it is not NULL, so an unparseable date shows up as a named failure
    in ``dq.dq_results`` instead of as a mysteriously empty dimension three weeks
    later.
    """
    attempts = [
        F.to_date(F.try_to_timestamp(F.trim(F.col(col)), F.lit(fmt))) for fmt in formats
    ]
    return F.coalesce(*attempts)


def parse_timestamp(col: str) -> Column:
    """Parse ISO-8601 with or without a timezone offset, ANSI-safely.

    ``2026-06-23T15:56:30+10:00`` carries an offset, so Spark converts it to the
    session timezone (Australia/Melbourne here). ``2026-06-13 23:50:00`` carries
    no offset and is interpreted *as* session-local time. Getting this wrong is
    how turnover ends up attributed to the wrong trading day.
    """
    trimmed = F.trim(F.col(col))
    return F.coalesce(
        F.try_to_timestamp(trimmed),
        F.try_to_timestamp(trimmed, F.lit("yyyy-MM-dd HH:mm:ss")),
        F.try_to_timestamp(F.substring(trimmed, 1, 19), F.lit("yyyy-MM-dd'T'HH:mm:ss")),
    )


def safe_int(col: str) -> Column:
    """ANSI-safe integer parse."""
    return F.trim(F.col(col)).try_cast("int")


def safe_decimal(col: str, precision: int, scale: int) -> Column:
    """ANSI-safe decimal parse, for numbers that are not money."""
    return F.trim(F.col(col)).try_cast(f"decimal({precision},{scale})")


def deduplicate(df: DataFrame, keys: list[str], order_by: list[Column]) -> DataFrame:
    """Keep one row per business key: the newest one.

    ``row_number()`` over a window, not ``dropDuplicates()``. The difference
    matters: ``dropDuplicates(["bet_id"])`` keeps an arbitrary row, so if the
    duplicate is a genuine update you get a coin flip. Ordering explicitly by the
    source's version column and taking row 1 makes "latest wins" deliberate.

    The trade-off is a shuffle. On a wide table it is cheaper to dedupe on the
    keys plus version column first and then join back, but at anything under a
    few hundred million rows this form is clearer and fast enough.
    """
    window = Window.partitionBy(*[F.col(k) for k in keys]).orderBy(*order_by)
    return (
        df.withColumn("_rn", F.row_number().over(window))
        .where(F.col("_rn") == 1)
        .drop("_rn")
    )


# ----------------------------------------------------------------- expectations

EXPECTATIONS: dict[str, list[Expectation]] = {
    "bets": [
        Expectation("bet_id_present", "bet_id IS NOT NULL", "Every bet must have an id"),
        Expectation("customer_present", "customer_id IS NOT NULL", "A bet without a customer cannot be attributed"),
        Expectation("stake_positive", "stake_amount IS NOT NULL AND stake_amount > 0",
                    "A zero or negative stake is a broken bet slip, not a bet"),
        Expectation("placed_at_parsed", "placed_at IS NOT NULL",
                    "If the timestamp failed to parse the bet cannot be attributed to a trading day"),
        Expectation("odds_at_least_one", "combined_odds >= 1", "Decimal odds below 1.0 are impossible",
                    severity="warn"),
        Expectation("stake_within_limits", "stake_amount <= 25000",
                    "Very large stakes are legitimate but should be reviewed", severity="warn"),
    ],
    "settlements": [
        Expectation("settlement_id_present", "settlement_id IS NOT NULL", "Settlements need an id"),
        Expectation("payout_not_negative", "payout_amount IS NOT NULL AND payout_amount >= 0",
                    "A negative payout would understate liability"),
        Expectation("status_known", "settlement_status IN ('WON','LOST','VOID','CASHED_OUT')",
                    "An unrecognised status means new business logic we have not modelled", severity="warn"),
    ],
    "customers": [
        Expectation("customer_id_present", "customer_id IS NOT NULL", "Customers need an id"),
        Expectation("signup_date_parsed", "signup_date IS NOT NULL",
                    "dd/mm/yyyy that failed to parse", severity="warn"),
        # Severity is 'warn', not 'drop', on purpose: dropping the customer would
        # orphan their bets and make turnover disagree with the ledger. A real
        # platform routes these to a compliance queue instead - the pipeline's job
        # is to make them impossible to miss, not to hide them.
        Expectation("customer_is_adult", "age_years IS NULL OR age_years >= 18",
                    "Under-18 account: a licence condition breach, not a data nit", severity="warn"),
        Expectation("state_recognised",
                    "state_code IS NULL OR state_code IN ('VIC','NSW','QLD','WA','SA','TAS','ACT','NT')",
                    "Unknown state code breaks jurisdictional reporting", severity="warn"),
    ],
    "bet_legs": [
        Expectation("leg_keys_present", "leg_id IS NOT NULL AND bet_id IS NOT NULL",
                    "A leg must belong to a bet"),
        Expectation("odds_taken_valid", "odds_taken IS NULL OR odds_taken >= 1",
                    "Odds taken below 1.0 is impossible", severity="warn"),
    ],
    "payments": [
        Expectation("payment_amount_positive", "amount IS NOT NULL AND amount > 0",
                    "Zero-value payments are noise"),
    ],
}


# ----------------------------------------------------------------- transforms


def _bronze_batch(spark: SparkSession, cfg: Config, name: str, batch: int) -> DataFrame:
    """Read one batch out of a bronze table.

    Because bronze is partitioned by ``_batch_id`` this is a partition prune, not
    a full scan - the physical plan skips the other batches' files entirely.
    """
    return spark.table(cfg.table("bronze", name)).where(F.col(BATCH_ID) == str(batch))


def transform_customers(df: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("customer_id").alias("customer_id"),
        F.initcap(F.trim(F.col("first_name"))).alias("first_name"),
        F.initcap(F.trim(F.col("last_name"))).alias("last_name"),
        F.lower(F.trim(F.col("email"))).alias("email"),
        parse_date("birth_date", "yyyy-MM-dd").alias("birth_date"),
        # The Australian one. ISO is tried as a fallback so that the day the CRM
        # team "fixes" their export, this keeps working.
        parse_date("signup_date", "dd/MM/yyyy", "yyyy-MM-dd").alias("signup_date"),
        F.upper(blank_to_null("state")).alias("state_code"),
        blank_to_null("suburb").alias("suburb"),
        blank_to_null("postcode").alias("postcode"),
        business_key("account_status").alias("account_status"),
        business_key("verification_status").alias("verification_status"),
        flag("self_excluded_flag").alias("is_self_excluded"),
        money("deposit_limit_weekly").alias("deposit_limit_weekly"),
        flag("marketing_opt_in").alias("is_marketing_opt_in"),
        business_key("vip_tier").alias("vip_tier"),
        parse_timestamp("updated_at").alias("source_updated_at"),
        *AUDIT_COLS,
    ).withColumn(
        # floor(months/12) rather than days/365.25: correct across leap years,
        # and it is the definition a regulator would use.
        "age_years",
        F.floor(F.months_between(F.current_date(), F.col("birth_date")) / 12).cast("int"),
    )
    return deduplicate(
        typed, ["customer_id"], [F.col("source_updated_at").desc(), F.col(INGEST_TS).desc()]
    )


def transform_events(df: DataFrame, competitions: DataFrame, sports: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("event_id").alias("event_id"),
        business_key("competition_id").alias("competition_id"),
        blank_to_null("event_name").alias("event_name"),
        blank_to_null("home_team").alias("home_team"),
        blank_to_null("away_team").alias("away_team"),
        blank_to_null("venue").alias("venue"),
        parse_timestamp("scheduled_start").alias("scheduled_start"),
        business_key("event_status").alias("event_status"),
        flag("live_betting_enabled").alias("is_live_betting_enabled"),
        *AUDIT_COLS,
    ).withColumn("event_date", F.to_date(F.col("scheduled_start")))

    # Conformance: pull sport down onto the event so that no downstream query has
    # to make this three-table hop again.
    #
    # F.broadcast() on the small side turns a shuffle join into a broadcast hash
    # join: the tiny table is shipped whole to every executor and the big table
    # never moves. With 6 sports and 9 competitions this is obviously right; AQE
    # would probably choose it anyway, but stating it is free and deterministic.
    return (
        typed.join(F.broadcast(competitions), on="competition_id", how="left")
        .join(F.broadcast(sports), on="sport_id", how="left")
        .select(
            typed["*"],
            F.col("competition_name"),
            F.col("sport_id"),
            F.col("sport_code"),
            F.col("sport_name"),
        )
    )


def transform_markets(df: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("market_id").alias("market_id"),
        business_key("event_id").alias("event_id"),
        business_key("market_type").alias("market_type"),
        blank_to_null("market_name").alias("market_name"),
        business_key("market_status").alias("market_status"),
        *AUDIT_COLS,
    )
    return deduplicate(typed, ["market_id"], [F.col(INGEST_TS).desc()])


def transform_selections(df: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("selection_id").alias("selection_id"),
        business_key("market_id").alias("market_id"),
        blank_to_null("selection_name").alias("selection_name"),
        safe_int("runner_number").alias("runner_number"),
        safe_decimal("decimal_odds", 10, 3).alias("decimal_odds"),
        business_key("selection_status").alias("selection_status"),
        parse_timestamp("price_updated_at").alias("price_updated_at"),
        *AUDIT_COLS,
    ).withColumn(
        # The bookmaker's implied probability. Sum these across a market and you
        # get the overround: 1.065 means the book is priced to keep 6.5%. Every
        # margin number in the gold layer is built from this column.
        "implied_probability",
        # try_divide, not "/": under ANSI mode a zero price would raise
        # DIVIDE_BY_ZERO and abort the batch. NULL is the right answer for a
        # nonsensical price, and the DQ rules pick it up from there.
        F.try_divide(F.lit(1), F.col("decimal_odds")).cast("decimal(9,6)"),
    )
    return deduplicate(
        typed, ["selection_id"], [F.col("price_updated_at").desc(), F.col(INGEST_TS).desc()]
    )


def transform_bets(df: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("bet_id").alias("bet_id"),
        business_key("customer_id").alias("customer_id"),
        business_key("bet_type").alias("bet_type"),
        business_key("channel").alias("channel_code"),
        money("stake_amount").alias("stake_amount"),
        safe_decimal("combined_odds", 12, 3).alias("combined_odds"),
        money("potential_payout").alias("potential_payout"),
        F.coalesce(business_key("currency_code"), F.lit("AUD")).alias("currency_code"),
        parse_timestamp("placed_at").alias("placed_at"),
        flag("in_play_flag").alias("is_in_play"),
        blank_to_null("promo_code").alias("promo_code"),
        business_key("bet_status").alias("bet_status"),
        *AUDIT_COLS,
    ).withColumn("placed_date", F.to_date(F.col("placed_at")))
    return deduplicate(typed, ["bet_id"], [F.col("placed_at").desc(), F.col(INGEST_TS).desc()])


def transform_bet_legs(df: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("leg_id").alias("leg_id"),
        business_key("bet_id").alias("bet_id"),
        safe_int("leg_number").alias("leg_number"),
        business_key("selection_id").alias("selection_id"),
        safe_decimal("odds_taken", 10, 3).alias("odds_taken"),
        *AUDIT_COLS,
    )
    return deduplicate(typed, ["leg_id"], [F.col(INGEST_TS).desc()])


def transform_settlements(df: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("settlement_id").alias("settlement_id"),
        business_key("bet_id").alias("bet_id"),
        business_key("settlement_status").alias("settlement_status"),
        money("payout_amount").alias("payout_amount"),
        parse_timestamp("settled_at").alias("settled_at"),
        business_key("settled_by").alias("settled_by"),
        *AUDIT_COLS,
    ).withColumn("settled_date", F.to_date(F.col("settled_at")))
    # Settlement files arrive shuffled, and a trader can manually re-settle a bet.
    # Latest settled_at wins.
    return deduplicate(
        typed, ["bet_id"], [F.col("settled_at").desc(), F.col(INGEST_TS).desc()]
    )


def transform_payments(df: DataFrame) -> DataFrame:
    typed = df.select(
        business_key("payment_id").alias("payment_id"),
        business_key("customer_id").alias("customer_id"),
        business_key("payment_type").alias("payment_type"),
        money("amount").alias("amount"),
        business_key("payment_method").alias("payment_method"),
        business_key("payment_status").alias("payment_status"),
        parse_timestamp("created_at").alias("created_at"),
        *AUDIT_COLS,
    ).withColumn("created_date", F.to_date(F.col("created_at")))
    return deduplicate(typed, ["payment_id"], [F.col(INGEST_TS).desc()])


def transform_reference(df: DataFrame, keys: list[str]) -> DataFrame:
    cols = [c for c in df.columns if c not in AUDIT_COLS]
    typed = df.select(*[F.trim(F.col(c)).alias(c) for c in cols], *AUDIT_COLS)
    return deduplicate(typed, keys, [F.col(INGEST_TS).desc()])


# ------------------------------------------------------------------- the layer

# Tables where one business key can legitimately have several versions over time,
# and therefore get a MERGE-maintained current-state companion table.
CURRENT_STATE_TABLES = {"customers": ["customer_id"], "selections": ["selection_id"]}


def build_silver(spark: SparkSession, cfg: Config, batch: int) -> dict[str, int]:
    """Build every silver table for one batch. Returns row counts written."""
    batch_id = str(batch)
    counts: dict[str, int] = {}

    # Reference data only lands in batch 1, so read the whole silver table (not
    # just this batch) when conforming events.
    for name, keys in (("sports", ["sport_id"]), ("competitions", ["competition_id"])):
        source = _bronze_batch(spark, cfg, name, batch)
        if not source.isEmpty():
            frame = transform_reference(source, keys)
            replace_batch(spark, frame, cfg.table("silver", name), cfg, BATCH_ID, batch_id)
            counts[name] = frame.count()

    sports = spark.table(cfg.table("silver", "sports")).select(
        "sport_id", "sport_code", "sport_name"
    )
    competitions = spark.table(cfg.table("silver", "competitions")).select(
        "competition_id", "sport_id", "competition_name"
    )

    frames: dict[str, DataFrame] = {
        "customers": transform_customers(_bronze_batch(spark, cfg, "customers", batch)),
        "events": transform_events(
            _bronze_batch(spark, cfg, "events", batch), competitions, sports
        ),
        "markets": transform_markets(_bronze_batch(spark, cfg, "markets", batch)),
        "selections": transform_selections(_bronze_batch(spark, cfg, "selections", batch)),
        "bets": transform_bets(_bronze_batch(spark, cfg, "bets", batch)),
        "bet_legs": transform_bet_legs(_bronze_batch(spark, cfg, "bet_legs", batch)),
        "settlements": transform_settlements(_bronze_batch(spark, cfg, "settlements", batch)),
        "payments": transform_payments(_bronze_batch(spark, cfg, "payments", batch)),
    }

    for name, frame in frames.items():
        if frame.isEmpty():
            continue
        target = cfg.table("silver", name)
        clean = apply_expectations(
            spark, frame, EXPECTATIONS.get(name, []), target, batch_id, cfg
        )
        # Append-only with an idempotent batch replace: history is preserved, and
        # re-running batch 2 does not double it.
        replace_batch(spark, clean, target, cfg, BATCH_ID, batch_id, partition_by=[BATCH_ID])
        counts[name] = clean.count()

        if name in CURRENT_STATE_TABLES:
            merge_into(
                spark, f"{target}_current", clean, cfg, keys=CURRENT_STATE_TABLES[name]
            )

    return counts


# ---------------------------------------------------------------------------
# A note on ANSI mode, because it changes how this whole layer has to be written.
#
# Spark 4 enables ANSI SQL mode by default (`spark.sql.ansi.enabled=true`). Under
# ANSI, these all raise instead of quietly returning NULL:
#
#     CAST('garbage' AS DOUBLE)          -> CAST_INVALID_INPUT
#     to_date('garbage', 'dd/MM/yyyy')   -> CANNOT_PARSE_TIMESTAMP
#     1 / 0                              -> DIVIDE_BY_ZERO
#
# That is a genuine improvement - silent NULLs from failed casts have hidden more
# data bugs than any other single behaviour in Spark - but it changes the job of a
# cleansing layer. One malformed row in a five-million-row batch will now abort the
# entire run rather than producing one NULL.
#
# So parsing anything you do not control goes through the `try_*` family:
#
#     try_cast(x AS DECIMAL(18,2))    money(), safe_decimal(), safe_int()
#     try_to_timestamp(x, fmt)        parse_date(), parse_timestamp()
#     try_divide(a, b)                implied_probability
#
# The value of that is not the NULL. It is that the NULL is paired with a DQ
# expectation which names the failure, counts it, and quarantines the row - so the
# bad row is *reported* rather than either crashing the batch or disappearing.
#
# Upgrading a Spark 3 pipeline to Spark 4 usually means finding every cast on
# externally-sourced data and making this exact change.
# ---------------------------------------------------------------------------
