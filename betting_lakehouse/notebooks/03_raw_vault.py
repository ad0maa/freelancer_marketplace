# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Raw Vault — Data Vault 2.0
# MAGIC
# MAGIC Data Vault splits every entity into exactly three table types:
# MAGIC
# MAGIC | type | holds | example |
# MAGIC |---|---|---|
# MAGIC | **Hub** | the business keys that exist | `hub_customer` — "these are the customers we have heard of" |
# MAGIC | **Link** | that keys are related | `link_bet_selection` — "this bet included this selection" |
# MAGIC | **Satellite** | descriptive attributes, with history | `sat_customer_details` — what the CRM said, and when |
# MAGIC
# MAGIC Three properties fall out of that, and they are the whole reason a regulated
# MAGIC business picks it:
# MAGIC
# MAGIC 1. **Insert-only** — nothing is ever updated or deleted, so the model is
# MAGIC    auditable by construction. When a regulator asks "what did you know about
# MAGIC    this customer's self-exclusion status on the 14th", the answer is a query.
# MAGIC 2. **Load-order independent** — hubs, links and satellites never read each
# MAGIC    other, so they load in parallel, in any order. That is why the Workflow in
# MAGIC    `databricks.yml` fans them out as three concurrent tasks.
# MAGIC 3. **Replayable** — loading the same batch twice inserts nothing.
# MAGIC
# MAGIC What it is *not* is queryable by humans. That is what notebook 04 is for.

# COMMAND ----------

import sys

REPO_ROOT = "/Workspace/Repos/you@example.com/betting_lakehouse"
if REPO_ROOT + "/src" not in sys.path:
    sys.path.insert(0, REPO_ROOT + "/src")

dbutils.widgets.text("batch", "1", "Landing batch to load")
BATCH = int(dbutils.widgets.get("batch"))

from pyspark.sql import functions as F

from betting_lakehouse.config import Config
from betting_lakehouse.vault import dv_helpers as dv
from betting_lakehouse.vault import raw_vault

cfg = Config.load(REPO_ROOT + "/conf/pipeline.yml")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Hash keys
# MAGIC
# MAGIC A hash key is SHA-256 over the business key, upper-cased, trimmed, `||`
# MAGIC separated, with NULL replaced by `^^`.
# MAGIC
# MAGIC **Why hash instead of a sequence number?** Because every job, on every source,
# MAGIC can compute it independently and in parallel with no lookup. A sequence needs a
# MAGIC central allocator, which serialises your loads and becomes the bottleneck the
# MAGIC moment two sources feed one hub.
# MAGIC
# MAGIC The costs are real: 64 hex characters is wide, and the value is meaningless to
# MAGIC read. Some shops use `xxhash64` for the size win.

# COMMAND ----------

display(
    spark.range(1).select(
        F.lit("C000123").alias("business_key"),
        dv.hash_key(F.lit("C000123")).alias("hk_customer"),
        dv.hash_key(F.lit("  c000123  ")).alias("hk_from_messy_input"),
    )
)

# COMMAND ----------

# MAGIC %md
# MAGIC Those two hashes are **identical**, and that is the point. Standardising the key
# MAGIC before hashing is not a nicety: `" c000123 "` and `"C000123"` are the same
# MAGIC person, and hashing them unnormalised gives you two hubs, two satellites and two
# MAGIC rows in every report about them. Because the hash is opaque, nobody notices
# MAGIC until someone asks why the customer count went up 4%.
# MAGIC
# MAGIC Order matters too — `hash_key(bet_id, selection_id)` ≠ `hash_key(selection_id,
# MAGIC bet_id)`. The column order in a link definition is part of its contract.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Load the vault

# COMMAND ----------

inserted = raw_vault.build_raw_vault(spark, cfg, BATCH)
display(spark.createDataFrame(list(inserted.items()), "table string, rows_inserted long"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run it again — nothing happens
# MAGIC
# MAGIC This is the headline property. Hubs and links anti-join on the hash key, so an
# MAGIC already-present key is skipped. Satellites compare a `hash_diff` of the payload
# MAGIC against the latest known version and insert only on a real change.
# MAGIC
# MAGIC This is why the job in `databricks.yml` can carry `max_retries: 2` without
# MAGIC anyone having to think about what a partial failure did.

# COMMAND ----------

second = raw_vault.build_raw_vault(spark, cfg, BATCH)
print(f"rows inserted on the second run of batch {BATCH}: {sum(second.values())}")
assert sum(second.values()) == 0, "vault load is not idempotent"

# COMMAND ----------

# MAGIC %md
# MAGIC ## `hash_diff`: how a satellite detects change
# MAGIC
# MAGIC One SHA-256 over the whole payload, compared against the latest stored version.
# MAGIC That beats comparing forty nullable columns with `IS DISTINCT FROM`, and it
# MAGIC keeps the loader completely generic — `load_satellite` does not know or care
# MAGIC what a customer is.
# MAGIC
# MAGIC **The trap:** add a column to the payload and every `hash_diff` changes, so the
# MAGIC next load inserts a new version of every row. Not corruption, but a surprise if
# MAGIC you expected a small delta.

# COMMAND ----------

sat = spark.table(cfg.table("vault", "sat_customer_details"))
multi = (
    sat.groupBy("hk_customer").agg(F.count(F.lit(1)).alias("versions")).where(F.col("versions") > 1)
)
print(f"customers with more than one version: {multi.count()}")

display(
    sat.join(multi.select("hk_customer"), on="hk_customer")
    .select("hk_customer", "load_date", "hash_diff", "state_code", "account_status",
            "vip_tier", "is_self_excluded", "record_source")
    .orderBy("hk_customer", "load_date")
    .limit(6)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Satellites store no end date — on purpose
# MAGIC
# MAGIC A satellite records `load_date`, the moment a version *started*, and nothing
# MAGIC else. Writing an end date onto the previous row would mean updating it, and that
# MAGIC would break the insert-only guarantee that makes the vault auditable.
# MAGIC
# MAGIC So end dates are derived at read time with `lead()`:

# COMMAND ----------

display(
    dv.satellite_scd2(sat, "hk_customer")
    .join(multi.select("hk_customer"), on="hk_customer")
    .select("hk_customer", "version_number", "state_code", "account_status",
            "effective_from", "effective_to", "is_current")
    .orderBy("hk_customer", "version_number")
    .limit(6)
)

# COMMAND ----------

# MAGIC %md
# MAGIC **That is the bridge between the two modelling styles.** An insert-only
# MAGIC satellite plus one `lead()` window *is* a type 2 dimension. `dim_customer` in
# MAGIC notebook 04 is built by running exactly this function.
# MAGIC
# MAGIC The open interval ends at `9999-12-31` rather than NULL so that
# MAGIC `BETWEEN effective_from AND effective_to` works with no special case for the
# MAGIC current row — a small thing that saves a NULL-handling bug in every join.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Two details worth stealing
# MAGIC
# MAGIC ### Hubs load from every source that carries the key
# MAGIC
# MAGIC `hub_selection` is loaded from the pricing feed *and* from bet legs. So a
# MAGIC selection id that appears on a bet but was never priced still exists in the hub,
# MAGIC with no satellite row. That is exactly the truth: we know the key exists, we know
# MAGIC nothing about it. The mart resolves those to an unknown member.
# MAGIC
# MAGIC ### `leg_number` is a dependent child key
# MAGIC
# MAGIC `link_bet_selection` hashes `(bet_id, selection_id, leg_number)`. Without
# MAGIC `leg_number`, a bet that includes the same selection on two legs collapses to one
# MAGIC row and a leg silently disappears. This is the most common Data Vault modelling
# MAGIC mistake and it shows up as quietly undercounted rows.

# COMMAND ----------

display(
    spark.sql(f"""
        SELECT h.selection_id,
               s.hk_selection IS NOT NULL AS has_price_satellite
        FROM {cfg.table('vault', 'hub_selection')} h
        LEFT JOIN {cfg.table('vault', 'sat_selection_price')} s USING (hk_selection)
        WHERE s.hk_selection IS NULL
        LIMIT 10
    """)
)

# COMMAND ----------

# MAGIC %md
# MAGIC **Next:** `04_gold_star_schema.py`
