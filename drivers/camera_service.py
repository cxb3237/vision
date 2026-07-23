"""由单一采集线程持有 VideoCapture 的最新帧服务。"""

from __future__ import annotations

from collections.abc import Callable
import logging
import platform
import threading
import time
from typing import Any, Protocol

import cv2

from core.models import CameraConfig, FramePacket


LOG = logging.getLogger(__name__)


class CaptureLike(Protocol):
    """测试替身和 OpenCV VideoCapture 的最小公共接口。"""

    def isOpened(self) -> bool: ...

    def read(self) -> tuple[bool, Any]: ...

    def release(self) -> None: ...

    def set(self, property_id: int, value: float) -> bool: ...

    def get(self, property_id: int) -> float: ...


class CameraService:
    """后台采集并仅保留最新一帧，采集线程独占摄像头句柄。"""

    def __init__(
        self,
        config: CameraConfig,
        capture_factory: Callable[[], CaptureLike] | None = None,
    ) -> None:
        self.config = config
        self._capture_factory = capture_factory
        self._capture: CaptureLike | None = None
        self._latest: FramePacket | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame_id = 0
        self._frames_ok = 0
        self._frames_failed = 0
        self._reconnects = 0
        self._timestamps: list[float] = []
        self._actual_width = 0
        self._actual_height = 0
        self._actual_fps = 0.0
        self._actual_fourcc = ""
        self._last_open_log = 0.0

    @staticmethod
    def _decode_fourcc(value: float) -> str:
        encoded = int(value)
        return "".join(chr((encoded >> (8 * index)) & 0xFF) for index in range(4)).rstrip("\x00")

    @staticmethod
    def _try_set_property(
        capture: CaptureLike,
        property_id: int,
        value: float | None,
        name: str,
    ) -> None:
        """按名称设置可选属性，并输出有意义的兼容性日志。"""

        if value is None:
            return
        try:
            if not capture.set(property_id, float(value)):
                LOG.warning("摄像头不支持参数 %s=%s", name, value)
        except Exception as exc:
            LOG.warning("设置摄像头参数 %s=%s 失败: %s", name, value, exc)

    def _new_capture(self, api: int) -> CaptureLike:
        if self._capture_factory is not None:
            return self._capture_factory()
        return cv2.VideoCapture(self.config.device, api)

    def _open_with_api(self, api: int) -> CaptureLike | None:
        capture = self._new_capture(api)
        if capture.isOpened():
            return capture
        capture.release()
        return None

    def _open_capture(self) -> CaptureLike | None:
        """在采集线程内创建并配置摄像头。"""

        try:
            preferred_api = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY
            capture = self._open_with_api(preferred_api)
            if capture is None and preferred_api != cv2.CAP_ANY and self._capture_factory is None:
                LOG.warning("V4L2 打开失败，回退到 CAP_ANY: %s", self.config.device)
                capture = self._open_with_api(cv2.CAP_ANY)
            if capture is None:
                now = time.monotonic()
                if now - self._last_open_log >= 2.0:
                    LOG.error("无法打开摄像头: %s", self.config.device)
                    self._last_open_log = now
                return None

            if len(self.config.fourcc) == 4:
                self._try_set_property(
                    capture,
                    cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc(*self.config.fourcc),
                    "fourcc",
                )
            self._try_set_property(capture, cv2.CAP_PROP_FRAME_WIDTH, self.config.width, "width")
            self._try_set_property(capture, cv2.CAP_PROP_FRAME_HEIGHT, self.config.height, "height")
            self._try_set_property(capture, cv2.CAP_PROP_FPS, self.config.fps, "fps")
            self._try_set_property(
                capture,
                cv2.CAP_PROP_BUFFERSIZE,
                self.config.buffer_size,
                "buffer_size",
            )
            if self.config.manual_exposure:
                self._try_set_property(capture, cv2.CAP_PROP_AUTO_EXPOSURE, 0.25, "auto_exposure")
                self._try_set_property(
                    capture,
                    cv2.CAP_PROP_EXPOSURE,
                    self.config.exposure,
                    "exposure",
                )
            self._try_set_property(capture, cv2.CAP_PROP_GAIN, self.config.gain, "gain")
            self._try_set_property(
                capture,
                cv2.CAP_PROP_AUTO_WB,
                1.0 if self.config.auto_white_balance else 0.0,
                "auto_white_balance",
            )
            self._try_set_property(
                capture,
                cv2.CAP_PROP_BRIGHTNESS,
                self.config.brightness,
                "brightness",
            )
            self._try_set_property(
                capture,
                cv2.CAP_PROP_CONTRAST,
                self.config.contrast,
                "contrast",
            )
            try:
                actual_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                actual_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                actual_fps = float(capture.get(cv2.CAP_PROP_FPS))
                actual_fourcc = self._decode_fourcc(capture.get(cv2.CAP_PROP_FOURCC))
            except (AttributeError, TypeError, ValueError):
                actual_width = self.config.width
                actual_height = self.config.height
                actual_fps = float(self.config.fps)
                actual_fourcc = self.config.fourcc
            with self._lock:
                self._actual_width = actual_width
                self._actual_height = actual_height
                self._actual_fps = actual_fps
                self._actual_fourcc = actual_fourcc
            LOG.info(
                "摄像头已打开 device=%s requested=%dx%d@%s/%s actual=%dx%d@%.2f/%s",
                self.config.device,
                self.config.width,
                self.config.height,
                self.config.fps,
                self.config.fourcc,
                actual_width,
                actual_height,
                actual_fps,
                actual_fourcc,
            )
            return capture
        except Exception as exc:
            now = time.monotonic()
            if now - self._last_open_log >= 2.0:
                LOG.error("打开摄像头异常: %s", exc)
                self._last_open_log = now
            return None

    def start(self) -> None:
        """启动采集线程；已运行时安全地保持幂等。"""

        if self.is_running():
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("旧摄像头线程尚未退出")
        with self._lock:
            self._latest = None
            self._timestamps.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="camera-capture",
            daemon=True,
        )
        self._thread.start()

    def _release_owned_capture(self) -> None:
        capture = self._capture
        self._capture = None
        if capture is not None:
            try:
                capture.release()
            except Exception:
                LOG.exception("释放摄像头失败")

    def _run(self) -> None:
        failures = 0
        try:
            while not self._stop_event.is_set():
                if self._capture is None:
                    self._capture = self._open_capture()
                    if self._capture is None:
                        self._stop_event.wait(0.25)
                        continue
                try:
                    ok, image = self._capture.read()
                except Exception as exc:
                    LOG.warning("读取摄像头异常: %s", exc)
                    ok, image = False, None
                if not ok or image is None:
                    with self._lock:
                        self._frames_failed += 1
                    failures += 1
                    if failures >= self.config.reconnect_after_failures:
                        LOG.warning("连续 %d 帧读取失败，采集线程将重连", failures)
                        self._release_owned_capture()
                        with self._lock:
                            self._reconnects += 1
                        failures = 0
                    self._stop_event.wait(0.01)
                    continue
                failures = 0
                now = time.monotonic()
                with self._lock:
                    self._frame_id += 1
                    self._latest = FramePacket(self._frame_id, now, image)
                    self._frames_ok += 1
                    self._timestamps.append(now)
                    del self._timestamps[:-120]
        finally:
            self._release_owned_capture()

    def stop(self, timeout: float = 2.0) -> None:
        """请求线程退出并等待；主线程绝不抢占释放采集句柄。"""

        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            LOG.error("摄像头线程在 %.2f 秒内未退出", timeout)
            return
        self._thread = None

    def is_running(self) -> bool:
        """返回采集线程是否存活。"""

        return bool(self._thread and self._thread.is_alive())

    def get_latest_frame(self, copy_image: bool = False) -> FramePacket | None:
        """线程安全获取最新帧；可选择复制图像数据。"""

        with self._lock:
            if self._latest is None:
                return None
            packet = self._latest
            image = packet.image.copy() if copy_image else packet.image
            return FramePacket(packet.frame_id, packet.capture_timestamp, image)

    def get_statistics(self) -> dict[str, int | float | str | None]:
        """返回线程安全的采集统计快照。"""

        with self._lock:
            timestamps = list(self._timestamps)
            latest = self._latest
            actual_fps = (
                (len(timestamps) - 1) / (timestamps[-1] - timestamps[0])
                if len(timestamps) > 1 and timestamps[-1] > timestamps[0]
                else 0.0
            )
            return {
                "frames_ok": self._frames_ok,
                "frames_failed": self._frames_failed,
                "actual_fps": actual_fps,
                "last_timestamp": latest.capture_timestamp if latest else None,
                "latest_frame_age_s": (
                    max(0.0, time.monotonic() - latest.capture_timestamp) if latest else None
                ),
                "actual_width": self._actual_width,
                "actual_height": self._actual_height,
                "device_reported_fps": self._actual_fps,
                "actual_fourcc": self._actual_fourcc,
                "reconnects": self._reconnects,
            }
