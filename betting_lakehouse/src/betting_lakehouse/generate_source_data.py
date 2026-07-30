"""Generate synthetic source-system extracts for an Australian wagering operator.

This stands in for the systems a real data engineer at a bookmaker actually pulls
from: a CRM database, the betting engine, the pricing service, the payments
platform. Nothing here is scraped or real - it is all generated from a fixed
seed, so every run produces identical data and the idempotency tests mean
something.

Two batches are produced, because a pipeline that has only ever seen one batch
is not a pipeline:

    batch 1  the initial load - customers, fixtures, prices, three weeks of bets
    batch 2  the next day's incremental - changed customers (CDC style), new
             bets, price updates, settlements landing late for batch 1 bets, and
             three fixtures that arrive *after* bets were already placed on them

THE DIRT (deliberate, and each defect is fixed at a specific layer)
-------------------------------------------------------------------
=================================  ============  =============================
defect                             source        fixed in
=================================  ============  =============================
duplicate rows (at-least-once CDC) bets, custs   silver: dedup window
money as "$1,250.00" strings       bets          silver: cast to decimal
dd/mm/yyyy vs ISO dates            customers     silver: multi-format parse
mixed case / padded business keys  all           silver: trim + upper
zero and negative stakes           bets          silver: DQ drop -> quarantine
selection_id that never exists     bet_legs      gold: unknown member (-1)
fixtures arriving after the bets   events        gold: late-arriving dimension
settlements arriving out of order  settlements   silver: latest-wins dedup
blank postcode / suburb            customers     silver: null standardisation
=================================  ============  =============================
"""

from __future__ import annotations

import csv
import json
import random
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config

# --------------------------------------------------------------------- reference

SPORTS = [
    ("SP01", "Australian Rules Football", "AFL"),
    ("SP02", "Rugby League", "NRL"),
    ("SP03", "Horse Racing", "RACING"),
    ("SP04", "Soccer", "SOCCER"),
    ("SP05", "Basketball", "BASKETBALL"),
    ("SP06", "Tennis", "TENNIS"),
]

COMPETITIONS = {
    "SP01": [("CP01", "AFL Premiership Season")],
    "SP02": [("CP02", "NRL Telstra Premiership")],
    "SP03": [("CP03", "Metropolitan Thoroughbred"), ("CP04", "Country Thoroughbred")],
    "SP04": [("CP05", "A-League Men"), ("CP06", "English Premier League")],
    "SP05": [("CP07", "NBL"), ("CP08", "NBA")],
    "SP06": [("CP09", "ATP Tour")],
}

TEAMS = {
    "CP01": [
        "Collingwood", "Carlton", "Richmond", "Essendon", "Geelong Cats",
        "Sydney Swans", "Brisbane Lions", "West Coast Eagles", "Adelaide Crows",
        "Port Adelaide", "Hawthorn", "Melbourne", "St Kilda", "Western Bulldogs",
        "Fremantle", "North Melbourne", "GWS Giants", "Gold Coast Suns",
    ],
    "CP02": [
        "Brisbane Broncos", "Melbourne Storm", "Penrith Panthers",
        "South Sydney Rabbitohs", "Sydney Roosters", "Manly Sea Eagles",
        "Parramatta Eels", "Newcastle Knights", "Cronulla Sharks",
        "North Queensland Cowboys", "Canberra Raiders", "St George Dragons",
        "Gold Coast Titans", "New Zealand Warriors", "Canterbury Bulldogs",
        "Wests Tigers",
    ],
    "CP05": [
        "Melbourne Victory", "Sydney FC", "Western Sydney Wanderers",
        "Adelaide United", "Brisbane Roar", "Perth Glory", "Central Coast Mariners",
        "Macarthur FC",
    ],
    "CP06": [
        "Arsenal", "Liverpool", "Manchester City", "Chelsea", "Tottenham",
        "Manchester United", "Newcastle United", "Aston Villa", "Brighton",
        "Everton",
    ],
    "CP07": [
        "Sydney Kings", "Melbourne United", "Perth Wildcats", "Tasmania JackJumpers",
        "Brisbane Bullets", "Adelaide 36ers", "Illawarra Hawks", "Cairns Taipans",
    ],
    "CP08": [
        "Boston Celtics", "Denver Nuggets", "Golden State Warriors", "Miami Heat",
        "Milwaukee Bucks", "Phoenix Suns", "LA Lakers", "New York Knicks",
    ],
    "CP09": [
        "A. Kokkinakis", "J. Sinner", "C. Alcaraz", "A. de Minaur", "N. Djokovic",
        "T. Fritz", "D. Medvedev", "H. Rune",
    ],
}

RACE_VENUES = {
    "CP03": ["Flemington", "Randwick", "Caulfield", "Moonee Valley", "Eagle Farm"],
    "CP04": ["Ballarat", "Bendigo", "Wagga", "Toowoomba", "Kalgoorlie"],
}

HORSE_PREFIX = [
    "Bold", "Northern", "Lucky", "Midnight", "Golden", "Silent", "Rapid", "Royal",
    "Coastal", "Iron", "Velvet", "Thunder", "Autumn", "Brazen", "Stellar",
]
HORSE_SUFFIX = [
    "Ambition", "Empress", "Dancer", "Comet", "Legend", "Whisper", "Warrior",
    "Sonnet", "Runner", "Miracle", "Charm", "Voyage", "Spirit", "Rebel", "Tempo",
]

STATES = ["VIC", "NSW", "QLD", "WA", "SA", "TAS", "ACT", "NT"]
STATE_WEIGHTS = [26, 32, 20, 10, 7, 2, 2, 1]
SUBURBS = {
    "VIC": ["Richmond", "Brunswick", "Geelong", "Ballarat", "Frankston"],
    "NSW": ["Newtown", "Parramatta", "Newcastle", "Wollongong", "Bondi"],
    "QLD": ["Fortitude Valley", "Southport", "Cairns", "Toowoomba", "Ipswich"],
    "WA": ["Fremantle", "Joondalup", "Bunbury", "Midland", "Scarborough"],
    "SA": ["Glenelg", "Norwood", "Elizabeth", "Port Adelaide", "Gawler"],
    "TAS": ["Hobart", "Launceston", "Devonport", "Burnie", "Kingston"],
    "ACT": ["Belconnen", "Gungahlin", "Woden", "Tuggeranong", "Braddon"],
    "NT": ["Darwin", "Palmerston", "Alice Springs", "Katherine", "Nhulunbuy"],
}
FIRST_NAMES = [
    "Adam", "Benjamin", "Chloe", "Daniel", "Ella", "Finn", "Grace", "Harry",
    "Isla", "Jack", "Kate", "Liam", "Mia", "Noah", "Olivia", "Patrick", "Ruby",
    "Sam", "Tara", "Will", "Zoe", "Mitchell", "Priya", "Wei", "Tane", "Sione",
]
LAST_NAMES = [
    "Nguyen", "Smith", "Tunchay", "Kelly", "Patel", "OBrien", "Zhang", "Wilson",
    "Mourinho", "Papadopoulos", "Singh", "Taylor", "Ryan", "Le", "Hausia",
    "Ahmed", "Brown", "Dimitriou", "Walker", "Fischer",
]

CHANNELS = ["IOS_APP", "ANDROID_APP", "WEB_DESKTOP", "WEB_MOBILE", "RETAIL"]
CHANNEL_WEIGHTS = [38, 30, 14, 15, 3]
VIP_TIERS = ["BRONZE", "SILVER", "GOLD", "PLATINUM"]
VIP_WEIGHTS = [70, 20, 8, 2]

MARKET_TYPES = {
    "AFL": ["HEAD_TO_HEAD", "LINE", "TOTAL_POINTS"],
    "NRL": ["HEAD_TO_HEAD", "LINE", "TOTAL_POINTS"],
    "BASKETBALL": ["HEAD_TO_HEAD", "LINE", "TOTAL_POINTS"],
    "SOCCER": ["HEAD_TO_HEAD_3WAY", "TOTAL_GOALS"],
    "TENNIS": ["HEAD_TO_HEAD"],
    "RACING": ["WIN", "PLACE"],
}


# ----------------------------------------------------------------- world model


@dataclass
class World:
    """Everything that "happened", before it is split into landing batches."""

    customers: list[dict] = field(default_factory=list)
    customer_changes: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    markets: list[dict] = field(default_factory=list)
    selections: list[dict] = field(default_factory=list)
    price_updates: list[dict] = field(default_factory=list)
    bets: list[dict] = field(default_factory=list)
    bet_legs: list[dict] = field(default_factory=list)
    settlements: list[dict] = field(default_factory=list)
    payments: list[dict] = field(default_factory=list)
    late_event_ids: set[str] = field(default_factory=set)


def _money(value: float, dirty: bool = False) -> str:
    """Money as the source systems emit it: a formatted string, not a number.

    This is not a strawman. Front-end and API extracts hand you "$1,250.00" all
    the time, and if you let Spark infer the schema you get a string column that
    silently sorts "$9.00" after "$1,250.00".
    """
    if dirty:
        return f"${value:,.2f}"
    return f"{value:.2f}"


def _odds_from_prob(prob: float, overround: float) -> float:
    """Decimal odds including the bookmaker's margin.

    Fair odds are 1/probability. Multiplying the probability by the overround
    before inverting is what builds in the margin: with an overround of 1.065 the
    implied probabilities across a market sum to ~106.5%, and that 6.5% is the
    theoretical hold. Everything the gold layer reports as "margin" traces back
    to this line.
    """
    return max(1.01, round(1.0 / (prob * overround), 2))


def _build_world(cfg: Config) -> World:
    gen = cfg.generator
    rng = random.Random(gen["seed"])
    overround = gen["overround"]
    w = World()

    start, end = cfg.start_date, cfg.end_date
    span_days = (end - start).days
    # Batch 1 covers the first three quarters of the window; batch 2 is "today".
    batch1_cutoff = start + timedelta(days=int(span_days * 0.75))

    # ---------------------------------------------------------- customers
    for i in range(1, gen["customers"] + 1):
        state = rng.choices(STATES, STATE_WEIGHTS)[0]
        signup = start - timedelta(days=rng.randint(30, 2200))
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        blank_address = rng.random() < 0.04
        w.customers.append(
            {
                "customer_id": f"C{i:06d}",
                # Mixed case and stray whitespace on a business key: the thing
                # that makes " c000123 " and "C000123" two different customers
                # if silver does not standardise it.
                "first_name": f"  {first}" if rng.random() < 0.05 else first,
                "last_name": last,
                "email": f"{first}.{last}{i}@example.com".lower()
                if rng.random() > 0.1
                else f"{first}.{last}{i}@EXAMPLE.COM",
                "birth_date": (
                    date(rng.randint(1955, 2006), rng.randint(1, 12), rng.randint(1, 28))
                ).isoformat(),
                # dd/mm/yyyy - Australian format, and unparseable by the default
                # Spark date cast, which returns NULL rather than failing loudly.
                "signup_date": signup.strftime("%d/%m/%Y"),
                "state": state if rng.random() > 0.08 else state.lower(),
                "suburb": "" if blank_address else rng.choice(SUBURBS[state]),
                "postcode": "" if blank_address else str(rng.randint(2000, 7999)),
                "account_status": rng.choices(
                    ["ACTIVE", "DORMANT", "SUSPENDED", "CLOSED"], [88, 7, 3, 2]
                )[0],
                "verification_status": rng.choices(["VERIFIED", "PENDING"], [94, 6])[0],
                "self_excluded_flag": "N",
                "deposit_limit_weekly": _money(rng.choice([0, 100, 250, 500, 1000, 2500])),
                "marketing_opt_in": rng.choice(["Y", "N"]),
                "vip_tier": rng.choices(VIP_TIERS, VIP_WEIGHTS)[0],
                "updated_at": datetime.combine(
                    batch1_cutoff, datetime.min.time()
                ).isoformat(timespec="seconds"),
            }
        )

    # Three accounts with a birth date that makes them under 18. In this
    # industry that is not a data quality nit, it is a licence condition, so the
    # pipeline has to surface it rather than quietly average over it.
    for customer in rng.sample(w.customers, min(3, len(w.customers))):
        customer["birth_date"] = date(2010, rng.randint(1, 12), rng.randint(1, 28)).isoformat()

    # ------------------------------------------- fixtures, markets, selections
    sport_by_comp = {c[0]: s[2] for s in SPORTS for c in COMPETITIONS[s[0]]}
    comp_ids = list(sport_by_comp.keys())
    event_seq, market_seq, selection_seq = 0, 0, 0
    # Selection probabilities are kept out of the warehouse - a bookmaker's true
    # model output is not something the data platform gets to see. It is used
    # here only to decide who actually won.
    true_prob: dict[str, float] = {}
    market_selections: dict[str, list[str]] = {}

    for _ in range(gen["events"]):
        event_seq += 1
        comp_id = rng.choice(comp_ids)
        sport_code = sport_by_comp[comp_id]
        event_id = f"E{event_seq:05d}"
        event_day = start + timedelta(days=rng.randint(0, span_days))
        scheduled = datetime.combine(event_day, datetime.min.time()) + timedelta(
            hours=rng.choice([12, 14, 16, 18, 19, 20]), minutes=rng.choice([0, 10, 40])
        )

        if sport_code == "RACING":
            venue = rng.choice(RACE_VENUES[comp_id])
            race_no = rng.randint(1, 9)
            event_name = f"{venue} Race {race_no}"
            home_team = away_team = ""
        else:
            teams = rng.sample(TEAMS[comp_id], 2)
            home_team, away_team = teams
            event_name = f"{home_team} v {away_team}"
            venue = f"{home_team} Home Ground"

        w.events.append(
            {
                "event_id": event_id,
                "competition_id": comp_id,
                "event_name": event_name,
                "home_team": home_team,
                "away_team": away_team,
                "venue": venue,
                "scheduled_start": scheduled.isoformat(timespec="seconds"),
                "event_status": "COMPLETED" if event_day <= end else "SCHEDULED",
                "live_betting_enabled": "Y" if sport_code != "RACING" else "N",
            }
        )

        for market_type in MARKET_TYPES[sport_code]:
            market_seq += 1
            market_id = f"M{market_seq:06d}"
            names_probs: list[tuple[str, float]] = []

            if market_type in ("HEAD_TO_HEAD",):
                p = rng.uniform(0.25, 0.75)
                a = home_team or "Runner A"
                b = away_team or "Runner B"
                names_probs = [(a, p), (b, 1 - p)]
            elif market_type == "HEAD_TO_HEAD_3WAY":
                draw = rng.uniform(0.2, 0.3)
                p = rng.uniform(0.3, 0.7) * (1 - draw)
                names_probs = [(home_team, p), ("Draw", draw), (away_team, 1 - draw - p)]
            elif market_type == "LINE":
                handicap = rng.choice([6.5, 12.5, 18.5, 24.5])
                p = rng.uniform(0.4, 0.6)
                names_probs = [
                    (f"{home_team} -{handicap}", p),
                    (f"{away_team} +{handicap}", 1 - p),
                ]
            elif market_type == "TOTAL_POINTS":
                total = rng.choice([155.5, 168.5, 172.5, 180.5])
                p = rng.uniform(0.45, 0.55)
                names_probs = [(f"Over {total}", p), (f"Under {total}", 1 - p)]
            elif market_type == "TOTAL_GOALS":
                total = rng.choice([2.5, 3.5])
                p = rng.uniform(0.45, 0.6)
                names_probs = [(f"Over {total}", p), (f"Under {total}", 1 - p)]
            elif market_type in ("WIN", "PLACE"):
                field_size = rng.randint(7, 12)
                weights = [rng.uniform(0.5, 6.0) for _ in range(field_size)]
                total_w = sum(weights)
                runners = [
                    f"{rng.choice(HORSE_PREFIX)} {rng.choice(HORSE_SUFFIX)}"
                    for _ in range(field_size)
                ]
                names_probs = [(n, wt / total_w) for n, wt in zip(runners, weights)]
                if market_type == "PLACE":
                    # A place bet pays for top 3, so the chance is much higher and
                    # the odds much shorter - the market does not sum to 1.
                    names_probs = [(n, min(0.93, p * 2.8)) for n, p in names_probs]

            w.markets.append(
                {
                    "market_id": market_id,
                    "event_id": event_id,
                    "market_type": market_type,
                    "market_name": market_type.replace("_", " ").title(),
                    "market_status": "SETTLED" if event_day <= end else "OPEN",
                }
            )

            ids = []
            for name, prob in names_probs:
                selection_seq += 1
                selection_id = f"S{selection_seq:07d}"
                ids.append(selection_id)
                true_prob[selection_id] = prob
                w.selections.append(
                    {
                        "selection_id": selection_id,
                        "market_id": market_id,
                        "selection_name": name,
                        "runner_number": len(ids) if market_type in ("WIN", "PLACE") else None,
                        "decimal_odds": _odds_from_prob(prob, overround),
                        "selection_status": "OPEN",
                        "price_updated_at": scheduled.isoformat(timespec="seconds"),
                    }
                )
            market_selections[market_id] = ids

    # Three fixtures whose reference data lands a day AFTER the bets on them.
    # This is the late-arriving dimension problem, and it is not hypothetical:
    # trading loads a fixture into the pricing system before the fixtures feed
    # syncs, and bets are taken in between.
    w.late_event_ids = {e["event_id"] for e in w.events[-3:]}

    # ------------------------------------------------------------------- bets
    market_ids = list(market_selections.keys())
    event_of_market = {m["market_id"]: m["event_id"] for m in w.markets}
    event_day_of = {
        e["event_id"]: datetime.fromisoformat(e["scheduled_start"]).date() for e in w.events
    }
    odds_of = {s["selection_id"]: s["decimal_odds"] for s in w.selections}

    total_bets = gen["bets"] + gen["batch2_bets"]
    # Betting is heavily skewed: a small share of customers place most of the
    # bets. Uniform random customers would make every per-customer metric
    # meaningless, and the responsible-gambling queries would find nothing.
    weights = [rng.paretovariate(1.4) for _ in w.customers]
    for i in range(1, total_bets + 1):
        bet_id = f"B{i:07d}"
        customer = rng.choices(w.customers, weights)[0]
        bet_type = rng.choices(["SINGLE", "MULTI", "SGM"], [72, 20, 8])[0]
        n_legs = 1 if bet_type == "SINGLE" else rng.randint(2, 4)

        if bet_type == "SGM":
            # Same game multi: every leg on one event.
            base_market = rng.choice(market_ids)
            event_id = event_of_market[base_market]
            candidate_markets = [m for m, e in event_of_market.items() if e == event_id]
            chosen_markets = rng.sample(
                candidate_markets, min(n_legs, len(candidate_markets))
            )
        else:
            chosen_markets = rng.sample(market_ids, n_legs)

        legs = []
        for leg_no, market_id in enumerate(chosen_markets, start=1):
            selection_id = rng.choice(market_selections[market_id])
            legs.append((leg_no, selection_id, odds_of[selection_id]))

        # Bets are placed some time before the earliest leg's event starts.
        earliest_event = min(event_day_of[event_of_market[m]] for m in chosen_markets)
        placed_day = earliest_event - timedelta(days=rng.choice([0, 0, 0, 1, 2, 3]))
        if placed_day < start:
            placed_day = start
        placed_at = datetime.combine(placed_day, datetime.min.time()) + timedelta(
            hours=rng.randint(7, 23), minutes=rng.randint(0, 59), seconds=rng.randint(0, 59)
        )

        stake = round(rng.choice([5, 10, 10, 20, 25, 50, 100, 250, 500]) * rng.uniform(0.6, 1.8), 2)
        if rng.random() < 0.001:
            stake = 0.0            # DQ: zero stake, must be quarantined
        combined_odds = round(_product(o for _, _, o in legs), 2)

        w.bets.append(
            {
                "bet_id": bet_id,
                "customer_id": customer["customer_id"],
                "bet_type": bet_type,
                "channel": rng.choices(CHANNELS, CHANNEL_WEIGHTS)[0],
                # Money as a formatted string, with thousands separators once it
                # gets big enough - the classic silver-layer cleanup job.
                "stake_amount": _money(stake, dirty=True),
                "combined_odds": combined_odds,
                "potential_payout": _money(round(stake * combined_odds, 2), dirty=True),
                "currency_code": "AUD",
                "placed_at": placed_at.isoformat(timespec="seconds") + "+10:00",
                "in_play_flag": "Y" if rng.random() < 0.22 else "N",
                "promo_code": rng.choice([None, None, None, "MULTI5", "CASHBACK10"]),
                "bet_status": "SETTLED",
            }
        )
        for leg_no, selection_id, leg_odds in legs:
            w.bet_legs.append(
                {
                    "leg_id": f"{bet_id}-{leg_no}",
                    "bet_id": bet_id,
                    "leg_number": leg_no,
                    "selection_id": selection_id,
                    "odds_taken": leg_odds,
                }
            )

    # A handful of legs point at a selection that does not exist anywhere. Real
    # cause: the leg was written by a service that had already been deployed with
    # a new market type. The mart must still balance, so these get the unknown
    # member rather than being dropped.
    for k, leg in enumerate(rng.sample(w.bet_legs, min(8, len(w.bet_legs)))):
        leg["selection_id"] = f"S999{k:04d}"

    # --------------------------------------------------------- settlements
    # Simulate the outcome of every market once, then derive leg results.
    winners: dict[str, set[str]] = {}
    for market_id, selection_ids in market_selections.items():
        market_type = next(m["market_type"] for m in w.markets if m["market_id"] == market_id)
        probs = [true_prob[s] for s in selection_ids]
        if market_type == "PLACE":
            # Top three place, so a market with three winners. Drawn weighted by
            # probability and without replacement, not uniformly: if longshots
            # placed as often as favourites, they would be paid at long odds far
            # too often and the book would post a negative margin on racing.
            remaining = list(zip(selection_ids, probs))
            placed: set[str] = set()
            for _ in range(min(3, len(remaining))):
                total = sum(p for _, p in remaining)
                pick = rng.choices(
                    [s for s, _ in remaining], [p / total for _, p in remaining]
                )[0]
                placed.add(pick)
                remaining = [(s, p) for s, p in remaining if s != pick]
            winners[market_id] = placed
        else:
            total = sum(probs)
            winners[market_id] = {rng.choices(selection_ids, [p / total for p in probs])[0]}

    market_of_selection = {
        s["selection_id"]: s["market_id"] for s in w.selections
    }
    legs_by_bet: dict[str, list[dict]] = {}
    for leg in w.bet_legs:
        legs_by_bet.setdefault(leg["bet_id"], []).append(leg)

    for bet in w.bets:
        legs = legs_by_bet[bet["bet_id"]]
        stake = float(bet["stake_amount"].replace("$", "").replace(",", ""))
        last_event_day = max(
            event_day_of[event_of_market[market_of_selection[leg["selection_id"]]]]
            for leg in legs
            if leg["selection_id"] in market_of_selection
        ) if any(l["selection_id"] in market_of_selection for l in legs) else end

        leg_results = []
        for leg in legs:
            market_id = market_of_selection.get(leg["selection_id"])
            if market_id is None:
                leg_results.append("VOID")     # orphan selection - void the leg
            elif leg["selection_id"] in winners[market_id]:
                leg_results.append("WON")
            else:
                leg_results.append("LOST")

        if "LOST" in leg_results:
            status, payout = "LOST", 0.0
        elif all(r == "VOID" for r in leg_results):
            status, payout = "VOID", stake
        elif rng.random() < 0.04:
            status = "CASHED_OUT"
            payout = round(stake * rng.uniform(0.4, 1.6), 2)
        else:
            status = "WON"
            live_odds = _product(
                leg["odds_taken"] for leg, r in zip(legs, leg_results) if r != "VOID"
            )
            payout = round(stake * live_odds, 2)

        settled_at = datetime.combine(last_event_day, datetime.min.time()) + timedelta(
            hours=rng.randint(20, 23), minutes=rng.randint(0, 59)
        )
        w.settlements.append(
            {
                "settlement_id": f"ST{bet['bet_id'][1:]}",
                "bet_id": bet["bet_id"],
                "settlement_status": status,
                "payout_amount": _money(payout),
                # A third date format, because why would three systems agree.
                "settled_at": settled_at.strftime("%Y-%m-%d %H:%M:%S"),
                "settled_by": rng.choice(["AUTO_SETTLEMENT", "AUTO_SETTLEMENT", "TRADER_MANUAL"]),
            }
        )

    # ----------------------------------------------------------- payments
    for i, customer in enumerate(w.customers, start=1):
        for j in range(rng.randint(1, 6)):
            is_deposit = rng.random() < 0.72
            amount = round(rng.choice([20, 50, 100, 200, 500]) * rng.uniform(0.8, 2.0), 2)
            txn_day = start + timedelta(days=rng.randint(0, span_days))
            w.payments.append(
                {
                    "payment_id": f"P{i:06d}{j:02d}",
                    "customer_id": customer["customer_id"],
                    "payment_type": "DEPOSIT" if is_deposit else "WITHDRAWAL",
                    "amount": _money(amount),
                    "payment_method": rng.choice(["CARD", "PAYID", "BANK_TRANSFER", "POLI"]),
                    "payment_status": rng.choices(["COMPLETED", "FAILED"], [96, 4])[0],
                    "created_at": (
                        datetime.combine(txn_day, datetime.min.time())
                        + timedelta(hours=rng.randint(6, 23))
                    ).isoformat(timespec="seconds"),
                }
            )

    # ------------------------------------------------- batch 2 CDC changes
    changed = rng.sample(w.customers, min(gen["batch2_customer_changes"], len(w.customers)))
    change_ts = datetime.combine(end, datetime.min.time()) + timedelta(hours=6)
    for customer in changed:
        updated = dict(customer)
        updated["updated_at"] = change_ts.isoformat(timespec="seconds")
        kind = rng.choice(["MOVED", "SELF_EXCLUDED", "TIER_UP", "SUSPENDED", "LIMIT"])
        if kind == "MOVED":
            new_state = rng.choice([s for s in STATES if s != customer["state"].upper()])
            updated["state"] = new_state
            updated["suburb"] = rng.choice(SUBURBS[new_state])
            updated["postcode"] = str(rng.randint(2000, 7999))
        elif kind == "SELF_EXCLUDED":
            # The change that matters most in this industry: once a customer
            # self-excludes, every downstream mart must reflect it immediately,
            # and history must still show what they were before.
            updated["self_excluded_flag"] = "Y"
            updated["account_status"] = "CLOSED"
            updated["marketing_opt_in"] = "N"
        elif kind == "TIER_UP":
            idx = min(VIP_TIERS.index(customer["vip_tier"]) + 1, len(VIP_TIERS) - 1)
            updated["vip_tier"] = VIP_TIERS[idx]
        elif kind == "SUSPENDED":
            updated["account_status"] = "SUSPENDED"
        else:
            updated["deposit_limit_weekly"] = _money(rng.choice([50, 100, 200]))
        w.customer_changes.append(updated)

    # Price moves for a sample of selections, so the price satellite has history.
    price_sample = min(gen.get("batch2_price_updates", 200), len(w.selections))
    for selection in rng.sample(w.selections, price_sample):
        drift = rng.uniform(0.75, 1.3)
        w.price_updates.append(
            {
                **selection,
                "decimal_odds": max(1.01, round(selection["decimal_odds"] * drift, 2)),
                "price_updated_at": (
                    datetime.combine(end, datetime.min.time()) + timedelta(hours=8)
                ).isoformat(timespec="seconds"),
            }
        )

    return w


def _product(values) -> float:
    result = 1.0
    for v in values:
        result *= v
    return result


# ------------------------------------------------------------------- batching


def _split(w: World, cfg: Config) -> dict[int, dict[str, list[dict]]]:
    """Split the world into two landing batches."""
    gen = cfg.generator
    batch1_bet_ids = {f"B{i:07d}" for i in range(1, gen["bets"] + 1)}

    late = w.late_event_ids
    late_markets = {m["market_id"] for m in w.markets if m["event_id"] in late}

    b1_events = [e for e in w.events if e["event_id"] not in late]
    b2_events = [e for e in w.events if e["event_id"] in late]
    b1_markets = [m for m in w.markets if m["market_id"] not in late_markets]
    b2_markets = [m for m in w.markets if m["market_id"] in late_markets]
    b1_selections = [s for s in w.selections if s["market_id"] not in late_markets]
    b2_selections = [s for s in w.selections if s["market_id"] in late_markets]

    b1_bets = [b for b in w.bets if b["bet_id"] in batch1_bet_ids]
    b2_bets = [b for b in w.bets if b["bet_id"] not in batch1_bet_ids]
    b1_legs = [l for l in w.bet_legs if l["bet_id"] in batch1_bet_ids]
    b2_legs = [l for l in w.bet_legs if l["bet_id"] not in batch1_bet_ids]

    # Settlements: only 70% of batch-1 bets are settled by the time batch 1 is
    # extracted. The other 30% land in batch 2 - late-arriving facts against
    # dimension rows that already exist.
    rng = random.Random(gen["seed"] + 99)
    b1_settlements, b2_settlements = [], []
    for s in w.settlements:
        in_b1 = s["bet_id"] in batch1_bet_ids
        if in_b1 and rng.random() < 0.7:
            b1_settlements.append(s)
        else:
            b2_settlements.append(s)
    # Out of order on purpose: never assume a file is sorted by event time.
    rng.shuffle(b1_settlements)
    rng.shuffle(b2_settlements)

    mid = len(w.payments) // 2
    batches = {
        1: {
            "customers": w.customers,
            "events": b1_events,
            "markets": b1_markets,
            "selections": b1_selections,
            "bets": b1_bets,
            "bet_legs": b1_legs,
            "settlements": b1_settlements,
            "payments": w.payments[:mid],
        },
        2: {
            # CDC style: batch 2 carries only the customers that changed.
            "customers": w.customer_changes,
            "events": b2_events,
            "markets": b2_markets,
            "selections": b2_selections + w.price_updates,
            "bets": b2_bets,
            "bet_legs": b2_legs,
            "settlements": b2_settlements,
            "payments": w.payments[mid:],
        },
    }

    # At-least-once delivery: duplicate a slice of rows in each batch. Every one
    # of these must be collapsed by the silver layer, or turnover is overstated.
    for batch_no, datasets in batches.items():
        dup_rng = random.Random(gen["seed"] + batch_no)
        for dataset in ("bets", "settlements", "customers"):
            rows = datasets[dataset]
            if len(rows) < 20:
                continue
            dupes = [dict(r) for r in dup_rng.sample(rows, max(1, len(rows) // 50))]
            datasets[dataset] = rows + dupes
            dup_rng.shuffle(datasets[dataset])

    return batches


# --------------------------------------------------------------------- writing

# CSV for the systems that hand you a nightly file drop, JSON Lines for the ones
# that hand you a Kafka topic dump. A real platform has both, and bronze has to
# cope with both.
CSV_DATASETS = {"customers", "payments"}


def write_batch(cfg: Config, batch: int) -> dict[str, int]:
    """Write one landing batch to disk and return per-dataset row counts."""
    world = _build_world(cfg)
    datasets = _split(world, cfg)[batch]
    counts: dict[str, int] = {}

    for dataset, rows in datasets.items():
        target = Path(cfg.landing_path(dataset, batch))
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

        if dataset in CSV_DATASETS:
            path = target / f"{dataset}.csv"
            fieldnames = list(rows[0].keys())
            with open(path, "w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
        else:
            path = target / f"{dataset}.json"
            with open(path, "w") as fh:
                for row in rows:
                    fh.write(json.dumps(row) + "\n")
        counts[dataset] = len(rows)

    # Reference data that never changes. Written once, at batch 1, the way a
    # slowly-changing lookup table would be.
    if batch == 1:
        ref_dir = Path(cfg.landing_path("reference", 1))
        ref_dir.mkdir(parents=True, exist_ok=True)
        with open(ref_dir / "sports.json", "w") as fh:
            for sport_id, name, code in SPORTS:
                fh.write(json.dumps({"sport_id": sport_id, "sport_name": name, "sport_code": code}) + "\n")
        with open(ref_dir / "competitions.json", "w") as fh:
            for sport_id, comps in COMPETITIONS.items():
                for comp_id, comp_name in comps:
                    fh.write(
                        json.dumps(
                            {
                                "competition_id": comp_id,
                                "sport_id": sport_id,
                                "competition_name": comp_name,
                            }
                        )
                        + "\n"
                    )
        counts["reference"] = len(SPORTS) + sum(len(c) for c in COMPETITIONS.values())

    return counts


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate synthetic wagering source data")
    parser.add_argument("--batch", type=int, default=1, choices=(1, 2))
    args = parser.parse_args()
    cfg = Config.load()
    counts = write_batch(cfg, args.batch)
    print(f"batch {args.batch} written to {cfg.landing_dir}")
    for dataset, n in sorted(counts.items()):
        print(f"  {dataset:<14} {n:>7,} rows")


if __name__ == "__main__":
    main()
