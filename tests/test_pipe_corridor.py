from __future__ import annotations

import math

import pytest

from core.pipe_corridor import (
    PipeAxis,
    PipeCorridorConfig,
    PipeCorridorFilter,
    effective_corridor_half_width_px,
    evaluate_pipe_corridor,
)


def config(**overrides) -> PipeCorridorConfig:
    values = {
        "enabled": True,
        "require_valid_geometry": True,
        "hold_last_valid_ms": 1000,
        "minimum_axis_length_px": 20,
        "corridor_half_width_px": 10,
        "end_margin_px": 5,
    }
    values.update(overrides)
    return PipeCorridorConfig(**values)


def decide(axis, point, **overrides):
    return evaluate_pipe_corridor(PipeAxis(axis[0], axis[1], 0), point, config(**overrides))


def test_horizontal_middle_and_boundary_are_accepted() -> None:
    assert decide(((10, 50), (110, 50)), (60, 50)).accepted
    boundary = decide(((10, 50), (110, 50)), (60, 60))
    assert boundary.accepted and boundary.perpendicular_distance_px == pytest.approx(10)


def test_tilted_axis_distance_is_correct() -> None:
    result = decide(((0, 0), (100, 100)), (50, 50))
    assert result.accepted and result.projection_t == pytest.approx(0.5)
    assert result.perpendicular_distance_px == pytest.approx(0)


def test_outside_corridor_is_rejected() -> None:
    assert decide(((10, 50), (110, 50)), (60, 61)).reason == "outside_corridor"


def test_before_and_after_end_are_rejected() -> None:
    assert decide(((10, 50), (110, 50)), (4, 50)).reason == "before_left_end"
    assert decide(((10, 50), (110, 50)), (116, 50)).reason == "after_right_end"


def test_end_margin_accepts_points_just_beyond_end() -> None:
    assert decide(((10, 50), (110, 50)), (5, 50)).accepted
    assert decide(((10, 50), (110, 50)), (115, 50)).accepted


def test_coincident_nonfinite_and_short_axes_are_rejected() -> None:
    assert decide(((10, 10), (10, 10)), (10, 10)).reason == "geometry_invalid"
    assert decide(((0, 0), (math.nan, 10)), (5, 5)).reason == "geometry_invalid"
    assert decide(((0, 0), (10, 0)), (5, 0)).reason == "axis_too_short"


def test_reversed_endpoints_preserve_corridor_acceptance() -> None:
    forward = decide(((10, 50), (110, 50)), (60, 55))
    reversed_axis = decide(((110, 50), (10, 50)), (60, 55))
    assert forward.accepted and reversed_axis.accepted
    assert forward.perpendicular_distance_px == reversed_axis.perpendicular_distance_px


def test_invalid_image_and_out_of_bounds_box_are_rejected() -> None:
    item = PipeCorridorFilter(config())
    assert item.update_axis((10, 50), (110, 50), (120, 100), now_ms=0)
    assert item.decide_point((60, 50), (0, 100), now_ms=0).reason == "geometry_invalid"
    assert item.decide_box({"x1": -1, "y1": 1, "x2": 10, "y2": 10}, (120, 100), now_ms=0).reason == "geometry_invalid"


def test_hold_last_valid_axis_expires() -> None:
    item = PipeCorridorFilter(config(hold_last_valid_ms=100))
    assert item.update_axis((10, 50), (110, 50), (120, 100), now_ms=1000)
    assert item.decide_point((60, 50), (120, 100), now_ms=1099).accepted
    expired = item.decide_point((60, 50), (120, 100), now_ms=1101)
    assert not expired.accepted and expired.reason == "geometry_missing"


def test_missing_geometry_can_fail_closed_or_open() -> None:
    closed = PipeCorridorFilter(config(require_valid_geometry=True))
    opened = PipeCorridorFilter(config(require_valid_geometry=False))
    assert not closed.decide_point((20, 20), (100, 100), now_ms=0).accepted
    assert opened.decide_point((20, 20), (100, 100), now_ms=0).accepted


def test_ratio_uses_current_axis_length_for_496_and_400_pixels() -> None:
    dynamic = config(corridor_half_width_ratio=0.04, corridor_half_width_px=0)
    assert effective_corridor_half_width_px(496, dynamic) == pytest.approx(19.84)
    assert effective_corridor_half_width_px(400, dynamic) == pytest.approx(16.0)


def test_effective_width_changes_with_current_or_held_axis() -> None:
    item = PipeCorridorFilter(
        config(
            corridor_half_width_ratio=0.04,
            corridor_half_width_px=0,
            hold_last_valid_ms=250,
        )
    )
    assert item.update_axis((0, 20), (100, 20), (600, 100), now_ms=0)
    first = item.decide_point((50, 23), (600, 100), now_ms=10)
    assert first.effective_half_width_px == pytest.approx(4.0)
    assert item.update_axis((0, 20), (200, 20), (600, 100), now_ms=20)
    changed = item.decide_point((100, 27), (600, 100), now_ms=30)
    held = item.decide_point((100, 27), (600, 100), now_ms=200)
    assert changed.effective_half_width_px == pytest.approx(8.0)
    assert held.effective_half_width_px == pytest.approx(8.0)


def test_fixed_width_is_used_when_ratio_is_not_positive() -> None:
    fixed = config(corridor_half_width_ratio=0, corridor_half_width_px=12.5)
    assert effective_corridor_half_width_px(400, fixed) == pytest.approx(12.5)
    fixed.corridor_half_width_ratio = -0.1
    assert effective_corridor_half_width_px(400, fixed) == pytest.approx(12.5)
