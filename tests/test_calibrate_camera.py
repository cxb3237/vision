"""Tests for OpenCV-version-independent reprojection error calculation."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from tools.calibrate_camera import calculate_reprojection_error


def _calculate(monkeypatch, observed: np.ndarray, projected: np.ndarray) -> float:
    monkeypatch.setattr(cv2, "projectPoints", lambda *args: (projected, None))
    count = observed.reshape(-1, 2).shape[0]
    return calculate_reprojection_error(
        [np.zeros((count, 3), dtype=np.float32)],
        [observed],
        (np.zeros((3, 1)),),
        (np.zeros((3, 1)),),
        np.eye(3),
        np.zeros(5),
    )


@pytest.mark.parametrize("shape", [(3, 1, 2), (3, 2)])
def test_supported_observed_shapes_have_zero_error(monkeypatch, shape) -> None:
    points = np.array([[1, 2], [3, 4], [5, 6]], dtype=np.float32)
    assert _calculate(monkeypatch, points.reshape(shape), points.reshape(3, 1, 2)) == 0.0


def test_supported_shapes_produce_the_same_result(monkeypatch) -> None:
    observed = np.array([[1, 2], [3, 4]], dtype=np.float32)
    projected = np.array([[[2, 2]], [[3, 6]]], dtype=np.float32)
    first = _calculate(monkeypatch, observed, projected)
    second = _calculate(monkeypatch, observed.reshape(2, 1, 2), projected)
    assert first == pytest.approx(second)
    assert first == pytest.approx(np.sqrt(5 / 2))


def test_mismatched_point_counts_raise(monkeypatch) -> None:
    with pytest.raises(ValueError, match="do not match"):
        _calculate(
            monkeypatch,
            np.zeros((2, 2), dtype=np.float32),
            np.zeros((3, 1, 2), dtype=np.float32),
        )


def test_empty_data_raise() -> None:
    with pytest.raises(ValueError, match="without image points"):
        calculate_reprojection_error([], [], (), (), np.eye(3), np.zeros(5))
