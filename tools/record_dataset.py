"""从 CameraService 录制去重视频或定时图片及增量 JSONL 元数据。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import time
from typing import TextIO

import cv2

from core.config_loader import load_camera_config
from core.models import CameraConfig, FramePacket
from drivers.camera_service import CameraService


class FrameDeduplicator:
    """只接受不同的 frame_id，防止轮询重复写同一帧。"""

    def __init__(self) -> None:
        self.last_frame_id: int | None = None

    def accept(self, frame: FramePacket | None) -> bool:
        """新帧返回 ``True`` 并更新状态。"""

        if frame is None or frame.frame_id == self.last_frame_id:
            return False
        self.last_frame_id = frame.frame_id
        return True


def validate_video_resolution(
    frame: FramePacket,
    expected: tuple[int, int],
) -> None:
    """后续帧尺寸变化时明确报错，避免损坏视频。"""

    actual = (frame.image.shape[1], frame.image.shape[0])
    if actual != expected:
        raise RuntimeError(f"视频帧分辨率发生变化: expected={expected}, actual={actual}")


def build_argument_parser() -> argparse.ArgumentParser:
    """创建录制工具参数解析器。"""

    parser = argparse.ArgumentParser(description="录制无重复帧的视频或图片数据集")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", help="输出视频路径")
    group.add_argument("--images", help="输出图片目录")
    parser.add_argument("--config", default="config/camera.yaml", help="摄像头 YAML 配置")
    parser.add_argument("--fps", type=float, help="覆盖视频容器 FPS")
    parser.add_argument("--interval", type=float, default=0.0, help="图片最小保存间隔秒数")
    parser.add_argument("--seconds", type=float, default=0.0, help="自动停止秒数，0 表示不限")
    parser.add_argument("--max-frames", type=int, default=0, help="最多保存帧数，0 表示不限")
    parser.add_argument("--startup-timeout", type=float, default=5.0, help="等待首帧超时秒数")
    parser.add_argument("--frame-timeout", type=float, default=2.0, help="运行中等待新帧超时秒数")
    return parser


def _metadata_path(args: argparse.Namespace) -> Path:
    target = Path(args.images) if args.images else Path(args.output)
    if args.images:
        return target / "metadata.jsonl"
    return target.with_suffix(target.suffix + ".jsonl")


def _choose_fps(config: CameraConfig, service: CameraService, override: float | None) -> float:
    if override is not None:
        if not math.isfinite(override) or override <= 0:
            raise ValueError("--fps 必须为有限正数")
        return override
    reported = float(service.get_statistics().get("device_reported_fps") or 0.0)
    return reported if math.isfinite(reported) and reported > 0 else float(config.fps)


def _write_jsonl(stream: TextIO, row: dict[str, object]) -> None:
    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    """运行录制；首帧、运行中断帧和尺寸变化均会明确失败。"""

    args = build_argument_parser().parse_args(argv)
    numeric = (args.interval, args.seconds, args.startup_timeout, args.frame_timeout)
    if any(not math.isfinite(value) or value < 0 for value in numeric):
        raise SystemExit("interval、seconds 和超时参数必须为有限非负数")
    if args.startup_timeout == 0 or args.frame_timeout == 0:
        raise SystemExit("startup-timeout 和 frame-timeout 必须大于 0")
    if args.max_frames < 0:
        raise SystemExit("max-frames 不能为负数")
    config = load_camera_config(args.config)
    service = CameraService(config)
    output_path = Path(args.images or args.output)
    if args.images:
        output_path.mkdir(parents=True, exist_ok=True)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_file = _metadata_path(args)
    metadata_file.parent.mkdir(parents=True, exist_ok=True)
    writer: cv2.VideoWriter | None = None
    expected_resolution: tuple[int, int] | None = None
    deduplicator = FrameDeduplicator()
    start = time.monotonic()
    last_new_frame = start
    last_saved = float("-inf")
    saved_count = 0
    container_fps = float(args.fps or config.fps)
    service.start()
    try:
        with metadata_file.open("w", encoding="utf-8") as metadata_stream:
            _write_jsonl(
                metadata_stream,
                {
                    "record_type": "session",
                    "started_wall_time": time.time(),
                    "camera_config": asdict(config),
                    "requested_fps": config.fps,
                    "container_fps": container_fps,
                },
            )
            while True:
                now = time.monotonic()
                elapsed = now - start
                if args.seconds and elapsed >= args.seconds:
                    break
                if args.max_frames and saved_count >= args.max_frames:
                    break
                frame = service.get_latest_frame(copy_image=True)
                if not deduplicator.accept(frame):
                    timeout = args.startup_timeout if saved_count == 0 else args.frame_timeout
                    reference = start if saved_count == 0 else last_new_frame
                    if now - reference >= timeout:
                        stage = "首帧" if saved_count == 0 else "新帧"
                        raise TimeoutError(f"等待{stage}超过 {timeout:.2f} 秒")
                    time.sleep(0.002)
                    continue
                assert frame is not None
                last_new_frame = now
                if args.images and elapsed - last_saved < args.interval:
                    continue
                height, width = frame.image.shape[:2]
                if args.images:
                    filename = f"frame_{frame.frame_id:08d}_{time.time_ns()}.jpg"
                    if not cv2.imwrite(str(output_path / filename), frame.image):
                        raise RuntimeError(f"图片写入失败: {output_path / filename}")
                    last_saved = elapsed
                else:
                    if writer is None:
                        container_fps = _choose_fps(config, service, args.fps)
                        expected_resolution = (width, height)
                        writer = cv2.VideoWriter(
                            str(output_path),
                            cv2.VideoWriter_fourcc(*"mp4v"),
                            container_fps,
                            expected_resolution,
                        )
                        if not writer.isOpened():
                            writer.release()
                            writer = None
                            raise RuntimeError(f"无法创建视频写入器: {output_path}")
                    assert expected_resolution is not None
                    validate_video_resolution(frame, expected_resolution)
                    writer.write(frame.image)
                    filename = output_path.name
                saved_count += 1
                statistics = service.get_statistics()
                _write_jsonl(
                    metadata_stream,
                    {
                        "record_type": "frame",
                        "frame_id": frame.frame_id,
                        "capture_timestamp": frame.capture_timestamp,
                        "relative_time_s": elapsed,
                        "file_name": filename,
                        "resolution": [width, height],
                        "requested_fps": config.fps,
                        "container_fps": container_fps,
                        "actual_capture_fps": statistics.get("actual_fps", 0.0),
                    },
                )
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        if writer is not None:
            writer.release()
    duration = max(time.monotonic() - start, 0.001)
    print(
        f"saved_frames={saved_count} duration_s={duration:.3f} "
        f"effective_write_fps={saved_count / duration:.3f} "
        f"container_fps={container_fps:.3f} metadata={metadata_file}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
