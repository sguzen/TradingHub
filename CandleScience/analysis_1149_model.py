#!/usr/bin/env python3
"""
11:49 ET Candle — Trade Model Analysis
=======================================
Entry: close of 11:49 candle (long or short)
Scan:  11:50 → 16:00 ET same day
Tests: stop sizes 10–50 pts × R:R targets 1R / 1.5R / 2R / 3R

Outputs:
  1. Best stop/RR combos by win rate and EV
  2. Long vs Short breakdown
  3. Results by day of week
  4. 11:49 candle direction filter (bull/bear/doji)
  5. Candle context: price vs session open (9:30 ET close)
"""

import duckdb
import pandas as pd
import numpy as np
from pathlib import Path
from itertools import product

DB_PATH   = Path(__file__).parent / "candle_science.duckdb"
STOPS     = [10, 15, 20, 25, 30, 40, 50]
RR_LIST   = [1.0, 1.5, 2.0, 3.0]
DOW_NAMES = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri"}

def breakeven_wr(rr):
    return 1 / (1 + rr)

def ev(wr, rr):
    return wr * rr - (1 - wr)

def pf(wr, rr):
    wins  = wr * rr
    losses = (1 - wr)
    return wins / losses if losses > 0 else float("inf")


def scan_outcome(window_highs, window_lows, ref, stop_pts, target_pts):
    """Return 'win', 'loss', or 'none' — whichever target is hit first."""
    long_target = ref + target_pts
    long_stop   = ref - stop_pts
    short_target= ref - target_pts
    short_stop  = ref + stop_pts

    long_result  = "none"
    short_result = "none"

    for h, l in zip(window_highs, window_lows):
        if long_result == "none":
            if h >= long_target:
                long_result = "win"
            elif l <= long_stop:
                long_result = "loss"

        if short_result == "none":
            if l <= short_target:
                short_result = "win"
            elif h >= short_stop:
                short_result = "loss"

        if long_result != "none" and short_result != "none":
            break

    return long_result, short_result


def main():
    con = duckdb.connect(str(DB_PATH))

    # 11:49 anchor candles
    anchors = con.execute("""
        SELECT
            timestamp,
            timezone('America/New_York', timestamp) AS et,
            open AS c_open, high AS c_high, low AS c_low, close AS c_close
        FROM nq_1m
        WHERE extract(hour   FROM timezone('America/New_York', timestamp)) = 11
          AND extract(minute FROM timezone('America/New_York', timestamp)) = 49
        ORDER BY timestamp
    """).df()

    # 9:30 candle close for session-open context
    session_open = con.execute("""
        SELECT
            DATE(timezone('America/New_York', timestamp)) AS date,
            close AS session_open_close
        FROM nq_1m
        WHERE extract(hour   FROM timezone('America/New_York', timestamp)) = 9
          AND extract(minute FROM timezone('America/New_York', timestamp)) = 30
    """).df()
    session_open["date"] = pd.to_datetime(session_open["date"])

    # All bars for scanning
    bars = con.execute("""
        SELECT
            timezone('America/New_York', timestamp) AS et,
            high, low
        FROM nq_1m
        ORDER BY timestamp
    """).df()
    bars["et"] = pd.to_datetime(bars["et"], utc=False)
    bars = bars.set_index("et")

    anchors["et"] = pd.to_datetime(anchors["et"], utc=False)

    # ── Build per-day records ─────────────────────────────────────────────────
    records = []
    for _, row in anchors.iterrows():
        et     = row["et"]
        ref    = row["c_close"]
        dow    = et.weekday()
        date   = et.date()

        # Candle direction
        body = row["c_close"] - row["c_open"]
        if body > 0.25:
            candle_dir = "bull"
        elif body < -0.25:
            candle_dir = "bear"
        else:
            candle_dir = "doji"

        # Session context: above/below 9:30 close
        d = pd.Timestamp(date)
        so_row = session_open[session_open["date"] == d]
        if not so_row.empty:
            so_close = so_row["session_open_close"].iloc[0]
            vs_session = "above" if ref > so_close else ("below" if ref < so_close else "at")
            session_pts = round(ref - so_close, 2)
        else:
            vs_session  = "unknown"
            session_pts = None

        # Forward window
        scan_start = et + pd.Timedelta(minutes=1)
        scan_end   = et.replace(hour=16, minute=0, second=0)
        window     = bars.loc[scan_start:scan_end]

        if window.empty:
            continue

        highs = window["high"].values
        lows  = window["low"].values

        rec = {
            "date":        date,
            "dow":         dow,
            "dow_name":    DOW_NAMES[dow],
            "ref":         ref,
            "candle_dir":  candle_dir,
            "vs_session":  vs_session,
            "session_pts": session_pts,
        }

        for stop in STOPS:
            for rr in RR_LIST:
                target = stop * rr
                long_r, short_r = scan_outcome(highs, lows, ref, stop, target)
                rec[f"L_{stop}s_{rr}rr"] = long_r
                rec[f"S_{stop}s_{rr}rr"] = short_r

        records.append(rec)

    df = pd.DataFrame(records)

    # ── Helper: stats from a result column ───────────────────────────────────
    def stats(col_vals):
        wins   = (col_vals == "win").sum()
        losses = (col_vals == "loss").sum()
        total  = wins + losses          # excludes "none"
        if total == 0:
            return None
        wr  = wins / total
        rr  = float(col_vals.name.split("rr")[0].split("_")[-1])
        return {
            "n":    total,
            "wins": wins,
            "wr":   wr,
            "ev":   round(ev(wr, rr), 4),
            "pf":   round(pf(wr, rr), 3),
        }

    # ── 1. FULL GRID: all stop × RR × direction ──────────────────────────────
    print()
    print("=" * 90)
    print("  11:49 ET CANDLE — TRADE MODEL GRID")
    print("  Entry: close of 11:49 | Scan: 11:50–16:00 ET | 2,785 days")
    print("=" * 90)
    print(f"\n  {'Key':<18}  {'N':>5}  {'WR':>7}  {'EV':>8}  {'PF':>7}  {'BE WR':>7}")
    print(f"  {'─'*18}  {'─'*5}  {'─'*7}  {'─'*8}  {'─'*7}  {'─'*7}")

    grid_rows = []
    for stop in STOPS:
        for rr in RR_LIST:
            be = breakeven_wr(rr)
            for d, prefix in [("Long", "L"), ("Short", "S")]:
                col = f"{prefix}_{stop}s_{rr}rr"
                col_data = df[col]
                col_data.name = col
                s = stats(col_data)
                if s:
                    edge = s["wr"] - be
                    grid_rows.append({**s, "stop": stop, "rr": rr, "dir": d, "edge": edge, "col": col})
                    marker = " ◀" if s["ev"] > 0.10 and s["pf"] > 1.3 else ""
                    print(f"  {d:<5} stop={stop:>2}  RR={rr}  "
                          f"{s['n']:>5,}  {s['wr']:>6.1%}  {s['ev']:>+8.4f}  "
                          f"{s['pf']:>7.3f}  {be:>6.1%}{marker}")

    grid_df = pd.DataFrame(grid_rows)

    # ── 2. TOP COMBOS BY EV ───────────────────────────────────────────────────
    print()
    print("─" * 90)
    print("  TOP 10 COMBOS BY EV  (min 100 resolved trades)")
    print("─" * 90)
    print(f"  {'Direction':<8}  {'Stop':>4}  {'RR':>4}  {'N':>5}  {'WR':>7}  {'EV':>8}  {'PF':>7}")
    top = grid_df[grid_df["n"] >= 100].sort_values("ev", ascending=False).head(10)
    for _, r in top.iterrows():
        print(f"  {r['dir']:<8}  {r['stop']:>4}  {r['rr']:>4}  {r['n']:>5,}  "
              f"{r['wr']:>6.1%}  {r['ev']:>+8.4f}  {r['pf']:>7.3f}")

    # ── 3. LONG vs SHORT — BEST STOP/RR ──────────────────────────────────────
    print()
    print("─" * 90)
    print("  LONG vs SHORT — BY DAY OF WEEK  (best combo: stop=20 RR=2.0)")
    print("─" * 90)

    for direction, prefix in [("Long", "L"), ("Short", "S")]:
        col = f"{prefix}_20s_2.0rr"
        print(f"\n  {direction}  (stop=20 target=40)")
        print(f"  {'Day':<10}  {'N':>5}  {'WR':>7}  {'EV':>8}  {'PF':>7}")
        print(f"  {'─'*10}  {'─'*5}  {'─'*7}  {'─'*8}  {'─'*7}")
        for dow in range(5):
            sub = df[df["dow"] == dow][col]
            sub.name = col
            s = stats(sub)
            if s:
                print(f"  {DOW_NAMES[dow]:<10}  {s['n']:>5,}  {s['wr']:>6.1%}  "
                      f"{s['ev']:>+8.4f}  {s['pf']:>7.3f}")
        sub_all = df[col]; sub_all.name = col
        s = stats(sub_all)
        if s:
            print(f"  {'ALL':<10}  {s['n']:>5,}  {s['wr']:>6.1%}  {s['ev']:>+8.4f}  {s['pf']:>7.3f}")

    # ── 4. CANDLE DIRECTION FILTER ────────────────────────────────────────────
    print()
    print("─" * 90)
    print("  CANDLE DIRECTION FILTER — does 11:49 candle color predict direction?")
    print("  (stop=20, RR=2.0)")
    print("─" * 90)
    print(f"  {'Filter':<18}  {'Days':>5}  {'Long WR':>8}  {'Long EV':>9}  {'Short WR':>9}  {'Short EV':>10}")
    print(f"  {'─'*18}  {'─'*5}  {'─'*8}  {'─'*9}  {'─'*9}  {'─'*10}")
    for f_val, f_label in [("bull","11:49 Bull"), ("bear","11:49 Bear"), ("doji","11:49 Doji")]:
        sub = df[df["candle_dir"] == f_val]
        lc = sub["L_20s_2.0rr"]; lc.name = "L_20s_2.0rr"
        sc = sub["S_20s_2.0rr"]; sc.name = "S_20s_2.0rr"
        sl = stats(lc); ss = stats(sc)
        if sl and ss:
            print(f"  {f_label:<18}  {len(sub):>5,}  {sl['wr']:>7.1%}  {sl['ev']:>+9.4f}  "
                  f"{ss['wr']:>8.1%}  {ss['ev']:>+10.4f}")

    # ── 5. SESSION CONTEXT FILTER ─────────────────────────────────────────────
    print()
    print("─" * 90)
    print("  SESSION CONTEXT — price at 11:49 vs 9:30 close  (stop=20, RR=2.0)")
    print("─" * 90)
    print(f"  {'Context':<22}  {'Days':>5}  {'Long WR':>8}  {'Long EV':>9}  {'Short WR':>9}  {'Short EV':>10}")
    print(f"  {'─'*22}  {'─'*5}  {'─'*8}  {'─'*9}  {'─'*9}  {'─'*10}")
    for f_val, f_label in [("above","Above 9:30 close"), ("below","Below 9:30 close")]:
        sub = df[df["vs_session"] == f_val]
        lc = sub["L_20s_2.0rr"]; lc.name = "L_20s_2.0rr"
        sc = sub["S_20s_2.0rr"]; sc.name = "S_20s_2.0rr"
        sl = stats(lc); ss = stats(sc)
        if sl and ss:
            print(f"  {f_label:<22}  {len(sub):>5,}  {sl['wr']:>7.1%}  {sl['ev']:>+9.4f}  "
                  f"{ss['wr']:>8.1%}  {ss['ev']:>+10.4f}")

    # ── 6. COMBINED BEST FILTER ───────────────────────────────────────────────
    print()
    print("─" * 90)
    print("  COMBINED FILTERS — candle dir + session context  (stop=20, RR=2.0)")
    print("─" * 90)
    print(f"  {'Filter':<34}  {'Days':>5}  {'Long WR':>8}  {'Long EV':>9}  {'Short WR':>9}  {'Short EV':>10}")
    print(f"  {'─'*34}  {'─'*5}  {'─'*8}  {'─'*9}  {'─'*9}  {'─'*10}")
    for cd in ["bull","bear"]:
        for vs in ["above","below"]:
            sub = df[(df["candle_dir"] == cd) & (df["vs_session"] == vs)]
            if len(sub) < 50: continue
            lc = sub["L_20s_2.0rr"]; lc.name = "L_20s_2.0rr"
            sc = sub["S_20s_2.0rr"]; sc.name = "S_20s_2.0rr"
            sl = stats(lc); ss = stats(sc)
            if sl and ss:
                label = f"{cd.capitalize()} candle + {vs} session"
                print(f"  {label:<34}  {len(sub):>5,}  {sl['wr']:>7.1%}  {sl['ev']:>+9.4f}  "
                      f"{ss['wr']:>8.1%}  {ss['ev']:>+10.4f}")

    print()
    print("=" * 90)
    print()
    con.close()


if __name__ == "__main__":
    main()
