"""安全退出本机kiosk浏览器；模块导入时不探测进程或平台硬件。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import platform
import re
import signal
import tempfile
from typing import Callable


LOG = logging.getLogger(__name__)
_BROWSER_NAMES = {
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "firefox",
}


class KioskExitError(RuntimeError):
    """PID文件或目标进程未通过安全验证。"""


def _read_pid(pid_file: Path) -> int:
    try:
        value = pid_file.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise KioskExitError(f"无法读取kiosk PID文件: {exc}") from exc
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise KioskExitError("kiosk PID文件必须只包含正整数")
    return int(value)


def _write_exit_marker(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.", suffix=".tmp", dir=marker.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write("exit\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def exit_kiosk(
    pid_file: str | Path,
    *,
    process_root: str | Path = "/proc",
    platform_name: str | None = None,
    current_uid: int | None = None,
    kill_process: Callable[[int, int], None] = os.kill,
) -> int:
    """验证PID归属和浏览器命令后，只向该kiosk进程发送SIGTERM。"""

    system = platform_name or platform.system()
    if system != "Linux":
        raise KioskExitError(f"当前平台不支持退出kiosk: {system}")
    path = Path(pid_file)
    pid = _read_pid(path)
    process_dir = Path(process_root) / str(pid)
    try:
        owner_uid = process_dir.stat().st_uid
        command_line = (process_dir / "cmdline").read_bytes().split(b"\0")
    except OSError as exc:
        raise KioskExitError(f"kiosk PID {pid}对应进程不存在或不可读: {exc}") from exc
    expected_uid = os.getuid() if current_uid is None else current_uid
    if owner_uid != expected_uid:
        raise KioskExitError(f"拒绝结束非当前用户拥有的进程 PID={pid}")
    arguments = [item.decode("utf-8", errors="replace") for item in command_line if item]
    executable = Path(arguments[0]).name.lower() if arguments else ""
    if executable not in _BROWSER_NAMES or "--kiosk" not in arguments:
        raise KioskExitError(f"PID={pid}不是受支持的Chrome/Chromium/Firefox kiosk进程")

    marker = path.with_name("kiosk.exit")
    try:
        _write_exit_marker(marker)
        kill_process(pid, signal.SIGTERM)
    except OSError as exc:
        marker.unlink(missing_ok=True)
        LOG.warning("kiosk退出失败 PID=%s: %s", pid, exc)
        raise KioskExitError(f"发送SIGTERM失败 PID={pid}: {exc}") from exc
    LOG.warning("已确认并请求退出kiosk PID=%s", pid)
    return pid
