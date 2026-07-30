# Databricks notebook source
# MAGIC %md
# MAGIC # 04 · Gold — the Kimball star schema
# MAGIC
# MAGIC Why build a star schema when the vault already holds everything? Because the
# MAGIC vault is optimised for *loading and auditing*, and a star is optimised for
# MAGIC *being queried by people*. "Turnover by sport by channel last Saturday" is one
# MAGIC join in a star and six joins plus two windows in a vault. Analysts and BI tools
# MAGIC will not get the six joins right every time, so you do it once, here.
# MAGIC
# MAGIC | | raw vault | gold marts |
# MAGIC |---|---|---|
# MAGIC | purpose | system of record | presentation |
# MAGIC | business rules | none | all of them |
# MAGIC | load pattern | insert-only | full rebuild |
# MAGIC | rebuildable | never | every run |
# MAGIC
# MAGIC The marts being disposable is the point: when a definition changes, you change
# MAGIC the rule and rebuild, and history is still intact in the vault.

# COMMAND ----------

import sys

REPO_ROOT = "/Workspace/Repos/you@example.com/betting_lakehouse"
if REPO_ROOT + "/src" not in sys.path:
    sys.path.insert(0, REPO_ROOT + "/src")

from pyspark.sql import functions as F

from betting_lakehouse.config import Config
from betting_lakehouse.gold import dimensions, facts

cfg = Config.load(REPO_ROOT + "/conf/pipeline.yml")

# COMMAND ----------

display(spark.createDataFrame(list(dimensions.build_all_dimensions(spark, cfg).items()),
                              "table string, rows long"))

# COMMAND ----------

display(spark.createDataFrame(list(facts.build_all_facts(spark, cfg).items()),
                              "table string, rows long"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## The shape of it
# MAGIC
# MAGIC ```
# MAGIC                    dim_date ─┐
# MAGIC                dim_customer ─┤
# MAGIC                   dim_event ─┼── fact_bet_leg          (one row per leg)
# MAGIC               dim_selection ─┤
# MAGIC                 dim_channel ─┤
# MAGIC                dim_bet_type ─┘
# MAGIC
# MAGIC                    dim_date ─┐
# MAGIC                dim_customer ─┼── fact_bet_settlement   (one row per bet slip)
# MAGIC                 dim_channel ─┤
# MAGIC                dim_bet_type ─┘
# MAGIC ```
# MAGIC
# MAGIC ## Two facts, two grains — and never mix them
# MAGIC
# MAGIC `fact_bet_leg` carries sport, event and selection, because those only make sense
# MAGIC per leg: a four-leg multi across AFL, NRL and two races has no single sport.
# MAGIC
# MAGIC `fact_bet_settlement` carries the money, because stake and payout belong to the
# MAGIC slip, not the leg.
# MAGIC
# MAGIC Putting stake on the leg fact and summing it **multiplies turnover by the number
# MAGIC of legs** — a 4-leg multi with a $10 stake reports $40. This is the single most
# MAGIC common way a betting warehouse produces numbers finance refuses to sign off, and
# MAGIC it always looks plausible until someone checks the total.

# COMMAND ----------

# MAGIC %md
# MAGIC ### Allocated measures
# MAGIC
# MAGIC The leg fact still needs a stake-shaped measure for "turnover by sport", so it
# MAGIC carries `stake_allocated = stake / legs_in_bet`. Allocated measures are additive
# MAGIC by construction — they sum back to the true total across any grouping — and the
# MAGIC name makes clear they are not the real stake.
# MAGIC
# MAGIC The check below is the one that catches a fan-out bug, and it belongs in your
# MAGIC test suite, not just in a notebook.

# COMMAND ----------

leg_total = spark.table(cfg.table("gold", "fact_bet_leg")).agg(F.sum("stake_allocated")).collect()[0][0]
slip_total = spark.table(cfg.table("gold", "fact_bet_settlement")).agg(F.sum("stake_amount")).collect()[0][0]
print(f"allocated turnover from legs : ${float(leg_total):>14,.2f}")
print(f"exact turnover from slips    : ${float(slip_total):>14,.2f}")
assert abs(float(leg_total) - float(slip_total)) < 1, "allocation does not reconcile"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Type 2: joining a fact to the customer *as they were*
# MAGIC
# MAGIC A customer who lived in NSW in June and moved to VIC in July has two rows in
# MAGIC `dim_customer`. Joining a June bet to the *current* row reports that bet as VIC
# MAGIC turnover, and the NSW jurisdictional return is wrong. Joining on the effective
# MAGIC date range attributes it to NSW, where it happened:
# MAGIC
# MAGIC ```sql
# MAGIC FROM fact f
# MAGIC JOIN dim_customer d
# MAGIC   ON  f.customer_dk = d.customer_dk
# MAGIC   AND f.placed_at BETWEEN d.effective_from AND d.effective_to
# MAGIC ```
# MAGIC
# MAGIC Keep the durable key in the condition. Spark hashes on that and applies the range
# MAGIC as a filter; join on the range alone and you get a nested loop over the whole
# MAGIC dimension.

# COMMAND ----------

display(
    spark.sql(f"""
        SELECT customer_id, version_number, state_code, account_status, vip_tier,
               is_self_excluded, effective_from, effective_to, is_current
        FROM {cfg.table('gold', 'dim_customer')}
        WHERE customer_dk IN (
            SELECT customer_dk FROM {cfg.table('gold', 'dim_customer')}
            WHERE customer_sk <> -1
            GROUP BY customer_dk HAVING count(*) > 1
        )
        ORDER BY customer_id, version_number
        LIMIT 8
    """)
)

# COMMAND ----------

# MAGIC %md
# MAGIC Version 1 starts at `1900-01-01`, not at the vault's `load_date`. The vault
# MAGIC records when *we learned* something, not when it became true, so without
# MAGIC back-dating, a bet placed before our first CRM extract would fail its as-at join
# MAGIC and land on the unknown member. Back-dating version 1 says "as far as this mart
# MAGIC is concerned, this is what they always were".
# MAGIC
# MAGIC That is a business rule, and it lives in the mart — not in the vault, which must
# MAGIC stay a literal record of what each source said.

# COMMAND ----------

# MAGIC %md
# MAGIC ## The unknown member (`-1`)
# MAGIC
# MAGIC Every dimension has a `-1` row. When a fact's dimension key cannot be resolved —
# MAGIC a fixture that has not arrived, a selection that was never priced — the fact
# MAGIC still loads, at the right grain, with its measures intact, pointing at `-1`.
# MAGIC
# MAGIC The alternative is dropping the fact row, which makes the warehouse quietly
# MAGIC disagree with the betting engine. A row labelled "Unknown" that somebody can go
# MAGIC and investigate is far better than a number that is silently short.

# COMMAND ----------

display(
    spark.sql(f"""
        SELECT
          count(*)                                              AS legs,
          sum(CASE WHEN event_sk     = -1 THEN 1 ELSE 0 END)    AS unknown_event,
          sum(CASE WHEN selection_sk = -1 THEN 1 ELSE 0 END)    AS unknown_selection,
          sum(CASE WHEN customer_sk  = -1 THEN 1 ELSE 0 END)    AS unknown_customer,
          round(sum(CASE WHEN event_sk = -1 THEN stake_allocated ELSE 0 END), 2) AS turnover_on_unknown
        FROM {cfg.table('gold', 'fact_bet_leg')}
    """)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Physical layout: do not partition small facts
# MAGIC
# MAGIC It is tempting to `partitionBy("placed_date")`. With 9,000 rows over 28 days that
# MAGIC produces tiny partitions and thousands of small files, and every query pays to
# MAGIC list them. Small files are the number one performance problem in a real
# MAGIC lakehouse.
# MAGIC
# MAGIC Partition a fact by date only once each daily partition is comfortably over
# MAGIC ~1 GB. Below that, cluster instead:
# MAGIC
# MAGIC ```sql
# MAGIC OPTIMIZE fact_bet_leg ZORDER BY (placed_date, event_sk);
# MAGIC -- or on Databricks, let it maintain itself:
# MAGIC ALTER TABLE fact_bet_leg CLUSTER BY (placed_date, event_sk);
# MAGIC ```
# MAGIC
# MAGIC Liquid clustering is the modern default on Databricks: same file skipping, no
# MAGIC partition-key decision to get wrong, and it can be changed later without a
# MAGIC rewrite of the whole table.

# COMMAND ----------

display(spark.sql(f"DESCRIBE DETAIL {cfg.table('gold', 'fact_bet_leg')}").select(
    "format", "numFiles", "sizeInBytes"))

# COMMAND ----------

# MAGIC %md
# MAGIC **Next:** `05_analytics_and_rg.py`
