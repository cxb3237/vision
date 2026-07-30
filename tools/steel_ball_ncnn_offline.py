"""Offline helpers for validating the deployed steel-ball NCNN model on local images."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

import cv2
import numpy as np

from core.config_loader import ConfigError, load_steel_ball_ncnn_config
from core.models import FramePacket
from detectors.steel_ball_yolo_ncnn_detector import SteelBallYoloNcnnDetector


SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


def validate_source_argument(value: str | Path) -> Path:
    raw = str(value).strip()
    normalized = raw.replace("\\", "/")
    if raw.isdecimal() or normalized.startswith("/dev/video"):
        raise ValueError("--source accepts local image files only")
    path = Path(raw).expanduser()
    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        raise ValueError("--source must be a jpg, jpeg, png or bmp image")
    if not path.is_file():
        raise ValueError(f"source image does not exist: {path}")
    return path.resolve()


def annotate_image(image_bgr: np.ndarray, detections: list[dict[str, Any]]) -> np.ndarray:
    output = image_bgr.copy()
    if not detections:
        cv2.putText(output, "No detection", (16, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
        return output
    for item in detections:
        p1 = (int(item["x1"]), int(item["y1"]))
        p2 = (int(item["x2"]), int(item["y2"]))
        center = (int(item["center_x"]), int(item["center_y"]))
        cv2.rectangle(output, p1, p2, (0, 220, 0), 2)
        cv2.circle(output, center, 4, (0, 220, 0), -1)
        cv2.putText(output, f"steel_ball {float(item['confidence']):.3f}", (p1[0], max(18, p1[1] - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 0), 2)
    return output


def make_benchmark(timing_rows: list[dict[str, float]], *, warmup: int, repeat: int, threads: int) -> dict[str, Any]:
    summary: dict[str, dict[str, float]] = {}
    series: dict[str, list[float]] = {}
    for phase in ("preprocess", "inference", "postprocess", "total"):
        values = np.asarray([float(row[phase]) for row in timing_rows], dtype=np.float64)
        key = f"{phase}_ms"
        series[key] = values.tolist()
        summary[key] = {
            "mean": float(np.mean(values)),
            "median": float(np.median(values)),
            "p95": float(np.percentile(values, 95)),
        }
    median = summary["total_ms"]["median"]
    return {
        "warmup": warmup,
        "repeat": repeat,
        "num_threads": threads,
        **series,
        "summary_ms": summary,
        "estimated_fps": 1000.0 / median if median > 0 else None,
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="复用正式检测器执行钢球 NCNN 离线推理")
    parser.add_argument("--image", "--source", dest="image", required=True)
    parser.add_argument("--config", default="config/steel_ball_ncnn.yaml")
    parser.add_argument("--output", help="可选标注图输出路径")
    return parser


def run_offline_inference(
    image_path: Path,
    config_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError(f"无法读取图片: {image_path}")
    detector = SteelBallYoloNcnnDetector(load_steel_ball_ncnn_config(config_path))
    try:
        detector.initialize()
        if not detector.model_loaded:
            raise RuntimeError(detector.detector_error or "NCNN 模型加载失败")
        packet = FramePacket(1, time.monotonic(), image)
        started = time.perf_counter()
        result = detector.process(packet)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if detector.detector_error:
            raise RuntimeError(detector.detector_error)
        if output_path is not None:
            destination = Path(output_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            annotated = detector.draw_debug(image, result)
            if not cv2.imwrite(str(destination), annotated):
                raise OSError(f"标注图写入失败: {destination}")
        return {
            "found": bool(result.found),
            "center_x": int(result.center_x) if result.found else None,
            "center_y": int(result.center_y) if result.found else None,
            "confidence": float(result.confidence) / 1000.0 if result.found else 0.0,
            "inference_ms": float(detector.get_runtime_status().get("inference_ms", elapsed_ms)),
            "total_elapsed_ms": elapsed_ms,
            "output": str(Path(output_path).resolve()) if output_path is not None else None,
        }
    finally:
        detector.close()


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        source = validate_source_argument(args.image)
        report = run_offline_inference(source, args.config, args.output)
    except (ValueError, ConfigError) as exc:
        print(f"参数或配置错误: {exc}", file=sys.stderr)
        return 2
    except (ImportError, RuntimeError) as exc:
        print(f"NCNN 推理不可用: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"文件错误: {exc}", file=sys.stderr)
        return 4
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
