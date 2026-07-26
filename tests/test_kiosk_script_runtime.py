"""在Linux上用假浏览器验证kiosk脚本的有界重启和信号清理。"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path: Path, mode: str, max_restarts: int = 5):
    if platform.system() != "Linux" or shutil.which("bash") is None:
        pytest.skip("kiosk运行时脚本测试仅在Linux执行")
    project = tmp_path / "project"
    deploy = project / "deploy"
    fake_bin = tmp_path / "bin"
    deploy.mkdir(parents=True)
    fake_bin.mkdir()
    script = deploy / "start_kiosk.sh"
    shutil.copy2(ROOT / "deploy/start_kiosk.sh", script)
    script.chmod(0o755)
    curl = fake_bin / "curl"
    curl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
    curl.chmod(0o755)
    browser = fake_bin / "chromium"
    browser.write_text(
        """#!/usr/bin/env bash
count=0
[[ -f "$MOCK_COUNT_FILE" ]] && count="$(cat "$MOCK_COUNT_FILE")"
count=$((count + 1))
printf '%s\n' "$count" > "$MOCK_COUNT_FILE"
printf '%s\n' "$*" >> "$MOCK_ARGS_FILE"
case "$MOCK_MODE" in
  normal) exit 0 ;;
  requested) touch "$MOCK_RUNTIME/kiosk.exit_requested"; exit 143 ;;
  abnormal_then_ok) [[ "$count" -eq 1 ]] && exit 2 || exit 0 ;;
  always_fail) exit 2 ;;
  sleep) exec sleep 30 ;;
  *) exit 9 ;;
esac
""",
        encoding="utf-8",
        newline="\n",
    )
    browser.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "MOCK_MODE": mode,
            "MOCK_COUNT_FILE": str(tmp_path / "count"),
            "MOCK_ARGS_FILE": str(tmp_path / "args"),
            "MOCK_RUNTIME": str(project / "runtime"),
            "VISION_KIOSK_MAX_RESTARTS": str(max_restarts),
            "VISION_KIOSK_RESTART_DELAY": "0",
        }
    )
    return script, environment, tmp_path / "count", tmp_path / "args", project / "runtime"


def _run(script: Path, environment: dict[str, str]):
    return subprocess.run(
        ["bash", str(script)],
        cwd=script.parents[1],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


def test_normal_browser_exit_does_not_restart(tmp_path: Path) -> None:
    script, environment, count, arguments, runtime = _fixture(tmp_path, "normal")
    runtime.mkdir(parents=True)
    (runtime / "kiosk.exit_requested").write_text("stale\n", encoding="ascii")
    completed = _run(script, environment)
    assert completed.returncode == 0
    assert count.read_text().strip() == "1"
    assert not (runtime / "kiosk.pid").exists()
    assert not (runtime / "kiosk.exit_requested").exists()
    assert f"--user-data-dir={runtime / 'chrome-profile'}" in arguments.read_text()


def test_exit_requested_does_not_restart_and_is_cleaned(tmp_path: Path) -> None:
    script, environment, count, _arguments, runtime = _fixture(tmp_path, "requested")
    completed = _run(script, environment)
    assert completed.returncode == 0 and count.read_text().strip() == "1"
    assert not (runtime / "kiosk.pid").exists()
    assert not (runtime / "kiosk.exit_requested").exists()


def test_abnormal_exit_retries_but_normal_second_exit_stops(tmp_path: Path) -> None:
    script, environment, count, _arguments, _runtime = _fixture(
        tmp_path, "abnormal_then_ok"
    )
    completed = _run(script, environment)
    assert completed.returncode == 0
    assert count.read_text().strip() == "2"
    assert "第1次重试" in completed.stderr


def test_abnormal_retries_are_limited(tmp_path: Path) -> None:
    script, environment, count, _arguments, _runtime = _fixture(
        tmp_path, "always_fail", max_restarts=2
    )
    completed = _run(script, environment)
    assert completed.returncode == 2
    assert count.read_text().strip() == "3"
    assert "达到最大重试次数2" in completed.stderr


def test_sigterm_stops_only_child_and_cleans_pid(tmp_path: Path) -> None:
    script, environment, _count, _arguments, runtime = _fixture(tmp_path, "sleep")
    process = subprocess.Popen(
        ["bash", str(script)],
        cwd=script.parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    pid_file = runtime / "kiosk.pid"
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text().strip())
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=5)
    assert process.returncode == 0
    assert not pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
