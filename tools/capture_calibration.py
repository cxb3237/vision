"""交互式棋盘格标定图片采集工具。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import time
from typing import Any, TextIO

import cv2
import numpy as np

from core.config_loader import load_camera_config
from core.models import FramePacket
from drivers.camera_service import CameraService


WINDOW_NAME = "Calibration Capture"
CALIBRATION_FILE_PATTERN = re.compile(r"^calib_(\d+)\.jpg$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class QualityResult:
    """当前帧是否允许保存以及不允许时的明确原因。"""

    ready: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SessionImage:
    """本次运行期间保存且允许删除的图片记录。"""

    path: Path
    frame_id: int


def calculate_blur_score(gray: np.ndarray) -> float:
    """使用拉普拉斯方差计算灰度图清晰度。"""

    if not isinstance(gray, np.ndarray) or gray.size == 0:
        raise ValueError("清晰度计算需要非空灰度图")
    if gray.ndim != 2:
        raise ValueError("清晰度计算输入必须是单通道灰度图")
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def calculate_board_area_ratio(
    corners: np.ndarray,
    image_width: int,
    image_height: int,
) -> float:
    """计算全部角点外接矩形面积占图像面积的比例。"""

    if image_width <= 0 or image_height <= 0:
        raise ValueError("图像宽高必须为正数")
    points = np.asarray(corners, dtype=np.float32).reshape(-1, 2)
    if len(points) < 2:
        return 0.0
    minimum = points.min(axis=0)
    maximum = points.max(axis=0)
    board_width = max(0.0, float(maximum[0] - minimum[0]))
    board_height = max(0.0, float(maximum[1] - minimum[1]))
    image_area = float(image_width * image_height)
    return min(1.0, board_width * board_height / image_area)


def evaluate_capture_quality(
    found: bool,
    blur_score: float,
    board_area_ratio: float,
    frame_id: int,
    last_saved_frame_id: int | None,
    min_blur: float,
    min_board_area_ratio: float,
    force_save: bool = False,
) -> QualityResult:
    """按固定顺序检查完整角点、清晰度、面积和重复帧。"""

    if not found:
        return QualityResult(False, "NOT FOUND")
    if frame_id == last_saved_frame_id:
        return QualityResult(False, "DUPLICATE FRAME")
    if not force_save and blur_score < min_blur:
        return QualityResult(False, "TOO BLURRY")
    if not force_save and board_area_ratio < min_board_area_ratio:
        return QualityResult(False, "BOARD TOO SMALL")
    return QualityResult(True, "READY")


def scan_calibration_images(output_dir: Path) -> list[Path]:
    """扫描并按数字编号排序已有 calib_*.jpg。"""

    if not output_dir.exists():
        return []
    numbered: list[tuple[int, Path]] = []
    for path in output_dir.iterdir():
        match = CALIBRATION_FILE_PATTERN.match(path.name)
        if path.is_file() and match:
            numbered.append((int(match.group(1)), path))
    return [path for _, path in sorted(numbered)]


def next_calibration_index(output_dir: Path) -> int:
    """返回大于所有已有编号的下一个编号。"""

    existing = scan_calibration_images(output_dir)
    if not existing:
        return 1
    match = CALIBRATION_FILE_PATTERN.match(existing[-1].name)
    assert match is not None
    return int(match.group(1)) + 1


def build_argument_parser() -> argparse.ArgumentParser:
    """创建标定图片采集参数解析器。"""

    parser = argparse.ArgumentParser(description="交互式采集棋盘格标定图片")
    parser.add_argument("--device", help="摄像头编号或 /dev/video0；默认使用 camera.yaml")
    parser.add_argument(
        "--camera-config",
        default="config/camera.yaml",
        help="摄像头配置，默认 config/camera.yaml",
    )
    parser.add_argument(
        "--output-dir",
        default="data/calibration/images",
        help="输出目录，默认 data/calibration/images",
    )
    parser.add_argument("--cols", type=int, required=True, help="棋盘格内部角点列数")
    parser.add_argument("--rows", type=int, required=True, help="棋盘格内部角点行数")
    parser.add_argument("--max-images", type=int, default=25, help="建议采集数量，默认 25")
    parser.add_argument(
        "--min-blur",
        type=float,
        default=80.0,
        help="拉普拉斯方差清晰度阈值，默认 80",
    )
    parser.add_argument(
        "--min-board-area-ratio",
        type=float,
        default=0.08,
        help="棋盘角点外接矩形最小画面占比，默认 0.08",
    )
    parser.add_argument(
        "--force-save",
        action="store_true",
        help="允许忽略清晰度和棋盘面积阈值，但仍要求完整角点且不是重复帧",
    )
    return parser


def _parse_device(value: str | None) -> str | int | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.cols <= 0 or args.rows <= 0:
        raise SystemExit("--cols 和 --rows 必须为正数")
    if args.max_images <= 0:
        raise SystemExit("--max-images 必须为正数")
    if not math.isfinite(args.min_blur) or args.min_blur < 0:
        raise SystemExit("--min-blur 必须为有限非负数")
    if (
        not math.isfinite(args.min_board_area_ratio)
        or not 0 <= args.min_board_area_ratio <= 1
    ):
        raise SystemExit("--min-board-area-ratio 必须在 0..1 范围内")


def detect_chessboard_corners(
    gray: np.ndarray,
    pattern_size: tuple[int, int],
) -> tuple[bool, np.ndarray | None]:
    """优先使用 SB 检测器，不可用时回退到经典检测和亚像素优化。"""

    expected_count = pattern_size[0] * pattern_size[1]
    sb_detector = getattr(cv2, "findChessboardCornersSB", None)
    if callable(sb_detector):
        try:
            flags = cv2.CALIB_CB_NORMALIZE_IMAGE
            found, corners = sb_detector(gray, pattern_size, flags=flags)
            complete = bool(found and corners is not None and len(corners) == expected_count)
            return complete, corners if complete else None
        except (AttributeError, cv2.error):
            pass
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    found, corners = cv2.findChessboardCorners(gray, pattern_size, flags)
    complete = bool(found and corners is not None and len(corners) == expected_count)
    if not complete:
        return False, None
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        30,
        0.001,
    )
    refined = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    return len(refined) == expected_count, refined


def _draw_status_lines(
    image: np.ndarray,
    lines: list[tuple[str, tuple[int, int, int]]],
) -> None:
    for index, (text, color) in enumerate(lines):
        cv2.putText(
            image,
            text,
            (12, 28 + index * 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
            cv2.LINE_AA,
        )


def _render_frame(
    frame: FramePacket,
    pattern_size: tuple[int, int],
    corners: np.ndarray | None,
    found: bool,
    blur_score: float,
    board_area_ratio: float,
    quality: QualityResult,
    saved_count: int,
    max_images: int,
    min_blur: float,
    min_board_area_ratio: float,
) -> np.ndarray:
    image = frame.image.copy()
    expected_count = pattern_size[0] * pattern_size[1]
    corner_count = len(corners) if found and corners is not None else 0
    if found and corners is not None:
        cv2.drawChessboardCorners(image, pattern_size, corners, True)
    status_color = (0, 220, 0) if found else (0, 0, 255)
    ready_color = (0, 220, 0) if quality.ready else (0, 0, 255)
    lines = [
        (f"Pattern: {pattern_size[0]}x{pattern_size[1]}", (255, 255, 255)),
        (f"Corners: {corner_count}/{expected_count}", status_color),
        (f"Blur: {blur_score:.1f} / {min_blur:.1f}", (255, 255, 255)),
        (
            f"Board area: {board_area_ratio:.1%} / {min_board_area_ratio:.1%}",
            (255, 255, 255),
        ),
        (f"Saved: {saved_count} / {max_images}", (255, 255, 255)),
        (quality.reason, ready_color),
    ]
    _draw_status_lines(image, lines)
    if saved_count >= max_images:
        cv2.putText(
            image,
            "ENOUGH IMAGES - press Q to finish or continue capturing",
            (12, image.shape[0] - 48),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
    cv2.putText(
        image,
        "SPACE/S: save    D: delete last    Q: quit",
        (12, image.shape[0] - 16),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    return image


def _append_metadata(stream: TextIO, record: dict[str, Any]) -> None:
    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    stream.flush()


def _save_frame(
    frame: FramePacket,
    output_dir: Path,
    next_index: int,
    metadata_stream: TextIO,
    args: argparse.Namespace,
    blur_score: float,
    board_area_ratio: float,
) -> tuple[SessionImage, int]:
    while True:
        path = output_dir / f"calib_{next_index:04d}.jpg"
        if not path.exists():
            break
        next_index += 1
    if not cv2.imwrite(str(path), frame.image):
        raise OSError(f"cv2.imwrite 保存失败: {path}")
    height, width = frame.image.shape[:2]
    _append_metadata(
        metadata_stream,
        {
            "record_type": "save",
            "file_name": path.name,
            "frame_id": frame.frame_id,
            "capture_timestamp": frame.capture_timestamp,
            "saved_wall_time": time.time(),
            "width": width,
            "height": height,
            "cols": args.cols,
            "rows": args.rows,
            "corners_found": args.cols * args.rows,
            "blur_score": blur_score,
            "board_area_ratio": board_area_ratio,
        },
    )
    print(
        f"SAVED {path.name} blur={blur_score:.2f} "
        f"area_ratio={board_area_ratio:.4f}"
    )
    return SessionImage(path, frame.frame_id), next_index + 1


def main(argv: list[str] | None = None) -> int:
    """运行交互式采集，按 q/Ctrl+C 退出并可靠停止 CameraService。"""

    args = build_argument_parser().parse_args(argv)
    _validate_arguments(args)
    overrides = {"device": _parse_device(args.device)}
    camera_config = load_camera_config(args.camera_config, overrides)
    camera = CameraService(camera_config)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    existing_count = len(scan_calibration_images(output_dir))
    next_index = next_calibration_index(output_dir)
    metadata_path = output_dir / "metadata.jsonl"
    session_images: list[SessionImage] = []
    last_processed_frame_id: int | None = None
    current_frame: FramePacket | None = None
    current_quality = QualityResult(False, "NOT FOUND")
    current_blur = 0.0
    current_area_ratio = 0.0
    pattern_size = (args.cols, args.rows)
    try:
        camera.start()
        cv2.namedWindow(WINDOW_NAME)
        with metadata_path.open("a", encoding="utf-8") as metadata_stream:
            while True:
                frame = camera.get_latest_frame(copy_image=False)
                if frame is None or frame.frame_id == last_processed_frame_id:
                    key = cv2.waitKey(5) & 0xFF
                    if key == ord("q"):
                        break
                    time.sleep(0.002)
                    continue
                last_processed_frame_id = frame.frame_id
                current_frame = frame
                gray = cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY)
                found, corners = detect_chessboard_corners(gray, pattern_size)
                current_blur = calculate_blur_score(gray)
                current_area_ratio = (
                    calculate_board_area_ratio(corners, frame.image.shape[1], frame.image.shape[0])
                    if found and corners is not None
                    else 0.0
                )
                last_saved_id = session_images[-1].frame_id if session_images else None
                current_quality = evaluate_capture_quality(
                    found,
                    current_blur,
                    current_area_ratio,
                    frame.frame_id,
                    last_saved_id,
                    args.min_blur,
                    args.min_board_area_ratio,
                    args.force_save,
                )
                saved_count = existing_count + len(session_images)
                display = _render_frame(
                    frame,
                    pattern_size,
                    corners,
                    found,
                    current_blur,
                    current_area_ratio,
                    current_quality,
                    saved_count,
                    args.max_images,
                    args.min_blur,
                    args.min_board_area_ratio,
                )
                cv2.imshow(WINDOW_NAME, display)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key in (ord("s"), ord(" ")):
                    if not current_quality.ready:
                        print(f"NOT SAVED: {current_quality.reason}")
                    else:
                        saved, next_index = _save_frame(
                            current_frame,
                            output_dir,
                            next_index,
                            metadata_stream,
                            args,
                            current_blur,
                            current_area_ratio,
                        )
                        session_images.append(saved)
                elif key == ord("d"):
                    if not session_images:
                        print("DELETE SKIPPED: no image saved in this session")
                    else:
                        deleted = session_images.pop()
                        try:
                            deleted.path.unlink()
                        except OSError as exc:
                            session_images.append(deleted)
                            print(f"DELETE FAILED {deleted.path.name}: {exc}")
                        else:
                            _append_metadata(
                                metadata_stream,
                                {
                                    "record_type": "delete",
                                    "file_name": deleted.path.name,
                                    "frame_id": deleted.frame_id,
                                    "deleted_wall_time": time.time(),
                                },
                            )
                            print(f"DELETED {deleted.path.name}")
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        camera.stop()
        cv2.destroyAllWindows()
        print(f"本次成功保存数量: {len(session_images)}")


if __name__ == "__main__":
    raise SystemExit(main())
