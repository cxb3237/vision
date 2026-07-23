"""CameraService 使用 fake capture 的生命周期测试。"""

import threading
import time

import cv2
import numpy as np

from core.models import CameraConfig
from drivers.camera_service import CameraService


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
