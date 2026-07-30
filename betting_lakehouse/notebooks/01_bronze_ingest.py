# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Bronze — land the raw data, change nothing
# MAGIC
# MAGIC This notebook is a Databricks *source-format* notebook: a plain `.py` file
# MAGIC with `# COMMAND ----------` cell separators and `# MAGIC %md` markdown cells.
# MAGIC Importing it into a workspace gives you real notebook cells, and because it is
# MAGIC still a Python file, git diffs on it are readable. Always commit notebooks in
# MAGIC this format — the `.ipynb` JSON alternative produces diffs nobody can review.
# MAGIC
# MAGIC ### What bronze is for
# MAGIC
# MAGIC A byte-faithful copy of what the source system sent, plus provenance. Every
# MAGIC column is a string. Nothing is cast, filtered or deduplicated.
# MAGIC
# MAGIC The reason is replay. When someone finds a bug in the silver logic in eighteen
# MAGIC months, you want to rebuild silver and gold from bronze — by which time the
# MAGIC source system has aged out its history, changed its schema, or been switched
# MAGIC off. The moment bronze applies a business rule, it stops being a record of
# MAGIC what happened and you lose that ability.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup
# MAGIC
# MAGIC On Databricks Repos the repo root is not automatically on `sys.path`, so a
# MAGIC notebook that imports the project package has to add it. In a bundle
# MAGIC deployment the package is installed as a wheel instead and this is a no-op.

# COMMAND ----------

import sys

REPO_ROOT = "/Workspace/Repos/you@example.com/betting_lakehouse"
if REPO_ROOT + "/src" not in sys.path:
    sys.path.insert(0, REPO_ROOT + "/src")

dbutils.widgets.text("batch", "1", "Landing batch to load")
BATCH = int(dbutils.widgets.get("batch"))

# COMMAND ----------

from pyspark.sql import functions as F

from betting_lakehouse import bronze
from betting_lakehouse.config import Config
from betting_lakehouse.io_utils import ensure_schemas

cfg = Config.load(REPO_ROOT + "/conf/pipeline.yml")

# NOTE: there is no SparkSession.builder call anywhere in this notebook. The
# cluster created `spark` before the first cell ran. Building your own session on
# Databricks either silently returns the existing one or fights the cluster config.
ensure_schemas(spark, cfg)
print(f"Writing into catalog: {cfg.catalog}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The batch ingest
# MAGIC
# MAGIC Each source dataset is read with an **explicit all-string schema** and four
# MAGIC audit columns are added:
# MAGIC
# MAGIC | column | why it exists |
# MAGIC |---|---|
# MAGIC | `_ingested_at` | when we received the row, as distinct from when it happened |
# MAGIC | `_source_file` | which file it came from — the first thing you need in an incident |
# MAGIC | `_batch_id` | which load produced it; makes a replay replaceable |
# MAGIC | `_record_source` | which system said it; carried all the way into the satellites |

# COMMAND ----------

counts = bronze.ingest_batch(spark, cfg, BATCH)
display(spark.createDataFrame(list(counts.items()), "table string, rows long"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Look at what landed
# MAGIC
# MAGIC `stake_amount` is `"$1,250.00"`. `placed_at` is a string with a `+10:00`
# MAGIC offset. That is correct for bronze — it is what the betting engine sent.

# COMMAND ----------

display(
    spark.table(cfg.table("bronze", "bets"))
    .select("bet_id", "stake_amount", "potential_payout", "placed_at", "_source_file", "_batch_id")
    .limit(10)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Why the ingest is replayable
# MAGIC
# MAGIC Bronze is append-only, which normally means re-running a failed job doubles
# MAGIC your raw rows. `replace_batch` deletes the batch's rows before appending, so a
# MAGIC retry converges instead of accumulating. Run the cell below twice — the count
# MAGIC does not move.
# MAGIC
# MAGIC On Delta the atomic version of this is a single write:
# MAGIC
# MAGIC ```python
# MAGIC (df.write.format("delta").mode("overwrite")
# MAGIC    .option("replaceWhere", f"_batch_id = '{BATCH}'")
# MAGIC    .saveAsTable(target))
# MAGIC ```
# MAGIC
# MAGIC …which replaces just that partition in one transaction, so readers never see
# MAGIC the table mid-delete.

# COMMAND ----------

before = spark.table(cfg.table("bronze", "bets")).count()
bronze.ingest_batch(spark, cfg, BATCH)
after = spark.table(cfg.table("bronze", "bets")).count()
print(f"bets before: {before:,}   after re-running the same batch: {after:,}")
assert before == after, "bronze ingest is not idempotent"

# COMMAND ----------

# MAGIC %md
# MAGIC ## In production this is Auto Loader, not a batch read
# MAGIC
# MAGIC The batch version has to be told which batch to load. Auto Loader tracks which
# MAGIC files it has already seen in a RocksDB checkpoint, so you point it at a
# MAGIC directory once and it picks up new files forever — no watermark table, no
# MAGIC "load yesterday" parameter, and no gap when a file lands late.
# MAGIC
# MAGIC ```python
# MAGIC (spark.readStream.format("cloudFiles")
# MAGIC    .option("cloudFiles.format", "json")
# MAGIC    .option("cloudFiles.schemaLocation", f"{checkpoint}/schema")
# MAGIC    .option("cloudFiles.schemaEvolutionMode", "rescue")
# MAGIC    .load(landing_path)
# MAGIC    .writeStream
# MAGIC    .option("checkpointLocation", f"{checkpoint}/commits")
# MAGIC    .trigger(availableNow=True)      # catch up on everything new, then stop
# MAGIC    .toTable("sportsbet_demo.bronze.bets"))
# MAGIC ```
# MAGIC
# MAGIC `trigger(availableNow=True)` is the one to know: it makes a stream behave like
# MAGIC a batch job on a schedule, which is what most "streaming" pipelines actually
# MAGIC want. See `bronze.ingest_stream_autoloader` for the full version.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Delta gives you time travel over bronze
# MAGIC
# MAGIC Every write is a numbered version, so you can query the table as it was before
# MAGIC a load. This is how you answer "did that column start arriving empty today, or
# MAGIC has it always been empty" without restoring a backup.

# COMMAND ----------

display(spark.sql(f"DESCRIBE HISTORY {cfg.table('bronze', 'bets')}"))

# COMMAND ----------

# MAGIC %md
# MAGIC ```sql
# MAGIC SELECT count(*) FROM sportsbet_demo.bronze.bets VERSION AS OF 1;
# MAGIC SELECT count(*) FROM sportsbet_demo.bronze.bets TIMESTAMP AS OF '2026-06-27';
# MAGIC RESTORE TABLE sportsbet_demo.bronze.bets TO VERSION AS OF 1;
# MAGIC ```
# MAGIC
# MAGIC Retention is bounded: `VACUUM` deletes files no longer referenced by any
# MAGIC retained version (default 7 days). Time travel is a safety net, not an archive.
# MAGIC
# MAGIC **Next:** `02_silver_cleanse.py`
