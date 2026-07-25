"""只保留最新帧的异步JPEG编码和MJPEG数据源。"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np


class LatestFrameStream:
    def __init__(self, max_fps: float = 10.0, jpeg_quality: int = 80, max_width: int = 960):
        if max_fps <= 0:
            raise ValueError("max_fps 必须为正数")
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality 必须在 1..100")
        if max_width <= 0:
            raise ValueError("max_width 必须为正数")
        self.max_fps = float(max_fps)
        self.jpeg_quality = int(jpeg_quality)
        self.max_width = int(max_width)
        self._condition = threading.Condition()
        self._pending: np.ndarray | None = None
        self._pending_id = 0
        self._jpeg: bytes | None = None
        self._jpeg_id = 0
        self._jpeg_monotonic = 0.0
        self._encoded_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._placeholder = self._create_placeholder()

    @staticmethod
    def _create_placeholder() -> bytes:
        image = np.full((360, 640, 3), 35, dtype=np.uint8)
        cv2.putText(
            image,
            "NO PREVIEW - CAMERA OFFLINE",
            (70, 185),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (220, 220, 220),
            2,
            cv2.LINE_AA,
        )
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return encoded.tobytes() if ok else b""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="preview-jpeg", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if not thread.is_alive():
                self._thread = None

    def submit_frame(self, image: np.ndarray) -> int:
        if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
            raise ValueError("预览帧必须为OpenCV图像")
        with self._condition:
            self._pending = image.copy()
            self._pending_id += 1
            frame_id = self._pending_id
            self._condition.notify_all()
            return frame_id

    def _run(self) -> None:
        interval = 1.0 / self.max_fps
        next_encode = 0.0
        while not self._stop.is_set():
            with self._condition:
                while self._pending is None and not self._stop.is_set():
                    self._condition.wait(0.2)
                if self._stop.is_set():
                    return
                image = self._pending
                frame_id = self._pending_id
                self._pending = None
            remaining = next_encode - time.monotonic()
            if remaining > 0 and self._stop.wait(remaining):
                return
            if image is None:
                continue
            height, width = image.shape[:2]
            if width > self.max_width:
                scale = self.max_width / width
                image = cv2.resize(
                    image,
                    (self.max_width, max(1, int(round(height * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, encoded = cv2.imencode(
                ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
            )
            next_encode = time.monotonic() + interval
            if not ok:
                continue
            with self._condition:
                self._jpeg = encoded.tobytes()
                self._jpeg_id = frame_id
                self._jpeg_monotonic = time.monotonic()
                self._encoded_count += 1
                self._condition.notify_all()

    def get_latest_jpeg(
        self,
        *,
        placeholder: bool = True,
        stale_after_s: float = 2.0,
    ) -> bytes | None:
        with self._condition:
            if self._jpeg is not None and (
                stale_after_s <= 0
                or time.monotonic() - self._jpeg_monotonic <= stale_after_s
            ):
                return bytes(self._jpeg)
        return bytes(self._placeholder) if placeholder else None

    def wait_for_jpeg(self, after_id: int, timeout: float = 1.0) -> tuple[int, bytes | None]:
        with self._condition:
            self._condition.wait_for(
                lambda: self._jpeg_id > after_id or self._stop.is_set(), timeout=timeout
            )
            if self._jpeg_id > after_id and self._jpeg is not None:
                return self._jpeg_id, bytes(self._jpeg)
            return after_id, None

    @property
    def encoded_count(self) -> int:
        with self._condition:
            return self._encoded_count
