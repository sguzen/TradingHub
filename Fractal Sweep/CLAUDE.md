# Fractal Sweep

Statistical backtesting engine for NQ and ES futures. Detects sweep + CISD setups across 15 years of 1-minute data and outputs probability dashboards.

## Stack
- Python 3.14 + DuckDB 1.4.4 + pandas
- No web framework — dashboards are standalone HTML files

## Database
- `candle_science.duckdb` — primary DB, tables: `nq_1m`, `es_1m`
- Schema: `timestamp TIMESTAMPTZ, open, high, low, close DOUBLE, volume BIGINT`
- Timestamps stored as `TIMESTAMP WITH TIME ZONE` (America/Toronto)
- Always convert to ET for analysis: `timezone('America/New_York', timestamp)`
- Large data files (`.duckdb`, `.dbn`, `.parquet`, `.csv`) are gitignored

## Key Files
- `model_stats.py` — sweep+CISD detection engine → `model_stats.json`
- `daily_update.py` — cron entry point (weekdays 7am); fetches missing bars from Databento
- `model_dashboard.html` — sweep model dashboard (loads `model_stats.json`)
- `fractal_sweep_cisd.pine` — TradingView Pine v5 indicator (live alert equivalent of the backtest)

## Running
```bash
python3 model_stats.py              # run all 4 sweep models
python3 daily_update.py             # fetch new bars from Databento
python3 -m http.server 8000         # serve dashboard at localhost:8000
```

## Trading Model
- 4 timeframe pairs: `4H_15M`, `1H_5M`, `1H_3M`, `30M_3M`
- Setup: prior candle swept in Q1 → price returns inside range → CISD confirms
- Entry: next candle open | Stop: sweep extreme | Target: 2R (1:2)
- RTH window: 07:00–16:00 ET (all models)

## Risk Profiles (RR_PROFILES in model_stats.py)

12 profiles total — 10 fixed-% DDLI-ranked + 2 structural/split-exit:

| profile_type | Key | Description |
|---|---|---|
| `pct` | `sl_026_tp_018` … `sl_019_tp_019` | Fixed % SL/TP of entry price (DDLI top-10) |
| `structural` | `structural_dynamic` | SL = sweep extreme (1×base_risk); TP1 @ 1R, 90% exit; runner (10%) free with BE stop |
| `split_tp` | `split_80_20` | SL = sweep extreme; TP1 @ PTQ level, 90% exit; 10% runner targets p50 MFE; BE stop on runner |

### split_tp profile mechanics
- `stop_dist = min(1 × base_risk, entry × mae_p90 / 100)` — tighter of structural or MAE p90 of winners
- `TP1 = entry ± entry × ptq_level / 100` (PTQ level from structural profile's winners-only MFE)
- Runner TP2 = `entry ± entry × p50_mfe / 100` (p50 MFE from winners)
- After TP1: 10% runner holds with BE stop toward TP2
- `net_r = 0.90 × tp1_r + 0.10 × runner_exit_r`
- All targets (PTQ, p50, MAE p90) are computed per TF period from the structural profile's winners

### MAE/MFE Recommendation Logic
- **PTQ**: highest reach_rate where P(positive exit | MFE ≥ X) ≥ 0.70, fallback 0.50
- **opt_sl**: tightest MAE where P(genuine loss | MAE ≥ X) ≥ 0.70, fallback 0.50
- Computed in both `model_stats.py` (backtest) and `model_dashboard.html` (client-side recent trades)

## Hourly Normalization

MAE/MFE are normalized by the entry hour's range to be comparable across volatility regimes:
- `hour_range_pts` — high minus low of all 1m bars sharing the trade's (date, hour)
- `mae_pct_hr = mae_pts / hour_range_pts × 100` — MAE as % of hourly range
- `mfe_pct_hr = mfe_pts / hour_range_pts × 100` — MFE as % of hourly range
- `agg()` emits `avg_mae`, `avg_mfe`, `avg_mae_hr`, `avg_mfe_hr` in all breakdowns (by_hour, by_session, by_dow, dir_summary, by_year)
- Dashboard tooltips show hour-normalized values

## Equity Tracking

- `min_equity_usd` — actual running minimum equity (not final equity)
- `max_dd_usd` — dollar amount of the worst peak-to-trough drawdown
- `max_dd_pct` — percentage drawdown from running peak

### Walk-Forward Regime Analysis
Custom date ranges view pairs consecutive ranges into train→test walk-forward pairs.
Train period derives MAE stop variants (max, p90, p85, p50) and MFE targets (PTQ, p50) from winners.
Test period resolves trades with each variant. Overfitting score = Test EV / Train EV × 100.
All computation is client-side in `model_dashboard.html` — no Python changes needed.

## Analysis Scripts Convention
- Reference point for candle analysis: use `close` of the anchor candle
- Scan window: anchor+1 bar through 16:00 ET same day
- Group results by day of week (0=Mon … 4=Fri)
