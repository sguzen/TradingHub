#!/usr/bin/env python3
"""
Candle Science — Probability Builder (v4)
==========================================
Builds:
  1. Base C2/C3 probabilities (c1/c2 color combos)
  2. Conditional C3 probabilities (given pill selections)
  3. Classification distribution: per combo, % of C3 matches in each day type
  4. MAE/MFE raw points: last 50 C3 data points per combo × DOW
     Each point: { date, dow, mae_bp, mfe_bp, bull, cls }

json structure:
  {
    "NQ": {
      "probs":       { combo: { n, c2, c3, c3_bull } },
      "conditional": { combo: { metric_dir: { n, c3 } } },
      "cls_dist":    { combo: { Range1, DWP, DNP, Range2 } each { n, pct } },
      "mae_points":  { combo: { all|Mon|Tue|Wed|Thu|Fri: [ {date,mae,mfe,bull,cls}, ... ] } }
    }
  }
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import duckdb

DB_PATH  = Path(__file__).parent / "candle_science.duckdb"
OUT_PATH = Path(__file__).parent / "candle_probs.json"

INSTRUMENTS = {
    "NQ": {"table": "nq_1m", "ref_price": 21000},
    "ES": {"table": "es_1m", "ref_price": 5800},
}

COLOR_COMBOS = {
    "bull_bull": "c1_bull AND c2_bull",
    "bull_bear": "c1_bull AND NOT c2_bull",
    "bear_bull": "NOT c1_bull AND c2_bull",
    "bear_bear": "NOT c1_bull AND NOT c2_bull",
    "all":       "TRUE",
}

DOW_NAMES  = ["Mon","Tue","Wed","Thu","Fri"]
# DuckDB: DAYOFWEEK 0=Sun,1=Mon,...,5=Fri,6=Sat
DOW_NUM = {"Mon":1,"Tue":2,"Wed":3,"Thu":4,"Fri":5}

C2_METRICS = [
    "c2_high_gt_c1_high","c2_high_gt_c1_open",
    "c2_low_gt_c1_low","c2_low_gt_c1_open",
    "c2_close_gt_c1_high","c2_close_gt_c1_low",
    "c2_close_gt_c1_close","c2_close_gt_c1_open",
    "c2_open_gt_c1_close","c2_open_gt_c1_open",
    "c2_open_gt_c1_high","c2_open_gt_c1_low",
]
C3_METRICS = [
    "c3_high_gt_c2_high","c3_high_gt_c2_open",
    "c3_low_gt_c2_low","c3_low_gt_c2_open",
    "c3_close_gt_c2_high","c3_close_gt_c2_low",
    "c3_close_gt_c2_close","c3_close_gt_c2_open",
    "c3_open_gt_c2_close","c3_open_gt_c2_open",
    "c3_open_gt_c2_high","c3_open_gt_c2_low",
    "c3_bull",
]
N_POINTS = 50  # raw data points per combo × DOW bucket


def build_instrument(con, symbol: str, cfg: dict) -> dict:
    table     = cfg["table"]
    # Use median close over the full history so basis-point calculations
    # stay accurate as the instrument's price level changes over time.
    ref_price = con.execute(f"SELECT MEDIAN(close) FROM {table}").fetchone()[0] or cfg["ref_price"]

    # ── 1. Daily OHLCV ───────────────────────────────────────────────────────
    print(f"\n  [{symbol}] Daily bars…")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE daily AS
        SELECT
            CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
            FIRST(open  ORDER BY timestamp) AS open,
            MAX(high)                       AS high,
            MIN(low)                        AS low,
            LAST(close  ORDER BY timestamp) AS close
        FROM {table}
        GROUP BY CAST(timezone('America/New_York', timestamp) AS DATE)
        ORDER BY date
    """)
    ndays = con.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    print(f"  [{symbol}] {ndays:,} days")

    # ── 2. Day classification ────────────────────────────────────────────────
    print(f"  [{symbol}] Classifying days…")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE intraday_feat AS
        SELECT
            CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
            FIRST(open ORDER BY timestamp) FILTER (
                WHERE EXTRACT(HOUR   FROM timezone('America/New_York', timestamp)) = 9
                  AND EXTRACT(MINUTE FROM timezone('America/New_York', timestamp)) = 30
            ) AS open_930,
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '09:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '10:00') AS hi_0930,
            MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '09:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '10:00') AS lo_0930,
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '10:00'
                               AND  timezone('America/New_York', timestamp)::TIME <  '10:30') AS hi_1000,
            MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '10:00'
                               AND  timezone('America/New_York', timestamp)::TIME <  '10:30') AS lo_1000,
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '10:00'
                               AND  timezone('America/New_York', timestamp)::TIME <  '16:15') AS hi_aft,
            MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '10:00'
                               AND  timezone('America/New_York', timestamp)::TIME <  '16:15') AS lo_aft,
            LAST(close ORDER BY timestamp) FILTER (
                WHERE timezone('America/New_York', timestamp)::TIME < '16:15'
            ) AS close_rth,
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME BETWEEN '09:30' AND '16:15') AS rth_high,
            MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME BETWEEN '09:30' AND '16:15') AS rth_low
        FROM {table}
        GROUP BY CAST(timezone('America/New_York', timestamp) AS DATE)
    """)
    # ── Classification logic (source: Pack Bootcamp transcripts) ────────────
    #
    # Frequencies taught: Range1=38%, DWP=32%, DNP=15%, Range2=12%
    #
    #   Range1 (~38-41%) — "making out with the 9:30": price orbits the 9:30
    #       candle ALL DAY. Defined as ALL 5 RTH hours (10,11,12,13,14) having
    #       their range intersect [lo_0930, hi_0930] → touch_count = 5.
    #       "Four or more hours inside of the 9:30" (Orientation transcript).
    #       touch=5 fires on ~39.5% of days, matching the taught 38%.
    #
    #   DNP (~13-15%) — "directional with no pullback / strike trend":
    #       Trends cleanly without any prior hourly low being taken (bull) or
    #       prior hourly high being taken (bear). The close must also be strongly
    #       displaced from 9:30 (≥50% of daily range) — a weak drift that
    #       never took a low is still DWP, not a true trend day.
    #       "No hourly low taken... your hourly stops would be safe" (Orientation).
    #
    #   Range2 (~6-7%) — "Kim Kardashian thigh gap": morning makes a notable
    #       displacement (9:30 candle > 20% of daily range) and close REVERTS
    #       back near 9:30 (≤20% of daily range from open_930). Evaluated AFTER
    #       Range1 so Range1 days with a wild open are not misclassified.
    #       "Move far far away and then revert all the way back" (Orientation).
    #
    #   DWP (~39-42%) — "directional with pullback": everything else. Moved
    #       away from 9:30, ranged in the afternoon, one hourly low swiped.
    #       Most common after Range1. "Move fast and furious away from 9:30,
    #       spend the rest of the afternoon in a range" (Orientation).
    #
    # Priority order in CASE: Range1 → DNP → Range2 → DWP
    #
    # Note: NQ data produces Range1≈41%, DWP≈39%, DNP≈13%, Range2≈7%.
    # The taught 38/32/15/12 targets cannot all be simultaneously achieved
    # with these definitions — Range2 is undercounted because 314 days that
    # have both a morning thigh gap AND all-day orbiting are correctly
    # classified as Range1 (transcript confirms: "it does have a thigh gap...
    # more than likely it's a range one").

    # Build hourly structure per day
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE hourly_struct AS
        WITH hourly AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                EXTRACT(HOUR FROM timezone('America/New_York', timestamp))::INT AS hr,
                MAX(high) AS h_high,
                MIN(low)  AS h_low
            FROM {table}
            WHERE timezone('America/New_York', timestamp)::TIME >= '09:30'
              AND timezone('America/New_York', timestamp)::TIME <  '16:15'
            GROUP BY 1, 2
        ),
        with_prev AS (
            SELECT
                date, hr, h_high, h_low,
                LAG(h_low,  1) OVER (PARTITION BY date ORDER BY hr) AS prev_h_low,
                LAG(h_high, 1) OVER (PARTITION BY date ORDER BY hr) AS prev_h_high
            FROM hourly
        )
        SELECT
            date,
            -- bull breach: any hour's low is below the prior hour's low
            MAX(CASE WHEN prev_h_low IS NOT NULL AND h_low < prev_h_low THEN 1 ELSE 0 END) AS bull_breach,
            -- bear breach: any hour's high is above the prior hour's high
            MAX(CASE WHEN prev_h_high IS NOT NULL AND h_high > prev_h_high THEN 1 ELSE 0 END) AS bear_breach,
            -- r1_touch: count of hours 10:00–14:59 whose range overlaps the 9:30 candle.
            -- An hour touches the 9:30 candle if h_high >= lo_0930 AND h_low <= hi_0930
            -- (i.e. the hour's range intersects [lo_0930, hi_0930]).
            -- We get lo_0930/hi_0930 from intraday_feat via a subquery.
            -- Computed separately below after intraday_feat exists.
            0 AS r1_touch_placeholder  -- replaced in next step
        FROM with_prev
        GROUP BY date
    """)

    # Compute r1_touch: hours 10:00–14:59 that overlap the 9:30 candle range
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE r1_touch AS
        WITH hourly_afternoon AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                EXTRACT(HOUR FROM timezone('America/New_York', timestamp))::INT AS hr,
                MAX(high) AS h_high,
                MIN(low)  AS h_low
            FROM {table}
            WHERE timezone('America/New_York', timestamp)::TIME >= '10:00'
              AND timezone('America/New_York', timestamp)::TIME <  '15:00'
            GROUP BY 1, 2
        )
        SELECT
            h.date,
            -- count hours that trade THROUGH the 9:30 open price —
            -- "making out with the 9:30" means returning to that specific
            -- price level, not just clipping anywhere in the 9:30 range
            SUM(CASE WHEN h.h_high >= f.lo_0930
                      AND h.h_low  <= f.hi_0930
                      THEN 1 ELSE 0 END) AS touch_count
        FROM hourly_afternoon h
        JOIN intraday_feat f ON f.date = h.date
        GROUP BY h.date
    """)

    # Rebuild hourly_struct with the real touch_count
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE hourly_struct AS
        WITH hourly AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                EXTRACT(HOUR FROM timezone('America/New_York', timestamp))::INT AS hr,
                MAX(high) AS h_high,
                MIN(low)  AS h_low
            FROM {table}
            WHERE timezone('America/New_York', timestamp)::TIME >= '10:00'
              AND timezone('America/New_York', timestamp)::TIME <  '15:00'
            GROUP BY 1, 2
        ),
        with_prev AS (
            SELECT
                date, hr, h_high, h_low,
                LAG(h_low,  1) OVER (PARTITION BY date ORDER BY hr) AS prev_h_low,
                LAG(h_high, 1) OVER (PARTITION BY date ORDER BY hr) AS prev_h_high
            FROM hourly
        ),
        breaches AS (
            SELECT
                date,
                -- Only hours 10:00-14:59: the 9:30 complexity hour is excluded
                -- (transcript says 9:30-10 complexity is NORMAL for Range1 + DNP)
                MAX(CASE WHEN prev_h_low  IS NOT NULL AND h_low  < prev_h_low  THEN 1 ELSE 0 END) AS bull_breach,
                MAX(CASE WHEN prev_h_high IS NOT NULL AND h_high > prev_h_high THEN 1 ELSE 0 END) AS bear_breach
            FROM with_prev
            GROUP BY date
        )
        SELECT b.date, b.bull_breach, b.bear_breach, COALESCE(t.touch_count, 0) AS touch_count
        FROM breaches b
        LEFT JOIN r1_touch t ON t.date = b.date
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE day_class AS
        SELECT
            f.date,
            CASE
                -- Range1: ALL 5 RTH hours (10–14) orbit the 9:30 candle range.
                -- "Four or more hours inside of the 9:30" (Orientation transcript).
                -- touch_count=5 (all 5 hours intersect [lo_0930, hi_0930]) fires
                -- on ~39.5% of days, matching the taught 38% frequency.
                WHEN s.touch_count >= 5
                THEN 'Range1'

                -- DNP: clean trend day with no prior hourly low taken (bull) or
                -- prior hourly high taken (bear). Close must be strongly displaced
                -- from 9:30 (≥50% of daily range) — a weak drift without any low
                -- taken is still DWP, not a true strike trend.
                -- "No hourly low taken... your hourly stops safe under the lows."
                WHEN (f.close_rth > f.open_930
                      AND s.bull_breach = 0
                      AND ABS(f.close_rth - f.open_930) >= (f.rth_high - f.rth_low) * 0.50)
                  OR (f.close_rth < f.open_930
                      AND s.bear_breach = 0
                      AND ABS(f.close_rth - f.open_930) >= (f.rth_high - f.rth_low) * 0.50)
                THEN 'DNP'

                -- Range2: morning thigh gap (9:30 candle > 20% of daily range)
                -- AND close reverts back near 9:30 (≤20% of daily range).
                -- Evaluated AFTER Range1 — days with both a gap and all-day
                -- orbiting are correctly Range1 ("it does have a thigh gap...
                -- more than likely it's a range one" — Orientation transcript).
                -- "Move far far away and then revert all the way back."
                WHEN (f.rth_high - f.rth_low) > 0
                 AND (f.hi_0930 - f.lo_0930) > (f.rth_high - f.rth_low) * 0.20
                 AND ABS(f.close_rth - f.open_930) <= (f.rth_high - f.rth_low) * 0.20
                THEN 'Range2'

                -- DWP: everything else — moved fast and furious, ranged afternoon.
                -- "Spend all the checkbook in 40 minutes, range rest of afternoon."
                ELSE 'DWP'
            END AS classification
        FROM intraday_feat f
        LEFT JOIN hourly_struct s ON s.date = f.date
        WHERE f.open_930 IS NOT NULL
          AND f.rth_high IS NOT NULL AND f.rth_low IS NOT NULL
          AND (f.rth_high - f.rth_low) > 0
    """)

    # ── 3. Triplets ──────────────────────────────────────────────────────────
    print(f"  [{symbol}] Triplets…")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE triplets AS
        WITH ord AS (
            SELECT date, open, high, low, close,
                   ROW_NUMBER() OVER (ORDER BY date) AS rn
            FROM daily
        )
        SELECT
            c1.date AS c1_date, c2.date AS c2_date, c3.date AS c3_date,
            c1.open AS c1_open, c1.high AS c1_high, c1.low AS c1_low, c1.close AS c1_close,
            c2.open AS c2_open, c2.high AS c2_high, c2.low AS c2_low, c2.close AS c2_close,
            c3.open AS c3_open, c3.high AS c3_high, c3.low AS c3_low, c3.close AS c3_close,
            (c1.close >= c1.open) AS c1_bull, (c2.close >= c2.open) AS c2_bull,
            (c3.close >= c3.open) AS c3_bull,
            (c2.high  > c1.high)  AS c2_high_gt_c1_high,
            (c2.high  > c1.open)  AS c2_high_gt_c1_open,
            (c2.low   > c1.low)   AS c2_low_gt_c1_low,
            (c2.low   > c1.open)  AS c2_low_gt_c1_open,
            (c2.close > c1.high)  AS c2_close_gt_c1_high,
            (c2.close > c1.low)   AS c2_close_gt_c1_low,
            (c2.close > c1.close) AS c2_close_gt_c1_close,
            (c2.close > c1.open)  AS c2_close_gt_c1_open,
            (c2.open  > c1.close) AS c2_open_gt_c1_close,
            (c2.open  > c1.open)  AS c2_open_gt_c1_open,
            (c2.open  > c1.high)  AS c2_open_gt_c1_high,
            (c2.open  > c1.low)   AS c2_open_gt_c1_low,
            (c3.high  > c2.high)  AS c3_high_gt_c2_high,
            (c3.high  > c2.open)  AS c3_high_gt_c2_open,
            (c3.low   > c2.low)   AS c3_low_gt_c2_low,
            (c3.low   > c2.open)  AS c3_low_gt_c2_open,
            (c3.close > c2.high)  AS c3_close_gt_c2_high,
            (c3.close > c2.low)   AS c3_close_gt_c2_low,
            (c3.close > c2.close) AS c3_close_gt_c2_close,
            (c3.close > c2.open)  AS c3_close_gt_c2_open,
            (c3.open  > c2.close) AS c3_open_gt_c2_close,
            (c3.open  > c2.open)  AS c3_open_gt_c2_open,
            (c3.open  > c2.high)  AS c3_open_gt_c2_high,
            (c3.open  > c2.low)   AS c3_open_gt_c2_low
        FROM ord c1
        JOIN ord c2 ON c2.rn = c1.rn + 1
        JOIN ord c3 ON c3.rn = c1.rn + 2
    """)
    ntriplets = con.execute("SELECT COUNT(*) FROM triplets").fetchone()[0]
    print(f"  [{symbol}] {ntriplets:,} triplets")

    # Join classification onto triplets (on c3 date)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE triplets_classed AS
        SELECT t.*, COALESCE(dc.classification, 'Range1') AS c3_class
        FROM triplets t
        LEFT JOIN day_class dc ON dc.date = t.c3_date
    """)

    # ── 4. RTH stats for MAE/MFE ─────────────────────────────────────────────
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE rth_stats AS
        SELECT
            CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
            FIRST(open ORDER BY timestamp) FILTER (
                WHERE timezone('America/New_York', timestamp)::TIME >= '09:30'
            ) AS rth_open,
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME BETWEEN '09:30' AND '16:15') AS rth_high,
            MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME BETWEEN '09:30' AND '16:15') AS rth_low
        FROM {table}
        GROUP BY CAST(timezone('America/New_York', timestamp) AS DATE)
    """)

    # 12-hour session open (06:00 ET) — bootcamp anchor for MAE/MFE measurement.
    # Mickey's tool measures adverse/favorable excursion from the 06:00 open
    # within the full RTH session (06:00–16:30). This always produces non-zero
    # values because price ALWAYS moves from the 06:00 open.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE session_06_open AS
        SELECT
            CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
            FIRST(open ORDER BY timestamp) FILTER (
                WHERE timezone('America/New_York', timestamp)::TIME >= '06:00'
                  AND timezone('America/New_York', timestamp)::TIME <  '06:15'
            ) AS open_0600
        FROM {table}
        GROUP BY CAST(timezone('America/New_York', timestamp) AS DATE)
    """)

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE triplets_mae AS
        SELECT
            tc.c3_date,
            DAYOFWEEK(tc.c3_date) AS dow_num,
            tc.c3_bull,
            tc.c3_class,
            tc.c1_bull, tc.c2_bull,
            -- Anchor: 06:00 session open (start of the 12-hour cycle Mickey teaches).
            -- MAE = adverse excursion from 06:00 open within the RTH session.
            --   Bull C3: adverse = downward — how far BELOW 06:00 open did RTH low go
            --   Bear C3: adverse = upward   — how far ABOVE 06:00 open did RTH high go
            ROUND(CASE WHEN tc.c3_bull
                 THEN GREATEST((s06.open_0600 - rs.rth_low)   / {ref_price} * 10000, 0)
                 ELSE GREATEST((rs.rth_high   - s06.open_0600) / {ref_price} * 10000, 0)
            END, 3) AS mae_bp,
            -- MFE = favorable excursion from 06:00 open within the RTH session.
            --   Bull C3: favorable = upward   — how far ABOVE 06:00 open did RTH high go
            --   Bear C3: favorable = downward — how far BELOW 06:00 open did RTH low go
            ROUND(CASE WHEN tc.c3_bull
                 THEN GREATEST((rs.rth_high   - s06.open_0600) / {ref_price} * 10000, 0)
                 ELSE GREATEST((s06.open_0600 - rs.rth_low)   / {ref_price} * 10000, 0)
            END, 3) AS mfe_bp
        FROM triplets_classed tc
        JOIN rth_stats       rs  ON rs.date  = tc.c3_date
        JOIN session_06_open s06 ON s06.date = tc.c3_date
        WHERE rs.rth_high   IS NOT NULL
          AND rs.rth_low    IS NOT NULL
          AND s06.open_0600 IS NOT NULL
          AND mae_bp >= 0
          AND mfe_bp >= 0
    """)

    # ── 5. Base probs ─────────────────────────────────────────────────────────
    def fetch_probs(metrics, where, tbl="triplets"):
        agg = ", ".join([
            f"ROUND(AVG(CAST({m} AS DOUBLE))*100,2) AS {m}" for m in metrics
        ])
        row = con.execute(f"SELECT COUNT(*) AS n, {agg} FROM {tbl} WHERE {where}").fetchone()
        return dict(zip(["n"] + metrics, row))

    result = {"probs": {}, "conditional": {}}

    print(f"  [{symbol}] Base probs…")
    for combo, where in COLOR_COMBOS.items():
        c2p = fetch_probs(C2_METRICS, where)
        c3p = fetch_probs(C3_METRICS, where)
        result["probs"][combo] = {
            "n":       c2p["n"],
            "c2":      {k: v for k, v in c2p.items() if k != "n"},
            "c3":      {k: v for k, v in c3p.items() if k not in ("n","c3_bull")},
            "c3_bull": c3p["c3_bull"],
        }

    print(f"  [{symbol}] Conditional probs…")
    for combo, base_where in COLOR_COMBOS.items():
        if combo == "all": continue
        result["conditional"][combo] = {}
        for c2m in C2_METRICS:
            for direction in ("above","below"):
                obs   = f"{c2m} = TRUE" if direction == "above" else f"{c2m} = FALSE"
                where = f"({base_where}) AND ({obs})"
                try:
                    c3p = fetch_probs(C3_METRICS, where)
                    result["conditional"][combo][f"{c2m}_{direction}"] = {
                        "n": c3p["n"],
                        "c3": {k: v for k, v in c3p.items() if k != "n"},
                    }
                except Exception as e:
                    print(f"    Warning {combo}/{c2m}_{direction}: {e}")

    # ── 6. Classification distribution per combo ──────────────────────────────
    # For each combo: how many C3 matches fall in each day type
    print(f"  [{symbol}] Classification distribution…")
    cls_dist = {}
    for combo, combo_where in COLOR_COMBOS.items():
        rows = con.execute(f"""
            SELECT c3_class, COUNT(*) AS n
            FROM triplets_classed
            WHERE {combo_where}
            GROUP BY c3_class
        """).fetchall()
        total = sum(r[1] for r in rows)
        cls_dist[combo] = {
            r[0]: {"n": int(r[1]), "pct": round(r[1]/total*100, 1) if total else 0}
            for r in rows
        }
        cls_dist[combo]["total"] = int(total)
    result["cls_dist"] = cls_dist

    # ── 6b. Raw day classification distribution (single candle, not triplet-weighted)
    raw_rows = con.execute("""
        SELECT classification, COUNT(*) AS n
        FROM day_class
        GROUP BY classification
    """).fetchall()
    raw_total = sum(r[1] for r in raw_rows)
    result["raw_cls_dist"] = {
        r[0]: {"n": int(r[1]), "pct": round(r[1]/raw_total*100, 1) if raw_total else 0}
        for r in raw_rows
    }
    result["raw_cls_dist"]["total"] = int(raw_total)

    # ── 7. MAE/MFE raw points — last N_POINTS per combo × DOW ────────────────
    print(f"  [{symbol}] MAE/MFE raw points…")
    # DOW filter: 1=Mon,2=Tue,3=Wed,4=Thu,5=Fri; 0=all (no filter)
    dow_filters = {"all": "TRUE"}
    for name, num in DOW_NUM.items():
        dow_filters[name] = f"dow_num = {num}"

    # combo_where needs to reference c1_bull / c2_bull columns in triplets_mae
    combo_where_mae = {
        "bull_bull": "c1_bull AND c2_bull",
        "bull_bear": "c1_bull AND NOT c2_bull",
        "bear_bull": "NOT c1_bull AND c2_bull",
        "bear_bear": "NOT c1_bull AND NOT c2_bull",
        "all":       "TRUE",
    }

    mae_points = {}
    for combo, combo_where in combo_where_mae.items():
        mae_points[combo] = {}
        for dow, dow_where in dow_filters.items():
            full_where = f"({combo_where}) AND ({dow_where})"
            try:
                rows = con.execute(f"""
                    SELECT
                        CAST(c3_date AS VARCHAR) AS date,
                        dow_num,
                        mae_bp,
                        mfe_bp,
                        c3_bull,
                        c3_class
                    FROM triplets_mae
                    WHERE {full_where}
                    ORDER BY c3_date DESC
                    LIMIT {N_POINTS}
                """).fetchall()
                if rows:
                    mae_points[combo][dow] = [
                        {
                            "date":  r[0],
                            "dow":   r[1],
                            "mae":   float(r[2]) if r[2] is not None else None,
                            "mfe":   float(r[3]) if r[3] is not None else None,
                            "bull":  bool(r[4]),
                            "cls":   r[5],
                        }
                        for r in rows
                        if r[2] is not None and r[3] is not None
                    ]
                else:
                    mae_points[combo][dow] = []
            except Exception as e:
                print(f"    MAE points warning {combo}/{dow}: {e}")
                mae_points[combo][dow] = []

    result["mae_points"] = mae_points

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 1 — 10-DAY MEDIAN RANGE + SESSION DISTROS (Realized Volatility)
    # ══════════════════════════════════════════════════════════════════════════
    # Sessions (ET):  Asia=18:00–02:30  London=02:30–07:30  NY1=07:30–11:30  NY2=11:30–16:15
    # "10-day median range" = realistic daily range expectation for local market.
    # Session distros = quarterly median range per session (last ~63 trading days).
    # Use: before 8:30 each day, compare today's session ranges to their
    # quarterly checkbook → overspend = volatility expanding.
    print(f"  [{symbol}] Realized volatility / session distros…")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE session_ranges AS
        SELECT
            CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
            -- Daily RTH range (09:30–16:15)
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME BETWEEN '09:30' AND '16:15')
          - MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME BETWEEN '09:30' AND '16:15')
            AS rth_range,
            -- Asia session range (prior day 18:00 – 02:30 ET)
            -- We approximate: Asia = 00:00–02:30 + prior 18:00–23:59
            -- Easier: pull the globex 18:00–09:30 window, split by subsession
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '18:00'
                               OR  timezone('America/New_York', timestamp)::TIME <  '02:30')
          - MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '18:00'
                               OR  timezone('America/New_York', timestamp)::TIME <  '02:30')
            AS asia_range,
            -- London session (02:30–07:30)
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '02:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '07:30')
          - MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '02:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '07:30')
            AS london_range,
            -- NY1 session (07:30–11:30)
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '07:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '11:30')
          - MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '07:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '11:30')
            AS ny1_range,
            -- NY2 session (11:30–16:15)
            MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '11:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '16:15')
          - MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '11:30'
                               AND  timezone('America/New_York', timestamp)::TIME <  '16:15')
            AS ny2_range
        FROM {table}
        GROUP BY CAST(timezone('America/New_York', timestamp) AS DATE)
    """)

    ten_row = con.execute("""
        SELECT
            MEDIAN(rth_range)    AS rth,
            MEDIAN(asia_range)   AS asia,
            MEDIAN(london_range) AS london,
            MEDIAN(ny1_range)    AS ny1,
            MEDIAN(ny2_range)    AS ny2
        FROM (SELECT * FROM session_ranges WHERE rth_range IS NOT NULL ORDER BY date DESC LIMIT 10)
    """).fetchone()

    q_row = con.execute("""
        SELECT
            MEDIAN(rth_range)    AS rth,
            MEDIAN(asia_range)   AS asia,
            MEDIAN(london_range) AS london,
            MEDIAN(ny1_range)    AS ny1,
            MEDIAN(ny2_range)    AS ny2
        FROM session_ranges
        WHERE rth_range IS NOT NULL
          AND date >= (SELECT MAX(date) - INTERVAL '90 days' FROM session_ranges)
    """).fetchone()

    # Last 30 trading days of daily ranges for sparkline
    range_hist = con.execute("""
        SELECT CAST(date AS VARCHAR), ROUND(rth_range, 2)
        FROM session_ranges
        WHERE rth_range IS NOT NULL
        ORDER BY date DESC LIMIT 30
    """).fetchall()

    result["volatility"] = {
        "ten_day": {
            "rth": round(float(ten_row[0] or 0), 2),
            "asia": round(float(ten_row[1] or 0), 2),
            "london": round(float(ten_row[2] or 0), 2),
            "ny1": round(float(ten_row[3] or 0), 2),
            "ny2": round(float(ten_row[4] or 0), 2),
        },
        "quarterly": {
            "rth": round(float(q_row[0] or 0), 2),
            "asia": round(float(q_row[1] or 0), 2),
            "london": round(float(q_row[2] or 0), 2),
            "ny1": round(float(q_row[3] or 0), 2),
            "ny2": round(float(q_row[4] or 0), 2),
        },
        "range_history": [{"date": r[0], "range": float(r[1])} for r in range_hist],
    }

    # ══════════════════════════════════════════════════════════════════════════
    # SHARED: 15-MINUTE BAR TABLE (used by all features below)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Building 15-min bars…")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE bars_15m AS
        SELECT
            CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
            DATE_TRUNC('hour', timezone('America/New_York', timestamp))
              + INTERVAL '15 minutes'
                * FLOOR(EXTRACT(MINUTE FROM timezone('America/New_York', timestamp)) / 15)::INT
            AS bar_ts,
            CAST(DATE_TRUNC('hour', timezone('America/New_York', timestamp))
              + INTERVAL '15 minutes'
                * FLOOR(EXTRACT(MINUTE FROM timezone('America/New_York', timestamp)) / 15)::INT
              AS TIME) AS t15,
            MAX(high) AS bar_high,
            MIN(low)  AS bar_low,
            FIRST(open ORDER BY timestamp) AS bar_open,
            LAST(close ORDER BY timestamp) AS bar_close
        FROM {table}
        GROUP BY 1, 2, 3
    """)

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 2 — SESSION VARIABLES
    # Fixed constant (FC) ranges per session:
    #   Asia   FC=18:00–19:30   Variable=19:30–02:30
    #   London FC=02:30–03:30   Variable=03:30–07:30
    #   NY1    FC=07:30–08:30   Variable=08:30–11:30
    #   NY2    FC=11:30–12:30   Variable=12:30–16:15
    #
    # Long  = variable phase first breaks above FC high
    # Short = variable phase first breaks below FC low
    # True  = variable phase stays on breakout side (never crosses back through FC)
    # False = variable phase crosses back through the opposite FC extreme
    # Broken= after variable phase, FC midline gets taken out
    #
    # False Time  = first 15m bar where price crossed back through FC (during var phase)
    # Broken Time = first 15m bar where FC midline was taken (after var phase ends)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Session variables…")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE session_vars AS
        WITH
        -- ASIA: FC=18:00-19:30, Variable=19:30-02:30
        -- CRITICAL: 18:00–23:59 belongs to the NEXT calendar day's trading session (C3 date)
        -- e.g. Tuesday 18:00 Asia open = Wednesday C3 session
        asia_raw AS (
            SELECT
                CASE
                    WHEN timezone('America/New_York', timestamp)::TIME >= '18:00'
                    THEN CAST(timezone('America/New_York', timestamp) AS DATE) + INTERVAL '1 day'
                    ELSE CAST(timezone('America/New_York', timestamp) AS DATE)
                END AS trading_date,
                high, low,
                timezone('America/New_York', timestamp)::TIME AS t
            FROM {table}
            WHERE timezone('America/New_York', timestamp)::TIME >= '18:00'
               OR timezone('America/New_York', timestamp)::TIME < '02:30'
        ),
        asia_agg AS (
            SELECT
                CAST(trading_date AS DATE) AS date,
                MAX(high) FILTER (WHERE t >= '18:00' AND t < '19:30') AS asia_fc_hi,
                MIN(low)  FILTER (WHERE t >= '18:00' AND t < '19:30') AS asia_fc_lo,
                MAX(high) FILTER (WHERE t >= '19:30' OR  t < '02:30') AS asia_var_hi,
                MIN(low)  FILTER (WHERE t >= '19:30' OR  t < '02:30') AS asia_var_lo
            FROM asia_raw
            GROUP BY 1
        ),
        -- LONDON/NY1/NY2: all bars are on the same calendar date as the C3 session
        day_agg AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                -- LONDON FC 02:30–03:30
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '02:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '03:30') AS lon_fc_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '02:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '03:30') AS lon_fc_lo,
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '03:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '07:30') AS lon_var_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '03:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '07:30') AS lon_var_lo,
                -- NY1 FC 07:30–08:30
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '07:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '08:30') AS ny1_fc_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '07:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '08:30') AS ny1_fc_lo,
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '08:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '11:30') AS ny1_var_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '08:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '11:30') AS ny1_var_lo,
                -- NY2 FC 11:30–12:30
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '11:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '12:30') AS ny2_fc_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '11:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '12:30') AS ny2_fc_lo,
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '12:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '16:15') AS ny2_var_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '12:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '16:15') AS ny2_var_lo,
                -- RTH full day high/low (for broken detection: did FC mid get taken after var phase?)
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '02:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '16:15') AS day_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '02:30'
                                   AND  timezone('America/New_York', timestamp)::TIME <  '16:15') AS day_lo
            FROM {table}
            GROUP BY 1
        )
        SELECT
            d.date,
            -- ASIA
            a.asia_fc_hi, a.asia_fc_lo, (a.asia_fc_hi+a.asia_fc_lo)/2.0 AS asia_fc_mid,
            -- LONDON
            d.lon_fc_hi,  d.lon_fc_lo,  (d.lon_fc_hi+d.lon_fc_lo)/2.0   AS lon_fc_mid,
            -- NY1
            d.ny1_fc_hi,  d.ny1_fc_lo,  (d.ny1_fc_hi+d.ny1_fc_lo)/2.0   AS ny1_fc_mid,
            -- NY2
            d.ny2_fc_hi,  d.ny2_fc_lo,  (d.ny2_fc_hi+d.ny2_fc_lo)/2.0   AS ny2_fc_mid,
            -- Direction (Long = var broke above FC high first)
            CASE WHEN a.asia_fc_hi IS NULL OR a.asia_fc_lo IS NULL THEN NULL
                 WHEN a.asia_var_hi > a.asia_fc_hi THEN TRUE
                 WHEN a.asia_var_lo  < a.asia_fc_lo THEN FALSE ELSE NULL END AS asia_long,
            CASE WHEN d.lon_fc_hi  IS NULL OR d.lon_fc_lo  IS NULL THEN NULL
                 WHEN d.lon_var_hi  > d.lon_fc_hi  THEN TRUE
                 WHEN d.lon_var_lo  < d.lon_fc_lo  THEN FALSE ELSE NULL END AS lon_long,
            CASE WHEN d.ny1_fc_hi  IS NULL OR d.ny1_fc_lo  IS NULL THEN NULL
                 WHEN d.ny1_var_hi  > d.ny1_fc_hi  THEN TRUE
                 WHEN d.ny1_var_lo  < d.ny1_fc_lo  THEN FALSE ELSE NULL END AS ny1_long,
            CASE WHEN d.ny2_fc_hi  IS NULL OR d.ny2_fc_lo  IS NULL THEN NULL
                 WHEN d.ny2_var_hi  > d.ny2_fc_hi  THEN TRUE
                 WHEN d.ny2_var_lo  < d.ny2_fc_lo  THEN FALSE ELSE NULL END AS ny2_long,
            -- True/False (True = never crossed back through opposite FC extreme)
            CASE WHEN a.asia_var_hi > a.asia_fc_hi THEN (a.asia_var_lo >= a.asia_fc_lo)
                 WHEN a.asia_var_lo  < a.asia_fc_lo THEN (a.asia_var_hi <= a.asia_fc_hi)
                 ELSE NULL END AS asia_true,
            CASE WHEN d.lon_var_hi  > d.lon_fc_hi  THEN (d.lon_var_lo  >= d.lon_fc_lo)
                 WHEN d.lon_var_lo  < d.lon_fc_lo  THEN (d.lon_var_hi  <= d.lon_fc_hi)
                 ELSE NULL END AS lon_true,
            CASE WHEN d.ny1_var_hi  > d.ny1_fc_hi  THEN (d.ny1_var_lo  >= d.ny1_fc_lo)
                 WHEN d.ny1_var_lo  < d.ny1_fc_lo  THEN (d.ny1_var_hi  <= d.ny1_fc_hi)
                 ELSE NULL END AS ny1_true,
            CASE WHEN d.ny2_var_hi  > d.ny2_fc_hi  THEN (d.ny2_var_lo  >= d.ny2_fc_lo)
                 WHEN d.ny2_var_lo  < d.ny2_fc_lo  THEN (d.ny2_var_hi  <= d.ny2_fc_hi)
                 ELSE NULL END AS ny2_true,
            -- Broken: after variable phase, did the FC midline get taken out?
            -- = day_hi/lo crossed the FC midline at any point after var phase
            CASE WHEN a.asia_fc_hi IS NULL THEN NULL
                 WHEN d.day_hi >= (a.asia_fc_hi+a.asia_fc_lo)/2.0
                  AND d.day_lo <= (a.asia_fc_hi+a.asia_fc_lo)/2.0 THEN TRUE
                 ELSE FALSE END AS asia_broken,
            CASE WHEN d.lon_fc_hi IS NULL THEN NULL
                 WHEN d.day_hi >= (d.lon_fc_hi+d.lon_fc_lo)/2.0
                  AND d.day_lo <= (d.lon_fc_hi+d.lon_fc_lo)/2.0 THEN TRUE
                 ELSE FALSE END AS lon_broken,
            CASE WHEN d.ny1_fc_hi IS NULL THEN NULL
                 WHEN d.day_hi >= (d.ny1_fc_hi+d.ny1_fc_lo)/2.0
                  AND d.day_lo <= (d.ny1_fc_hi+d.ny1_fc_lo)/2.0 THEN TRUE
                 ELSE FALSE END AS ny1_broken,
            CASE WHEN d.ny2_fc_hi IS NULL THEN NULL
                 WHEN d.day_hi >= (d.ny2_fc_hi+d.ny2_fc_lo)/2.0
                  AND d.day_lo <= (d.ny2_fc_hi+d.ny2_fc_lo)/2.0 THEN TRUE
                 ELSE FALSE END AS ny2_broken
        FROM day_agg d
        LEFT JOIN asia_agg a ON a.date = d.date
    """)

    # ── 15-min False Times & Broken Times per session ────────────────────────
    # False Time: first 15m bar in variable phase where price crossed back through FC
    #   Long False: first bar where bar_low < fc_lo
    #   Short False: first bar where bar_high > fc_hi
    # Broken Time: first 15m bar (after variable phase) where FC midline is tagged

    def fetch_false_broken_times(sess, var_start, var_end, wrap=False):
        """Returns (false_times, broken_times) as list of {t15, n} dicts."""
        fc_hi_col = f"{sess}_fc_hi"
        fc_lo_col = f"{sess}_fc_lo"
        fc_mid_col = f"{sess}_fc_mid"
        long_col  = f"{sess}_long"
        true_col  = f"{sess}_true"

        # time filter for variable phase
        if not wrap:
            var_filter = f"b.t15 >= '{var_start}' AND b.t15 < '{var_end}'"
        else:
            # wraps midnight e.g. Asia var 19:30–02:30
            var_filter = f"(b.t15 >= '{var_start}' OR b.t15 < '{var_end}')"

        # broken phase = after variable phase ends until end of session day
        # For simplicity: any bar after var_end on same date
        if not wrap:
            brk_filter = f"b.t15 >= '{var_end}'"
        else:
            brk_filter = f"b.t15 >= '{var_end}' AND b.t15 < '16:30'"

        false_rows = con.execute(f"""
            WITH false_bars AS (
                SELECT
                    sv.date,
                    sv.{long_col},
                    MIN(b.t15) FILTER (
                        WHERE {var_filter}
                          AND sv.{true_col} = FALSE
                          AND (
                            (sv.{long_col} = TRUE  AND b.bar_low  < sv.{fc_lo_col})
                         OR (sv.{long_col} = FALSE AND b.bar_high > sv.{fc_hi_col})
                          )
                    ) AS false_t
                FROM session_vars sv
                JOIN bars_15m b ON b.date = sv.date
                WHERE sv.{long_col} IS NOT NULL AND sv.{true_col} = FALSE
                GROUP BY sv.date, sv.{long_col}
            )
            SELECT CAST(false_t AS VARCHAR) AS false_t, COUNT(*) AS n
            FROM false_bars
            WHERE false_t IS NOT NULL
            GROUP BY false_t
            ORDER BY false_t
        """).fetchall()

        broken_rows = con.execute(f"""
            WITH broken_bars AS (
                SELECT
                    sv.date,
                    MIN(b.t15) FILTER (
                        WHERE {brk_filter}
                          AND b.bar_high >= sv.{fc_mid_col}
                          AND b.bar_low  <= sv.{fc_mid_col}
                    ) AS broken_t
                FROM session_vars sv
                JOIN bars_15m b ON b.date = sv.date
                WHERE sv.{long_col} IS NOT NULL
                  AND sv.{fc_mid_col} IS NOT NULL
                GROUP BY sv.date
            )
            SELECT CAST(broken_t AS VARCHAR) AS broken_t, COUNT(*) AS n
            FROM broken_bars
            WHERE broken_t IS NOT NULL
            GROUP BY broken_t
            ORDER BY broken_t
        """).fetchall()

        return (
            [{"t": str(r[0])[:5], "n": int(r[1])} for r in false_rows],
            [{"t": str(r[0])[:5], "n": int(r[1])} for r in broken_rows],
        )

    asia_false_t, asia_broken_t = fetch_false_broken_times("asia", "19:30", "02:30", wrap=True)
    lon_false_t,  lon_broken_t  = fetch_false_broken_times("lon",  "03:30", "07:30", wrap=False)
    ny1_false_t,  ny1_broken_t  = fetch_false_broken_times("ny1",  "08:30", "11:30", wrap=False)
    ny2_false_t,  ny2_broken_t  = fetch_false_broken_times("ny2",  "12:30", "16:15", wrap=False)

    # ── Base rates ────────────────────────────────────────────────────────────
    def sv_base_rates(sess):
        lc = f"{sess}_long"; tc = f"{sess}_true"; bc = f"{sess}_broken"
        row = con.execute(f"""
            SELECT
                ROUND(AVG(CAST({lc} AS DOUBLE))*100, 2),
                ROUND(AVG(CAST({tc} AS DOUBLE))*100, 2),
                ROUND(AVG(CAST({bc} AS DOUBLE))*100, 2),
                COUNT(*) FILTER (WHERE {lc} IS NOT NULL)
            FROM session_vars
        """).fetchone()
        return {"long_pct": row[0] or 0, "true_pct": row[1] or 0,
                "broken_pct": row[2] or 0, "n": int(row[3] or 0)}

    # ── Conditional: given Asia+London → NY1/NY2 outcomes ───────────────────
    sv_combos = con.execute("""
        SELECT
            asia_long, asia_true, asia_broken,
            lon_long,  lon_true,  lon_broken,
            ny1_long,  ny1_true,  ny1_broken,
            ny2_long,  ny2_true,  ny2_broken,
            COUNT(*) AS n
        FROM session_vars
        WHERE asia_long IS NOT NULL AND lon_long IS NOT NULL
          AND ny1_long  IS NOT NULL AND ny2_long IS NOT NULL
        GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12
        ORDER BY n DESC
    """).fetchall()

    # ── DOW breakdown for session vars ──────────────────────────────────────────
    # Add day-of-week to session_vars table using triplets_classed (C3 date)
    sv_dow = {}
    for dow_name, dow_num in [("Mon",1),("Tue",2),("Wed",3),("Thu",4),("Fri",5)]:
        def sv_base_dow(sess, dn):
            lc = f"{sess}_long"; tc = f"{sess}_true"; bc = f"{sess}_broken"
            row = con.execute(f"""
                SELECT
                    ROUND(AVG(CAST({lc} AS DOUBLE))*100, 2),
                    ROUND(AVG(CAST({tc} AS DOUBLE))*100, 2),
                    ROUND(AVG(CAST({bc} AS DOUBLE))*100, 2),
                    COUNT(*) FILTER (WHERE {lc} IS NOT NULL)
                FROM session_vars
                WHERE EXTRACT(DOW FROM date)::INT = {dn}
            """).fetchone()
            return {"long_pct": row[0] or 0, "true_pct": row[1] or 0,
                    "broken_pct": row[2] or 0, "n": int(row[3] or 0)}

        def ft_dow(sess, var_start, var_end, wrap, dn):
            fc_hi  = f"{sess}_fc_hi"
            fc_lo  = f"{sess}_fc_lo"
            fc_mid = f"{sess}_fc_mid"
            lc     = f"{sess}_long"
            tc     = f"{sess}_true"
            vf = f"(b.t15 >= '{var_start}' OR b.t15 < '{var_end}')" if wrap else f"b.t15 >= '{var_start}' AND b.t15 < '{var_end}'"
            bf = f"b.t15 >= '{var_end}' AND b.t15 < '16:30'" if wrap else f"b.t15 >= '{var_end}'"
            fr = con.execute(f"""
                WITH fb AS (
                    SELECT sv.date, MIN(b.t15) FILTER (
                        WHERE {vf} AND sv.{tc}=FALSE
                        AND ((sv.{lc}=TRUE AND b.bar_low<sv.{fc_lo}) OR (sv.{lc}=FALSE AND b.bar_high>sv.{fc_hi}))
                    ) AS false_t
                    FROM session_vars sv JOIN bars_15m b ON b.date=sv.date
                    WHERE sv.{lc} IS NOT NULL AND sv.{tc}=FALSE
                      AND EXTRACT(DOW FROM sv.date)::INT={dn}
                    GROUP BY sv.date
                )
                SELECT CAST(false_t AS VARCHAR), COUNT(*) FROM fb WHERE false_t IS NOT NULL
                GROUP BY false_t ORDER BY false_t
            """).fetchall()
            br = con.execute(f"""
                WITH bb AS (
                    SELECT sv.date, MIN(b.t15) FILTER (
                        WHERE {bf} AND b.bar_high>=sv.{fc_mid} AND b.bar_low<=sv.{fc_mid}
                    ) AS broken_t
                    FROM session_vars sv JOIN bars_15m b ON b.date=sv.date
                    WHERE sv.{lc} IS NOT NULL AND sv.{fc_mid} IS NOT NULL
                      AND EXTRACT(DOW FROM sv.date)::INT={dn}
                    GROUP BY sv.date
                )
                SELECT CAST(broken_t AS VARCHAR), COUNT(*) FROM bb WHERE broken_t IS NOT NULL
                GROUP BY broken_t ORDER BY broken_t
            """).fetchall()
            return (
                [{"t": str(r[0])[:5], "n": int(r[1])} for r in fr],
                [{"t": str(r[0])[:5], "n": int(r[1])} for r in br],
            )

        dn = dow_num
        a_ft, a_bt = ft_dow("asia","19:30","02:30",True,dn)
        l_ft, l_bt = ft_dow("lon","03:30","07:30",False,dn)
        n1_ft, n1_bt = ft_dow("ny1","08:30","11:30",False,dn)
        n2_ft, n2_bt = ft_dow("ny2","12:30","16:15",False,dn)
        sv_dow[dow_name] = {
            "asia": {**sv_base_dow("asia",dn), "false_times": a_ft,  "broken_times": a_bt},
            "lon":  {**sv_base_dow("lon",dn),  "false_times": l_ft,  "broken_times": l_bt},
            "ny1":  {**sv_base_dow("ny1",dn),  "false_times": n1_ft, "broken_times": n1_bt},
            "ny2":  {**sv_base_dow("ny2",dn),  "false_times": n2_ft, "broken_times": n2_bt},
        }

    result["session_vars"] = {
        "asia": {**sv_base_rates("asia"), "false_times": asia_false_t, "broken_times": asia_broken_t},
        "lon":  {**sv_base_rates("lon"),  "false_times": lon_false_t,  "broken_times": lon_broken_t},
        "ny1":  {**sv_base_rates("ny1"),  "false_times": ny1_false_t,  "broken_times": ny1_broken_t},
        "ny2":  {**sv_base_rates("ny2"),  "false_times": ny2_false_t,  "broken_times": ny2_broken_t},
        "by_dow": sv_dow,
        "combos": [
            {"asia_long": r[0], "asia_true": r[1], "asia_broken": r[2],
             "lon_long":  r[3], "lon_true":  r[4], "lon_broken":  r[5],
             "ny1_long":  r[6], "ny1_true":  r[7], "ny1_broken":  r[8],
             "ny2_long":  r[9], "ny2_true":  r[10], "ny2_broken": r[11],
             "n": int(r[12])}
            for r in sv_combos
        ],
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 3 — HOD/LOD TIMING (15-min resolution, filterable by session vars)
    # HOD = first 15m bar whose high equals the day's high
    # LOD = first 15m bar whose low equals the day's low
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] HOD/LOD timing…")

    con.execute("""
        CREATE OR REPLACE TEMP TABLE hod_lod AS
        WITH day_ext AS (
            SELECT date, MAX(bar_high) AS day_high, MIN(bar_low) AS day_low
            FROM bars_15m GROUP BY date
        ),
        hod_bar AS (
            SELECT b.date, MIN(b.t15) AS hod_t
            FROM bars_15m b JOIN day_ext e ON e.date=b.date AND b.bar_high=e.day_high
            GROUP BY b.date
        ),
        lod_bar AS (
            SELECT b.date, MIN(b.t15) AS lod_t
            FROM bars_15m b JOIN day_ext e ON e.date=b.date AND b.bar_low=e.day_low
            GROUP BY b.date
        )
        SELECT h.date, h.hod_t, l.lod_t
        FROM hod_bar h JOIN lod_bar l ON l.date=h.date
    """)

    # Base histograms (all days)
    hod_base = con.execute("""
        SELECT CAST(hod_t AS VARCHAR) AS hod_t, COUNT(*) AS n FROM hod_lod
        WHERE hod_t IS NOT NULL GROUP BY hod_t ORDER BY hod_t
    """).fetchall()
    lod_base = con.execute("""
        SELECT CAST(lod_t AS VARCHAR) AS lod_t, COUNT(*) AS n FROM hod_lod
        WHERE lod_t IS NOT NULL GROUP BY lod_t ORDER BY lod_t
    """).fetchall()

    # HOD/LOD by Asia+London session variable combo (for conditional filtering)
    hod_by_sv = con.execute("""
        SELECT sv.asia_long, sv.asia_true, sv.lon_long, sv.lon_true,
               CAST(hl.hod_t AS VARCHAR) AS hod_t, COUNT(*) AS n
        FROM hod_lod hl JOIN session_vars sv ON sv.date=hl.date
        WHERE sv.asia_long IS NOT NULL AND sv.lon_long IS NOT NULL
          AND hl.hod_t IS NOT NULL
        GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5
    """).fetchall()
    lod_by_sv = con.execute("""
        SELECT sv.asia_long, sv.asia_true, sv.lon_long, sv.lon_true,
               CAST(hl.lod_t AS VARCHAR) AS lod_t, COUNT(*) AS n
        FROM hod_lod hl JOIN session_vars sv ON sv.date=hl.date
        WHERE sv.asia_long IS NOT NULL AND sv.lon_long IS NOT NULL
          AND hl.lod_t IS NOT NULL
        GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5
    """).fetchall()

    def mode_median_t(rows, t_key="t"):
        if not rows: return None, None
        total = sum(r["n"] for r in rows)
        mode  = max(rows, key=lambda r: r["n"])[t_key]
        cum = 0
        median = rows[0][t_key]
        for r in rows:
            cum += r["n"]
            if cum >= total / 2:
                median = r[t_key]
                break
        return mode, median

    hod_rows = [{"t": str(r[0])[:5], "n": int(r[1])} for r in hod_base if r[0]]
    lod_rows = [{"t": str(r[0])[:5], "n": int(r[1])} for r in lod_base if r[0]]
    hod_mode, hod_median = mode_median_t(hod_rows)
    lod_mode, lod_median = mode_median_t(lod_rows)

    result["hod_lod"] = {
        "hod_hist":   hod_rows,
        "lod_hist":   lod_rows,
        "hod_mode":   hod_mode,
        "hod_median": hod_median,
        "lod_mode":   lod_mode,
        "lod_median": lod_median,
        "hod_by_sv":  [{"asia_long": bool(r[0]), "asia_true": bool(r[1]),
                        "lon_long": bool(r[2]),  "lon_true": bool(r[3]),
                        "t": str(r[4])[:5], "n": int(r[5])} for r in hod_by_sv if r[4]],
        "lod_by_sv":  [{"asia_long": bool(r[0]), "asia_true": bool(r[1]),
                        "lon_long": bool(r[2]),  "lon_true": bool(r[3]),
                        "t": str(r[4])[:5], "n": int(r[5])} for r in lod_by_sv if r[4]],
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 4 — OU LINE HIT TIMING (15-min histograms)
    # OU = midline of FC range. "When does each OU get hit?"
    # P12 High/Mid/Low hit timing also computed here (same pattern).
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] OU / level hit timing…")

    # Build a level-hit table: for each key level on each day, first 15m bar that tags it
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE level_hits AS
        WITH levels AS (
            SELECT
                sv.date,
                sv.asia_fc_mid  AS asia_ou,
                sv.lon_fc_mid   AS lon_ou,
                sv.ny1_fc_mid   AS ny1_ou,
                sv.ny2_fc_mid   AS ny2_ou,
                -- P12 = prior 12-hour session high/mid/low (18:00–06:00 previous night)
                -- approximate as prior-day-night = this date 00:00–06:00 (already in data)
                NULL::DOUBLE     AS p12_hi,
                NULL::DOUBLE     AS p12_mid,
                NULL::DOUBLE     AS p12_lo
            FROM session_vars sv
        )
        SELECT
            l.date,
            b.t15,
            CASE WHEN l.asia_ou IS NOT NULL
                  AND b.bar_high >= l.asia_ou AND b.bar_low <= l.asia_ou THEN 1 ELSE 0 END AS asia_ou_hit,
            CASE WHEN l.lon_ou IS NOT NULL
                  AND b.bar_high >= l.lon_ou  AND b.bar_low <= l.lon_ou  THEN 1 ELSE 0 END AS lon_ou_hit,
            CASE WHEN l.ny1_ou IS NOT NULL
                  AND b.bar_high >= l.ny1_ou  AND b.bar_low <= l.ny1_ou  THEN 1 ELSE 0 END AS ny1_ou_hit,
            CASE WHEN l.ny2_ou IS NOT NULL
                  AND b.bar_high >= l.ny2_ou  AND b.bar_low <= l.ny2_ou  THEN 1 ELSE 0 END AS ny2_ou_hit
        FROM levels l JOIN bars_15m b ON b.date = l.date
    """)

    def ou_first_hit_hist(hit_col, min_t=None, max_t=None):
        """First bar where OU was hit, histogram by t15."""
        t_filter = ""
        if min_t: t_filter += f" AND t15 >= '{min_t}'"
        if max_t: t_filter += f" AND t15 <  '{max_t}'"
        rows = con.execute(f"""
            WITH first_hits AS (
                SELECT date, MIN(t15) AS first_t
                FROM level_hits
                WHERE {hit_col} = 1 {t_filter}
                GROUP BY date
            )
            SELECT CAST(first_t AS VARCHAR) AS first_t, COUNT(*) AS n
            FROM first_hits WHERE first_t IS NOT NULL
            GROUP BY first_t ORDER BY first_t
        """).fetchall()
        return [{"t": str(r[0])[:5], "n": int(r[1])} for r in rows if r[0]]

    def ou_hit_rate(hit_col):
        row = con.execute(f"""
            SELECT
                COUNT(DISTINCT date) FILTER (WHERE {hit_col}=1) * 100.0
                / NULLIF(COUNT(DISTINCT date), 0)
            FROM level_hits
        """).fetchone()
        return round(float(row[0] or 0), 1)

    asia_ou_hist = ou_first_hit_hist("asia_ou_hit")
    lon_ou_hist  = ou_first_hit_hist("lon_ou_hit")
    ny1_ou_hist  = ou_first_hit_hist("ny1_ou_hit")
    ny2_ou_hist  = ou_first_hit_hist("ny2_ou_hit")

    asia_ou_mode, asia_ou_median = mode_median_t(asia_ou_hist)
    lon_ou_mode,  lon_ou_median  = mode_median_t(lon_ou_hist)
    ny1_ou_mode,  ny1_ou_median  = mode_median_t(ny1_ou_hist)

    result["ou_lines"] = {
        "asia": {"hit_rate": ou_hit_rate("asia_ou_hit"), "hist": asia_ou_hist,
                 "mode": asia_ou_mode, "median": asia_ou_median},
        "lon":  {"hit_rate": ou_hit_rate("lon_ou_hit"),  "hist": lon_ou_hist,
                 "mode": lon_ou_mode,  "median": lon_ou_median},
        "ny1":  {"hit_rate": ou_hit_rate("ny1_ou_hit"),  "hist": ny1_ou_hist,
                 "mode": ny1_ou_mode,  "median": ny1_ou_median},
        "ny2":  {"hit_rate": ou_hit_rate("ny2_ou_hit"),  "hist": ny2_ou_hist},
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 5 — P12 LEVELS + DAILY KEY LEVELS (hit rate + timing histograms)
    # P12 = prior 12-hour session (18:00–06:00 day before)
    # Each level: hit rate % + 15-min histogram of WHEN it gets hit during RTH
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] P12 and key levels…")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE p12_levels AS
        WITH night AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                MAX(high) FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '18:00'
                                   OR   timezone('America/New_York', timestamp)::TIME <  '06:00') AS p12_hi,
                MIN(low)  FILTER (WHERE timezone('America/New_York', timestamp)::TIME >= '18:00'
                                   OR   timezone('America/New_York', timestamp)::TIME <  '06:00') AS p12_lo
            FROM {table}
            GROUP BY 1
        )
        SELECT
            date,
            p12_hi,
            p12_lo,
            (p12_hi + p12_lo) / 2.0 AS p12_mid,
            -- 6:00–9:30 pre-RTH range
            LAG(p12_hi,  1) OVER (ORDER BY date) AS prev_p12_hi,
            LAG(p12_lo,  1) OVER (ORDER BY date) AS prev_p12_lo,
            LAG((p12_hi+p12_lo)/2.0, 1) OVER (ORDER BY date) AS prev_p12_mid
        FROM night
    """)

    def level_hit_timing(level_expr, label, min_t='06:00', max_t='16:30', overnight=False):
        """Hit rate + 15-min histogram for a price level.
        overnight=True uses OR logic for wrap-around windows (e.g. 18:00–06:00).
        level_expr uses alias 'p' e.g. 'p.p12_hi' — strip it for standalone subqueries.
        """
        bare = level_expr.replace("p.", "")
        # Build time filter — overnight ranges wrap past midnight so need OR
        if overnight:
            tf = f"(b.t15 >= '{min_t}' OR b.t15 < '{max_t}')"
        else:
            tf = f"(b.t15 >= '{min_t}' AND b.t15 < '{max_t}')"
        rows = con.execute(f"""
            WITH tagged AS (
                SELECT
                    p.date,
                    MIN(b.t15) AS first_hit_t
                FROM p12_levels p
                JOIN bars_15m b ON b.date = p.date
                WHERE {tf}
                  AND b.bar_high >= ({level_expr})
                  AND b.bar_low  <= ({level_expr})
                  AND ({level_expr}) IS NOT NULL
                GROUP BY p.date
            ),
            all_days AS (SELECT DISTINCT date FROM p12_levels WHERE {bare} IS NOT NULL)
            SELECT
                CAST(t.first_hit_t AS VARCHAR) AS first_hit_t,
                COUNT(*) AS n,
                COUNT(*) * 100.0 / (SELECT COUNT(*) FROM all_days) AS hit_pct
            FROM tagged t
            GROUP BY t.first_hit_t
            ORDER BY t.first_hit_t
        """).fetchall()
        total_hit = con.execute(f"""
            SELECT COUNT(DISTINCT p.date) * 100.0
                   / NULLIF((SELECT COUNT(DISTINCT date) FROM p12_levels WHERE {bare} IS NOT NULL), 0)
            FROM p12_levels p
            JOIN bars_15m b ON b.date=p.date
            WHERE {tf}
              AND b.bar_high >= ({level_expr}) AND b.bar_low <= ({level_expr})
              AND ({level_expr}) IS NOT NULL
        """).fetchone()[0]
        hist = [{"t": str(r[0])[:5], "n": int(r[1])} for r in rows if r[0]]
        mode_t, med_t = mode_median_t(hist)
        return {
            "label": label,
            "hit_rate": round(float(total_hit or 0), 1),
            "hist": hist,
            "mode": mode_t,
            "median": med_t,
        }

    result["p12"] = {
        "p12_hi":  level_hit_timing("p.p12_hi",       "P12 HIGH",  min_t='06:00', max_t='16:30'),
        "p12_mid": level_hit_timing("p.p12_mid",      "P12 MID",   min_t='06:00', max_t='16:30'),
        "p12_lo":  level_hit_timing("p.p12_lo",       "P12 LOW",   min_t='06:00', max_t='16:30'),
        "prev_hi": level_hit_timing("p.prev_p12_hi",  "PREV HIGH", min_t='18:00', max_t='06:00', overnight=True),
        "prev_mid":level_hit_timing("p.prev_p12_mid", "PREV MID",  min_t='18:00', max_t='06:00', overnight=True),
        "prev_lo": level_hit_timing("p.prev_p12_lo",  "PREV LOW",  min_t='18:00', max_t='06:00', overnight=True),
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 6 — REALIZED VOLATILITY (daily range + session distributions)
    # Matches "Daily Low Distribution" / "Daily High Distribution" in profiler:
    #   x-axis = range as % of ref_price in bp buckets
    #   y-axis = % of days that fell in that bucket
    # Also: 10-day median + quarterly median per session
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Realized volatility…")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE session_ranges AS
        SELECT
            CAST(timezone('America/New_York', r.timestamp) AS DATE) AS date,
            -- Use MAX(CASE)/MIN(CASE) instead of aggregate FILTER on expressions
            MAX(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '09:30'
                      AND timezone('America/New_York', r.timestamp)::TIME <= '16:15'
                     THEN r.high END)
            - MIN(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '09:30'
                        AND timezone('America/New_York', r.timestamp)::TIME <= '16:15'
                       THEN r.low END) AS rth_range,
            MAX(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '18:00'
                      OR  timezone('America/New_York', r.timestamp)::TIME <  '02:30'
                     THEN r.high END)
            - MIN(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '18:00'
                        OR  timezone('America/New_York', r.timestamp)::TIME <  '02:30'
                       THEN r.low END) AS asia_range,
            MAX(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '02:30'
                      AND timezone('America/New_York', r.timestamp)::TIME <  '07:30'
                     THEN r.high END)
            - MIN(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '02:30'
                        AND timezone('America/New_York', r.timestamp)::TIME <  '07:30'
                       THEN r.low END) AS london_range,
            MAX(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '07:30'
                      AND timezone('America/New_York', r.timestamp)::TIME <  '11:30'
                     THEN r.high END)
            - MIN(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '07:30'
                        AND timezone('America/New_York', r.timestamp)::TIME <  '11:30'
                       THEN r.low END) AS ny1_range,
            MAX(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '11:30'
                      AND timezone('America/New_York', r.timestamp)::TIME <  '16:15'
                     THEN r.high END)
            - MIN(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '11:30'
                        AND timezone('America/New_York', r.timestamp)::TIME <  '16:15'
                       THEN r.low END) AS ny2_range,
            -- daily low/high as % delta from 9:30 open
            -- Use open_930 from intraday_feat (already reliably computed) instead of
            -- re-deriving from raw bars, which fails when no bar is timestamped exactly 09:30
            ROUND((
                MIN(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '09:30'
                           AND timezone('America/New_York', r.timestamp)::TIME <= '16:15'
                          THEN r.low END)
                - f.open_930
            ) / {ref_price} * 100, 2) AS pct_low,
            ROUND((
                MAX(CASE WHEN timezone('America/New_York', r.timestamp)::TIME >= '09:30'
                           AND timezone('America/New_York', r.timestamp)::TIME <= '16:15'
                          THEN r.high END)
                - f.open_930
            ) / {ref_price} * 100, 2) AS pct_high
        FROM {table} r
        JOIN intraday_feat f ON f.date = CAST(timezone('America/New_York', r.timestamp) AS DATE)
        GROUP BY CAST(timezone('America/New_York', r.timestamp) AS DATE), f.open_930
    """)

    ten_row = con.execute("""
        SELECT MEDIAN(rth_range), MEDIAN(asia_range), MEDIAN(london_range),
               MEDIAN(ny1_range), MEDIAN(ny2_range)
        FROM (SELECT * FROM session_ranges WHERE rth_range IS NOT NULL ORDER BY date DESC LIMIT 10)
    """).fetchone()

    q_row = con.execute("""
        SELECT MEDIAN(rth_range), MEDIAN(asia_range), MEDIAN(london_range),
               MEDIAN(ny1_range), MEDIAN(ny2_range)
        FROM session_ranges
        WHERE rth_range IS NOT NULL
          AND date >= (SELECT MAX(date) - INTERVAL '90 days' FROM session_ranges)
    """).fetchone()

    # Distribution histograms: bucket pct_low (negative) and pct_high (positive) in 0.1% buckets
    # 0.5% was too coarse — e.g. NQ -50pts = -0.24% rounds to 0.0% giving false mode=0
    def range_dist(col):
        rows = con.execute(f"""
            SELECT ROUND({col} / 0.1) * 0.1 AS bucket, COUNT(*) AS n
            FROM session_ranges
            WHERE {col} IS NOT NULL
            GROUP BY bucket ORDER BY bucket
        """).fetchall()
        total = sum(r[1] for r in rows)
        return [{"x": float(r[0]), "pct": round(r[1]/total*100, 1)} for r in rows]

    range_hist = con.execute("""
        SELECT CAST(date AS VARCHAR), ROUND(rth_range, 2)
        FROM session_ranges WHERE rth_range IS NOT NULL
        ORDER BY date DESC LIMIT 30
    """).fetchall()

    result["volatility"] = {
        "ten_day":  {"rth": round(float(ten_row[0] or 0), 2), "asia": round(float(ten_row[1] or 0), 2),
                     "london": round(float(ten_row[2] or 0), 2), "ny1": round(float(ten_row[3] or 0), 2),
                     "ny2": round(float(ten_row[4] or 0), 2)},
        "quarterly":{"rth": round(float(q_row[0] or 0), 2),   "asia": round(float(q_row[1] or 0), 2),
                     "london": round(float(q_row[2] or 0), 2), "ny1": round(float(q_row[3] or 0), 2),
                     "ny2": round(float(q_row[4] or 0), 2)},
        "low_dist":  range_dist("pct_low"),
        "high_dist": range_dist("pct_high"),
        "range_history": [{"date": r[0], "range": float(r[1])} for r in range_hist],
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 7 — FOUR-STEP REVERSAL STATS
    # Bear reversal steps (price was up at 9:30, reversing down):
    #   S1: 10:00 bar breaks below 9:30 1m low
    #   S2: 10:00 bar breaks below 9:00 hour midpoint
    #   S3: 10:00 hour takes out 9:00 hourly low
    #   S4: Q1 of 10:00 (first 15min) puts in a lower high than 9:30 high
    # Bull reversal = mirror image
    # Score 0-4 → correlate with whether HOD/LOD was made in 9:30–10:30 window
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Four-step reversal…")

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE fsr AS
        WITH b AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                timezone('America/New_York', timestamp)::TIME AS t,
                EXTRACT(HOUR   FROM timezone('America/New_York', timestamp))::INT AS hr,
                EXTRACT(MINUTE FROM timezone('America/New_York', timestamp))::INT AS mn,
                high, low, open, close
            FROM {table}
        ),
        c930 AS (
            SELECT date,
                MAX(high) FILTER (WHERE hr=9 AND mn=30) AS h930,
                MIN(low)  FILTER (WHERE hr=9 AND mn=30) AS l930
            FROM b GROUP BY date
        ),
        h900 AS (
            SELECT date,
                MAX(high) FILTER (WHERE hr=9 AND mn<30) AS h900,
                MIN(low)  FILTER (WHERE hr=9 AND mn<30) AS l900,
                (MAX(high) FILTER (WHERE hr=9 AND mn<30)
               + MIN(low)  FILTER (WHERE hr=9 AND mn<30)) / 2.0 AS mid900
            FROM b GROUP BY date
        ),
        h1000 AS (
            SELECT date,
                MAX(high) AS h1000, MIN(low) AS l1000,
                MAX(high) FILTER (WHERE mn < 15) AS h1000_q1,
                MIN(low)  FILTER (WHERE mn < 15) AS l1000_q1
            FROM b WHERE hr=10 GROUP BY date
        ),
        pivot_window AS (
            SELECT date, MAX(high) AS pw_high, MIN(low) AS pw_low
            FROM b WHERE (hr=9 AND mn>=30) OR hr=10 GROUP BY date
        ),
        day_ext AS (
            SELECT date, MAX(high) AS dh, MIN(low) AS dl FROM b GROUP BY date
        )
        SELECT
            c.date,
            -- Bear steps
            CASE WHEN h10.l1000 < c.l930    THEN 1 ELSE 0 END AS bs1,
            CASE WHEN h10.l1000 < h9.mid900 THEN 1 ELSE 0 END AS bs2,
            CASE WHEN h10.l1000 < h9.l900   THEN 1 ELSE 0 END AS bs3,
            CASE WHEN h10.h1000_q1 < c.h930 THEN 1 ELSE 0 END AS bs4,
            -- Bull steps
            CASE WHEN h10.h1000 > c.h930    THEN 1 ELSE 0 END AS us1,
            CASE WHEN h10.h1000 > h9.mid900 THEN 1 ELSE 0 END AS us2,
            CASE WHEN h10.h1000 > h9.h900   THEN 1 ELSE 0 END AS us3,
            CASE WHEN h10.l1000_q1 > c.l930 THEN 1 ELSE 0 END AS us4,
            -- Pivot flag: HOD or LOD was made in 9:30–10:30 window
            CASE WHEN pw.pw_high >= de.dh * 0.9999 OR pw.pw_low <= de.dl * 1.0001 THEN 1 ELSE 0 END AS pivot_930_1030
        FROM c930 c
        JOIN h900 h9 ON h9.date=c.date
        JOIN h1000 h10 ON h10.date=c.date
        JOIN pivot_window pw ON pw.date=c.date
        JOIN day_ext de ON de.date=c.date
        WHERE c.l930 IS NOT NULL AND h9.mid900 IS NOT NULL AND h10.l1000 IS NOT NULL
    """)

    def score_dist(s1,s2,s3,s4):
        rows = con.execute(f"""
            SELECT ({s1}+{s2}+{s3}+{s4}) AS score, COUNT(*) AS n,
                   ROUND(AVG(CAST(pivot_930_1030 AS DOUBLE))*100,1) AS pivot_pct
            FROM fsr GROUP BY score ORDER BY score
        """).fetchall()
        return [{"score": int(r[0]), "n": int(r[1]), "pivot_pct": r[2]} for r in rows]

    step_rates = con.execute("""
        SELECT ROUND(AVG(CAST(bs1 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(bs2 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(bs3 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(bs4 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(us1 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(us2 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(us3 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(us4 AS DOUBLE))*100,1),
               ROUND(AVG(CAST(pivot_930_1030 AS DOUBLE))*100,1),
               COUNT(*) FROM fsr
    """).fetchone()

    result["four_step"] = {
        "bear_scores": score_dist("bs1","bs2","bs3","bs4"),
        "bull_scores": score_dist("us1","us2","us3","us4"),
        "step_rates": {
            "bs1": step_rates[0], "bs2": step_rates[1], "bs3": step_rates[2], "bs4": step_rates[3],
            "us1": step_rates[4], "us2": step_rates[5], "us3": step_rates[6], "us4": step_rates[7],
            "pivot_pct": step_rates[8], "n": int(step_rates[9]),
        },
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 8 — MAE/MFE TIMING HISTOGRAMS
    # When during the 06:00–16:30 session is the adverse extreme (MAE) and
    # favorable extreme (MFE) first established?
    #
    # For bull C3: MAE = session low (going against you), MFE = session high
    # For bear C3: MAE = session high (going against you), MFE = session low
    #
    # Key insight (from bootcamp): the mode is the most common TIME the extreme
    # occurs.  Shallow MAE = low is put in early (before median), reversal quick.
    # Deep check mark = price reaches the median-MAE price zone THEN reverses.
    # Mickey's chart: red ticks = MAE time, green ticks = MFE time,
    # dotted lines = median MAE time and median MFE time.
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] MAE/MFE timing histograms…")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE mae_timing_tbl AS
        WITH
        -- Session extremes per day in the 06:00–16:30 window
        sess_ext AS (
            SELECT date,
                   MIN(bar_low)  FILTER (WHERE t15 >= '06:00' AND t15 < '16:30') AS sess_low,
                   MAX(bar_high) FILTER (WHERE t15 >= '06:00' AND t15 < '16:30') AS sess_high
            FROM bars_15m
            GROUP BY date
        ),
        -- First 15m bar where bar_low touches the session low
        first_low AS (
            SELECT b.date, MIN(b.t15) AS low_t
            FROM bars_15m b JOIN sess_ext e ON e.date = b.date
            WHERE b.t15 >= '06:00' AND b.t15 < '16:30'
              AND b.bar_low <= e.sess_low + 0.01
            GROUP BY b.date
        ),
        -- First 15m bar where bar_high touches the session high
        first_high AS (
            SELECT b.date, MIN(b.t15) AS high_t
            FROM bars_15m b JOIN sess_ext e ON e.date = b.date
            WHERE b.t15 >= '06:00' AND b.t15 < '16:30'
              AND b.bar_high >= e.sess_high - 0.01
            GROUP BY b.date
        )
        SELECT
            tc.c3_date                        AS date,
            tc.c3_bull                        AS bull,
            EXTRACT(DOW FROM tc.c3_date)::INT AS dow_num,
            dc.classification                 AS cls,
            fl.low_t,
            fh.high_t,
            -- direction-adjusted extremes
            CASE WHEN tc.c3_bull THEN fl.low_t  ELSE fh.high_t END AS mae_t,
            CASE WHEN tc.c3_bull THEN fh.high_t ELSE fl.low_t  END AS mfe_t
        FROM triplets_classed tc
        JOIN first_low  fl ON fl.date = tc.c3_date
        JOIN first_high fh ON fh.date = tc.c3_date
        JOIN day_class  dc ON dc.date = tc.c3_date
        WHERE fl.low_t IS NOT NULL AND fh.high_t IS NOT NULL
    """)

    def timing_hist_q(where="TRUE"):
        def hist(col):
            rows = con.execute(f"""
                SELECT CAST({col} AS VARCHAR) AS t, COUNT(*) AS n
                FROM mae_timing_tbl WHERE ({where}) AND {col} IS NOT NULL
                GROUP BY {col} ORDER BY {col}
            """).fetchall()
            h = [{"t": str(r[0])[:5], "n": int(r[1])} for r in rows if r[0]]
            m, med = mode_median_t(h)
            return {"hist": h, "mode": m, "median": med}
        n = con.execute(f"SELECT COUNT(*) FROM mae_timing_tbl WHERE ({where})").fetchone()[0]
        return {"mae": hist("mae_t"), "mfe": hist("mfe_t"), "n": int(n)}

    mt = {
        "all":  timing_hist_q(),
        "bull": timing_hist_q("bull = TRUE"),
        "bear": timing_hist_q("bull = FALSE"),
    }
    for d, n in DOW_NUM.items():
        mt[d]          = timing_hist_q(f"dow_num = {n}")
        mt[f"{d}_bull"] = timing_hist_q(f"dow_num = {n} AND bull = TRUE")
        mt[f"{d}_bear"] = timing_hist_q(f"dow_num = {n} AND bull = FALSE")
    result["mae_timing"] = mt

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 9 — SESSION EXPANSION HISTORY
    # For the last 20 trading days: each session's actual range vs its quarterly
    # median (= 100%).  Values above 100% = overspent (volatile), below = quiet.
    # Used to read the "checkbook" — if Asia overspent, does London follow?
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Session expansion history…")
    exp_rows = con.execute("""
        WITH q_med AS (
            SELECT
                MEDIAN(asia_range)   AS q_asia,
                MEDIAN(london_range) AS q_lon,
                MEDIAN(ny1_range)    AS q_ny1,
                MEDIAN(ny2_range)    AS q_ny2,
                MEDIAN(rth_range)    AS q_rth
            FROM session_ranges
            WHERE rth_range IS NOT NULL
              AND date >= (SELECT MAX(date) - INTERVAL '90 days' FROM session_ranges)
        )
        SELECT
            CAST(sr.date AS VARCHAR)  AS dt,
            CASE WHEN q.q_asia > 0  THEN ROUND(sr.asia_range   / q.q_asia  * 100) ELSE NULL END,
            CASE WHEN q.q_lon  > 0  THEN ROUND(sr.london_range / q.q_lon   * 100) ELSE NULL END,
            CASE WHEN q.q_ny1  > 0  THEN ROUND(sr.ny1_range    / q.q_ny1   * 100) ELSE NULL END,
            CASE WHEN q.q_ny2  > 0  THEN ROUND(sr.ny2_range    / q.q_ny2   * 100) ELSE NULL END,
            CASE WHEN q.q_rth  > 0  THEN ROUND(sr.rth_range    / q.q_rth   * 100) ELSE NULL END,
            ROUND(sr.asia_range,   2), ROUND(sr.london_range, 2),
            ROUND(sr.ny1_range,    2), ROUND(sr.ny2_range,    2), ROUND(sr.rth_range, 2)
        FROM session_ranges sr, q_med q
        WHERE sr.rth_range IS NOT NULL
        ORDER BY sr.date DESC LIMIT 20
    """).fetchall()
    result["volatility"]["expansion_history"] = [
        {"date": r[0], "asia": int(r[1] or 0), "lon": int(r[2] or 0),
         "ny1":  int(r[3] or 0), "ny2": int(r[4] or 0), "rth": int(r[5] or 0),
         "asia_r": float(r[6] or 0), "lon_r": float(r[7] or 0),
         "ny1_r":  float(r[8] or 0), "ny2_r": float(r[9] or 0), "rth_r": float(r[10] or 0)}
        for r in exp_rows
    ]

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 10 — WEEKLY STRUCTURE
    # Weekly 5-period SMA, Sunday FC range (18:00–19:30), Tuesday range (9:30–10:30)
    # Basis-point hit rates from weekly SMA5: 98% touch, 86% at 0.5%, etc.
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Weekly structure…")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE weekly_bars AS
        SELECT
            CAST(DATE_TRUNC('week', date) AS DATE) AS wk_start,
            MAX(date)                              AS wk_end,
            FIRST(open  ORDER BY date)             AS wk_open,
            MAX(high)                              AS wk_high,
            MIN(low)                               AS wk_low,
            LAST(close  ORDER BY date)             AS wk_close
        FROM daily
        GROUP BY DATE_TRUNC('week', date)
        HAVING COUNT(*) >= 3
        ORDER BY wk_start
    """)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE weekly_sma AS
        SELECT *,
            AVG(wk_close) OVER (
                ORDER BY wk_start
                ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
            ) AS sma5
        FROM weekly_bars
    """)
    # Sunday Asia FC (18:00–19:30 ET Sunday) — belongs to the following week
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE weekly_sunday AS
        WITH sun AS (
            SELECT
                CAST(CAST(timezone('America/New_York', timestamp) AS DATE) + INTERVAL '1 day' AS DATE) AS wk_start,
                high, low
            FROM {table}
            WHERE EXTRACT(DOW FROM CAST(timezone('America/New_York', timestamp) AS DATE))::INT = 0
              AND timezone('America/New_York', timestamp)::TIME >= '18:00'
              AND timezone('America/New_York', timestamp)::TIME <  '19:30'
        )
        SELECT wk_start, MAX(high) AS sun_hi, MIN(low) AS sun_lo
        FROM sun GROUP BY wk_start
    """)
    # Tuesday 9:30–10:30 weekly confirmation range
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE weekly_tuesday AS
        WITH tue AS (
            SELECT
                CAST(DATE_TRUNC('week', CAST(timezone('America/New_York', timestamp) AS DATE)) AS DATE) AS wk_start,
                high, low
            FROM {table}
            WHERE EXTRACT(DOW FROM CAST(timezone('America/New_York', timestamp) AS DATE))::INT = 2
              AND timezone('America/New_York', timestamp)::TIME >= '09:30'
              AND timezone('America/New_York', timestamp)::TIME <  '10:30'
        )
        SELECT wk_start, MAX(high) AS tue_hi, MIN(low) AS tue_lo
        FROM tue GROUP BY wk_start
    """)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE weekly_full AS
        SELECT
            ws.wk_start, ws.wk_end, ws.wk_open, ws.wk_high, ws.wk_low, ws.wk_close, ws.sma5,
            su.sun_hi, su.sun_lo,
            t.tue_hi, t.tue_lo,
            ROUND((ws.wk_high - ws.sma5) / {ref_price} * 10000) AS bp_above_sma5,
            ROUND((ws.sma5 - ws.wk_low)  / {ref_price} * 10000) AS bp_below_sma5,
            CAST(ws.wk_start AS VARCHAR) AS wk_start_str,
            CAST(ws.wk_end   AS VARCHAR) AS wk_end_str
        FROM weekly_sma ws
        LEFT JOIN weekly_sunday  su ON su.wk_start = ws.wk_start
        LEFT JOIN weekly_tuesday t  ON t.wk_start  = ws.wk_start
        WHERE ws.sma5 IS NOT NULL
        ORDER BY ws.wk_start
    """)

    # Basis-point hit rates from weekly SMA5
    bp_levels = [25, 50, 75, 100, 150, 200, 250, 300, 500]
    sma_hit_rates = []
    for bp in bp_levels:
        row = con.execute(f"""
            SELECT
                COUNT(*) FILTER (WHERE bp_above_sma5 >= {bp}) AS above_n,
                COUNT(*) FILTER (WHERE bp_below_sma5 >= {bp}) AS below_n,
                COUNT(*) FILTER (WHERE bp_above_sma5 >= {bp} OR bp_below_sma5 >= {bp}) AS either_n,
                COUNT(*) AS total
            FROM weekly_full
            WHERE bp_above_sma5 IS NOT NULL AND bp_below_sma5 IS NOT NULL
        """).fetchone()
        total = row[3] or 1
        sma_hit_rates.append({
            "bp": bp,
            "above_pct":  round(row[0]/total*100, 1),
            "below_pct":  round(row[1]/total*100, 1),
            "either_pct": round(row[2]/total*100, 1),
        })

    # Sunday range stats
    sun_row = con.execute(f"""
        SELECT
            COUNT(*) FILTER (WHERE sun_hi IS NOT NULL) AS n,
            ROUND(MEDIAN(sun_hi - sun_lo) / {ref_price} * 10000) AS med_range_bp,
            COUNT(*) FILTER (WHERE wk_high > sun_hi) * 100.0
              / NULLIF(COUNT(*) FILTER (WHERE sun_hi IS NOT NULL), 0) AS pct_broke_above,
            COUNT(*) FILTER (WHERE wk_low < sun_lo) * 100.0
              / NULLIF(COUNT(*) FILTER (WHERE sun_lo IS NOT NULL), 0) AS pct_broke_below
        FROM weekly_full
    """).fetchone()

    # Tuesday range stats
    tue_row = con.execute("""
        SELECT
            COUNT(*) FILTER (WHERE tue_hi IS NOT NULL) AS n,
            COUNT(*) FILTER (WHERE wk_high > tue_hi) * 100.0
              / NULLIF(COUNT(*) FILTER (WHERE tue_hi IS NOT NULL), 0) AS pct_broke_above,
            COUNT(*) FILTER (WHERE wk_low < tue_lo) * 100.0
              / NULLIF(COUNT(*) FILTER (WHERE tue_lo IS NOT NULL), 0) AS pct_broke_below
        FROM weekly_full
    """).fetchone()

    # Combined pattern: when both Sunday AND Tuesday agree (both long or both short)
    # what % of those weeks is the high/low of week locked in early?
    combo_row = con.execute("""
        SELECT
            -- Both above Sun hi AND above Tue hi = weekly bullish confirmation
            COUNT(*) FILTER (
                WHERE wk_open > sun_hi AND wk_open > tue_hi
            ) AS n_bull_confirm,
            COUNT(*) FILTER (
                WHERE wk_open > sun_hi AND wk_open > tue_hi
                  AND wk_low > sun_lo
            ) * 100.0
              / NULLIF(COUNT(*) FILTER (WHERE wk_open > sun_hi AND wk_open > tue_hi), 0)
            AS bull_held_lo_pct,
            COUNT(*) FILTER (
                WHERE wk_open < sun_lo AND wk_open < tue_lo
            ) AS n_bear_confirm,
            COUNT(*) FILTER (
                WHERE wk_open < sun_lo AND wk_open < tue_lo
                  AND wk_high < sun_hi
            ) * 100.0
              / NULLIF(COUNT(*) FILTER (WHERE wk_open < sun_lo AND wk_open < tue_lo), 0)
            AS bear_held_hi_pct
        FROM weekly_full
        WHERE sun_hi IS NOT NULL AND tue_hi IS NOT NULL
    """).fetchone()

    # Last 10 weeks for chart display
    recent_wks = con.execute(f"""
        SELECT wk_start_str, wk_end_str,
               ROUND(wk_open,2), ROUND(wk_high,2), ROUND(wk_low,2), ROUND(wk_close,2),
               ROUND(sma5,2),
               ROUND(sun_hi,2), ROUND(sun_lo,2), ROUND(tue_hi,2), ROUND(tue_lo,2),
               bp_above_sma5, bp_below_sma5
        FROM weekly_full ORDER BY wk_start DESC LIMIT 10
    """).fetchall()

    result["weekly"] = {
        "sma5_hit_rates": sma_hit_rates,
        "sun_stats": {
            "n":                int(sun_row[0] or 0),
            "med_range_bp":     int(sun_row[1] or 0),
            "pct_broke_above":  round(float(sun_row[2] or 0), 1),
            "pct_broke_below":  round(float(sun_row[3] or 0), 1),
        },
        "tue_stats": {
            "n":               int(tue_row[0] or 0),
            "pct_broke_above": round(float(tue_row[1] or 0), 1),
            "pct_broke_below": round(float(tue_row[2] or 0), 1),
        },
        "combo_confirm": {
            "bull_n":          int(combo_row[0] or 0),
            "bull_held_lo":    round(float(combo_row[1] or 0), 1),
            "bear_n":          int(combo_row[2] or 0),
            "bear_held_hi":    round(float(combo_row[3] or 0), 1),
        },
        "recent_weeks": [
            {"wk":   r[0], "wk_end": r[1],
             "open":  float(r[2] or 0),  "high":  float(r[3] or 0),
             "low":   float(r[4] or 0),  "close": float(r[5] or 0),
             "sma5":  float(r[6] or 0),
             "sun_hi": float(r[7]) if r[7] is not None else None,
             "sun_lo": float(r[8]) if r[8] is not None else None,
             "tue_hi": float(r[9]) if r[9] is not None else None,
             "tue_lo": float(r[10]) if r[10] is not None else None,
             "bp_above_sma5": int(r[11] or 0),
             "bp_below_sma5": int(r[12] or 0)}
            for r in recent_wks
        ],
    }

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 11 — P12 SCENARIOS
    # Classify the 06:00–09:30 pre-RTH price action relative to P12 hi/mid/lo
    # into Mickey's 5 canonical scenarios, then show what day type followed.
    #
    # Scenario 1: Bounced P12 mid → accepted ABOVE P12 hi by 9:00 (bullish)
    # Scenario 2: Traded below P12 lo → accepted ABOVE P12 mid by 9:00 (Carl Hungus)
    # Scenario 3: Rejected P12 hi → found footing on P12 mid → continued up
    # Scenario 4: Entire 06-09 range outside P12 range (above hi or below lo) → strong trend
    # Scenario 5a: Range swiped ALL P12 levels (big overnight range) → likely range day
    # Scenario 5b: Stayed within P12 range, ping-ponging P12 mid → shallow MAE or check mark
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] P12 scenarios…")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE p12_scenarios AS
        WITH pre AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                MAX(high) FILTER (
                    WHERE timezone('America/New_York', timestamp)::TIME >= '06:00'
                      AND timezone('America/New_York', timestamp)::TIME <  '09:30'
                ) AS pre_high,
                MIN(low) FILTER (
                    WHERE timezone('America/New_York', timestamp)::TIME >= '06:00'
                      AND timezone('America/New_York', timestamp)::TIME <  '09:30'
                ) AS pre_low,
                LAST(close ORDER BY timestamp) FILTER (
                    WHERE timezone('America/New_York', timestamp)::TIME >= '09:00'
                      AND timezone('America/New_York', timestamp)::TIME <  '09:30'
                ) AS pre_close
            FROM {table}
            GROUP BY 1
        )
        SELECT
            p12.date,
            p12.p12_hi, p12.p12_lo, p12.p12_mid,
            pre.pre_high, pre.pre_low, pre.pre_close,
            dc.classification AS cls,
            CASE
                -- S4: entire pre-RTH action is OUTSIDE P12 range (strong trend running)
                WHEN pre.pre_low  > p12.p12_hi THEN '4_bull'
                WHEN pre.pre_high < p12.p12_lo THEN '4_bear'
                -- S5a: swiped ALL P12 levels (high ≥ p12_hi AND low ≤ p12_lo)
                WHEN pre.pre_high >= p12.p12_hi AND pre.pre_low <= p12.p12_lo THEN '5a'
                -- S1: bounced P12 mid (went to or below it) then closed ABOVE P12 hi
                WHEN pre.pre_low <= p12.p12_mid AND pre.pre_close >= p12.p12_hi THEN '1_bull'
                WHEN pre.pre_high >= p12.p12_mid AND pre.pre_close <= p12.p12_lo THEN '1_bear'
                -- S2: went below P12 lo (or above P12 hi) then came back THROUGH P12 mid
                WHEN pre.pre_low < p12.p12_lo AND pre.pre_close > p12.p12_mid THEN '2_bull'
                WHEN pre.pre_high > p12.p12_hi AND pre.pre_close < p12.p12_mid THEN '2_bear'
                -- S3: tested P12 hi/lo, found footing at P12 mid, continued directionally
                WHEN pre.pre_high >= p12.p12_hi AND pre.pre_low >= p12.p12_lo
                     AND pre.pre_close > p12.p12_mid THEN '3_bull'
                WHEN pre.pre_low <= p12.p12_lo AND pre.pre_high <= p12.p12_hi
                     AND pre.pre_close < p12.p12_mid THEN '3_bear'
                -- S5b: stayed within P12 range, no resolution at hi or lo
                WHEN pre.pre_high < p12.p12_hi AND pre.pre_low > p12.p12_lo THEN '5b'
                ELSE 'other'
            END AS scenario
        FROM p12_levels p12
        JOIN pre      ON pre.date = p12.date
        JOIN day_class dc ON dc.date = p12.date
        WHERE p12.p12_hi IS NOT NULL AND p12.p12_lo IS NOT NULL
          AND pre.pre_high IS NOT NULL AND pre.pre_low IS NOT NULL
          AND (p12.p12_hi - p12.p12_lo) > 0
    """)

    sc_rows = con.execute("""
        SELECT scenario, COUNT(*) AS n,
               ROUND(AVG(CASE WHEN cls='Range1' THEN 1.0 ELSE 0.0 END)*100, 1) AS r1_pct,
               ROUND(AVG(CASE WHEN cls='DWP'    THEN 1.0 ELSE 0.0 END)*100, 1) AS dwp_pct,
               ROUND(AVG(CASE WHEN cls='DNP'    THEN 1.0 ELSE 0.0 END)*100, 1) AS dnp_pct,
               ROUND(AVG(CASE WHEN cls='Range2' THEN 1.0 ELSE 0.0 END)*100, 1) AS r2_pct
        FROM p12_scenarios
        GROUP BY scenario ORDER BY n DESC
    """).fetchall()
    total_sc = sum(r[1] for r in sc_rows)
    result["p12"]["scenarios"] = [
        {"scenario": r[0], "n": int(r[1]),
         "pct": round(r[1]/total_sc*100, 1) if total_sc else 0,
         "outcomes": {"Range1": float(r[2] or 0), "DWP": float(r[3] or 0),
                      "DNP": float(r[4] or 0), "Range2": float(r[5] or 0)}}
        for r in sc_rows
    ]


    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 12 — PRICE MODELS (per session, bull vs bear)
    # For each session, compute the average cumulative % price change from the
    # session open at each 15-min bar, split by whether the session was long or
    # short (bull/bear direction).
    # Result: "average shape" of price action in each session — the green line
    # shows where price goes on bull sessions, red on bear sessions.
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Price models…")

    SESSION_DEFS = [
        ("asia",   "18:00", "02:30", "asia_long"),
        ("lon",    "02:30", "07:30", "lon_long"),
        ("ny1",    "07:30", "11:30", "ny1_long"),
        ("ny2",    "11:30", "16:30", "ny2_long"),
    ]

    price_models = {}
    for sess_key, t_start, t_end, dir_col in SESSION_DEFS:
        overnight = t_start > t_end  # only Asia crosses midnight
        if overnight:
            tf_b    = f"(b.t15 >= '{t_start}' OR b.t15 < '{t_end}')"   # alias b
            tf_bare = f"(t15   >= '{t_start}' OR t15   < '{t_end}')"   # no alias
        else:
            tf_b    = f"(b.t15 >= '{t_start}' AND b.t15 < '{t_end}')"
            tf_bare = f"(t15   >= '{t_start}' AND t15   < '{t_end}')"

        rows_bull = con.execute(f"""
            WITH sess_open AS (
                SELECT sv.date,
                    FIRST(b.bar_open ORDER BY b.t15) AS s_open
                FROM session_vars sv
                JOIN bars_15m b ON b.date = sv.date
                WHERE sv.{dir_col} = TRUE
                  AND {tf_b}
                GROUP BY sv.date
            ),
            pct_moves AS (
                SELECT b.t15,
                    AVG((b.bar_close - so.s_open) / NULLIF(so.s_open, 0) * 100) AS avg_pct
                FROM bars_15m b
                JOIN sess_open so ON so.date = b.date
                WHERE {tf_b}
                GROUP BY b.t15
                ORDER BY b.t15
            )
            SELECT CAST(t15 AS VARCHAR), ROUND(avg_pct, 4) FROM pct_moves
        """).fetchall()

        rows_bear = con.execute(f"""
            WITH sess_open AS (
                SELECT sv.date,
                    FIRST(b.bar_open ORDER BY b.t15) AS s_open
                FROM session_vars sv
                JOIN bars_15m b ON b.date = sv.date
                WHERE sv.{dir_col} = FALSE
                  AND {tf_b}
                GROUP BY sv.date
            ),
            pct_moves AS (
                SELECT b.t15,
                    AVG((b.bar_close - so.s_open) / NULLIF(so.s_open, 0) * 100) AS avg_pct
                FROM bars_15m b
                JOIN sess_open so ON so.date = b.date
                WHERE {tf_b}
                GROUP BY b.t15
                ORDER BY b.t15
            )
            SELECT CAST(t15 AS VARCHAR), ROUND(avg_pct, 4) FROM pct_moves
        """).fetchall()

        price_models[sess_key] = {
            "bull": [{"t": str(r[0])[:5], "v": float(r[1] or 0)} for r in rows_bull if r[0]],
            "bear": [{"t": str(r[0])[:5], "v": float(r[1] or 0)} for r in rows_bear if r[0]],
        }

    result["price_models"] = price_models

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 13 — SESSION HIGH/LOW TIMES (per session)
    # For each session, when is the session high first established?
    # When is the session low first established?
    # Dual histogram: high time (green, positive) / low time (red, negative)
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Session high/low times…")

    sess_hl = {}
    for sess_key, t_start, t_end, dir_col in SESSION_DEFS:
        overnight = t_start > t_end
        if overnight:
            # tf0: no table alias (for bare FROM bars_15m WHERE ...)
            tf0 = f"(t15 >= '{t_start}' OR t15 < '{t_end}')"
            # tf: with alias b (for FROM bars_15m b WHERE ...)
            tf  = f"(b.t15 >= '{t_start}' OR b.t15 < '{t_end}')"
        else:
            tf0 = f"(t15 >= '{t_start}' AND t15 < '{t_end}')"
            tf  = f"(b.t15 >= '{t_start}' AND b.t15 < '{t_end}')"

        rows = con.execute(f"""
            WITH sess_ext AS (
                SELECT date,
                    MAX(bar_high) AS s_high,
                    MIN(bar_low)  AS s_low
                FROM bars_15m WHERE {tf0}
                GROUP BY date
            ),
            first_high AS (
                SELECT b.date, MIN(b.t15) AS high_t
                FROM bars_15m b JOIN sess_ext e ON e.date=b.date
                WHERE {tf} AND b.bar_high >= e.s_high - 0.01
                GROUP BY b.date
            ),
            first_low AS (
                SELECT b.date, MIN(b.t15) AS low_t
                FROM bars_15m b JOIN sess_ext e ON e.date=b.date
                WHERE {tf} AND b.bar_low <= e.s_low + 0.01
                GROUP BY b.date
            )
            SELECT
                CAST(fh.high_t AS VARCHAR) AS ht,
                COUNT(*) FILTER (WHERE fh.high_t IS NOT NULL) AS n_high,
                CAST(fl.low_t AS VARCHAR)  AS lt,
                COUNT(*) FILTER (WHERE fl.low_t IS NOT NULL) AS n_low
            FROM first_high fh
            FULL OUTER JOIN first_low fl ON fl.date = fh.date
            GROUP BY ht, lt
            ORDER BY COALESCE(ht, lt)
        """).fetchall()

        # Build separate high/low histograms then compute mode/median
        ht_map = {}
        lt_map = {}
        for r in rows:
            if r[0]: ht_map[str(r[0])[:5]] = ht_map.get(str(r[0])[:5], 0) + int(r[1] or 0)
            if r[2]: lt_map[str(r[2])[:5]] = lt_map.get(str(r[2])[:5], 0) + int(r[3] or 0)

        def hist_mode_median(m):
            h = [{"t": k, "n": v} for k, v in sorted(m.items())]
            if not h: return h, None, None
            mode = max(h, key=lambda x: x["n"])["t"]
            total = sum(x["n"] for x in h)
            cum = 0
            median = h[0]["t"]
            for x in h:
                cum += x["n"]
                if cum >= total / 2:
                    median = x["t"]
                    break
            return h, mode, median

        ht_hist, ht_mode, ht_med = hist_mode_median(ht_map)
        lt_hist, lt_mode, lt_med = hist_mode_median(lt_map)

        sess_hl[sess_key] = {
            "high": {"hist": ht_hist, "mode": ht_mode, "median": ht_med},
            "low":  {"hist": lt_hist, "mode": lt_mode, "median": lt_med},
        }

    result["sess_hl"] = sess_hl

    # ══════════════════════════════════════════════════════════════════════════
    # FEATURE 14 — HOURLY 09:30 TOUCHES + DWP PULLBACK HOUR
    # 09:30 Touches: per RTH hour (10–16), how many times does price cross back
    # through the 9:30 first-minute open price?
    # DWP Pullback: for DWP days, what hour sees the first meaningful pullback
    # (defined as first hour with a retracement >= 20% of prior range)?
    # ══════════════════════════════════════════════════════════════════════════
    print(f"  [{symbol}] Hourly 09:30 touches + DWP pullback…")

    touch_rows = con.execute(f"""
        WITH ref AS (
            SELECT date, open_930 AS ref_px
            FROM intraday_feat
            WHERE open_930 IS NOT NULL
        ),
        bar_hr AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                EXTRACT(HOUR FROM timezone('America/New_York', timestamp))::INT AS hr,
                high, low
            FROM {table}
            WHERE timezone('America/New_York', timestamp)::TIME >= '10:00'
              AND timezone('America/New_York', timestamp)::TIME <  '16:30'
        ),
        crosses AS (
            SELECT b.date, b.hr,
                BOOL_OR(b.low <= r.ref_px AND b.high >= r.ref_px) AS touched
            FROM bar_hr b JOIN ref r ON r.date = b.date
            GROUP BY b.date, b.hr
        )
        SELECT hr,
            COUNT(*) AS total_days,
            SUM(CASE WHEN touched THEN 1 ELSE 0 END) AS touch_days,
            ROUND(AVG(CASE WHEN touched THEN 1.0 ELSE 0.0 END) * 100, 1) AS touch_pct
        FROM crosses
        GROUP BY hr ORDER BY hr
    """).fetchall()

    dwp_rows = con.execute(f"""
        WITH dwp_days AS (
            SELECT dc.date
            FROM day_class dc
            WHERE dc.classification = 'DWP'
        ),
        rth_by_hour AS (
            SELECT
                CAST(timezone('America/New_York', timestamp) AS DATE) AS date,
                EXTRACT(HOUR FROM timezone('America/New_York', timestamp))::INT AS hr,
                MAX(high) AS h_high, MIN(low) AS h_low
            FROM {table}
            WHERE timezone('America/New_York', timestamp)::TIME >= '09:30'
              AND timezone('America/New_York', timestamp)::TIME <  '16:30'
            GROUP BY 1, 2
        ),
        cumulative AS (
            SELECT rh.date, rh.hr,
                MAX(rh.h_high) OVER (PARTITION BY rh.date ORDER BY rh.hr) AS cum_high,
                MIN(rh.h_low)  OVER (PARTITION BY rh.date ORDER BY rh.hr) AS cum_low
            FROM rth_by_hour rh JOIN dwp_days d ON d.date = rh.date
        ),
        pullback AS (
            SELECT c.date, c.hr,
                c.cum_high, c.cum_low,
                (c.cum_high - c.cum_low) AS range_so_far,
                LAG(c.cum_high) OVER (PARTITION BY c.date ORDER BY c.hr) AS prev_high
            FROM cumulative c
        ),
        first_pull AS (
            SELECT date,
                MIN(hr) FILTER (
                    WHERE hr > 10
                      AND prev_high IS NOT NULL
                      AND range_so_far > 0
                      AND (cum_high - prev_high) / NULLIF(cum_high - cum_low, 0) >= 0.20
                ) AS pull_hr
            FROM pullback
            GROUP BY date
        )
        SELECT pull_hr, COUNT(*) AS n
        FROM first_pull
        WHERE pull_hr IS NOT NULL
        GROUP BY pull_hr ORDER BY pull_hr
    """).fetchall()

    dwp_total = sum(r[1] for r in dwp_rows) or 1
    result["day_stats"] = {
        "open930_touches": [
            {"hr": int(r[0]), "n": int(r[2]), "pct": float(r[3])} for r in touch_rows
        ],
        "dwp_pullback_hr": [
            {"hr": int(r[0]), "n": int(r[1]),
             "pct": round(r[1]/dwp_total*100, 1)} for r in dwp_rows
        ],
    }

    print(f"  [{symbol}] ✅ Done")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=list(INSTRUMENTS.keys()))
    args = parser.parse_args()
    keys = [args.symbol] if args.symbol else list(INSTRUMENTS.keys())

    existing = {}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
        except Exception:
            pass

    output = {"generated": datetime.now().isoformat()}
    con    = duckdb.connect(str(DB_PATH))

    for key in keys:
        cfg    = INSTRUMENTS[key]
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        if cfg["table"] not in tables:
            print(f"  [{key}] Table '{cfg['table']}' not found — run fetch_data.py first")
            continue
        output[key] = build_instrument(con, key, cfg)

    con.close()

    # Preserve instruments not rebuilt
    for key in INSTRUMENTS:
        if key not in output and key in existing:
            output[key] = existing[key]

    OUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"\n✅ Saved → {OUT_PATH}")
    for key in keys:
        if key in output:
            n = sum(output[key]["probs"][c]["n"]
                    for c in ["bull_bull","bull_bear","bear_bull","bear_bear"])
            print(f"   {key}: {n:,} triplets")


if __name__ == "__main__":
    main()
