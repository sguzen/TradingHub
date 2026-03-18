#!/usr/bin/env python3
"""
NQ Daily Update
===============
1. Fetches any missing 1-minute bars from Databento
2. Upserts them into DuckDB
3. Rebuilds nq_probs.json for the dashboard
4. Sends a Mac notification when done

Schedule: runs automatically via crontab (see bottom of file or README).

Usage:
    python3 nq_daily_update.py

Crontab (weekdays at 7:00 AM):
    0 7 * * 1-5 /usr/bin/python3 /FULL/PATH/TO/nq_daily_update.py >> /FULL/PATH/TO/nq_daily_update.log 2>&1
"""

import sys
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pytz

# ── CONFIG — edit these ────────────────────────────────────────────────────────
DB_PATH   = Path("/Users/abhi/Downloads/CandleScience/NQ_futures.duckdb")
JSON_PATH = Path("/Users/abhi/Downloads/CandleScience/nq_probs.json")
API_KEY   = "REDACTED_API_KEY"
DB_PATH   = Path(__file__).parent / "NQ_futures.duckdb"   # DuckDB database
JSON_PATH = Path(__file__).parent / "nq_probs.json"       # Dashboard JSON output
API_KEY   = "REDACTED_API_KEY"                                # Databento API key
DATASET   = "GLBX.MDP3"
SYMBOL    = "NQ.c.0"
SCHEMA    = "ohlcv-1m"
STYPE     = "continuous"
TIMEZONE  = "America/Toronto"
MAC_NOTIFY = True   # Set False to disable Mac banner notifications
# ───────────────────────────────────────────────────────────────────────────────

LOG_PREFIX = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"


def log(msg):
    print(f"{LOG_PREFIX} {msg}", flush=True)


# ── STEP 1: FETCH NEW DATA FROM DATABENTO ─────────────────────────────────────
def fetch_new_data(con):
    try:
        import databento as db
    except ImportError:
        log("ERROR: databento not installed. Run: pip install databento")
        sys.exit(1)

    tz  = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    # Get latest timestamp already in the DB
    result  = con.execute("SELECT MAX(timestamp) FROM nq_1m").fetchone()
    last_ts = result[0]

    if last_ts is None:
        log("No data found in DB — skipping fetch (run initial download first)")
        return 0

    # Convert to timezone-aware datetime
    if hasattr(last_ts, 'tzinfo') and last_ts.tzinfo is None:
        last_ts = tz.localize(last_ts)
    elif hasattr(last_ts, 'tzinfo') and last_ts.tzinfo is not None:
        last_ts = last_ts.astimezone(tz)

    fetch_from = last_ts + timedelta(minutes=1)
    fetch_to   = now - timedelta(minutes=5)  # small buffer for incomplete bars

    # Skip if market is closed or data is already fresh (within 12 hours)
    if fetch_to <= fetch_from:
        log(f"Data already up to date (last: {last_ts.strftime('%Y-%m-%d %H:%M')})")
        return 0

    # Skip weekends
    if now.weekday() >= 5:
        log("Weekend — skipping fetch")
        return 0

    log(f"Fetching data from {fetch_from.strftime('%Y-%m-%d %H:%M')} → {fetch_to.strftime('%Y-%m-%d %H:%M')}")

    try:
        client  = db.Historical(API_KEY)
        dbn_path = Path(__file__).parent / "NQ_1m_latest.dbn"

        client.timeseries.get_range(
            dataset   = DATASET,
            symbols   = [SYMBOL],
            schema    = SCHEMA,
            stype_in  = STYPE,
            start     = fetch_from.strftime("%Y-%m-%dT%H:%M:%S"),
            end       = fetch_to.strftime("%Y-%m-%dT%H:%M:%S"),
            path      = str(dbn_path),
        )

        store = db.DBNStore.from_file(str(dbn_path))
        df    = store.to_df()

        if df.empty:
            log("No new bars returned from Databento")
            return 0

        df = df.reset_index()
        df = df.rename(columns={"ts_event": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(TIMEZONE)

        # Keep only the columns we need
        df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()

        # Upsert: delete overlap then insert
        min_ts = df["timestamp"].min()
        con.execute(f"DELETE FROM nq_1m WHERE timestamp >= '{min_ts}'")
        con.execute("INSERT INTO nq_1m SELECT * FROM df")

        n = len(df)
        log(f"Inserted {n:,} new rows into nq_1m")
        return n

    except Exception as e:
        log(f"ERROR fetching data: {e}")
        return 0


# ── STEP 2: REBUILD nq_probs.json ─────────────────────────────────────────────
def rebuild_probs(con):
    log("Rebuilding probability tables...")

    # Build daily OHLCV
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

    # Build consecutive day triplets
    con.execute("""
        CREATE OR REPLACE TEMP TABLE triplets AS
        SELECT
            c1.date                                         AS date,
            c1.open  AS c1_open,  c1.high AS c1_high,
            c1.low   AS c1_low,   c1.close AS c1_close,
            c2.open  AS c2_open,  c2.high AS c2_high,
            c2.low   AS c2_low,   c2.close AS c2_close,
            c3.open  AS c3_open,  c3.high AS c3_high,
            c3.low   AS c3_low,   c3.close AS c3_close,
            (c1.close >= c1.open)   AS c1_bull,
            (c2.close >= c2.open)   AS c2_bull,
            (c3.close >= c3.open)   AS c3_bull,
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

    COLOR_COMBOS = {
        "bull_bull": "c1_bull AND c2_bull",
        "bull_bear": "c1_bull AND NOT c2_bull",
        "bear_bull": "NOT c1_bull AND c2_bull",
        "bear_bear": "NOT c1_bull AND NOT c2_bull",
        "all":       "TRUE",
    }

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
        agg      = ", ".join([f"ROUND(AVG(CAST({m} AS DOUBLE))*100, 2) AS {m}" for m in metrics])
        row      = con.execute(f"SELECT COUNT(*) AS n, {agg} FROM triplets WHERE {where}").fetchone()
        cols     = ["n"] + metrics
        return dict(zip(cols, row))

    output = {"generated": datetime.now().isoformat(), "probs": {}, "conditional": {}}

    for combo, where in COLOR_COMBOS.items():
        c2p = fetch_probs(C2_METRICS, where)
        c3p = fetch_probs(C3_METRICS, where)
        output["probs"][combo] = {
            "n":      c2p["n"],
            "c2":     {k: v for k, v in c2p.items() if k != "n"},
            "c3":     {k: v for k, v in c3p.items() if k not in ("n", "c3_bull")},
            "c3_bull": c3p["c3_bull"],
        }

    for combo, base_where in COLOR_COMBOS.items():
        if combo == "all":
            continue
        output["conditional"][combo] = {}
        for c2m in C2_METRICS:
            for direction in ("above", "below"):
                obs   = f"{c2m} = TRUE" if direction == "above" else f"{c2m} = FALSE"
                where = f"({base_where}) AND ({obs})"
                try:
                    c3p  = fetch_probs(C3_METRICS, where)
                    key  = f"{c2m}_{direction}"
                    output["conditional"][combo][key] = {
                        "n":  c3p["n"],
                        "c3": {k: v for k, v in c3p.items() if k not in ("n",)},
                    }
                except Exception as e:
                    log(f"  Warning: {combo}/{c2m}_{direction}: {e}")

    with open(JSON_PATH, "w") as f:
        json.dump(output, f, indent=2)

    total_cond = sum(len(v) for v in output["conditional"].values())
    log(f"Saved nq_probs.json — {len(output['probs'])} combos, {total_cond} conditional entries")


# ── STEP 3: MAC NOTIFICATION ───────────────────────────────────────────────────
def notify(title, message):
    if not MAC_NOTIFY:
        return
    # Try terminal-notifier first (brew install terminal-notifier)
    try:
        subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message, "-sound", "default"],
            check=True, capture_output=True
        )
        return
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    # Fallback: osascript
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except Exception:
        pass


# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    log("=" * 60)
    log("NQ Daily Update starting")

    con = duckdb.connect(str(DB_PATH))

    # Step 1: fetch new market data
    new_rows = fetch_new_data(con)

    # Step 2: rebuild probabilities
    rebuild_probs(con)

    con.close()

    # Step 3: notify
    ts = datetime.now(pytz.timezone(TIMEZONE)).strftime("%b %d %H:%M")
    if new_rows > 0:
        msg = f"{new_rows:,} new bars added · probabilities updated · {ts}"
    else:
        msg = f"No new data · probabilities refreshed · {ts}"

    notify("NQ Dashboard Updated", msg)
    log(f"Done. {msg}")
    log("=" * 60)


if __name__ == "__main__":
    main()
