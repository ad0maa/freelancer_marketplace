"""Tests for the dimensional marts.

Most of these assert *properties of the model* rather than specific values, which
is what you want in a warehouse test suite: values change every time the data
changes, but "allocated measures sum back to the total" and "type 2 intervals do
not overlap" must hold forever.
"""

from __future__ import annotations

from pyspark.sql import functions as F

from betting_lakehouse.gold.dimensions import UNKNOWN_KEY


# --------------------------------------------------------------- grain and sums


def test_allocated_turnover_reconciles_to_slip_turnover(spark, lakehouse):
    """The single most important test in the repo.

    ``fact_bet_leg`` is at leg grain, so the bet's stake is divided across its legs.
    If that allocation is wrong - or if somebody sums ``bet_stake_amount`` instead -
    turnover is multiplied by the average number of legs. The number still looks
    plausible, which is exactly why it needs a test rather than an eyeball.
    """
    cfg = lakehouse
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    slips = spark.table(cfg.table("gold", "fact_bet_settlement"))

    allocated = float(legs.agg(F.sum("stake_allocated")).collect()[0][0])
    exact = float(slips.agg(F.sum("stake_amount")).collect()[0][0])
    assert abs(allocated - exact) < 0.01, f"allocated {allocated} != exact {exact}"


def test_summing_the_unallocated_stake_overstates_turnover(spark, lakehouse):
    """Documents the bug the allocation prevents, so the intent cannot be lost."""
    cfg = lakehouse
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    naive = float(legs.agg(F.sum("bet_stake_amount")).collect()[0][0])
    correct = float(legs.agg(F.sum("stake_allocated")).collect()[0][0])
    assert naive > correct * 1.2, "test data has no multis, so it proves nothing"


def test_fact_bet_settlement_is_one_row_per_bet(spark, lakehouse):
    cfg = lakehouse
    slips = spark.table(cfg.table("gold", "fact_bet_settlement"))
    assert slips.count() == slips.select("bet_id").distinct().count()


def test_fact_bet_leg_is_one_row_per_leg(spark, lakehouse):
    cfg = lakehouse
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    assert legs.count() == legs.select("bet_id", "leg_number").distinct().count()


def test_aggregate_agrees_with_the_fact_it_summarises(spark, lakehouse):
    """An aggregate is a cache, and a cache that disagrees with its source is a bug.

    It is also the more dangerous kind of bug, because the aggregate is the fast
    table and therefore the one people actually query.
    """
    cfg = lakehouse
    agg = spark.table(cfg.table("gold", "agg_daily_sport_channel"))
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))

    from_agg = float(agg.agg(F.sum("turnover_amount")).collect()[0][0])
    from_fact = float(legs.agg(F.sum("stake_allocated")).collect()[0][0])
    assert abs(from_agg - from_fact) < 1.0


# ------------------------------------------------------------------ type 2


def test_dim_customer_intervals_do_not_overlap(spark, lakehouse):
    """Overlapping intervals make an as-at join match two versions and double a fact."""
    cfg = lakehouse
    overlaps = spark.sql(f"""
        SELECT count(*) AS n FROM (
          SELECT customer_dk, effective_to,
                 lead(effective_from) OVER (
                     PARTITION BY customer_dk ORDER BY effective_from
                 ) AS next_from
          FROM {cfg.table('gold', 'dim_customer')}
          WHERE customer_sk <> {UNKNOWN_KEY}
        )
        WHERE next_from IS NOT NULL AND next_from <= effective_to
    """).collect()[0]["n"]
    assert overlaps == 0


def test_dim_customer_has_exactly_one_current_row_per_customer(spark, lakehouse):
    cfg = lakehouse
    offenders = spark.sql(f"""
        SELECT count(*) AS n FROM (
          SELECT customer_dk FROM {cfg.table('gold', 'dim_customer')}
          WHERE is_current AND customer_sk <> {UNKNOWN_KEY}
          GROUP BY customer_dk HAVING count(*) > 1
        )
    """).collect()[0]["n"]
    assert offenders == 0


def test_dim_customer_actually_has_history(spark, lakehouse):
    cfg = lakehouse
    versions = (
        spark.table(cfg.table("gold", "dim_customer"))
        .where(F.col("customer_sk") != UNKNOWN_KEY)
        .groupBy("customer_dk")
        .agg(F.count(F.lit(1)).alias("n"))
        .where(F.col("n") > 1)
    )
    assert versions.count() > 0, "no type 2 history was produced"


def test_first_version_is_backdated_so_early_facts_resolve(spark, lakehouse):
    """The vault's load_date is when we learned, not when it became true.

    Without back-dating version 1, every bet placed before the first CRM extract
    would fail its as-at join and land on the unknown member.

    Compared inside Spark rather than on a collected datetime: collecting converts
    through the driver JVM's timezone, which turns 1900-01-01 00:00 Melbourne into
    1899-12-31 14:00 UTC and makes a correct value look wrong.
    """
    cfg = lakehouse
    first_versions = spark.table(cfg.table("gold", "dim_customer")).where(
        (F.col("version_number") == 1) & (F.col("customer_sk") != UNKNOWN_KEY)
    )
    assert first_versions.count() > 0
    assert first_versions.where(F.col("effective_from") > F.lit("1901-01-01")).count() == 0


def test_no_bet_lands_on_the_unknown_customer(spark, lakehouse):
    """Because hub_customer is loaded from bets as well as from the CRM."""
    cfg = lakehouse
    unknown = (
        spark.table(cfg.table("gold", "fact_bet_settlement"))
        .where(F.col("customer_sk") == UNKNOWN_KEY)
        .count()
    )
    assert unknown == 0


# ----------------------------------------------------------- unknown members


def test_every_dimension_has_exactly_one_unknown_member(spark, lakehouse):
    cfg = lakehouse
    for dim, sk in (
        ("dim_customer", "customer_sk"),
        ("dim_event", "event_sk"),
        ("dim_selection", "selection_sk"),
        ("dim_channel", "channel_sk"),
        ("dim_bet_type", "bet_type_sk"),
        ("dim_date", "date_key"),
    ):
        count = spark.table(cfg.table("gold", dim)).where(F.col(sk) == UNKNOWN_KEY).count()
        assert count == 1, f"{dim} has {count} unknown members, expected exactly 1"


def test_unresolvable_legs_are_kept_not_dropped(spark, lakehouse):
    """A missing *dimension* must not cost you a fact row.

    The generator plants selection ids that appear on bet legs but exist in no
    pricing feed. Those legs survive into the fact on the unknown member - dropping
    them would make the warehouse quietly disagree with the ledger.
    """
    cfg = lakehouse
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    unknown_legs = legs.where(F.col("selection_sk") == UNKNOWN_KEY).count()
    assert unknown_legs > 0, "test data no longer contains orphan selections"


def test_legs_of_quarantined_bets_do_not_reach_the_fact(spark, lakehouse):
    """A missing *bet*, on the other hand, does cost you the leg - deliberately.

    This is the one place the fact build uses an inner join. The distinction:

    * unknown dimension  -> keep the fact, point at -1. The bet was real and its
      stake is real turnover; we just cannot describe one of its attributes.
    * rejected bet       -> drop its legs. A bet quarantined at silver for a zero
      stake is not a bet, so its legs are not turnover, and inventing rows for them
      would put money in the fact that the betting engine never took.

    ``link_bet_selection`` still holds those legs, because the vault records what
    the source said regardless of whether the mart wants it - which is exactly the
    division of responsibility the vault is for.
    """
    cfg = lakehouse
    legs = spark.table(cfg.table("gold", "fact_bet_leg"))
    link = spark.table(cfg.table("vault", "link_bet_selection")).select("hk_bet", "leg_number")
    accepted_bets = spark.table(cfg.table("vault", "sat_bet_details")).select("hk_bet").distinct()

    expected = link.join(accepted_bets, on="hk_bet", how="inner").count()
    assert legs.count() == expected

    # The dropped legs are exactly the legs of bets that silver quarantined.
    dropped = link.count() - legs.count()
    quarantined_bets = (
        spark.table(cfg.table("dq", "quarantine"))
        .where(F.col("table_name") == cfg.table("silver", "bets"))
        .count()
    )
    assert dropped > 0 and quarantined_bets > 0
    assert dropped <= quarantined_bets * 4, "more legs dropped than the quarantine explains"


def test_surrogate_keys_are_deterministic(spark, lakehouse):
    """A rebuild must not renumber the warehouse.

    Hash-based surrogate keys mean an extract taken yesterday still joins after
    today's full rebuild. Identity columns would give up that property.
    """
    from betting_lakehouse.gold.dimensions import build_dim_channel

    cfg = lakehouse
    before = {r["channel_code"]: r["channel_sk"]
              for r in spark.table(cfg.table("gold", "dim_channel")).collect()}
    build_dim_channel(spark, cfg)
    after = {r["channel_code"]: r["channel_sk"]
             for r in spark.table(cfg.table("gold", "dim_channel")).collect()}
    assert before == after


# ------------------------------------------------------------------- metrics


def test_hold_pct_is_null_when_there_is_no_turnover(spark):
    """A day with no turnover has an undefined hold, and NULL is the honest answer.

    Returning 0 would drag down every average that includes it.
    """
    from betting_lakehouse.gold import metrics as M

    df = spark.createDataFrame(
        [("2026-06-01", 0.0, 0.0)], "d string, stake_amount double, payout_amount double"
    )
    result = df.groupBy("d").agg(M.hold_pct()).collect()[0]["hold_pct"]
    assert result is None


def test_hold_pct_arithmetic(spark):
    from betting_lakehouse.gold import metrics as M

    # $1,000 staked, $920 paid out -> the book kept $80, an 8% hold.
    df = spark.createDataFrame(
        [("d", 600.0, 500.0), ("d", 400.0, 420.0)],
        "d string, stake_amount double, payout_amount double",
    )
    row = df.groupBy("d").agg(*M.settlement_metrics()).collect()[0]
    assert float(row["turnover_amount"]) == 1000.00
    assert float(row["gross_win_amount"]) == 80.00
    assert float(row["hold_pct"]) == 8.0
