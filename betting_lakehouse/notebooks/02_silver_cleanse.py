# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Silver — make the data trustworthy
# MAGIC
# MAGIC Bronze is faithful but unusable. Silver is where strings become dates and
# MAGIC decimals, duplicates are collapsed, business keys are standardised, and
# MAGIC anything that fails a quality rule is quarantined rather than quietly averaged
# MAGIC into a report.
# MAGIC
# MAGIC Four things happen, in this order, and the order matters: you cannot
# MAGIC deduplicate on a business key until you have standardised it, and you cannot
# MAGIC apply a rule like `stake > 0` until `stake` is a number.

# COMMAND ----------

import sys

REPO_ROOT = "/Workspace/Repos/you@example.com/betting_lakehouse"
if REPO_ROOT + "/src" not in sys.path:
    sys.path.insert(0, REPO_ROOT + "/src")

dbutils.widgets.text("batch", "1", "Landing batch to load")
BATCH = int(dbutils.widgets.get("batch"))

from pyspark.sql import functions as F

from betting_lakehouse import silver
from betting_lakehouse.config import Config

cfg = Config.load(REPO_ROOT + "/conf/pipeline.yml")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1 · Typing: money is `decimal`, never `double`
# MAGIC
# MAGIC `"$1,250.00"` → strip the symbols → cast to `decimal(18,2)`.
# MAGIC
# MAGIC Use `decimal`, not `double`. Floating point cannot represent `0.10` exactly, so
# MAGIC summing a few million stakes as doubles drifts by cents — and a finance team
# MAGIC reconciling turnover to the cent will find it. Spark's decimal arithmetic is
# MAGIC exact and costs nothing at this scale.

# COMMAND ----------

raw = spark.table(cfg.table("bronze", "bets")).where(F.col("_batch_id") == str(BATCH))

display(
    raw.select(
        "stake_amount",
        silver.money("stake_amount").alias("as_decimal"),
        F.col("stake_amount").cast("double").alias("naive_double_cast"),
    ).limit(5)
)

# COMMAND ----------

# MAGIC %md
# MAGIC A naive `cast("double")` on `"$1,250.00"` returns **NULL** — Spark does not
# MAGIC raise, it just gives you nothing. That silent NULL is the whole reason this
# MAGIC layer exists, and it is why every parsed column gets a DQ rule asserting the
# MAGIC result is not NULL.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2 · Dates: three source systems, three formats
# MAGIC
# MAGIC | source | format | example |
# MAGIC |---|---|---|
# MAGIC | CRM | `dd/MM/yyyy` (Australian) | `11/12/2025` |
# MAGIC | betting engine | ISO 8601 with offset | `2026-06-23T15:56:30+10:00` |
# MAGIC | settlement | space-separated, no zone | `2026-06-13 23:50:00` |
# MAGIC
# MAGIC The offset one matters most. `+10:00` is converted into the session timezone,
# MAGIC which this project pins to `Australia/Melbourne` — not UTC. A wagering day is a
# MAGIC local business day, and a timezone slip moves a Saturday's AFL turnover into
# MAGIC Friday's report.

# COMMAND ----------

print("session timezone:", spark.conf.get("spark.sql.session.timeZone"))
display(
    spark.table(cfg.table("bronze", "customers"))
    .select("signup_date", silver.parse_date("signup_date", "dd/MM/yyyy", "yyyy-MM-dd").alias("parsed"))
    .limit(5)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3 · Deduplication: `row_number()`, not `dropDuplicates()`
# MAGIC
# MAGIC Source systems deliver at-least-once, so the same bet arrives twice. The fix is
# MAGIC a window function:
# MAGIC
# MAGIC ```python
# MAGIC window = Window.partitionBy("bet_id").orderBy(F.col("placed_at").desc())
# MAGIC df.withColumn("_rn", F.row_number().over(window)).where("_rn = 1")
# MAGIC ```
# MAGIC
# MAGIC `dropDuplicates(["bet_id"])` looks equivalent and is not: it keeps an
# MAGIC *arbitrary* row. When the duplicate is a genuine update, that is a coin flip.
# MAGIC Ordering by the source's version column and taking row 1 makes "latest wins" a
# MAGIC decision instead of an accident.

# COMMAND ----------

dupes = (
    raw.groupBy("bet_id").agg(F.count(F.lit(1)).alias("copies")).where(F.col("copies") > 1)
)
print(f"bet_ids arriving more than once in batch {BATCH}: {dupes.count()}")
display(dupes.limit(5))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4 · Run the layer
# MAGIC
# MAGIC Silver here is **append-only history with a `_current` companion**, not
# MAGIC overwrite-in-place. Each batch's cleansed rows are appended with their
# MAGIC `_batch_id`, so when a customer moves house in batch 2 we still hold what they
# MAGIC were in batch 1. That history is what makes the Data Vault satellites and the
# MAGIC type 2 dimension possible — if silver overwrote, it would be gone before
# MAGIC modelling ever saw it.
# MAGIC
# MAGIC `silver.customers_current` is the exception: maintained with a Delta `MERGE`,
# MAGIC one row per customer, for consumers who just want current state.

# COMMAND ----------

counts = silver.build_silver(spark, cfg, BATCH)
display(spark.createDataFrame(list(counts.items()), "table string, rows long"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### The MERGE, written out
# MAGIC
# MAGIC ```sql
# MAGIC MERGE INTO sportsbet_demo.silver.customers_current AS t
# MAGIC USING batch_of_changes AS s
# MAGIC   ON t.customer_id = s.customer_id
# MAGIC WHEN MATCHED THEN UPDATE SET *
# MAGIC WHEN NOT MATCHED THEN INSERT *
# MAGIC ```
# MAGIC
# MAGIC One catch worth knowing: the source must already be deduplicated on the merge
# MAGIC key. If two source rows match one target row, Delta raises an error rather than
# MAGIC picking one — which is a feature. It catches grain bugs at write time instead of
# MAGIC letting them become a wrong number in a dashboard.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data quality: quarantine, do not drop silently
# MAGIC
# MAGIC Three severities, and picking between them is a modelling decision:
# MAGIC
# MAGIC * `warn` — record it, keep the row
# MAGIC * `drop` — quarantine the row so it cannot poison the marts
# MAGIC * `fail` — abort the run; reserved for "the numbers would be wrong"
# MAGIC
# MAGIC Note the under-18 rule is `warn`, not `drop`. Dropping the customer would orphan
# MAGIC their bets and make turnover disagree with the ledger. A real platform routes
# MAGIC these to a compliance queue — the pipeline's job is to make them impossible to
# MAGIC miss, not to hide them.

# COMMAND ----------

display(
    spark.table(cfg.table("dq", "dq_results"))
    .where(F.col("rows_failed") > 0)
    .select("table_name", "expectation", "severity", "rows_checked", "rows_failed", "description")
    .orderBy(F.col("rows_failed").desc())
)

# COMMAND ----------

display(spark.table(cfg.table("dq", "quarantine")).limit(10))

# COMMAND ----------

# MAGIC %md
# MAGIC The same rules expressed declaratively in Delta Live Tables would be:
# MAGIC
# MAGIC ```python
# MAGIC @dlt.expect_or_drop("stake_positive", "stake_amount > 0")
# MAGIC @dlt.expect("customer_is_adult", "age_years >= 18")
# MAGIC @dlt.expect_or_fail("payout_not_negative", "payout_amount >= 0")
# MAGIC ```
# MAGIC
# MAGIC See `dlt/betting_dlt_pipeline.py`. DLT tracks pass rates per expectation in its
# MAGIC event log automatically, which is most of what `dq/expectations.py` builds by
# MAGIC hand here.
# MAGIC
# MAGIC **Next:** `03_raw_vault.py`
