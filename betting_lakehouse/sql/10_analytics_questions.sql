-- ===========================================================================
-- The questions a wagering business actually asks.
--
-- Every one of these is a single join away because the star schema was built for
-- it. Ask the same questions of the raw vault and each becomes six joins and two
-- window functions - which is the whole argument for having both models.
--
-- Run locally after `make run`:
--     .venv/bin/spark-sql -f sql/10_analytics_questions.sql
-- or paste individual queries into a Databricks SQL editor.
-- ===========================================================================

-- Locally the catalog is spark_catalog; on Databricks it is sportsbet_demo.
USE spark_catalog.gold;

-- ---------------------------------------------------------------------------
-- 1. Yesterday's headline numbers.
--    The first thing anyone looks at, and the reason the pipeline runs at 05:15.
-- ---------------------------------------------------------------------------
SELECT
    d.full_date,
    d.day_name,
    round(sum(f.stake_amount), 2)                                     AS turnover,
    round(sum(f.payout_amount), 2)                                    AS payouts,
    round(sum(f.gross_win_amount), 2)                                 AS gross_win,
    round(100 * sum(f.gross_win_amount) / sum(f.stake_amount), 2)      AS hold_pct,
    count(*)                                                          AS bets,
    count(DISTINCT f.customer_dk)                                     AS active_customers,
    round(avg(f.stake_amount), 2)                                     AS avg_stake
FROM fact_bet_settlement f
JOIN dim_date d ON f.placed_date_key = d.date_key
GROUP BY d.full_date, d.day_name
ORDER BY d.full_date DESC
LIMIT 14;

-- ---------------------------------------------------------------------------
-- 2. Turnover and margin by sport and channel.
--    Uses the LEG fact and the ALLOCATED stake, because sport only exists per leg.
--    Summing bet_stake_amount here would multiply a 4-leg multi's stake by four.
-- ---------------------------------------------------------------------------
SELECT
    e.sport_name,
    ch.channel_group,
    round(sum(l.stake_allocated), 2)                                        AS turnover,
    round(sum(l.gross_win_allocated), 2)                                    AS gross_win,
    round(100 * sum(l.gross_win_allocated) / sum(l.stake_allocated), 2)     AS hold_pct,
    count(DISTINCT l.bet_id)                                                AS bets,
    count(*)                                                                AS legs
FROM fact_bet_leg l
JOIN dim_event e   ON l.event_sk = e.event_sk
JOIN dim_channel ch ON l.channel_sk = ch.channel_sk
WHERE l.is_settled
GROUP BY e.sport_name, ch.channel_group
ORDER BY turnover DESC;

-- ---------------------------------------------------------------------------
-- 3. Which markets are priced well?
--    Realised hold against the theoretical margin built into the odds. A big gap
--    is a signal: hold far below theoretical suggests sharp customers or stale
--    prices, far above suggests a settlement or void-handling bug.
-- ---------------------------------------------------------------------------
WITH theoretical AS (
    SELECT
        market_type,
        -- sum(1/odds) across a market exceeds 1 by the overround; averaged per
        -- market that is the theoretical margin.
        round(100 * (sum(current_implied_probability) - count(DISTINCT market_id))
              / sum(current_implied_probability), 2) AS theoretical_margin_pct
    FROM dim_selection
    WHERE selection_sk <> -1
    GROUP BY market_type
),
realised AS (
    SELECT
        market_type,
        round(sum(stake_allocated), 2)                                    AS turnover,
        round(100 * sum(gross_win_allocated) / sum(stake_allocated), 2)   AS hold_pct
    FROM fact_bet_leg
    WHERE is_settled
    GROUP BY market_type
)
SELECT r.market_type, r.turnover, r.hold_pct, t.theoretical_margin_pct,
       round(r.hold_pct - t.theoretical_margin_pct, 2) AS gap_pct
FROM realised r
LEFT JOIN theoretical t USING (market_type)
ORDER BY r.turnover DESC;

-- ---------------------------------------------------------------------------
-- 4. Multi vs single.
--    Multis hold far more because the overround compounds across legs, which is
--    exactly why they get marketed.
-- ---------------------------------------------------------------------------
SELECT
    bt.bet_type_name,
    count(*)                                                        AS bets,
    round(avg(f.legs_in_bet), 2)                                    AS avg_legs,
    round(sum(f.stake_amount), 2)                                   AS turnover,
    round(avg(f.stake_amount), 2)                                   AS avg_stake,
    round(100 * sum(f.gross_win_amount) / sum(f.stake_amount), 2)    AS hold_pct,
    round(100 * avg(CASE WHEN f.is_won THEN 1 ELSE 0 END), 1)        AS win_rate_pct
FROM fact_bet_settlement f
JOIN dim_bet_type bt ON f.bet_type_sk = bt.bet_type_sk
WHERE f.is_settled
GROUP BY bt.bet_type_name
ORDER BY turnover DESC;

-- ---------------------------------------------------------------------------
-- 5. Jurisdictional turnover.
--    Australian wagering is regulated per state, and turnover must be attributed
--    to where the customer was WHEN THE BET WAS PLACED. The fact already carries
--    the as-at customer_sk, so there is no date logic in this query - that work
--    was done once, at load time.
-- ---------------------------------------------------------------------------
SELECT
    c.state_code,
    count(DISTINCT f.customer_dk)                                   AS customers,
    count(*)                                                        AS bets,
    round(sum(f.stake_amount), 2)                                   AS turnover,
    round(100 * sum(f.gross_win_amount) / sum(f.stake_amount), 2)    AS hold_pct,
    round(sum(f.stake_amount) / count(DISTINCT f.customer_dk), 2)    AS turnover_per_customer
FROM fact_bet_settlement f
JOIN dim_customer c ON f.customer_sk = c.customer_sk
GROUP BY c.state_code
ORDER BY turnover DESC;

-- ---------------------------------------------------------------------------
-- 6. Value by VIP tier and age band.
--    The kind of segmentation that drives marketing spend - and the reason the
--    age band is derived in the mart rather than the vault, so it can be
--    redefined without reloading history.
-- ---------------------------------------------------------------------------
SELECT
    c.vip_tier,
    c.age_band,
    count(DISTINCT f.customer_dk)                                   AS customers,
    round(sum(f.stake_amount), 2)                                   AS turnover,
    round(sum(f.stake_amount) / count(DISTINCT f.customer_dk), 2)    AS turnover_per_customer,
    round(100 * sum(f.gross_win_amount) / sum(f.stake_amount), 2)    AS hold_pct
FROM fact_bet_settlement f
JOIN dim_customer c ON f.customer_sk = c.customer_sk
WHERE c.customer_sk <> -1
GROUP BY c.vip_tier, c.age_band
ORDER BY turnover DESC;

-- ---------------------------------------------------------------------------
-- 7. In-play vs pre-game.
--    In-play is the fastest-growing and highest-risk product: prices move in
--    seconds, so pricing errors are expensive and latency is a real constraint.
-- ---------------------------------------------------------------------------
SELECT
    CASE WHEN is_in_play THEN 'In-play' ELSE 'Pre-game' END          AS bet_timing,
    count(*)                                                        AS bets,
    round(sum(stake_amount), 2)                                     AS turnover,
    round(100 * sum(stake_amount) / sum(sum(stake_amount)) OVER (), 1) AS pct_of_turnover,
    round(100 * sum(gross_win_amount) / sum(stake_amount), 2)        AS hold_pct
FROM fact_bet_settlement
WHERE is_settled
GROUP BY is_in_play;

-- ---------------------------------------------------------------------------
-- 8. Where is the money concentrated?
--    Wagering revenue is extremely top-heavy. This decile view is both a
--    commercial fact and a responsible-gambling one: the same concentration that
--    makes VIP programmes profitable is what makes harm-minimisation obligations
--    real.
-- ---------------------------------------------------------------------------
WITH per_customer AS (
    SELECT customer_dk,
           sum(stake_amount)     AS turnover,
           sum(gross_win_amount) AS book_win
    FROM fact_bet_settlement
    GROUP BY customer_dk
),
deciled AS (
    SELECT *, ntile(10) OVER (ORDER BY turnover DESC) AS turnover_decile
    FROM per_customer
)
SELECT
    turnover_decile,
    count(*)                                                          AS customers,
    round(sum(turnover), 2)                                           AS turnover,
    round(100 * sum(turnover) / sum(sum(turnover)) OVER (), 1)        AS pct_of_total_turnover,
    round(avg(turnover), 2)                                           AS avg_turnover_per_customer,
    round(sum(book_win), 2)                                           AS gross_win
FROM deciled
GROUP BY turnover_decile
ORDER BY turnover_decile;

-- ---------------------------------------------------------------------------
-- 9. Responsible gambling: escalating stakes in a long session.
--    Long unbroken sessions with rising stakes and mounting losses are the
--    classic loss-chasing pattern. Australian operators are required to identify
--    and act on it, which makes this query part of the licence, not the roadmap.
-- ---------------------------------------------------------------------------
SELECT
    customer_id,
    placed_date,
    bet_count,
    round(betting_span_hours, 1)     AS session_hours,
    round(turnover_amount, 2)        AS turnover,
    round(max_single_stake, 2)       AS biggest_stake,
    round(-1 * gross_win_amount, 2)  AS customer_net_position,
    in_play_bet_count
FROM agg_daily_customer_activity
WHERE bet_count >= 8
  AND betting_span_hours >= 6
  AND gross_win_amount > 0          -- book won, i.e. the customer lost
ORDER BY gross_win_amount DESC
LIMIT 20;

-- ---------------------------------------------------------------------------
-- 10. Responsible gambling: activity on a self-excluded account.
--     This must return nothing. If it returns rows, it is an incident.
-- ---------------------------------------------------------------------------
SELECT f.bet_id, f.customer_id, f.placed_at, f.stake_amount, c.effective_from
FROM fact_bet_settlement f
JOIN dim_customer c ON f.customer_sk = c.customer_sk
WHERE c.is_self_excluded;

-- ---------------------------------------------------------------------------
-- 11. New customer cohorts: do they come back?
--     Retention by signup month, which is the number that decides whether
--     acquisition spend was worth it.
-- ---------------------------------------------------------------------------
WITH cohorts AS (
    SELECT DISTINCT customer_dk, date_format(signup_date, 'yyyy-MM') AS signup_month
    FROM dim_customer
    WHERE is_current AND customer_sk <> -1
),
activity AS (
    SELECT customer_dk, count(DISTINCT placed_date) AS active_days, sum(stake_amount) AS turnover
    FROM fact_bet_settlement
    GROUP BY customer_dk
)
SELECT
    c.signup_month,
    count(*)                                                              AS customers,
    count(a.customer_dk)                                                  AS bet_in_period,
    round(100.0 * count(a.customer_dk) / count(*), 1)                     AS pct_active,
    round(avg(a.active_days), 1)                                          AS avg_active_days,
    round(sum(a.turnover), 2)                                             AS turnover
FROM cohorts c
LEFT JOIN activity a USING (customer_dk)
GROUP BY c.signup_month
HAVING count(*) >= 10
ORDER BY c.signup_month DESC
LIMIT 12;

-- ---------------------------------------------------------------------------
-- 12. Data quality dashboard.
--     Every expectation, every run. Somebody has to look at this, otherwise the
--     quarantine table is just a slower DELETE.
-- ---------------------------------------------------------------------------
SELECT
    table_name,
    expectation,
    severity,
    sum(rows_checked)                                                  AS rows_checked,
    sum(rows_failed)                                                   AS rows_failed,
    round(100.0 * (sum(rows_checked) - sum(rows_failed)) / sum(rows_checked), 3) AS pass_pct,
    max(checked_at)                                                    AS last_checked
FROM spark_catalog.dq.dq_results
GROUP BY table_name, expectation, severity
ORDER BY rows_failed DESC, table_name;
