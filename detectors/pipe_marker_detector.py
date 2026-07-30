"""Single-frame color marker detection and pipe-coordinate projection."""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


Point = tuple[int, int]


class PipeMarkerDetector:
    """Detect the blue A marker and green B marker in one BGR frame."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.enabled = bool(config["enabled"])
        self.marker_a_config = config["marker_a"]
        self.marker_b_config = config["marker_b"]

    @staticmethod
    def _detect_largest(hsv: np.ndarray, lower: list[int], upper: list[int]) -> Point | None:
        mask = cv2.inRange(
            hsv,
            np.asarray(lower, dtype=np.uint8),
            np.asarray(upper, dtype=np.uint8),
        )
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        contour = max(contours, key=cv2.contourArea)
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            return None
        return (
            int(round(moments["m10"] / moments["m00"])),
            int(round(moments["m01"] / moments["m00"])),
        )

    def detect(self, frame: np.ndarray) -> tuple[Point | None, Point | None]:
        if not self.enabled:
            return None, None
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        marker_a = self._detect_largest(
            hsv,
            self.marker_a_config["hsv_lower"],
            self.marker_a_config["hsv_upper"],
        )
        marker_b = self._detect_largest(
            hsv,
            self.marker_b_config["hsv_lower"],
            self.marker_b_config["hsv_upper"],
        )
        return marker_a, marker_b


def compute_ball_position_mm(
    marker_a: tuple[int | float, int | float],
    marker_b: tuple[int | float, int | float],
    ball_center: tuple[int | float, int | float],
    marker_a_mm: float,
    marker_b_mm: float,
) -> float | None:
    """Project the ball center onto AB and convert it to pipe millimetres."""

    ax, ay = marker_a
    bx, by = marker_b
    cx, cy = ball_center
    ab_x = bx - ax
    ab_y = by - ay
    denominator = ab_x * ab_x + ab_y * ab_y
    if denominator == 0:
        return None
    t = ((cx - ax) * ab_x + (cy - ay) * ab_y) / denominator
    position_mm = marker_a_mm + t * (marker_b_mm - marker_a_mm)
    return max(-125.0, min(125.0, float(position_mm)))
