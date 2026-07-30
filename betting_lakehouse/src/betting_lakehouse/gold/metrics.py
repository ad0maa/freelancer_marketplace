"""Wagering metric definitions, in one place.

The reason this file exists is not code reuse. It is that "turnover" has to mean
exactly one thing across every dashboard, every notebook and every ad-hoc query,
and the only way to guarantee that is for there to be one definition and for
everything to import it.

The failure mode when you skip this is specific and expensive: trading calculates
hold using cashed-out bets, finance excludes them, both numbers get presented in
the same meeting, and the next two days are spent reconciling instead of deciding.

Glossary (the vocabulary a wagering analyst uses)
-------------------------------------------------
**Turnover** (or *handle*) - total amount staked. The volume metric. Note it
counts money wagered, not money lost: a customer who bets $10 twenty times has
$200 of turnover.

**Payout** - total returned to customers on settled bets, including returned
stakes on voids and cash-outs.

**Gross win** - turnover minus payouts. Positive means the book won. This is
revenue *before* bonuses and promotional costs.

**GGR** (gross gaming revenue) - gross win net of promotional/bonus cost. What a
regulator and the ASX care about, and the number taxes are levied on.

**Hold %** (or *win %*, or *actual margin*) - gross win / turnover. The real,
realised margin. Typically single digits, and volatile until you have a lot of
settled bets.

**Overround** / **theoretical margin** - the margin priced into a market:
sum(1/odds) across all selections, minus 1. If a head-to-head is priced at 1.90 /
1.95 then the implied probabilities sum to about 1.039, so the theoretical margin
is ~3.9%. Hold should tend towards this over time; a persistent gap means the
prices, the customer mix, or the settlement data need attention.

**Multi** - one bet on several outcomes where all must win; odds multiply.
**SGM** (same game multi) - a multi whose legs are all in one event.
**In-play / live** - a bet placed after the event has started.
"""

from __future__ import annotations

from pyspark.sql import Column
from pyspark.sql import functions as F

# Column names used consistently across the gold layer.
STAKE = "stake_amount"
PAYOUT = "payout_amount"
STAKE_ALLOC = "stake_allocated"
PAYOUT_ALLOC = "payout_allocated"


def turnover(stake_col: str = STAKE) -> Column:
    """Total amount staked."""
    return F.sum(stake_col).cast("decimal(18,2)").alias("turnover_amount")


def total_payout(payout_col: str = PAYOUT) -> Column:
    """Total returned to customers on settled bets."""
    return F.coalesce(F.sum(payout_col), F.lit(0)).cast("decimal(18,2)").alias("payout_amount")


def gross_win(stake_col: str = STAKE, payout_col: str = PAYOUT) -> Column:
    """Turnover minus payouts: what the book actually kept."""
    return (
        (F.sum(stake_col) - F.coalesce(F.sum(payout_col), F.lit(0)))
        .cast("decimal(18,2)")
        .alias("gross_win_amount")
    )


def hold_pct(stake_col: str = STAKE, payout_col: str = PAYOUT) -> Column:
    """Realised margin as a percentage of turnover.

    Guarded against divide-by-zero with a NULLIF rather than a CASE: a day with no
    turnover has an undefined hold, and NULL is the honest answer. Returning 0
    would drag every average that includes it towards zero.
    """
    numerator = F.sum(stake_col) - F.coalesce(F.sum(payout_col), F.lit(0))
    return (
        (F.lit(100) * numerator / F.nullif(F.sum(stake_col), F.lit(0)))
        .cast("decimal(9,4)")
        .alias("hold_pct")
    )


def theoretical_margin_pct(implied_prob_col: str = "implied_probability") -> Column:
    """Margin priced into the market, from the sum of implied probabilities."""
    return (
        (F.lit(100) * (F.sum(implied_prob_col) - F.lit(1)) / F.nullif(F.sum(implied_prob_col), F.lit(0)))
        .cast("decimal(9,4)")
        .alias("theoretical_margin_pct")
    )


def bet_count(bet_col: str = "bet_id") -> Column:
    """Distinct bet slips. Distinct, because the leg-grain fact repeats bet_id."""
    return F.countDistinct(bet_col).alias("bet_count")


def leg_count() -> Column:
    return F.count(F.lit(1)).alias("leg_count")


def active_customers(customer_col: str = "customer_id") -> Column:
    """Customers who placed at least one bet.

    ``countDistinct`` is an exact but shuffle-heavy aggregate. At billions of rows
    the usual swap is ``approx_count_distinct`` (HyperLogLog, ~1% error, far
    cheaper); do not make that swap on anything a regulator reads.
    """
    return F.countDistinct(customer_col).alias("active_customers")


def avg_stake(stake_col: str = STAKE) -> Column:
    return F.avg(stake_col).cast("decimal(18,2)").alias("avg_stake_amount")


def settlement_metrics(stake_col: str = STAKE, payout_col: str = PAYOUT) -> list[Column]:
    """The standard metric set for any grouping of settled bets."""
    return [
        turnover(stake_col),
        total_payout(payout_col),
        gross_win(stake_col, payout_col),
        hold_pct(stake_col, payout_col),
        avg_stake(stake_col),
    ]
