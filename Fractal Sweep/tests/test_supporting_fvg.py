"""Tests for find_supporting_fvg — supporting FVG confluence detection."""
import numpy as np
import pytest

import model_stats as ms


def _arrs(bars):
    """Build a minimal arrs dict with just the OHLC fields find_supporting_fvg uses."""
    n = len(bars)
    return dict(
        open  = np.array([b[0] for b in bars], dtype='float64'),
        high  = np.array([b[1] for b in bars], dtype='float64'),
        low   = np.array([b[2] for b in bars], dtype='float64'),
        close = np.array([b[3] for b in bars], dtype='float64'),
    )


def test_bullish_strict_fvg_between_sl_and_entry():
    # 5 bars total. FVG forms at index 2 (3-bar pattern using bars 0,1,2).
    # bar 0 high = 100, bar 2 low = 105 → bullish FVG band (100, 105].
    # No subsequent bar dips below 100 → unfilled.
    bars = [
        # (open, high, low, close)
        (95, 100, 92, 99),   # 0 — defines lower edge (high=100)
        (99, 103, 98, 102),  # 1 — middle bar (irrelevant to gap)
        (102, 108, 105, 107),# 2 — defines upper edge (low=105) → FVG forms here
        (107, 110, 106, 109),# 3 — stays above 100, doesn't fill
        (109, 112, 108, 111),# 4 — entry bar (will not be inspected past entry_idx)
    ]
    arrs = _arrs(bars)
    # Long trade: sweep_extreme=98 (below the gap), entry_price=109 (above the gap).
    # Body of gap (100, 105] is fully between 98 and 109 → strict True, loose True.
    strict, loose = ms.find_supporting_fvg(
        arrs, window_start_idx=0, entry_idx=4,
        sweep_extreme=98.0, entry_price=109.0, direction='LONG',
    )
    assert strict is True
    assert loose is True
