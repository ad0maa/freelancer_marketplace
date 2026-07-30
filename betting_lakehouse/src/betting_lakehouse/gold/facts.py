"""Gold facts: the transactional tables at the centre of the star.

Two facts, two grains, and the reason there are two is the most important idea in
this file.

``fact_bet_leg`` - one row per leg of a bet. This is where sport, event and
selection live, because those only make sense at leg level: a four-leg multi
across AFL, NRL and two races has no single sport.

``fact_bet_settlement`` - one row per bet slip. This is where the financials live,
because stake and payout are properties of the slip, not of a leg.

**Never mix them.** Putting stake on the leg fact and summing it multiplies
turnover by the number of legs - a 4-leg multi with a $10 stake reports $40 of
turnover. This is the single most common way a betting warehouse produces numbers
that finance refuses to sign off, and it always looks plausible until someone
checks the total.

The leg fact does still need a stake-shaped measure for "turnover by sport", so it
carries an **allocated** one: stake divided by the number of legs. Allocated
measures are additive by construction - they sum back to the true total across any
grouping - and they are clearly named so nobody confuses them with the real stake.
That is the standard Kimball answer to a measure that lives at a coarser grain than
the fact.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.window import Window

from ..config import Config
from ..io_utils import optimize, write_table
from ..vault.dv_helpers import satellite_current
from . import metrics as M
from .dimensions import UNKNOWN_KEY

SETTLED_STATUSES = ("WON", "LOST", "VOID", "CASHED_OUT")


def _bet_core(spark: SparkSession, cfg: Config) -> DataFrame:
    """Bet-level attributes assembled from the vault: details, customer, settlement.

    Reassembling one logical entity from a hub, a link and two satellites is
    exactly the cost of a Data Vault, and exactly why the gold layer exists - this
    join is written once here instead of by every analyst who wants a bet.
    """
    details = satellite_current(spark, cfg, "sat_bet_details", "hk_bet").select(
        "hk_bet", "bet_type", "channel_code", "stake_amount", "combined_odds",
        "potential_payout", "currency_code", "placed_at", "placed_date", "is_in_play",
        "promo_code",
    )
    bet_customer = spark.table(cfg.table("vault", "link_bet_customer")).select(
        "hk_bet", "hk_customer", "customer_id"
    )
    settlement = satellite_current(spark, cfg, "sat_bet_settlement", "hk_bet").select(
        "hk_bet", "settlement_status", "payout_amount", "settled_at", "settled_date",
        "settled_by",
    )
    bets = spark.table(cfg.table("vault", "hub_bet")).select("hk_bet", "bet_id")

    return (
        bets.join(details, on="hk_bet", how="inner")
        .join(bet_customer, on="hk_bet", how="left")
        # LEFT, not INNER: an unsettled bet is a real bet and must appear in
        # turnover the day it was placed, even though its payout is not known yet.
        # An inner join here would silently understate today's turnover and then
        # "fix itself" tomorrow, which is worse than being wrong consistently.
        .join(settlement, on="hk_bet", how="left")
    )


def _resolve_customer_as_at(
    source: DataFrame, spark: SparkSession, cfg: Config, event_time_col: str
) -> DataFrame:
    """Attach ``customer_sk`` as the customer was *at the time of the bet*.

    This is the entire payoff of a type 2 dimension, and it is worth being precise
    about what it buys. A customer who lived in NSW in June and moved to VIC in
    July has two rows in ``dim_customer``. Joining a June bet to the *current* row
    would report that bet as VIC turnover, and the NSW jurisdictional return would
    be wrong. Joining on the effective-date range attributes it to NSW, where it
    happened.

    The range predicate means this is not an equi-join, so Spark cannot use a hash
    join on it alone: it hashes on the durable key and applies the range as a
    filter. Keep the durable key in the condition (as here) and it stays fast;
    join on the range alone and you get a nested loop over the whole dimension.
    """
    dim = spark.table(cfg.table("gold", "dim_customer")).select(
        F.col("customer_sk"),
        F.col("customer_dk").alias("_dim_customer_dk"),
        F.col("effective_from").alias("_dim_from"),
        F.col("effective_to").alias("_dim_to"),
    )
    return (
        source.join(
            dim,
            (F.col("hk_customer") == F.col("_dim_customer_dk"))
            & (F.col(event_time_col) >= F.col("_dim_from"))
            & (F.col(event_time_col) <= F.col("_dim_to")),
            how="left",
        )
        .withColumn("customer_sk", F.coalesce("customer_sk", F.lit(UNKNOWN_KEY)))
        .drop("_dim_customer_dk", "_dim_from", "_dim_to")
    )


def _lookup(spark: SparkSession, cfg: Config, dim: str, key: str, sk: str) -> DataFrame:
    """Small dimension lookup, broadcast on purpose."""
    return F.broadcast(
        spark.table(cfg.table("gold", dim)).select(F.col(key), F.col(sk)).where(F.col(sk) != UNKNOWN_KEY)
    )


# ------------------------------------------------------------- fact_bet_leg


def build_fact_bet_leg(spark: SparkSession, cfg: Config) -> int:
    """One row per leg. Sport/event/selection analysis happens here."""
    legs = spark.table(cfg.table("vault", "link_bet_selection")).select(
        "hk_bet_selection", "hk_bet", "hk_selection", "bet_id", "selection_id", "leg_number"
    )
    leg_odds = satellite_current(spark, cfg, "sat_bet_leg_odds", "hk_bet_selection").select(
        "hk_bet_selection", "odds_taken"
    )

    # How many legs the parent bet has - needed to allocate bet-level money down
    # to leg grain. A window over the link table, so it is correct even for the
    # legs whose selection never made it into the pricing feed.
    legs = legs.withColumn(
        "legs_in_bet", F.count(F.lit(1)).over(Window.partitionBy("hk_bet"))
    )

    selections = spark.table(cfg.table("gold", "dim_selection")).select(
        F.col("selection_dk").alias("_sel_dk"),
        "selection_sk",
        F.col("event_id").alias("_sel_event_id"),
        F.col("market_type"),
    )
    events = spark.table(cfg.table("gold", "dim_event")).select(
        F.col("event_id").alias("_evt_id"), "event_sk"
    )

    # bet_id is dropped from the bet-level frame because the link table already
    # carries it; keeping both would make every later reference to `bet_id`
    # ambiguous, which Spark reports at analysis time rather than guessing.
    core = _bet_core(spark, cfg).drop("bet_id")
    joined = (
        legs.join(leg_odds, on="hk_bet_selection", how="left")
        # INNER on the bet, and this is the one place that is deliberate. Note the
        # contrast with the dimension joins below, which are all LEFT with a
        # fallback to the unknown member:
        #
        #   unknown dimension -> keep the leg, point at -1. The bet was real, so
        #       its stake is real turnover; we just cannot describe an attribute.
        #   no bet at all     -> drop the leg. A bet quarantined at silver for a
        #       zero stake is not a bet, so its legs are not turnover. Keeping
        #       them would put money in the fact the betting engine never took.
        #
        # The vault still holds those legs in link_bet_selection, because the vault
        # records what the source said whether or not the mart wants it.
        .join(core, on="hk_bet", how="inner")
        .join(selections, legs["hk_selection"] == F.col("_sel_dk"), how="left")
        .join(events, F.col("_sel_event_id") == F.col("_evt_id"), how="left")
        .join(_lookup(spark, cfg, "dim_channel", "channel_code", "channel_sk"),
              on="channel_code", how="left")
        .join(_lookup(spark, cfg, "dim_bet_type", "bet_type_code", "bet_type_sk"),
              core["bet_type"] == F.col("bet_type_code"), how="left")
    )
    joined = _resolve_customer_as_at(joined, spark, cfg, "placed_at")

    legs_in_bet = F.col("legs_in_bet").cast("decimal(9,0)")
    fact = joined.select(
        # ---- foreign keys ------------------------------------------------
        F.date_format("placed_date", "yyyyMMdd").cast("int").alias("placed_date_key"),
        F.col("customer_sk"),
        F.coalesce("event_sk", F.lit(UNKNOWN_KEY)).alias("event_sk"),
        F.coalesce("selection_sk", F.lit(UNKNOWN_KEY)).alias("selection_sk"),
        F.coalesce("channel_sk", F.lit(UNKNOWN_KEY)).alias("channel_sk"),
        F.coalesce("bet_type_sk", F.lit(UNKNOWN_KEY)).alias("bet_type_sk"),
        # ---- degenerate dimensions --------------------------------------
        # Kept on the fact rather than given their own dimension table: they are
        # identifiers with no attributes of their own, and analysts need them to
        # drill back to a specific bet slip.
        F.col("bet_id"),
        F.col("leg_number"),
        F.col("selection_id"),
        F.col("market_type"),
        # ---- timestamps --------------------------------------------------
        F.col("placed_at"),
        F.col("placed_date"),
        # ---- measures ----------------------------------------------------
        F.col("legs_in_bet"),
        F.col("odds_taken"),
        # Non-additive at this grain. Named so that summing it looks wrong.
        F.col("stake_amount").alias("bet_stake_amount"),
        F.col("combined_odds").alias("bet_combined_odds"),
        # Additive: sums back to true turnover across any grouping of legs.
        (F.col("stake_amount") / legs_in_bet).cast("decimal(18,4)").alias(M.STAKE_ALLOC),
        (F.coalesce("payout_amount", F.lit(0)) / legs_in_bet)
        .cast("decimal(18,4)").alias(M.PAYOUT_ALLOC),
        (
            (F.col("stake_amount") - F.coalesce("payout_amount", F.lit(0))) / legs_in_bet
        ).cast("decimal(18,4)").alias("gross_win_allocated"),
        # ---- flags -------------------------------------------------------
        F.col("is_in_play"),
        F.col("settlement_status"),
        F.col("settlement_status").isNotNull().alias("is_settled"),
        (F.col("settlement_status") == F.lit("WON")).alias("is_bet_won"),
        F.col("promo_code").isNotNull().alias("has_promo"),
    )

    target = cfg.table("gold", "fact_bet_leg")
    write_table(fact, target, cfg)
    # Not partitioned. 9,000 rows across 28 days would give tiny partitions and
    # thousands of small files - the classic over-partitioning mistake. Partition
    # a fact by date only once each daily partition is comfortably over ~1GB;
    # below that, ZORDER (or liquid clustering on Databricks) gives you the file
    # skipping without the file-count explosion.
    optimize(spark, target, cfg, zorder_by=["placed_date", "event_sk"])
    return fact.count()


# ------------------------------------------------------ fact_bet_settlement


def build_fact_bet_settlement(spark: SparkSession, cfg: Config) -> int:
    """One row per bet slip. All money lives here, unallocated and exact."""
    core = _bet_core(spark, cfg)
    leg_counts = (
        spark.table(cfg.table("vault", "link_bet_selection"))
        .groupBy("hk_bet")
        .agg(F.count(F.lit(1)).alias("legs_in_bet"))
    )

    joined = (
        core.join(leg_counts, on="hk_bet", how="left")
        .join(_lookup(spark, cfg, "dim_channel", "channel_code", "channel_sk"),
              on="channel_code", how="left")
        .join(_lookup(spark, cfg, "dim_bet_type", "bet_type_code", "bet_type_sk"),
              core["bet_type"] == F.col("bet_type_code"), how="left")
    )
    joined = _resolve_customer_as_at(joined, spark, cfg, "placed_at")

    fact = joined.select(
        F.date_format("placed_date", "yyyyMMdd").cast("int").alias("placed_date_key"),
        # A second date FK on the same dimension - a "role-playing dimension".
        # In SQL you alias dim_date twice; the fact just carries both keys.
        F.coalesce(F.date_format("settled_date", "yyyyMMdd").cast("int"), F.lit(UNKNOWN_KEY))
        .alias("settled_date_key"),
        F.col("customer_sk"),
        F.coalesce("channel_sk", F.lit(UNKNOWN_KEY)).alias("channel_sk"),
        F.coalesce("bet_type_sk", F.lit(UNKNOWN_KEY)).alias("bet_type_sk"),
        F.col("bet_id"),
        F.col("customer_id"),
        F.col("hk_customer").alias("customer_dk"),
        F.col("placed_at"),
        F.col("placed_date"),
        F.col("settled_at"),
        F.col("settled_date"),
        F.col("legs_in_bet"),
        F.col("bet_type"),
        F.col("currency_code"),
        # Exact, unallocated money at its natural grain.
        F.col("stake_amount"),
        F.col("combined_odds"),
        F.col("potential_payout"),
        F.coalesce("payout_amount", F.lit(0)).cast("decimal(18,2)").alias("payout_amount"),
        (F.col("stake_amount") - F.coalesce("payout_amount", F.lit(0)))
        .cast("decimal(18,2)").alias("gross_win_amount"),
        F.coalesce("settlement_status", F.lit("PENDING")).alias("settlement_status"),
        F.col("settled_by"),
        F.col("is_in_play"),
        F.col("promo_code"),
        F.col("settlement_status").isNotNull().alias("is_settled"),
        (F.col("settlement_status") == F.lit("WON")).alias("is_won"),
        (F.col("settlement_status") == F.lit("CASHED_OUT")).alias("is_cashed_out"),
    )

    target = cfg.table("gold", "fact_bet_settlement")
    write_table(fact, target, cfg)
    optimize(spark, target, cfg, zorder_by=["placed_date", "customer_dk"])
    return fact.count()


# ------------------------------------------------------------- aggregates


def build_agg_daily_sport_channel(spark: SparkSession, cfg: Config) -> int:
    """Pre-aggregated daily turnover and margin by sport and channel.

    Built off the *leg* fact using the allocated measures, because sport only
    exists at leg grain. Sum ``stake_allocated`` across every sport and you get
    back exactly the turnover in ``fact_bet_settlement`` - which is the property
    that makes allocation safe, and is asserted in the test suite.

    An aggregate table is a cache, and the rule for caches applies: it must be
    derivable from the fact, and it must be rebuilt whenever the fact is, or the
    two will disagree and the aggregate will win because it is the fast one.
    """
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    events = spark.table(cfg.table("gold", "dim_event")).select(
        "event_sk", "sport_code", "sport_name"
    )
    channels = spark.table(cfg.table("gold", "dim_channel")).select(
        "channel_sk", "channel_code", "channel_group"
    )

    agg = (
        legs.join(F.broadcast(events), on="event_sk", how="left")
        .join(F.broadcast(channels), on="channel_sk", how="left")
        .groupBy(
            "placed_date_key",
            "placed_date",
            F.coalesce("sport_code", F.lit("UNKNOWN")).alias("sport_code"),
            F.coalesce("sport_name", F.lit("Unknown")).alias("sport_name"),
            F.coalesce("channel_code", F.lit("UNKNOWN")).alias("channel_code"),
            F.coalesce("channel_group", F.lit("UNKNOWN")).alias("channel_group"),
        )
        .agg(
            *M.settlement_metrics(M.STAKE_ALLOC, M.PAYOUT_ALLOC),
            M.bet_count(),
            M.leg_count(),
            F.sum(F.col("is_in_play").cast("int")).alias("in_play_leg_count"),
        )
    )
    write_table(agg, cfg.table("gold", "agg_daily_sport_channel"), cfg)
    return agg.count()


def build_agg_daily_customer(spark: SparkSession, cfg: Config) -> int:
    """Daily per-customer activity - the base table for responsible gambling work.

    Keyed on ``customer_dk`` (the durable key) rather than ``customer_sk``, because
    a customer's SCD2 version can change mid-period and this table needs one row
    per customer per day regardless of how many versions of them exist.
    """
    bets = spark.table(cfg.table("gold", "fact_bet_settlement"))
    agg = bets.groupBy("placed_date_key", "placed_date", "customer_dk", "customer_id").agg(
        *M.settlement_metrics(),
        F.count(F.lit(1)).alias("bet_count"),
        F.max("stake_amount").cast("decimal(18,2)").alias("max_single_stake"),
        F.sum(F.col("is_in_play").cast("int")).alias("in_play_bet_count"),
        F.countDistinct("bet_type").alias("distinct_bet_types"),
        F.min("placed_at").alias("first_bet_at"),
        F.max("placed_at").alias("last_bet_at"),
    ).withColumn(
        # Session span in hours: a crude but genuinely used responsible-gambling
        # signal. Long unbroken sessions correlate with loss chasing, and
        # AU operators are required to act on patterns like this.
        "betting_span_hours",
        F.round(
            (F.col("last_bet_at").cast("long") - F.col("first_bet_at").cast("long")) / 3600, 2
        ),
    )
    write_table(agg, cfg.table("gold", "agg_daily_customer_activity"), cfg)
    return agg.count()


def build_all_facts(spark: SparkSession, cfg: Config) -> dict[str, int]:
    """Rebuild facts then aggregates. Order matters here: aggregates read facts."""
    counts = {
        "fact_bet_leg": build_fact_bet_leg(spark, cfg),
        "fact_bet_settlement": build_fact_bet_settlement(spark, cfg),
    }
    counts["agg_daily_sport_channel"] = build_agg_daily_sport_channel(spark, cfg)
    counts["agg_daily_customer_activity"] = build_agg_daily_customer(spark, cfg)
    return counts
