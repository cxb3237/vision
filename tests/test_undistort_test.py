"""Unit tests for live undistortion timeout and resolution helpers."""

from __future__ import annotations

import numpy as np
import pytest

from core.models import CalibrationConfig
from tools.undistort_test import (
    build_argument_parser,
    is_new_frame_timed_out,
    validate_calibration_resolution,
)


def _calibration(width: int = 640, height: int = 480) -> CalibrationConfig:
    return CalibrationConfig(False, width, height, [], [], None, None)


def test_recent_new_frame_does_not_timeout_after_long_program_runtime() -> None:
    assert not is_new_frame_timed_out(1000.0, 999.5, 5.0)


def test_continuous_missing_new_frame_times_out() -> None:
    assert is_new_frame_timed_out(16.0, 10.0, 5.0)


def test_new_frame_resets_timeout_reference() -> None:
    previous_new_frame_time = 10.0
    assert is_new_frame_timed_out(16.0, previous_new_frame_time, 5.0)
    previous_new_frame_time = 16.0
    assert not is_new_frame_timed_out(18.0, previous_new_frame_time, 5.0)


@pytest.mark.parametrize("value", [0, -0.1])
def test_nonpositive_frame_timeout_is_rejected(value: float) -> None:
    with pytest.raises(SystemExit):
        build_argument_parser().parse_args(
            ["--input", "x.jpg", "--frame-timeout", str(value)]
        )
    with pytest.raises(ValueError):
        is_new_frame_timed_out(1.0, 0.0, value)


def test_matching_calibration_resolution_passes() -> None:
    validate_calibration_resolution(np.zeros((480, 640, 3), np.uint8), _calibration())


def test_mismatched_calibration_resolution_raises() -> None:
    with pytest.raises(ValueError, match="expected=.*actual="):
        validate_calibration_resolution(np.zeros((240, 320, 3), np.uint8), _calibration())
