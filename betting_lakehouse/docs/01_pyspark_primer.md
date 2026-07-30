# PySpark, for someone who has written SQL and Python

You do not need to learn a new language. PySpark is a Python API that builds a
query plan, which a distributed engine then executes. If you know SQL, you already
know most of what it does — the surprises are all about *where* the code runs and
*when*.

---

## 1. The mental model: you are writing a query, not a program

```python
df = spark.read.table("silver.bets")          # nothing has happened
big = df.where(F.col("stake_amount") > 100)   # still nothing
count = big.count()                           # NOW Spark runs something
```

Every line except the last builds a *plan*. `where`, `select`, `join`, `groupBy`
are **transformations** — lazy, they only describe intent. `count`, `collect`,
`show`, `write` are **actions** — they compile the plan, optimise it, and execute.

Two consequences that bite everyone once:

**A DataFrame is a recipe, not a result.** Use one twice and the work happens
twice, from the source, unless you `.cache()` it. This is why
`dq/expectations.py` calls `.persist()` before evaluating several counts over the
same frame.

**Errors surface late.** A typo in a column name shows up at the action, not at
the line that made the mistake — so a stack trace points at `.count()` twenty
lines below the actual bug.

---

## 2. Partitions, and why work gets slow

Your data is split into **partitions**, and one partition is processed by one
task on one core. That single fact explains almost every Spark performance
question.

**Narrow transformations** (`select`, `where`, `withColumn`) work inside a
partition. No data moves. Nearly free.

**Wide transformations** (`groupBy`, `join`, `orderBy`, `distinct`, window
functions) need rows with the same key together, so Spark writes every partition
to disk, moves it across the network, and reads it back. That is a **shuffle**,
and it is the expensive thing. When a job takes an hour instead of a minute, the
answer is almost always "how many shuffles, and how big".

You cannot avoid shuffles — you can only avoid unnecessary ones:

```python
# Two shuffles: one to join, one to aggregate.
legs.join(events, "event_sk").groupBy("sport_code").agg(F.sum("stake_allocated"))

# One shuffle: the small side is broadcast to every executor instead of moved.
legs.join(F.broadcast(events), "event_sk").groupBy("sport_code").agg(...)
```

A **broadcast join** ships the small table whole to every executor so the big
table never moves. It is the single highest-value optimisation in day-to-day work,
and it is used deliberately throughout this repo — see `silver.transform_events`
and `facts._lookup`. It stops working when the "small" side is not small: 100MB+
and you are shipping that to every executor.

### `spark.sql.shuffle.partitions`

The default is 200, meaning every shuffle produces 200 partitions regardless of
your data size. On the 9,000 rows in this demo that is 200 nearly-empty tasks and
a pipeline that spends all its time on scheduling — hence `shuffle_partitions: 8`
in `conf/pipeline.yml`. On real volumes you leave it alone and let Adaptive Query
Execution coalesce partitions at runtime.

---

## 3. Adaptive Query Execution (AQE)

On by default since Spark 3.2. At each shuffle boundary, Spark looks at the actual
data it just produced and re-plans:

- coalesces hundreds of tiny post-shuffle partitions into a few sensible ones
- switches a sort-merge join to a broadcast join once it sees the real size
- splits skewed partitions so one enormous key does not hold up the whole stage

This is why "just tune the partition count" is much less often the answer than it
was in Spark 2. Check whether AQE already fixed it before you tune anything.

---

## 4. Skew

If one key has far more rows than the others, one task gets all of it, and your
stage is as slow as that task. In wagering data this is guaranteed: a Melbourne
Cup fixture carries more bets than a Tuesday NBL game by orders of magnitude, and
the top 1% of customers place a large share of all bets.

Symptom: 199 tasks finish in seconds, one runs for twenty minutes.

Fixes, cheapest first: let AQE's skew join handle it; broadcast the small side to
avoid the shuffle entirely; or salt the key (append a random suffix, aggregate,
then re-aggregate).

---

## 5. `Column` expressions, not Python values

This is the thing that trips up Python developers hardest:

```python
# WRONG - `if` runs in Python, on a Column object, once, at plan time
if df["stake_amount"] > 100:
    ...

# RIGHT - a Column expression, evaluated per row by the engine
df.withColumn("is_big", F.when(F.col("stake_amount") > 100, True).otherwise(False))
```

`F.col("x")` is a *reference* to a column, not its value. Python control flow
cannot see row data. Anything conditional per row is `F.when(...).otherwise(...)`;
anything null-handling is `F.coalesce`, `F.nullif` or `.eqNullSafe`.

### NULLs will get you

`F.col("a") != F.col("b")` is **NULL**, not True, when either side is NULL — and
NULL is not true, so the row is filtered out. This is why the satellite loader in
`vault/dv_helpers.py` uses `.eqNullSafe()`: a brand new key has no previous
`hash_diff`, and a plain `!=` would silently skip exactly the rows it needs to
insert.

---

## 6. Avoid Python UDFs

```python
# Python UDF: every row leaves the JVM, is pickled into a Python process,
# comes back. Roughly 10-100x slower, and invisible to the optimiser.
@F.udf("double")
def implied_probability(odds):
    return 1.0 / odds

# Built-in: runs in the JVM, vectorised, and the optimiser can reason about it.
F.try_divide(F.lit(1), F.col("decimal_odds"))
```

There is a built-in for almost everything — 400+ functions, including
`regexp_replace`, `try_to_timestamp`, `sha2`, `xxhash64`, `months_between`,
`array_compact`. Search before you write a UDF. This repo has zero UDFs, and that
is not a constraint anyone had to work around.

If you genuinely need Python logic, use a **Pandas UDF** (`@F.pandas_udf`), which
moves data in Arrow batches instead of row by row and is usually 10x faster than a
plain UDF.

---

## 7. ANSI mode: the big Spark 4 change

Spark 4 turns ANSI SQL mode on by default, and it changes error behaviour rather
than syntax:

| expression | Spark 3 | Spark 4 (ANSI) |
|---|---|---|
| `CAST('abc' AS INT)` | `NULL` | raises `CAST_INVALID_INPUT` |
| `to_date('abc', 'dd/MM/yyyy')` | `NULL` | raises `CANNOT_PARSE_TIMESTAMP` |
| `1 / 0` | `NULL` | raises `DIVIDE_BY_ZERO` |

This is a genuine improvement — silent NULLs from failed casts have hidden more
data bugs than anything else in Spark. But it changes how a cleansing layer has to
be written: **one malformed row in a five-million-row batch now aborts the whole
run.**

The answer is the `try_*` family:

```python
F.col("stake").try_cast("decimal(18,2)")           # NULL, not an exception
F.try_to_timestamp(F.col("placed_at"))
F.try_divide(F.lit(1), F.col("decimal_odds"))
```

…paired with a data quality rule that asserts the result is not NULL. That way the
bad row is *named and quarantined* rather than either crashing the batch or
disappearing. See `silver.money` and the note at the bottom of `silver.py`.

Migrating a Spark 3 pipeline to Spark 4 is largely a matter of finding every cast
on data you do not control and making this change.

---

## 8. Window functions

The workhorse of every warehouse pipeline. Three uses in this repo, and they cover
most real cases:

```python
# 1. Deduplicate: keep the newest row per key
w = Window.partitionBy("bet_id").orderBy(F.col("placed_at").desc())
df.withColumn("_rn", F.row_number().over(w)).where("_rn = 1")

# 2. Derive an end date from the next row (SCD2)
w = Window.partitionBy("hk_customer").orderBy("load_date")
df.withColumn("effective_to", F.lead("load_date").over(w) - F.expr("INTERVAL 1 SECOND"))

# 3. Add an aggregate without collapsing rows
df.withColumn("legs_in_bet", F.count(F.lit(1)).over(Window.partitionBy("hk_bet")))
```

Use `row_number()` for deduplication, not `dropDuplicates()` — the latter keeps an
*arbitrary* row, so when the duplicate is a genuine update you get a coin flip.

Cost: a window function is a shuffle plus a sort. Partition by something with
reasonable cardinality; `Window.partitionBy()` with no argument pulls the entire
dataset into one partition and will fall over on real volumes.

---

## 9. Reading a query plan

```python
df.explain(True)          # parsed, analysed, optimised, physical
df.explain("formatted")   # easier to read; start here
```

Four things to look for, in order of how often they matter:

1. **`BroadcastHashJoin` vs `SortMergeJoin`** — did the small side get broadcast?
2. **`Exchange`** — each one is a shuffle. Count them.
3. **`PushedFilters`** — did your `WHERE` reach the file scan, or is Spark reading
   everything and filtering afterwards?
4. **`PartitionFilters`** — did partition pruning happen?

In the Spark UI (or Databricks' query profile) the same information is visual, and
the number to look at is **spill**: if a stage spills to disk, it did not have
enough memory and everything after it is slow.

---

## 10. Decimal, not double, for money

```python
F.col("stake").try_cast("decimal(18,2)")   # exact
F.col("stake").cast("double")              # drifts
```

Floating point cannot represent `0.10` exactly. Sum a few million stakes as
doubles and the total is out by cents; a finance team that reconciles to the cent
*will* find it. Decimal arithmetic in Spark is exact and the performance
difference is irrelevant at any realistic scale.

---

## Where to see each of these in this repo

| idea | file |
|---|---|
| lazy evaluation, `.persist()` before repeated counts | `dq/expectations.py` |
| broadcast joins | `silver.transform_events`, `gold/facts._lookup` |
| `row_number()` deduplication | `silver.deduplicate` |
| `lead()` for SCD2 end-dating | `vault/dv_helpers.satellite_scd2` |
| window aggregate without collapsing | `gold/facts.build_fact_bet_leg` |
| null-safe comparison | `vault/dv_helpers.load_satellite` |
| ANSI-safe parsing | `silver.money`, `silver.parse_date` |
| decimal money | everywhere; see `silver.money` |
| shuffle partition config | `spark.py`, `conf/pipeline.yml` |
