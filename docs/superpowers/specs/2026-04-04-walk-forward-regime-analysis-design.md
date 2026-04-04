# Walk-Forward Regime Analysis — Design Spec

**Date:** 2026-04-04
**Scope:** Replace current custom date ranges view with walk-forward regime analysis
**Location:** `Fractal Sweep/model_dashboard.html` — client-side only, no Python changes

---

## Goal

Answer two questions at a glance:
1. **Am I overfitting?** — do train-derived MAE/MFE parameters degrade when applied out-of-sample?
2. **Is the edge regime-stable?** — does the model maintain positive EV across different market conditions?

---

## Data Flow

### Pairing
User defines N date ranges manually (existing UI). Consecutive ranges are paired:
- R1→R2 (train→test), R3→R4, R5→R6, etc.
- Odd last range = unpaired standalone comparison (no walk-forward)
- Single range = current range result card format (no pairing)

### Train Period Computation
From the train period's winners, compute:
- **MAE percentiles:** max, p90, p85, p50 → become stop variants
- **MFE targets:** PTQ (highest reach with p_pos ≥ 0.70, fallback 0.50), p50 → become TP1/TP2

### Test Period Resolution
Resolve test trades **4 times**, once per stop variant:
| Variant | SL Cap | TP1 | TP2 |
|---------|--------|-----|-----|
| Max MAE Stop | train winners' max MAE | train PTQ | train p50 MFE |
| P90 MAE Stop | train winners' p90 MAE | train PTQ | train p50 MFE |
| P85 MAE Stop | train winners' p85 MAE | train PTQ | train p50 MFE |
| P50 MAE Stop | train winners' p50 MAE | train PTQ | train p50 MFE |

All variants apply `min(structural_stop, MAE_cap)` — same logic as current split profile.

### Train Period Self-Resolution
Train period also resolves its own trades with the same 4 variants, for side-by-side overfitting comparison.

### Trade Resolution Logic (Client-Side)
For each variant, given a set of trades and a MAE stop cap:
- If `trade.mae_pct > sl_cap_pct`: the trade would have been stopped out → outcome = LOSS, r = -1.0
- Otherwise: keep original outcome and r value
- Recompute all stats from the adjusted trade set via `computeRangeStats`

This is an approximation — it doesn't re-resolve bar-by-bar with new stop/target prices. It uses the existing MAE/MFE data to simulate what would have happened with a tighter stop. This is accurate for stops (if MAE exceeded the cap, you were stopped out) but approximate for targets (PTQ/p50 targets aren't re-resolved). Acceptable for a diagnostic view.

---

## Layout (Top to Bottom)

### A. Combined Summary Bar
Hero tiles computed across **all test periods combined** (not train). Quick health check for overall out-of-sample profitability. Same 12-tile layout as current (WR, EV, PF, CE, P&L, Min Equity, Max DD, Sharpe, Avg Win R, Max W Run, Max L Run, Account SAFE/BLOWN).

### B. Walk-Forward Pairs (repeated per pair)
Each pair renders as a full section:

**Header:**
- Pair label: "Pair 1: R1 → R2"
- Train date range, test date range, trade counts
- Overfitting score badge (see Diagnostics)

**Train Parameters Block:**
- MAE thresholds: Max, P90, P85, P50 (with winner count)
- MFE targets: PTQ level + reach rate, P50

**Regime Fingerprint:**
Compact row comparing train vs test regime characteristics:
- Avg risk pts (volatility proxy)
- Long/Short split %
- MFE/MAE ratio (efficiency)
- Trade density (trades/day)

**Stop Variant Cards (4 cards in a row):**
Each card shows train and test results side-by-side in row format:

```
┌─────────────────────┐
│  Max MAE Stop       │
│  SL cap: 0.1716%    │
├──────────┬──────────┤
│  TRAIN   │  TEST    │
├──────────┼──────────┤
│ WR  46.3 │ WR  42.1 │
│ EV  248  │ EV  220  │
│ Sharpe 2.3│Sharpe 1.9│
│ PF  4.09 │ PF  3.50 │
│ MaxDD-600│MaxDD-720 │
│ MCL    4 │ MCL    5 │
│ Total 10k│ Total 8k │
│ Final 14k│ Final 12k│
└──────────┴──────────┘
```

Metrics per column: Win Rate %, EV $/trade, Sharpe, Profit Factor, Max DD $, Max Consec L, Total $, Final Bal $.

Train column rendered with muted opacity. Test column is primary.

**Distribution Overlays (2 charts per pair):**
- MAE KDE: train (dashed) vs test (solid) density curves overlaid
- MFE KDE: train (dashed) vs test (solid) density curves overlaid
- Visual check: similar shapes = regime consistent; divergent shapes = regime shifted

### C. Drift Summary Table
Single table across all pairs:

| Metric | Pair 1 Train | Pair 1 Test | Δ1 | Pair 2 Train | Pair 2 Test | Δ2 | ... |
|--------|-------------|-------------|-----|-------------|-------------|-----|-----|
| WR % | 46.3 | 42.1 | -4.2 | 43.9 | 38.5 | -5.4 | |
| EV $ | 249 | 220 | -29 | 243 | 195 | -48 | |
| PF | 4.09 | 3.50 | -0.59 | 3.89 | 2.90 | -0.99 | |
| Sharpe | 2.31 | 1.90 | -0.41 | 2.22 | 1.60 | -0.62 | |

Δ column color-coded:
- |Δ| < 20% of train value → green (robust)
- |Δ| 20-40% → amber (mild decay)
- |Δ| > 40% → red (overfit signal)

Table shows the **best-performing stop variant per pair** (highest test EV). A subtitle notes which variant was selected.

**Best Stop Variant Highlight:**
Below the drift table, a callout identifying which stop variant has the most consistent test performance across all pairs (lowest coefficient of variation in test EV). E.g.: "P90 MAE Stop is the most regime-stable — test EV coefficient of variation: 12%"

### D. Existing Sections
Computed from combined test data (same as current custom ranges view):
- Stability Dashboard charts
- Hourly Edge Persistence
- MAE/MFE Deep Study
- Efficiency analysis
- R-Distribution
- Equity Curve Overlay
- Volatility Regime
- Streak Analysis
- Trades Table

---

## Diagnostics

### Overfitting Score
Per pair, per stop variant: `(Test EV / Train EV) × 100`

Displayed as badge on pair header (using best stop variant):
- 80-120% → green **ROBUST**
- 60-80% → amber **MILD DECAY**
- <60% → red **OVERFIT**

### Regime Fingerprint
Per period (train and test), 4 metrics:
- **Avg risk pts** — mean `risk_pts` across trades (volatility proxy)
- **Long/Short %** — directional bias
- **MFE/MAE ratio** — median MFE / median MAE (trending = higher)
- **Trade density** — trades / calendar days in period

Displayed as a compact comparison row. Highlights which metrics shifted between train and test, helping explain performance changes.

### Best Stop Variant
Across all pairs, for each stop variant compute the coefficient of variation (std/mean) of test EV. The variant with the lowest CV is the most regime-stable. Highlighted as a recommendation.

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| 1 range | No pairing. Shows standalone range result card (current behavior) |
| 2 ranges | 1 pair. Most common use case |
| Odd last range | Unpaired — shows as standalone with label "Unpaired — no walk-forward analysis" |
| Train < 20 winners | Pair still shown. Warning badge: "Low sample (N winners) — parameters may be unreliable" |
| Test has 0 trades | Pair shown with empty test column and "No trades in test period" message |

---

## Implementation Constraints

- **Client-side only** — no changes to `model_stats.py`. All computation uses existing `recent_trades` array.
- **No new Python resolution** — trade outcome adjustment is done by checking `mae_pct` against the stop cap, not by re-running bar-by-bar resolution. Accurate for stop simulation, approximate for targets.
- **Replaces current custom view** — `renderCustomViewV2` is rewritten. `computeRangeStats` is reused as-is.
- **Existing sections preserved** — stability charts, hourly persistence, equity curves, trades table all remain, computed from combined test data.
- **Performance** — 4 variants × 2 (train+test) × N pairs = 8N stat computations. Each is a filter + `computeRangeStats` call on in-memory trades. Fast for typical range sizes (<500 trades per period).
