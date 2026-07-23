"""YAML 配置读取、项目路径解析和明确校验。"""

from __future__ import annotations

from pathlib import Path
import math
from typing import Any

import yaml

from core.models import CalibrationConfig, CameraConfig, ShapeConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    """配置文件缺失、格式错误或字段值非法。"""


def resolve_config_path(path: str | Path) -> Path:
    """将相对配置路径稳定解析到项目根目录。"""

    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _read(path: str | Path) -> dict[str, Any]:
    resolved = resolve_config_path(path)
    if not resolved.exists():
        raise ConfigError(f"配置文件不存在: {resolved}")
    try:
        value = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取 YAML {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"配置根节点必须是映射: {resolved}")
    return value


def _required(data: dict[str, Any], keys: tuple[str, ...]) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise ConfigError(f"缺少关键字段: {', '.join(missing)}")


def _apply(data: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    if not overrides:
        return data
    return {**data, **{key: value for key, value in overrides.items() if value is not None}}


def _positive_number(data: dict[str, Any], key: str) -> None:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"字段 {key} 必须为正数")


def load_camera_config(
    path: str | Path = "config/camera.yaml",
    overrides: dict[str, Any] | None = None,
) -> CameraConfig:
    """读取并校验摄像头配置。"""

    data = _apply(_read(path), overrides)
    _required(
        data,
        (
            "device",
            "width",
            "height",
            "fps",
            "fourcc",
            "buffer_size",
            "manual_exposure",
            "exposure",
            "gain",
            "auto_white_balance",
            "brightness",
            "contrast",
            "reconnect_after_failures",
        ),
    )
    for key in ("width", "height", "fps", "buffer_size", "reconnect_after_failures"):
        _positive_number(data, key)
    if not isinstance(data["device"], (str, int)) or isinstance(data["device"], bool):
        raise ConfigError("字段 device 必须为字符串或整数")
    if not isinstance(data["fourcc"], str) or len(data["fourcc"]) != 4:
        raise ConfigError("字段 fourcc 必须是 4 个字符")
    for key in ("exposure", "gain", "brightness", "contrast"):
        value = data[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ConfigError(f"字段 {key} 必须为数值或 null")
    try:
        return CameraConfig(**data)
    except TypeError as exc:
        raise ConfigError(f"摄像头配置包含未知字段: {exc}") from exc


def load_color_config(path: str | Path = "config/colors.yaml") -> dict[str, dict[str, Any]]:
    """读取并完整校验所有 HSV 颜色配置。"""

    data = _read(path)
    for color, value in data.items():
        if not isinstance(value, dict):
            raise ConfigError(f"颜色 {color} 必须为映射")
        _required(value, ("ranges", "min_area", "max_area", "morph_open", "morph_close"))
        ranges = value["ranges"]
        if not isinstance(ranges, list) or not ranges:
            raise ConfigError(f"颜色 {color}.ranges 必须是非空列表")
        for index, hsv_range in enumerate(ranges):
            if not isinstance(hsv_range, dict):
                raise ConfigError(f"颜色 {color}.ranges[{index}] 必须为映射")
            for bound in ("lower", "upper"):
                channels = hsv_range.get(bound)
                if not isinstance(channels, list) or len(channels) != 3:
                    raise ConfigError(f"颜色 {color}.ranges[{index}].{bound} 无效")
                limits = (179, 255, 255)
                if any(
                    isinstance(channel, bool)
                    or not isinstance(channel, int)
                    or not 0 <= channel <= limits[position]
                    for position, channel in enumerate(channels)
                ):
                    raise ConfigError(f"颜色 {color}.ranges[{index}].{bound} 超出 HSV 范围")
        _positive_number(value, "min_area")
        _positive_number(value, "max_area")
        if value["min_area"] > value["max_area"]:
            raise ConfigError(f"颜色 {color} 的 min_area 不能大于 max_area")
        for key in ("morph_open", "morph_close"):
            if not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] < 0:
                raise ConfigError(f"颜色 {color}.{key} 必须为非负整数")
    return data


def load_mission_config(
    path: str | Path = "config/mission.yaml",
    overrides: dict[str, Any] | None = None,
    colors_path: str | Path = "config/colors.yaml",
) -> dict[str, Any]:
    """读取并校验主程序实际使用的任务配置。"""

    data = _apply(_read(path), overrides)
    required = (
        "default_mode",
        "detector",
        "target_color",
        "confirm_frames",
        "lost_frames",
        "max_jump_px",
        "smoothing_alpha",
        "camera_frame_timeout_ms",
        "serial_link_timeout_ms",
        "video_loop",
        "statistics_interval_s",
        "serial_enabled",
        "serial_port",
        "serial_baudrate",
        "heartbeat_hz",
        "vision_result_hz",
        "display",
        "save_debug_frames",
    )
    _required(data, required)
    if data["default_mode"] not in {
        "idle",
        "search",
        "track",
        "calibration",
    }:
        raise ConfigError("default_mode 必须是当前已实现的 idle/search/track/calibration")
    if data["detector"] not in {"color", "shape"}:
        raise ConfigError("detector 必须是 color 或 shape")
    for key in (
        "confirm_frames",
        "lost_frames",
        "max_jump_px",
        "camera_frame_timeout_ms",
        "serial_link_timeout_ms",
        "statistics_interval_s",
        "serial_baudrate",
        "heartbeat_hz",
        "vision_result_hz",
    ):
        _positive_number(data, key)
    alpha = data["smoothing_alpha"]
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 < alpha <= 1:
        raise ConfigError("smoothing_alpha 必须在 (0, 1] 范围内")
    colors = load_color_config(colors_path)
    if data["target_color"] not in colors:
        raise ConfigError(f"目标颜色不存在: {data['target_color']}")
    return data


def load_calibration_config(
    path: str | Path = "config/calibration.yaml",
) -> CalibrationConfig:
    """读取相机标定配置。"""

    data = _read(path)
    _required(
        data,
        (
            "calibrated",
            "image_width",
            "image_height",
            "camera_matrix",
            "distortion_coefficients",
            "reprojection_error",
        ),
    )
    if not isinstance(data["calibrated"], bool):
        raise ConfigError("calibrated 必须为布尔值")
    for key in ("image_width", "image_height"):
        value = data[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"{key} 必须为正整数")
    matrix = data["camera_matrix"]
    distortion = data["distortion_coefficients"]
    if data["calibrated"] or matrix:
        if not (
            isinstance(matrix, list)
            and len(matrix) == 3
            and all(isinstance(row, list) and len(row) == 3 for row in matrix)
        ):
            raise ConfigError("calibrated=true 时 camera_matrix 必须为 3x3 数组")
    if data["calibrated"] or distortion:
        if not isinstance(distortion, list) or len(distortion) not in (4, 5, 8, 12, 14):
            raise ConfigError("畸变参数长度必须为 4、5、8、12 或 14")
    numeric_values = [item for row in matrix for item in row] if matrix else []
    numeric_values.extend(distortion if isinstance(distortion, list) else [])
    if any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        for item in numeric_values
    ):
        raise ConfigError("标定矩阵和畸变参数必须全部为有限数值")
    for key in ("reprojection_error", "rms_error"):
        value = data.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
            or not math.isfinite(value)
        ):
            raise ConfigError(f"{key} 必须为非负数或 null")
    try:
        return CalibrationConfig(**data)
    except TypeError as exc:
        raise ConfigError(f"标定配置包含未知字段: {exc}") from exc


def load_shape_config(path: str | Path = "config/shapes.yaml") -> ShapeConfig:
    """读取并校验传统形状检测参数。"""

    data = _read(path)
    required = (
        "min_area",
        "max_area",
        "canny_low",
        "canny_high",
        "approximation_factor",
        "square_ratio_tolerance",
        "circle_threshold",
    )
    _required(data, required)
    _positive_number(data, "min_area")
    _positive_number(data, "max_area")
    if data["min_area"] > data["max_area"]:
        raise ConfigError("形状 min_area 不能大于 max_area")
    for key in ("canny_low", "canny_high"):
        value = data[key]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
            raise ConfigError(f"{key} 必须为 0..255 整数")
    if data["canny_low"] >= data["canny_high"]:
        raise ConfigError("canny_low 必须小于 canny_high")
    for key in ("approximation_factor", "square_ratio_tolerance", "circle_threshold"):
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
            raise ConfigError(f"{key} 必须在 (0, 1) 范围内")
    try:
        return ShapeConfig(**data)
    except TypeError as exc:
        raise ConfigError(f"形状配置包含未知字段: {exc}") from exc
