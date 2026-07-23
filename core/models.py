"""全工程共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import numpy as np


class TargetState(IntEnum):
    """目标检测与跟踪状态。"""

    NONE = 0
    CANDIDATE = 1
    LOCKED = 2
    LOST = 3
    OCCLUDED = 4


class ColorClass(IntEnum):
    """协议与检测器共用的稳定颜色类别编号。"""

    UNKNOWN = 0
    RED = 1
    GREEN = 2
    BLUE = 3
    YELLOW = 4
    BLACK = 5
    WHITE = 6

    @classmethod
    def from_name(cls, name: str) -> "ColorClass":
        """按配置名称返回稳定类别，不支持的名称返回 UNKNOWN。"""

        return cls.__members__.get(name.upper(), cls.UNKNOWN)


@dataclass(slots=True)
class FramePacket:
    """带采集序号和时间戳的一帧图像。"""

    frame_id: int
    capture_timestamp: float
    image: np.ndarray


@dataclass(slots=True)
class VisionResult:
    """检测器和跟踪器之间传递的统一结果。"""

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

    def clear_target(self, state: TargetState = TargetState.NONE) -> None:
        """清空所有目标字段，同时保留帧和耗时信息。"""

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


@dataclass(slots=True)
class CameraConfig:
    """摄像头采集配置；硬件可选属性用 ``None`` 表示不设置。"""

    device: str | int = 0
    width: int = 640
    height: int = 480
    fps: int = 30
    fourcc: str = "MJPG"
    buffer_size: int = 1
    manual_exposure: bool = False
    exposure: float | None = None
    gain: float | None = None
    auto_white_balance: bool = True
    brightness: float | None = None
    contrast: float | None = None
    reconnect_after_failures: int = 20
    v4l2_controls: dict[str, Any] | None = None


@dataclass(slots=True)
class DetectorConfig:
    """颜色检测和时序跟踪的组合配置。"""

    min_area: float = 300.0
    max_area: float = 300_000.0
    morph_open: int = 3
    morph_close: int = 5
    confirm_frames: int = 3
    lost_frames: int = 5
    max_jump_px: float = 160.0
    smoothing_alpha: float = 0.45

    @classmethod
    def from_color_config(
        cls,
        color_config: dict[str, Any],
        **timing_overrides: Any,
    ) -> "DetectorConfig":
        """合并颜色 YAML 参数与任务时序参数，避免丢失面积和形态学配置。"""

        values: dict[str, Any] = {
            "min_area": color_config["min_area"],
            "max_area": color_config["max_area"],
            "morph_open": color_config["morph_open"],
            "morph_close": color_config["morph_close"],
        }
        values.update({key: value for key, value in timing_overrides.items() if value is not None})
        return cls(**values)


@dataclass(slots=True)
class CalibrationConfig:
    """相机内参、畸变参数与标定质量。"""

    calibrated: bool = False
    image_width: int = 640
    image_height: int = 480
    camera_matrix: list[list[float]] = field(default_factory=list)
    distortion_coefficients: list[float] = field(default_factory=list)
    reprojection_error: float | None = None
    rms_error: float | None = None


@dataclass(slots=True)
class ShapeConfig:
    """传统形状检测器的现场可调参数。"""

    min_area: float = 300.0
    max_area: float = 300_000.0
    canny_low: int = 60
    canny_high: int = 140
    approximation_factor: float = 0.04
    square_ratio_tolerance: float = 0.15
    circle_threshold: float = 0.70


@dataclass(slots=True)
class DigitConfig:
    """单个印刷数字检测的分组配置。"""

    roi: dict[str, Any]
    preprocess: dict[str, Any]
    candidate: dict[str, Any]
    normalization: dict[str, Any]
    matching: dict[str, Any]
    tracking: dict[str, Any]


@dataclass(slots=True)
class SteelBallConfig:
    """直径已知钢球的传统视觉检测参数。"""

    roi: list[int] | None = None
    known_diameter_mm: float = 10.0
    target_class: int = 100
    clahe_enabled: bool = True
    clahe_clip_limit: float = 2.0
    clahe_tile_grid_size: int = 8
    gaussian_kernel: int = 5
    threshold_mode: str = "fixed"
    threshold: int = 120
    adaptive_block_size: int = 21
    adaptive_c: float = 5.0
    invert: bool = False
    morph_open: int = 3
    morph_close: int = 5
    min_diameter_px: float = 12.0
    max_diameter_px: float = 120.0
    min_area_px: float = 80.0
    max_area_px: float = 12_000.0
    min_circularity: float = 0.72
    min_aspect_ratio: float = 0.75
    max_aspect_ratio: float = 1.33
    confirm_frames: int = 3
    lost_frames: int = 5
    max_jump_px: float = 160.0
    hough_enabled: bool = False
    hough_dp: float = 1.2
    hough_min_dist: float = 20.0
    hough_param1: float = 100.0
    hough_param2: float = 20.0
    hough_min_radius: int = 5
    hough_max_radius: int = 80
