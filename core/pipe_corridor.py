"""Thread-safe pipe-axis state and geometry-only detection corridor filtering."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Sequence


Point = tuple[float, float]


@dataclass(slots=True)
class PipeCorridorConfig:
    enabled: bool = False
    require_valid_geometry: bool = True
    hold_last_valid_ms: int = 1000
    minimum_axis_length_px: float = 100.0
    corridor_half_width_ratio: float = 0.0
    corridor_half_width_px: float = 0.0
    end_margin_px: float = 15.0
    debug_overlay: bool = False


@dataclass(frozen=True, slots=True)
class PipeAxis:
    left_endpoint: Point
    right_endpoint: Point
    updated_at_ms: float

    @property
    def length_px(self) -> float:
        return math.hypot(
            self.right_endpoint[0] - self.left_endpoint[0],
            self.right_endpoint[1] - self.left_endpoint[1],
        )


@dataclass(frozen=True, slots=True)
class PipeCorridorDecision:
    accepted: bool
    reason: str
    projection_t: float | None
    perpendicular_distance_px: float | None
    axis_length_px: float | None
    center_x: float
    center_y: float
    effective_half_width_px: float | None = None


class PipeCorridorFilter:
    """Hold a recent valid axis and evaluate original-image detection centres."""

    def __init__(self, config: PipeCorridorConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._axis: PipeAxis | None = None

    @staticmethod
    def _now_ms() -> float:
        return time.monotonic() * 1000.0

    @staticmethod
    def _finite_point(point: Sequence[float] | None) -> Point | None:
        if point is None or len(point) != 2:
            return None
        try:
            value = (float(point[0]), float(point[1]))
        except (TypeError, ValueError, OverflowError):
            return None
        return value if all(math.isfinite(item) for item in value) else None

    @staticmethod
    def _image_valid(image_size: tuple[int, int]) -> bool:
        width, height = image_size
        return (
            isinstance(width, int)
            and not isinstance(width, bool)
            and isinstance(height, int)
            and not isinstance(height, bool)
            and width > 0
            and height > 0
        )

    def update_axis(
        self,
        left_endpoint: Sequence[float] | None,
        right_endpoint: Sequence[float] | None,
        image_size: tuple[int, int],
        *,
        now_ms: float | None = None,
    ) -> bool:
        """Update only from a finite in-image axis meeting the configured minimum."""

        left = self._finite_point(left_endpoint)
        right = self._finite_point(right_endpoint)
        if left is None or right is None or not self._image_valid(image_size):
            return False
        width, height = image_size
        if not all(0.0 <= x < width and 0.0 <= y < height for x, y in (left, right)):
            return False
        axis = PipeAxis(left, right, self._now_ms() if now_ms is None else float(now_ms))
        if not math.isfinite(axis.length_px) or axis.length_px < self.config.minimum_axis_length_px:
            return False
        with self._lock:
            self._axis = axis
        return True

    def current_axis(self, *, now_ms: float | None = None) -> tuple[PipeAxis | None, float | None]:
        current = self._now_ms() if now_ms is None else float(now_ms)
        with self._lock:
            axis = self._axis
        if axis is None:
            return None, None
        age = max(0.0, current - axis.updated_at_ms)
        if age > self.config.hold_last_valid_ms:
            return None, age
        return axis, age

    def decide_point(
        self,
        center: Sequence[float],
        image_size: tuple[int, int],
        *,
        now_ms: float | None = None,
    ) -> PipeCorridorDecision:
        point = self._finite_point(center)
        cx = float(point[0]) if point is not None else float("nan")
        cy = float(point[1]) if point is not None else float("nan")
        if point is None or not self._image_valid(image_size):
            return PipeCorridorDecision(False, "geometry_invalid", None, None, None, cx, cy)
        width, height = image_size
        if not 0.0 <= cx < width or not 0.0 <= cy < height:
            return PipeCorridorDecision(False, "geometry_invalid", None, None, None, cx, cy)
        axis, _age = self.current_axis(now_ms=now_ms)
        if axis is None:
            accepted = not self.config.require_valid_geometry
            return PipeCorridorDecision(
                accepted,
                "geometry_missing" if not accepted else "accepted",
                None,
                None,
                None,
                cx,
                cy,
            )
        return evaluate_pipe_corridor(axis, point, self.config)

    def decide_box(
        self,
        detection: dict[str, Any],
        image_size: tuple[int, int],
        *,
        now_ms: float | None = None,
    ) -> PipeCorridorDecision:
        try:
            x1 = float(detection["x1"])
            y1 = float(detection["y1"])
            x2 = float(detection["x2"])
            y2 = float(detection["y2"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return PipeCorridorDecision(
                False, "geometry_invalid", None, None, None, float("nan"), float("nan")
            )
        width, height = image_size
        values = (x1, y1, x2, y2)
        if (
            not self._image_valid(image_size)
            or not all(math.isfinite(value) for value in values)
            or x2 <= x1
            or y2 <= y1
            or x1 < 0.0
            or y1 < 0.0
            or x2 > width
            or y2 > height
        ):
            return PipeCorridorDecision(
                False,
                "geometry_invalid",
                None,
                None,
                None,
                (x1 + x2) / 2.0,
                (y1 + y2) / 2.0,
            )
        return self.decide_point(((x1 + x2) / 2.0, (y1 + y2) / 2.0), image_size, now_ms=now_ms)


def evaluate_pipe_corridor(
    axis: PipeAxis,
    center: Point,
    config: PipeCorridorConfig,
) -> PipeCorridorDecision:
    """Evaluate one point against an already validated axis."""

    cx, cy = center
    x0, y0 = axis.left_endpoint
    x1, y1 = axis.right_endpoint
    values = (
        cx,
        cy,
        x0,
        y0,
        x1,
        y1,
        config.corridor_half_width_ratio,
        config.corridor_half_width_px,
        config.end_margin_px,
    )
    if not all(math.isfinite(float(value)) for value in values):
        return PipeCorridorDecision(False, "geometry_invalid", None, None, None, cx, cy)
    vx = x1 - x0
    vy = y1 - y0
    length_squared = vx * vx + vy * vy
    if length_squared <= 0.0:
        return PipeCorridorDecision(False, "geometry_invalid", None, None, 0.0, cx, cy)
    length = math.sqrt(length_squared)
    if length < config.minimum_axis_length_px:
        return PipeCorridorDecision(False, "axis_too_short", None, None, length, cx, cy)
    effective_half_width = effective_corridor_half_width_px(length, config)
    if effective_half_width is None or config.end_margin_px < 0.0:
        return PipeCorridorDecision(False, "geometry_invalid", None, None, length, cx, cy)
    dx = cx - x0
    dy = cy - y0
    projection = (dx * vx + dy * vy) / length_squared
    distance = abs(vx * dy - vy * dx) / length
    margin_t = config.end_margin_px / length
    if projection < -margin_t:
        reason = "before_left_end"
    elif projection > 1.0 + margin_t:
        reason = "after_right_end"
    elif distance > effective_half_width:
        reason = "outside_corridor"
    else:
        reason = "accepted"
    return PipeCorridorDecision(
        reason == "accepted",
        reason,
        projection,
        distance,
        length,
        cx,
        cy,
        effective_half_width,
    )


def effective_corridor_half_width_px(
    axis_length_px: float,
    config: PipeCorridorConfig,
) -> float | None:
    """Prefer a current-axis ratio and fall back to an explicit fixed pixel width."""

    length = float(axis_length_px)
    if not math.isfinite(length) or length <= 0.0:
        return None
    if math.isfinite(float(config.corridor_half_width_ratio)) and config.corridor_half_width_ratio > 0.0:
        return length * float(config.corridor_half_width_ratio)
    if math.isfinite(float(config.corridor_half_width_px)) and config.corridor_half_width_px > 0.0:
        return float(config.corridor_half_width_px)
    return None
