"""Extract deduplicated human-review frames from Candidate Raw/ROI comparison CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
from typing import Any

import cv2
import numpy as np


CATEGORIES = (
    "roi_outside_review",
    "fast_no_output_review",
    "long_no_output_review",
    "low_conf_inside_roi_review",
    "geometry_invalid_review",
)
MANIFEST_FIELDS = (
    "category",
    "frame_index",
    "timestamp_ms",
    "image_path",
    "motion_level",
    "raw_detected",
    "raw_confidence",
    "roi_geometry_valid",
    "roi_accepted",
    "roi_reason",
    "notes",
)
REQUIRED_FIELDS = {
    "frame_index",
    "timestamp_ms",
    "motion_level",
    "baseline_detected",
    "baseline_confidence",
    "candidate_detected",
    "candidate_roi_geometry_valid",
    "candidate_roi_accepted_count",
    "candidate_roi_reason",
    "candidate_raw_reference_roi_accepted",
    "candidate_low_conf_inside_roi_count",
}


def _truth(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes"}


def load_comparison_rows(path: str | Path) -> list[dict[str, str]]:
    source = Path(path).expanduser()
    if not source.is_file():
        raise ValueError(f"frames.csv 不存在: {source}")
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or ())
        missing = sorted(REQUIRED_FIELDS - fields)
        if missing:
            raise ValueError("frames.csv 缺少字段: " + ", ".join(missing))
        rows = list(reader)
    if not rows:
        raise ValueError("frames.csv 没有数据行")
    try:
        for row in rows:
            row["frame_index"] = str(int(row["frame_index"]))
            float(row["timestamp_ms"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"frames.csv 帧索引或时间戳无效: {exc}") from exc
    return rows


def _longest_no_output_rows(rows: list[dict[str, str]], maximum: int) -> list[dict[str, str]]:
    runs: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for row in rows:
        if not _truth(row["candidate_detected"]):
            current.append(row)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    if not runs or maximum <= 0:
        return []
    run = max(runs, key=lambda item: (len(item), -int(item[0]["frame_index"])))
    count = min(maximum, len(run))
    if count == 1:
        return [run[len(run) // 2]]
    indices = sorted({round(index * (len(run) - 1) / (count - 1)) for index in range(count)})
    return [run[index] for index in indices]


def classify_rows(
    rows: list[dict[str, str]], *, max_per_category: int
) -> dict[str, list[dict[str, str]]]:
    result = {name: [] for name in CATEGORIES}
    for row in rows:
        raw_detected = _truth(row["baseline_detected"])
        roi_detected = _truth(row["candidate_detected"])
        reference = row["candidate_raw_reference_roi_accepted"].strip()
        geometry = row["candidate_roi_geometry_valid"].strip()
        confidence = float(row["baseline_confidence"]) if raw_detected else 0.0
        if raw_detected and reference == "0":
            result["roi_outside_review"].append(row)
        if row["motion_level"] == "fast" and not roi_detected:
            result["fast_no_output_review"].append(row)
        if int(row["candidate_low_conf_inside_roi_count"]) > 0 or (
            raw_detected and 0.40 <= confidence < 0.50 and reference == "1"
        ):
            result["low_conf_inside_roi_review"].append(row)
        if geometry == "0":
            result["geometry_invalid_review"].append(row)
    result["long_no_output_review"] = _longest_no_output_rows(rows, max_per_category)
    return result


def _review_readme() -> str:
    return """# 钢球困难帧人工复核说明

这些图片只用于人工复核；工具不会生成 YOLO 标签、空标签或任何真实类别结论。

- `roi_outside_review`：确认框附近是否确实没有钢球。若为背景输出，可在人工确认后作为负样本；若钢球实际位于该处，应先检查 ROI 几何是否错误。
- `fast_no_output_review`：检查钢球是否可见；可见时后续人工标注，不可见时不要标注钢球。
- `long_no_output_review`：来自最长连续无最终输出区间的均匀抽帧，逐张判断现场真实情况。
- `low_conf_inside_roi_review`：检查 0.50 阈值是否过滤了真实钢球。
- `geometry_invalid_review`：先检查红蓝端点识别与标定，不要直接作为钢球训练样本。

所有训练标签必须人工审核。`manifest.csv` 的 `notes` 列留给复核人员填写。
"""


def extract_hard_frames(
    video: str | Path,
    frames_csv: str | Path,
    output_dir: str | Path,
    *,
    max_per_category: int = 120,
    minimum_interval_ms: float = 500.0,
    duplicate_mad_threshold: float = 1.0,
) -> dict[str, int]:
    video_path = Path(video).expanduser()
    if not video_path.is_file():
        raise ValueError(f"视频不存在: {video_path}")
    if max_per_category <= 0:
        raise ValueError("max-per-category 必须大于 0")
    rows = load_comparison_rows(frames_csv)
    classified = classify_rows(rows, max_per_category=max_per_category)
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for category in CATEGORIES:
        (destination / category).mkdir(exist_ok=True)
    selected_by_frame: dict[int, list[tuple[str, dict[str, str]]]] = {}
    for category, category_rows in classified.items():
        for row in category_rows:
            selected_by_frame.setdefault(int(row["frame_index"]), []).append((category, row))

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise ValueError(f"视频无法打开: {video_path}")
    last_time = {category: float("-inf") for category in CATEGORIES}
    last_gray: dict[str, np.ndarray] = {}
    counts = {category: 0 for category in CATEGORIES}
    manifest: list[dict[str, Any]] = []
    try:
        frame_index = 0
        remaining = set(selected_by_frame)
        while remaining:
            ok, frame = capture.read()
            if not ok:
                break
            matches = selected_by_frame.get(frame_index, [])
            if matches:
                small = cv2.resize(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                    (64, 36),
                    interpolation=cv2.INTER_AREA,
                )
                for category, row in matches:
                    if counts[category] >= max_per_category:
                        continue
                    timestamp = float(row["timestamp_ms"])
                    if timestamp - last_time[category] < minimum_interval_ms:
                        continue
                    previous = last_gray.get(category)
                    if previous is not None and float(cv2.absdiff(small, previous).mean()) < duplicate_mad_threshold:
                        continue
                    filename = f"{category}_frame_{frame_index:08d}_{timestamp:012.1f}ms.jpg"
                    image_path = destination / category / filename
                    if not cv2.imwrite(str(image_path), frame):
                        raise OSError(f"困难帧写入失败: {image_path}")
                    last_time[category] = timestamp
                    last_gray[category] = small.copy()
                    counts[category] += 1
                    manifest.append(
                        {
                            "category": category,
                            "frame_index": frame_index,
                            "timestamp_ms": timestamp,
                            "image_path": image_path.relative_to(destination).as_posix(),
                            "motion_level": row["motion_level"],
                            "raw_detected": row["baseline_detected"],
                            "raw_confidence": row["baseline_confidence"],
                            "roi_geometry_valid": row["candidate_roi_geometry_valid"],
                            "roi_accepted": row["candidate_roi_accepted_count"],
                            "roi_reason": row["candidate_roi_reason"],
                            "notes": "",
                        }
                    )
                remaining.discard(frame_index)
            frame_index += 1
    finally:
        capture.release()
    missing = sorted(remaining)
    if missing:
        raise RuntimeError(f"视频未能解码到 CSV 中的帧索引，首个缺失帧: {missing[0]}")
    with (destination / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(manifest)
    (destination / "README.md").write_text(_review_readme(), encoding="utf-8")
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--frames-csv", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-per-category", type=int, default=120)
    parser.add_argument("--minimum-interval-ms", type=float, default=500.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        counts = extract_hard_frames(
            args.video,
            args.frames_csv,
            args.output_dir,
            max_per_category=args.max_per_category,
            minimum_interval_ms=args.minimum_interval_ms,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"困难帧提取失败: {exc}", file=sys.stderr)
        return 2
    print("困难帧提取完成: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
