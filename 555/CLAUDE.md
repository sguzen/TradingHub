# 555 Model

5M SMT + Inverse FVG + CISD backtesting engine for NQ futures.

## Stack
- Python 3.14 + DuckDB + pandas + numpy + scipy
- No web framework — dashboard is standalone HTML

## Database
- `../Fractal Sweep/candle_science.duckdb` — shared DB (read-only)
- Tables: `nq_1m`, `es_1m`
- Schema: `timestamp TIMESTAMPTZ, open, high, low, close DOUBLE, volume BIGINT`
- Always convert to ET: `timezone('America/New_York', timestamp)`

## Key Files
- `model_stats.py` — detection engine → `model_stats.json`
- `model_dashboard.html` — probability dashboard (loads model_stats.json)

## Running
```bash
python3 model_stats.py              # run backtest → model_stats.json
python3 -m http.server 8002         # serve dashboard at localhost:8002
```

## Trading Model
- **Conditions (sequential):** SMT divergence (5m) → Inverse FVG (3m or 5m) + CISD (5m) in any order
- **SMT:** NQ sweeps a 5m swing fractal high/low, ES does NOT sweep its corresponding swing
- **IFVG:** 3-candle gap that gets inverted. Min size: 2 points. Checked on both 3m and 5m.
- **CISD:** Body-only change in state of delivery (same logic as Fractal Sweep)
- **Entry:** Next 5m candle open after both IFVG and CISD have occurred
- **Stop:** IFVG invalidation — checked on 5m candle CLOSES only (not wicks)
- **Target:** TP1 at 1R (90% exit), runner (10%) with BE stop, marks to market at 16:00 ET

## Constants
- `MIN_IFVG_PTS = 2.0` — minimum IFVG gap size in points
- `MAX_RISK_PTS = 112.5` — MNQ $225 ÷ $2.00/pt
- `SESSION_HRS = (7.0, 16.0)` — RTH window in ET
- `ACCOUNT_SIZE = 4500`, `RISK_PER_TRADE = 225`, `POINT_VALUE = 2.0`
