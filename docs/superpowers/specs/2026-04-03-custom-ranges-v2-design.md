# Custom Date Ranges V2 — Deep Comparative Study

## Summary

Redesign the Custom Ranges view in the Fractal Sweep dashboard from basic stat tiles into a comprehensive PhD-level statistical study that answers: "How does the model sustain across different time periods, and where does the edge degrade?"

## Data Source

Client-side only. All analysis computed from `recent_trades` array (every resolved trade with date, direction, hr, mn, session, dow, entry_price, sweep_extreme, risk_pts, r, outcome, mae_pct, mfe_pct, classification). No Python changes needed.

## Layout: Single Scrollable View

When "Custom Ranges" is selected and Apply is clicked, the view renders vertically with these sections:

---

### Section A: Combined Hero Tiles (keep as-is)

One row of hero cards for the merged dataset: WR, EV(R), PF, CE, Total P&L, Sharpe, Max DD, Max L Run. Subtitle: "Combined · N trades · X ranges"

---

### Section B: Side-by-Side Range Cards (keep as-is)

One compact card per range (color-coded): dates, trade count, WR, EV, PF, CE, Max DD, Long/Short split.

---

### Section C: Stability Dashboard

A grid of comparison metrics showing whether the model's edge holds, degrades, or improves across the selected ranges. Each metric is a **grouped bar chart** with one bar per range + combined.

**Row 1 — Core Edge Metrics:**
- Win Rate bar chart (all ranges + combined, with 50% breakeven line)
- EV per trade bar chart
- Profit Factor bar chart
- CE (Combined Edge) bar chart

**Row 2 — Risk Metrics:**
- Max Drawdown % bar chart
- Max Consecutive Losses bar chart
- Sharpe Ratio bar chart
- Win/Loss Ratio (avg win R / avg loss R) bar chart

**Row 3 — Directional Stability:**
- Long WR vs Short WR per range (grouped)
- Long EV vs Short EV per range (grouped)

---

### Section D: Hourly Edge Persistence

Heatmap table: rows = hours (07:00–16:00), columns = ranges + combined. Cell = WR% with color gradient (green > 60%, amber 50–60%, red < 50%). Cell also shows trade count.

This answers: "Does the 9am edge hold in 2024 the same as 2020?"

---

### Section E: MAE Deep Study

**E1. Percentile Comparison Table**
Table: rows = percentiles (p5, p10, p25, p50, p75, p90, p95), columns = ranges + combined. Cells = MAE% value. Color gradient: lower = greener (tighter stops = better entries).

**E2. Mode Bar Chart**
One bar per range + combined. Bar height = MAE mode (most common bin at 0.01% granularity). Shows where most trades cluster.

**E3. Median Bar Chart**
Same layout. Bar height = MAE median. More robust than mean for skewed distributions.

**E4. Standard Deviation Bar Chart**
Same layout. Bar height = MAE std dev. Higher = more dispersed MAE = less predictable entries.

**E5. Bell Curve Overlay**
Single canvas, all ranges overlaid as density curves (kernel density estimation or binned histogram smoothed). Each range in its color. X-axis = MAE%, Y-axis = density. This shows the shape of each range's MAE distribution and whether they cluster similarly or drift.

---

### Section F: MFE Deep Study

Same structure as Section E but for MFE:
- F1. Percentile Comparison Table
- F2. Mode Bar Chart
- F3. Median Bar Chart  
- F4. Standard Deviation Bar Chart
- F5. Bell Curve Overlay

---

### Section G: MFE/MAE Efficiency Ratio

**G1. Ratio Bar Chart**
For each range + combined: bar = median(MFE) / median(MAE). Higher = more efficient entries (favorable excursion outpaces adverse). This is the single best number for "entry quality."

**G2. Ratio Scatter**
Each trade plotted: x = MAE%, y = MFE%. One color per range. Overlaid on same chart. Cluster position shows if entries are improving or degrading. A tight cluster in the bottom-left (low MAE, high MFE relative to MAE) = ideal.

---

### Section H: R-Distribution Comparison

**H1. R-Distribution Histogram Overlay**
Stacked/overlaid histograms of R values. X-axis = R buckets (-1, 0–1, 1–2, 2–3, 3–5, 5+). One bar group per range. Shows if the tail (big winners) is thicker in certain periods.

**H2. Average Win R Bar Chart**
One bar per range. Shows if the runner captures more R in certain periods (higher = better trending environment).

---

### Section I: Equity Curve Overlay

All ranges' equity curves plotted on the same chart (each starting from $0 or $4,500). X-axis = trade number (normalized, so ranges of different lengths are comparable). Y-axis = equity. Each range in its color. Shows trajectory shape: smooth = consistent, jagged = streaky.

---

### Section J: Volatility Regime Analysis

**J1. Risk Size Distribution**
Box plots or violin plots of `risk_pts` per range. Shows if certain periods have structurally different stop sizes (higher vol = larger stops).

**J2. WR by Volatility Quartile**
Table: rows = vol quartiles (Low/Med-Low/Med-High/High based on risk_pts percentiles), columns = ranges. Cells = WR%. Shows if the model works better in specific volatility environments and whether that changes across periods.

---

### Section K: Streak Analysis

**K1. Win Streak Distribution**
Box plot or histogram of consecutive win streak lengths per range. Shows if momentum clustering is stable.

**K2. Loss Streak Distribution**
Same for loss streaks. Critical for risk: does the model have longer drawdown periods in certain ranges?

**K3. Recovery Time**
After each loss streak, how many trades until a new equity high? Bar chart of median recovery time per range.

---

## Color Scheme

| Range | Swatch | Hex |
|-------|--------|-----|
| 1 | Blue | `#3b82f6` |
| 2 | Amber | `#f59e0b` |
| 3 | Purple | `#8b5cf6` |
| 4 | Teal | `#14b8a6` |
| Combined | Gray | `#94a3b8` |

## Chart Implementation Notes

All charts rendered on `<canvas>` elements using 2D context, matching the existing dashboard's chart style (dark background, monospace fonts, gridlines).

**Bell curve / density estimation:** Use simple kernel density estimation in JS — for each value, add a Gaussian kernel (bandwidth = Silverman's rule: `0.9 * min(std, IQR/1.34) * n^(-1/5)`). Sample 200 points across the range to build the curve.

**Box plots:** Draw min, p25, median, p75, max as a standard box-and-whisker using canvas lines and rectangles.

**Scatter plots:** Direct canvas point drawing with semi-transparent dots for overlap visibility.

## Computation: `computeRangeStats()` v2

Extend the existing function to compute all metrics needed:

```javascript
{
  // existing
  n, nWins, nLosses, wr, ev_r, pf, ce, mcl, maxDDPct, totalPnl, sharpe, blown,
  byClass, maeDist, mfeDist, mfeMaeRatio, longN, shortN, longWR, shortWR,
  
  // new
  avgWinR,              // mean R of winning trades
  avgLossR,             // mean R of losing trades (always -1 for structural)
  winLossRatio,         // avgWinR / |avgLossR|
  
  byHour: {             // hr → {n, wins, wr}
    7: {n, wins, wr}, 8: {...}, ...
  },
  
  maePercentiles: {     // p5, p10, p25, p50, p75, p90, p95
    p5, p10, p25, p50, p75, p90, p95
  },
  mfePercentiles: {     // same
    p5, p10, p25, p50, p75, p90, p95
  },
  
  rDistribution: {      // bucket → count
    '-1': n, '0-1': n, '1-2': n, '2-3': n, '3-5': n, '5+': n
  },
  
  riskPtsQuartiles: {   // quartile → {n, wr, ev}
    low: {n, wr, ev}, medLow: {...}, medHigh: {...}, high: {...}
  },
  
  winStreaks: [lengths], // array of consecutive win streak lengths
  lossStreaks: [lengths], // array of consecutive loss streak lengths
  medianRecovery: n,    // median trades to new equity high after loss streak
  
  equityCurve: [values], // sequential equity values for each trade
  
  maeDensity: [{x, y}], // KDE density curve points
  mfeDensity: [{x, y}], // KDE density curve points
  
  scatterPoints: [{mae, mfe}], // raw MAE/MFE pairs for scatter plot
}
```

## Scope

- Dashboard-only change (`model_dashboard.html`)
- No changes to `model_stats.py` or JSON
- All computation client-side from `recent_trades`
- Replaces the existing custom ranges sections C–F (removes classification table, basic histograms)
- Keeps sections A (combined hero) and B (side-by-side cards)
