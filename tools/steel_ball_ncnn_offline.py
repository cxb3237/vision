"""Offline, local-image-only NCNN inference for the steel-ball model."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import zipfile

import cv2
import numpy as np

from inference.steel_ball_ncnn_runtime import (
    DEFAULT_NUM_THREADS,
    SteelBallNcnnError,
    SteelBallNcnnRuntime,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = Path("models/steel_ball/best_ncnn_model")
DEFAULT_OUTPUT_DIR = Path("artifacts/steel_ball_ncnn_offline")
SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}


def validate_source_argument(value: str | Path) -> Path:
    """Accept a local image path and explicitly reject camera device syntax."""

    raw = str(value).strip()
    normalized = raw.replace("\\", "/")
    if not raw:
        raise ValueError("--source must be a non-empty local image path")
    if raw.isdecimal() or normalized.startswith("/dev/video"):
        raise ValueError(
            "--source accepts local image files only; camera numbers and /dev/video devices "
            "are not supported"
        )
    path = Path(raw).expanduser()
    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        raise ValueError(
            f"--source must be a jpg, jpeg, png or bmp image, got: {path}"
        )
    if not path.is_file():
        raise ValueError(f"source image does not exist: {path}")
    return path.resolve()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return parsed


def _unit_float(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be in the range [0, 1]")
    return parsed


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用 CPU NCNN 对一张本地图片执行钢球离线检测"
    )
    parser.add_argument("--source", required=True, help="本地 jpg/jpeg/png/bmp 图片路径")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--imgsz", type=_positive_int, default=416)
    parser.add_argument("--conf", type=_unit_float, default=0.40)
    parser.add_argument("--iou", type=_unit_float, default=0.60)
    parser.add_argument("--max-det", type=_positive_int, default=30)
    parser.add_argument("--threads", type=_positive_int, default=DEFAULT_NUM_THREADS)
    parser.add_argument("--warmup", type=_non_negative_int, default=2)
    parser.add_argument("--repeat", type=_positive_int, default=10)
    parser.add_argument("--debug-shapes", action="store_true")
    return parser


def _resolve_from_project(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (PROJECT_ROOT / expanded).resolve()


def _read_image(path: Path) -> np.ndarray:
    try:
        encoded = np.fromfile(path, dtype=np.uint8)
    except OSError as exc:
        raise ValueError(f"failed to read source image {path}: {exc}") from exc
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError(f"OpenCV could not decode source image: {path}")
    return image


def _write_image(path: Path, image: np.ndarray) -> None:
    suffix = path.suffix or ".jpg"
    ok, encoded = cv2.imencode(suffix, image)
    if not ok:
        raise OSError(f"OpenCV failed to encode annotated image: {path}")
    try:
        encoded.tofile(path)
    except OSError as exc:
        raise OSError(f"failed to write annotated image {path}: {exc}") from exc


def annotate_image(image_bgr: np.ndarray, detections: list[dict[str, object]]) -> np.ndarray:
    """Draw results on a copy, never on the caller's source image."""

    annotated = image_bgr.copy()
    if not detections:
        cv2.putText(
            annotated,
            "No detection",
            (16, 36),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (0, 0, 255),
            2,
            cv2.LINE_AA,
        )
        return annotated
    for detection in detections:
        x1, y1 = int(detection["x1"]), int(detection["y1"])
        x2, y2 = int(detection["x2"]), int(detection["y2"])
        center_x, center_y = int(detection["center_x"]), int(detection["center_y"])
        confidence = float(detection["confidence"])
        cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 220, 0), 2)
        cv2.circle(annotated, (center_x, center_y), 4, (0, 220, 0), -1)
        label_y = max(18, y1 - 8)
        cv2.putText(
            annotated,
            f"steel_ball {confidence:.3f}",
            (x1, label_y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 220, 0),
            2,
            cv2.LINE_AA,
        )
    return annotated


def _series_summary(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "p95": float(np.percentile(array, 95)),
    }


def make_benchmark(
    timing_rows: list[dict[str, float]],
    *,
    warmup: int,
    repeat: int,
    threads: int,
) -> dict[str, object]:
    phase_keys = ("preprocess", "inference", "postprocess", "total")
    series = {
        f"{phase}_ms": [float(row[phase]) for row in timing_rows] for phase in phase_keys
    }
    summary = {key: _series_summary(values) for key, values in series.items()}
    total_median = summary["total_ms"]["median"]
    return {
        "warmup": warmup,
        "repeat": repeat,
        "num_threads": threads,
        **series,
        "summary_ms": summary,
        "estimated_fps": 1000.0 / total_median if total_median > 0.0 else None,
        "scope_note": (
            "Offline repeated single-image processing; excludes camera capture, web display "
            "and UART transmission overhead."
        ),
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _report_text(
    *,
    source: Path,
    model_dir: Path,
    result: dict[str, object],
    benchmark: dict[str, object],
    interpreter: str,
    ncnn_version: str,
) -> str:
    summary = benchmark["summary_ms"]
    detections = result["detections"]
    detection_lines = [
        (
            f"- confidence={float(item['confidence']):.6f}, "
            f"bbox=({item['x1']}, {item['y1']}, {item['x2']}, {item['y2']}), "
            f"center=({item['center_x']}, {item['center_y']}), "
            f"error_x_permille={item['error_x_permille']}"
        )
        for item in detections
    ] or ["- No detection above the configured threshold."]
    fps = benchmark["estimated_fps"]
    return "\n".join(
        [
            "# Steel-ball NCNN Offline Inference Report",
            "",
            f"- Timestamp: {datetime.now(timezone.utc).astimezone().isoformat()}",
            f"- Python: `{interpreter}`",
            f"- ncnn: `{ncnn_version}`",
            f"- Source: `{source}`",
            f"- Model: `{model_dir}`",
            f"- Class names: `{result['model_class_names']}`",
            f"- Input tensor shape: `{result['input_tensor_shape']}`",
            f"- Output tensor shapes: `{result['output_tensor_shapes']}`",
            "- Output meaning: raw Detect tensor `(xywh + class scores, anchors)`; "
            "this model has one `steel_ball` score and requires confidence filtering and NMS.",
            f"- Detection count: {result['detection_count']}",
            "",
            "## Detections",
            "",
            *detection_lines,
            "",
            "## Benchmark",
            "",
            f"- Warmup / repeats: {benchmark['warmup']} / {benchmark['repeat']}",
            f"- Threads: {benchmark['num_threads']}",
            f"- Preprocess median: {summary['preprocess_ms']['median']:.3f} ms",
            f"- NCNN inference median: {summary['inference_ms']['median']:.3f} ms",
            f"- Postprocess median: {summary['postprocess_ms']['median']:.3f} ms",
            f"- Total median: {summary['total_ms']['median']:.3f} ms",
            f"- Total P95: {summary['total_ms']['p95']:.3f} ms",
            f"- Estimated FPS: {float(fps):.3f}" if fps is not None else "- Estimated FPS: n/a",
            "",
            "This is offline repeated single-image processing speed. It excludes camera "
            "capture, web display and UART transmission overhead and is not a claim about "
            "end-to-end live-camera FPS.",
            "",
            "The inference path uses NumPy, OpenCV and ncnn only. It does not import "
            "Ultralytics, PyTorch or TorchVision.",
            "",
        ]
    )


def _write_execution_log(
    path: Path,
    *,
    source: Path,
    model_dir: Path,
    result: dict[str, object],
    benchmark: dict[str, object],
    ncnn_version: str,
) -> None:
    summary = benchmark["summary_ms"]
    lines = [
        f"timestamp={datetime.now(timezone.utc).astimezone().isoformat()}",
        f"python={sys.executable}",
        f"ncnn={ncnn_version}",
        f"source={source}",
        f"model={model_dir}",
        f"input_shape={result['input_tensor_shape']}",
        f"output_shapes={result['output_tensor_shapes']}",
        f"detection_count={result['detection_count']}",
        f"total_median_ms={summary['total_ms']['median']:.6f}",
        f"estimated_fps={benchmark['estimated_fps']}",
        "camera_opened=false",
        "serial_opened=false",
        "services_modified=false",
        "ultralytics_imported=false",
        "pytorch_imported=false",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _create_report_zip(output_dir: Path) -> Path:
    zip_path = output_dir / "steel_ball_ncnn_offline_report.zip"
    members = [
        output_dir / "annotated.jpg",
        output_dir / "detections.json",
        output_dir / "benchmark.json",
        output_dir / "OFFLINE_INFERENCE_REPORT.md",
        output_dir / "execution.log",
        PROJECT_ROOT / "inference/steel_ball_ncnn_runtime.py",
        PROJECT_ROOT / "tools/steel_ball_ncnn_offline.py",
        PROJECT_ROOT / "tests/test_steel_ball_ncnn_runtime.py",
    ]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            if not member.is_file():
                raise OSError(f"report archive member is missing: {member}")
            if member.is_relative_to(output_dir):
                arcname = member.relative_to(output_dir)
            else:
                arcname = member.relative_to(PROJECT_ROOT)
            archive.write(member, arcname.as_posix())
    return zip_path


def run(args: argparse.Namespace) -> dict[str, object]:
    source = validate_source_argument(args.source)
    model_dir = _resolve_from_project(args.model)
    output_dir = _resolve_from_project(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    image = _read_image(source)

    runtime = SteelBallNcnnRuntime(
        model_dir=model_dir,
        imgsz=args.imgsz,
        conf_threshold=args.conf,
        iou_threshold=args.iou,
        max_det=args.max_det,
        num_threads=args.threads,
    )
    try:
        runtime.load()
        ncnn_version = str(getattr(runtime._ncnn, "__version__", "unknown"))
        for _ in range(args.warmup):
            runtime.predict(image)
        results: list[dict[str, object]] = []
        timing_rows: list[dict[str, float]] = []
        for _ in range(args.repeat):
            result = runtime.predict(image)
            results.append(result)
            timing_rows.append(result["timings_ms"])
        final_result = results[-1]
    finally:
        runtime.close()

    if args.debug_shapes:
        print(f"input_tensor_shape={final_result['input_tensor_shape']}")
        print(f"output_tensor_shapes={final_result['output_tensor_shapes']}")
        print(final_result["output_tensor_diagnostics"])

    benchmark = make_benchmark(
        timing_rows,
        warmup=args.warmup,
        repeat=args.repeat,
        threads=args.threads,
    )
    height, width = image.shape[:2]
    detections_document = {
        "source": str(source),
        "original_image": {"width": width, "height": height},
        "model": str(model_dir),
        "imgsz": args.imgsz,
        "conf": args.conf,
        "iou": args.iou,
        "detections": final_result["detections"],
        "detection_count": final_result["detection_count"],
        "input_tensor_shape": final_result["input_tensor_shape"],
        "output_tensor_shapes": final_result["output_tensor_shapes"],
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "model_class_names": final_result["model_class_names"],
    }
    _write_image(output_dir / "annotated.jpg", annotate_image(image, final_result["detections"]))
    _write_json(output_dir / "detections.json", detections_document)
    _write_json(output_dir / "benchmark.json", benchmark)
    (output_dir / "OFFLINE_INFERENCE_REPORT.md").write_text(
        _report_text(
            source=source,
            model_dir=model_dir,
            result=final_result,
            benchmark=benchmark,
            interpreter=sys.executable,
            ncnn_version=ncnn_version,
        ),
        encoding="utf-8",
    )
    _write_execution_log(
        output_dir / "execution.log",
        source=source,
        model_dir=model_dir,
        result=final_result,
        benchmark=benchmark,
        ncnn_version=ncnn_version,
    )
    zip_path = _create_report_zip(output_dir)
    return {
        "result": final_result,
        "benchmark": benchmark,
        "detections_path": output_dir / "detections.json",
        "annotated_path": output_dir / "annotated.jpg",
        "zip_path": zip_path,
        "ncnn_version": ncnn_version,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        outcome = run(args)
    except (ValueError, OSError, SteelBallNcnnError) as exc:
        parser.error(str(exc))
    result = outcome["result"]
    benchmark = outcome["benchmark"]
    print(f"detections={result['detection_count']}")
    print(f"total_median_ms={benchmark['summary_ms']['total_ms']['median']:.3f}")
    print(f"estimated_fps={benchmark['estimated_fps']:.3f}")
    print(f"annotated={outcome['annotated_path']}")
    print(f"report_zip={outcome['zip_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
