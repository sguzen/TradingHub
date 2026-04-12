# Deep Data Analytics — MAE/MFE Study Tab

## Overview

A dedicated "Study" tab in the TTFM Fractal Sweep dashboard providing deep statistical analysis of MAE (Maximum Adverse Excursion) and MFE (Maximum Favorable Excursion) distributions. All computation is client-side JavaScript from `recent_trades` data.

## Layout

- New page tab: **Study** (after Trades)
- T-Spot type pill selector at top: **All | Normal | Expansive | Pro-trend**
- Two-column layout: **MAE (left) | MFE (right)**
- Full-width panel at bottom: **MFE:MAE Ratio Distribution**
- Respects existing Direction filter and Period filter dropdowns

## Panels (mirrored for MAE and MFE)

### 1. Distribution Chart (histogram + KDE overlay)

- Histogram: auto-binned frequency bars (Freedman-Diaconis rule for bin width)
- KDE: smooth Gaussian kernel density curve overlaid on histogram
- Vertical dashed reference lines:
  - Mean (blue) with label
  - Median (amber) with label  
  - Mode (purple) — KDE peak with label
- X-axis: percentage values (e.g. 0.00% to max)
- Y-axis: frequency count (left) / density (right, for KDE)
- SVG rendered, theme-aware colors

### 2. Percentile Ladder

- Table with columns: Percentile | Value | Visual
- Rows: P5, P10, P25, P50, P75, P90, P95, P99
- Visual column: horizontal dot-strip showing position on min-max scale
- IQR (P25-P75) highlighted with a colored band
- All values formatted as `X.XXXX%`

### 3. Shape Statistics

Cards in a 2x2 grid:
- **Skewness**: value + interpretation label (e.g. "Right-skewed" / "Symmetric" / "Left-skewed")
- **Excess Kurtosis**: value + interpretation (e.g. "Heavy-tailed" / "Normal" / "Light-tailed")
- **Coefficient of Variation**: std/mean as percentage — measures relative dispersion
- **IQR/Median Ratio**: (P75-P25)/P50 — robust spread measure

Formulas:
- Skewness: `(1/n) * sum((xi - mean)^3) / std^3`
- Excess Kurtosis: `(1/n) * sum((xi - mean)^4) / std^4 - 3`
- CV: `std / mean`

### 4. Mode Analysis

- Primary mode: peak of KDE curve (scan for local maxima)
- If multimodal (secondary peaks > 30% of primary peak height): show top 2-3 modes
- Each mode shows: value, density height, percentage of trades within ±1 bin-width
- Modes marked on the distribution chart with purple triangles

### 5. Tail Analysis

Four stat cards:
- **Extreme %**: percentage of trades beyond P90
- **2x Median %**: percentage of trades exceeding 2x the median value
- **Top 10% Mean vs Bottom 10% Mean**: ratio showing asymmetry between tails
- **Tail Ratio (P95/P50)**: how extreme the upper tail is relative to the center

### 6. MFE:MAE Ratio Distribution (full-width)

- Histogram of per-trade `mfe_pct / mae_pct` ratio (cap at 20x for display)
- Bins: 0-1x, 1-2x, 2-3x, 3-5x, 5-10x, 10x+
- Each bar shows count and percentage
- Cumulative line overlay: "X% of trades have MFE > Nx MAE"
- Summary cards: % of trades where MFE > 1x MAE, > 2x, > 3x, > 5x, > 10x

## T-Spot Type Filter

Pills at top of Study tab: All | Normal | Expansive | Pro-trend
- Filters `recent_trades` by `tspot_type` before computing all panels
- "All" shows combined data
- Active pill uses accent color styling
- Composable with existing Direction and Period filters

## Interactions

- Hover on histogram bars: tooltip with exact count, percentage of total, bin range
- Hover on KDE curve: tooltip with density value at cursor position
- Hover on percentile dots: tooltip with exact value
- All reference lines (mean/median/mode) have persistent labels

## Technical Notes

- KDE bandwidth: Silverman's rule of thumb: `h = 1.06 * std * n^(-1/5)`
- KDE evaluation: 200 evenly spaced points across data range
- Histogram bins: Freedman-Diaconis: `bin_width = 2 * IQR * n^(-1/3)`, minimum 15 bins, maximum 50
- All SVG charts, no canvas
- Theme-aware: uses CSS variables (`--bg-card`, `--text-primary`, etc.)
- Trades with null MAE/MFE are excluded from computations
- MFE:MAE ratio: exclude trades where MAE = 0 (division by zero)

## File Changes

- Modify: `Fractal Sweep/model_dashboard.html` — add Study tab HTML, JS computation functions, SVG rendering
- No engine changes needed — all data is already in `recent_trades`
