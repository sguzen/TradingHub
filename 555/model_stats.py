#!/usr/bin/env python3
"""
model_stats.py  —  5M SMT + Inverse FVG + CISD Statistical Engine v1.0
=======================================================================
Detects trading setups based on three 5-minute conditions:
  1. SMT divergence (NQ sweeps a 5m swing high/low, ES does not)
  2. Inverse FVG (on 3m or 5m — earliest wins)
  3. CISD (on 5m — body-only, same as Fractal Sweep)

Entry = next 5m candle open after BOTH IFVG and CISD have occurred.
Stop  = IFVG invalidation, checked on 5m candle CLOSES only.
Target= TP1 at 1R (90% exit), runner (10%) with BE stop to EOD.

Usage:
    python3 model_stats.py
    python3 model_stats.py --db ../path/to/db.duckdb
    python3 model_stats.py --output custom_output.json
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import scipy.stats as _scipy_stats

# ── PATHS ─────────────────────────────────────────────────────────────────────
DB_PATH  = Path(__file__).parent.parent / 'Fractal Sweep' / 'candle_science.duckdb'
OUT_PATH = Path(__file__).parent / 'model_stats.json'
TABLE    = 'nq_1m'

# ── GLOBAL CONSTANTS ──────────────────────────────────────────────────────────
ACCOUNT_SIZE     = 4500
RISK_PER_TRADE   = 225
POINT_VALUE      = 2.0
MAX_RISK_PTS     = RISK_PER_TRADE / POINT_VALUE   # 112.5
MIN_RISK_PTS     = 3.0
MIN_IFVG_PTS     = 2.0
OUTCOME_MAX_BARS = 108   # 9 hrs x 12 bars/hr on 5m
SESSION_HRS      = (7.0, 16.0)
NS_PER_MIN       = np.int64(60_000_000_000)

DOW_NAMES  = {0: 'Sun', 1: 'Mon', 2: 'Tue', 3: 'Wed', 4: 'Thu', 5: 'Fri', 6: 'Sat'}
HR_LABELS  = {h: f"{h:02d}:00" for h in range(0, 24)}
_DOW_ORDER  = [1, 2, 3, 4, 5]
_DOW_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri']

# ── DATE CLASSIFICATION ──────────────────────────────────────────────────────
def _load_date_classification():
    """Build {date_str: cls_key} by running the daily classifier on DB data."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent / 'NY1 FPFVG'))
        from daily_classifier import classify_day

        con = duckdb.connect(str(DB_PATH), read_only=True)
        df = con.execute("""
            SELECT
                timezone('America/New_York', timestamp) AS datetime,
                open::DOUBLE AS open, high::DOUBLE AS high,
                low::DOUBLE  AS low,  close::DOUBLE AS close
            FROM nq_1m
            ORDER BY timestamp
        """).df()
        con.close()

        df['datetime'] = pd.to_datetime(df['datetime'])
        df['date'] = df['datetime'].dt.date

        mapping = {}
        for day, grp in df.groupby('date'):
            grp = grp.reset_index(drop=True)
            day_class, _ = classify_day(grp)
            mapping[str(day)] = day_class

        print(f'  Classified {len(mapping)} dates from DuckDB (live)')
        return mapping
    except Exception as e:
        print(f'  [info] Classification unavailable ({e}), skipping')
        return {}

DATE_CLASSIFICATION = _load_date_classification()
print(f'  Loaded {len(DATE_CLASSIFICATION)} date classifications')


# ── DB ────────────────────────────────────────────────────────────────────────
def connect(db_path):
    return duckdb.connect(str(db_path), read_only=True)


# ── LOAD 1m BARS ──────────────────────────────────────────────────────────────
def load_1m(con, table):
    print(f"[load] Loading {table} ...")
    raw = con.execute(f"""
        SELECT
            CAST(timezone('America/New_York', timestamp) AS DATE)  AS trade_date,
            CAST(timezone('America/New_York', timestamp) AS TIME)  AS bar_time,
            open::DOUBLE AS open, high::DOUBLE AS high,
            low::DOUBLE  AS low,  close::DOUBLE AS close,
            date_part('year',   timezone('America/New_York', timestamp)) AS yr,
            date_part('month',  timezone('America/New_York', timestamp)) AS mo,
            date_part('dow',    timezone('America/New_York', timestamp)) AS dow,
            date_part('hour',   timezone('America/New_York', timestamp)) AS hr,
            date_part('minute', timezone('America/New_York', timestamp)) AS mn
        FROM {table}
        ORDER BY timestamp
    """).df()
    raw['ts'] = pd.to_datetime(
        raw['trade_date'].astype(str) + ' ' + raw['bar_time'].astype(str)
    )
    raw = raw.sort_values('ts').reset_index(drop=True).set_index('ts')
    raw_rth = raw[
        (raw['hr'] >= 7) & ((raw['hr'] < 16) | ((raw['hr'] == 16) & (raw['mn'] == 0)))
    ].copy()
    print(f"   {len(raw):,} total bars  |  {len(raw_rth):,} RTH (07:00-16:00)")
    return raw, raw_rth


# ── RESAMPLE ──────────────────────────────────────────────────────────────────
def resample(df_1m, tf_min, label):
    print(f"   Building {label} bars ...")
    df2 = df_1m.copy()
    df2['ts_tf'] = df2.index.floor(f"{tf_min}min")
    agged = df2.groupby('ts_tf').agg(
        trade_date=('trade_date', 'first'), yr=('yr', 'first'), mo=('mo', 'first'),
        dow=('dow', 'first'), hr=('hr', 'first'),
        mn=('mn', 'first'),
        open_tf=('open', 'first'), high_tf=('high', 'max'),
        low_tf=('low', 'min'),    close_tf=('close', 'last'),
    ).sort_index()
    print(f"      {len(agged):,} {label} bars")
    return agged


# ── NUMPY ARRAY BUILDERS ─────────────────────────────────────────────────────
def df_to_arrays(df):
    """Convert a time-indexed resampled OHLC dataframe to numpy arrays."""
    return dict(
        ts_ns      = df.index.view('int64').copy(),
        open       = df['open_tf'].values.astype('float64'),
        high       = df['high_tf'].values.astype('float64'),
        low        = df['low_tf'].values.astype('float64'),
        close      = df['close_tf'].values.astype('float64'),
        trade_date = df['trade_date'].values.copy(),
        yr         = df['yr'].values.astype('int32'),
        dow        = df['dow'].values.astype('int32'),
        hr         = df['hr'].values.astype('int32'),
        mn         = df['mn'].values.astype('int32') if 'mn' in df.columns else np.zeros(len(df), dtype='int32'),
    )


def df_1m_to_arrays(df):
    """Convert 1m dataframe to numpy arrays for vectorised outcome resolution."""
    return dict(
        ts_ns      = df.index.view('int64').copy(),
        open       = df['open'].values.astype('float64'),
        high       = df['high'].values.astype('float64'),
        low        = df['low'].values.astype('float64'),
        close      = df['close'].values.astype('float64'),
        hr         = df['hr'].values.astype('int32'),
        mn         = df['mn'].values.astype('int32'),
        yr         = df['yr'].values.astype('int32'),
        dow        = df['dow'].values.astype('int32'),
        trade_date = df['trade_date'].values.copy(),
    )


# ── SESSION LABEL ─────────────────────────────────────────────────────────────
def get_session(hr_float):
    if 8.5  <= hr_float < 11.5: return 'NY1'
    if 11.5 <= hr_float < 16.0: return 'NY2'
    if 7.0  <= hr_float < 8.5:  return 'PRE'
    if 0    <= hr_float < 7.0:  return 'OVERNIGHT'
    return 'OTHER'


# ── SWING FRACTALS (3-bar on 5m) ─────────────────────────────────────────────
def detect_swing_fractals(arrs):
    """
    Simple 3-bar fractal on 5m data.
    Swing high: bar whose high > both prior and next bar highs.
    Swing low: bar whose low < both prior and next bar lows.
    Returns dict with 'highs' and 'lows' — each a list of (index, price, ts_ns).
    """
    highs_arr = arrs['high']
    lows_arr  = arrs['low']
    ts_arr    = arrs['ts_ns']
    n = len(highs_arr)

    swing_highs = []
    swing_lows  = []

    for i in range(1, n - 1):
        # Swing high
        if highs_arr[i] > highs_arr[i - 1] and highs_arr[i] > highs_arr[i + 1]:
            swing_highs.append((i, float(highs_arr[i]), int(ts_arr[i])))
        # Swing low
        if lows_arr[i] < lows_arr[i - 1] and lows_arr[i] < lows_arr[i + 1]:
            swing_lows.append((i, float(lows_arr[i]), int(ts_arr[i])))

    return {'highs': swing_highs, 'lows': swing_lows}


# ── SMT DIVERGENCE DETECTION ─────────────────────────────────────────────────
def detect_smt_divergences(nq_5m_arrs, es_5m_arrs, nq_swings):
    """
    For each NQ 5m bar that sweeps a prior NQ swing high/low:
    - Find temporally corresponding ES swing (nearest prior of same type)
    - Check if ES bar at same timestamp also swept
    - If ES did NOT sweep -> SMT divergence

    Returns list of dicts: {bar_idx, direction, sweep_price, swing_level, ts_ns}
    """
    nq_ts   = nq_5m_arrs['ts_ns']
    nq_high = nq_5m_arrs['high']
    nq_low  = nq_5m_arrs['low']
    nq_n    = len(nq_ts)

    es_ts   = es_5m_arrs['ts_ns']
    es_high = es_5m_arrs['high']
    es_low  = es_5m_arrs['low']

    divergences = []

    # Build sorted arrays of swing highs/lows for efficient lookup
    sh_list = nq_swings['highs']  # list of (idx, price, ts_ns)
    sl_list = nq_swings['lows']

    # Also detect ES swing fractals for the "corresponding ES swing" lookup
    es_swings = detect_swing_fractals(es_5m_arrs)
    es_sh_list = es_swings['highs']
    es_sl_list = es_swings['lows']

    # Convert ES swings to arrays for quick lookup
    es_sh_ts    = np.array([s[2] for s in es_sh_list], dtype='int64') if es_sh_list else np.array([], dtype='int64')
    es_sh_price = np.array([s[1] for s in es_sh_list], dtype='float64') if es_sh_list else np.array([], dtype='float64')
    es_sl_ts    = np.array([s[2] for s in es_sl_list], dtype='int64') if es_sl_list else np.array([], dtype='int64')
    es_sl_price = np.array([s[1] for s in es_sl_list], dtype='float64') if es_sl_list else np.array([], dtype='float64')

    # Scan NQ bars for sweeps of prior swing highs (bearish SMT -> SHORT)
    for sh_idx_in_list in range(len(sh_list)):
        swing_idx, swing_price, swing_ts = sh_list[sh_idx_in_list]
        # Check bars after the swing confirmation bar (swing_idx + 1 onward)
        # The swing is confirmed at bar swing_idx+1 (need the next bar to confirm)
        start_bar = swing_idx + 2  # bar after the confirmation bar
        if start_bar >= nq_n:
            continue

        for bar_i in range(start_bar, min(start_bar + 200, nq_n)):
            # Check if this bar sweeps the swing high
            if nq_high[bar_i] > swing_price:
                bar_ts = int(nq_ts[bar_i])

                # Find the corresponding ES bar at the same timestamp
                es_bar_idx = int(np.searchsorted(es_ts, bar_ts, side='left'))
                if es_bar_idx >= len(es_ts) or abs(es_ts[es_bar_idx] - bar_ts) > NS_PER_MIN:
                    break  # no matching ES bar

                # Find nearest prior ES swing high
                es_sh_before = np.searchsorted(es_sh_ts, bar_ts, side='left')
                if es_sh_before == 0:
                    break  # no prior ES swing high
                es_swing_price = float(es_sh_price[es_sh_before - 1])

                # Did ES also sweep its swing high?
                es_also_swept = float(es_high[es_bar_idx]) > es_swing_price
                if not es_also_swept:
                    # SMT divergence: NQ swept high, ES didn't
                    divergences.append({
                        'bar_idx':     bar_i,
                        'direction':   'SHORT',
                        'sweep_price': float(nq_high[bar_i]),
                        'swing_level': swing_price,
                        'ts_ns':       bar_ts,
                    })
                break  # only first sweep per swing

    # Scan NQ bars for sweeps of prior swing lows (bullish SMT -> LONG)
    for sl_idx_in_list in range(len(sl_list)):
        swing_idx, swing_price, swing_ts = sl_list[sl_idx_in_list]
        start_bar = swing_idx + 2
        if start_bar >= nq_n:
            continue

        for bar_i in range(start_bar, min(start_bar + 200, nq_n)):
            if nq_low[bar_i] < swing_price:
                bar_ts = int(nq_ts[bar_i])

                es_bar_idx = int(np.searchsorted(es_ts, bar_ts, side='left'))
                if es_bar_idx >= len(es_ts) or abs(es_ts[es_bar_idx] - bar_ts) > NS_PER_MIN:
                    break

                es_sl_before = np.searchsorted(es_sl_ts, bar_ts, side='left')
                if es_sl_before == 0:
                    break
                es_swing_price = float(es_sl_price[es_sl_before - 1])

                es_also_swept = float(es_low[es_bar_idx]) < es_swing_price
                if not es_also_swept:
                    divergences.append({
                        'bar_idx':     bar_i,
                        'direction':   'LONG',
                        'sweep_price': float(nq_low[bar_i]),
                        'swing_level': swing_price,
                        'ts_ns':       bar_ts,
                    })
                break

    return divergences


# ── INVERSE FVG DETECTION ─────────────────────────────────────────────────────
def find_inverse_fvg(arrs, start_ts_ns, direction, session_end_ns):
    """
    After SMT, scan forward for a 3-candle FVG that gets inverted.

    For LONG: bearish FVG = candle1.low > candle3.high (gap down)
              -> inverted when a subsequent bar's high trades back up into the gap
    For SHORT: bullish FVG = candle1.high < candle3.low (gap up)
              -> inverted when a subsequent bar's low trades back down into the gap

    Returns (ifvg_ts_ns, ifvg_high, ifvg_low) or (None, None, None).
    ifvg_ts_ns = timestamp of the bar that inverted the FVG.
    """
    ts    = arrs['ts_ns']
    highs = arrs['high']
    lows  = arrs['low']
    n     = len(ts)

    start_idx = int(np.searchsorted(ts, start_ts_ns, side='left'))
    if start_idx >= n:
        return None, None, None

    # Determine end based on session_end_ns
    end_idx = int(np.searchsorted(ts, session_end_ns, side='right'))
    end_idx = min(end_idx, n)

    if direction == 'LONG':
        # Looking for bearish FVG (gap down): candle1.low > candle3.high
        for i in range(start_idx, end_idx - 2):
            c1_low  = lows[i]
            c3_high = highs[i + 2]
            gap_size = c1_low - c3_high
            if gap_size >= MIN_IFVG_PTS:
                # FVG found: zone is (c3_high, c1_low)
                ifvg_low  = float(c3_high)
                ifvg_high = float(c1_low)
                # Now scan forward for inversion: bar whose high >= ifvg_low (trades into the gap)
                for j in range(i + 3, end_idx):
                    if highs[j] >= ifvg_low:
                        return int(ts[j]), ifvg_high, ifvg_low
        return None, None, None

    else:  # SHORT
        # Looking for bullish FVG (gap up): candle3.low > candle1.high
        for i in range(start_idx, end_idx - 2):
            c1_high = highs[i]
            c3_low  = lows[i + 2]
            gap_size = c3_low - c1_high
            if gap_size >= MIN_IFVG_PTS:
                ifvg_low  = float(c1_high)
                ifvg_high = float(c3_low)
                # Scan forward for inversion: bar whose low <= ifvg_high (trades into the gap)
                for j in range(i + 3, end_idx):
                    if lows[j] <= ifvg_high:
                        return int(ts[j]), ifvg_high, ifvg_low
        return None, None, None


# ── CISD DETECTION (copied from Fractal Sweep) ───────────────────────────────
def _find_cisd(c_opens, c_closes, c_ts_ns, start_idx, n_bars, direction):
    """
    CISD -- Change in State of Delivery (body-only).

    Step 1 -- BACKWARD scan from start_idx (the return bar):
      Find the consecutive opposing delivery run that formed the high/low
      BEFORE the return to range.
        SHORT: look for consecutive up-close candles (bullish delivery run)
        LONG:  look for consecutive down-close candles (bearish delivery run)

    cisd_level = open of the FIRST (earliest in time) candle in that run:
        SHORT: first = lowest open of the ascending bullish run
        LONG:  first = highest open of the descending bearish run

    Step 2 -- FORWARD scan from start_idx:
      Fire on the first bar whose close crosses cisd_level:
        LONG:  close > cisd_level
        SHORT: close < cisd_level

    Dojis (close == open) are neutral -- skipped, do not break the run.
    """
    end  = min(start_idx + n_bars, len(c_closes))
    back = max(0, start_idx - n_bars * 4)

    if direction == 'LONG':
        # Step 1: backward -- find consecutive bearish run before start_idx
        j = start_idx - 1
        while j >= back and c_closes[j] == c_opens[j]:
            j -= 1
        if j < back or c_closes[j] >= c_opens[j]:
            return None, None
        run_start = j
        k = j - 1
        while k >= back:
            if   c_closes[k] < c_opens[k]:  run_start = k; k -= 1
            elif c_closes[k] == c_opens[k]:  k -= 1
            else: break
        cisd_level = float(c_opens[run_start])

        # Step 2: forward -- first bar that closes above cisd_level
        for i in range(start_idx, end):
            if c_closes[i] > cisd_level:
                return c_ts_ns[i], cisd_level

    else:  # SHORT
        j = start_idx - 1
        while j >= back and c_closes[j] == c_opens[j]:
            j -= 1
        if j < back or c_closes[j] <= c_opens[j]:
            return None, None
        run_start = j
        k = j - 1
        while k >= back:
            if   c_closes[k] > c_opens[k]:  run_start = k; k -= 1
            elif c_closes[k] == c_opens[k]:  k -= 1
            else: break
        cisd_level = float(c_opens[run_start])

        for i in range(start_idx, end):
            if c_closes[i] < cisd_level:
                return c_ts_ns[i], cisd_level

    return None, None


def find_cisd(c_arrs, start_ts_ns, direction, session_end_ns):
    """
    Wrapper: uses searchsorted to find the integer index.
    Scans from start_ts_ns to session end.
    Returns (fired_ts_ns, cisd_level) or (None, None).
    """
    start_idx = int(np.searchsorted(c_arrs['ts_ns'], start_ts_ns, side='left'))
    if start_idx >= len(c_arrs['ts_ns']):
        return None, None
    end_idx = int(np.searchsorted(c_arrs['ts_ns'], session_end_ns, side='right'))
    n_forward = max(end_idx - start_idx, 40)
    return _find_cisd(
        c_arrs['open'], c_arrs['close'], c_arrs['ts_ns'],
        start_idx, n_forward, direction
    )


# ── STRUCTURAL OUTCOME RESOLUTION (close-based SL) ──────────────────────────
def resolve_outcomes_structural(arrs_5m, pending):
    """
    Resolve outcomes for the 555 model with CLOSE-BASED stop loss.

    Phase 1: scan 5m bars for SL (close through IFVG) or TP1 (1R on highs/lows)
      - SL: LONG -> close below ifvg_low; SHORT -> close above ifvg_high
             Checked on 5m CLOSES only (not wicks).
      - TP1: checked on highs/lows (intra-candle) at 1R.
    Phase 2 (if TP1 hits first): 90% exits at +1R; runner (10%) with BE stop on highs/lows.
      Runner marks to market at EOD or BE stop.
    net_r = 0.90 * 1.0 + 0.10 * runner_exit_r

    For LOSS: compute actual R at the close that triggered the stop.

    Returns list of (outcome, net_r, mae_pct, mfe_pct, tp1_hit, runner_exit_r).
    """
    ts_ns  = arrs_5m['ts_ns']
    highs  = arrs_5m['high']
    lows   = arrs_5m['low']
    closes = arrs_5m['close']
    N      = len(ts_ns)

    results = []
    for e in pending:
        entry_ts_ns  = e['entry_ts_ns']
        entry_price  = e['entry_price']
        stop_price   = e['stop_price']    # ifvg boundary
        target_price = e['target_price']  # entry +/- 1R
        direction    = e['direction']

        risk = abs(entry_price - stop_price)
        if risk < MIN_RISK_PTS or risk > MAX_RISK_PTS:
            results.append(('INVALID', 0.0, 0.0, 0.0, False, 0.0))
            continue

        start = int(np.searchsorted(ts_ns, entry_ts_ns, side='right'))
        end   = min(start + OUTCOME_MAX_BARS, N)

        if start >= N:
            results.append(('EXPIRED', 0.0, 0.0, 0.0, False, 0.0))
            continue

        h = highs[start:end]
        l = lows[start:end]
        c = closes[start:end]

        # TP1 checked on highs/lows (intra-candle)
        if direction == 'LONG':
            tp1_hit_arr = h >= target_price
            # SL checked on CLOSES only
            sl_hit_arr  = c <= stop_price
        else:
            tp1_hit_arr = l <= target_price
            sl_hit_arr  = c >= stop_price

        tp1_any = tp1_hit_arr.any()
        sl_any  = sl_hit_arr.any()

        tp1_idx = int(np.argmax(tp1_hit_arr)) if tp1_any else len(h)
        sl_idx  = int(np.argmax(sl_hit_arr))  if sl_any  else len(h)

        # MAE/MFE over full trade window
        if direction == 'LONG':
            mae_pct = round(float(max(0.0, entry_price - l.min()) / entry_price * 100), 4)
            mfe_pct = round(float(max(0.0, h.max() - entry_price) / entry_price * 100), 4)
        else:
            mae_pct = round(float(max(0.0, h.max() - entry_price) / entry_price * 100), 4)
            mfe_pct = round(float(max(0.0, entry_price - l.min()) / entry_price * 100), 4)

        # Outcome determination
        if not tp1_any and not sl_any:
            last_r = ((c[-1] - entry_price) / risk
                      if direction == 'LONG'
                      else (entry_price - c[-1]) / risk)
            results.append(('EXPIRED', round(float(last_r), 2), mae_pct, mfe_pct, False, 0.0))
            continue

        if sl_any and (not tp1_any or sl_idx < tp1_idx):
            # SL hit first -- compute actual R at the close that triggered
            sl_close = float(c[sl_idx])
            if direction == 'LONG':
                actual_loss_r = (sl_close - entry_price) / risk
            else:
                actual_loss_r = (entry_price - sl_close) / risk
            actual_loss_r = round(float(actual_loss_r), 3)
            results.append(('LOSS', actual_loss_r, mae_pct, mfe_pct, False, 0.0))
            continue

        # TP1 hit first -- run the runner with BE stop
        runner_start = tp1_idx + 1
        runner_end   = len(h)
        runner_exit_r = 0.0

        if runner_start < runner_end:
            rh = h[runner_start:runner_end]
            rl = l[runner_start:runner_end]
            if direction == 'LONG':
                be_hit = rl <= entry_price
            else:
                be_hit = rh >= entry_price

            if not be_hit.any():
                # Runner survived to EOD
                last_close = c[runner_end - 1]
                if direction == 'LONG':
                    runner_exit_r = (last_close - entry_price) / risk
                else:
                    runner_exit_r = (entry_price - last_close) / risk
                runner_exit_r = max(0.0, round(float(runner_exit_r), 3))

        net_r = round(0.90 * 1.0 + 0.10 * runner_exit_r, 3)
        results.append(('WIN', net_r, mae_pct, mfe_pct, True, runner_exit_r))

    return results


# ── SETUP DETECTION ───────────────────────────────────────────────────────────
def detect_setups(nq_5m_arrs, nq_3m_arrs, es_5m_arrs, m1_arrs):
    """
    Full 555 setup detection pipeline:
    1. Detect 5m swing fractals on NQ
    2. Find SMT divergences (NQ sweeps swing, ES doesn't)
    3. For each SMT, scan for IFVG (on 3m and 5m, earliest wins) and CISD (on 5m)
    4. Entry = next 5m candle open after BOTH IFVG and CISD have occurred
    5. Dedup: one setup per date + direction

    Returns (rows, pending) for outcome resolution.
    """
    nq_ts   = nq_5m_arrs['ts_ns']
    nq_open = nq_5m_arrs['open']
    nq_hr   = nq_5m_arrs['hr']
    nq_mn   = nq_5m_arrs['mn']
    nq_dow  = nq_5m_arrs['dow']
    nq_date = nq_5m_arrs['trade_date']

    m1_ts   = m1_arrs['ts_ns']
    m1_hr   = m1_arrs['hr']
    m1_mn   = m1_arrs['mn']
    m1_dow  = m1_arrs['dow']
    m1_date = m1_arrs['trade_date']
    m1_high = m1_arrs['high']
    m1_low  = m1_arrs['low']

    print("[3] Detecting 5m swing fractals on NQ ...")
    nq_swings = detect_swing_fractals(nq_5m_arrs)
    print(f"    {len(nq_swings['highs']):,} swing highs, {len(nq_swings['lows']):,} swing lows")

    print("[4] Detecting SMT divergences ...")
    smt_divs = detect_smt_divergences(nq_5m_arrs, es_5m_arrs, nq_swings)
    print(f"    {len(smt_divs):,} raw SMT divergences found")

    # Filter to session hours
    smt_session = []
    for d in smt_divs:
        bar_i = d['bar_idx']
        hr_val = float(nq_hr[bar_i])
        mn_val = float(nq_mn[bar_i])
        hr_f = hr_val + mn_val / 60.0
        if SESSION_HRS[0] <= hr_f < SESSION_HRS[1]:
            smt_session.append(d)
    print(f"    {len(smt_session):,} within session hours ({SESSION_HRS[0]}-{SESSION_HRS[1]})")

    # Precompute hourly range lookup: (date, hr) -> high-low pts
    _hr_range = {}
    _prev_date_hr = (None, None)
    _hr_hi = _hr_lo = 0.0
    for _k in range(len(m1_ts)):
        _dh = (m1_date[_k], m1_hr[_k])
        if _dh != _prev_date_hr:
            if _prev_date_hr[0] is not None:
                _hr_range[_prev_date_hr] = _hr_hi - _hr_lo
            _prev_date_hr = _dh
            _hr_hi = m1_high[_k]
            _hr_lo = m1_low[_k]
        else:
            if m1_high[_k] > _hr_hi: _hr_hi = m1_high[_k]
            if m1_low[_k]  < _hr_lo: _hr_lo = m1_low[_k]
    if _prev_date_hr[0] is not None:
        _hr_range[_prev_date_hr] = _hr_hi - _hr_lo

    print(f"[5] Scanning for IFVG + CISD after each SMT ({len(_hr_range):,} hourly ranges) ...")

    rows    = []
    pending = []
    seen_date_dir = set()  # dedup: one per date + direction

    for smt in smt_session:
        bar_i     = smt['bar_idx']
        direction = smt['direction']
        smt_ts_ns = smt['ts_ns']

        # Get date info from 5m arrays
        trade_date = nq_date[bar_i]
        date_str   = str(trade_date)

        # Dedup: first SMT per day per direction
        dedup_key = (date_str, direction)
        if dedup_key in seen_date_dir:
            continue
        seen_date_dir.add(dedup_key)

        yr_val  = int(nq_5m_arrs['yr'][bar_i])
        dow_val = int(nq_dow[bar_i])
        hr_val  = int(nq_hr[bar_i])
        mn_val  = int(nq_mn[bar_i])

        # Compute session end timestamp for this day (16:00 ET)
        # Use the SMT bar's date to find 16:00 on same day
        # Find the last 5m bar with hr < 16 on this date, or use a time calc
        date_mask = (nq_date == trade_date)
        day_indices = np.where(date_mask)[0]
        if len(day_indices) == 0:
            continue
        # Session end = last bar of this day within session
        last_day_idx = day_indices[-1]
        session_end_ns = int(nq_ts[last_day_idx]) + 5 * NS_PER_MIN

        # Scan for IFVG on 3m (earliest) and 5m
        ifvg_5m_ts, ifvg_5m_high, ifvg_5m_low = find_inverse_fvg(
            nq_5m_arrs, smt_ts_ns, direction, session_end_ns)
        ifvg_3m_ts, ifvg_3m_high, ifvg_3m_low = find_inverse_fvg(
            nq_3m_arrs, smt_ts_ns, direction, session_end_ns)

        # Pick earliest IFVG
        ifvg_ts = ifvg_high = ifvg_low = None
        if ifvg_5m_ts is not None and ifvg_3m_ts is not None:
            if ifvg_3m_ts <= ifvg_5m_ts:
                ifvg_ts, ifvg_high, ifvg_low = ifvg_3m_ts, ifvg_3m_high, ifvg_3m_low
            else:
                ifvg_ts, ifvg_high, ifvg_low = ifvg_5m_ts, ifvg_5m_high, ifvg_5m_low
        elif ifvg_3m_ts is not None:
            ifvg_ts, ifvg_high, ifvg_low = ifvg_3m_ts, ifvg_3m_high, ifvg_3m_low
        elif ifvg_5m_ts is not None:
            ifvg_ts, ifvg_high, ifvg_low = ifvg_5m_ts, ifvg_5m_high, ifvg_5m_low

        if ifvg_ts is None:
            # No IFVG found
            rows.append(dict(
                date=date_str, yr=yr_val, dow=dow_val, direction=direction,
                hr=hr_val, mn=mn_val, session=get_session(hr_val + mn_val / 60.0),
                entry_price=None, stop_price=None, target_price=None,
                risk_pts=None, r=0.0, outcome='SKIP', rejected_by='NO_IFVG',
                mae_pct=None, mfe_pct=None, mae_pct_hr=None, mfe_pct_hr=None,
                hour_range_pts=None, ifvg_high=None, ifvg_low=None,
                cisd_level=None, smt=True,
                sweep_price=smt['sweep_price'], swing_level=smt['swing_level'],
            ))
            continue

        # Scan for CISD on 5m after SMT
        cisd_ts_ns, cisd_level = find_cisd(nq_5m_arrs, smt_ts_ns, direction, session_end_ns)

        if cisd_ts_ns is None:
            rows.append(dict(
                date=date_str, yr=yr_val, dow=dow_val, direction=direction,
                hr=hr_val, mn=mn_val, session=get_session(hr_val + mn_val / 60.0),
                entry_price=None, stop_price=None, target_price=None,
                risk_pts=None, r=0.0, outcome='SKIP', rejected_by='NO_CISD',
                mae_pct=None, mfe_pct=None, mae_pct_hr=None, mfe_pct_hr=None,
                hour_range_pts=None, ifvg_high=round(ifvg_high, 2), ifvg_low=round(ifvg_low, 2),
                cisd_level=None, smt=True,
                sweep_price=smt['sweep_price'], swing_level=smt['swing_level'],
            ))
            continue

        # Both IFVG and CISD found -- entry at next 5m candle open after BOTH complete
        trigger_ts = max(ifvg_ts, cisd_ts_ns)

        # Find next 5m candle after trigger
        entry_5m_idx = int(np.searchsorted(nq_ts, trigger_ts, side='right'))
        if entry_5m_idx >= len(nq_ts):
            continue

        entry_ts_ns = int(nq_ts[entry_5m_idx])
        entry_price = float(nq_open[entry_5m_idx])

        # Stop = IFVG invalidation
        if direction == 'LONG':
            stop_price = ifvg_low
        else:
            stop_price = ifvg_high

        risk_pts = abs(entry_price - stop_price)

        # Entry must be on correct side of stop
        if direction == 'LONG' and entry_price <= stop_price:
            continue
        if direction == 'SHORT' and entry_price >= stop_price:
            continue

        # Target = 1R from entry
        if direction == 'LONG':
            target_price = entry_price + risk_pts
        else:
            target_price = entry_price - risk_pts

        # Get entry time info from 5m arrays
        entry_hr  = int(nq_hr[entry_5m_idx])
        entry_mn  = int(nq_mn[entry_5m_idx])
        entry_dow = int(nq_dow[entry_5m_idx])
        entry_date = nq_date[entry_5m_idx]
        entry_date_str = str(entry_date)

        # Hourly range
        _hr_rng = _hr_range.get((entry_date, entry_hr), 0.0)

        row = dict(
            date          = entry_date_str,
            yr            = yr_val,
            dow           = entry_dow,
            direction     = direction,
            hr            = entry_hr,
            mn            = entry_mn,
            session       = get_session(entry_hr + entry_mn / 60.0),
            entry_price   = round(entry_price, 2),
            stop_price    = round(stop_price, 2),
            target_price  = round(target_price, 2),
            risk_pts      = round(risk_pts, 2),
            ifvg_high     = round(ifvg_high, 2),
            ifvg_low      = round(ifvg_low, 2),
            cisd_level    = round(cisd_level, 2) if cisd_level else None,
            smt           = True,
            sweep_price   = round(smt['sweep_price'], 2),
            swing_level   = round(smt['swing_level'], 2),
            hour_range_pts= round(float(_hr_rng), 2),
            rejected_by   = '',
            outcome       = '',
            r             = 0.0,
            mae_pct       = None,
            mfe_pct       = None,
            mae_pct_hr    = None,
            mfe_pct_hr    = None,
            tp1_hit       = False,
            runner_exit_r = 0.0,
        )

        # Risk validation
        if risk_pts < MIN_RISK_PTS:
            row['rejected_by'] = 'RISK_TOO_SMALL'
            row['outcome'] = 'INVALID'
            rows.append(row)
            continue
        if risk_pts > MAX_RISK_PTS:
            row['rejected_by'] = 'RISK_TOO_LARGE'
            row['outcome'] = 'INVALID'
            rows.append(row)
            continue

        rows.append(row)
        pending.append(dict(
            idx           = len(rows) - 1,
            entry_ts_ns   = entry_ts_ns,
            entry_price   = entry_price,
            stop_price    = stop_price,
            target_price  = target_price,
            direction     = direction,
            hour_range_pts= float(_hr_rng),
        ))

    print(f"    {len(pending):,} valid entries for outcome resolution")
    print(f"    {len(rows):,} total rows (including skips/invalids)")
    return rows, pending


# ── FULL DISTRIBUTION STATS ──────────────────────────────────────────────────
def _dist_stats(vals_series, n_bins=30):
    """Return full percentile stats + histogram for a pandas Series."""
    vals = vals_series.dropna()
    vals = vals[vals > 0]
    if len(vals) == 0:
        return {}
    counts, edges = np.histogram(vals, bins=n_bins)
    hist = [{'lo': round(float(edges[i]), 4),
             'hi': round(float(edges[i+1]), 4),
             'n':  int(counts[i])} for i in range(len(counts))]
    mode_bucket = max(hist, key=lambda x: x['n'])
    mode_val = round((mode_bucket['lo'] + mode_bucket['hi']) / 2, 4)
    return dict(
        count = int(len(vals)),
        min   = round(float(vals.min()), 4),
        p10   = round(float(vals.quantile(.10)), 4),
        p25   = round(float(vals.quantile(.25)), 4),
        p50   = round(float(vals.quantile(.50)), 4),
        p75   = round(float(vals.quantile(.75)), 4),
        p90   = round(float(vals.quantile(.90)), 4),
        p95   = round(float(vals.quantile(.95)), 4),
        p99   = round(float(vals.quantile(.99)), 4),
        max   = round(float(vals.max()), 4),
        mean  = round(float(vals.mean()), 4),
        std   = round(float(vals.std(ddof=1)), 4) if len(vals) > 1 else 0.0,
        mode  = mode_val,
        hist  = hist,
    )


def _lognorm_fit(vals_np):
    """Return log-normal fit params for a positive numpy array."""
    lv = np.log(vals_np[vals_np > 0])
    if len(lv) < 5:
        return {}
    mu    = float(lv.mean())
    sigma = float(lv.std(ddof=1))
    n     = len(vals_np)
    probs = (np.arange(1, n + 1) - 0.5) / n
    theoretical = np.exp(mu + sigma * _scipy_stats.norm.ppf(probs))
    empirical   = np.sort(vals_np)
    r = float(np.corrcoef(empirical, theoretical)[0, 1]) if len(empirical) > 1 else 0.0
    return dict(
        mu             = round(mu, 4),
        sigma          = round(sigma, 4),
        implied_median = round(float(np.exp(mu)), 4),
        implied_mean   = round(float(np.exp(mu + sigma**2 / 2)), 4),
        implied_mode   = round(float(np.exp(mu - sigma**2)), 4),
        goodness       = round(r, 4),
    )


def _clusters(vals, n):
    """Return 3-tier cluster stats at p33 / p75 breakpoints."""
    p33 = float(vals.quantile(0.33))
    p75 = float(vals.quantile(0.75))
    tiers = [
        (0,   p33,          'Tight',    '0 -> p33'),
        (p33, p75,          'Moderate', 'p33 -> p75'),
        (p75, float('inf'), 'Wide',     'p75 -> max'),
    ]
    out = []
    for lo, hi, label, rng in tiers:
        if hi < float('inf'):
            mask = (vals >= lo) & (vals < hi)
        else:
            mask = vals >= lo
        cv = vals[mask]
        out.append(dict(
            label        = label,
            range        = rng,
            n            = int(len(cv)),
            pct_of_trades= round(len(cv) / max(n, 1) * 100, 1),
            mean         = round(float(cv.mean()), 4)   if len(cv) else 0,
            median       = round(float(cv.median()), 4) if len(cv) else 0,
            max          = round(float(cv.max()), 4)    if len(cv) else 0,
        ))
    return out


def _build_excursion_heatmap(wl, col, n_bins=20):
    """Pre-bin MAE or MFE by day-of-week."""
    vals = wl[[col, 'dow']].dropna()
    vals = vals[vals[col] >= 0]
    if len(vals) < 5:
        return {'grid': [], 'val_max': 0.5, 'labels': _DOW_LABELS, 'n': 0}
    val_max = float(vals[col].quantile(0.95)) or 0.5
    grid = [[0] * n_bins for _ in range(5)]
    for row in vals.itertuples():
        dow_idx = _DOW_ORDER.index(int(row.dow)) if int(row.dow) in _DOW_ORDER else None
        if dow_idx is None:
            continue
        xi = min(int(getattr(row, col) / val_max * n_bins), n_bins - 1)
        grid[dow_idx][xi] += 1
    return {'grid': grid, 'val_max': round(val_max, 4), 'labels': _DOW_LABELS, 'n': len(vals)}


def _full_mae_stats(wl, ce=None, n_bins=50):
    """Rich MAE distribution: log-normal fit, clusters, full percentiles, SL sweep."""
    vals = wl['mae_pct'].dropna()
    vals = vals[vals > 0]
    n = len(vals)
    if n < 20:
        return None

    p99_val  = float(vals.quantile(0.99))
    clipped  = vals[vals <= p99_val]
    counts_h, edges_h = np.histogram(clipped, bins=n_bins)
    mode_idx = int(np.argmax(counts_h))
    mode_v   = round((float(edges_h[mode_idx]) + float(edges_h[mode_idx + 1])) / 2, 4)

    pct_levels = [5,10,15,20,25,30,35,40,50,60,65,70,75,80,85,90,95,99]
    percentiles = {f'p{p}': round(float(vals.quantile(p / 100)), 4) for p in pct_levels}

    raw_thresholds = [float(vals.quantile(p / 100)) for p in [5,10,15,20,25,30,35,40,50,60,70,80,90,95]]
    thresholds = sorted({round(t, 4) for t in raw_thresholds})
    sl_sweep = []
    best_opt = None; _opt_fallback = None
    for thr in thresholds:
        touched = wl[wl['mae_pct'] >= thr]
        if len(touched) == 0:
            continue
        nt = len(touched)
        n_win  = int((touched['outcome'] == 'WIN').sum())
        n_loss = int((touched['outcome'] == 'LOSS').sum())
        p_rec = round(n_win  / nt, 4)
        p_ko  = round(n_loss / nt, 4)
        exc   = round(nt / n * 100, 1)
        sl_sweep.append(dict(threshold=thr, exceed_pct=exc, p_recovered=p_rec, p_ko=p_ko))
        if best_opt is None and p_ko >= 0.70:
            best_opt = thr
        if _opt_fallback is None and p_ko >= 0.50:
            _opt_fallback = thr

    if best_opt is None:
        best_opt = _opt_fallback

    return dict(
        n          = n,
        mean       = round(float(vals.mean()), 4),
        median     = round(float(vals.median()), 4),
        std        = round(float(vals.std(ddof=1)), 4),
        mode       = mode_v,
        skewness   = round(float(_scipy_stats.skew(vals)), 3),
        kurtosis   = round(float(_scipy_stats.kurtosis(vals)), 3),
        percentiles= percentiles,
        lognorm    = _lognorm_fit(vals.values),
        clusters   = _clusters(vals, n),
        histogram  = dict(
            edges  = [round(float(e), 4) for e in edges_h],
            counts = [int(c) for c in counts_h],
        ),
        sl_sweep   = sl_sweep,
        opt_sl     = best_opt,
        ce         = ce,
    )


def _full_mfe_stats(wl, n_bins=50):
    """Rich MFE distribution: log-normal fit, clusters, full percentiles, BE triggers, PTQ."""
    vals = wl['mfe_pct'].dropna()
    vals = vals[vals > 0]
    n = len(vals)
    if n < 20:
        return None

    p99_val  = float(vals.quantile(0.99))
    clipped  = vals[vals <= p99_val]
    counts_h, edges_h = np.histogram(clipped, bins=n_bins)
    mode_idx = int(np.argmax(counts_h))
    mode_v   = round((float(edges_h[mode_idx]) + float(edges_h[mode_idx + 1])) / 2, 4)

    pct_levels = [5,10,15,20,25,30,35,40,50,60,65,70,75,80,85,90,95,99]
    percentiles = {f'p{p}': round(float(vals.quantile(p / 100)), 4) for p in pct_levels}

    p33 = float(vals.quantile(0.33))
    p75 = float(vals.quantile(0.75))
    mfe_tiers = [
        (0,   p33,          'Small',    '0 -> p33'),
        (p33, p75,          'Moderate', 'p33 -> p75'),
        (p75, float('inf'), 'Large',    'p75 -> max'),
    ]
    clusters = []
    for lo, hi, label, rng in mfe_tiers:
        mask = (vals >= lo) & (vals < hi) if hi < float('inf') else (vals >= lo)
        cv = vals[mask]
        clusters.append(dict(
            label=label, range=rng, n=int(len(cv)),
            pct_of_trades=round(len(cv) / max(n, 1) * 100, 1),
            mean=round(float(cv.mean()), 4) if len(cv) else 0,
            median=round(float(cv.median()), 4) if len(cv) else 0,
            max=round(float(cv.max()), 4) if len(cv) else 0,
        ))

    net_r_col = 'net_r' if 'net_r' in wl.columns else None
    if net_r_col is None and 'r' in wl.columns:
        net_r_col = 'r'
    ev_base   = round(float(wl[net_r_col].mean()), 4) if net_r_col else None
    avg_loss  = float(wl.loc[wl[net_r_col] < 0, net_r_col].mean()) if net_r_col and (wl[net_r_col] < 0).any() else -1.0

    raw_trigs = [float(vals.quantile(p / 100)) for p in [5,10,15,20,25,30,35,40,50,60,70,80,90]]
    triggers  = sorted({round(t, 4) for t in raw_trigs})
    be_triggers = []
    ptq_level = None; ptq_reach_rate = None
    _ptq_fallback = None; _ptq_fallback_rr = None
    for thr in triggers:
        reached = wl[wl['mfe_pct'] >= thr]
        if len(reached) == 0:
            continue
        nr = len(reached)
        reach_rate = round(nr / n * 100, 1)
        if net_r_col:
            n_pos = int((reached[net_r_col] > 0).sum())
            n_neg = int((reached[net_r_col] <= 0).sum())
        else:
            n_pos = int((reached['outcome'] == 'WIN').sum())
            n_neg = int((reached['outcome'] == 'LOSS').sum())
        p_pos = round(n_pos / nr, 4)
        n_rescued = n_neg
        ev_delta  = round(n_rescued / n * abs(avg_loss), 4)
        new_ev    = round((ev_base or 0) + ev_delta, 4)
        be_triggers.append(dict(
            trigger_pct = thr,
            reach_rate  = reach_rate,
            p_pos_given = p_pos,
            n_rescued   = n_rescued,
            ev_delta    = ev_delta,
            new_ev      = new_ev,
        ))
        if ptq_level is None and p_pos >= 0.70:
            ptq_level = thr; ptq_reach_rate = reach_rate
        if _ptq_fallback is None and p_pos >= 0.50:
            _ptq_fallback = thr; _ptq_fallback_rr = reach_rate

    if ptq_level is None and _ptq_fallback is not None:
        ptq_level = _ptq_fallback; ptq_reach_rate = _ptq_fallback_rr

    return dict(
        n           = n,
        mean        = round(float(vals.mean()), 4),
        median      = round(float(vals.median()), 4),
        std         = round(float(vals.std(ddof=1)), 4),
        mode        = mode_v,
        skewness    = round(float(_scipy_stats.skew(vals)), 3),
        kurtosis    = round(float(_scipy_stats.kurtosis(vals)), 3),
        percentiles = percentiles,
        lognorm     = _lognorm_fit(vals.values),
        clusters    = clusters,
        histogram   = dict(
            edges  = [round(float(e), 4) for e in edges_h],
            counts = [int(c) for c in counts_h],
        ),
        be_triggers    = be_triggers,
        ptq_level      = ptq_level,
        ptq_reach_rate = ptq_reach_rate,
    )


# ── AGGREGATION ───────────────────────────────────────────────────────────────
def agg(g):
    """Aggregate a group of trades into summary stats."""
    n = len(g)
    if n == 0:
        return dict(n=0, wins=0, wr=0, ev=0, pf=0, avg_risk_pts=0, avg_rr=0,
                    avg_mae=None, avg_mfe=None, avg_mae_hr=None, avg_mfe_hr=None)
    wins    = int(g['win'].sum())
    wr      = round(wins / n, 4)
    ev      = round(float(g['r'].sum()) / n, 3)
    win_r   = float(g.loc[g['win'] == 1, 'r'].sum())
    loss_r  = float(abs(g.loc[g['win'] == 0, 'r'].sum()))
    pf      = round(win_r / max(loss_r, 0.001), 3)
    avg_rr  = round(win_r / max(wins, 1), 2)
    ar      = round(float(g['risk_pts'].mean()), 1) \
              if 'risk_pts' in g.columns and g['risk_pts'].notna().any() else 0
    _mae = g['mae_pct'].dropna() if 'mae_pct' in g.columns else None
    _mfe = g['mfe_pct'].dropna() if 'mfe_pct' in g.columns else None
    avg_mae = round(float(_mae.mean()), 4) if _mae is not None and len(_mae) > 0 else None
    avg_mfe = round(float(_mfe.mean()), 4) if _mfe is not None and len(_mfe) > 0 else None
    _mae_hr = g['mae_pct_hr'].dropna() if 'mae_pct_hr' in g.columns else None
    _mfe_hr = g['mfe_pct_hr'].dropna() if 'mfe_pct_hr' in g.columns else None
    avg_mae_hr = round(float(_mae_hr.mean()), 4) if _mae_hr is not None and len(_mae_hr) > 0 else None
    avg_mfe_hr = round(float(_mfe_hr.mean()), 4) if _mfe_hr is not None and len(_mfe_hr) > 0 else None
    return dict(n=n, wins=wins, wr=wr, ev=ev, pf=pf, avg_risk_pts=ar, avg_rr=avg_rr,
                avg_mae=avg_mae, avg_mfe=avg_mfe, avg_mae_hr=avg_mae_hr, avg_mfe_hr=avg_mfe_hr)


# ── FILTER IMPACT ─────────────────────────────────────────────────────────────
def compute_filter_impact(df_all):
    FILTER_ORDER = ['RISK_TOO_SMALL', 'RISK_TOO_LARGE', 'NO_IFVG', 'NO_CISD']
    FILTER_LABELS_MAP = {
        'RISK_TOO_SMALL': 'Risk Too Small (< 3 pts)',
        'RISK_TOO_LARGE': 'Risk Too Large (> $225 MNQ)',
        'NO_IFVG':        'No Inverse FVG Found',
        'NO_CISD':        'No CISD Formed',
    }
    def ev_of(df):
        wl = df[df['outcome'].isin(['WIN','LOSS'])].copy()
        if len(wl) == 0: return 0, 0, 0, 0
        wl['win'] = (wl['outcome'] == 'WIN').astype(int)
        wins  = int(wl['win'].sum())
        wr    = wins / len(wl)
        ev    = float(wl['r'].sum()) / len(wl)
        win_r = float(wl.loc[wl['win'] == 1, 'r'].sum())
        pf    = win_r / max(float(abs(wl.loc[wl['win'] == 0, 'r'].sum())), 0.001)
        return round(wr,4), round(ev,3), round(pf,3), len(wl)

    base = df_all[~df_all['outcome'].isin(['SKIP','INVALID'])].copy()
    wr0, ev0, pf0, n0 = ev_of(base)
    results = [dict(label='Baseline (unfiltered)', n=n0, wr=wr0, ev=ev0,
                    pf=pf0, removed=0)]
    remaining = base.copy()
    for fcode in FILTER_ORDER:
        removed = remaining[remaining['rejected_by'] == fcode]
        if len(removed) == 0: continue
        remaining = remaining[remaining['rejected_by'] != fcode]
        wr, ev_v, pf, n = ev_of(remaining)
        results.append(dict(label=FILTER_LABELS_MAP.get(fcode, fcode),
                            filter_code=fcode, n=n, wr=wr, ev=ev_v, pf=pf,
                            removed=len(removed)))
    return results


def compute_filter_variants(df_all):
    """Analyze filter combinations."""
    FILTERS = ['RISK_TOO_SMALL', 'RISK_TOO_LARGE']
    FILTER_LABELS = {
        'RISK_TOO_SMALL': 'Risk Too Small',
        'RISK_TOO_LARGE': 'Risk Too Large',
    }

    def stats_of(df):
        wl = df[df['outcome'].isin(['WIN', 'LOSS'])].copy()
        if len(wl) == 0:
            return dict(n=0, wr=0, ev=0, pf=0, spd=0,
                        avg_risk=0, max_dd_pct=0, sharpe=None)
        wl['win'] = (wl['outcome'] == 'WIN').astype(int)
        n = len(wl)
        wins = int(wl['win'].sum())
        wr = round(wins / n, 4)
        win_r = float(wl.loc[wl['win'] == 1, 'r'].sum())
        loss_r = float(abs(wl.loc[wl['win'] == 0, 'r'].sum()))
        ev = round(float(wl['r'].sum()) / n, 3)
        pf = round(win_r / max(loss_r, 0.001), 3)
        avg_risk = round(float(wl['risk_pts'].mean()), 1) if 'risk_pts' in wl.columns and wl['risk_pts'].notna().any() else 0
        eq = float(ACCOUNT_SIZE); peak = eq; max_dd = 0.0
        for r_val in wl['r'].values:
            eq += float(r_val) * RISK_PER_TRADE
            if eq > peak: peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            if dd > max_dd: max_dd = dd
        daily_pnl = {}
        for _, row in wl.iterrows():
            d = str(row['date'])[:10]
            daily_pnl[d] = daily_pnl.get(d, 0) + float(row['r']) * RISK_PER_TRADE
        dp = list(daily_pnl.values())
        sharpe = None
        if len(dp) > 1:
            mu = sum(dp) / len(dp)
            sd = (sum((v - mu)**2 for v in dp) / (len(dp) - 1)) ** 0.5
            if sd > 0:
                sharpe = round(mu / sd * (252 ** 0.5), 2)
        dates = wl['date'].astype(str).str[:10].nunique()
        spd = round(n / max(dates, 1), 2) if dates else 0
        return dict(n=n, wr=wr, ev=ev, pf=pf, spd=spd,
                    avg_risk=avg_risk, max_dd_pct=round(max_dd * 100, 2),
                    sharpe=sharpe)

    all_valid = df_all[~df_all['outcome'].isin(['SKIP', 'INVALID'])].copy()

    def apply_filters(active_set):
        mask = pd.Series(True, index=all_valid.index)
        for f in active_set:
            mask &= all_valid['rejected_by'] != f
        return all_valid[mask]

    fully_filtered = apply_filters(FILTERS)
    baseline = stats_of(fully_filtered)
    baseline['label'] = 'All Filters (current)'
    baseline['filters'] = list(FILTERS)

    unfiltered = stats_of(all_valid)
    unfiltered['label'] = 'No Filters (raw)'
    unfiltered['filters'] = []

    individual_removal = []
    for f in FILTERS:
        remaining = [ff for ff in FILTERS if ff != f]
        kept = apply_filters(remaining)
        s = stats_of(kept)
        s['label'] = f'Without {FILTER_LABELS.get(f, f)}'
        s['removed_filter'] = f
        s['ev_delta'] = round(s['ev'] - baseline['ev'], 3)
        s['wr_delta'] = round((s['wr'] - baseline['wr']) * 100, 2)
        s['n_added'] = s['n'] - baseline['n']
        individual_removal.append(s)

    from itertools import combinations
    combos = []
    for r in range(len(FILTERS) + 1):
        for combo in combinations(FILTERS, r):
            kept = apply_filters(list(combo))
            s = stats_of(kept)
            s['filters'] = list(combo)
            s['label'] = ' + '.join(FILTER_LABELS.get(f, f) for f in combo) if combo else 'No Filters'
            s['n_filters'] = len(combo)
            combos.append(s)
    combos.sort(key=lambda x: x['ev'], reverse=True)
    best = combos[0] if combos else None

    return dict(
        baseline=baseline,
        unfiltered=unfiltered,
        individual_removal=individual_removal,
        all_combinations=combos,
        best_combination=best,
    )


# ── BY CLASSIFICATION ─────────────────────────────────────────────────────────
def _compute_by_classification(wl_sorted):
    """Compute WR / PF / PnL / Sharpe / MaxDD per day classification."""
    if not DATE_CLASSIFICATION or wl_sorted is None or len(wl_sorted) == 0:
        return {}
    cls_order = ['DWP', 'DNP', 'R1', 'R2', 'Unclassified']
    result = {}
    for cls in cls_order:
        dates = {d for d, c in DATE_CLASSIFICATION.items() if c == cls}
        sub = wl_sorted[wl_sorted['date'].astype(str).str[:10].isin(dates)].copy()
        n = len(sub)
        if n == 0:
            result[cls] = None
            continue
        wins   = int((sub['win'] == 1).sum())
        losses = n - wins
        wr     = round(wins / n * 100, 2)
        gross_win  = float(sub.loc[sub['win'] == 1, 'r'].sum()) * RISK_PER_TRADE
        gross_loss = abs(float(sub.loc[sub['win'] == 0, 'r'].sum())) * RISK_PER_TRADE
        pf     = round(gross_win / gross_loss, 3) if gross_loss > 0 else 0.0
        eq = float(ACCOUNT_SIZE)
        peak_eq = eq
        max_dd = 0.0
        daily_pnl = {}
        for _, row in sub.iterrows():
            r_val = row['r']
            if r_val is None or np.isnan(float(r_val)):
                continue
            trade_pnl = float(r_val) * RISK_PER_TRADE
            eq += trade_pnl
            if eq > peak_eq:
                peak_eq = eq
            dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            try:
                td = str(row['date'])[:10]
                daily_pnl[td] = daily_pnl.get(td, 0.0) + trade_pnl
            except Exception:
                pass
        total_pnl = round(eq - ACCOUNT_SIZE, 0)
        max_dd_pct = round(max_dd * 100, 2)
        blown = eq <= 0
        if len(daily_pnl) > 1:
            dpnl = np.array(list(daily_pnl.values()))
            sharpe = round(float(dpnl.mean() / dpnl.std(ddof=1) * np.sqrt(252)), 2) \
                     if dpnl.std(ddof=1) > 0 else None
        else:
            sharpe = None
        result[cls] = {
            'trades': n, 'wins': wins, 'losses': losses,
            'wr': wr, 'pf': pf, 'pnl': total_pnl,
            'sharpe': sharpe, 'max_dd': max_dd_pct, 'blown': blown,
        }
    return result


# ── BY TIMEFRAME ──────────────────────────────────────────────────────────────
def _compute_by_tf(wl, wl_sorted):
    """Compute compact hero+chart stats for each sub-timeframe slice."""
    from datetime import date, timedelta

    if wl is None or len(wl) == 0:
        return {}

    max_date_str = str(wl_sorted['date'].max())[:10]
    try:
        ref = date.fromisoformat(max_date_str)
    except Exception:
        return {}

    TF_CUTOFFS = {
        '2y': ref - timedelta(days=730),
        '1y': ref - timedelta(days=365),
        '6m': ref - timedelta(days=182),
        '3m': ref - timedelta(days=91),
        '1m': ref - timedelta(days=30),
    }

    result = {}
    dates_str = wl['date'].astype(str).str[:10]
    for tf_key, cutoff in TF_CUTOFFS.items():
        mask = dates_str >= cutoff.isoformat()
        sub  = wl[mask]
        if len(sub) < 3:
            result[tf_key] = None
            continue
        sub = sub.copy()
        if 'win' not in sub.columns:
            sub['win'] = (sub['outcome'] == 'WIN').astype(int)

        n     = len(sub)
        wins  = int((sub['win'] == 1).sum())
        wr    = round(wins / n, 4)
        ev    = round(float(sub['r'].sum()) / n, 3)
        win_r = float(sub.loc[sub['win'] == 1, 'r'].sum())
        los_r = float(abs(sub.loc[sub['win'] == 0, 'r'].sum()))
        pf    = round(win_r / max(los_r, 0.001), 3)
        date_range = f"{str(sub['date'].min())[:10]} - {str(sub['date'].max())[:10]}"

        # Equity / risk stats
        ws = sub.sort_values('date')
        eq = float(ACCOUNT_SIZE); peak_eq = eq; max_dd = 0.0; min_eq = eq
        daily_pnl = {}
        for _, row in ws.iterrows():
            r_val = row['r']
            if r_val is None or np.isnan(float(r_val)): continue
            tp = float(r_val) * RISK_PER_TRADE
            eq += tp
            if eq < min_eq: min_eq = eq
            if eq > peak_eq: peak_eq = eq
            dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0.0
            if dd > max_dd: max_dd = dd
            try:
                td = str(row['date'])[:10]
                daily_pnl[td] = daily_pnl.get(td, 0.0) + tp
            except Exception:
                pass
        total_pnl = round(eq - ACCOUNT_SIZE, 2)
        max_dd_pct = round(max_dd * 100, 2)
        blown = eq <= 0.0
        if len(daily_pnl) > 1:
            dpnl = np.array(list(daily_pnl.values()))
            sharpe = round(float(dpnl.mean() / dpnl.std(ddof=1) * np.sqrt(252)), 2) \
                     if dpnl.std(ddof=1) > 0 else None
        else:
            sharpe = None
        ce = round(ev * pf, 6) if pf and n > 0 else None

        # by_hour
        bh = []
        for (hr, direction), g in sub.groupby(['hr', 'direction']):
            s = agg(g)
            if s['n'] >= 3:
                s.update(hr=int(hr), hr_label=HR_LABELS.get(int(hr), f'{int(hr):02d}:00'))
                bh.append(s)
        # by_session
        bs = []
        for (sess, direction), g in sub.groupby(['session', 'direction']):
            s = agg(g); s.update(session=sess, direction=direction); bs.append(s)
        # by_dow
        bd = []
        for (dow, direction), g in sub.groupby(['dow', 'direction']):
            s = agg(g)
            if s['n'] >= 3:
                s.update(dow=int(dow), dow_name=DOW_NAMES.get(int(dow), '?'))
                bd.append(s)
        # dir_summary
        ds = []
        for direction, g in sub.groupby('direction'):
            s = agg(g); s['direction'] = direction; ds.append(s)
        # by_year
        sub_yr = sub.copy()
        sub_yr['year'] = sub_yr['date'].astype(str).str[:4].astype(int)
        by_yr = []
        for yr, g in sub_yr.groupby('year'):
            s = agg(g); s['yr'] = int(yr); by_yr.append(s)

        # recent trades
        recent_cols = ['date','direction','hr','mn','session','dow','entry_price',
                       'stop_price','target_price','risk_pts','r','outcome',
                       'mae_pct','mfe_pct','ifvg_high','ifvg_low','cisd_level']
        available = [c for c in recent_cols if c in sub.columns]
        rt = sub[available].sort_values('date', ascending=False).copy()
        rt['dow_name'] = rt['dow'].map(lambda d: DOW_NAMES.get(int(d), '?'))
        rt['classification'] = rt['date'].astype(str).str[:10].map(
            lambda d: DATE_CLASSIFICATION.get(d, 'Unclassified'))
        recent_trades = rt.to_dict('records')
        for t in recent_trades:
            t['date'] = str(t['date'])[:10]
            for k in ['dow','hr','mn']:
                if k in t: t[k] = int(t[k])
            for k in ['entry_price','stop_price','target_price','risk_pts','r']:
                if k in t and t[k] is not None:
                    t[k] = round(float(t[k]), 2)

        outcomes_seq = ws['win'].tolist()
        def _mc(seq, val):
            mx = cur = 0
            for v in seq:
                cur = cur + 1 if v == val else 0; mx = max(mx, cur)
            return mx

        result[tf_key] = {
            'meta': {
                'total_wl': n, 'win_rate': wr, 'ev_per_trade': ev,
                'profit_factor': pf, 'date_range': date_range, 'rr_target': 1.0,
            },
            'risk_stats': {
                'account_size': ACCOUNT_SIZE, 'risk_per_trade': RISK_PER_TRADE,
                'trades': n, 'wins': wins, 'losses': n - wins, 'be_count': 0,
                'blown': blown, 'min_equity_usd': round(min_eq, 2),
                'max_consec_wins': _mc(outcomes_seq, 1),
                'max_consec_losses': _mc(outcomes_seq, 0),
                'sl_pct': None, 'tp_pct': None,
                'max_dd_pct': max_dd_pct, 'total_pnl_usd': total_pnl,
                'sharpe': sharpe, 'ce': ce,
            },
            'by_hour': bh, 'by_session': bs, 'by_dow': bd,
            'dir_summary': ds, 'by_year': by_yr,
            'by_classification': _compute_by_classification(ws),
            'mae_heatmap': _build_excursion_heatmap(sub, 'mae_pct'),
            'mfe_heatmap': _build_excursion_heatmap(sub, 'mfe_pct'),
            'recent_trades': recent_trades,
        }

    return result


# ── BUILD MODEL STATS ─────────────────────────────────────────────────────────
def build_model_stats(df_raw, trading_days):
    """Build the complete stats dict for the single 555 model."""
    df  = df_raw[df_raw['rejected_by'] == ''].copy()
    wl  = df[df['outcome'].isin(['WIN','LOSS'])].copy()
    wl['win'] = (wl['outcome'] == 'WIN').astype(int)

    # Breakdowns
    by_hour = []
    for (hr, direction), g in wl.groupby(['hr', 'direction']):
        s = agg(g)
        if s['n'] >= 5:
            s.update(hr=int(hr), direction=direction,
                     hr_label=HR_LABELS.get(int(hr), f"{int(hr):02d}:00"))
            by_hour.append(s)

    by_session = []
    for (sess, direction), g in wl.groupby(['session', 'direction']):
        s = agg(g); s.update(session=sess, direction=direction)
        by_session.append(s)

    by_dow = []
    for (dow, direction), g in wl.groupby(['dow', 'direction']):
        s = agg(g)
        if s['n'] >= 5:
            s.update(dow=int(dow), dow_name=DOW_NAMES.get(int(dow), '?'),
                     direction=direction)
            by_dow.append(s)

    heatmap = []
    for (hr, dow), g in wl.groupby(['hr', 'dow']):
        s = agg(g)
        if s['n'] >= 3:
            s.update(hr=int(hr), dow=int(dow), dow_name=DOW_NAMES.get(int(dow), '?'))
            heatmap.append(s)

    combos = []
    for (hr, dow, direction), g in wl.groupby(['hr', 'dow', 'direction']):
        s = agg(g)
        if s['n'] >= 6:
            dn = DOW_NAMES.get(int(dow), '?')
            s.update(hr=int(hr), dow=int(dow), dow_name=dn, direction=direction,
                     label=f"{dn} {int(hr):02d}:00 {direction}")
            combos.append(s)
    combos.sort(key=lambda x: x['ev'], reverse=True)

    by_year = []
    for yr, g in wl.groupby('yr'):
        s = agg(g); s.update(yr=int(yr))
        by_year.append(s)

    # R histogram
    r_buckets = [
        ('Loss',    lambda r: r <  0),
        ('0-0.5R',  lambda r: (0 <= r) & (r <  0.5)),
        ('0.5-1R',  lambda r: (0.5 <= r) & (r <  1.05)),
        ('1R',      lambda r: (0.95 <= r) & (r <= 1.15)),
        ('>1R',     lambda r: r > 1.05),
    ]
    df_r   = df[df['outcome'] != 'INVALID'].copy()
    r_hist = [{'bucket': lbl, 'n': int(fn(df_r['r']).sum())} for lbl, fn in r_buckets]

    dir_summary = []
    for direction, g in wl.groupby('direction'):
        s = agg(g); s.update(direction=direction)
        dir_summary.append(s)

    # SMT summary (all are SMT in this model, but provide the structure)
    smt_summary = []
    if 'smt' in wl.columns:
        for smt_val, g in wl.groupby('smt'):
            s = agg(g); s.update(smt=bool(smt_val))
            smt_summary.append(s)

    # Distribution stats
    mae = wl['mae_pct'].dropna()
    mfe = wl['mfe_pct'].dropna()
    wins_wl2 = wl[wl['win'] == 1]
    loss_wl2 = wl[wl['win'] == 0]

    mae_dist_legacy = dict(
        median = round(float(mae.median()), 4) if len(mae) else 0,
        p80    = round(float(mae.quantile(.80)), 4) if len(mae) else 0,
        p90    = round(float(mae.quantile(.90)), 4) if len(mae) else 0,
        mean   = round(float(mae.mean()), 4) if len(mae) else 0,
        **{f'ext_{k}': v for k, v in _dist_stats(mae).items()},
    )
    mfe_dist_legacy = dict(
        median = round(float(mfe.median()), 4) if len(mfe) else 0,
        p80    = round(float(mfe.quantile(.80)), 4) if len(mfe) else 0,
        p90    = round(float(mfe.quantile(.90)), 4) if len(mfe) else 0,
        mean   = round(float(mfe.mean()), 4) if len(mfe) else 0,
        **{f'ext_{k}': v for k, v in _dist_stats(mfe).items()},
    )

    mae_wins_dist = _dist_stats(wins_wl2['mae_pct'].dropna())
    mfe_wins_dist = _dist_stats(wins_wl2['mfe_pct'].dropna())
    mae_loss_dist = _dist_stats(loss_wl2['mae_pct'].dropna())
    mfe_loss_dist = _dist_stats(loss_wl2['mfe_pct'].dropna())

    rp = wl['risk_pts'].dropna()
    risk_dist = dict(
        mean   = round(float(rp.mean()), 1)          if len(rp) else 0,
        median = round(float(rp.median()), 1)        if len(rp) else 0,
        p25    = round(float(rp.quantile(.25)), 1)   if len(rp) else 0,
        p75    = round(float(rp.quantile(.75)), 1)   if len(rp) else 0,
        p90    = round(float(rp.quantile(.90)), 1)   if len(rp) else 0,
        max    = round(float(rp.max()), 1)            if len(rp) else 0,
    )

    overall  = agg(wl)
    ny_setup = df[df['session'].isin(['NY1', 'NY2'])]
    spd      = round(len(ny_setup) / max(trading_days, 1), 2)

    filter_impact   = compute_filter_impact(df_raw)
    filter_variants = compute_filter_variants(df_raw)

    # Recent trades
    recent_cols = ['date','direction','hr','mn','session','dow','entry_price',
                   'stop_price','target_price','risk_pts','r','outcome',
                   'mae_pct','mfe_pct','ifvg_high','ifvg_low','cisd_level']
    available = [c for c in recent_cols if c in wl.columns]
    recent_rows = wl[available].sort_values('date', ascending=False).copy()
    recent_rows['dow_name'] = recent_rows['dow'].map(lambda d: DOW_NAMES.get(int(d), '?'))
    recent_rows['classification'] = recent_rows['date'].astype(str).str[:10].map(
        lambda d: DATE_CLASSIFICATION.get(d, 'Unclassified'))
    recent_trades = recent_rows.to_dict('records')
    for t in recent_trades:
        t['date'] = str(t['date'])[:10]
        t['dow']  = int(t['dow'])
        t['hr']   = int(t['hr'])
        t['mn']   = int(t['mn'])
        for k in ('entry_price','stop_price','target_price','risk_pts','r'):
            if t[k] is not None:
                t[k] = round(float(t[k]), 2)
        for k in ('mae_pct','mfe_pct'):
            if k in t and t[k] is not None:
                t[k] = round(float(t[k]), 4)

    # ── Risk stats ────────────────────────────────────────────────────────────
    wl_sorted   = wl.sort_values('date')
    outcomes_seq = wl_sorted['win'].tolist()
    def _max_consec(seq, val):
        mx = cur = 0
        for v in seq:
            cur = cur + 1 if v == val else 0
            mx  = max(mx, cur)
        return mx
    rs_max_cw = _max_consec(outcomes_seq, 1)
    rs_max_cl = _max_consec(outcomes_seq, 0)

    wins_df     = wl_sorted[wl_sorted['win'] == 1]
    losses_df   = wl_sorted[wl_sorted['win'] == 0]
    avg_win_r   = round(float(wins_df['r'].mean()), 4)   if len(wins_df)   else 1.0
    avg_loss_r  = round(float(losses_df['r'].mean()), 4) if len(losses_df) else -1.0
    avg_win_usd  = round(avg_win_r  * RISK_PER_TRADE, 2)
    avg_loss_usd = round(avg_loss_r * RISK_PER_TRADE, 2)

    eq = float(ACCOUNT_SIZE)
    peak_eq = eq
    min_eq = eq
    max_dd_abs = 0.0
    daily_pnl = {}
    for _, row in wl_sorted.iterrows():
        r_val = row['r']
        if r_val is not None and not np.isnan(r_val):
            trade_pnl = float(r_val) * RISK_PER_TRADE
            eq += trade_pnl
            if eq < min_eq:
                min_eq = eq
            if eq > peak_eq:
                peak_eq = eq
            dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0.0
            if dd > max_dd_abs:
                max_dd_abs = dd
            try:
                trade_date = str(row['date'])[:10]
                if trade_date:
                    daily_pnl[trade_date] = daily_pnl.get(trade_date, 0.0) + trade_pnl
            except Exception:
                pass
    min_eq = round(min_eq, 2)
    max_dd_pct = round(max_dd_abs * 100, 2)
    total_pnl_usd = round(eq - ACCOUNT_SIZE, 2)
    blown  = min_eq <= 0.0

    if len(daily_pnl) > 1:
        dpnl_arr = np.array(list(daily_pnl.values()))
        sharpe_val = round(float(dpnl_arr.mean() / dpnl_arr.std(ddof=1) * np.sqrt(252)), 2) \
                     if dpnl_arr.std(ddof=1) > 0 else None
    else:
        sharpe_val = None

    _pf = overall['pf']
    _n_wins = len(wins_df)
    _n_losses = len(losses_df)
    _n_total = _n_wins + _n_losses
    _ev_dollar = (avg_win_usd * _n_wins + avg_loss_usd * _n_losses) / _n_total if _n_total > 0 else 0
    _ev_r = _ev_dollar / RISK_PER_TRADE if RISK_PER_TRADE > 0 else 0
    ce = round(_ev_r * _pf, 6) if _pf and _n_total > 0 else None

    risk_stats = {
        'account_size':     ACCOUNT_SIZE,
        'risk_per_trade':   RISK_PER_TRADE,
        'trades':           overall['n'],
        'wins':             overall['wins'],
        'losses':           overall['n'] - overall['wins'],
        'be_count':         0,
        'avg_win_usd':      avg_win_usd,
        'avg_loss_usd':     avg_loss_usd,
        'blown':            blown,
        'min_equity_usd':   min_eq,
        'max_consec_wins':  rs_max_cw,
        'max_consec_losses': rs_max_cl,
        'sl_pct':           None,
        'tp_pct':           None,
        'max_dd_pct':       max_dd_pct,
        'total_pnl_usd':    total_pnl_usd,
        'sharpe':           sharpe_val,
        'ce':               ce,
    }

    # MAE bell curve
    mae_vals = wl_sorted['mae_pct'].dropna()
    mae_vals = mae_vals[mae_vals > 0]
    if len(mae_vals) > 1 and mae_vals.std() > 0:
        mae_mu = float(mae_vals.mean())
        mae_sd = float(mae_vals.std(ddof=1))
        mae_np = mae_vals.values
        risk_stats['mae_bell'] = {
            'mean':      round(mae_mu, 4),
            'std':       round(mae_sd, 4),
            'plus_0_5s': round(mae_mu + 0.5 * mae_sd, 4),
            'plus_1s':   round(mae_mu + mae_sd, 4),
            'plus_1_5s': round(mae_mu + 1.5 * mae_sd, 4),
            'plus_2s':   round(mae_mu + 2.0 * mae_sd, 4),
            'cov_mean':  round(float(np.mean(mae_np <= mae_mu)) * 100, 1),
            'cov_0_5s':  round(float(np.mean(mae_np <= mae_mu + 0.5*mae_sd)) * 100, 1),
            'cov_1s':    round(float(np.mean(mae_np <= mae_mu + mae_sd)) * 100, 1),
            'cov_1_5s':  round(float(np.mean(mae_np <= mae_mu + 1.5*mae_sd)) * 100, 1),
            'cov_2s':    round(float(np.mean(mae_np <= mae_mu + 2.0*mae_sd)) * 100, 1),
        }
    else:
        risk_stats['mae_bell'] = None

    # Rich MAE/MFE studies
    rich_mae = _full_mae_stats(wl, ce=ce)
    rich_mfe = _full_mfe_stats(wl)
    wins_only = wl[wl['win'] == 1].copy()
    rich_mae_wins = _full_mae_stats(wins_only, ce=ce) if len(wins_only) > 10 else None
    rich_mfe_wins = _full_mfe_stats(wins_only) if len(wins_only) > 10 else None
    losses_only = wl[wl['win'] == 0].copy()
    rich_mae_losses = _full_mae_stats(losses_only, ce=ce) if len(losses_only) > 10 else None
    rich_mfe_losses = _full_mfe_stats(losses_only) if len(losses_only) > 10 else None

    # Structural stats
    structural_stats = None
    if 'tp1_hit' in wl.columns:
        tp1_rate   = round(float(wl['tp1_hit'].mean()), 4)
        tp1_trades = wl[wl['tp1_hit'] == True]
        runner_col = tp1_trades['runner_exit_r'].dropna()
        runner_ran = runner_col[runner_col > 0]
        structural_stats = {
            'tp1_hit_rate':            tp1_rate,
            'runner_ran_further_rate': round(float(len(runner_ran) / max(len(runner_col), 1)), 4),
            'runner_stats':            _dist_stats(runner_col),
            'runner_ran_stats':        _dist_stats(runner_ran),
        }

    # Breakeven WR for structural: wr * 0.9R = (1-wr) * 1R -> wr = 1/1.9
    be_wr = round(1.0 / 1.9, 4)

    # by_tf
    print("   Computing by_tf slices ...")
    by_tf = _compute_by_tf(wl, wl_sorted)

    full_key = '5M_SMT_IFVG_CISD'
    date_range_str = (f"{str(df['date'].min())[:10]} - {str(df['date'].max())[:10]}"
                      if len(df) else '-')

    return {
        'meta': {
            'model_key':          full_key,
            'full_key':           full_key,
            'profile_key':        'structural_dynamic',
            'profile_type':       'structural',
            'model_label':        '5M SMT + Inverse FVG + CISD',
            'instrument':         'NQ',
            'date_range':         date_range_str,
            'trading_days':       trading_days,
            'total_raw':          len(df_raw),
            'total_wl':           overall['n'],
            'total_expired':      int((df['outcome'] == 'EXPIRED').sum()),
            'win_rate':           overall['wr'],
            'ev_per_trade':       overall['ev'],
            'profit_factor':      overall['pf'],
            'avg_risk_pts':       overall['avg_risk_pts'],
            'setups_per_day_ny':  spd,
            'risk_breakeven_wr':  be_wr,
            'rr_target':          1.0,
            'stop_mult':          1.0,
            'target_mult':        1.0,
            **{f'risk_{k}': v for k, v in risk_dist.items()},
            **{f'mae_{k}':  v for k, v in mae_dist_legacy.items()},
            **{f'mfe_{k}':  v for k, v in mfe_dist_legacy.items()},
        },
        'by_hour':         by_hour,
        'by_session':      by_session,
        'by_dow':          by_dow,
        'heatmap':         heatmap,
        'top_combos':      combos[:15],
        'worst_combos':    sorted(combos, key=lambda x: x['ev'])[:5],
        'by_year':         by_year,
        'r_hist':          r_hist,
        'dir_summary':     dir_summary,
        'smt_summary':     smt_summary,
        'risk_dist':       risk_dist,
        'filter_impact':   filter_impact,
        'filter_variants': filter_variants,
        'mae_dist':        mae_dist_legacy,
        'mfe_dist':        mfe_dist_legacy,
        'rich_mae':        rich_mae,
        'rich_mfe':        rich_mfe,
        'rich_mae_wins':   rich_mae_wins,
        'rich_mfe_wins':   rich_mfe_wins,
        'rich_mae_losses': rich_mae_losses,
        'rich_mfe_losses': rich_mfe_losses,
        'mae_wins_dist':   mae_wins_dist,
        'mfe_wins_dist':   mfe_wins_dist,
        'mae_loss_dist':   mae_loss_dist,
        'mfe_loss_dist':   mfe_loss_dist,
        'mae_heatmap':     _build_excursion_heatmap(wl, 'mae_pct'),
        'mfe_heatmap':     _build_excursion_heatmap(wl, 'mfe_pct'),
        'recent_trades':   recent_trades,
        'risk_stats':      risk_stats,
        'structural_stats': structural_stats,
        'by_classification': _compute_by_classification(wl_sorted),
        'by_tf':           by_tf,
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='555 Model Backtesting Engine')
    parser.add_argument('--db',     default=str(DB_PATH))
    parser.add_argument('--output', default=str(OUT_PATH))
    args = parser.parse_args()

    print("\n" + "=" * 65)
    print("  555 MODEL ENGINE v1.0  -  5M SMT + Inverse FVG + CISD")
    print("  Instrument: NQ (Micro Nasdaq)")
    print("  Stop: IFVG invalidation (close-based)")
    print("  Target: 1R (90% exit), runner (10%) with BE stop")
    print("=" * 65)

    # Load data
    print("\n[1] Loading NQ 1m data ...")
    con = connect(args.db)
    nq_1m_full, nq_1m_rth = load_1m(con, 'nq_1m')
    trading_days = nq_1m_rth['trade_date'].nunique()
    print(f"   {trading_days:,} trading days")

    print("\n[1b] Loading ES 1m data for SMT divergence ...")
    try:
        es_1m_full, es_1m_rth = load_1m(con, 'es_1m')
        has_es = True
    except Exception as e:
        print(f"   [warn] ES data not available ({e}), cannot proceed")
        has_es = False
    con.close()

    if not has_es:
        print("BLOCKED: ES data required for SMT divergence detection")
        return

    # Build timeframes
    print("\n[2] Building timeframes ...")
    nq_5m_df = resample(nq_1m_rth, 5, '5min')
    nq_3m_df = resample(nq_1m_rth, 3, '3min')
    es_5m_df = resample(es_1m_rth, 5, 'ES_5min')

    # Convert to numpy arrays
    print("\n   Converting to numpy arrays ...")
    nq_5m_arrs = df_to_arrays(nq_5m_df)
    nq_3m_arrs = df_to_arrays(nq_3m_df)
    es_5m_arrs = df_to_arrays(es_5m_df)
    m1_arrs    = df_1m_to_arrays(nq_1m_rth)
    print("   Done.")

    # Detect setups
    rows, pending = detect_setups(nq_5m_arrs, nq_3m_arrs, es_5m_arrs, m1_arrs)

    if not pending:
        print("\n   No valid setups found. Exiting.")
        return

    # Resolve outcomes on 5m bars (close-based stop)
    print(f"\n[6] Resolving outcomes ({len(pending):,} trades) on 5m bars ...")
    outcomes = resolve_outcomes_structural(nq_5m_arrs, pending)

    for po, (outcome, r_val, mae_pct, mfe_pct, tp1_hit, runner_exit_r) in zip(pending, outcomes):
        idx = po['idx']
        rows[idx]['outcome']       = outcome
        rows[idx]['r']             = r_val
        rows[idx]['mae_pct']       = mae_pct
        rows[idx]['mfe_pct']       = mfe_pct
        rows[idx]['tp1_hit']       = tp1_hit
        rows[idx]['runner_exit_r'] = runner_exit_r
        hr_rng = po.get('hour_range_pts', 0.0)
        if hr_rng > 0 and mae_pct is not None:
            mae_pts = mae_pct / 100.0 * po['entry_price']
            mfe_pts = mfe_pct / 100.0 * po['entry_price']
            rows[idx]['mae_pct_hr'] = round(mae_pts / hr_rng * 100, 4)
            rows[idx]['mfe_pct_hr'] = round(mfe_pts / hr_rng * 100, 4)
        if outcome == 'INVALID':
            rows[idx]['rejected_by'] = rows[idx]['rejected_by'] or 'INVALID_RISK'

    df_all = pd.DataFrame(rows)
    wl_count = int(df_all['outcome'].isin(['WIN','LOSS']).sum())
    exp_count = int((df_all['outcome'] == 'EXPIRED').sum())
    print(f"   {wl_count:,} WIN/LOSS  |  {exp_count:,} EXPIRED")

    # Build stats
    print("\n[7] Building model statistics ...")
    stats = build_model_stats(df_all, trading_days)

    # Wrap in expected JSON structure
    output = {
        '5M_SMT_IFVG_CISD': {
            'profiles': {
                'structural_dynamic': stats
            }
        }
    }

    # Write JSON
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w') as f:
        json.dump(output, f, indent=2, default=str)

    # Summary
    m = stats['meta']
    rs = stats['risk_stats']
    print(f"\n{'=' * 65}")
    print(f"  SUMMARY")
    print(f"{'=' * 65}")
    print(f"  Model:        {m['model_label']}")
    print(f"  Date Range:   {m['date_range']}")
    print(f"  Trading Days: {m['trading_days']:,}")
    print(f"  Total Setups: {m['total_raw']:,} raw  |  {m['total_wl']:,} resolved  |  {m['total_expired']:,} expired")
    print(f"  Win Rate:     {m['win_rate']:.1%}")
    print(f"  EV/Trade:     {m['ev_per_trade']:+.3f}R")
    print(f"  Profit Factor:{m['profit_factor']:.3f}")
    print(f"  Setups/Day NY:{m['setups_per_day_ny']:.2f}")
    print(f"  Avg Risk:     {m['avg_risk_pts']:.1f} pts")
    print(f"  Total P&L:    ${rs['total_pnl_usd']:,.2f}")
    print(f"  Max DD:       {rs['max_dd_pct']:.2f}%")
    if rs['sharpe'] is not None:
        print(f"  Sharpe:       {rs['sharpe']:.2f}")
    print(f"{'=' * 65}")
    print(f"\n  Written -> {out}")
    print(f"  Open http://localhost:8002/model_dashboard.html\n")


if __name__ == '__main__':
    main()
