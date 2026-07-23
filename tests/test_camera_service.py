"""CameraService 使用 fake capture 的生命周期测试。"""

import threading
import time

import cv2
import numpy as np
import pytest

from core.models import CameraConfig
import drivers.camera_service as camera_service_module
from drivers.camera_service import CameraService
from drivers.v4l2_controls import V4L2ControlError


class FakeCapture:
    def __init__(self, fail_reads: int = 0) -> None:
        self.opened = True
        self.fail_reads = fail_reads
        self.release_count = 0
        self.set_calls: list[tuple[int, float]] = []
        self.read_count = 0
        self.lock = threading.Lock()

    def isOpened(self) -> bool:
        return self.opened

    def read(self):
        time.sleep(0.001)
        with self.lock:
            self.read_count += 1
            if self.fail_reads > 0:
                self.fail_reads -= 1
                return False, None
        return True, np.full((24, 32, 3), self.read_count % 255, np.uint8)

    def release(self) -> None:
        self.release_count += 1
        self.opened = False

    def set(self, property_id: int, value: float) -> bool:
        self.set_calls.append((property_id, value))
        return True

    def get(self, property_id: int) -> float:
        values = {
            cv2.CAP_PROP_FRAME_WIDTH: 32,
            cv2.CAP_PROP_FRAME_HEIGHT: 24,
            cv2.CAP_PROP_FPS: 25,
            cv2.CAP_PROP_FOURCC: cv2.VideoWriter_fourcc(*"MJPG"),
        }
        return float(values.get(property_id, 0))


def wait_for_frame(service: CameraService, timeout: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = service.get_latest_frame()
        if frame is not None:
            return frame
        time.sleep(0.005)
    raise AssertionError("未收到 fake 帧")


def test_start_stop_latest_and_single_release() -> None:
    fake = FakeCapture()
    service = CameraService(CameraConfig(), lambda: fake)
    service.start()
    first = wait_for_frame(service)
    time.sleep(0.01)
    latest = wait_for_frame(service)
    assert latest.frame_id >= first.frame_id
    service.stop()
    assert not service.is_running()
    assert fake.release_count == 1


def test_repeated_start_stop_uses_new_owned_capture() -> None:
    captures: list[FakeCapture] = []

    def factory() -> FakeCapture:
        capture = FakeCapture()
        captures.append(capture)
        return capture

    service = CameraService(CameraConfig(), factory)
    for _ in range(2):
        service.start()
        wait_for_frame(service)
        service.stop()
    assert len(captures) == 2
    assert all(capture.release_count == 1 for capture in captures)


def test_optional_none_properties_are_not_set() -> None:
    fake = FakeCapture()
    config = CameraConfig(exposure=None, gain=None, brightness=None, contrast=None)
    service = CameraService(config, lambda: fake)
    service.start()
    wait_for_frame(service)
    service.stop()
    property_ids = {call[0] for call in fake.set_calls}
    assert cv2.CAP_PROP_GAIN not in property_ids
    assert cv2.CAP_PROP_BRIGHTNESS not in property_ids
    assert cv2.CAP_PROP_CONTRAST not in property_ids


def test_read_failures_trigger_reconnect() -> None:
    captures = [FakeCapture(fail_reads=2), FakeCapture()]
    service = CameraService(
        CameraConfig(reconnect_after_failures=2),
        lambda: captures.pop(0),
    )
    service.start()
    wait_for_frame(service)
    service.stop()
    assert service.get_statistics()["reconnects"] >= 1


def test_restart_clears_old_latest_frame_before_new_capture() -> None:
    first = FakeCapture()
    second = FakeCapture()
    allow_second_read = threading.Event()
    original_read = second.read

    def blocked_read():
        allow_second_read.wait(1.0)
        return original_read()

    second.read = blocked_read
    captures = [first, second]
    service = CameraService(CameraConfig(), lambda: captures.pop(0))
    service.start()
    wait_for_frame(service)
    service.stop()
    assert service.get_latest_frame() is not None
    service.start()
    assert service.get_latest_frame() is None
    allow_second_read.set()
    wait_for_frame(service)
    service.stop()


def _mock_v4l2(monkeypatch, actual: dict[str, int], events: list[str] | None = None) -> None:
    def fake_apply(device, controls, strict=False):
        return {
            name: {
                "requested": value,
                "success": True,
                "skipped": False,
                "error": None,
            }
            for name, value in controls.items()
        }

    def fake_read(device, names):
        if events is not None:
            events.append("v4l2_read")
        return {name: actual.get(name) for name in names}

    monkeypatch.setattr(camera_service_module, "apply_v4l2_controls", fake_apply)
    monkeypatch.setattr(camera_service_module, "read_v4l2_controls", fake_read)


def test_v4l2_white_balance_prevents_opencv_auto_wb_override(monkeypatch) -> None:
    _mock_v4l2(monkeypatch, {"white_balance_automatic": 0})
    fake = FakeCapture()
    service = CameraService(
        CameraConfig(
            auto_white_balance=True,
            v4l2_controls={"enabled": True, "white_balance_automatic": 0},
        ),
        lambda: fake,
    )
    capture = service._open_capture()
    assert capture is fake
    assert cv2.CAP_PROP_AUTO_WB not in {item[0] for item in fake.set_calls}
    fake.release()


def test_opencv_auto_wb_is_set_without_v4l2_authority() -> None:
    fake = FakeCapture()
    service = CameraService(CameraConfig(auto_white_balance=True), lambda: fake)
    capture = service._open_capture()
    assert capture is fake
    assert (cv2.CAP_PROP_AUTO_WB, 1.0) in fake.set_calls
    fake.release()


def test_v4l2_brightness_prevents_opencv_override(monkeypatch) -> None:
    _mock_v4l2(monkeypatch, {"brightness": 7})
    fake = FakeCapture()
    service = CameraService(
        CameraConfig(
            brightness=99,
            v4l2_controls={"enabled": True, "brightness": 7},
        ),
        lambda: fake,
    )
    capture = service._open_capture()
    assert capture is fake
    assert cv2.CAP_PROP_BRIGHTNESS not in {item[0] for item in fake.set_calls}
    fake.release()


def test_final_v4l2_read_occurs_after_opencv_property_sets(monkeypatch) -> None:
    events: list[str] = []
    _mock_v4l2(monkeypatch, {"brightness": 7}, events)
    fake = FakeCapture()
    original_set = fake.set

    def recording_set(property_id, value):
        events.append("opencv_set")
        return original_set(property_id, value)

    fake.set = recording_set
    service = CameraService(
        CameraConfig(v4l2_controls={"enabled": True, "brightness": 7}),
        lambda: fake,
    )
    capture = service._open_capture()
    assert capture is fake
    assert events[-1] == "v4l2_read"
    assert "opencv_set" in events[:-1]
    fake.release()


def test_v4l2_mismatch_warns_when_not_strict(monkeypatch, caplog) -> None:
    _mock_v4l2(monkeypatch, {"brightness": 8})
    fake = FakeCapture()
    service = CameraService(
        CameraConfig(
            v4l2_controls={"enabled": True, "strict": False, "brightness": 7}
        ),
        lambda: fake,
    )
    capture = service._open_capture()
    assert capture is fake
    assert "V4L2 最终参数与请求不一致" in caplog.text
    fake.release()


def test_v4l2_mismatch_releases_capture_and_raises_when_strict(monkeypatch) -> None:
    _mock_v4l2(monkeypatch, {"brightness": 8})
    fake = FakeCapture()
    service = CameraService(
        CameraConfig(
            v4l2_controls={"enabled": True, "strict": True, "brightness": 7}
        ),
        lambda: fake,
    )
    with pytest.raises(V4L2ControlError, match="brightness"):
        service._open_capture()
    assert fake.release_count == 1
