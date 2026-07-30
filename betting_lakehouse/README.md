# Betting lakehouse — a PySpark + Databricks demo

A working data platform for an Australian wagering operator, built the way a
Databricks data engineering team builds one: medallion architecture in PySpark, a
**Data Vault 2.0** raw vault as the system of record, and **Kimball dimensional
marts** on top for analysts.

It runs end to end on a laptop in about four minutes, with real Delta Lake tables,
a real metastore, and synthetic-but-realistic AFL/NRL/racing betting data. No
Databricks account needed.

```bash
cd betting_lakehouse
make setup     # venv + PySpark 4 + Delta Lake  (~2 min, downloads ~400MB)
make run       # generate two batches of source data and run everything
make test      # 48 tests
```

Everything is self-contained in this directory and touches nothing else in the
repository.

There is also a **[visual walkthrough](https://claude.ai/code/artifact/e0c1f128-b4cd-48b9-b46e-8114cbb3ee39)**
— the same story as a single page, tracing one $18.04 multi from a raw file to a
margin number, with the real figures from a run. Start there if you would rather
read than clone.

---

## What it builds

```
   SOURCE SYSTEMS              landing files (JSON + CSV)
   CRM · betting engine  ──►   customers, events, markets, selections,
   pricing · payments          bets, bet_legs, settlements, payments
                                          │
   ┌──────────────────────────────────────▼──────────────────────────────────┐
   │ BRONZE   raw, unmodified, every column a string, + provenance columns   │
   │          replayable: re-running a batch replaces it, never duplicates   │
   ├─────────────────────────────────────────────────────────────────────────┤
   │ SILVER   typed · deduplicated · standardised · conformed · quarantined  │
   │          append-only history + MERGE-maintained *_current tables        │
   ├─────────────────────────────────────────────────────────────────────────┤
   │ RAW VAULT (Data Vault 2.0)          system of record, never rebuilt     │
   │   5 hubs · 4 links · 7 satellites · insert-only · auditable            │
   ├─────────────────────────────────────────────────────────────────────────┤
   │ GOLD (Kimball)                      presentation, rebuilt every run     │
   │   6 dimensions (dim_customer is SCD2) · 2 facts at 2 grains · 2 aggs   │
   └─────────────────────────────────────────────────────────────────────────┘
```

Two batches are loaded, because a pipeline that has only ever seen one batch is not
a pipeline. Batch 2 is "the next day": changed customers, new bets, price moves,
settlements arriving late for batch 1 bets, and **three fixtures that arrive after
bets were already placed on them**.

---

## What `make run` prints

Real output from a run, not an illustration:

```
-- RAW VAULT  silver -> hubs, links, satellites  (batch 2) -------------------
  hub_customer                              0 inserted
  hub_bet                               1,199 inserted
  ...

TYPE 2 HISTORY: WHAT dim_customer LOOKS LIKE WHEN SOMEONE CHANGES
+--------------+----------+--------------+----------------+-------------------+-------------------+----------+
|version_number|state_code|account_status|is_self_excluded|effective_from     |effective_to       |is_current|
+--------------+----------+--------------+----------------+-------------------+-------------------+----------+
|1             |SA        |ACTIVE        |false           |1900-01-01 00:00:00|2026-06-28 11:59:59|false     |
|2             |SA        |CLOSED        |true            |2026-06-28 12:00:00|9999-12-31 23:59:59|true      |
+--------------+----------+--------------+----------------+-------------------+-------------------+----------+

DOES IT ADD UP?
  Turnover from the leg fact (allocated) : $810,040.43
  Turnover from the slip fact (exact)    : $810,040.43
  Difference                             : $0.00  OK

LATE-ARRIVING DIMENSIONS, RESOLVING THEMSELVES
                    after batch 1     after batch 2
    event_sk        142                8
```

Then:

```bash
python run_pipeline.py --batch 2 --skip-generate    # every vault load inserts 0 rows
```

---

## Where to look

Read in this order. The whole thing is about 3,000 lines, and the comments carry
the teaching.

| # | file | what it shows |
|---|---|---|
| 1 | `docs/01_pyspark_primer.md` | shuffles, broadcast joins, windows, ANSI mode |
| 2 | `docs/02_databricks_primer.md` | Delta, Unity Catalog, Auto Loader, DLT, bundles, cost |
| 3 | `docs/03_data_vault_vs_dimensional.md` | why both models exist here |
| 4 | `docs/04_betting_domain_glossary.md` | odds, overround, turnover, GGR, hold %, AU regulation |
| 5 | **`src/betting_lakehouse/vault/dv_helpers.py`** | the four generic Data Vault loaders — the code most worth reading twice |
| 6 | `src/betting_lakehouse/gold/facts.py` | fact grain, and the allocated-measure trap |
| 7 | `src/betting_lakehouse/silver.py` | typing, deduplication, ANSI-safe parsing |
| 8 | `notebooks/` | the same story as runnable Databricks notebooks |
| 9 | `sql/10_analytics_questions.sql` | twelve questions a wagering business actually asks |

---

## Layout

```
betting_lakehouse/
├── conf/pipeline.yml            all paths, names, dates and volumes
├── run_pipeline.py              local orchestrator (Databricks uses Workflows)
├── src/betting_lakehouse/
│   ├── config.py                one place that knows local vs Databricks
│   ├── spark.py                 what a Databricks cluster configures for you
│   ├── io_utils.py              the 4 write patterns: replace_batch / merge /
│   │                            insert_new_only / append
│   ├── generate_source_data.py  synthetic AU wagering data, with planted defects
│   ├── bronze.py                schema-on-read ingest + Auto Loader equivalent
│   ├── silver.py                cleanse, dedupe, conform, quarantine
│   ├── dq/expectations.py       warn / drop / fail + quarantine table
│   ├── vault/dv_helpers.py      hash_key, hash_diff, load_hub/link/satellite
│   ├── vault/raw_vault.py       the wagering vault model
│   ├── gold/dimensions.py       SCD2, unknown members, surrogate keys
│   ├── gold/facts.py            two grains, allocated measures, as-at joins
│   ├── gold/metrics.py          turnover, GGR, hold %, margin — defined once
│   └── cli.py                   one entry point per layer, for wheel tasks
├── notebooks/                   5 Databricks source-format notebooks
├── dlt/betting_dlt_pipeline.py  the declarative alternative, and its trade-offs
├── databricks.yml               Asset Bundle: job DAG, clusters, schedule, targets
├── sql/                         Unity Catalog setup + analytics questions
├── tests/                       48 pytest tests over a temp lakehouse
└── docs/                        the four primers
```

---

## The ideas worth taking away

**Bronze does not fix anything.** Every column is a string; `"$1,250.00"` is stored
as `"$1,250.00"`. That looks wrong until you need to rebuild silver eighteen months
later, when the source system has changed schema or been switched off.

**Idempotency is a design property, not a habit.** Bronze replaces a batch instead
of appending it. Vault hubs and links anti-join on the hash key. Satellites compare
a `hash_diff`. Because of that, every task in `databricks.yml` can carry automatic
retries, and re-running yesterday's job is a no-op instead of an incident.

**Data Vault and Kimball are not competitors.** The vault is optimised for loading
and auditing — insert-only, hash keys computed independently per source, no business
rules. Nobody can query it. So you build stars on top where turnover by sport is one
join, and put every business rule there. The marts are disposable; the vault is not.
The join between them is smaller than people expect: an insert-only satellite plus
one `lead()` window *is* a type 2 dimension.

**Grain is the decision that matters.** Stake belongs to the bet slip, sport belongs
to the leg. Put stake on the leg fact and sum it, and a 4-leg $10 multi reports $40
of turnover. Hence two facts, an allocated measure, and a test asserting they
reconcile to the cent.

**Never drop a fact row because a dimension is missing.** 142 legs were placed on
fixtures the warehouse had not received. They loaded against the `-1` unknown
member, turnover stayed correct, and the mart rebuild after batch 2 attached them to
their real fixtures with no backfill.

**Spark 4 turns ANSI mode on.** `CAST('garbage' AS INT)` now raises instead of
returning NULL, so one malformed row aborts a five-million-row batch. Parse with
`try_cast` / `try_to_timestamp` / `try_divide`, and pair each with a quality rule so
the bad row is named and quarantined rather than crashing the job or vanishing.

**Timezones are a business decision.** Reports are cut in Australia/Melbourne, not
UTC. An off-by-one moves a Saturday's AFL turnover into Friday.

---

## Notes on the data

Entirely synthetic and generated from a fixed seed, so every run is identical — which
is what makes the idempotency tests meaningful. Odds are derived from simulated
probabilities with a 6.5% overround, so the realised hold in the marts converges
towards a plausible book margin rather than being made up.

Defects are planted on purpose, each fixed at a specific layer:

| defect | fixed in |
|---|---|
| duplicate rows (at-least-once delivery) | silver: `row_number()` dedup |
| money as `"$1,250.00"` | silver: `try_cast` to decimal |
| `dd/mm/yyyy` vs ISO vs space-separated dates | silver: multi-format `try_to_timestamp` |
| padded / mixed-case business keys | silver: trim + upper before hashing |
| zero stakes | silver: DQ drop → `dq.quarantine` |
| under-18 accounts | silver: DQ warn (kept — dropping would orphan their bets) |
| selection ids in no pricing feed | gold: unknown member |
| fixtures arriving after the bets | gold: unknown member, then resolved on rebuild |
| settlements out of order and re-graded | silver: latest-wins dedup |

---

## Requirements

Python 3.10+, Java 17 or 21, ~1GB disk. `make setup` handles the rest. Delta Lake
jars are fetched from Maven on first run; if that is blocked, set
`table_format: parquet` in `conf/pipeline.yml` and everything still runs (`MERGE`
degrades to an anti-join rewrite, which is instructive in itself).

Nothing here connects to a real Databricks workspace. `databricks.yml`,
`notebooks/`, `dlt/` and `sql/00_unity_catalog_setup.sql` are the deployment story —
readable, and deployable if you point them at a workspace.
