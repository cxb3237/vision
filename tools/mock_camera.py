"""用视频文件或图片目录模拟 CameraService。"""

from __future__ import annotations

from pathlib import Path
import time

import cv2

from core.models import FramePacket


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}


class MockCamera:
    """同步读取模拟源，支持循环、源 FPS 和结束状态。"""

    def __init__(
        self,
        source: str,
        fps: float = 30.0,
        loop: bool = True,
        realtime: bool = True,
    ) -> None:
        self.source = Path(source)
        self.configured_fps = fps
        self.fps = fps
        self.loop = loop
        self.realtime = realtime
        self._capture: cv2.VideoCapture | None = None
        self._files: list[Path] = []
        self._index = 0
        self._frame_id = 0
        self._last_frame_time = 0.0
        self._running = False
        self._finished = False
        self._frames_ok = 0
        self._frames_failed = 0

    def start(self) -> None:
        """验证并打开模拟源；无效视频或空目录立即报错。"""

        if self._running:
            return
        self._finished = False
        self._index = 0
        if self.source.is_dir():
            self._files = sorted(
                path for path in self.source.iterdir() if path.suffix.lower() in IMAGE_EXTENSIONS
            )
            if not self._files:
                raise ValueError(f"模拟相机图片目录为空: {self.source}")
            self._files = [path for path in self._files if cv2.imread(str(path)) is not None]
            if not self._files:
                raise ValueError(f"模拟相机目录没有可读取的图片: {self.source}")
        elif self.source.is_file():
            capture = cv2.VideoCapture(str(self.source))
            if not capture.isOpened():
                capture.release()
                raise ValueError(f"无法打开模拟视频: {self.source}")
            source_fps = float(capture.get(cv2.CAP_PROP_FPS))
            if source_fps > 0:
                self.fps = source_fps
            else:
                self.fps = self.configured_fps
            self._capture = capture
        else:
            raise ValueError(f"模拟相机源不存在: {self.source}")
        self._running = True

    def stop(self) -> None:
        """释放视频句柄并停止读取；可安全重复调用。"""

        capture = self._capture
        self._capture = None
        if capture is not None:
            capture.release()
        self._running = False

    def _pace(self) -> None:
        if not self.realtime or not self._last_frame_time or self.fps <= 0:
            return
        elapsed = time.monotonic() - self._last_frame_time
        if elapsed < 1.0 / self.fps:
            time.sleep(1.0 / self.fps - elapsed)

    def _read_directory(self):
        while True:
            if self._index >= len(self._files):
                if not self.loop:
                    return False, None
                self._index = 0
            image = cv2.imread(str(self._files[self._index]))
            self._index += 1
            if image is not None:
                return True, image
            self._frames_failed += 1
            if not self.loop and self._index >= len(self._files):
                return False, None

    def _read_video(self):
        if self._capture is None:
            return False, None
        ok, image = self._capture.read()
        if ok and image is not None:
            return True, image
        self._frames_failed += 1
        if not self.loop:
            return False, None
        self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, image = self._capture.read()
        if not ok or image is None:
            return False, None
        return True, image

    def get_latest_frame(self, copy_image: bool = False) -> FramePacket | None:
        """读取下一帧；源结束返回 ``None``，不递归也不空转。"""

        if not self._running or self._finished:
            return None
        self._pace()
        if self._files:
            ok, image = self._read_directory()
        else:
            ok, image = self._read_video()
        if not ok or image is None:
            self._finished = True
            self._running = False
            return None
        self._frame_id += 1
        self._frames_ok += 1
        self._last_frame_time = time.monotonic()
        if copy_image:
            image = image.copy()
        return FramePacket(self._frame_id, self._last_frame_time, image)

    def is_running(self) -> bool:
        """返回模拟源是否仍可读取。"""

        return self._running

    def is_finished(self) -> bool:
        """返回非循环源是否已到末尾。"""

        return self._finished

    def get_statistics(self) -> dict[str, int | float | bool]:
        """返回与 CameraService 相似的统计快照。"""

        return {
            "frames_ok": self._frames_ok,
            "frames_failed": self._frames_failed,
            "actual_fps": self.fps,
            "reconnects": 0,
            "finished": self._finished,
        }
