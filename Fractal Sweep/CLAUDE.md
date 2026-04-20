# Fractal Sweep

Statistical backtesting engine for NQ and ES futures. Detects sweep + CISD setups across 15 years of 1-minute data and drives an interactive probability dashboard.

> Historical note: this folder previously coexisted with `Fractal Sweep Legacy/`. As of 2026-04-19 the Legacy engine is the canonical one; it was merged here and the Legacy folder was deleted. See `LEGACY_NOTE.md` for the earlier history.

## Stack
- Python 3.14 · DuckDB 1.4.4 · pandas
- Dashboards are standalone HTML (zero CDN deps)

## Folder Layout
```
Fractal Sweep/
├── model_dashboard.html        dashboard (served from repo root)
├── model_stats.json            engine output (gitignored, ~140 MB)
├── candle_science.duckdb       shared DB (gitignored, ~550 MB)
├── engine/                     Python backtest code
│   ├── model_stats.py          sweep+CISD detection engine
│   ├── daily_update.py         cron entry point — Databento fetch + rerun backtests
│   ├── master_backtester.py    supporting tool
│   ├── sltp_analyzer.py        supporting tool
│   ├── recalc.py               supporting tool
│   └── install_cron.sh         one-time cron setup helper
├── pine/                       TradingView scripts
│   ├── fractal_sweep.pine      indicator
│   ├── fractal_sweep_strategy.pine
│   ├── ttfm+fadi.pine          separate experiment
│   └── snapshots/              dated backups of the indicator
├── data/                       raw Databento dumps (gitignored)
├── docs/                       standalone notes (indicator description, analysis write-ups)
├── assets/                     images used by the dashboard/hub
└── tests/                      pytest suite — 195 pass · 20 skip as of 2026-04-19
```

## Running
All engine scripts self-locate — run them from the `Fractal Sweep/` folder (they resolve `candle_science.duckdb` and `model_stats.json` via `Path(__file__).parent.parent`).

```bash
python3 engine/model_stats.py                         # all 4 sweep models
python3 engine/model_stats.py --models 1H_5M 1H_3M   # subset
python3 engine/model_stats.py --table es_1m           # ES instead of NQ
python3 -m pytest tests/ -q                           # test suite
python3 engine/daily_update.py                        # fetch new bars from Databento
```

Dashboard served from the repo root: `python3 -m http.server 8001`, then open `http://localhost:8001/Fractal Sweep/model_dashboard.html`.

## Trading Model
- 4 timeframe pairs: `4H_15M`, `1H_5M`, `1H_3M`, `30M_3M`
- Setup: prior candle swept → price returns inside range → CISD confirms
- Entry: next candle open · Stop: sweep extreme · Target: 1R (`simple_1r`)
- Sweep detection runs across the **full HTF period** — no Q1 window gate
- CISD has no bar limit — can form anytime after sweep returns
- Baseline filters (always on): `SWEEP_MAX_PCT = 0.50` (reference only — now a runtime toggle), `MIN_RISK_PTS = 3.0`, `MAX_RISK_PTS = 112.5`
- `long_base`/`short_base` separated from `max_risk` check — enables over-risk detection
- **F1 (min prior range) is removed.** Data showed it rejected above-average trades on all 4 TFs (2026-04-15).

## Runtime Filters (6, dashboard-toggleable)

Two groups below the Period/TF/Profile dropdowns. Each chip shows a live `±N` badge before you click.

**Setup Quality** (default ON, uncheck to relax)
- **Shallow Sweep** (`F3_SWEEP_TOO_LARGE`) — sweep pierced ≤ 50% of prior range
- **Closed Back Inside** (`F4_NO_CLOSE_BACK`) — price returned inside prior range

**Add Confirmation** (default OFF, check to narrow)
- **NQ-ES Divergence** (`SMT`) — NQ swept but ES did not
- **Hour Open Aligned** (`HOUR_ALIGNED`) — CISD close on correct side of current hour open
- **Prior Bar Counters** (`PRIOR_COUNTER_CLOSE`) — prior sweep-TF candle closed against trade direction
- **Prior Bar Engulfs** (`PRIOR_ENGULFING`) — prior sweep-TF candle engulfs its predecessor wick-inclusive

`2⁶ = 64` combinations are precomputed in `filter_variants.all_combinations` for the Filters tab.

Filters work on **every Period** (All Time, 2y, 1y, 6m, 3m, 1m). `_compute_by_tf` builds `recent_trades` per sub-slice from `wl_full` (which includes F3/F4-rejected trades), so runtime toggles can bring rejected trades back in.

## SMT Divergence

SMT = NQ sweeps its HTF level but ES does **not** sweep its corresponding level. Exposed as "NQ-ES Divergence" in the dashboard filter bar.

- `engine/model_stats.py` loads `es_1m` alongside `nq_1m`, builds ES sweep-TF candles, checks the ES window at NQ sweep detection time
- Each trade row carries `smt: bool`
- `smt_summary` in JSON output: WR/EV/PF split for SMT vs non-SMT

## Risk Profiles (`RR_PROFILES` in `engine/model_stats.py`)

| profile_type | Key | Description |
|---|---|---|
| `mult` | `simple_1r` | SL = sweep extreme (1× base_risk); TP = 1R (100% exit) |
| `raw` | `raw_measure` | No SL/TP — records full-session MAE/MFE only, `outcome='MEASURED'` |

`simple_1r` is the default and drives all win/loss stats. `raw_measure` is the measurement-only profile used for MAE/MFE distribution studies.

### MAE/MFE Recommendation Logic
- **PTQ**: highest reach_rate where P(positive exit | MFE ≥ X) ≥ 0.70, fallback 0.50
- **opt_sl**: tightest MAE where P(genuine loss | MAE ≥ X) ≥ 0.70, fallback 0.50
- Computed both in `engine/model_stats.py` and client-side in `model_dashboard.html` for recent trades

## Hourly Normalization
- `hour_range_pts` — high minus low of all 1m bars sharing the trade's (date, hour)
- `mae_pct_hr = mae_pts / hour_range_pts × 100`
- `mfe_pct_hr = mfe_pts / hour_range_pts × 100`
- `agg()` emits `avg_mae`, `avg_mfe`, `avg_mae_hr`, `avg_mfe_hr` in all breakdowns

## Equity Tracking
- `min_equity_usd` — actual running minimum equity (not final equity)
- `max_dd_usd` — dollar value of worst peak-to-trough drawdown
- `max_dd_pct` — percentage drawdown from running peak

### Walk-Forward Regime Analysis
Custom date ranges view pairs consecutive ranges into train→test walk-forward pairs. Train period derives MAE stop variants (max, p90, p85, p50) and MFE targets (PTQ, p50) from winners. Test period resolves trades with each variant. Overfitting score = Test EV / Train EV × 100. Fully client-side in `model_dashboard.html`.

## Analysis Scripts Convention
- Reference point for candle analysis: `close` of the anchor candle
- Scan window: anchor+1 bar through 16:00 ET same day
- Group results by day of week (0=Mon … 4=Fri)

## Date Classification
- `DATE_CLASSIFICATION` is an empty dict — the classifier source (`daily_classifier.py`) was in the deleted NY1 FPFVG folder
- Downstream aggregations read it defensively; absence is fine
