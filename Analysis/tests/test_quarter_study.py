"""Tests for Analysis/engine/quarter_study.py."""
import pandas as pd
import numpy as np
import pytest
import quarter_study as qs
import bars
import helpers


def _build_one_hour(ohlc=(100, 110, 90, 105), high_min=20, low_min=40):
    minutes = helpers.make_hour('2024-01-02 10:00', ohlc=ohlc,
                                high_at_minute=high_min, low_at_minute=low_min)
    enriched = bars._enrich_minutes(minutes)
    hourly, quarters = bars.build_all_from_minutes(enriched)
    return enriched, hourly, quarters


def test_q_of_high_when_high_in_q2():
    _, hourly, quarters = _build_one_hour(high_min=20, low_min=40)
    feats = qs.build_features(hourly, quarters)
    assert feats.iloc[0]['q_of_high'] == 2  # minute 20 → Q2


def test_q_of_high_when_high_in_q4():
    _, hourly, quarters = _build_one_hour(high_min=50, low_min=10)
    feats = qs.build_features(hourly, quarters)
    assert feats.iloc[0]['q_of_high'] == 4
    assert feats.iloc[0]['q_of_low'] == 1


def test_extreme_first_high_before_low():
    _, hourly, quarters = _build_one_hour(high_min=10, low_min=50)
    feats = qs.build_features(hourly, quarters)
    assert feats.iloc[0]['extreme_first'] == 'H'


def test_extreme_first_low_before_high():
    _, hourly, quarters = _build_one_hour(high_min=50, low_min=10)
    feats = qs.build_features(hourly, quarters)
    assert feats.iloc[0]['extreme_first'] == 'L'


def test_quarter_directions_present():
    _, hourly, quarters = _build_one_hour()
    feats = qs.build_features(hourly, quarters)
    assert 'q1_dir' in feats.columns
    assert 'q4_range' in feats.columns
    assert 'hour_dir' in feats.columns
