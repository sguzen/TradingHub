"""Tests for Analysis/engine/bars.py."""
import pandas as pd
import pytest
import bars
import helpers


def test_db_path_resolves_to_fractal_sweep_duckdb():
    p = bars.db_path()
    assert p.name == 'candle_science.duckdb'
    assert p.parent.name == 'Fractal Sweep'
