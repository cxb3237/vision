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
AUTOMATIC_CONTROL_ORDER = (
    "white_balance_automatic",
    "exposure_auto",
    "focus_auto",
)
MANUAL_CONTROL_ORDER = (
    "white_balance_temperature",
    "exposure_absolute",
    "focus_absolute",
)


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
    available = {
        name: int(value)
        for name, value in controls.items()
        if name not in RESERVED_NAMES and value is not None
    }
    names = list(available)

    def place(first: str, second: str) -> None:
        if first not in names or second not in names:
            return
        names.remove(first)
        names.insert(names.index(second), first)

    if "white_balance_automatic" in available:
        if available["white_balance_automatic"] == 0:
            place("white_balance_automatic", "white_balance_temperature")
        else:
            place("white_balance_temperature", "white_balance_automatic")
    if "exposure_auto" in available:
        if available["exposure_auto"] == 1:
            place("exposure_auto", "exposure_absolute")
        else:
            place("exposure_absolute", "exposure_auto")
    if "focus_auto" in available:
        if available["focus_auto"] == 0:
            place("focus_auto", "focus_absolute")
        else:
            place("focus_absolute", "focus_auto")
    return {name: available[name] for name in names}


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


def query_v4l2_control_info(
    device: str | int,
    names: list[str],
) -> dict[str, dict[str, Any]]:
    """读取控制范围和实际值；非Linux平台返回明确的不支持状态。"""

    unique_names = list(dict.fromkeys(name for name in names if name not in RESERVED_NAMES))
    result = {
        name: {
            "name": name,
            "type": None,
            "supported": False,
            "minimum": None,
            "maximum": None,
            "step": None,
            "default": None,
            "actual": None,
            "choices": [],
            "flags": [],
            "read_only": False,
            "writable": False,
            "error": None,
        }
        for name in unique_names
    }
    if not unique_names:
        return result
    if platform.system() != "Linux":
        reason = f"当前平台 {platform.system()} 不支持V4L2"
        for info in result.values():
            info["error"] = reason
        return result
    executable = shutil.which("v4l2-ctl")
    if executable is None:
        for info in result.values():
            info["error"] = INSTALL_HINT
        return result
    try:
        completed = subprocess.run(
            [executable, "--device", resolve_video_device(device), "--list-ctrls-menus"],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    except OSError as exc:
        for info in result.values():
            info["error"] = str(exc)
        return result
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or "读取V4L2控制列表失败"
        for info in result.values():
            info["error"] = error
        return result

    header_pattern = re.compile(
        r"^\s*(?P<name>[a-zA-Z0-9_]+)\s+"
        r"(?:0x[0-9a-fA-F]+\s+)?\((?P<type>[^)]+)\)\s*:\s*(?P<body>.*)$"
    )
    value_pattern = re.compile(
        r"\b(?P<key>min|max|step|default|value)=\s*(?P<value>-?\d+)"
    )
    choice_pattern = re.compile(r"^\s*(?P<value>-?\d+)\s*:\s*(?P<label>.+?)\s*$")
    flags_pattern = re.compile(r"\bflags=(?P<flags>.+?)\s*$")

    current_name: str | None = None
    for line in completed.stdout.splitlines():
        header = header_pattern.match(line)
        if header is not None:
            name = header.group("name")
            current_name = name if name in result else None
            if current_name is None:
                continue
            control_type = header.group("type").strip().lower()
            info = result[current_name]
            info.update(
                {
                    "type": control_type,
                    "supported": True,
                    "error": None,
                }
            )
            if control_type == "bool":
                info.update({"minimum": 0, "maximum": 1, "step": 1})
            elif control_type == "menu":
                info["step"] = 1
            body = header.group("body")
        elif current_name is not None and line[:1].isspace():
            body = line
        else:
            current_name = None
            continue

        info = result[current_name]
        choice = choice_pattern.match(body)
        if choice is not None:
            info["choices"].append(
                {"value": int(choice.group("value")), "label": choice.group("label")}
            )
            continue
        for item in value_pattern.finditer(body):
            key = item.group("key")
            destination = {
                "min": "minimum",
                "max": "maximum",
                "step": "step",
                "default": "default",
                "value": "actual",
            }[key]
            info[destination] = int(item.group("value"))
        flags = flags_pattern.search(body)
        if flags is not None:
            info["flags"] = [
                flag.strip().lower()
                for flag in flags.group("flags").split(",")
                if flag.strip()
            ]
    for name, info in result.items():
        if not info["supported"]:
            info["error"] = f"摄像头不支持控制 {name}"
            continue
        if info["type"] == "bool" and not info["choices"]:
            info["choices"] = [
                {"value": 0, "label": "Off"},
                {"value": 1, "label": "On"},
            ]
        info["read_only"] = "read-only" in info["flags"]
        info["writable"] = (
            info["type"] in {"int", "integer", "integer64", "bool", "menu"}
            and not info["read_only"]
            and "inactive" not in info["flags"]
        )
    return result
