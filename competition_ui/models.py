"""Configuration model for the competition media web service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml


class CompetitionUIConfigError(ValueError):
    """Raised when ``competition_ui.yaml`` is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class CompetitionUIConfig:
    enabled: bool
    host: str
    port: int
    preview_max_fps: float
    jpeg_quality: int
    preview_max_width: int
    recording_directory: Path
    recording_fps: float
    queue_size: int
    minimum_free_space_mb: int
    filename_prefix: str
    codec_candidates: tuple[str, ...]
    status_poll_interval_ms: int
    project_root: Path
    source_path: Path


def _mapping(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise CompetitionUIConfigError(f"{field} 必须是映射")
    return value


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CompetitionUIConfigError(f"{field} 必须在 {minimum}..{maximum} 范围内")
    return value


def _number(value: object, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CompetitionUIConfigError(f"{field} 必须是数值")
    result = float(value)
    if not minimum <= result <= maximum:
        raise CompetitionUIConfigError(f"{field} 必须在 {minimum}..{maximum} 范围内")
    return result


def load_competition_ui_config(
    path: str | Path = "config/competition_ui.yaml",
    *,
    project_root: str | Path | None = None,
) -> CompetitionUIConfig:
    root = Path(project_root or Path.cwd()).resolve()
    source = Path(path)
    source = source if source.is_absolute() else root / source
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CompetitionUIConfigError(f"无法读取比赛网站配置 {source}: {exc}") from exc
    document = _mapping(document, "competition_ui")
    enabled = document.get("enabled")
    if not isinstance(enabled, bool):
        raise CompetitionUIConfigError("enabled 必须是布尔值")
    server = _mapping(document.get("server"), "server")
    preview = _mapping(document.get("preview"), "preview")
    recording = _mapping(document.get("recording"), "recording")
    ui = _mapping(document.get("ui"), "ui")

    host = server.get("host")
    if host != "127.0.0.1":
        raise CompetitionUIConfigError("server.host 只允许 127.0.0.1")
    port = _integer(server.get("port"), "server.port", 1, 65535)
    preview_max_fps = _number(preview.get("max_fps"), "preview.max_fps", 0.1, 30)
    jpeg_quality = _integer(preview.get("jpeg_quality"), "preview.jpeg_quality", 1, 100)
    preview_max_width = _integer(preview.get("max_width"), "preview.max_width", 160, 4096)
    recording_fps = _number(recording.get("fps"), "recording.fps", 1, 60)
    queue_size = _integer(recording.get("queue_size"), "recording.queue_size", 1, 64)
    minimum_free_space_mb = _integer(
        recording.get("minimum_free_space_mb"),
        "recording.minimum_free_space_mb",
        1,
        1_048_576,
    )
    status_poll_interval_ms = _integer(
        ui.get("status_poll_interval_ms"), "ui.status_poll_interval_ms", 100, 10_000
    )

    prefix = recording.get("filename_prefix")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
        raise CompetitionUIConfigError("recording.filename_prefix 只能包含字母、数字、_ 和 -")
    codecs = recording.get("codec_candidates")
    if (
        not isinstance(codecs, list)
        or not codecs
        or any(not isinstance(codec, str) or len(codec) != 4 for codec in codecs)
    ):
        raise CompetitionUIConfigError("recording.codec_candidates 必须是非空四字符编码列表")

    relative_directory = Path(recording.get("directory", ""))
    if relative_directory.is_absolute() or ".." in relative_directory.parts:
        raise CompetitionUIConfigError("recording.directory 必须是工程内相对路径")
    directory = (root / relative_directory).resolve()
    try:
        directory.relative_to(root)
    except ValueError as exc:
        raise CompetitionUIConfigError("recording.directory 超出工程目录") from exc

    return CompetitionUIConfig(
        enabled=enabled,
        host=host,
        port=port,
        preview_max_fps=preview_max_fps,
        jpeg_quality=jpeg_quality,
        preview_max_width=preview_max_width,
        recording_directory=directory,
        recording_fps=recording_fps,
        queue_size=queue_size,
        minimum_free_space_mb=minimum_free_space_mb,
        filename_prefix=prefix,
        codec_candidates=tuple(codecs),
        status_poll_interval_ms=status_poll_interval_ms,
        project_root=root,
        source_path=source.resolve(),
    )
