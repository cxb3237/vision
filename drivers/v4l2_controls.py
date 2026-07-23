"""通过 Linux ``v4l2-ctl`` 独立设置和读取摄像头控制参数。"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from typing import Any


LOG = logging.getLogger(__name__)
INSTALL_HINT = "未找到 v4l2-ctl；请安装：sudo apt install v4l-utils"
RESERVED_NAMES = {"enabled", "strict"}


class V4L2ControlError(RuntimeError):
    """严格模式下 V4L2 控制不可用或设置失败。"""


def resolve_video_device(device: str | int) -> str:
    """把数字设备编号解析为 Linux ``/dev/videoN`` 路径。"""

    if isinstance(device, bool):
        raise ValueError("摄像头设备不能是布尔值")
    if isinstance(device, int):
        if device < 0:
            raise ValueError("摄像头设备编号不能为负数")
        return f"/dev/video{device}"
    text = str(device).strip()
    if text.isdigit():
        return f"/dev/video{int(text)}"
    return text


def is_v4l2_available() -> bool:
    """仅当运行于 Linux 且 ``v4l2-ctl`` 在 PATH 中时返回真。"""

    return platform.system() == "Linux" and shutil.which("v4l2-ctl") is not None


def _requested_controls(controls: dict[str, Any]) -> dict[str, int]:
    return {
        name: int(value)
        for name, value in controls.items()
        if name not in RESERVED_NAMES and value is not None
    }


def _skipped_results(
    requested: dict[str, int],
    reason: str,
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "requested": value,
            "success": False,
            "skipped": True,
            "error": reason,
        }
        for name, value in requested.items()
    }


def apply_v4l2_controls(
    device: str | int,
    controls: dict,
    strict: bool = False,
) -> dict[str, dict[str, Any]]:
    """逐项应用控制，返回每项的请求值、成功状态和错误原因。"""

    requested = _requested_controls(controls)
    if not requested:
        return {}
    if platform.system() != "Linux":
        reason = f"当前平台 {platform.system()} 不是 Linux，已跳过 V4L2 控制"
        LOG.info(reason)
        return _skipped_results(requested, reason)
    executable = shutil.which("v4l2-ctl")
    if executable is None:
        LOG.warning(INSTALL_HINT)
        if strict:
            raise V4L2ControlError(INSTALL_HINT)
        return _skipped_results(requested, INSTALL_HINT)

    resolved_device = resolve_video_device(device)
    results: dict[str, dict[str, Any]] = {}
    for name, value in requested.items():
        command = [
            executable,
            "--device",
            resolved_device,
            "--set-ctrl",
            f"{name}={value}",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                shell=False,
            )
            success = completed.returncode == 0
            error = None if success else (completed.stderr.strip() or completed.stdout.strip())
        except OSError as exc:
            success = False
            error = str(exc)
        results[name] = {
            "requested": value,
            "success": success,
            "skipped": False,
            "error": error,
        }
        if not success:
            message = f"设置 V4L2 控制 {name}={value} 失败: {error or '未知错误'}"
            if strict:
                raise V4L2ControlError(message)
            LOG.warning(message)
    return results


def read_v4l2_controls(
    device: str | int,
    names: list[str],
) -> dict[str, int | None]:
    """逐项读取 V4L2 控制的当前实际整数值。"""

    unique_names = list(dict.fromkeys(name for name in names if name not in RESERVED_NAMES))
    values = {name: None for name in unique_names}
    if not unique_names:
        return values
    if platform.system() != "Linux":
        LOG.info("当前平台 %s 不是 Linux，已跳过读取 V4L2 控制", platform.system())
        return values
    executable = shutil.which("v4l2-ctl")
    if executable is None:
        LOG.warning(INSTALL_HINT)
        return values

    resolved_device = resolve_video_device(device)
    for name in unique_names:
        try:
            completed = subprocess.run(
                [
                    executable,
                    "--device",
                    resolved_device,
                    "--get-ctrl",
                    name,
                ],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
            )
        except OSError as exc:
            LOG.warning("读取 V4L2 控制 %s 失败: %s", name, exc)
            continue
        if completed.returncode != 0:
            error = completed.stderr.strip() or completed.stdout.strip() or "未知错误"
            LOG.warning("读取 V4L2 控制 %s 失败: %s", name, error)
            continue
        match = re.search(rf"(?:^|\n)\s*{re.escape(name)}\s*:\s*(-?\d+)", completed.stdout)
        if match is None:
            LOG.warning("无法解析 V4L2 控制 %s 的输出: %s", name, completed.stdout.strip())
            continue
        values[name] = int(match.group(1))
    return values
