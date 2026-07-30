"""Thread-safe competition MP4 recorder with timestamp-based resampling."""

from __future__ import annotations

from collections import deque
from enum import Enum
import json
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any

import cv2
import numpy as np


class RecordingState(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RECORDING = "RECORDING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class CompetitionRecorder:
    """Record frames from the existing camera stream without owning a camera."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.directory = config.recording_directory
        self._condition = threading.Condition()
        self._queue: deque[tuple[int, float, np.ndarray]] = deque()
        self._thread: threading.Thread | None = None
        self._shutdown_requested = False
        self._accept_sessions = False
        self._state = RecordingState.IDLE
        self._writer: Any = None
        self._temporary_path: Path | None = None
        self._file_name = ""
        self._codec = ""
        self._error = ""
        self._started_wall_time = 0.0
        self._ended_wall_time = 0.0
        self._first_capture_timestamp: float | None = None
        self._last_capture_timestamp: float | None = None
        self._last_submit_monotonic: float | None = None
        self._stop_capture_timestamp: float | None = None
        self._first_frame_id: int | None = None
        self._last_frame_id: int | None = None
        self._last_image: np.ndarray | None = None
        self._resolution: tuple[int, int] | None = None
        self._source_frame_count = 0
        self._written_frames = 0
        self._duplicated_frames = 0
        self._dropped_frames = 0

    def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._shutdown_requested = False
            self._accept_sessions = True
            self._thread = threading.Thread(
                target=self._run, name="competition-recorder", daemon=True
            )
            self._thread.start()

    def stop_accepting_new_sessions(self) -> None:
        with self._condition:
            self._accept_sessions = False

    def storage_status(self) -> dict[str, int]:
        try:
            free = shutil.disk_usage(self.directory).free
        except OSError:
            free = 0
        return {"free_bytes": int(free), "free_mb": int(free // 1_048_576)}

    def _status_locked(self) -> dict[str, Any]:
        active = self._state in {
            RecordingState.STARTING,
            RecordingState.RECORDING,
            RecordingState.STOPPING,
        }
        elapsed = max(0.0, time.time() - self._started_wall_time) if active else 0.0
        timeline_duration = self._written_frames / self.config.recording_fps
        duration = elapsed if active else timeline_duration
        actual_fps = self._written_frames / duration if duration > 0 else 0.0
        return {
            "state": self._state.value,
            "active": active,
            "file_name": self._file_name,
            "duration_s": duration,
            "written_frames": self._written_frames,
            "source_frame_count": self._source_frame_count,
            "duplicated_frames": self._duplicated_frames,
            "dropped_frames": self._dropped_frames,
            "actual_fps": actual_fps,
            "codec": self._codec,
            "error": self._error,
        }

    def status(self) -> dict[str, Any]:
        with self._condition:
            return self._status_locked()

    def _reset_session_locked(self) -> None:
        self._queue.clear()
        self._writer = None
        self._temporary_path = None
        self._codec = ""
        self._error = ""
        self._ended_wall_time = 0.0
        self._first_capture_timestamp = None
        self._last_capture_timestamp = None
        self._last_submit_monotonic = None
        self._stop_capture_timestamp = None
        self._first_frame_id = None
        self._last_frame_id = None
        self._last_image = None
        self._resolution = None
        self._source_frame_count = 0
        self._written_frames = 0
        self._duplicated_frames = 0
        self._dropped_frames = 0

    def request_start(self) -> dict[str, Any]:
        with self._condition:
            if not self._accept_sessions:
                raise RuntimeError("录像服务停止中")
            if self._state in {
                RecordingState.STARTING,
                RecordingState.RECORDING,
                RecordingState.STOPPING,
            }:
                return self._status_locked()
        try:
            free = shutil.disk_usage(self.directory).free
        except OSError as exc:
            with self._condition:
                self._state = RecordingState.ERROR
                self._error = f"无法读取磁盘空间: {exc}"
                return self._status_locked()
        with self._condition:
            if free < self.config.minimum_free_space_mb * 1_048_576:
                self._state = RecordingState.ERROR
                self._error = "磁盘空间不足"
                return self._status_locked()
            self._reset_session_locked()
            base = f"{self.config.filename_prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
            self._file_name = f"{base}.mp4"
            suffix = 1
            while (
                (self.directory / self._file_name).exists()
                or (self.directory / f".{Path(self._file_name).stem}.tmp.mp4").exists()
            ):
                self._file_name = f"{base}_{suffix:02d}.mp4"
                suffix += 1
            self._started_wall_time = time.time()
            self._state = RecordingState.STARTING
            self._condition.notify_all()
            return self._status_locked()

    def request_stop(self) -> dict[str, Any]:
        with self._condition:
            if self._state not in {RecordingState.STARTING, RecordingState.RECORDING}:
                result = self._status_locked()
                result["result"] = "ALREADY_STOPPED"
                return result
            self._state = RecordingState.STOPPING
            if self._last_capture_timestamp is not None and self._last_submit_monotonic is not None:
                self._stop_capture_timestamp = self._last_capture_timestamp + max(
                    0.0, time.monotonic() - self._last_submit_monotonic
                )
            self._condition.notify_all()
            return self._status_locked()

    def submit_frame(self, frame_id: int, capture_timestamp: float, image: np.ndarray) -> bool:
        if not isinstance(image, np.ndarray) or image.ndim not in (2, 3):
            raise ValueError("录像帧必须是OpenCV图像")
        if not isinstance(capture_timestamp, (int, float)):
            raise ValueError("capture_timestamp 必须是数值")
        with self._condition:
            if self._state not in {RecordingState.STARTING, RecordingState.RECORDING}:
                return False
            if self._last_frame_id is not None and frame_id <= self._last_frame_id:
                return False
            self._last_frame_id = int(frame_id)
            self._last_capture_timestamp = float(capture_timestamp)
            self._last_submit_monotonic = time.monotonic()
            self._source_frame_count += 1
            if len(self._queue) >= self.config.queue_size:
                self._queue.popleft()
                self._dropped_frames += 1
            self._queue.append((int(frame_id), float(capture_timestamp), image.copy()))
            self._condition.notify_all()
            return True

    def _open_writer(self, image: np.ndarray) -> bool:
        height, width = image.shape[:2]
        temporary = self.directory / f".{Path(self._file_name).stem}.tmp.mp4"
        temporary.unlink(missing_ok=True)
        for codec in self.config.codec_candidates:
            writer = cv2.VideoWriter(
                str(temporary),
                cv2.VideoWriter_fourcc(*codec),
                self.config.recording_fps,
                (width, height),
            )
            if writer.isOpened():
                with self._condition:
                    self._writer = writer
                    self._temporary_path = temporary
                    self._codec = codec
                    self._resolution = (width, height)
                return True
            writer.release()
            temporary.unlink(missing_ok=True)
        with self._condition:
            self._state = RecordingState.ERROR
            self._error = "所有 codec 均失败"
            self._queue.clear()
        return False

    def _write(self, image: np.ndarray, *, duplicate: bool = False) -> None:
        self._writer.write(image)
        with self._condition:
            self._written_frames += 1
            if duplicate:
                self._duplicated_frames += 1

    def _consume(self, item: tuple[int, float, np.ndarray]) -> None:
        frame_id, capture_timestamp, image = item
        if self._writer is None and not self._open_writer(image):
            return
        with self._condition:
            if self._first_capture_timestamp is None:
                self._first_capture_timestamp = capture_timestamp
                self._first_frame_id = frame_id
                first = True
            else:
                first = False
                target_count = max(
                    1,
                    int(round((capture_timestamp - self._first_capture_timestamp) * self.config.recording_fps)),
                )
                target_index = target_count - 1
                current_index = self._written_frames - 1
                previous = self._last_image
        if first:
            self._write(image)
        elif target_index <= current_index:
            with self._condition:
                self._dropped_frames += 1
            return
        else:
            while current_index + 1 < target_index and previous is not None:
                self._write(previous, duplicate=True)
                current_index += 1
            self._write(image)
        with self._condition:
            self._last_image = image
            if self._state == RecordingState.STARTING:
                self._state = RecordingState.RECORDING

    def _pad_final_interval(self) -> None:
        with self._condition:
            first_timestamp = self._first_capture_timestamp
            stop_timestamp = self._stop_capture_timestamp or self._last_capture_timestamp
            last_image = self._last_image
            current_index = self._written_frames - 1
        if first_timestamp is None or stop_timestamp is None or last_image is None:
            return
        target_count = max(
            1,
            int(round((stop_timestamp - first_timestamp) * self.config.recording_fps)),
        )
        target_index = max(current_index, target_count - 1)
        while current_index < target_index:
            self._write(last_image, duplicate=True)
            current_index += 1

    def _metadata_locked(self, completed: bool) -> dict[str, Any]:
        return {
            "file_name": self._file_name,
            "started_wall_time": self._started_wall_time,
            "ended_wall_time": self._ended_wall_time,
            "duration_s": self._written_frames / self.config.recording_fps,
            "container_fps": self.config.recording_fps,
            "source_frame_count": self._source_frame_count,
            "written_frames": self._written_frames,
            "duplicated_frames": self._duplicated_frames,
            "dropped_frames": self._dropped_frames,
            "first_frame_id": self._first_frame_id,
            "last_frame_id": self._last_frame_id,
            "resolution": list(self._resolution) if self._resolution else None,
            "codec": self._codec,
            "completed": completed,
            "error": self._error,
        }

    def _finish(self) -> None:
        if self._writer is not None:
            self._pad_final_interval()
            self._writer.release()
        with self._condition:
            self._writer = None
            self._ended_wall_time = time.time()
            temporary = self._temporary_path
            final = self.directory / self._file_name if self._file_name else None
            completed = bool(temporary and temporary.exists() and self._written_frames and not self._error)
            metadata = self._metadata_locked(completed)
        if completed and temporary is not None and final is not None:
            os.replace(temporary, final)
            metadata_temporary = final.with_suffix(".json.tmp")
            with metadata_temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(metadata, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(metadata_temporary, final.with_suffix(".json"))
        elif temporary is not None:
            temporary.unlink(missing_ok=True)
        with self._condition:
            self._temporary_path = None
            self._queue.clear()
            self._state = RecordingState.ERROR if self._error else RecordingState.IDLE
            self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: bool(self._queue)
                    or self._state == RecordingState.STOPPING
                    or self._shutdown_requested,
                    timeout=0.2,
                )
                item = self._queue.popleft() if self._queue else None
                should_finish = (
                    item is None
                    and self._state == RecordingState.STOPPING
                )
                should_exit = item is None and self._shutdown_requested
            if item is not None:
                self._consume(item)
            elif should_finish:
                self._finish()
            elif should_exit:
                break
        with self._condition:
            active = self._state in {
                RecordingState.STARTING,
                RecordingState.RECORDING,
                RecordingState.STOPPING,
            }
        if active:
            self._finish()

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._accept_sessions = False
            if self._state in {RecordingState.STARTING, RecordingState.RECORDING}:
                self._state = RecordingState.STOPPING
                if self._last_capture_timestamp is not None:
                    self._stop_capture_timestamp = self._last_capture_timestamp
            self._shutdown_requested = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._condition:
            if thread is not None and not thread.is_alive():
                self._thread = None

    def list_recordings(self) -> list[dict[str, Any]]:
        recordings: list[dict[str, Any]] = []
        try:
            candidates = list(self.directory.glob("*.mp4"))
        except OSError:
            return recordings
        for path in candidates:
            if path.is_symlink() or path.name.startswith(".") or not path.is_file():
                continue
            metadata: dict[str, Any] = {}
            sidecar = path.with_suffix(".json")
            try:
                if sidecar.is_file() and not sidecar.is_symlink():
                    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                stat = path.stat()
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            recordings.append(
                {
                    "file_name": path.name,
                    "created_time": stat.st_mtime,
                    "duration_s": float(metadata.get("duration_s", 0.0)),
                    "size_bytes": stat.st_size,
                    "written_frames": int(metadata.get("written_frames", 0)),
                    "dropped_frames": int(metadata.get("dropped_frames", 0)),
                    "duplicated_frames": int(metadata.get("duplicated_frames", 0)),
                    "resolution": metadata.get("resolution"),
                    "fps": float(metadata.get("container_fps", 0.0)),
                    "codec": str(metadata.get("codec", "")),
                    "completed": bool(metadata.get("completed", False)),
                    "play_url": f"/recordings/{path.name}",
                    "download_url": f"/recordings/{path.name}?download=1",
                }
            )
        return sorted(recordings, key=lambda item: item["created_time"], reverse=True)
