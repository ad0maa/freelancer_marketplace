# The betting domain, for a data engineer

You do not need to be a punter to build this, but you do need the vocabulary — most
of it turns up in column names, and a few of the concepts change how you model.
Australian wagering specifics are flagged.

---

## Odds

**Decimal odds** — the Australian and European convention, and what this repo uses.
`$10 at 2.50` returns `$25` **including** your stake, so the profit is `$15`.

```
payout = stake × decimal_odds        (winning bet)
profit = stake × (decimal_odds − 1)
```

**Implied probability** = `1 / decimal_odds`. Odds of 2.50 imply a 40% chance.

**Fractional odds** (`3/1`) are UK convention, **moneyline** (`+300` / `-150`) is
US. You will see both in APIs; convert to decimal at the boundary and store one
form.

**Overround / theoretical margin** — the bookmaker's edge, priced in. Sum the
implied probabilities across every selection in a market and you get more than
100%; the excess is the margin.

> A head-to-head priced at 1.90 / 1.95 implies 52.6% + 51.3% = 103.9%.
> The theoretical margin is about 3.8%.

This is the single most useful concept for understanding the data model: it is why
`silver.transform_selections` derives `implied_probability`, and every "margin"
number in the gold layer traces back to it.

**Price movement** — odds change constantly as money comes in and information
changes. A bet is settled at **the odds the customer took**, not the current price,
which is why `sat_bet_leg_odds` hangs off the bet-to-selection link rather than off
the selection.

**SP (starting price)** vs **fixed odds** — in racing you can take a fixed price
now, or the price at jump time. SP bets have no known odds until the race starts,
which matters if you assume every leg has a price at placement.

---

## Bet types

**Single** — one selection. About 70% of slips, less of the turnover.

**Multi (parlay/accumulator)** — several selections, all must win, odds
**multiply**. `2.0 × 3.0 × 4.0 = 24.0`. Margin compounds too, which is why multis
hold far more than singles and get marketed hard.

**SGM (same game multi)** — a multi whose legs are all in one event
("Collingwood to win *and* over 180.5 points"). Legs are correlated, so pricing them
is genuinely hard; commercially it is one of the most important products in the
Australian market.

**Each way** — in racing, two bets: one for the win, one for a place.

**Cash out** — settle a bet early for an offered amount, before the event finishes.
It is a settlement status, not a bet type, and it is why `settlement_status` has
four values rather than the obvious two. It also breaks the naive assumption that
payout is either zero or `stake × odds`.

**In-play / live** — placed after the event has started. The fastest-growing and
highest-risk product: prices move in seconds, so latency and pricing errors both
cost real money.

---

## Markets

**Market** — a question with mutually exclusive answers. **Selection** (or
**runner**, or **outcome**) — one possible answer.

| market | selections |
|---|---|
| Head to Head | the two teams (three in soccer, with the Draw) |
| Line / handicap | favourite with a points start given, underdog receiving it |
| Total Points / Over-Under | Over 180.5, Under 180.5 |
| Win (racing) | every runner in the field |
| Place (racing) | every runner; pays if it finishes top 3 |
| First Try Scorer | every player |

Half-point lines (`180.5`) exist to make a draw impossible.

A **Place** market does not sum to 100% implied probability, because several
selections can win at once. If you validate "implied probabilities sum to roughly
1" as a data quality rule, exclude Place markets or the rule fires constantly.

---

## The money metrics

These have precise meanings, and getting them confused between teams is a
recurring, expensive source of disagreement. They are defined once in
`gold/metrics.py` so that every dashboard imports the same definition.

**Turnover** (or **handle**) — total staked. The volume metric. It counts money
wagered, not money lost: a customer betting $10 twenty times generates $200 of
turnover.

**Payout** — total returned to customers on settled bets, including returned stakes
on voids and cash-outs.

**Gross win** — `turnover − payouts`. Positive means the book won. Revenue *before*
promotional cost.

**GGR (gross gaming revenue)** — gross win net of bonuses and promotional cost.
This is what a regulator and the ASX care about, and what wagering taxes are levied
on.

**NGR (net gaming revenue)** — GGR after taxes and levies.

**Hold %** (or **win %**, or **actual margin**) — `gross win / turnover`. The
realised margin. Single digits, and volatile until a lot of bets have settled.

**Theoretical hold** — the overround. Hold should tend towards it as volume grows.

> **The gap between hold and theoretical is a genuine signal, and its direction
> tells you where to look.** Hold well *below* theoretical usually means sharp
> customers or stale prices. Well *above* usually means a settlement or
> void-handling bug — that is, a data problem.

**Average stake**, **bets per active customer**, **active customers** — the standard
engagement set. Watch the definitions: "active" almost never means "placed a bet
today", and whatever it does mean belongs in one place.

### The trap worth repeating

Revenue is extremely concentrated: a small share of customers generates most of the
turnover, which is why the generator in this repo draws customers from a Pareto
distribution rather than uniformly. Uniform customers make every per-customer
metric meaningless and every responsible-gambling query return nothing.

---

## Settlement

**Settlement** — grading a bet once the event finishes and paying out.

| status | meaning | payout |
|---|---|---|
| `WON` | all legs won | `stake × combined odds` |
| `LOST` | at least one leg lost | 0 |
| `VOID` | event abandoned, or a runner scratched | stake returned |
| `CASHED_OUT` | settled early at an offered price | the offered amount |

Three modelling consequences:

1. **Settlement arrives late, and out of order.** A bet placed Saturday may settle
   Sunday night. Your pipeline must handle a fact whose dimension rows already
   exist but whose settlement does not — hence the LEFT join in `facts._bet_core`
   and `settlement_status` defaulting to `PENDING`.
2. **Settlement can be revised.** A trader re-grades a bet hours later, so
   settlements need latest-wins deduplication, not first-wins.
3. **Void legs do not void the bet.** A multi with one void leg settles on the
   remaining legs at reduced odds.

---

## Australian specifics

**The operators** — Sportsbet (Flutter), Tabcorp/TAB, Ladbrokes and Neds
(Entain), bet365, PointsBet, Betr. Online **sports betting** is legal and
licensed; online casino is not.

**Regulation is per state**, and licensing is largely through the Northern
Territory. Turnover has to be attributable to the customer's jurisdiction **at the
time of the bet** — which is precisely why `dim_customer` is type 2 and the fact
carries an as-at surrogate key rather than the current one. A customer who moves
from NSW to VIC mid-year must have their June bets counted in NSW.

**Point of consumption tax** — each state taxes net wagering revenue from its own
residents (roughly 10-15%), which makes jurisdictional attribution a tax
calculation, not just a report.

**Responsible gambling obligations** are real and enforced:

- **Self-exclusion** (BetStop, the national register) — once a customer
  self-excludes, every downstream system must reflect it immediately, and history
  must still show what they were before. This is a strong argument for an
  insert-only vault and for type 2 dimensions.
- **Deposit limits** and mandatory pre-commitment tooling.
- **Activity statements** and mandatory messaging.
- **Verification** — identity must be verified within 72 hours of account opening.
- **No credit betting**, and heavy restrictions on inducements.
- **Under-18 accounts** are a licence condition breach, not a data quality nit —
  which is why `silver.EXPECTATIONS` flags them as a named expectation rather than
  filtering them out quietly.

**The sports** — AFL and NRL dominate, then thoroughbred racing (harness and
greyhounds too), soccer (A-League and EPL), cricket (BBL), basketball (NBL and
NBA), tennis. Turnover is extremely peaky: Saturday afternoons, the Melbourne Cup,
the AFL Grand Final. That peakiness is what makes date partitioning tempting and
usually wrong, and what makes join skew a real concern.

**Timezone matters commercially.** A wagering day is a local business day. Reports
are cut in Australia/Melbourne, not UTC, and an off-by-one timezone bug moves a
Saturday's AFL turnover into Friday. Hence `spark.sql.session.timeZone` being set
explicitly in `spark.py` rather than left at the default.

---

## Job-description vocabulary

**Trading / pricing** — the team that sets odds and manages risk. Your most
demanding internal customer, and the one that notices a broken number first.

**Risk / liability** — potential exposure if particular outcomes land. Near
real-time, and usually its own system rather than the warehouse.

**Sharp** — a consistently profitable customer. Identifying them is a real
modelling problem.

**Bonus / free bet / promo** — the reason GGR differs from gross win, and the
reason `promo_code` exists on the bet.

**Turnover requirement / rollover** — how much of a bonus must be wagered before it
converts to cash.

**Bet slip** — the transaction a customer submits; one slip, one or many legs. The
distinction between a slip and a leg is exactly the two-fact grain split in this
repo, and it is the thing most worth understanding before an interview.
