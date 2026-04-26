"""Tests for engine.outcomes — SL/TP scanner.

Per the design spec, Category D (Outcome resolver fidelity): same-bar tie → SL,
expired excluded from WR, MAE/MFE measured to resolution, direction symmetric,
deterministic & idempotent.
"""
from __future__ import annotations

import pandas as pd
import pytest

from engine.outcomes import resolve_outcome, Setup, Outcome


def _make_bars(prices: list[tuple[float, float, float, float]], start_ts: str = "2024-01-02 10:00") -> pd.DataFrame:
    """prices is a list of (open, high, low, close) tuples, one per minute starting at start_ts."""
    base = pd.Timestamp(start_ts, tz="America/New_York")
    rows = []
    for i, (o, h, l, c) in enumerate(prices):
        rows.append({
            "ts": base + pd.Timedelta(minutes=i),
            "open": o, "high": h, "low": l, "close": c, "volume": 10,
        })
    df = pd.DataFrame(rows)
    df["ts"] = df["ts"].astype("datetime64[ns, America/New_York]")
    df["volume"] = df["volume"].astype("int64")
    return df


def test_long_tp_hit_returns_r_one():
    bars = _make_bars([
        (100.0, 100.5, 99.8, 100.2),  # entry bar (excluded from resolution)
        (100.2, 100.6, 100.1, 100.5),
        (100.5, 105.0, 100.4, 104.8),  # high reaches TP=105
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=105.0, direction="long",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "TP"
    assert out.r == pytest.approx(1.0)


def test_long_sl_hit_returns_r_negative_one():
    bars = _make_bars([
        (100.0, 100.5, 99.8, 100.2),
        (100.2, 100.6, 100.1, 100.5),
        (100.5, 100.7, 94.0, 95.0),  # low reaches SL=95
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=105.0, direction="long",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "SL"
    assert out.r == pytest.approx(-1.0)


def test_same_bar_tp_and_sl_resolves_to_sl():
    """Per spec invariant D.1: same-bar tie → SL."""
    bars = _make_bars([
        (100.0, 100.5, 99.8, 100.2),  # entry bar
        (100.5, 106.0, 94.0, 100.0),  # high≥TP AND low≤SL → SL wins
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=105.0, direction="long",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "SL"
    assert out.r == pytest.approx(-1.0)


def test_short_tp_hit_returns_r_one():
    """Direction symmetry: short setup with falling price."""
    bars = _make_bars([
        (100.0, 100.2, 99.5, 99.8),
        (99.8, 99.9, 95.0, 95.5),  # low reaches TP=95
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=105.0, tp_price=95.0, direction="short",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "TP"
    assert out.r == pytest.approx(1.0)


def test_short_sl_hit_returns_r_negative_one():
    bars = _make_bars([
        (100.0, 100.2, 99.5, 99.8),
        (99.8, 105.5, 99.7, 105.0),  # high reaches SL=105
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=105.0, tp_price=95.0, direction="short",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "SL"
    assert out.r == pytest.approx(-1.0)


def test_expired_when_max_bars_exhausted():
    """Per spec invariant D.2: unresolved within OUTCOME_MAX_BARS → EXPIRED."""
    # Constant prices, never hits TP or SL
    bars = _make_bars([(100.0, 100.5, 99.5, 100.0)] * 1500)
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=105.0, direction="long",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "EXPIRED"
    assert out.r is None


def test_entry_bar_excluded_from_resolution():
    """Per spec invariant C.3: bars with ts == entry_ts do not contribute to TP/SL."""
    # Entry bar's high reaches TP, but it must be ignored
    bars = _make_bars([
        (100.0, 106.0, 99.9, 100.0),  # entry bar — high=106 but ignored
        (100.0, 100.2, 99.9, 100.0),  # subsequent bar, no resolution
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=105.0, direction="long",
    )
    out = resolve_outcome(bars, setup)
    # Should NOT be TP — entry bar must be excluded
    assert out.outcome != "TP"


def test_mae_mfe_measured_to_resolution():
    """Per spec invariant D.4: MAE/MFE for resolved trades stop at resolution bar."""
    bars = _make_bars([
        (100.0, 100.5, 99.8, 100.2),  # entry bar
        (100.2, 102.0, 100.0, 101.5),
        (101.5, 103.0, 101.0, 102.5),
        (102.5, 105.5, 102.0, 105.2),  # TP hit here
        (105.2, 110.0, 95.0, 95.0),    # AFTER resolution — must not affect MAE/MFE
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=105.0, direction="long",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "TP"
    # MFE in points = 5.5 (from 100 to 105.5 in resolution bar)
    assert out.mfe_pts == pytest.approx(5.5)
    # MAE in points = 1.0 (from 100 down to 99.8 in entry-not-counted ... actually first non-entry bar low=100.0)
    # Lowest low post-entry up to resolution bar = min(100.0, 101.0, 102.0) = 100.0
    assert out.mae_pts == pytest.approx(0.0, abs=0.01)


def test_resolve_is_deterministic_and_idempotent():
    """Per spec invariant B.4: same input → same output, every time."""
    bars = _make_bars([
        (100.0, 100.5, 99.8, 100.2),
        (100.2, 100.6, 100.1, 100.5),
        (100.5, 105.0, 100.4, 104.8),
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=105.0, direction="long",
    )
    out1 = resolve_outcome(bars, setup)
    out2 = resolve_outcome(bars, setup)
    assert out1 == out2


def test_known_fixture_r_math_long():
    """Per spec invariant F.6: entry=100, SL=95, exit=110 → r=2.0 for long."""
    bars = _make_bars([
        (100.0, 100.5, 99.8, 100.2),
        (100.5, 110.5, 100.0, 110.2),  # high reaches manual TP=110 → r=2 since risk=5
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=95.0, tp_price=110.0, direction="long",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "TP"
    assert out.r == pytest.approx(2.0)


def test_known_fixture_r_math_short():
    """Mirror: entry=100, SL=105, exit=90 → r=2.0 for short."""
    bars = _make_bars([
        (100.0, 100.2, 99.5, 99.8),
        (99.8, 100.0, 89.5, 90.0),  # low reaches TP=90 → r=2 since risk=5
    ])
    setup = Setup(
        entry_ts=bars["ts"].iloc[0], entry_price=100.0,
        sl_price=105.0, tp_price=90.0, direction="short",
    )
    out = resolve_outcome(bars, setup)
    assert out.outcome == "TP"
    assert out.r == pytest.approx(2.0)


def test_setup_with_zero_risk_raises():
    """A setup with entry == sl_price has zero risk; we can't compute R. Reject."""
    bars = _make_bars([(100.0, 100.5, 99.8, 100.2)])
    with pytest.raises(ValueError, match="risk"):
        Setup(entry_ts=bars["ts"].iloc[0], entry_price=100.0,
              sl_price=100.0, tp_price=105.0, direction="long")


def test_invalid_direction_raises():
    bars = _make_bars([(100.0, 100.5, 99.8, 100.2)])
    with pytest.raises(ValueError, match="direction"):
        Setup(entry_ts=bars["ts"].iloc[0], entry_price=100.0,
              sl_price=95.0, tp_price=105.0, direction="sideways")
