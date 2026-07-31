"""Shared data models for the steel-ball runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import numpy as np

from core.pipe_corridor import PipeCorridorConfig


class TargetState(IntEnum):
    NONE = 0
    CANDIDATE = 1
    LOCKED = 2
    LOST = 3
    OCCLUDED = 4


@dataclass(slots=True)
class FramePacket:
    frame_id: int
    capture_timestamp: float
    image: np.ndarray


@dataclass(slots=True)
class VisionResult:
    frame_id: int
    capture_timestamp: float
    process_timestamp: float
    found: bool = False
    target_state: int = TargetState.NONE
    target_class: int = 0
    center_x: int = 0
    center_y: int = 0
    error_x_px: int = 0
    error_y_px: int = 0
    bbox_x: int = 0
    bbox_y: int = 0
    bbox_width: int = 0
    bbox_height: int = 0
    area_px: float = 0.0
    distance_mm: int = 0xFFFF
    confidence: int = 0
    processing_delay_ms: int = 0
    image_width: int = 0
    image_height: int = 0
    ball_position_mm: float | None = None
    marker_a_x: int | None = None
    marker_a_y: int | None = None
    marker_b_x: int | None = None
    marker_b_y: int | None = None

    def clear_target(self, state: TargetState = TargetState.NONE) -> None:
        self.found = False
        self.target_state = state
        self.target_class = 0
        self.center_x = 0
        self.center_y = 0
        self.error_x_px = 0
        self.error_y_px = 0
        self.bbox_x = 0
        self.bbox_y = 0
        self.bbox_width = 0
        self.bbox_height = 0
        self.area_px = 0.0
        self.distance_mm = 0xFFFF
        self.confidence = 0
        self.ball_position_mm = None


@dataclass(slots=True)
class CameraConfig:
    device: str | int = 0
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"
    buffer_size: int = 1
    manual_exposure: bool = False
    exposure: float | None = None
    gain: float | None = None
    auto_white_balance: bool = False
    brightness: float | None = None
    contrast: float | None = None
    reconnect_after_failures: int = 20
    v4l2_controls: dict[str, Any] | None = None


@dataclass(slots=True)
class SteelBallNcnnConfig:
    backend: str = "ncnn"
    model_path: str = "models/steel_ball/best_ncnn_model"
    imgsz: int = 416
    conf_threshold: float = 0.40
    iou_threshold: float = 0.60
    max_det: int = 30
    num_threads: int = 4
    target_class: int = 100
    debug_tensor_shapes: bool = False
    pipe_roi: PipeCorridorConfig = field(default_factory=PipeCorridorConfig)
