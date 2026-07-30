"""Frame fan-out for competition preview and recording."""

from __future__ import annotations

import threading
import logging
import time
from typing import Any

from competition_ui.recorder import CompetitionRecorder
from touch_ui.frame_stream import LatestFrameStream


LOG = logging.getLogger(__name__)


class CompetitionMediaService:
    """Reuse the sole CameraService and never open a camera itself."""

    def __init__(self, camera_service: Any, config: Any) -> None:
        self.camera_service = camera_service
        self.config = config
        self.frame_stream = LatestFrameStream(
            config.preview_max_fps,
            config.jpeg_quality,
            config.preview_max_width,
        )
        self.recorder = CompetitionRecorder(config)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._media_worker_alive = False
        self._last_media_error = ""
        self._last_media_error_at: float | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        with self._state_lock:
            self._media_worker_alive = True
            self._last_media_error = ""
            self._last_media_error_at = None
        self.frame_stream.start()
        try:
            self.recorder.start()
        except Exception:
            with self._state_lock:
                self._media_worker_alive = False
            self.frame_stream.stop()
            raise
        self._thread = threading.Thread(target=self._run, name="competition-media", daemon=True)
        try:
            self._thread.start()
        except Exception:
            with self._state_lock:
                self._media_worker_alive = False
            self.recorder.shutdown()
            self.frame_stream.stop()
            raise

    def _run(self) -> None:
        try:
            last_frame_id: int | None = None
            while not self._stop_event.is_set():
                frame = self.camera_service.get_latest_frame(copy_image=True)
                if frame is None or frame.frame_id == last_frame_id:
                    self._stop_event.wait(0.005)
                    continue
                last_frame_id = frame.frame_id
                self.frame_stream.submit_frame(frame.image)
                self.recorder.submit_frame(
                    frame.frame_id,
                    frame.capture_timestamp,
                    frame.image,
                )
        except Exception:
            with self._state_lock:
                self._last_media_error = "比赛媒体帧处理失败"
                self._last_media_error_at = time.time()
            LOG.exception("competition media worker failed")
        finally:
            with self._state_lock:
                self._media_worker_alive = False

    def stop(self, timeout: float = 5.0) -> None:
        self.recorder.stop_accepting_new_sessions()
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(min(timeout, 2.0))
            if not thread.is_alive():
                self._thread = None
        self.recorder.shutdown(timeout)
        self.frame_stream.stop()

    def status(self) -> dict[str, Any]:
        preview = self.frame_stream.get_statistics()
        with self._state_lock:
            worker = {
                "media_worker_alive": self._media_worker_alive,
                "last_media_error": self._last_media_error,
                "last_media_error_at": self._last_media_error_at,
            }
        return {
            "recording": self.recorder.status(),
            "preview": {
                "fps": preview["preview_fps"],
                "overwritten_count": preview["preview_overwritten_count"],
            },
            "storage": self.recorder.storage_status(),
            **worker,
        }
