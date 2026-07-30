# Databricks, and what it adds to Spark

Databricks is a managed platform built by the people who created Spark. Knowing
Spark is most of the job; what follows is the rest — the parts that only exist on
the platform, and the parts where the platform's default is different from
open-source Spark's.

---

## 1. Compute

You never install Spark. You pick a **cluster**, and Databricks starts one.

**All-purpose clusters** are interactive — you attach notebooks, they stay warm,
several people share them. Convenient, and roughly double the DBU rate.

**Job clusters** start for one job run and terminate when it finishes. Cheaper,
fully isolated. **Scheduled work should always run on a job cluster** — running it
on an interactive cluster is the most common way a Databricks bill doubles for no
benefit. See `job_clusters` in `databricks.yml`.

**Serverless** removes the cluster entirely: no node types, no autoscaling config,
starts in seconds. It is the default for new SQL warehouses and DLT pipelines, and
increasingly for jobs. The trade-off is less control over Spark conf and library
versions.

**Photon** is Databricks' C++ rewrite of Spark's execution engine. Same API, 2-4x
faster on scan- and aggregate-heavy SQL. Toggled on, not coded for.

### The DBR version matters

Databricks Runtime (`spark_version: 16.4.x-scala2.13`) pins the Spark version, the
Delta version, and the Python libraries. It is the single most important thing to
know about an environment: `to_date('garbage')` returning NULL versus raising
depends on it, because ANSI mode arrived as a default in Spark 4.

---

## 2. Delta Lake

Parquet files plus a transaction log. That log is what turns a directory of files
into a table:

**ACID transactions.** A write either commits or does not. Readers never see a
half-written table, so a failed job leaves the previous version intact — which is
why the mart rebuilds in this repo are safe to run over live tables.

**`MERGE`.** Upserts, in one statement. Before Delta this was read-everything,
anti-join, rewrite-the-table — which is exactly what `io_utils.merge_into` falls
back to when Delta is unavailable, and it is worth reading those two branches side
by side to see what Delta bought.

```sql
MERGE INTO silver.customers_current AS t
USING changes AS s ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
```

**Time travel.** Every write is a version.

```sql
SELECT * FROM bronze.bets VERSION AS OF 3;
SELECT * FROM bronze.bets TIMESTAMP AS OF '2026-06-27';
RESTORE TABLE bronze.bets TO VERSION AS OF 3;
DESCRIBE HISTORY bronze.bets;
```

This is how you answer "did that column start arriving empty today, or has it
always been empty" without restoring a backup. Retention is bounded — `VACUUM`
removes files no longer referenced by a retained version (7 days by default), so
it is a safety net, not an archive.

**Schema enforcement and evolution.** A write with the wrong types is rejected
rather than silently corrupting the table. `mergeSchema` allows *additive* changes;
`overwriteSchema` allows anything on a full overwrite.

**`replaceWhere`.** Atomic partition replacement in one write — the production
form of the batch-replay trick in `io_utils.replace_batch`:

```python
df.write.mode("overwrite").option("replaceWhere", "_batch_id = '2'").saveAsTable(t)
```

**Change Data Feed.** Turn it on and Delta records row-level inserts, updates and
deletes, readable as a stream — so a downstream table can consume changes without
you building a change-detection mechanism.

---

## 3. Small files, and the two ways to fix them

The number one performance problem in a real lakehouse. A stream committing every
30 seconds produces 2,880 files per table per day, and every query pays to list
them.

```sql
OPTIMIZE gold.fact_bet_leg;                                  -- compact
OPTIMIZE gold.fact_bet_leg ZORDER BY (placed_date, event_sk); -- compact + cluster
```

`ZORDER` co-locates rows with similar values so a filter on `placed_date` skips
whole files instead of reading them. **Liquid clustering** is the modern
replacement:

```sql
ALTER TABLE gold.fact_bet_leg CLUSTER BY (placed_date, event_sk);
```

Same skipping, no partition-key decision to get wrong, and the keys can be changed
later without rewriting the table. On Databricks-managed tables, *predictive
optimization* runs this maintenance for you.

### When to partition

Only when each partition is comfortably over ~1GB. Partitioning a 9,000-row fact
by date — as this demo deliberately does not — creates hundreds of tiny files and
makes everything slower. Cluster instead. Getting this backwards is one of the most
common mistakes on real projects.

---

## 4. Unity Catalog

The governance layer, and the reason table names have three parts:

```
catalog . schema . table
sportsbet_demo.gold.fact_bet_leg
```

**The catalog is your dev/prod boundary.** A dev job physically cannot write to
prod tables because it has no grant on the prod catalog. Schema-name prefixes
(`dev_gold`) give you none of that. This is why `config.py` resolves the catalog
from one variable and every table name is built from it.

**Grants are on objects, not files.** An analyst gets `SELECT` on the gold schema
and cannot reach the underlying storage, even though the data is just Parquet in a
bucket.

**Row filters and column masks** attach to the *table*, so they apply however it is
queried — notebook, SQL warehouse, BI tool, job. Masking in a view is trivially
bypassed by querying the underlying table; a UC column mask is not. See
`sql/00_unity_catalog_setup.sql` for the email mask and the self-exclusion row
filter.

**Lineage and audit are automatic.** Column-level lineage across the whole
medallion stack is a query rather than a documentation exercise. In a regulated
industry this is often the actual reason to adopt it.

**Volumes** are UC-governed file storage for non-tabular data — the modern
replacement for DBFS mounts, which you should not create in new work.

---

## 5. Auto Loader

Incremental file ingestion, and the thing you will use for every bronze table.

```python
(spark.readStream.format("cloudFiles")
   .option("cloudFiles.format", "json")
   .option("cloudFiles.schemaLocation", f"{checkpoint}/schema")
   .option("cloudFiles.schemaEvolutionMode", "rescue")
   .load(landing_path)
   .writeStream
   .option("checkpointLocation", f"{checkpoint}/commits")
   .trigger(availableNow=True)
   .toTable("bronze.bets"))
```

It tracks which files it has already processed in a RocksDB checkpoint, so you
point it at a directory once and it picks up new files forever — no watermark
table, no "load yesterday" parameter, and no gap when a file arrives late.

`trigger(availableNow=True)` is the option to remember: it makes a stream behave
like a batch job that catches up on everything new and then stops, which is what
most "streaming" pipelines actually want. Drop it for a continuous stream.

`schemaEvolutionMode: rescue` puts unexpected columns into `_rescued_data` instead
of dropping them — in bronze that is always right, because an upstream team adding
a field should never be a data loss event.

---

## 6. Workflows and Asset Bundles

A **Workflow** is a DAG of tasks with retries, schedules, notifications and
concurrency limits.

A **Databricks Asset Bundle** is that Workflow as a YAML file in your repo. Before
bundles, jobs were clicked together in the UI and the definition lived nowhere —
you could not review it, diff it, or promote it without repeating the clicking.

```bash
databricks bundle validate
databricks bundle deploy --target dev
databricks bundle run betting_lakehouse
databricks bundle deploy --target prod
```

Two details from `databricks.yml` worth copying:

**`mode: development`** prefixes resources with your username and pauses schedules,
so deploying your branch cannot fire the production schedule. **`mode: production`**
refuses to deploy with uncommitted changes.

**`run_as: service_principal_name`** — production jobs run as a service principal,
never as a person, otherwise the pipeline breaks the day that person's account is
disabled.

### Wheel tasks vs notebook tasks

`python_wheel_task` runs an entry point from an installed package;
`notebook_task` runs a notebook. Prefer the wheel for pipeline logic: the code is
importable, so it can be unit tested off-cluster (see `tests/`), and a failure
gives you a stack trace in a module rather than a cell number. Notebooks are for
exploration and for documentation you can run.

---

## 7. Delta Live Tables

Declarative pipelines: you define tables as functions, and DLT derives the DAG from
your `dlt.read()` calls, manages checkpoints, tracks expectations as metrics, and
maintains SCD2 tables from a change feed.

```python
@dlt.table
@dlt.expect_or_drop("stake_positive", "stake_amount > 0")
def silver_bets():
    return dlt.read_stream("bronze_bets").select(...)
```

What it costs you: control of the write (no `MERGE` you wrote), testability
off-cluster (`import dlt` only resolves on a DLT cluster), and portability.

The split this repo uses is the one that holds up in practice: **DLT for ingest and
cleansing**, where the work is repetitive and the value is in expectations and
automatic incrementalisation; **imperative jobs for the vault and marts**, where the
load semantics *are* the design. Insert-only hubs, hash-diff satellites and
allocated measures are not expressible as "declare a table". Compare
`dlt/betting_dlt_pipeline.py` with `src/` and the boundary is fairly clear.

---

## 8. Notebooks, in a repo

Commit notebooks in **source format** — a `.py` file with `# COMMAND ----------`
separators and `# MAGIC %md` markdown cells, as in `notebooks/`. Importing it into
a workspace gives real cells, and because it is still a Python file the git diffs
are reviewable. The `.ipynb` JSON alternative produces diffs nobody can review.

Things that exist only in a notebook:

- `spark` and `dbutils` are pre-defined — **never build your own SparkSession**
- `%sql`, `%md`, `%sh`, `%pip` magics
- `dbutils.widgets` for parameters, which is how a job passes arguments to a
  notebook task
- `display()` — renders a DataFrame with sorting and charting, unlike `.show()`

---

## 9. Cost, briefly

You are billed for DBUs (compute time × instance rate) plus the underlying cloud
VMs and storage. The things that actually move the number:

- scheduled work on all-purpose instead of job clusters — roughly 2x
- clusters with no auto-termination, idling overnight
- `spot_with_fallback` instances for workers: 60-90% cheaper, and a lost spot node
  on an idempotent pipeline just means a retry
- small files: more files means more listing, more tasks, more DBUs for the same data
- serverless SQL warehouses with a long auto-stop window

---

## 10. What to be able to talk about

If a conversation about a Databricks data engineering role goes technical, these
come up:

- medallion architecture, and what belongs in each layer (and what does not)
- Delta `MERGE`, time travel, `OPTIMIZE`/`ZORDER`, liquid clustering
- Auto Loader, and why it beats a watermark table
- Unity Catalog three-level namespace, and why the catalog is the dev/prod boundary
- job clusters vs all-purpose, and the cost difference
- DLT vs imperative jobs, and when you would choose each
- Asset Bundles for deployment
- idempotency: why every task in `databricks.yml` can safely carry `max_retries`
- small files, partitioning vs clustering, and shuffle skew
