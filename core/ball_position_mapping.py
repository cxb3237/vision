"""Calibrated pixel-X to steel-ball lateral position conversion."""

from __future__ import annotations

import math

from core.models import BallPositionMappingConfig


def _round_half_away_from_zero(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def pixel_x_to_mm(
    center_x: int | float,
    mapping: BallPositionMappingConfig,
) -> int | None:
    """Map a detected pixel X to -125..125 mm, or None when uncalibrated."""

    if not mapping.calibrated:
        return None
    if isinstance(center_x, bool) or not isinstance(center_x, (int, float)):
        raise TypeError("center_x must be numeric")
    value = float(center_x)
    if not math.isfinite(value):
        raise ValueError("center_x must be finite")
    span = mapping.x_plus_125_px - mapping.x_minus_125_px
    if span == 0:
        raise ValueError("position mapping endpoints must differ")
    mapped = -125.0 + 250.0 * (
        (value - mapping.x_minus_125_px) / span
    )
    return max(-125, min(125, _round_half_away_from_zero(mapped)))
