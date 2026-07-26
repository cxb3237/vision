"""触摸界面的配置、命令和API数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
import uuid

import yaml


class TouchUIConfigError(ValueError):
    """触摸界面配置字段无效。"""


class CommandStatus(str, Enum):
    QUEUED = "QUEUED"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


class CommandType(str, Enum):
    SET_CAMERA_CONTROL = "SET_CAMERA_CONTROL"
    SELECT_DETECTOR = "SELECT_DETECTOR"
    SAVE_RUNTIME = "SAVE_RUNTIME"
    RESTORE_LAST_GOOD = "RESTORE_LAST_GOOD"
    RESTORE_BASELINE = "RESTORE_BASELINE"
    ENTER_COMPETITION = "ENTER_COMPETITION"
    EXIT_COMPETITION = "EXIT_COMPETITION"


@dataclass(slots=True, frozen=True)
class RuntimeCommand:
    command_id: str
    command_type: CommandType
    payload: dict[str, Any]

    @classmethod
    def create(
        cls,
        command_type: CommandType | str,
        payload: dict[str, Any] | None = None,
    ) -> "RuntimeCommand":
        return cls(
            uuid.uuid4().hex,
            CommandType(command_type),
            dict(payload or {}),
        )

    @property
    def coalesce_key(self) -> str | None:
        if self.command_type == CommandType.SET_CAMERA_CONTROL:
            return f"camera:{self.payload.get('name', '')}"
        return None


@dataclass(slots=True, frozen=True)
class TouchUIConfig:
    host: str
    port: int
    preview_max_fps: float
    jpeg_quality: int
    preview_max_width: int
    startup_detector: str
    restore_runtime_overrides: bool
    startup_competition_mode: bool
    status_poll_interval_ms: int
    parameter_debounce_ms: int
    exit_competition_hold_ms: int
    runtime_directory: Path
    camera_override_file: Path
    ui_state_file: Path
    backup_directory: Path
    backup_limit: int
    source_path: Path


def _mapping(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise TouchUIConfigError(f"touch_ui.{name} 必须为映射")
    return value


def _required(group: dict[str, Any], group_name: str, names: tuple[str, ...]) -> None:
    missing = [name for name in names if name not in group]
    if missing:
        raise TouchUIConfigError(
            f"touch_ui.{group_name} 缺少字段: {', '.join(missing)}"
        )


def _integer_range(
    group: dict[str, Any], group_name: str, name: str, minimum: int, maximum: int
) -> int:
    value = group[name]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TouchUIConfigError(
            f"touch_ui.{group_name}.{name} 必须在 {minimum}..{maximum} 范围内"
        )
    return value


def _path(project_root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise TouchUIConfigError(f"{field} 必须为项目相对路径")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise TouchUIConfigError(f"{field} 必须为项目内相对路径")
    resolved = (project_root / candidate).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise TouchUIConfigError(f"{field} 超出项目目录") from exc
    return resolved


def load_touch_ui_config(
    path: str | Path = "config/touch_ui.yaml",
    *,
    project_root: str | Path | None = None,
    create_runtime: bool = True,
) -> TouchUIConfig:
    """安全读取并验证触摸界面配置，不执行任何硬件探测。"""

    root = Path(project_root).resolve() if project_root is not None else Path.cwd().resolve()
    source = Path(path)
    if not source.is_absolute():
        source = root / source
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TouchUIConfigError(f"触摸界面配置不存在: {source}") from exc
    except yaml.YAMLError as exc:
        raise TouchUIConfigError(f"触摸界面YAML无效: {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TouchUIConfigError("触摸界面配置根节点必须为映射")

    server = _mapping(raw, "server")
    preview = _mapping(raw, "preview")
    startup = _mapping(raw, "startup")
    ui = _mapping(raw, "ui")
    runtime = _mapping(raw, "runtime")
    _required(server, "server", ("host", "port"))
    _required(preview, "preview", ("max_fps", "jpeg_quality", "max_width"))
    _required(
        startup,
        "startup",
        ("detector", "restore_runtime_overrides", "competition_mode"),
    )
    _required(
        ui,
        "ui",
        ("status_poll_interval_ms", "parameter_debounce_ms", "exit_competition_hold_ms"),
    )
    _required(
        runtime,
        "runtime",
        ("directory", "camera_override_file", "ui_state_file", "backup_directory"),
    )
    if not isinstance(server["host"], str) or not server["host"].strip():
        raise TouchUIConfigError("touch_ui.server.host 必须为非空字符串")
    port = _integer_range(server, "server", "port", 1, 65535)
    max_fps = preview["max_fps"]
    if isinstance(max_fps, bool) or not isinstance(max_fps, (int, float)) or not 0 < max_fps <= 60:
        raise TouchUIConfigError("touch_ui.preview.max_fps 必须在 (0, 60] 范围内")
    jpeg_quality = _integer_range(preview, "preview", "jpeg_quality", 1, 100)
    max_width = _integer_range(preview, "preview", "max_width", 160, 4096)
    detector = startup["detector"]
    if detector not in {"color", "shape", "steel_ball", "digit"}:
        raise TouchUIConfigError("touch_ui.startup.detector 无效")
    for name in ("restore_runtime_overrides", "competition_mode"):
        if not isinstance(startup[name], bool):
            raise TouchUIConfigError(f"touch_ui.startup.{name} 必须为布尔值")
    poll = _integer_range(ui, "ui", "status_poll_interval_ms", 100, 10000)
    debounce = _integer_range(ui, "ui", "parameter_debounce_ms", 0, 5000)
    hold = _integer_range(ui, "ui", "exit_competition_hold_ms", 1000, 10000)
    backup_limit = runtime.get("backup_limit", 5)
    if isinstance(backup_limit, bool) or not isinstance(backup_limit, int) or not 1 <= backup_limit <= 100:
        raise TouchUIConfigError("touch_ui.runtime.backup_limit 必须在 1..100 范围内")

    runtime_directory = _path(root, runtime["directory"], "touch_ui.runtime.directory")
    camera_override = _path(
        root, runtime["camera_override_file"], "touch_ui.runtime.camera_override_file"
    )
    ui_state = _path(root, runtime["ui_state_file"], "touch_ui.runtime.ui_state_file")
    backup_directory = _path(
        root, runtime["backup_directory"], "touch_ui.runtime.backup_directory"
    )
    if create_runtime:
        runtime_directory.mkdir(parents=True, exist_ok=True)
        backup_directory.mkdir(parents=True, exist_ok=True)

    return TouchUIConfig(
        host=server["host"].strip(),
        port=port,
        preview_max_fps=float(max_fps),
        jpeg_quality=jpeg_quality,
        preview_max_width=max_width,
        startup_detector=detector,
        restore_runtime_overrides=startup["restore_runtime_overrides"],
        startup_competition_mode=startup["competition_mode"],
        status_poll_interval_ms=poll,
        parameter_debounce_ms=debounce,
        exit_competition_hold_ms=hold,
        runtime_directory=runtime_directory,
        camera_override_file=camera_override,
        ui_state_file=ui_state,
        backup_directory=backup_directory,
        backup_limit=backup_limit,
        source_path=source.resolve(),
    )
