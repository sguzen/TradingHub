"""Hourly breakout follow-through study.

For each hour H1 with a valid prev hour H0:
- bullish breakout: H1.close > H0.high  (strict)
- bearish breakout: H1.close < H0.low   (strict)
- neither: everything else (including inside bars)

Then for each breakout, look at H2 to detect:
- bullish follow-through: H2 prints high > H1.high at any minute
- bearish follow-through: H2 prints low < H1.low at any minute
- immediate-reversal: H2 takes out the *opposite* extreme of H1
"""
from __future__ import annotations
import pandas as pd
import numpy as np


def classify(hourly: pd.DataFrame) -> pd.DataFrame:
    """Add a `breakout` column to the hourly dataframe.

    Values: 'bullish', 'bearish', 'neither', or 'no_prev' (first row, no H0).
    """
    df = hourly.copy()
    breakout = pd.Series('neither', index=df.index, dtype='object')
    no_prev_mask = df['prev_hour_high'].isna()
    bull = df['close'] > df['prev_hour_high']
    bear = df['close'] < df['prev_hour_low']
    breakout.loc[bull] = 'bullish'
    breakout.loc[bear] = 'bearish'
    breakout.loc[no_prev_mask] = 'no_prev'
    df['breakout'] = breakout
    # h1_open vs prev_mid (above/below/equal)
    df['h1_open_vs_prev_mid'] = np.where(
        df['open'] > df['prev_hour_mid'], 'above',
        np.where(df['open'] < df['prev_hour_mid'], 'below', 'equal')
    )
    # Use pd.NA so downstream code can check with pd.isna(); plain None
    # would be silently coerced to float NaN due to the string-typed column.
    df.loc[no_prev_mask, 'h1_open_vs_prev_mid'] = pd.NA
    return df
