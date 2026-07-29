from __future__ import annotations

from argparse import Namespace
import threading
import time

import numpy as np

from core.models import FramePacket, TargetState, VisionResult
from core.vision_runtime import VisionRuntime
from detectors.target_tracker import TargetTracker
from drivers.ball_uart_client import BallUartClient, encode_command


class FakeDetector:
    target_class = 100

    def __init__(self):
        self.calls = 0

    def initialize(self):
        pass

    def close(self):
        pass

    def process(self, frame):
        self.calls += 1
        return VisionResult(frame.frame_id, frame.capture_timestamp, time.monotonic(), True, TargetState.NONE, 100, 320, 240, image_width=640, image_height=480, confidence=900)

    def draw_debug(self, image, _result):
        return image.copy()

    def get_runtime_status(self):
        return {"model_loaded": True, "detector_error": ""}


class FakeCamera:
    def __init__(self):
        self.frame = FramePacket(1, time.monotonic(), np.zeros((480, 640, 3), np.uint8))
        self.started = self.stopped = 0
        self.latest_frame_age_s = 0.0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def get_latest_frame(self, copy_image=False):
        return self.frame

    def get_statistics(self):
        return {
            "running": self.started > self.stopped,
            "frames_ok": 1,
            "actual_fps": 30.0,
            "latest_frame_age_s": self.latest_frame_age_s,
        }

    def is_running(self):
        return self.started > self.stopped


class FakeUart:
    enabled = True

    def __init__(self):
        self.positions = []
        self.invalid = 0
        self.discards = 0
        self.starts = 0
        self.stops = 0

    def start(self):
        pass

    def close(self):
        pass

    def send_start(self):
        self.starts += 1

    def send_stop(self):
        self.stops += 1

    def publish_ball_position(self, value):
        self.positions.append(value)

    def send_invalid(self):
        self.invalid += 1

    def discard_pending_ball_position(self):
        self.discards += 1

    def pixel_x_to_mm(self, x):
        return round((x - 320) * 250 / 496)

    def get_statistics(self):
        return {"connected": True, "mcu_ready": True, "uart_state": "MCU 已就绪", "mcu_status": {}, "position_tx_count": len(self.positions)}


def make_runtime(competition=False, *, calibrated=True, left=72, right=568, servo_side="right"):
    runtime = VisionRuntime(
        args=Namespace(display=False, headless=True),
        mission={
            "camera_online_timeout_s": 1.0,
            "ball_uart": {
                "calibrated": calibrated,
                "left_endpoint_px": left,
                "right_endpoint_px": right,
                "servo_side": servo_side,
            },
        },
        detector=FakeDetector(),
        camera_service=FakeCamera(),
        ball_uart=FakeUart(),
        tracker=TargetTracker(confirm_frames=1),
        display_handler=lambda *_: (True, None),
        initial_competition_mode=competition,
    )
    runtime._refresh_camera_controls = lambda: None
    return runtime


def run_one_frame(runtime):
    runtime.start()
    thread = threading.Thread(target=runtime.run_forever)
    thread.start()
    deadline = time.monotonic() + 1
    while runtime.get_status_snapshot()["vision_processed_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    runtime.request_stop()
    thread.join(1)
    assert not thread.is_alive()


def test_debug_mode_detects_and_updates_web_but_does_not_send() -> None:
    runtime = make_runtime(False)
    run_one_frame(runtime)
    status = runtime.get_status_snapshot()
    assert runtime.detector.calls == 1
    assert status["ball_x_px"] == 320 and status["confidence"] == 900
    assert runtime.ball_uart.positions == []
    assert status["vision_output_enabled"] is False


def test_competition_mode_sends_only_ascii_position_submission() -> None:
    runtime = make_runtime(True)
    run_one_frame(runtime)
    assert runtime.ball_uart.positions == [0]
    assert runtime.get_status_snapshot()["vision_output_enabled"] is True


def test_uncalibrated_competition_mode_sends_invalid_not_position() -> None:
    runtime = make_runtime(True, calibrated=False)
    run_one_frame(runtime)
    status = runtime.get_status_snapshot()
    assert runtime.ball_uart.positions == []
    assert runtime.ball_uart.invalid == 1
    assert status["ball_position_calibrated"] is False
    assert "未标定" in status["ball_position_calibration_error"]


def test_valid_calibration_allows_position() -> None:
    runtime = make_runtime(True, calibrated=True)
    run_one_frame(runtime)
    assert runtime.ball_uart.positions == [0]


def test_equal_calibration_endpoints_are_reported_without_crashing() -> None:
    runtime = make_runtime(calibrated=True, left=72, right=72)
    status = runtime.get_status_snapshot()
    assert status["ball_position_calibrated"] is False
    assert "不能相同" in status["ball_position_calibration_error"]


def test_calibration_endpoint_outside_actual_image_is_rejected() -> None:
    runtime = make_runtime(calibrated=True, right=640)
    status = runtime._ball_calibration_status(640)
    assert status["ball_position_calibrated"] is False
    assert "图像宽度" in status["ball_position_calibration_error"]


def test_invalid_servo_side_is_reported_without_crashing() -> None:
    runtime = make_runtime(calibrated=True, servo_side="up")
    status = runtime.get_status_snapshot()
    assert status["ball_position_calibrated"] is False
    assert "servo_side" in status["ball_position_calibration_error"]


def test_camera_online_uses_recent_frame_age_and_recovers() -> None:
    runtime = make_runtime()
    runtime.start()
    try:
        runtime.camera_service.latest_frame_age_s = None
        runtime._update_service_status()
        assert runtime.get_status_snapshot()["camera_online"] is False
        runtime.camera_service.latest_frame_age_s = 1.5
        runtime._update_service_status()
        assert runtime.get_status_snapshot()["camera_online"] is False
        runtime.camera_service.latest_frame_age_s = 0.1
        runtime._update_service_status()
        status = runtime.get_status_snapshot()
        assert status["camera_online"] is True
        assert status["latest_frame_age_s"] == 0.1
    finally:
        runtime.stop()


def test_exiting_competition_discards_pending_and_stops_future_output() -> None:
    runtime = make_runtime(True)
    runtime._set_competition(False)
    assert runtime.ball_uart.discards >= 1
    assert runtime.ball_uart.stops == 1
    assert runtime.get_status_snapshot()["competition_mode"] is False


def test_enter_then_immediate_exit_leaves_only_stop_for_mcu() -> None:
    runtime = make_runtime(False)
    runtime.ball_uart = BallUartClient()
    runtime._set_competition(True)
    runtime._set_competition(False)
    with runtime.ball_uart._outbound_lock:
        controls = list(runtime.ball_uart._control)
    assert encode_command("STOP") in controls
    assert encode_command("START") not in controls


def test_exit_then_immediate_enter_leaves_only_start_for_mcu() -> None:
    runtime = make_runtime(True)
    runtime.ball_uart = BallUartClient()
    runtime._set_competition(False)
    runtime._set_competition(True)
    with runtime.ball_uart._outbound_lock:
        controls = list(runtime.ball_uart._control)
    assert encode_command("START") in controls
    assert encode_command("STOP") not in controls
