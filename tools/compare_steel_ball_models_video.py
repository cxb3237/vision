"""Compare two steel-ball NCNN profiles on exactly the same video frames."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
from typing import Any, Callable, Iterable

import cv2
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config_loader import ConfigError, load_steel_ball_ncnn_config
from core.models import FramePacket
from detectors.steel_ball_yolo_ncnn_detector import SteelBallYoloNcnnDetector


MODEL_FIELDS = (
    "detected",
    "confidence",
    "x1",
    "y1",
    "x2",
    "y2",
    "center_x",
    "center_y",
    "box_width",
    "box_height",
    "inference_ms",
)
FRAME_FIELDS = (
    "frame_index",
    "timestamp_ms",
    "source_fps",
    "motion_score",
    "motion_level",
    "near_duplicate_frame",
    *(f"baseline_{name}" for name in MODEL_FIELDS),
    *(f"candidate_{name}" for name in MODEL_FIELDS),
    "both_detected",
    "baseline_only",
    "candidate_only",
    "neither_detected",
    "center_x_difference_px",
    "confidence_difference",
)
FAIR_CONFIG_FIELDS = (
    "backend",
    "imgsz",
    "conf_threshold",
    "iou_threshold",
    "max_det",
    "num_threads",
    "target_class",
)


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    encoded_frame_count: int
    file_size: int


@dataclass
class PassResult:
    rows: list[dict[str, Any]]
    decoded_frame_count: int
    metadata: VideoInfo


def iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: Iterable[float], percent: float) -> float | None:
    data = sorted(float(value) for value in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return data[lower]
    return data[lower] + (data[upper] - data[lower]) * (position - lower)


def safe_mean(values: Iterable[float]) -> float | None:
    data = [float(value) for value in values]
    return statistics.fmean(data) if data else None


def ratio(numerator: int, denominator: int) -> float:
    return float(numerator) / denominator if denominator else 0.0


def validate_video_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(f"视频不存在: {path}")
    return path.resolve()


def inspect_video(path: Path) -> VideoInfo:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError(f"视频无法打开: {path}")
        width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
        height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        if width <= 0 or height <= 0:
            raise ValueError(f"视频宽高无效: {width}x{height}")
        if not math.isfinite(fps) or fps <= 0.0:
            raise ValueError(f"视频 FPS 无效: {fps}")
        ok, frame = capture.read()
        if not ok or frame is None or frame.size == 0:
            raise ValueError(f"视频没有有效帧: {path}")
    finally:
        capture.release()
    return VideoInfo(width, height, fps, max(0, frame_count), path.stat().st_size)


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write_probe.tmp"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise OSError(f"输出目录不可写: {path}: {exc}") from exc


def default_output_dir(video: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return PROJECT_ROOT / "runs/model_compare" / f"{video.stem}_{timestamp}"


def motion_score(frame: np.ndarray, previous_gray: np.ndarray | None) -> tuple[float, np.ndarray]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
    score = 0.0 if previous_gray is None else float(cv2.absdiff(small, previous_gray).mean())
    return score, small


def classify_motion(scores: Iterable[float]) -> dict[str, float]:
    values = [max(0.0, float(value)) for value in scores if math.isfinite(float(value))]
    positives = [value for value in values if value > 1e-9]
    if not positives:
        return {
            "near_zero": 0.0,
            "near_duplicate": 0.0,
            "static_max": 0.0,
            "slow_max": 0.0,
        }
    q10 = float(percentile(positives, 10) or 0.0)
    q25 = float(percentile(positives, 25) or q10)
    q65 = float(percentile(positives, 65) or q25)
    return {
        "near_zero": max(1e-9, q10 * 0.1),
        "near_duplicate": q10,
        "static_max": q25,
        "slow_max": max(q25, q65),
    }


def motion_level(score: float, thresholds: dict[str, float]) -> str:
    if score <= thresholds["near_zero"] or score <= thresholds["static_max"]:
        return "static"
    if score <= thresholds["slow_max"]:
        return "slow"
    return "fast"


def _detector_row(result: Any, detector: Any) -> dict[str, Any]:
    status = detector.get_runtime_status()
    inference_ms = float(status.get("inference_ms", 0.0))
    if not math.isfinite(inference_ms) or inference_ms < 0.0:
        raise RuntimeError(f"Detector 返回了无效 inference_ms: {inference_ms}")
    if not bool(result.found):
        return {
            "detected": 0,
            "confidence": "",
            "x1": "",
            "y1": "",
            "x2": "",
            "y2": "",
            "center_x": "",
            "center_y": "",
            "box_width": "",
            "box_height": "",
            "inference_ms": inference_ms,
        }
    x1 = int(result.bbox_x)
    y1 = int(result.bbox_y)
    width = int(result.bbox_width)
    height = int(result.bbox_height)
    return {
        "detected": 1,
        "confidence": float(result.confidence) / 1000.0,
        "x1": x1,
        "y1": y1,
        "x2": x1 + width,
        "y2": y1 + height,
        "center_x": int(result.center_x),
        "center_y": int(result.center_y),
        "box_width": width,
        "box_height": height,
        "inference_ms": inference_ms,
    }


def _read_warmup_frame(path: Path) -> np.ndarray:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError(f"预热时无法打开视频: {path}")
        while True:
            ok, frame = capture.read()
            if not ok:
                raise ValueError(f"视频没有可用于预热的有效帧: {path}")
            if frame is not None and frame.size:
                return frame
    finally:
        capture.release()


def process_model_pass(
    label: str,
    video_path: Path,
    config: Any,
    *,
    warmup: int,
    frame_step: int,
    max_frames: int,
    progress_every: int,
    detector_factory: Callable[[Any], Any] = SteelBallYoloNcnnDetector,
    expected_indices: list[int] | None = None,
    calculate_motion: bool = False,
    partial_csv_path: Path | None = None,
) -> PassResult:
    detector = detector_factory(config)
    capture: Any = None
    rows: list[dict[str, Any]] = []
    decoded = 0
    previous_gray: np.ndarray | None = None
    try:
        detector.initialize()
        if not getattr(detector, "model_loaded", False):
            raise RuntimeError(getattr(detector, "detector_error", "") or f"{label} 模型加载失败")
        warmup_frame = _read_warmup_frame(video_path)
        for index in range(warmup):
            detector.process(FramePacket(-(index + 1), time.monotonic(), warmup_frame))
            if getattr(detector, "detector_error", ""):
                raise RuntimeError(f"{label} 预热失败: {detector.detector_error}")

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise ValueError(f"{label} 处理时无法打开视频: {video_path}")
        metadata = VideoInfo(
            int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))),
            int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))),
            float(capture.get(cv2.CAP_PROP_FPS)),
            max(0, int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))),
            video_path.stat().st_size,
        )
        frame_index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if frame is None or frame.size == 0:
                raise RuntimeError(f"{label} 第 {frame_index} 帧解码为空")
            decoded += 1
            score = 0.0
            if calculate_motion:
                score, previous_gray = motion_score(frame, previous_gray)
            if frame_index % frame_step == 0:
                result = detector.process(
                    FramePacket(frame_index, frame_index / metadata.fps, frame)
                )
                if getattr(detector, "detector_error", ""):
                    raise RuntimeError(f"{label} 第 {frame_index} 帧推理失败: {detector.detector_error}")
                row = {
                    "frame_index": frame_index,
                    "timestamp_ms": frame_index * 1000.0 / metadata.fps,
                    "source_fps": metadata.fps,
                    "motion_score": score if calculate_motion else None,
                    **_detector_row(result, detector),
                }
                rows.append(row)
                if progress_every and len(rows) % progress_every == 0:
                    print(f"[{label}] 已处理 {len(rows)} 帧，源帧 {frame_index}", flush=True)
                if max_frames and len(rows) >= max_frames:
                    break
            frame_index += 1
        if not rows:
            raise ValueError(f"{label} 没有可处理的视频帧")
        indices = [int(row["frame_index"]) for row in rows]
        if expected_indices is not None and indices != expected_indices:
            raise RuntimeError(
                f"两遍解码帧索引不一致: expected={len(expected_indices)}, actual={len(indices)}"
            )
        return PassResult(rows, decoded, metadata)
    except KeyboardInterrupt:
        if partial_csv_path is not None and rows:
            write_model_partial_csv(partial_csv_path, rows)
        raise
    finally:
        if capture is not None:
            capture.release()
        detector.close()


def merge_passes(
    baseline_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    if [row["frame_index"] for row in baseline_rows] != [
        row["frame_index"] for row in candidate_rows
    ]:
        raise RuntimeError("baseline 与 candidate 处理的帧索引不一致")
    thresholds = classify_motion(row["motion_score"] for row in baseline_rows)
    merged: list[dict[str, Any]] = []
    for position, (baseline, candidate) in enumerate(
        zip(baseline_rows, candidate_rows, strict=True)
    ):
        row: dict[str, Any] = {
            "frame_index": baseline["frame_index"],
            "timestamp_ms": baseline["timestamp_ms"],
            "source_fps": baseline["source_fps"],
            "motion_score": baseline["motion_score"],
            "motion_level": motion_level(baseline["motion_score"], thresholds),
            "near_duplicate_frame": int(
                position > 0 and baseline["motion_score"] <= thresholds["near_duplicate"]
            ),
        }
        row.update({f"baseline_{name}": baseline[name] for name in MODEL_FIELDS})
        row.update({f"candidate_{name}": candidate[name] for name in MODEL_FIELDS})
        baseline_found = bool(baseline["detected"])
        candidate_found = bool(candidate["detected"])
        row.update(
            {
                "both_detected": int(baseline_found and candidate_found),
                "baseline_only": int(baseline_found and not candidate_found),
                "candidate_only": int(candidate_found and not baseline_found),
                "neither_detected": int(not baseline_found and not candidate_found),
                "center_x_difference_px": (
                    abs(float(baseline["center_x"]) - float(candidate["center_x"]))
                    if baseline_found and candidate_found
                    else ""
                ),
                "confidence_difference": (
                    float(candidate["confidence"]) - float(baseline["confidence"])
                    if baseline_found and candidate_found
                    else ""
                ),
            }
        )
        merged.append(row)
    return merged, thresholds


def missing_runs(rows: list[dict[str, Any]], prefix: str, fps: float) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    start: int | None = None
    timestamp_deltas = [
        float(current["timestamp_ms"]) - float(previous["timestamp_ms"])
        for previous, current in zip(rows, rows[1:])
        if float(current["timestamp_ms"]) > float(previous["timestamp_ms"])
    ]
    frame_interval_ms = (
        statistics.median(timestamp_deltas) if timestamp_deltas else 1000.0 / fps
    )
    for position in range(len(rows) + 1):
        detected = position < len(rows) and bool(rows[position][f"{prefix}_detected"])
        if position < len(rows) and not detected and start is None:
            start = position
        if start is not None and (position == len(rows) or detected):
            section = rows[start:position]
            start_frame = int(section[0]["frame_index"])
            end_frame = int(section[-1]["frame_index"])
            duration_ms = (
                float(section[-1]["timestamp_ms"])
                - float(section[0]["timestamp_ms"])
                + frame_interval_ms
            )
            levels = {name: 0 for name in ("static", "slow", "fast")}
            for row in section:
                levels[str(row["motion_level"])] += 1
            runs.append(
                {
                    "frames": len(section),
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "start_timestamp_ms": float(section[0]["timestamp_ms"]),
                    "end_timestamp_ms": float(section[-1]["timestamp_ms"]),
                    "duration_ms": duration_ms,
                    "mean_motion_score": safe_mean(row["motion_score"] for row in section),
                    "motion_level_counts": levels,
                }
            )
            start = None
    return sorted(runs, key=lambda item: (-item["frames"], item["start_frame"]))


def model_summary(rows: list[dict[str, Any]], prefix: str, fps: float) -> dict[str, Any]:
    total = len(rows)
    detected = [row for row in rows if row[f"{prefix}_detected"]]
    non_duplicate = [row for row in rows if not row["near_duplicate_frame"]]
    confidence = [float(row[f"{prefix}_confidence"]) for row in detected]
    inference = [float(row[f"{prefix}_inference_ms"]) for row in rows]
    centers = [float(row[f"{prefix}_center_x"]) for row in detected]
    runs = missing_runs(rows, prefix, fps)
    summary: dict[str, Any] = {
        "processed_frames": total,
        "detected_frames": len(detected),
        "detected_frame_ratio": ratio(len(detected), total),
        "non_duplicate_frames": len(non_duplicate),
        "non_duplicate_detected_frames": sum(
            bool(row[f"{prefix}_detected"]) for row in non_duplicate
        ),
        "missing_frames": total - len(detected),
        "longest_missing_run_frames": runs[0]["frames"] if runs else 0,
        "longest_missing_run_ms": runs[0]["duration_ms"] if runs else 0.0,
        "mean_confidence": safe_mean(confidence),
        "median_confidence": statistics.median(confidence) if confidence else None,
        "p95_confidence": percentile(confidence, 95),
        "mean_inference_ms": safe_mean(inference),
        "median_inference_ms": statistics.median(inference) if inference else None,
        "p95_inference_ms": percentile(inference, 95),
        "max_inference_ms": max(inference) if inference else None,
        "mean_center_x": safe_mean(centers),
        "center_x_standard_deviation": statistics.pstdev(centers) if centers else None,
        "longest_missing_runs": runs[:5],
    }
    summary["non_duplicate_detected_ratio"] = ratio(
        summary["non_duplicate_detected_frames"], summary["non_duplicate_frames"]
    )
    for level in ("static", "slow", "fast"):
        level_rows = [row for row in rows if row["motion_level"] == level]
        found = sum(bool(row[f"{prefix}_detected"]) for row in level_rows)
        summary[f"{level}_frames"] = len(level_rows)
        summary[f"{level}_detected_frames"] = found
        summary[f"{level}_detected_ratio"] = ratio(found, len(level_rows))
    return summary


def comparison_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    result: dict[str, Any] = {}
    for name in ("both_detected", "baseline_only", "candidate_only", "neither_detected"):
        count = sum(bool(row[name]) for row in rows)
        result[f"{name}_frames"] = count
        result[f"{name}_ratio"] = ratio(count, total)
    differences = [
        float(row["center_x_difference_px"])
        for row in rows
        if row["center_x_difference_px"] != ""
    ]
    result["mean_center_x_difference_px"] = safe_mean(differences)
    result["p95_center_x_difference_px"] = percentile(differences, 95)
    return result


def write_frames_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FRAME_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_model_partial_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = (
        "frame_index",
        "timestamp_ms",
        "source_fps",
        "motion_score",
        *MODEL_FIELDS,
    )
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _writer_opened(writer: Any) -> bool:
    return bool(writer is not None and writer.isOpened())


def open_video_writer(
    output_dir: Path,
    stem: str,
    fps: float,
    size: tuple[int, int],
    *,
    writer_factory: Callable[..., Any] = cv2.VideoWriter,
) -> tuple[Any, Path, str]:
    attempts = ((".mp4", "mp4v"), (".avi", "MJPG"))
    for suffix, codec in attempts:
        path = output_dir / f"{stem}{suffix}"
        writer = writer_factory(str(path), cv2.VideoWriter_fourcc(*codec), fps, size)
        if _writer_opened(writer):
            return writer, path, codec
        if writer is not None:
            writer.release()
        if path.exists():
            path.unlink()
    raise OSError(f"VideoWriter 无法打开（已尝试 mp4v MP4 和 MJPG AVI）: {stem}")


def annotate_frame(frame: np.ndarray, row: dict[str, Any], prefix: str, label: str) -> np.ndarray:
    output = frame.copy()
    found = bool(row[f"{prefix}_detected"])
    color = (40, 220, 40) if found else (30, 30, 230)
    lines = [
        f"{label} | frame={row['frame_index']} | t={float(row['timestamp_ms']):.1f}ms",
        f"motion={row['motion_level']}",
    ]
    if found:
        p1 = (int(row[f"{prefix}_x1"]), int(row[f"{prefix}_y1"]))
        p2 = (int(row[f"{prefix}_x2"]), int(row[f"{prefix}_y2"]))
        cv2.rectangle(output, p1, p2, color, 2)
        cv2.circle(
            output,
            (int(row[f"{prefix}_center_x"]), int(row[f"{prefix}_center_y"])),
            4,
            color,
            -1,
        )
        lines.append(
            f"conf={float(row[f'{prefix}_confidence']):.3f} "
            f"center_x={row[f'{prefix}_center_x']} "
            f"inference={float(row[f'{prefix}_inference_ms']):.2f}ms"
        )
    else:
        lines.append(f"NONE | inference={float(row[f'{prefix}_inference_ms']):.2f}ms")
    for index, text_line in enumerate(lines):
        cv2.putText(
            output,
            text_line,
            (12, 26 + index * 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.56,
            color,
            2,
            cv2.LINE_AA,
        )
    return output


def render_videos(
    video_path: Path,
    output_dir: Path,
    rows: list[dict[str, Any]],
    *,
    frame_step: int,
    writer_factory: Callable[..., Any] = cv2.VideoWriter,
) -> list[dict[str, Any]]:
    width = int(rows[0].get("source_width", 0))
    height = int(rows[0].get("source_height", 0))
    capture = cv2.VideoCapture(str(video_path))
    writers: list[Any] = []
    try:
        if not capture.isOpened():
            raise ValueError(f"生成标注视频时无法打开源视频: {video_path}")
        if width <= 0:
            width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
            height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        output_fps = float(rows[0]["source_fps"]) / frame_step
        definitions = (
            ("baseline_annotated", (width, height)),
            ("candidate_annotated", (width, height)),
            ("comparison_side_by_side", (width * 2, height)),
        )
        outputs: list[dict[str, Any]] = []
        for stem, size in definitions:
            writer, path, codec = open_video_writer(
                output_dir, stem, output_fps, size, writer_factory=writer_factory
            )
            writers.append(writer)
            outputs.append({"path": str(path.resolve()), "codec": codec})
        wanted = {int(row["frame_index"]): row for row in rows}
        rendered = 0
        frame_index = 0
        while rendered < len(rows):
            ok, frame = capture.read()
            if not ok:
                break
            row = wanted.get(frame_index)
            if row is not None:
                baseline = annotate_frame(frame, row, "baseline", "BASELINE")
                candidate = annotate_frame(frame, row, "candidate", "CANDIDATE")
                side = np.hstack((baseline, candidate))
                if row["baseline_only"]:
                    state = "BASELINE ONLY"
                elif row["candidate_only"]:
                    state = "CANDIDATE ONLY"
                elif row["neither_detected"]:
                    state = "BOTH NONE"
                else:
                    state = "BOTH DETECTED"
                header = (
                    f"frame={frame_index} t={float(row['timestamp_ms']):.1f}ms "
                    f"motion={row['motion_level']} | {state}"
                )
                cv2.rectangle(side, (0, 0), (side.shape[1], 34), (0, 0, 0), -1)
                cv2.putText(
                    side,
                    header,
                    (12, 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.62,
                    (255, 255, 255),
                    2,
                    cv2.LINE_AA,
                )
                writers[0].write(baseline)
                writers[1].write(candidate)
                writers[2].write(side)
                rendered += 1
            frame_index += 1
        if rendered != len(rows):
            raise RuntimeError(f"标注视频帧数不一致: expected={len(rows)}, actual={rendered}")
        return outputs
    finally:
        capture.release()
        for writer in writers:
            writer.release()


def git_value(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def build_report(summary: dict[str, Any]) -> str:
    baseline = summary["baseline"]
    candidate = summary["candidate"]
    comparison = summary["comparison"]
    duplicate = summary["duplicate_frames"]

    def fmt(value: Any, digits: int = 3) -> str:
        return "N/A" if value is None else f"{float(value):.{digits}f}"

    def model_table(name: str, data: dict[str, Any]) -> str:
        return "\n".join(
            [
                f"### {name}",
                "",
                "| 指标 | 数值 |",
                "|---|---:|",
                f"| 处理帧 | {data['processed_frames']} |",
                f"| 有检测输出帧 / 比例 | {data['detected_frames']} / {fmt(data['detected_frame_ratio'])} |",
                f"| 非近重复帧输出比例 | {fmt(data['non_duplicate_detected_ratio'])} |",
                f"| fast 帧输出比例 | {fmt(data['fast_detected_ratio'])} |",
                f"| 最长连续无输出帧 / ms | {data['longest_missing_run_frames']} / {fmt(data['longest_missing_run_ms'])} |",
                f"| 平均 / 中位 / P95 置信度 | {fmt(data['mean_confidence'])} / {fmt(data['median_confidence'])} / {fmt(data['p95_confidence'])} |",
                f"| 平均 / P50 / P95 / 最大推理 ms | {fmt(data['mean_inference_ms'])} / {fmt(data['median_inference_ms'])} / {fmt(data['p95_inference_ms'])} / {fmt(data['max_inference_ms'])} |",
                f"| center_x 均值 / 标准差 | {fmt(data['mean_center_x'])} / {fmt(data['center_x_standard_deviation'])} |",
            ]
        )

    def runs(name: str, data: dict[str, Any]) -> str:
        lines = [f"### {name}", "", "| 帧数 | 起始帧 | 结束帧 | 持续 ms | 平均 motion | static/slow/fast |", "|---:|---:|---:|---:|---:|---:|"]
        for item in data["longest_missing_runs"]:
            levels = item["motion_level_counts"]
            lines.append(
                f"| {item['frames']} | {item['start_frame']} | {item['end_frame']} | "
                f"{fmt(item['duration_ms'])} | {fmt(item['mean_motion_score'])} | "
                f"{levels['static']}/{levels['slow']}/{levels['fast']} |"
            )
        if not data["longest_missing_runs"]:
            lines.append("| 0 | - | - | 0 | - | - |")
        return "\n".join(lines)

    video_outputs = summary.get("video_outputs", [])
    side_path = next(
        (item["path"] for item in video_outputs if "side_by_side" in item["path"]),
        "未生成（--no-write-videos）",
    )
    conclusion = []
    if baseline["detected_frame_ratio"] != candidate["detected_frame_ratio"]:
        winner = "Baseline" if baseline["detected_frame_ratio"] > candidate["detected_frame_ratio"] else "Candidate"
        conclusion.append(f"- {winner} 的检测输出更连续（仅指本视频上的输出帧比例）。")
    if baseline["fast_detected_ratio"] != candidate["fast_detected_ratio"]:
        winner = "Baseline" if baseline["fast_detected_ratio"] > candidate["fast_detected_ratio"] else "Candidate"
        conclusion.append(f"- {winner} 在 fast 分组帧上输出更多。")
    if baseline["median_inference_ms"] != candidate["median_inference_ms"]:
        winner = "Baseline" if (baseline["median_inference_ms"] or math.inf) < (candidate["median_inference_ms"] or math.inf) else "Candidate"
        conclusion.append(f"- {winner} 的推理 P50 更短。")
    if baseline["longest_missing_run_frames"] != candidate["longest_missing_run_frames"]:
        winner = "Baseline" if baseline["longest_missing_run_frames"] < candidate["longest_missing_run_frames"] else "Candidate"
        conclusion.append(f"- {winner} 的最长连续无检测输出区间更短。")
    if not conclusion:
        conclusion.append("- 本次自动连续性指标未显示明确差异。")

    return f"""# 钢球双模型同视频 A/B 对比报告

## 输入视频信息

- 路径：`{summary['input_video']['path']}`
- SHA256：`{summary['input_video']['sha256']}`
- 大小：{summary['input_video']['file_size']} 字节
- 宽高：{summary['input_video']['width']} x {summary['input_video']['height']}
- 标称 FPS：{fmt(summary['input_video']['fps'])}
- 编码总帧数：{summary['input_video']['encoded_frame_count']}
- 实际解码帧数：{summary['input_video']['decoded_frame_count']}

标称 FPS 是容器元数据，不等同于摄像头真实有效帧率。

## 模型与配置

- Baseline 模型：`{summary['configs']['baseline']['model_path']}`
- Candidate 模型：`{summary['configs']['candidate']['model_path']}`
- Baseline 配置：`{summary['configs']['baseline_config_path']}`
- Candidate 配置：`{summary['configs']['candidate_config_path']}`
- 公平性参数：`{json.dumps(summary['configs']['fair_parameters'], ensure_ascii=False)}`

## 运动分组与近重复帧

motion_score 是相邻解码帧缩小为 64x36 灰度图后的平均绝对差，只是画面运动强度代理，不是钢球物理速度。阈值由当前视频有效分数的分位数自适应生成：`{json.dumps(summary['motion_thresholds'], ensure_ascii=False)}`。

- 近重复帧：{duplicate['count']} / {duplicate['total']}（{fmt(duplicate['ratio'])}）
- 非近重复帧数量：{duplicate['non_duplicate_count']}

## 模型指标

{model_table('Baseline', baseline)}

{model_table('Candidate', candidate)}

center_x 标准差表示检测输出位置的离散程度，其中可能包含钢球真实运动，不能解释为位置误差。

## 同帧差异

- 双方都有输出：{comparison['both_detected_frames']}（{fmt(comparison['both_detected_ratio'])}）
- 仅 Baseline：{comparison['baseline_only_frames']}（{fmt(comparison['baseline_only_ratio'])}）
- 仅 Candidate：{comparison['candidate_only_frames']}（{fmt(comparison['candidate_only_ratio'])}）
- 双方均无输出：{comparison['neither_detected_frames']}（{fmt(comparison['neither_detected_ratio'])}）
- 同时输出帧 center_x 差异均值 / P95：{fmt(comparison['mean_center_x_difference_px'])} / {fmt(comparison['p95_center_x_difference_px'])} px

## fast 阶段输出连续性

- Baseline：{baseline['fast_detected_frames']} / {baseline['fast_frames']}（{fmt(baseline['fast_detected_ratio'])}）
- Candidate：{candidate['fast_detected_frames']} / {candidate['fast_frames']}（{fmt(candidate['fast_detected_ratio'])}）

## 最长连续无检测输出区间（前 5 段）

以下区间是连续无检测输出区间，不是在没有人工真值时确定的漏检结论。

{runs('Baseline', baseline)}

{runs('Candidate', candidate)}

## 标注视频

- Side-by-side：`{side_path}`
- 实际视频输出：`{json.dumps(video_outputs, ensure_ascii=False)}`

## 自动结论与限制

{chr(10).join(conclusion)}

自动统计无法区分正确检测与误检，最终选择前必须人工查看 comparison_side_by_side 视频。
"""


def write_manifest(output_dir: Path) -> dict[str, Any]:
    files = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and path.name != "run_manifest.json":
            files.append(
                {
                    "path": path.relative_to(output_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    manifest = {"generated_at": iso_now(), "files": files}
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def _metadata_equal(first: VideoInfo, second: VideoInfo) -> bool:
    return (
        first.width == second.width
        and first.height == second.height
        and math.isclose(first.fps, second.fps, rel_tol=1e-6, abs_tol=1e-6)
        and first.encoded_frame_count == second.encoded_frame_count
    )


def run_comparison(
    video: str | Path,
    baseline_config: str | Path,
    candidate_config: str | Path,
    output_dir: str | Path,
    *,
    warmup: int = 5,
    frame_step: int = 1,
    max_frames: int = 0,
    write_videos: bool = True,
    progress_every: int = 50,
    order: str = "baseline,candidate",
    detector_factory: Callable[[Any], Any] = SteelBallYoloNcnnDetector,
    writer_factory: Callable[..., Any] = cv2.VideoWriter,
) -> dict[str, Any]:
    started_wall = iso_now()
    started = time.perf_counter()
    video_path = validate_video_path(video)
    initial_info = inspect_video(video_path)
    destination = Path(output_dir).expanduser().resolve()
    ensure_output_dir(destination)
    baseline_path = Path(baseline_config).expanduser()
    candidate_path = Path(candidate_config).expanduser()
    if not baseline_path.is_file():
        raise ValueError(f"Baseline 配置不存在: {baseline_path}")
    if not candidate_path.is_file():
        raise ValueError(f"Candidate 配置不存在: {candidate_path}")
    baseline_path = baseline_path.resolve()
    candidate_path = candidate_path.resolve()
    configs = {
        "baseline": load_steel_ball_ncnn_config(baseline_path),
        "candidate": load_steel_ball_ncnn_config(candidate_path),
    }
    fair_baseline = {name: getattr(configs["baseline"], name) for name in FAIR_CONFIG_FIELDS}
    fair_candidate = {name: getattr(configs["candidate"], name) for name in FAIR_CONFIG_FIELDS}
    if fair_baseline != fair_candidate:
        raise ValueError(
            "两份配置除 model_path/debug 外的推理参数必须一致: "
            f"baseline={fair_baseline}, candidate={fair_candidate}"
        )
    labels = order.split(",")
    if labels not in (["baseline", "candidate"], ["candidate", "baseline"]):
        raise ValueError("--order 必须是 baseline,candidate 或 candidate,baseline")
    results: dict[str, PassResult] = {}
    expected_indices: list[int] | None = None
    for label in labels:
        result = process_model_pass(
            label,
            video_path,
            configs[label],
            warmup=warmup,
            frame_step=frame_step,
            max_frames=max_frames,
            progress_every=progress_every,
            detector_factory=detector_factory,
            expected_indices=expected_indices,
            calculate_motion=expected_indices is None,
            partial_csv_path=destination / f"{label}_frames.partial.csv",
        )
        results[label] = result
        write_model_partial_csv(destination / f"{label}_frames.partial.csv", result.rows)
        if expected_indices is None:
            expected_indices = [int(row["frame_index"]) for row in result.rows]
        else:
            # Motion is intentionally calculated once, regardless of requested model order.
            motion_source = results[labels[0]].rows
            for source, target in zip(motion_source, result.rows, strict=True):
                if target["motion_score"] is None:
                    target["motion_score"] = source["motion_score"]
    if not _metadata_equal(results["baseline"].metadata, results["candidate"].metadata):
        raise RuntimeError("两遍打开视频得到的总帧数、宽高或 FPS 不一致")
    if results["baseline"].decoded_frame_count != results["candidate"].decoded_frame_count:
        raise RuntimeError(
            "两遍视频实际成功解码帧数不一致: "
            f"baseline={results['baseline'].decoded_frame_count}, "
            f"candidate={results['candidate'].decoded_frame_count}"
        )
    # When candidate ran first, copy its once-computed motion scores to baseline.
    if labels[0] == "candidate":
        for candidate, baseline in zip(
            results["candidate"].rows, results["baseline"].rows, strict=True
        ):
            baseline["motion_score"] = candidate["motion_score"]
    rows, thresholds = merge_passes(results["baseline"].rows, results["candidate"].rows)
    write_frames_csv(destination / "frames.csv", rows)
    for label in ("baseline", "candidate"):
        partial_path = destination / f"{label}_frames.partial.csv"
        if partial_path.exists():
            partial_path.unlink()
    video_outputs: list[dict[str, Any]] = []
    if write_videos:
        video_outputs = render_videos(
            video_path,
            destination,
            rows,
            frame_step=frame_step,
            writer_factory=writer_factory,
        )
    baseline_summary = model_summary(rows, "baseline", initial_info.fps)
    candidate_summary = model_summary(rows, "candidate", initial_info.fps)
    duplicate_count = sum(bool(row["near_duplicate_frame"]) for row in rows)
    finished_wall = iso_now()
    summary = {
        "input_video": {
            "path": str(video_path),
            "sha256": sha256_file(video_path),
            "file_size": initial_info.file_size,
            "width": initial_info.width,
            "height": initial_info.height,
            "fps": initial_info.fps,
            "encoded_frame_count": initial_info.encoded_frame_count,
            "decoded_frame_count": results["baseline"].decoded_frame_count,
            "processed_frame_count": len(rows),
        },
        "motion_thresholds": thresholds,
        "duplicate_frames": {
            "count": duplicate_count,
            "total": len(rows),
            "ratio": ratio(duplicate_count, len(rows)),
            "non_duplicate_count": len(rows) - duplicate_count,
        },
        "baseline": baseline_summary,
        "candidate": candidate_summary,
        "comparison": comparison_summary(rows),
        "configs": {
            "baseline_config_path": str(baseline_path),
            "candidate_config_path": str(candidate_path),
            "baseline": asdict(configs["baseline"]),
            "candidate": asdict(configs["candidate"]),
            "fair_parameters": fair_baseline,
        },
        "arguments": {
            "warmup": warmup,
            "frame_step": frame_step,
            "max_frames": max_frames,
            "write_videos": write_videos,
            "progress_every": progress_every,
            "order": order,
        },
        "started_at": started_wall,
        "finished_at": finished_wall,
        "total_runtime_seconds": time.perf_counter() - started,
        "python_version": platform.python_version(),
        "opencv_version": cv2.__version__,
        "numpy_version": np.__version__,
        "git_commit": git_value("rev-parse", "HEAD"),
        "git_branch": git_value("branch", "--show-current"),
        "video_outputs": video_outputs,
    }
    (destination / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (destination / "report.md").write_text(build_report(summary), encoding="utf-8")
    write_manifest(destination)
    return summary


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="钢球双模型同视频离线 A/B 输出连续性对比")
    parser.add_argument("--video", required=True, help="本地输入视频")
    parser.add_argument(
        "--baseline-config", default="config/model_profiles/steel_ball_baseline.yaml"
    )
    parser.add_argument(
        "--candidate-config", default="config/model_profiles/steel_ball_candidate.yaml"
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--frame-step", type=int, default=1)
    parser.add_argument("--max-frames", type=int, default=0)
    video_group = parser.add_mutually_exclusive_group()
    video_group.add_argument("--write-videos", dest="write_videos", action="store_true")
    video_group.add_argument("--no-write-videos", dest="write_videos", action="store_false")
    parser.set_defaults(write_videos=True)
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument("--order", default="baseline,candidate")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.warmup < 0 or args.frame_step < 1 or args.max_frames < 0 or args.progress_every < 0:
        parser.error("warmup/max-frames/progress-every 不能为负，frame-step 必须至少为 1")
    video_hint = Path(args.video).expanduser()
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else default_output_dir(video_hint)
    try:
        summary = run_comparison(
            args.video,
            args.baseline_config,
            args.candidate_config,
            output_dir,
            warmup=args.warmup,
            frame_step=args.frame_step,
            max_frames=args.max_frames,
            write_videos=args.write_videos,
            progress_every=args.progress_every,
            order=args.order,
        )
    except KeyboardInterrupt:
        print("用户中断；资源已释放，不生成正式 summary/report。", file=sys.stderr)
        return 130
    except (ValueError, ConfigError) as exc:
        print(f"参数、配置或视频错误: {exc}", file=sys.stderr)
        return 2
    except (ImportError, RuntimeError) as exc:
        print(f"模型或处理错误: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"文件或视频编码错误: {exc}", file=sys.stderr)
        return 4
    print(json.dumps({"output_dir": str(output_dir.resolve()), "comparison": summary["comparison"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
