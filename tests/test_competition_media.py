from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time

import numpy as np

from competition_ui.media_service import CompetitionMediaService
from competition_ui.models import load_competition_ui_config
from core.models import FramePacket
from tests.test_vision_runtime import make_runtime


ROOT = Path(__file__).resolve().parents[1]


class OneFrameCamera:
    def __init__(self) -> None:
        self.frame = FramePacket(17, 123.25, np.zeros((20, 30, 3), dtype=np.uint8))
        self.calls = 0

    def get_latest_frame(self, copy_image=False):
        self.calls += 1
        assert copy_image is True
        return self.frame


class StubFrameStream:
    def __init__(self) -> None:
        self.images = []

    def start(self):
        pass

    def stop(self):
        pass

    def submit_frame(self, image):
        self.images.append(image.copy())

    def get_statistics(self):
        return {"preview_fps": 0.0, "preview_overwritten_count": 0}


class StubRecorder:
    def __init__(self) -> None:
        self.frames = []
        self.received = threading.Event()

    def start(self):
        pass

    def stop_accepting_new_sessions(self):
        pass

    def submit_frame(self, frame_id, capture_timestamp, image):
        self.frames.append((frame_id, capture_timestamp, image.copy()))
        self.received.set()

    def shutdown(self, _timeout):
        pass

    def status(self):
        return {"active": False}

    def storage_status(self):
        return {"free_bytes": 0, "free_mb": 0}


def test_media_service_reuses_camera_and_passes_frame_id_timestamp_and_image(tmp_path: Path) -> None:
    camera = OneFrameCamera()
    config = replace(
        load_competition_ui_config(project_root=ROOT),
        recording_directory=tmp_path,
    )
    media = CompetitionMediaService(camera, config)
    media.frame_stream = StubFrameStream()
    media.recorder = StubRecorder()
    media.start()
    assert media.recorder.received.wait(1)
    assert media.status()["media_worker_alive"] is True
    media.stop()
    assert media.camera_service is camera
    frame_id, timestamp, image = media.recorder.frames[0]
    assert (frame_id, timestamp) == (17, 123.25)
    assert image.shape == (20, 30, 3)


def test_media_worker_exception_is_reported_without_sensitive_path(tmp_path: Path) -> None:
    class FailingCamera:
        def get_latest_frame(self, copy_image=False):
            raise OSError("/home/private/camera-secret failed")

    config = replace(
        load_competition_ui_config(project_root=ROOT),
        recording_directory=tmp_path,
    )
    media = CompetitionMediaService(FailingCamera(), config)
    media.start()
    deadline = time.monotonic() + 1
    while media.status()["media_worker_alive"] and time.monotonic() < deadline:
        time.sleep(0.005)
    status = media.status()
    media.stop()
    assert status["media_worker_alive"] is False
    assert status["last_media_error"] == "比赛媒体帧处理失败"
    assert status["last_media_error_at"] is not None
    assert "/home/private" not in status["last_media_error"]


def test_competition_media_start_failure_does_not_stop_visual_main_chain() -> None:
    runtime = make_runtime()

    class FailingMedia:
        def start(self):
            raise OSError("recording directory unavailable")

    runtime.competition_media_service = FailingMedia()
    runtime.start()
    try:
        assert runtime._started is True
        assert runtime.camera_service.started == 1
        assert runtime._competition_media_started is False
    finally:
        runtime.stop()


def test_media_worker_exception_does_not_stop_visual_main_loop(tmp_path: Path) -> None:
    runtime = make_runtime()
    original_get_latest_frame = runtime.camera_service.get_latest_frame

    def get_latest_frame(copy_image=False):
        if copy_image:
            raise OSError("/home/private/media-worker-only failure")
        return original_get_latest_frame(copy_image=False)

    runtime.camera_service.get_latest_frame = get_latest_frame
    config = replace(
        load_competition_ui_config(project_root=ROOT),
        recording_directory=tmp_path,
    )
    media = CompetitionMediaService(runtime.camera_service, config)
    runtime.competition_media_service = media

    runtime.start()
    thread = threading.Thread(target=runtime.run_forever)
    thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = media.status()
        if runtime.detector.calls >= 1 and not status["media_worker_alive"]:
            break
        time.sleep(0.005)
    runtime.request_stop()
    thread.join(1)

    assert not thread.is_alive()
    assert runtime.detector.calls >= 1
    assert media.status()["last_media_error"]


def test_competition_and_debug_modules_do_not_open_camera_or_uart() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for folder in (ROOT / "competition_ui", ROOT / "web_competition", ROOT / "web_debug")
        for path in folder.rglob("*")
        if path.is_file() and path.suffix in {".py", ".js", ".html"}
    )
    assert "cv2.VideoCapture" not in sources
    assert "CameraService(" not in sources
    assert "serial.Serial" not in sources
    assert "BallUartClient" not in sources
    assert "navigator.serial" not in sources
