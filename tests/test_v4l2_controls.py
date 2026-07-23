"""V4L2 子进程封装及 CameraService 生命周期集成测试。"""

from __future__ import annotations

from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

from core.models import CameraConfig
import drivers.camera_service as camera_service_module
from drivers.camera_service import CameraService
import drivers.v4l2_controls as v4l2


def _linux_with_command(monkeypatch) -> None:
    monkeypatch.setattr(v4l2.platform, "system", lambda: "Linux")
    monkeypatch.setattr(v4l2.shutil, "which", lambda name: "/usr/bin/v4l2-ctl")


def test_device_number_is_resolved_to_path() -> None:
    assert v4l2.resolve_video_device(0) == "/dev/video0"
    assert v4l2.resolve_video_device("0") == "/dev/video0"
    assert v4l2.resolve_video_device("/dev/video1") == "/dev/video1"


def test_null_control_is_skipped(monkeypatch) -> None:
    _linux_with_command(monkeypatch)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(v4l2.subprocess, "run", fake_run)
    results = v4l2.apply_v4l2_controls(0, {"brightness": None, "contrast": 16})
    assert "brightness" not in results
    assert list(results) == ["contrast"]
    assert len(calls) == 1


def test_windows_is_skipped_without_subprocess(monkeypatch) -> None:
    monkeypatch.setattr(v4l2.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        v4l2.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("subprocess must not be called"),
    )
    results = v4l2.apply_v4l2_controls(0, {"brightness": 0}, strict=True)
    assert results["brightness"]["skipped"]
    assert not results["brightness"]["success"]


def test_missing_v4l2_ctl_reports_install_hint(monkeypatch, caplog) -> None:
    monkeypatch.setattr(v4l2.platform, "system", lambda: "Linux")
    monkeypatch.setattr(v4l2.shutil, "which", lambda name: None)
    results = v4l2.apply_v4l2_controls(0, {"brightness": 0})
    assert "sudo apt install v4l-utils" in results["brightness"]["error"]
    assert "sudo apt install v4l-utils" in caplog.text
    with pytest.raises(v4l2.V4L2ControlError, match="v4l-utils"):
        v4l2.apply_v4l2_controls(0, {"brightness": 0}, strict=True)


def test_single_control_is_set_successfully_without_shell(monkeypatch) -> None:
    _linux_with_command(monkeypatch)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(v4l2.subprocess, "run", fake_run)
    results = v4l2.apply_v4l2_controls("1", {"brightness": 7})
    command, kwargs = calls[0]
    assert command == [
        "/usr/bin/v4l2-ctl",
        "--device",
        "/dev/video1",
        "--set-ctrl",
        "brightness=7",
    ]
    assert kwargs["shell"] is False
    assert results["brightness"]["success"]


def test_failed_control_warns_when_not_strict(monkeypatch, caplog) -> None:
    _linux_with_command(monkeypatch)
    monkeypatch.setattr(
        v4l2.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="unknown control brightness"
        ),
    )
    results = v4l2.apply_v4l2_controls(0, {"brightness": 0}, strict=False)
    assert not results["brightness"]["success"]
    assert "brightness" in results["brightness"]["error"]
    assert "brightness" in caplog.text


def test_failed_control_raises_when_strict(monkeypatch) -> None:
    _linux_with_command(monkeypatch)
    monkeypatch.setattr(
        v4l2.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="invalid value"
        ),
    )
    with pytest.raises(v4l2.V4L2ControlError, match="brightness=0"):
        v4l2.apply_v4l2_controls(0, {"brightness": 0}, strict=True)


def test_read_controls_parses_actual_values_without_shell(monkeypatch) -> None:
    _linux_with_command(monkeypatch)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        name = command[-1]
        return SimpleNamespace(returncode=0, stdout=f"{name}: -3\n", stderr="")

    monkeypatch.setattr(v4l2.subprocess, "run", fake_run)
    assert v4l2.read_v4l2_controls(0, ["brightness"]) == {"brightness": -3}
    assert calls[0]["shell"] is False


def test_automatic_control_is_applied_before_manual_value(monkeypatch) -> None:
    _linux_with_command(monkeypatch)
    controls_in_order = []

    def fake_run(command, **kwargs):
        controls_in_order.append(command[-1].split("=", 1)[0])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(v4l2.subprocess, "run", fake_run)
    v4l2.apply_v4l2_controls(
        0,
        {
            "white_balance_temperature": 5000,
            "brightness": 0,
            "white_balance_automatic": 0,
        },
    )
    assert controls_in_order.index("white_balance_automatic") < controls_in_order.index(
        "white_balance_temperature"
    )


class _FakeCapture:
    def __init__(self, fail_reads: int = 0) -> None:
        self.fail_reads = fail_reads
        self.opened = True

    def isOpened(self) -> bool:
        return self.opened

    def read(self):
        time.sleep(0.001)
        if self.fail_reads:
            self.fail_reads -= 1
            return False, None
        return True, np.zeros((24, 32, 3), np.uint8)

    def release(self) -> None:
        self.opened = False

    def set(self, property_id, value) -> bool:
        return True

    def get(self, property_id) -> float:
        return 0.0


def _wait_for_frame(service: CameraService) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if service.get_latest_frame() is not None:
            return
        time.sleep(0.005)
    raise AssertionError("未收到测试帧")


def test_camera_service_reapplies_controls_on_reconnect(monkeypatch) -> None:
    apply_threads: list[str] = []

    def fake_apply(device, controls, strict=False):
        apply_threads.append(threading.current_thread().name)
        return {
            name: {"requested": value, "success": True, "skipped": False, "error": None}
            for name, value in controls.items()
        }

    monkeypatch.setattr(camera_service_module, "apply_v4l2_controls", fake_apply)
    monkeypatch.setattr(
        camera_service_module,
        "read_v4l2_controls",
        lambda device, names: {name: 0 for name in names},
    )
    captures = [_FakeCapture(fail_reads=1), _FakeCapture()]
    service = CameraService(
        CameraConfig(
            reconnect_after_failures=1,
            v4l2_controls={"enabled": True, "strict": False, "brightness": 0},
        ),
        lambda: captures.pop(0),
    )
    service.start()
    _wait_for_frame(service)
    service.stop()
    assert len(apply_threads) >= 2
    assert set(apply_threads) == {"camera-capture"}


def test_camera_service_reapplies_controls_after_restart(monkeypatch) -> None:
    apply_count = 0

    def fake_apply(device, controls, strict=False):
        nonlocal apply_count
        apply_count += 1
        return {
            name: {"requested": value, "success": True, "skipped": False, "error": None}
            for name, value in controls.items()
        }

    monkeypatch.setattr(camera_service_module, "apply_v4l2_controls", fake_apply)
    monkeypatch.setattr(
        camera_service_module,
        "read_v4l2_controls",
        lambda device, names: {name: 0 for name in names},
    )
    captures = [_FakeCapture(), _FakeCapture()]
    service = CameraService(
        CameraConfig(v4l2_controls={"enabled": True, "brightness": 0}),
        lambda: captures.pop(0),
    )
    for _ in range(2):
        service.start()
        _wait_for_frame(service)
        service.stop()
    assert apply_count == 2
