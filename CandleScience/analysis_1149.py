#!/usr/bin/env python3
"""
11:49 ET Candle Analysis
========================
For each 11:49 ET candle, scan forward bars (until 16:00 ET same day) and
determine whether price reaches +40 pts upside BEFORE reaching -40 pts downside
(measured from the 11:49 candle's close).

Outputs: occurrences and percentages grouped by day of week.
"""

import duckdb
import pandas as pd
from pathlib import Path

DB_PATH   = Path(__file__).parent / "candle_science.duckdb"
THRESHOLD = 40.0   # points
SCAN_HOUR = 16     # stop scanning at 16:00 ET

DOW_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}

def main():
    con = duckdb.connect(str(DB_PATH))

    # Pull all 11:49 ET candles
    anchors = con.execute("""
        SELECT
            timestamp,
            timezone('America/New_York', timestamp) AS et,
            close
        FROM nq_1m
        WHERE extract(hour   FROM timezone('America/New_York', timestamp)) = 11
          AND extract(minute FROM timezone('America/New_York', timestamp)) = 49
        ORDER BY timestamp
    """).df()

    # Pull all 1m bars into memory for fast scanning
    bars = con.execute("""
        SELECT
            timestamp,
            timezone('America/New_York', timestamp) AS et,
            high, low
        FROM nq_1m
        ORDER BY timestamp
    """).df()

    bars["et"] = pd.to_datetime(bars["et"], utc=False)
    bars        = bars.set_index("et")

    anchors["et"] = pd.to_datetime(anchors["et"], utc=False)

    results = []

    for _, row in anchors.iterrows():
        et_anchor  = row["et"]
        ref        = row["close"]
        dow        = et_anchor.weekday()       # 0=Mon … 4=Fri

        # Scan bars from 11:50 to 16:00 ET same day
        scan_start = et_anchor + pd.Timedelta(minutes=1)
        scan_end   = et_anchor.replace(hour=SCAN_HOUR, minute=0, second=0)

        window = bars.loc[scan_start:scan_end]

        hit_up   = False
        hit_down = False
        max_dd   = 0.0   # max adverse excursion (downside pts from ref)

        for bar_et, bar in window.iterrows():
            dd = ref - bar["low"]    # positive = below ref
            up = bar["high"] - ref   # positive = above ref

            if dd > max_dd:
                max_dd = dd

            if up >= THRESHOLD:
                hit_up = True
                break
            if dd >= THRESHOLD:
                hit_down = True
                break

        # Condition: upside +40 reached with < 40 pts drawdown first
        up_before_down = hit_up and not hit_down

        results.append({
            "date":            et_anchor.date(),
            "dow":             dow,
            "dow_name":        DOW_NAMES.get(dow, "Weekend"),
            "ref_close":       ref,
            "max_drawdown":    round(max_dd, 2),
            "hit_up_first":    up_before_down,
            "hit_down_first":  hit_down and not hit_up,
            "no_target":       not hit_up and not hit_down,
        })

    df = pd.DataFrame(results)

    # ── Summary by day of week ──────────────────────────────────────────────────
    dow_order = [0, 1, 2, 3, 4]
    summary = (
        df.groupby(["dow", "dow_name"])
        .agg(
            total          = ("date",          "count"),
            up_before_down = ("hit_up_first",  "sum"),
            down_before_up = ("hit_down_first","sum"),
            no_target      = ("no_target",     "sum"),
        )
        .reset_index()
        .sort_values("dow")
    )
    summary["pct_up_first"]   = (summary["up_before_down"] / summary["total"] * 100).round(1)
    summary["pct_down_first"] = (summary["down_before_up"] / summary["total"] * 100).round(1)
    summary["pct_no_target"]  = (summary["no_target"]      / summary["total"] * 100).round(1)

    # ── Print results ───────────────────────────────────────────────────────────
    print()
    print("11:49 ET Candle — +40 pts upside reached with < 40 pts drawdown first")
    print(f"Reference: close of 11:49 candle  |  Threshold: {THRESHOLD} pts  |  Scan window: 11:50–16:00 ET")
    print(f"{'═'*78}")
    print(f"  {'Day':<12}  {'Total':>6}  {'↑ First':>8}  {'%':>6}  {'↓ First':>8}  {'%':>6}  {'No Hit':>7}  {'%':>6}")
    print(f"  {'─'*12}  {'─'*6}  {'─'*8}  {'─'*6}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}")

    totals = {"total": 0, "up_before_down": 0, "down_before_up": 0, "no_target": 0}
    for _, r in summary.iterrows():
        print(f"  {r['dow_name']:<12}  {r['total']:>6,}  {int(r['up_before_down']):>8,}  "
              f"{r['pct_up_first']:>5.1f}%  {int(r['down_before_up']):>8,}  "
              f"{r['pct_down_first']:>5.1f}%  {int(r['no_target']):>7,}  {r['pct_no_target']:>5.1f}%")
        for k in totals:
            totals[k] += r[k]

    print(f"  {'─'*12}  {'─'*6}  {'─'*8}  {'─'*6}  {'─'*8}  {'─'*6}  {'─'*7}  {'─'*6}")
    t = totals
    print(f"  {'ALL DAYS':<12}  {t['total']:>6,}  {t['up_before_down']:>8,}  "
          f"{t['up_before_down']/t['total']*100:>5.1f}%  {t['down_before_up']:>8,}  "
          f"{t['down_before_up']/t['total']*100:>5.1f}%  {t['no_target']:>7,}  "
          f"{t['no_target']/t['total']*100:>5.1f}%")
    print(f"{'═'*78}")
    print()

    con.close()


if __name__ == "__main__":
    main()
