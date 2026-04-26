"""Bar construction layer.

Builds canonical hourly + quarter-of-hour bar dataframes from the shared
nq_1m table. All downstream studies consume these dataframes.

Trading day convention: 18:00 ET → next-day 17:00 ET.
- 17:00 ET hour is excluded (settlement gap, no data).
- Sunday 18:00 hour treats Friday 16:00 as its previous trading hour
  (handled implicitly: cleaned hourly dataframe has Sun 18:00 as the
   row immediately after Fri 16:00, so shift(1) gives the right answer).
"""
from __future__ import annotations
from pathlib import Path
import duckdb
import pandas as pd
import numpy as np


def db_path() -> Path:
    """Resolve the shared DuckDB path (always Fractal Sweep/candle_science.duckdb)."""
    # Analysis/engine/bars.py → Analysis/engine → Analysis → Statistic.ally
    return Path(__file__).resolve().parent.parent.parent / 'Fractal Sweep' / 'candle_science.duckdb'
