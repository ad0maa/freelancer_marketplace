# Data Vault and dimensional modelling — why this repo has both

Two modelling styles, and they are not competitors. They solve different problems,
and mature platforms run both: a Data Vault as the system of record, dimensional
marts as the presentation layer. That is what a job description saying *"data
modelling, using data vault and dimensional modelling"* is describing.

---

## The short version

| | Data Vault 2.0 | Dimensional (Kimball) |
|---|---|---|
| optimised for | **loading and auditing** | **querying by people** |
| business rules | none — raw source truth | all of them |
| structure | hubs, links, satellites | facts, dimensions |
| write pattern | insert-only, never updated | rebuilt from the vault |
| history | inherent — every version kept | only where modelled (SCD2) |
| adding a source | add satellites; nothing else changes | remodel the affected star |
| "turnover by sport last Saturday" | 6 joins + 2 windows | 1 join |
| who queries it | pipelines and auditors | analysts and BI tools |

Vault answers *"what did each system tell us, and when"*. Star answers *"what is
the number"*.

---

## Data Vault 2.0

Three table types, and only three.

### Hub — the business keys that exist

```
hub_customer
  hk_customer      SHA-256 of the business key
  customer_id      the business key itself
  load_date        when we first saw it
  record_source    which system told us
```

One row per real-world thing, ever. No attributes, so nothing ever needs updating.

**Hubs load from every source that carries the key**, not just the source that
"owns" the entity. In this repo `hub_customer` loads from the CRM extract *and*
from bets, so a bet is never orphaned by extract timing. `hub_selection` loads from
the pricing feed *and* from bet legs — so a selection id that appears on a bet but
was never priced still exists in the hub with no satellite row. That is exactly the
truth: we know the key exists, we know nothing about it.

### Link — that keys are related

```
link_bet_selection
  hk_bet_selection   hash of (bet_id, selection_id, leg_number)
  hk_bet             -> hub_bet
  hk_selection       -> hub_selection
  leg_number         dependent child key
  load_date, record_source
```

Links are always many-to-many, even when today's source system says
one-to-many — which is the point. When the business starts allowing something new,
the link already supports it without a migration.

**Dependent child keys** are the detail that catches people. `leg_number` is part
of the relationship's identity but is not a business key on its own. Leave it out
of the hash and a bet that includes the same selection on two legs collapses to one
row, and a leg silently disappears from turnover. This is the most common Data
Vault modelling mistake, which is why `tests/test_dv_helpers.py` asserts both
behaviours side by side.

### Satellite — the descriptive attributes, with history

```
sat_customer_details
  hk_customer      -> hub_customer
  load_date        when this version started
  hash_diff        SHA-256 of the payload, for change detection
  record_source
  state_code, account_status, vip_tier, is_self_excluded, ...
```

A new row every time the payload changes; existing rows are never touched.

**Split satellites by source system and by rate of change**, never by convenience.
`sat_bet_details` and `sat_bet_settlement` both hang off `hub_bet` but are separate
tables, because a bet's details never change after placement while its settlement
can be re-graded hours later. In one satellite, every settlement would rewrite the
stake and the odds too.

**Satellites can hang off links.** `sat_bet_leg_odds` holds the price a customer
actually took, which is an attribute of the bet-to-selection *relationship*, not of
either one alone — the same selection is taken at different prices all day.

**Satellites store no end date.** Only `load_date`, the moment a version started.
Writing an end date would mean updating the previous row, which breaks the
insert-only guarantee. End dates are derived at read time with `lead()` — see
`satellite_scd2` in `vault/dv_helpers.py`.

### Why hash keys instead of sequence numbers

Every job, on every source, can compute a hash independently and in parallel with
no lookup. A sequence needs a central allocator, which serialises your loads and
becomes the bottleneck the moment two sources feed one hub.

The cost is real: 64 hex characters is wide, and the value is meaningless to read.

Two rules that are not optional:

1. **Normalise before hashing** — trim, upper-case, and use a token for NULL.
   `" c000123 "` and `"C000123"` are the same person; hash them unnormalised and
   you get two hubs, two satellites and two rows in every report. Because the hash
   is opaque, nobody notices until someone asks why the customer count rose 4%.
2. **Never change the recipe.** Change the delimiter, the null token or the case
   rule and every hash key in the vault changes; the whole model needs reloading.
   `tests/test_dv_helpers.py` pins the hash against a literal so that becomes a
   failing build.

### What the vault buys you

**Auditability by construction.** Nothing is updated or deleted, so "what did we
know about this customer's self-exclusion status on the 14th" is a query. For a
licensed bookmaker that is not a nice-to-have.

**Parallel, order-independent loads.** No hub reads a link, no link reads a
satellite. They load concurrently, in any order — which is why `databricks.yml`
fans them out as three tasks.

**Replayability.** Loading the same batch twice inserts nothing, so every task can
carry automatic retries.

**Source additions are additive.** A new source that describes customers becomes a
new satellite on `hub_customer`. Nothing existing is remodelled, and no history is
rewritten.

### What it costs you

**Table count explodes.** Seven source tables became sixteen vault tables here. At
real scale it is hundreds.

**It is unqueryable by humans.** Reassembling one bet needs a hub, a link and two
satellites, each with a "latest version" window. No analyst will do that correctly
every time.

**It is not free.** Every load hashes, anti-joins and compares. On a wide satellite
that is real compute.

Which is why you do not stop here.

---

## Dimensional modelling (Kimball)

A **fact** table of measurements, surrounded by **dimensions** of descriptive
attributes. One join from fact to dimension, and that is the entire model.

```
        dim_date ─┐
    dim_customer ─┤
       dim_event ─┼── fact_bet_leg
   dim_selection ─┤
     dim_channel ─┤
    dim_bet_type ─┘
```

### Grain is the first decision, and the one that matters most

**Grain is what one row means.** Get it wrong and every number is wrong.

This repo has two facts because betting has two grains:

- `fact_bet_leg` — one row per leg. Sport, event and selection live here, because
  they only make sense per leg: a four-leg multi across AFL, NRL and two races has
  no single sport.
- `fact_bet_settlement` — one row per bet slip. Stake and payout live here, because
  they belong to the slip.

**Never mix them.** Put stake on the leg fact and sum it, and turnover is
multiplied by the number of legs — a 4-leg multi with a $10 stake reports $40. This
is the most common way a betting warehouse produces numbers finance refuses to
sign off, and it always looks plausible until someone checks the total.

### Allocated measures

The leg fact still needs a stake-shaped measure for "turnover by sport". The Kimball
answer is to **allocate**: `stake_allocated = stake / legs_in_bet`. Allocated
measures are additive by construction — they sum back to the true total across any
grouping — and the name makes clear they are not the real stake.

The corresponding test is the most valuable one in the repo: sum
`stake_allocated` over every leg and it must equal the sum of `stake_amount` over
every slip, to the cent.

### Slowly changing dimensions

**Type 1** — overwrite. Use it when history has no business value. `dim_event` is
type 1: nobody asks what a fixture's venue was before it was rescheduled. Choosing
type 1 deliberately is a real decision — an unnecessary type 2 doubles the row
count and forces an as-at predicate onto every join.

**Type 2** — a new row per change, with `effective_from` / `effective_to` /
`is_current`. `dim_customer` is type 2, and the reason is concrete: a customer who
lived in NSW in June and moved to VIC in July has two rows. Join a June bet to the
*current* row and it is reported as VIC turnover, and the NSW jurisdictional return
is wrong. Join on the effective range and it is attributed to NSW, where it
happened.

```sql
FROM fact f
JOIN dim_customer d
  ON  f.customer_dk = d.customer_dk
  AND f.placed_at BETWEEN d.effective_from AND d.effective_to
```

**This is where the two models meet.** An insert-only satellite plus one `lead()`
window *is* a type 2 dimension. `build_dim_customer` is a thin wrapper around
`satellite_scd2`.

### Surrogate keys, durable keys, unknown members

**Surrogate key** (`customer_sk`) identifies one *version* of a customer. This repo
derives it as `abs(xxhash64(hk_customer, effective_from))` — deterministic, so a
full rebuild does not renumber the warehouse and yesterday's extract still joins.
Identity columns are more compact but renumber on rebuild.

**Durable key** (`customer_dk`) is stable across all versions. Group by this when
you want "this customer" regardless of which version a bet attached to.

**Unknown member** — every dimension has a `-1` row. When a fact's dimension key
cannot be resolved, the fact still loads, at the right grain, with its measures
intact, pointing at `-1`. Dropping the fact makes the warehouse quietly disagree
with the source system, and a row labelled "Unknown" that somebody can investigate
is far better than a total that is silently short.

Note the deliberate asymmetry in `build_fact_bet_leg`: a missing *dimension* keeps
the fact on `-1`; a missing *bet* (one quarantined at silver for a zero stake) drops
the leg. A bet that was rejected is not turnover.

### Denormalise dimensions on purpose

`dim_selection` carries market type, market name and the parent event id, rather
than making the fact join a separate `dim_market`. Kimball's advice is to prefer a
few wide dimensions over many narrow ones: every extra join is a chance for an
analyst to get it wrong and a cost on every query. The vault keeps them properly
separated, so nothing is lost — this is a presentation choice, made once.

---

## How they fit together here

```
sources -> bronze -> silver -> RAW VAULT -> GOLD MARTS -> dashboards
                               (never          (rebuilt
                                rebuilt)        every run)
```

The vault is the system of record: insert-only, auditable, no business rules,
never rebuilt. The marts are disposable: full of business rules, denormalised for
speed, and safe to drop and rebuild whenever a definition changes.

That asymmetry is the whole point. When the business redefines "active customer",
you change one expression in the mart and rebuild — and the history is still intact
in the vault, because the vault never had an opinion about what "active" meant.

### Where business rules go

Deliberately **not** in the raw vault:

- age bands → `dim_customer` (redefine them next year without reloading history)
- back-dating version 1 to 1900 → `dim_customer` (the vault's `load_date` is when
  we *learned*, not when it became *true*)
- turnover, GGR, hold % → `gold/metrics.py`
- channel groupings → `dim_channel`

Anything in the raw vault is what a source system said, verbatim. The moment you
apply a rule in there, you can no longer rebuild history when the rule changes —
and business rules always change.

### Things a full implementation adds

This repo stops at a raw vault and information marts, which is the core. A larger
platform typically adds:

- **Business vault** — computed satellites for expensive derivations shared by many
  marts, so they are calculated once rather than per mart.
- **PIT (point-in-time) tables** — pre-joined snapshots of several satellites at
  common timestamps, so a mart build does not window five satellites at once. This
  is the standard fix when vault query performance becomes the bottleneck.
- **Bridge tables** — pre-joined hub/link paths, for the same reason.
- **Reference tables** — small code/description lookups that do not warrant a hub.
- **Effectivity satellites** — for links whose relationship itself starts and ends
  (a customer's account manager, say).

---

## Answering "why both?" in an interview

> Because they optimise for different things and neither one does the other's job.
>
> The vault is built for loading and auditing: insert-only, hash keys computed
> independently per source so loads run in parallel, and no business rules, so it
> stays a literal record of what each system said. For a licensed operator that
> matters — you have to be able to show what you knew and when.
>
> But nobody can query it. Reassembling a single bet is a hub, a link and two
> satellites with a latest-version window on each. So you build star schemas on top,
> where turnover by sport is one join, and you put all the business rules there.
> Because the marts are derived, they are disposable: when a definition changes you
> rebuild them, and the history is untouched because the vault never encoded the
> definition in the first place.
>
> The join between the two is smaller than people expect: an insert-only satellite
> plus a `lead()` window is already a type 2 dimension.
