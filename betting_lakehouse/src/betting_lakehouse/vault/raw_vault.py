"""Load the raw vault from silver.

"Raw" vault means: model the source data as it was given to us, with no business
rules applied. If the CRM says a customer is in VIC, the satellite says VIC. Any
derivation, reclassification or calculation belongs downstream - in a business
vault or, as here, in the gold marts.

That restraint is the discipline that makes a vault worth having. The raw vault is
supposed to be a lossless, auditable record of what each source told us and when.
The moment you apply a business rule in here, you can no longer rebuild history
when the rule changes - and business rules always change.

Grain of this vault
-------------------
::

    hub_customer   <-- link_bet_customer -->  hub_bet  --> sat_bet_details
         |                                       |         sat_bet_settlement
    sat_customer_details                         |
                                        link_bet_selection (+ leg_number)
                                                 |
                                            hub_selection --> sat_selection_price
                                                 |
                                        link_selection_market
                                                 |
                                            hub_market --> sat_market_details
                                                 |
                                          link_market_event
                                                 |
                                            hub_event --> sat_event_details
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from ..bronze import BATCH_ID
from ..config import Config
from .dv_helpers import load_hub, load_link, load_satellite


def batch_load_date(cfg: Config, batch: int) -> datetime:
    """The load timestamp stamped on every row this batch inserts.

    In production this is simply the moment the job ran. It is pinned to a
    derived business timestamp here so that the demo is reproducible: the SCD2
    effective dates in ``dim_customer`` come out identical on every run, which is
    what lets the tests assert on them.

    One load_date for the whole batch, not ``current_timestamp()`` per row: every
    row inserted by one run must share a load_date, or the "latest version per
    key" window becomes ambiguous.
    """
    day = cfg.end_date - timedelta(days=1) if batch == 1 else cfg.end_date
    return datetime.combine(day, time(2, 0, 0))


def _silver(spark: SparkSession, cfg: Config, name: str, batch: int) -> DataFrame:
    return spark.table(cfg.table("silver", name)).where(F.col(BATCH_ID) == str(batch))


def load_hubs(spark: SparkSession, cfg: Config, batch: int) -> dict[str, int]:
    """Load every hub for one batch. Returns rows inserted per hub.

    A hub is loaded from *every* source that carries its business key, not just
    the source that "owns" the entity. Two consequences worth understanding:

    * hub_customer picks up customer_ids seen on bets even if the CRM extract has
      not mentioned them yet, so a bet is never orphaned by extract timing.
    * hub_selection picks up selection_ids seen on bet legs even when pricing
      never sent that selection. Those keys then exist in the hub with no
      satellite row - which is exactly the truth: we know the key exists, we know
      nothing about it. The mart resolves them to an unknown member.
    """
    ld = batch_load_date(cfg, batch)
    customers = _silver(spark, cfg, "customers", batch)
    bets = _silver(spark, cfg, "bets", batch)
    legs = _silver(spark, cfg, "bet_legs", batch)
    events = _silver(spark, cfg, "events", batch)
    markets = _silver(spark, cfg, "markets", batch)
    selections = _silver(spark, cfg, "selections", batch)
    inserted: dict[str, int] = {}

    inserted["hub_customer"] = load_hub(
        spark, cfg, "hub_customer", customers, ["customer_id"], "hk_customer",
        F.col("_record_source"), ld,
    )
    inserted["hub_customer"] += load_hub(
        spark, cfg, "hub_customer", bets.select("customer_id", "_record_source"),
        ["customer_id"], "hk_customer", F.col("_record_source"), ld,
    )
    inserted["hub_bet"] = load_hub(
        spark, cfg, "hub_bet", bets, ["bet_id"], "hk_bet", F.col("_record_source"), ld,
    )
    inserted["hub_event"] = load_hub(
        spark, cfg, "hub_event", events, ["event_id"], "hk_event", F.col("_record_source"), ld,
    )
    inserted["hub_market"] = load_hub(
        spark, cfg, "hub_market", markets, ["market_id"], "hk_market",
        F.col("_record_source"), ld,
    )
    inserted["hub_selection"] = load_hub(
        spark, cfg, "hub_selection", selections, ["selection_id"], "hk_selection",
        F.col("_record_source"), ld,
    )
    inserted["hub_selection"] += load_hub(
        spark, cfg, "hub_selection", legs.select("selection_id", "_record_source"),
        ["selection_id"], "hk_selection", F.col("_record_source"), ld,
    )
    return inserted


def load_links(spark: SparkSession, cfg: Config, batch: int) -> dict[str, int]:
    """Load every link for one batch. Returns rows inserted per link."""
    ld = batch_load_date(cfg, batch)
    bets = _silver(spark, cfg, "bets", batch)
    legs = _silver(spark, cfg, "bet_legs", batch)
    markets = _silver(spark, cfg, "markets", batch)
    selections = _silver(spark, cfg, "selections", batch)
    inserted: dict[str, int] = {}

    inserted["link_bet_customer"] = load_link(
        spark, cfg, "link_bet_customer", bets, "hk_bet_customer",
        {"hk_bet": ["bet_id"], "hk_customer": ["customer_id"]},
        F.col("_record_source"), ld,
    )
    # leg_number is a dependent child key: without it, a two-leg bet that happens
    # to include the same selection twice would collapse to one link row.
    inserted["link_bet_selection"] = load_link(
        spark, cfg, "link_bet_selection", legs, "hk_bet_selection",
        {"hk_bet": ["bet_id"], "hk_selection": ["selection_id"]},
        F.col("_record_source"), ld, dependent_child_keys=["leg_number"],
    )
    inserted["link_selection_market"] = load_link(
        spark, cfg, "link_selection_market", selections, "hk_selection_market",
        {"hk_selection": ["selection_id"], "hk_market": ["market_id"]},
        F.col("_record_source"), ld,
    )
    inserted["link_market_event"] = load_link(
        spark, cfg, "link_market_event", markets, "hk_market_event",
        {"hk_market": ["market_id"], "hk_event": ["event_id"]},
        F.col("_record_source"), ld,
    )
    return inserted


def load_satellites(spark: SparkSession, cfg: Config, batch: int) -> dict[str, int]:
    """Load every satellite for one batch. Returns rows inserted per satellite.

    Satellites are split by source system and by rate of change, never by
    convenience. ``sat_bet_details`` and ``sat_bet_settlement`` both hang off
    hub_bet but are separate tables because they come from different extracts and
    change at completely different times - a bet's details never change after
    placement, while its settlement can be re-graded by a trader hours later.
    Putting them in one satellite would rewrite the whole payload, including the
    stake and the odds, every time a bet settled.
    """
    ld = batch_load_date(cfg, batch)
    customers = _silver(spark, cfg, "customers", batch)
    bets = _silver(spark, cfg, "bets", batch)
    legs = _silver(spark, cfg, "bet_legs", batch)
    settlements = _silver(spark, cfg, "settlements", batch)
    events = _silver(spark, cfg, "events", batch)
    markets = _silver(spark, cfg, "markets", batch)
    selections = _silver(spark, cfg, "selections", batch)
    inserted: dict[str, int] = {}

    inserted["sat_customer_details"] = load_satellite(
        spark, cfg, "sat_customer_details", customers, "hk_customer", ["customer_id"],
        payload=[
            "first_name", "last_name", "email", "birth_date", "signup_date",
            "state_code", "suburb", "postcode", "account_status", "verification_status",
            "is_self_excluded", "deposit_limit_weekly", "is_marketing_opt_in", "vip_tier",
        ],
        record_source=F.col("_record_source"), load_date=ld, version_col="source_updated_at",
    )
    inserted["sat_bet_details"] = load_satellite(
        spark, cfg, "sat_bet_details", bets, "hk_bet", ["bet_id"],
        payload=[
            "bet_type", "channel_code", "stake_amount", "combined_odds",
            "potential_payout", "currency_code", "placed_at", "placed_date",
            "is_in_play", "promo_code", "bet_status",
        ],
        record_source=F.col("_record_source"), load_date=ld,
    )
    inserted["sat_bet_settlement"] = load_satellite(
        spark, cfg, "sat_bet_settlement", settlements, "hk_bet", ["bet_id"],
        payload=["settlement_status", "payout_amount", "settled_at", "settled_date", "settled_by"],
        record_source=F.col("_record_source"), load_date=ld, version_col="settled_at",
    )
    # A satellite on a *link*, not on a hub. The odds a customer actually took is
    # an attribute of the relationship between the bet and the selection, not of
    # either one alone: the same selection is taken at different prices by
    # different bets all day long. Hanging it off hub_selection would be wrong,
    # and hanging it off hub_bet would break as soon as a multi's legs had
    # different prices - which they always do.
    inserted["sat_bet_leg_odds"] = load_satellite(
        spark, cfg, "sat_bet_leg_odds", legs, "hk_bet_selection",
        ["bet_id", "selection_id", "leg_number"],
        payload=["odds_taken"],
        record_source=F.col("_record_source"), load_date=ld,
    )
    inserted["sat_selection_price"] = load_satellite(
        spark, cfg, "sat_selection_price", selections, "hk_selection", ["selection_id"],
        payload=[
            "selection_name", "runner_number", "decimal_odds", "implied_probability",
            "selection_status",
        ],
        record_source=F.col("_record_source"), load_date=ld, version_col="price_updated_at",
    )
    inserted["sat_event_details"] = load_satellite(
        spark, cfg, "sat_event_details", events, "hk_event", ["event_id"],
        payload=[
            "event_name", "home_team", "away_team", "venue", "scheduled_start",
            "event_date", "event_status", "is_live_betting_enabled", "competition_id",
            "competition_name", "sport_id", "sport_code", "sport_name",
        ],
        record_source=F.col("_record_source"), load_date=ld,
    )
    inserted["sat_market_details"] = load_satellite(
        spark, cfg, "sat_market_details", markets, "hk_market", ["market_id"],
        payload=["market_type", "market_name", "market_status"],
        record_source=F.col("_record_source"), load_date=ld,
    )
    return inserted


def build_raw_vault(spark: SparkSession, cfg: Config, batch: int) -> dict[str, int]:
    """Load the whole raw vault for one batch.

    Returns rows *inserted* per table - which on a second run of the same batch is
    zero for everything, and that is the headline property of the whole design.

    Called sequentially here because a local demo has one JVM. Nothing forces that
    order: no hub reads a link, no link reads a satellite, no satellite reads a
    hub. On a cluster these are three concurrent tasks, which is how the Workflow
    in databricks.yml is laid out.
    """
    inserted = load_hubs(spark, cfg, batch)
    inserted.update(load_links(spark, cfg, batch))
    inserted.update(load_satellites(spark, cfg, batch))
    return inserted
