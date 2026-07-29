"""Raspberry Pi-only four-profile vision performance baseline runner.

This utility deliberately has no serial, web, GUI, service-manager, Torch, or
Ultralytics dependency.  On non-Linux hosts it creates an honest skeleton
report and exits without touching camera hardware.
"""

from __future__ import annotations

import argparse
from collections import deque
import csv
import json
import logging
from pathlib import Path
import platform
import subprocess
import time
from typing import Any, Iterable
import zipfile

import cv2

from core.config_loader import (
    load_camera_config,
    load_mission_config,
    load_steel_ball_ncnn_config,
)
from core.performance_metrics import RollingRate, RollingSamples
from detectors.steel_ball_yolo_ncnn_detector import SteelBallYoloNcnnDetector
from detectors.target_tracker import TargetTracker
from drivers.camera_service import CameraService
from touch_ui.frame_stream import LatestFrameStream
from touch_ui.models import load_touch_ui_config


LOG = logging.getLogger("vision_baseline")
PROFILE_NAMES = (
    "camera_only",
    "camera_preview",
    "camera_yolo",
    "camera_yolo_preview",
)
SAMPLE_LIMIT = 20_000
ANNOTATED_IMAGE_NAME = "camera_yolo_preview_annotated.jpg"
REPORT_COLUMNS = (
    "profile",
    "camera_fps",
    "unique_frame_fps",
    "vision_fps",
    "preview_fps",
    "ncnn_total_median_ms",
    "ncnn_total_p95_ms",
    "capture_to_result_median_ms",
    "capture_to_result_p95_ms",
    "vision_loop_median_ms",
    "vision_loop_p95_ms",
    "skipped_camera_frames",
    "preview_overwritten_count",
    "process_cpu_percent_estimate",
    "rss_max_kib",
    "temperature_max_c",
    "throttled_start",
    "throttled_end",
    "model_loaded_during_profile",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Raspberry Pi vision baseline benchmark")
    parser.add_argument("--camera-config", default="config/camera.yaml")
    parser.add_argument("--mission-config", default="config/mission.yaml")
    parser.add_argument("--steel-ball-ncnn-config", default="config/steel_ball_ncnn.yaml")
    parser.add_argument("--touch-config", default="config/touch_ui.yaml")
    parser.add_argument("--warmup-seconds", type=float, default=5.0)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--output-dir", default="artifacts/vision_baseline")
    parser.add_argument("--profiles", nargs="+", choices=PROFILE_NAMES, default=list(PROFILE_NAMES))
    return parser


def _validate_durations(warmup: float, duration: float) -> None:
    if warmup < 0:
        raise ValueError("warmup-seconds must be non-negative")
    if duration <= 0:
        raise ValueError("duration-seconds must be positive")


def normalize_profiles(profiles: Iterable[str]) -> list[str]:
    """Deduplicate selected profiles and enforce the canonical benchmark order."""

    requested = set(profiles)
    unknown = requested.difference(PROFILE_NAMES)
    if unknown:
        raise ValueError(f"unknown benchmark profile: {', '.join(sorted(unknown))}")
    return [name for name in PROFILE_NAMES if name in requested]


def remove_stale_annotation(output_dir: Path) -> bool:
    """Remove only this benchmark's prior annotated-frame artifact."""

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ANNOTATED_IMAGE_NAME
    if not path.exists():
        return False
    path.unlink()
    return True


def _read_rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _run_vcgencmd(argument: str) -> str | None:
    try:
        completed = subprocess.run(
            ["vcgencmd", argument],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _read_temperature_c() -> float | None:
    value = _run_vcgencmd("measure_temp")
    if value is None:
        return None
    try:
        return float(value.split("=", 1)[1].split("'", 1)[0])
    except (ValueError, IndexError):
        return None


def _summary_fields(prefix: str, samples: RollingSamples) -> dict[str, float]:
    summary = samples.summary()
    return {
        f"{prefix}_median_ms": round(float(summary["median"]), 3),
        f"{prefix}_p95_ms": round(float(summary["p95"]), 3),
    }


class HardwareBenchmark:
    """Own one camera and one model while running selected profiles sequentially."""

    def __init__(
        self,
        camera: Any,
        detector: Any,
        tracker: Any,
        preview: Any,
        *,
        warmup_seconds: float,
        duration_seconds: float,
        output_dir: Path,
        clock=time.monotonic,
    ) -> None:
        self.camera = camera
        self.detector = detector
        self.tracker = tracker
        self.preview = preview
        self.warmup_seconds = warmup_seconds
        self.duration_seconds = duration_seconds
        self.output_dir = output_dir
        self.clock = clock
        self.samples: deque[dict[str, Any]] = deque(maxlen=SAMPLE_LIMIT)
        self.execution_log: list[str] = []
        self._model_initialized = False
        self._model_initialize_attempted = False

    def _log(self, message: str) -> None:
        self.execution_log.append(message)
        LOG.info(message)

    def _initialize_model_once(self, profile: str) -> None:
        if self._model_initialized:
            return
        self._model_initialize_attempted = True
        self._log(f"model initialization started before profile={profile}")
        self.detector.initialize()
        if not self.detector.model_loaded:
            message = self.detector.detector_error or "NCNN model failed to load"
            self._log(f"model initialization failed: {message}")
            raise RuntimeError(message)
        self._model_initialized = True
        self._log(f"model initialization completed before profile={profile}")

    def run(self, profiles: Iterable[str]) -> list[dict[str, Any]]:
        requested = normalize_profiles(profiles)
        if remove_stale_annotation(self.output_dir):
            self._log(f"removed stale annotation: {ANNOTATED_IMAGE_NAME}")
        self.camera.start()
        self._log("camera started once for benchmark run")
        summaries: list[dict[str, Any]] = []
        try:
            for profile in requested:
                preview_enabled = profile in {"camera_preview", "camera_yolo_preview"}
                yolo_enabled = "yolo" in profile
                self._log(f"profile started: {profile}")
                if yolo_enabled:
                    self._initialize_model_once(profile)
                self.tracker.reset()
                if preview_enabled:
                    self.preview.start()
                    try:
                        self._run_period(
                            profile,
                            self.warmup_seconds,
                            collect=False,
                            model_loaded_during_profile=self._model_initialized,
                        )
                    finally:
                        self.preview.stop()
                    self.preview.reset_statistics(clear_buffers=True)
                    self.preview.start()
                    try:
                        summary = self._run_period(
                            profile,
                            self.duration_seconds,
                            collect=True,
                            model_loaded_during_profile=self._model_initialized,
                        )
                    finally:
                        self.preview.stop()
                    self._apply_preview_statistics(summary, self.preview.get_statistics())
                else:
                    self._run_period(
                        profile,
                        self.warmup_seconds,
                        collect=False,
                        model_loaded_during_profile=self._model_initialized,
                    )
                    summary = self._run_period(
                        profile,
                        self.duration_seconds,
                        collect=True,
                        model_loaded_during_profile=self._model_initialized,
                    )
                summaries.append(summary)
                self._log(f"profile completed: {profile}")
        except Exception as exc:
            self._log(f"benchmark error: {type(exc).__name__}: {exc}")
            raise
        finally:
            self.camera.stop()
            self._log("camera stopped once for benchmark run")
            if self._model_initialize_attempted:
                self.detector.close()
        return summaries

    @staticmethod
    def _apply_preview_statistics(
        summary: dict[str, Any], statistics: dict[str, Any]
    ) -> None:
        summary.update(
            {
                "preview_fps": round(float(statistics.get("preview_fps", 0.0)), 3),
                "preview_encode_ms": round(
                    float(statistics.get("preview_encode_ms", 0.0)), 3
                ),
                "preview_encode_median_ms": round(
                    float(statistics.get("preview_encode_median_ms", 0.0)), 3
                ),
                "preview_encode_p95_ms": round(
                    float(statistics.get("preview_encode_p95_ms", 0.0)), 3
                ),
                "preview_submitted_count": int(
                    statistics.get("preview_submitted_count", 0)
                ),
                "preview_encoded_count": int(
                    statistics.get("preview_encoded_count", 0)
                ),
                "preview_overwritten_count": int(
                    statistics.get("preview_overwritten_count", 0)
                ),
            }
        )

    def _run_period(
        self,
        profile: str,
        seconds: float,
        *,
        collect: bool,
        model_loaded_during_profile: bool = False,
    ) -> dict[str, Any]:
        start = self.clock()
        deadline = start + seconds
        cpu_start = time.process_time()
        rss_start = _read_rss_kib()
        temperature_start = _read_temperature_c()
        throttled_start = _run_vcgencmd("get_throttled")
        max_rss = rss_start
        max_temperature = temperature_start
        next_resource_sample = start
        rates = {
            "unique": RollingRate(max(2.0, seconds + 1.0), 8192),
            "vision": RollingRate(max(2.0, seconds + 1.0), 8192),
        }
        timings = {
            name: RollingSamples(4096)
            for name in (
                "preprocess", "inference", "postprocess", "ncnn_total",
                "frame_age_before_process", "capture_to_result", "vision_loop", "tracker",
                "draw_debug", "preview_submit",
            )
        }
        processed = skipped = found = 0
        previous_id: int | None = None
        last_annotated = None
        preview_enabled = profile in {"camera_preview", "camera_yolo_preview"}
        while self.clock() < deadline:
            frame = self.camera.get_latest_frame()
            if frame is None or frame.frame_id == previous_id:
                time.sleep(0.001)
                continue
            event_at = self.clock()
            if previous_id is not None:
                skipped += max(0, frame.frame_id - previous_id - 1)
            previous_id = frame.frame_id
            rates["unique"].record(event_at)
            result = None
            process_start = self.clock()
            frame_age = max(0.0, (process_start - frame.capture_timestamp) * 1000.0)
            capture_to_result_ms = vision_loop_ms = tracker_ms = draw_ms = submit_ms = 0.0
            detector_status: dict[str, Any] = {}
            if "yolo" in profile:
                detected = self.detector.process(frame)
                tracker_start = self.clock()
                result = self.tracker.update(detected)
                tracker_ms = (self.clock() - tracker_start) * 1000.0
                process_end = self.clock()
                capture_to_result_ms = (
                    process_end - frame.capture_timestamp
                ) * 1000.0
                vision_loop_ms = (process_end - process_start) * 1000.0
                rates["vision"].record(process_end)
                processed += 1
                found += int(bool(result.found))
                timings["frame_age_before_process"].add(frame_age)
                timings["capture_to_result"].add(capture_to_result_ms)
                timings["vision_loop"].add(vision_loop_ms)
                timings["tracker"].add(tracker_ms)
                detector_status = self.detector.get_runtime_status()
                for source, destination in (
                    ("preprocess_ms", "preprocess"),
                    ("inference_ms", "inference"),
                    ("postprocess_ms", "postprocess"),
                    ("total_ms", "ncnn_total"),
                ):
                    timings[destination].add(float(detector_status.get(source, 0.0)))
            if profile == "camera_yolo_preview":
                draw_start = self.clock()
                last_annotated = self.detector.draw_debug(frame.image, result)
                draw_ms = (self.clock() - draw_start) * 1000.0
                timings["draw_debug"].add(draw_ms)
                submit_start = self.clock()
                self.preview.submit_frame(last_annotated)
                submit_ms = (self.clock() - submit_start) * 1000.0
                timings["preview_submit"].add(submit_ms)
            elif profile == "camera_preview":
                submit_start = self.clock()
                self.preview.submit_frame(frame.image)
                submit_ms = (self.clock() - submit_start) * 1000.0
                timings["preview_submit"].add(submit_ms)
            rss = None
            temperature = None
            if event_at >= next_resource_sample:
                rss = _read_rss_kib()
                temperature = _read_temperature_c()
                max_rss = rss if max_rss is None else max(max_rss, rss or max_rss)
                max_temperature = (
                    temperature
                    if max_temperature is None
                    else max(
                        max_temperature,
                        temperature if temperature is not None else max_temperature,
                    )
                )
                next_resource_sample = event_at + 1.0
            if collect:
                self.samples.append({
                    "profile": profile,
                    "elapsed_s": round(self.clock() - start, 6),
                    "frame_id": frame.frame_id,
                    "found": bool(result and result.found),
                    "frame_age_before_process_ms": round(frame_age, 3),
                    "capture_to_result_ms": round(capture_to_result_ms, 3),
                    "preprocess_ms": detector_status.get("preprocess_ms", 0.0),
                    "inference_ms": detector_status.get("inference_ms", 0.0),
                    "postprocess_ms": detector_status.get("postprocess_ms", 0.0),
                    "ncnn_total_ms": detector_status.get("total_ms", 0.0),
                    "vision_loop_ms": round(vision_loop_ms, 3),
                    "tracker_ms": round(tracker_ms, 3),
                    "draw_debug_ms": round(draw_ms, 3),
                    "preview_submit_ms": round(submit_ms, 3),
                    "rss_kib": rss if rss is not None else "unsupported",
                    "temperature_c": temperature if temperature is not None else "unsupported",
                })
        end = self.clock()
        wall = max(0.0, end - start)
        cpu_time = max(0.0, time.process_time() - cpu_start)
        camera_stats = self.camera.get_statistics()
        preview_after = self.preview.get_statistics() if preview_enabled else {}
        annotated_image_generated = False
        if collect and profile == "camera_yolo_preview" and last_annotated is not None:
            annotated_image_generated = bool(
                cv2.imwrite(str(self.output_dir / ANNOTATED_IMAGE_NAME), last_annotated)
            )
        temperature_end = _read_temperature_c()
        summary: dict[str, Any] = {
            "profile": profile,
            "wall_time_s": round(wall, 3),
            "camera_fps": round(float(camera_stats.get("actual_fps", 0.0)), 3),
            "unique_frame_fps": round(rates["unique"].rate(end), 3),
            "vision_fps": round(rates["vision"].rate(end), 3),
            "preview_fps": round(float(preview_after.get("preview_fps", 0.0)), 3),
            "processed_count": processed,
            "skipped_camera_frames": skipped,
            "detection_found_ratio": round(found / processed, 6) if processed else 0.0,
            "model_loaded_during_profile": bool(model_loaded_during_profile),
            "annotated_image_generated": annotated_image_generated,
            "rss_start_kib": rss_start if rss_start is not None else "unsupported",
            "rss_end_kib": _read_rss_kib() or "unsupported",
            "rss_max_kib": max_rss if max_rss is not None else "unsupported",
            "process_cpu_time_s": round(cpu_time, 3),
            "process_cpu_percent_estimate": round(cpu_time / wall * 100.0, 2) if wall else 0.0,
            "temperature_start_c": temperature_start if temperature_start is not None else "unsupported",
            "temperature_end_c": temperature_end if temperature_end is not None else "unsupported",
            "temperature_max_c": max_temperature if max_temperature is not None else "unsupported",
            "throttled_start": throttled_start or "unsupported",
            "throttled_end": _run_vcgencmd("get_throttled") or "unsupported",
        }
        for name, values in timings.items():
            summary.update(_summary_fields(name, values))
        self._apply_preview_statistics(summary, preview_after)
        return summary


def _write_outputs(
    output_dir: Path,
    payload: dict[str, Any],
    summaries: list[dict[str, Any]],
    samples: Iterable[dict[str, Any]],
    execution_lines: Iterable[str] = (),
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = [
        "# Vision Baseline Report",
        "",
        f"- Windows code validation: {platform.system() == 'Windows'}",
        f"- hardware_benchmark_executed: {str(payload['hardware_benchmark_executed']).lower()}",
        f"- hardware_benchmark_required: {str(payload['hardware_benchmark_required']).lower()}",
        "",
        "This report never substitutes Windows measurements for Raspberry Pi hardware data.",
    ]
    if summaries:
        report.extend(
            [
                "",
                "## Profile summary",
                "",
                "| " + " | ".join(REPORT_COLUMNS) + " |",
                "| " + " | ".join("---" for _ in REPORT_COLUMNS) + " |",
            ]
        )
        for summary in summaries:
            report.append(
                "| "
                + " | ".join(str(summary.get(name, "unsupported")) for name in REPORT_COLUMNS)
                + " |"
            )
    else:
        report.extend(
            [
                "",
                "No hardware profile summaries are present on this host; no numeric hardware results were fabricated.",
            ]
        )
    (output_dir / "VISION_BASELINE_REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    with (output_dir / "profile_summary.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = sorted({key for row in summaries for key in row}) or ["profile"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summaries)
    sample_rows = list(samples)
    with (output_dir / "samples.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = sorted({key for row in sample_rows for key in row}) or ["profile"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sample_rows)
    log_lines = [
        f"platform={payload.get('platform', platform.system())}",
        "parameters=" + json.dumps(payload.get("parameters", {}), ensure_ascii=False, sort_keys=True),
        f"output_path={output_dir.resolve()}",
        *list(execution_lines),
        (
            "hardware benchmark completed"
            if payload["hardware_benchmark_executed"]
            else "hardware benchmark not executed or incomplete"
        ),
    ]
    if payload.get("error"):
        log_lines.append(f"error={payload['error']}")
    (output_dir / "execution.log").write_text(
        "\n".join(log_lines) + "\n", encoding="utf-8"
    )
    archive = output_dir / "vision_baseline_report.zip"
    allowed = [
        output_dir / "VISION_BASELINE_REPORT.md",
        output_dir / "summary.json",
        output_dir / "profile_summary.csv",
        output_dir / "samples.csv",
        output_dir / "execution.log",
    ]
    include_annotation = any(
        summary.get("profile") == "camera_yolo_preview"
        and summary.get("annotated_image_generated") is True
        for summary in summaries
    )
    if include_annotation:
        allowed.append(output_dir / ANNOTATED_IMAGE_NAME)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in allowed:
            if path.is_file():
                bundle.write(path, path.name)
    return archive


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_durations(args.warmup_seconds, args.duration_seconds)
    output_dir = Path(args.output_dir)
    stale_annotation_removed = remove_stale_annotation(output_dir)
    profiles = normalize_profiles(args.profiles)
    parameters = {
        "camera_config": args.camera_config,
        "mission_config": args.mission_config,
        "steel_ball_ncnn_config": args.steel_ball_ncnn_config,
        "touch_config": args.touch_config,
        "warmup_seconds": args.warmup_seconds,
        "duration_seconds": args.duration_seconds,
        "profiles": profiles,
    }
    if platform.system() != "Linux":
        execution_lines = []
        if stale_annotation_removed:
            execution_lines.append(f"removed stale annotation: {ANNOTATED_IMAGE_NAME}")
        execution_lines.extend(
            f"profile skipped on non-Linux host: {name}" for name in profiles
        )
        payload = {
            "hardware_benchmark_executed": False,
            "hardware_benchmark_required": True,
            "platform": platform.system(),
            "parameters": parameters,
            "profiles": profiles,
        }
        archive = _write_outputs(
            output_dir,
            payload,
            [],
            [],
            execution_lines,
        )
        print(f"Non-Linux host: camera/model benchmark skipped; report: {archive}")
        return 0

    print("Before running: stop vision-touch.service, place a real steel ball in view,")
    print("do not move the camera during measurement, and restart the service afterward.")
    camera_config = load_camera_config(args.camera_config)
    mission = load_mission_config(args.mission_config)
    detector_config = load_steel_ball_ncnn_config(args.steel_ball_ncnn_config)
    touch = load_touch_ui_config(args.touch_config, project_root=Path.cwd())
    camera = CameraService(camera_config)
    detector = SteelBallYoloNcnnDetector(detector_config)
    tracker = TargetTracker(
        alpha=mission["smoothing_alpha"],
        max_jump_px=mission["max_jump_px"],
        confirm_frames=mission["confirm_frames"],
        lost_frames=mission["lost_frames"],
    )
    preview = LatestFrameStream(
        max_fps=touch.preview_max_fps,
        jpeg_quality=touch.jpeg_quality,
        max_width=touch.preview_max_width,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = HardwareBenchmark(
        camera,
        detector,
        tracker,
        preview,
        warmup_seconds=args.warmup_seconds,
        duration_seconds=args.duration_seconds,
        output_dir=output_dir,
    )
    if stale_annotation_removed:
        runner.execution_log.append(f"removed stale annotation: {ANNOTATED_IMAGE_NAME}")
    try:
        summaries = runner.run(profiles)
    except Exception as exc:
        payload = {
            "hardware_benchmark_executed": False,
            "hardware_benchmark_required": True,
            "platform": platform.system(),
            "parameters": parameters,
            "profiles": [],
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_outputs(
            output_dir,
            payload,
            [],
            runner.samples,
            runner.execution_log,
        )
        raise
    payload = {
        "hardware_benchmark_executed": True,
        "hardware_benchmark_required": False,
        "platform": platform.system(),
        "parameters": parameters,
        "profiles": summaries,
    }
    archive = _write_outputs(
        output_dir,
        payload,
        summaries,
        runner.samples,
        runner.execution_log,
    )
    print(f"Benchmark complete: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
