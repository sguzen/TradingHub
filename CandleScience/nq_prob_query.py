import duckdb

con = duckdb.connect("NQ_futures.duckdb")

result = con.execute("""
WITH daily AS (
    SELECT
        CAST(timestamp AS DATE)              AS date,
        FIRST(open  ORDER BY timestamp)      AS open,
        MAX(high)                            AS high,
        MIN(low)                             AS low,
        LAST(close  ORDER BY timestamp)      AS close
    FROM nq_1m
    GROUP BY CAST(timestamp AS DATE)
    ORDER BY date
),
triplets AS (
    SELECT
        c1.date                              AS c1_date,
        c1.open AS c1_open, c1.high AS c1_high, c1.low AS c1_low, c1.close AS c1_close,
        c2.open AS c2_open, c2.high AS c2_high, c2.low AS c2_low, c2.close AS c2_close,
        -- candle colors
        (c1.close >= c1.open)                AS c1_bull,
        (c2.close >= c2.open)                AS c2_bull,
        -- C2 vs C1 relationships
        (c2.high  > c1.high)                 AS c2_high_above_c1_high,
        (c2.high  > c1.open)                 AS c2_high_above_c1_open,
        (c2.low   > c1.low)                  AS c2_low_above_c1_low,
        (c2.low   > c1.open)                 AS c2_low_above_c1_open,
        (c2.close > c1.high)                 AS c2_close_above_c1_high,
        (c2.close > c1.low)                  AS c2_close_above_c1_low,
        (c2.close > c1.close)                AS c2_close_above_c1_close,
        (c2.close > c1.open)                 AS c2_close_above_c1_open,
        (c2.open  > c1.close)                AS c2_open_above_c1_close,
        (c2.open  > c1.open)                 AS c2_open_above_c1_open,
        (c2.open  > c1.high)                 AS c2_open_above_c1_high,
        (c2.open  > c1.low)                  AS c2_open_above_c1_low
    FROM daily c1
    JOIN daily c2 ON c2.date = (
        SELECT MIN(date) FROM daily WHERE date > c1.date
    )
)

-- ── Main question ──────────────────────────────────────────
SELECT
    '=== C2 HIGH vs C1 HIGH ===' AS section,
    '' AS condition,
    0  AS n,
    0.0 AS pct
UNION ALL
SELECT
    'Both Bull (C1 green + C2 green)'  AS section,
    'C2 High > C1 High'                AS condition,
    COUNT(*)                           AS n,
    ROUND(AVG(CAST(c2_high_above_c1_high AS DOUBLE)) * 100, 2) AS pct
FROM triplets WHERE c1_bull AND c2_bull

UNION ALL
SELECT
    'C1 Bull + C2 Bear'                AS section,
    'C2 High > C1 High'                AS condition,
    COUNT(*), ROUND(AVG(CAST(c2_high_above_c1_high AS DOUBLE)) * 100, 2)
FROM triplets WHERE c1_bull AND NOT c2_bull

UNION ALL
SELECT
    'C1 Bear + C2 Bull'                AS section,
    'C2 High > C1 High'                AS condition,
    COUNT(*), ROUND(AVG(CAST(c2_high_above_c1_high AS DOUBLE)) * 100, 2)
FROM triplets WHERE NOT c1_bull AND c2_bull

UNION ALL
SELECT
    'Both Bear (C1 red + C2 red)'      AS section,
    'C2 High > C1 High'                AS condition,
    COUNT(*), ROUND(AVG(CAST(c2_high_above_c1_high AS DOUBLE)) * 100, 2)
FROM triplets WHERE NOT c1_bull AND NOT c2_bull

UNION ALL
SELECT 'ALL (regardless of color)', 'C2 High > C1 High',
    COUNT(*), ROUND(AVG(CAST(c2_high_above_c1_high AS DOUBLE)) * 100, 2)
FROM triplets

UNION ALL
SELECT '=== ALL C2 vs C1 METRICS (Both Bull) ===', '', 0, 0.0

UNION ALL SELECT 'Both Bull', 'C2 High  > C1 High',  COUNT(*), ROUND(AVG(CAST(c2_high_above_c1_high  AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 High  > C1 Open',  COUNT(*), ROUND(AVG(CAST(c2_high_above_c1_open  AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Low   > C1 Low',   COUNT(*), ROUND(AVG(CAST(c2_low_above_c1_low    AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Low   > C1 Open',  COUNT(*), ROUND(AVG(CAST(c2_low_above_c1_open   AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Close > C1 High',  COUNT(*), ROUND(AVG(CAST(c2_close_above_c1_high AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Close > C1 Low',   COUNT(*), ROUND(AVG(CAST(c2_close_above_c1_low  AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Close > C1 Close', COUNT(*), ROUND(AVG(CAST(c2_close_above_c1_close AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Close > C1 Open',  COUNT(*), ROUND(AVG(CAST(c2_close_above_c1_open AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Open  > C1 Close', COUNT(*), ROUND(AVG(CAST(c2_open_above_c1_close AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Open  > C1 Open',  COUNT(*), ROUND(AVG(CAST(c2_open_above_c1_open  AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Open  > C1 High',  COUNT(*), ROUND(AVG(CAST(c2_open_above_c1_high  AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull
UNION ALL SELECT 'Both Bull', 'C2 Open  > C1 Low',   COUNT(*), ROUND(AVG(CAST(c2_open_above_c1_low   AS DOUBLE))*100,2) FROM triplets WHERE c1_bull AND c2_bull

ORDER BY section, condition
""").df()

con.close()

print(result.to_string(index=False))
