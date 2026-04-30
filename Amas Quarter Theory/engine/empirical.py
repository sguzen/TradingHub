"""Empirical probability aggregator.

Reduces a stream of DecisionPointSample → a DataFrame with columns:
  state_key | outcome | p | ci_lo | ci_hi | n
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

import pandas as pd

from engine.sampler import DecisionPointSample
from engine.stats import wilson_ci


def aggregate_samples(samples: Iterable[DecisionPointSample]) -> pd.DataFrame:
    """Aggregate samples into a probability table.

    Probability = outcome_count / total_for_state. Wilson CI on the proportion.
    """
    by_state: dict[str, Counter] = {}
    for s in samples:
        by_state.setdefault(s.state_key, Counter())[s.outcome] += 1

    rows = []
    for key, counts in by_state.items():
        n = sum(counts.values())
        for outcome, wins in counts.items():
            p = wins / n
            lo, hi = wilson_ci(wins=wins, n=n)
            rows.append({
                "state_key": key, "outcome": outcome,
                "p": p, "ci_lo": lo, "ci_hi": hi, "n": n,
            })
    return pd.DataFrame(rows)


def run_full_empirical(df_bars: pd.DataFrame, sym: str) -> pd.DataFrame:
    """End-to-end: bars → walk triads → sample decision points → aggregate.

    Wraps walk_triads + sample_decision_points + aggregate_samples for callers
    that just want the final parquet-shape DataFrame.
    """
    from engine.walker import walk_triads
    from engine.sampler import sample_decision_points

    all_samples = []
    for episode in walk_triads(df_bars):
        all_samples.extend(sample_decision_points(episode, sym=sym))
    return aggregate_samples(all_samples)
