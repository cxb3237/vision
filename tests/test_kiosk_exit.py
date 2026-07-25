"""kiosk退出仅验证和操作固定PID文件中的当前用户浏览器进程。"""

from pathlib import Path
import signal

import pytest

from touch_ui.kiosk import KioskExitError, exit_kiosk


def _fake_process(tmp_path: Path, command: bytes) -> tuple[Path, Path, int]:
    pid = 4321
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pid_file = runtime / "kiosk.pid"
    pid_file.write_text(f"{pid}\n", encoding="ascii")
    process = tmp_path / "proc" / str(pid)
    process.mkdir(parents=True)
    (process / "cmdline").write_bytes(command)
    return pid_file, tmp_path / "proc", pid


def test_valid_owned_chromium_kiosk_receives_sigterm(tmp_path: Path) -> None:
    pid_file, process_root, pid = _fake_process(
        tmp_path, b"/usr/bin/chromium\0--kiosk\0--app=http://127.0.0.1:8765\0"
    )
    calls = []
    uid = (process_root / str(pid)).stat().st_uid
    assert exit_kiosk(
        pid_file,
        process_root=process_root,
        platform_name="Linux",
        current_uid=uid,
        kill_process=lambda process_id, sig: calls.append((process_id, sig)),
    ) == pid
    assert calls == [(pid, signal.SIGTERM)]
    assert (pid_file.parent / "kiosk.exit").read_text(encoding="ascii") == "exit\n"


def test_unrelated_process_is_never_terminated(tmp_path: Path) -> None:
    pid_file, process_root, pid = _fake_process(tmp_path, b"/usr/bin/python3\0worker.py\0")
    calls = []
    uid = (process_root / str(pid)).stat().st_uid
    with pytest.raises(KioskExitError, match="不是受支持"):
        exit_kiosk(
            pid_file,
            process_root=process_root,
            platform_name="Linux",
            current_uid=uid,
            kill_process=lambda *args: calls.append(args),
        )
    assert calls == []


def test_pid_file_rejects_non_numeric_content(tmp_path: Path) -> None:
    pid_file = tmp_path / "kiosk.pid"
    pid_file.write_text("1; shutdown -h now\n", encoding="ascii")
    with pytest.raises(KioskExitError, match="正整数"):
        exit_kiosk(pid_file, platform_name="Linux", current_uid=0)


def test_windows_reports_unsupported_without_process_access(tmp_path: Path) -> None:
    with pytest.raises(KioskExitError, match="当前平台不支持"):
        exit_kiosk(tmp_path / "missing.pid", platform_name="Windows")
