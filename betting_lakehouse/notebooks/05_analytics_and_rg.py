# Databricks notebook source
# MAGIC %md
# MAGIC # 05 · What the business actually asks — and the DQ gate
# MAGIC
# MAGIC The star schema exists so that these questions are one join. This notebook is
# MAGIC also the final task in the Workflow (`dq_gate` in `databricks.yml`): it asserts
# MAGIC the marts reconcile, so if they do not, the job goes **red** and the dashboards
# MAGIC reading it are known-bad rather than quietly wrong.
# MAGIC
# MAGIC A pipeline that goes green while producing wrong numbers is worse than one that
# MAGIC fails, because somebody will make a decision on the wrong numbers.

# COMMAND ----------

import sys

REPO_ROOT = "/Workspace/Repos/you@example.com/betting_lakehouse"
if REPO_ROOT + "/src" not in sys.path:
    sys.path.insert(0, REPO_ROOT + "/src")

dbutils.widgets.text("catalog", "", "Catalog override (blank = use pipeline.yml)")

from pyspark.sql import functions as F

from betting_lakehouse.config import Config

cfg = Config.load(REPO_ROOT + "/conf/pipeline.yml")
GOLD = cfg.schema("gold")
spark.sql(f"USE {GOLD}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Turnover, gross win and hold % by sport
# MAGIC
# MAGIC **Turnover** is money staked. **Gross win** is turnover minus payouts — what the
# MAGIC book kept. **Hold %** is gross win over turnover, the realised margin. Typically
# MAGIC single digits, and volatile until a lot of bets have settled.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT sport_name,
# MAGIC        round(sum(turnover_amount), 2)                                  AS turnover,
# MAGIC        round(sum(gross_win_amount), 2)                                 AS gross_win,
# MAGIC        round(100 * sum(gross_win_amount) / sum(turnover_amount), 2)    AS hold_pct,
# MAGIC        sum(bet_count)                                                  AS bets
# MAGIC FROM agg_daily_sport_channel
# MAGIC GROUP BY sport_name
# MAGIC ORDER BY turnover DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## Realised hold vs the margin that was priced in
# MAGIC
# MAGIC Every market is priced with an **overround**: the implied probabilities
# MAGIC (`1 / decimal_odds`) sum to more than 1, and the excess is the theoretical margin.
# MAGIC Hold should drift towards it as volume grows.
# MAGIC
# MAGIC A persistent gap is a real signal, and which direction it goes tells you where to
# MAGIC look: hold far *below* theoretical usually means sharp customers or stale prices;
# MAGIC far *above* usually means a settlement or void-handling bug.

# COMMAND ----------

# MAGIC %sql
# MAGIC WITH priced AS (
# MAGIC   SELECT market_type,
# MAGIC          round(100 * (sum(current_implied_probability) - count(DISTINCT market_id))
# MAGIC                / sum(current_implied_probability), 2) AS theoretical_margin_pct
# MAGIC   FROM dim_selection
# MAGIC   WHERE selection_sk <> -1
# MAGIC   GROUP BY market_type
# MAGIC ),
# MAGIC realised AS (
# MAGIC   SELECT market_type,
# MAGIC          round(sum(stake_allocated), 2)                                        AS turnover,
# MAGIC          round(100 * sum(gross_win_allocated) / sum(stake_allocated), 2)       AS hold_pct
# MAGIC   FROM fact_bet_leg
# MAGIC   WHERE is_settled
# MAGIC   GROUP BY market_type
# MAGIC )
# MAGIC SELECT r.market_type, r.turnover, r.hold_pct, p.theoretical_margin_pct,
# MAGIC        round(r.hold_pct - p.theoretical_margin_pct, 2) AS gap_pct
# MAGIC FROM realised r LEFT JOIN priced p USING (market_type)
# MAGIC ORDER BY r.turnover DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## Channel mix
# MAGIC
# MAGIC Mobile app share is the number every wagering exec watches, because acquisition
# MAGIC cost and retention differ sharply by channel.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT channel_group,
# MAGIC        round(sum(turnover_amount), 2)                                       AS turnover,
# MAGIC        round(100 * sum(turnover_amount) / sum(sum(turnover_amount)) OVER (), 1) AS pct_of_turnover,
# MAGIC        round(100 * sum(gross_win_amount) / sum(turnover_amount), 2)         AS hold_pct,
# MAGIC        round(sum(in_play_leg_count) * 100.0 / sum(leg_count), 1)            AS in_play_pct
# MAGIC FROM agg_daily_sport_channel
# MAGIC GROUP BY channel_group
# MAGIC ORDER BY turnover DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## Multi vs single
# MAGIC
# MAGIC Multis carry a much higher margin, because the overround compounds across legs.
# MAGIC That is exactly why they are marketed hard — and why the leg/slip grain split
# MAGIC matters so much: get the allocation wrong and multis dominate turnover.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT bet_type,
# MAGIC        count(*)                                                      AS bets,
# MAGIC        round(avg(legs_in_bet), 2)                                    AS avg_legs,
# MAGIC        round(sum(stake_amount), 2)                                   AS turnover,
# MAGIC        round(avg(stake_amount), 2)                                   AS avg_stake,
# MAGIC        round(100 * sum(gross_win_amount) / sum(stake_amount), 2)     AS hold_pct,
# MAGIC        round(100 * avg(CASE WHEN is_won THEN 1 ELSE 0 END), 1)       AS win_rate_pct
# MAGIC FROM fact_bet_settlement
# MAGIC WHERE is_settled
# MAGIC GROUP BY bet_type
# MAGIC ORDER BY turnover DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## Jurisdictional turnover — and why type 2 matters here
# MAGIC
# MAGIC Australian wagering is regulated per state, so turnover has to be attributed to
# MAGIC where the customer was **when the bet was placed**. The fact already carries the
# MAGIC as-at `customer_sk`, so this is a plain join — no date logic in the query, which
# MAGIC is the whole point of resolving it once at load time.

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT c.state_code,
# MAGIC        count(DISTINCT f.customer_dk)                            AS customers,
# MAGIC        round(sum(f.stake_amount), 2)                            AS turnover,
# MAGIC        round(100 * sum(f.gross_win_amount) / sum(f.stake_amount), 2) AS hold_pct
# MAGIC FROM fact_bet_settlement f
# MAGIC JOIN dim_customer c ON f.customer_sk = c.customer_sk
# MAGIC GROUP BY c.state_code
# MAGIC ORDER BY turnover DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ## Responsible gambling
# MAGIC
# MAGIC Not an afterthought in this industry — it is a licence condition, and the data
# MAGIC platform is what makes it enforceable. Three checks that a real operator runs:
# MAGIC
# MAGIC 1. **Any activity on a self-excluded account.** Must be zero. If it is not, that
# MAGIC    is an incident, not a metric.
# MAGIC 2. **Loss chasing** — stakes escalating within a long unbroken session.
# MAGIC 3. **Sustained heavy losses** relative to the customer's own baseline.

# COMMAND ----------

excluded_activity = spark.sql(f"""
    SELECT f.bet_id, f.customer_id, f.placed_at, f.stake_amount
    FROM {GOLD}.fact_bet_settlement f
    JOIN {GOLD}.dim_customer c ON f.customer_sk = c.customer_sk
    WHERE c.is_self_excluded
""")
print(f"bets placed while flagged self-excluded: {excluded_activity.count()}")
display(excluded_activity.limit(10))

# COMMAND ----------

# MAGIC %md
# MAGIC Any rows above are bets placed *before* the exclusion took effect but attributed
# MAGIC to the version of the customer that carries the flag — which is exactly the kind
# MAGIC of thing the as-at join is designed to make visible and explainable, rather than
# MAGIC something you have to reconstruct by hand later.

# COMMAND ----------

# MAGIC %sql
# MAGIC -- Escalating stakes in a long session: a standard loss-chasing signal.
# MAGIC SELECT customer_id,
# MAGIC        placed_date,
# MAGIC        bet_count,
# MAGIC        round(betting_span_hours, 1)      AS session_hours,
# MAGIC        round(turnover_amount, 2)         AS turnover,
# MAGIC        round(max_single_stake, 2)        AS biggest_stake,
# MAGIC        round(-1 * gross_win_amount, 2)   AS customer_net_position
# MAGIC FROM agg_daily_customer_activity
# MAGIC WHERE bet_count >= 8
# MAGIC   AND betting_span_hours >= 6
# MAGIC   AND gross_win_amount > 0          -- the book won, i.e. the customer lost
# MAGIC ORDER BY gross_win_amount DESC
# MAGIC LIMIT 15

# COMMAND ----------

# MAGIC %md
# MAGIC ## The gate
# MAGIC
# MAGIC Assertions, not charts. Each one is a property that must hold for the marts to be
# MAGIC usable at all, and each maps to a specific way the pipeline could have broken.

# COMMAND ----------

failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(f"{name} {detail}".strip())


legs = spark.table(f"{GOLD}.fact_bet_leg")
slips = spark.table(f"{GOLD}.fact_bet_settlement")
dim_customer = spark.table(f"{GOLD}.dim_customer")

leg_turnover = float(legs.agg(F.sum("stake_allocated")).collect()[0][0] or 0)
slip_turnover = float(slips.agg(F.sum("stake_amount")).collect()[0][0] or 0)

# Allocation must be lossless, or "turnover by sport" disagrees with the total.
check("allocated turnover reconciles to slip turnover",
      abs(leg_turnover - slip_turnover) < 1,
      f"legs={leg_turnover:,.2f} slips={slip_turnover:,.2f}")

# One row per bet slip. A duplicate here doubles that bet in every financial report.
check("fact_bet_settlement is one row per bet",
      slips.count() == slips.select("bet_id").distinct().count())

# Type 2 intervals must not overlap, or an as-at join matches two versions and
# silently doubles the fact row.
overlaps = spark.sql(f"""
    SELECT count(*) AS n FROM (
      SELECT customer_dk, effective_to,
             lead(effective_from) OVER (PARTITION BY customer_dk ORDER BY effective_from) AS next_from
      FROM {GOLD}.dim_customer WHERE customer_sk <> -1
    ) WHERE next_from IS NOT NULL AND next_from <= effective_to
""").collect()[0]["n"]
check("dim_customer type 2 intervals do not overlap", overlaps == 0, f"{overlaps} overlaps")

# Exactly one current row per customer.
multi_current = spark.sql(f"""
    SELECT count(*) AS n FROM (
      SELECT customer_dk FROM {GOLD}.dim_customer
      WHERE is_current AND customer_sk <> -1
      GROUP BY customer_dk HAVING count(*) > 1
    )
""").collect()[0]["n"]
check("exactly one current row per customer", multi_current == 0)

# Every dimension has its unknown member, or unresolvable facts get dropped.
for dim, sk in (("dim_customer", "customer_sk"), ("dim_event", "event_sk"),
                ("dim_selection", "selection_sk"), ("dim_channel", "channel_sk")):
    check(f"{dim} has an unknown member",
          spark.table(f"{GOLD}.{dim}").where(F.col(sk) == -1).count() == 1)

# No negative money anywhere in the marts.
check("no negative stakes or payouts",
      slips.where((F.col("stake_amount") < 0) | (F.col("payout_amount") < 0)).count() == 0)

if failures:
    raise AssertionError("Gold layer failed its checks:\n  - " + "\n  - ".join(failures))
print("\nAll gold-layer checks passed.")
