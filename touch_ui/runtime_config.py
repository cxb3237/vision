"""现场参数的原子保存、备份与恢复。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

import yaml

from touch_ui.models import TouchUIConfig


LOG = logging.getLogger(__name__)


class RuntimeConfigStore:
    def __init__(self, config: TouchUIConfig) -> None:
        self.config = config
        config.runtime_directory.mkdir(parents=True, exist_ok=True)
        config.backup_directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_mapping(path: Path) -> dict[str, Any]:
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"读取现场配置失败 {path}: {exc}") from exc
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"现场配置必须为映射: {path}")
        return value

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _backup(self, path: Path) -> None:
        if not path.exists():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        destination = self.config.backup_directory / f"{path.stem}-{stamp}-{time.time_ns()}.yaml"
        shutil.copy2(path, destination)
        backups = sorted(
            self.config.backup_directory.glob(f"{path.stem}-*.yaml"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        for old in backups[self.config.backup_limit :]:
            old.unlink(missing_ok=True)

    def save_camera_override(self, controls: dict[str, int]) -> None:
        self._backup(self.config.camera_override_file)
        self._atomic_write(
            self.config.camera_override_file,
            {"v4l2_controls": dict(controls), "saved_wall_time": time.time()},
        )
        LOG.info("已原子保存现场摄像头参数: %s", self.config.camera_override_file)

    def load_camera_override(self) -> dict[str, int]:
        data = self._read_mapping(self.config.camera_override_file)
        controls = data.get("v4l2_controls", {})
        if not isinstance(controls, dict):
            raise ValueError("runtime camera override的v4l2_controls必须为映射")
        result: dict[str, int] = {}
        for name, value in controls.items():
            if not isinstance(name, str) or isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"runtime camera override字段无效: {name}")
            result[name] = value
        return result

    def save_ui_state(self, competition_mode: bool, detector: str) -> None:
        self._backup(self.config.ui_state_file)
        self._atomic_write(
            self.config.ui_state_file,
            {
                "competition_mode": bool(competition_mode),
                "detector": str(detector),
                "saved_wall_time": time.time(),
            },
        )

    def load_ui_state(self) -> dict[str, Any]:
        return self._read_mapping(self.config.ui_state_file)

    def restore_baseline(self) -> bool:
        path = self.config.camera_override_file
        if not path.exists():
            return False
        self._backup(path)
        path.unlink()
        LOG.info("已停用现场override，基础YAML未修改")
        return True
