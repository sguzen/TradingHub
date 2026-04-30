"""Tests for the Pine emitter.

The emitter takes parquet-shape DataFrames and produces a Pine source string
with hashed state-keys and per-state probability arrays. We don't validate
Pine syntax (TradingView's parser is the source of truth there); we validate
the structural shape, hash determinism, quantization, and size-budget assert.
"""
from __future__ import annotations

import re

import pandas as pd
import pytest

from engine import pine_emit
from engine.state_vector import canonical_hash


# Build a minimal-but-valid v1 state_key for tests. Format must match
# build_triad_key / build_hour_key — we hand-craft keys here so the tests
# don't depend on those helpers' internals.
def _triad_key(suffix: str = "a") -> str:
    return (
        f"v1|sym=NQ|tf=triad|block=09-12|c1cls=line-up|c2q=Q1|"
        f"c2vh=inside|c2vl=inside|c2sw_c1h=N|c2sw_c1l=N|c2_inside=N|"
        f"midhr=untouched|mid3h=untouched|box_react=none|tag={suffix}"
    )


def _hour_key(suffix: str = "a") -> str:
    return (
        f"v1|sym=NQ|tf=hour|block=09-12|hour_idx=1|q=Q1|"
        f"q1cls=in-stat-low|q2cls=inside|q3cls=inside|q4cls=inside|"
        f"sweep_set=none|midhr=untouched|box_react=none|tag={suffix}"
    )


def _row(state_key: str, outcome: str, p: float, n: int) -> dict:
    return {
        "state_key": state_key, "outcome": outcome,
        "p": p, "ci_lo": max(0.0, p - 0.1), "ci_hi": min(1.0, p + 0.1), "n": n,
    }


# ── Triad emission shape ─────────────────────────────────────────────────

def test_triad_emits_six_element_array():
    df = pd.DataFrame([
        _row(_triad_key("t1"), "line-up",   0.4, 100),
        _row(_triad_key("t1"), "line-down", 0.1, 100),
        _row(_triad_key("t1"), "apex-up",   0.2, 100),
        _row(_triad_key("t1"), "apex-down", 0.1, 100),
        _row(_triad_key("t1"), "doji",      0.2, 100),
    ])
    out = pine_emit.emit_pine_tables(df, df.iloc[0:0].copy())  # NQ has data, ES empty
    h = canonical_hash(_triad_key("t1"))
    expected = f'map.put(EMPIRICAL_NQ_TRIAD, "{h}", array.from(0.4, 0.1, 0.2, 0.1, 0.2, 100.0))'
    assert expected in out


def test_hour_emits_four_element_array():
    df = pd.DataFrame([
        _row(_hour_key("h1"), "line-up",   0.5, 50),
        _row(_hour_key("h1"), "line-down", 0.2, 50),
        _row(_hour_key("h1"), "doji",      0.3, 50),
    ])
    out = pine_emit.emit_pine_tables(df, df.iloc[0:0].copy())
    h = canonical_hash(_hour_key("h1"))
    expected = f'map.put(EMPIRICAL_NQ_HOUR, "{h}", array.from(0.5, 0.2, 0.3, 50.0))'
    assert expected in out


def test_missing_outcome_defaults_to_zero():
    # Triad row with only line-up + doji recorded; the other 3 outcomes
    # should default to 0.0 (no NaN, no exception).
    df = pd.DataFrame([
        _row(_triad_key("partial"), "line-up", 0.7, 30),
        _row(_triad_key("partial"), "doji",    0.3, 30),
    ])
    out = pine_emit.emit_pine_tables(df, df.iloc[0:0].copy())
    h = canonical_hash(_triad_key("partial"))
    expected = f'map.put(EMPIRICAL_NQ_TRIAD, "{h}", array.from(0.7, 0.0, 0.0, 0.0, 0.3, 30.0))'
    assert expected in out


# ── Probabilities are quantized to 1 decimal place ──────────────────────

def test_probabilities_quantized_to_one_decimal():
    df = pd.DataFrame([_row(_hour_key("q"), "line-up", 0.4567, 25)])
    out = pine_emit.emit_pine_tables(df, df.iloc[0:0].copy())
    # 0.4567 → 0.5 (one-decimal rounding)
    assert "0.5" in out
    assert "0.4567" not in out


# ── Both NQ and ES sections present ─────────────────────────────────────

def test_both_symbols_emit_independent_blocks():
    nq = pd.DataFrame([_row(_hour_key("nq"), "line-up", 0.4, 20)])
    es = pd.DataFrame([_row(_hour_key("es"), "doji",    0.6, 18)])
    out = pine_emit.emit_pine_tables(nq, es)
    assert "EMPIRICAL_NQ_HOUR" in out
    assert "EMPIRICAL_NQ_TRIAD" in out
    assert "EMPIRICAL_ES_HOUR" in out
    assert "EMPIRICAL_ES_TRIAD" in out


def test_paste_region_sentinels_present():
    out = pine_emit.emit_pine_tables(pd.DataFrame(columns=["state_key","outcome","p","ci_lo","ci_hi","n"]),
                                     pd.DataFrame(columns=["state_key","outcome","p","ci_lo","ci_hi","n"]))
    assert pine_emit.PASTE_START in out
    assert pine_emit.PASTE_END   in out
    # START must precede END.
    assert out.index(pine_emit.PASTE_START) < out.index(pine_emit.PASTE_END)


# ── Routing: tf=triad goes to triad map, tf=hour goes to hour map ───────

def test_triad_and_hour_keys_route_to_correct_maps():
    df = pd.DataFrame([
        _row(_triad_key("only_triad"), "line-up", 0.3, 40),
        _row(_hour_key("only_hour"),   "doji",    0.5, 22),
    ])
    out = pine_emit.emit_pine_tables(df, df.iloc[0:0].copy())
    triad_h = canonical_hash(_triad_key("only_triad"))
    hour_h  = canonical_hash(_hour_key("only_hour"))
    # Triad hash should appear in the TRIAD map block, not the HOUR map block.
    triad_block_start = out.index("EMPIRICAL_NQ_TRIAD")
    hour_block_start  = out.index("EMPIRICAL_NQ_HOUR")
    triad_section = out[triad_block_start:hour_block_start]
    hour_section  = out[hour_block_start:]
    assert triad_h in triad_section
    assert triad_h not in hour_section
    assert hour_h in hour_section
    assert hour_h not in triad_section


# ── Hash collisions: two different state_keys → two different hashes ────

def test_distinct_state_keys_produce_distinct_hashes():
    df = pd.DataFrame([
        _row(_triad_key("aaa"), "line-up", 0.5, 100),
        _row(_triad_key("bbb"), "line-up", 0.5, 100),
    ])
    out = pine_emit.emit_pine_tables(df, df.iloc[0:0].copy())
    h_a = canonical_hash(_triad_key("aaa"))
    h_b = canonical_hash(_triad_key("bbb"))
    assert h_a != h_b
    assert h_a in out
    assert h_b in out


# ── Size budget assertion ───────────────────────────────────────────────

def test_size_budget_passes_for_small_input():
    df = pd.DataFrame([_row(_hour_key("small"), "line-up", 0.4, 30)])
    out = pine_emit.emit_pine_tables(df, df.iloc[0:0].copy())
    pine_emit.assert_size_budget(out)  # should not raise


def test_size_budget_raises_when_exceeded():
    out = "x" * (pine_emit.MAX_BYTES + 1)
    with pytest.raises(AssertionError, match="exceeds budget"):
        pine_emit.assert_size_budget(out)


# ── splice_into_pine_file: replaces between sentinels ───────────────────

def test_splice_replaces_paste_region(tmp_path):
    pine_file = tmp_path / "ind.pine"
    pine_file.write_text(
        "//@version=6\n"
        "indicator(\"x\")\n"
        f"{pine_emit.PASTE_START}\n"
        "// stale content here\n"
        f"{pine_emit.PASTE_END}\n"
        "// trailing code\n"
    )

    new_block = (
        f"{pine_emit.PASTE_START}\n"
        f"// fresh content\n"
        f"{pine_emit.PASTE_END}\n"
    )
    pine_emit.splice_into_pine_file(str(pine_file), new_block)

    text = pine_file.read_text()
    assert "stale content" not in text
    assert "fresh content" in text
    assert "// trailing code" in text  # surrounding code preserved
    # Indicator header still intact
    assert text.startswith("//@version=6\n")


def test_splice_raises_if_sentinels_missing(tmp_path):
    pine_file = tmp_path / "no_sentinels.pine"
    pine_file.write_text("//@version=6\nindicator(\"x\")\n")
    with pytest.raises(ValueError, match="missing or out-of-order"):
        pine_emit.splice_into_pine_file(str(pine_file), "// whatever\n")
