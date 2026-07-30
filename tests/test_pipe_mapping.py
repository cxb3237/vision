from __future__ import annotations

import pytest

from detectors.pipe_marker_detector import compute_ball_position_mm


def position(marker_a, marker_b, ball_center):
    return compute_ball_position_mm(marker_a, marker_b, ball_center, -125.0, 125.0)


def test_ball_at_marker_a_is_negative_endpoint() -> None:
    assert position((10, 20), (110, 20), (10, 20)) == pytest.approx(-125.0)


def test_ball_at_midpoint_is_zero() -> None:
    assert position((10, 20), (110, 20), (60, 20)) == pytest.approx(0.0)


def test_ball_at_marker_b_is_positive_endpoint() -> None:
    assert position((10, 20), (110, 20), (110, 20)) == pytest.approx(125.0)


def test_tilted_marker_midpoint_is_zero() -> None:
    assert position((10, 20), (110, 120), (60, 70)) == pytest.approx(0.0)


def test_ball_beyond_endpoints_is_clamped() -> None:
    assert position((10, 20), (110, 20), (-40, 20)) == pytest.approx(-125.0)
    assert position((10, 20), (110, 20), (160, 20)) == pytest.approx(125.0)
