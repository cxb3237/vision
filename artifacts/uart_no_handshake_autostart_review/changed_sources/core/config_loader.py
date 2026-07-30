"""Strict YAML loaders for the production steel-ball runtime."""

from __future__ import annotations

from pathlib import Path
import math
from typing import Any

import yaml

from core.models import CameraConfig, SteelBallNcnnConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(ValueError):
    pass


def resolve_config_path(path: str | Path) -> Path:
    source = Path(path)
    return source if source.is_absolute() else PROJECT_ROOT / source


def _read(path: str | Path) -> dict[str, Any]:
    source = resolve_config_path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {source}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 无效: {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置根节点必须是映射: {source}")
    return data


def _reject_unknown(data: dict[str, Any], allowed: set[str], prefix: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{prefix} 包含未知字段: {', '.join(unknown)}")


def load_camera_config(path: str | Path = "config/camera.yaml", overrides: dict[str, Any] | None = None) -> CameraConfig:
    data = _read(path)
    if overrides:
        data.update(overrides)
    allowed = set(CameraConfig.__dataclass_fields__)
    _reject_unknown(data, allowed, "camera")
    try:
        config = CameraConfig(**data)
    except TypeError as exc:
        raise ConfigError(f"camera 字段无效: {exc}") from exc
    for name in ("width", "height", "fps", "buffer_size", "reconnect_after_failures"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"camera.{name} 必须为正整数")
    if isinstance(config.device, bool) or not isinstance(config.device, (str, int)):
        raise ConfigError("camera.device 必须是编号或设备路径")
    if not isinstance(config.fourcc, str) or len(config.fourcc) != 4:
        raise ConfigError("camera.fourcc 必须是 4 个字符")
    for name in ("manual_exposure", "auto_white_balance"):
        if not isinstance(getattr(config, name), bool):
            raise ConfigError(f"camera.{name} 必须为布尔值")
    if config.v4l2_controls is not None:
        if not isinstance(config.v4l2_controls, dict):
            raise ConfigError("camera.v4l2_controls 必须是映射")
        for name in ("enabled", "strict"):
            value = config.v4l2_controls.get(name, False)
            if not isinstance(value, bool):
                raise ConfigError(f"camera.v4l2_controls.{name} 必须为布尔值")
        for name, value in config.v4l2_controls.items():
            if name in {"enabled", "strict"} or value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"camera.v4l2_controls.{name} 必须为整数或 null")
    return config


def load_mission_config(path: str | Path = "config/mission.yaml", overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    data = _read(path)
    if overrides:
        data.update(overrides)
    runtime_defaults = {
        "confirm_frames": 3,
        "lost_frames": 5,
        "max_jump_px": 160.0,
        "smoothing_alpha": 0.45,
        "camera_online_timeout_s": 1.0,
    }
    _reject_unknown(data, {"default_mode", "ball_uart", *runtime_defaults}, "mission")
    if data.get("default_mode") != "track":
        raise ConfigError("mission.default_mode 必须为 track")
    profile = data.get("ball_uart")
    if not isinstance(profile, dict):
        raise ConfigError("mission.ball_uart 必须是映射")
    defaults: dict[str, Any] = {
        "enabled": True,
        "port": "/dev/ttyAMA0",
        "baudrate": 9600,
        "timeout_s": 0.02,
        "write_timeout_s": 0.05,
        "reconnect_interval_s": 1.0,
        "send_rate_hz": 50.0,
        "line_ending": "\r\n",
        "wait_ready": False,
        "debug_position_interval_s": 1.0,
        "statistics_interval_s": 1.0,
        "calibrated": False,
        "left_endpoint_px": 72,
        "right_endpoint_px": 568,
        "servo_side": "right",
    }
    _reject_unknown(profile, set(defaults), "mission.ball_uart")
    for name, value in defaults.items():
        profile.setdefault(name, value)
    for name in ("enabled", "wait_ready", "calibrated"):
        if not isinstance(profile[name], bool):
            raise ConfigError(f"mission.ball_uart.{name} 必须为布尔值")
    if profile["wait_ready"]:
        raise ConfigError("mission.ball_uart.wait_ready 必须为 false（无握手模式）")
    if not isinstance(profile["port"], str) or not profile["port"].strip():
        raise ConfigError("mission.ball_uart.port 不能为空")
    for name in (
        "baudrate", "timeout_s", "write_timeout_s", "reconnect_interval_s",
        "send_rate_hz", "debug_position_interval_s", "statistics_interval_s",
    ):
        value = profile[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"mission.ball_uart.{name} 必须为正数")
    for name in ("left_endpoint_px", "right_endpoint_px"):
        if isinstance(profile[name], bool) or not isinstance(profile[name], (int, float)):
            raise ConfigError(f"mission.ball_uart.{name} 必须为数值")
    if not isinstance(profile["servo_side"], str):
        raise ConfigError("mission.ball_uart.servo_side 必须为字符串")
    if profile["line_ending"] != "\r\n":
        raise ConfigError("mission.ball_uart.line_ending 必须为 CRLF")
    for name, value in runtime_defaults.items():
        data.setdefault(name, value)
    for name in ("confirm_frames", "lost_frames"):
        if isinstance(data[name], bool) or not isinstance(data[name], int) or data[name] <= 0:
            raise ConfigError(f"mission.{name} 必须为正整数")
    if data["max_jump_px"] <= 0:
        raise ConfigError("mission.max_jump_px 必须为正数")
    if not 0 < data["smoothing_alpha"] <= 1:
        raise ConfigError("mission.smoothing_alpha 必须在 (0, 1] 范围内")
    timeout = data["camera_online_timeout_s"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ConfigError("mission.camera_online_timeout_s 必须为有限正数")
    return data


def load_steel_ball_ncnn_config(path: str | Path = "config/steel_ball_ncnn.yaml") -> SteelBallNcnnConfig:
    data = _read(path)
    allowed = set(SteelBallNcnnConfig.__dataclass_fields__)
    _reject_unknown(data, allowed, "steel_ball_ncnn")
    try:
        config = SteelBallNcnnConfig(**data)
    except TypeError as exc:
        raise ConfigError(f"steel_ball_ncnn 字段无效: {exc}") from exc
    if config.backend != "ncnn":
        raise ConfigError("steel_ball_ncnn.backend 必须为 ncnn")
    model_path = Path(config.model_path)
    if not model_path.is_absolute():
        model_path = PROJECT_ROOT / model_path
    config.model_path = str(model_path.resolve())
    if isinstance(config.imgsz, bool) or not isinstance(config.imgsz, int) or config.imgsz <= 0:
        raise ConfigError("steel_ball_ncnn.imgsz 必须为正整数")
    for name in ("conf_threshold", "iou_threshold"):
        value = getattr(config, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ConfigError(f"steel_ball_ncnn.{name} 必须在 0..1 范围内")
    if isinstance(config.max_det, bool) or not isinstance(config.max_det, int) or not 1 <= config.max_det <= 100:
        raise ConfigError("steel_ball_ncnn.max_det 必须在 1..100 范围内")
    if isinstance(config.num_threads, bool) or not isinstance(config.num_threads, int) or not 1 <= config.num_threads <= 4:
        raise ConfigError("steel_ball_ncnn.num_threads 必须在 1..4 范围内")
    if isinstance(config.target_class, bool) or not isinstance(config.target_class, int):
        raise ConfigError("steel_ball_ncnn.target_class 必须为整数")
    if not isinstance(config.debug_tensor_shapes, bool):
        raise ConfigError("steel_ball_ncnn.debug_tensor_shapes 必须为布尔值")
    return config


def load_pipe_mapping_config(path: str | Path = "config/pipe_mapping.yaml") -> dict[str, Any]:
    data = _read(path)
    for name in ("enabled", "marker_a", "marker_b"):
        if name not in data:
            raise ConfigError(f"pipe_mapping 缺少必要字段: {name}")
    for marker_name in ("marker_a", "marker_b"):
        marker = data[marker_name]
        if not isinstance(marker, dict):
            raise ConfigError(f"pipe_mapping.{marker_name} 必须是映射")
        for name in ("name", "hsv_lower", "hsv_upper", "position_mm"):
            if name not in marker:
                raise ConfigError(f"pipe_mapping.{marker_name} 缺少必要字段: {name}")
        for name in ("hsv_lower", "hsv_upper"):
            if not isinstance(marker[name], list) or len(marker[name]) != 3:
                raise ConfigError(f"pipe_mapping.{marker_name}.{name} 必须包含 3 个值")
    return data
