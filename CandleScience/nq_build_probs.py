"""
NQ Probability Builder
Computes all conditional probabilities from DuckDB and saves to nq_probs.json.
Run once (or add to your daily update script) to refresh dashboard numbers.

Usage: python3 nq_build_probs.py
Output: nq_probs.json
"""

import duckdb
import json
from datetime import datetime

DB_PATH  = "NQ_futures.duckdb"
OUT_PATH = "nq_probs.json"

con = duckdb.connect(DB_PATH)

print("Building daily bars...")
con.execute("""
CREATE OR REPLACE TEMP TABLE daily AS
    SELECT
        CAST(timestamp AS DATE)             AS date,
        FIRST(open  ORDER BY timestamp)     AS open,
        MAX(high)                           AS high,
        MIN(low)                            AS low,
        LAST(close  ORDER BY timestamp)     AS close
    FROM nq_1m
    GROUP BY CAST(timestamp AS DATE)
    ORDER BY date
""")

print("Building triplets...")
con.execute("""
CREATE OR REPLACE TEMP TABLE triplets AS
SELECT
    c1.date                                         AS date,
    -- C1
    c1.open  AS c1_open,  c1.high AS c1_high,
    c1.low   AS c1_low,   c1.close AS c1_close,
    -- C2
    c2.open  AS c2_open,  c2.high AS c2_high,
    c2.low   AS c2_low,   c2.close AS c2_close,
    -- C3
    c3.open  AS c3_open,  c3.high AS c3_high,
    c3.low   AS c3_low,   c3.close AS c3_close,
    -- Colors
    (c1.close >= c1.open)   AS c1_bull,
    (c2.close >= c2.open)   AS c2_bull,
    (c3.close >= c3.open)   AS c3_bull,
    -- C2 vs C1
    (c2.high  > c1.high)    AS c2_high_gt_c1_high,
    (c2.high  > c1.open)    AS c2_high_gt_c1_open,
    (c2.low   > c1.low)     AS c2_low_gt_c1_low,
    (c2.low   > c1.open)    AS c2_low_gt_c1_open,
    (c2.close > c1.high)    AS c2_close_gt_c1_high,
    (c2.close > c1.low)     AS c2_close_gt_c1_low,
    (c2.close > c1.close)   AS c2_close_gt_c1_close,
    (c2.close > c1.open)    AS c2_close_gt_c1_open,
    (c2.open  > c1.close)   AS c2_open_gt_c1_close,
    (c2.open  > c1.open)    AS c2_open_gt_c1_open,
    (c2.open  > c1.high)    AS c2_open_gt_c1_high,
    (c2.open  > c1.low)     AS c2_open_gt_c1_low,
    -- C3 vs C2
    (c3.high  > c2.high)    AS c3_high_gt_c2_high,
    (c3.high  > c2.open)    AS c3_high_gt_c2_open,
    (c3.low   > c2.low)     AS c3_low_gt_c2_low,
    (c3.low   > c2.open)    AS c3_low_gt_c2_open,
    (c3.close > c2.high)    AS c3_close_gt_c2_high,
    (c3.close > c2.low)     AS c3_close_gt_c2_low,
    (c3.close > c2.close)   AS c3_close_gt_c2_close,
    (c3.close > c2.open)    AS c3_close_gt_c2_open,
    (c3.open  > c2.close)   AS c3_open_gt_c2_close,
    (c3.open  > c2.open)    AS c3_open_gt_c2_open,
    (c3.open  > c2.high)    AS c3_open_gt_c2_high,
    (c3.open  > c2.low)     AS c3_open_gt_c2_low
FROM daily c1
JOIN daily c2 ON c2.date = (SELECT MIN(date) FROM daily WHERE date > c1.date)
JOIN daily c3 ON c3.date = (SELECT MIN(date) FROM daily WHERE date > c2.date)
""")

# Color combinations
COLOR_COMBOS = {
    "bull_bull": "c1_bull AND c2_bull",
    "bull_bear": "c1_bull AND NOT c2_bull",
    "bear_bull": "NOT c1_bull AND c2_bull",
    "bear_bear": "NOT c1_bull AND NOT c2_bull",
    "all":       "TRUE",
}

# All metrics to compute
C2_METRICS = [
    "c2_high_gt_c1_high",  "c2_high_gt_c1_open",
    "c2_low_gt_c1_low",    "c2_low_gt_c1_open",
    "c2_close_gt_c1_high", "c2_close_gt_c1_low",
    "c2_close_gt_c1_close","c2_close_gt_c1_open",
    "c2_open_gt_c1_close", "c2_open_gt_c1_open",
    "c2_open_gt_c1_high",  "c2_open_gt_c1_low",
]

C3_METRICS = [
    "c3_high_gt_c2_high",  "c3_high_gt_c2_open",
    "c3_low_gt_c2_low",    "c3_low_gt_c2_open",
    "c3_close_gt_c2_high", "c3_close_gt_c2_low",
    "c3_close_gt_c2_close","c3_close_gt_c2_open",
    "c3_open_gt_c2_close", "c3_open_gt_c2_open",
    "c3_open_gt_c2_high",  "c3_open_gt_c2_low",
    "c3_bull",
]

def fetch_probs(metrics, where):
    agg = ", ".join([
        f"ROUND(AVG(CAST({m} AS DOUBLE)) * 100, 2) AS {m}"
        for m in metrics
    ])
    count_col = "COUNT(*) AS n"
    row = con.execute(f"SELECT {count_col}, {agg} FROM triplets WHERE {where}").fetchone()
    cols = ["n"] + metrics
    return dict(zip(cols, row))

print("Computing probabilities for all color combinations...")
output = {
    "generated": datetime.now().isoformat(),
    "probs": {}
}

for combo, where in COLOR_COMBOS.items():
    print(f"  {combo}...")
    c2_probs = fetch_probs(C2_METRICS, where)
    c3_probs = fetch_probs(C3_METRICS, where)
    output["probs"][combo] = {
        "n":   c2_probs["n"],
        "c2":  {k: v for k, v in c2_probs.items() if k != "n"},
        "c3":  {k: v for k, v in c3_probs.items() if k != "n"},
        "c3_bull": c3_probs["c3_bull"],
    }

# Also compute C3 conditioned on BOTH c1/c2 color AND a specific C2-vs-C1 observation
# e.g. given both bull AND c2_high > c1_high, what is P(c3_high > c2_high)?
print("Computing conditional C3 probs (given color + C2 observation)...")
output["conditional"] = {}

for combo, base_where in COLOR_COMBOS.items():
    if combo == "all":
        continue
    output["conditional"][combo] = {}
    for c2m in C2_METRICS:
        for direction in ["above", "below"]:
            obs = f"{c2m} = TRUE" if direction == "above" else f"{c2m} = FALSE"
            where = f"({base_where}) AND ({obs})"
            try:
                c3_probs = fetch_probs(C3_METRICS, where)
                key = f"{c2m}_{direction}"
                output["conditional"][combo][key] = {
                    "n":  c3_probs["n"],
                    "c3": {k: v for k, v in c3_probs.items() if k != "n"},
                }
            except Exception as e:
                print(f"    Warning: {combo} / {key}: {e}")

con.close()

with open(OUT_PATH, "w") as f:
    json.dump(output, f, indent=2)

print(f"\n✅ Done! Saved to {OUT_PATH}")
print(f"   Color combos: {list(output['probs'].keys())}")
total_conditions = sum(
    len(v) for v in output["conditional"].values()
)
print(f"   Conditional entries: {total_conditions}")
