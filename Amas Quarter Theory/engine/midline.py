"""Midline source resolution.

The active midline of the current hour (or current 3h block) is:
- the PRIOR candle's mid, if the current candle is strictly inside prior range
  (current.low > prior.low AND current.high < prior.high)
- otherwise the CURRENT candle's mid.

Equality on either side counts as broken-out (uses current).
"""
from __future__ import annotations

from typing import Literal, Optional

MidlineSource = Literal["prior", "current"]


def resolve_midline_source(
    current_high: float,
    current_low: float,
    prior_high: Optional[float],
    prior_low: Optional[float],
) -> tuple[float, MidlineSource]:
    """Return (mid_price, source). If no prior, falls back to current."""
    if prior_high is None or prior_low is None:
        return ((current_high + current_low) / 2.0, "current")

    strictly_inside = (current_low > prior_low) and (current_high < prior_high)
    if strictly_inside:
        return ((prior_high + prior_low) / 2.0, "prior")
    return ((current_high + current_low) / 2.0, "current")
