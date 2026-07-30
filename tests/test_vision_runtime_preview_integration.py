from __future__ import annotations

from argparse import Namespace
import threading
import time

from core.vision_runtime import VisionRuntime
from detectors.target_tracker import TargetTracker
from tests.test_vision_runtime import FakeCamera, FakeDetector, FakeUart
from touch_ui.models import TouchUIConfig


def test_touch_preview_submit_frame_encodes_and_stops(tmp_path) -> None:
    config = TouchUIConfig(
        host="127.0.0.1",
        port=8765,
        preview_max_fps=30,
        jpeg_quality=70,
        preview_max_width=320,
        restore_runtime_overrides=False,
        startup_competition_mode=False,
        status_poll_interval_ms=250,
        parameter_debounce_ms=150,
        exit_competition_hold_ms=3000,
        runtime_directory=tmp_path,
        camera_override_file=tmp_path / "camera.yaml",
        ui_state_file=tmp_path / "ui.json",
        backup_directory=tmp_path / "backup",
        backup_limit=5,
        source_path=tmp_path / "touch.yaml",
    )
    runtime = VisionRuntime(
        args=Namespace(display=False, headless=True),
        mission={
            "camera_online_timeout_s": 1,
            "ball_uart": {
                "calibrated": True,
                "left_endpoint_px": 72,
                "right_endpoint_px": 568,
                "servo_side": "right",
            },
        },
        detector=FakeDetector(),
        camera_service=FakeCamera(),
        ball_uart=FakeUart(),
        tracker=TargetTracker(confirm_frames=1),
        display_handler=lambda *_: (True, None),
        touch_config=config,
    )
    runtime._refresh_camera_controls = lambda: None

    runtime.start()
    worker = threading.Thread(target=runtime.run_forever, name="preview-runtime-test")
    worker.start()
    deadline = time.monotonic() + 2
    while runtime.frame_stream.encoded_count < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    runtime.request_stop()
    worker.join(2)

    assert runtime.frame_stream.encoded_count >= 1
    assert not worker.is_alive()
    assert runtime.frame_stream._thread is None
