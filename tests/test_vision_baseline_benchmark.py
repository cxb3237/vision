from __future__ import annotations

import inspect
import json
from pathlib import Path
import statistics
import time
import zipfile

import numpy as np

from core.models import FramePacket, TargetState, VisionResult
import tools.vision_baseline_benchmark as benchmark


def test_benchmark_profile_names_are_exact() -> None:
    assert benchmark.PROFILE_NAMES == (
        "camera_only",
        "camera_preview",
        "camera_yolo",
        "camera_yolo_preview",
    )


def test_profile_subset_is_deduplicated_and_uses_canonical_order() -> None:
    assert benchmark.normalize_profiles(
        ["camera_yolo_preview", "camera_only", "camera_yolo", "camera_only"]
    ) == ["camera_only", "camera_yolo", "camera_yolo_preview"]


def test_benchmark_has_no_serial_or_training_framework_imports() -> None:
    source = inspect.getsource(benchmark)
    assert "SerialService" not in source
    assert "import torch" not in source
    assert "import ultralytics" not in source
    assert "shell=True" not in source


def test_non_linux_run_writes_honest_skeleton(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(benchmark.platform, "system", lambda: "Windows")
    output = tmp_path / "baseline"
    output.mkdir()
    (output / "camera_yolo_preview_annotated.jpg").write_bytes(b"stale-hardware-file")
    assert benchmark.main([
        "--output-dir", str(output), "--duration-seconds", "1",
        "--profiles", "camera_only", "camera_only",
    ]) == 0
    payload = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert payload["hardware_benchmark_executed"] is False
    assert payload["hardware_benchmark_required"] is True
    assert payload["profiles"] == ["camera_only"]
    report = (output / "VISION_BASELINE_REPORT.md").read_text(encoding="utf-8")
    assert "no numeric hardware results were fabricated" in report
    assert "| camera_only |" not in report
    assert (output / "vision_baseline_report.zip").is_file()
    with zipfile.ZipFile(output / "vision_baseline_report.zip") as archive:
        assert "camera_yolo_preview_annotated.jpg" not in archive.namelist()
    assert not (output / "camera_yolo_preview_annotated.jpg").exists()


class _LifecycleFake:
    def __init__(self, role: str, events: list[str]) -> None:
        self.role = role
        self.events = events
        self.start_count = self.stop_count = self.initialize_count = self.close_count = 0
        self.model_loaded = True
        self.detector_error = ""

    def start(self) -> None:
        self.start_count += 1
        self.events.append(f"{self.role}.start")

    def stop(self) -> None:
        self.stop_count += 1
        self.events.append(f"{self.role}.stop")

    def initialize(self) -> None:
        self.initialize_count += 1
        self.events.append(f"{self.role}.initialize")

    def close(self) -> None:
        self.close_count += 1
        self.events.append(f"{self.role}.close")

    def reset(self) -> None:
        self.events.append(f"{self.role}.reset")

    def reset_statistics(self, clear_buffers: bool = False) -> None:
        self.events.append(f"{self.role}.reset_statistics:{clear_buffers}")

    @staticmethod
    def get_statistics() -> dict[str, int | float]:
        return {
            "preview_fps": 0.0,
            "preview_encode_ms": 0.0,
            "preview_encode_median_ms": 0.0,
            "preview_encode_p95_ms": 0.0,
            "preview_submitted_count": 0,
            "preview_encoded_count": 0,
            "preview_overwritten_count": 0,
        }


def test_runner_delays_model_load_and_uses_clean_preview_boundaries(
    monkeypatch, tmp_path: Path
) -> None:
    events: list[str] = []
    camera = _LifecycleFake("camera", events)
    detector = _LifecycleFake("detector", events)
    tracker = _LifecycleFake("tracker", events)
    preview = _LifecycleFake("preview", events)
    runner = benchmark.HardwareBenchmark(
        camera, detector, tracker, preview,
        warmup_seconds=0, duration_seconds=0.001, output_dir=tmp_path,
    )

    def fake_period(profile, _seconds, *, collect, model_loaded_during_profile=False):
        events.append(f"period:{profile}:{'measure' if collect else 'warmup'}")
        return {
            "profile": profile,
            "model_loaded_during_profile": model_loaded_during_profile,
        }

    monkeypatch.setattr(runner, "_run_period", fake_period)
    summaries = runner.run([
        "camera_yolo_preview", "camera_only", "camera_preview",
        "camera_yolo", "camera_only",
    ])
    assert [item["profile"] for item in summaries] == list(benchmark.PROFILE_NAMES)
    assert [item["model_loaded_during_profile"] for item in summaries] == [
        False, False, True, True,
    ]
    first_initialize = events.index("detector.initialize")
    assert events.index("period:camera_only:measure") < first_initialize
    assert events.index("period:camera_preview:measure") < first_initialize
    assert first_initialize < events.index("period:camera_yolo:warmup")
    assert detector.initialize_count == detector.close_count == 1
    assert camera.start_count == camera.stop_count == 1
    assert events.count("preview.reset_statistics:True") == 2
    assert preview.start_count == preview.stop_count == 4


class _TickClock:
    def __init__(self) -> None:
        self.value = 10.0

    def __call__(self) -> float:
        self.value += 0.001
        return self.value


class _FrameCamera:
    def __init__(self, clock: _TickClock) -> None:
        self.clock = clock
        self.frame_id = 0

    def get_latest_frame(self) -> FramePacket:
        self.frame_id += 1
        return FramePacket(
            self.frame_id,
            self.clock.value - 0.005,
            np.zeros((24, 32, 3), np.uint8),
        )

    @staticmethod
    def get_statistics() -> dict[str, float]:
        return {"actual_fps": 30.0}


class _Detector:
    model_loaded = True
    detector_error = ""

    def process(self, frame: FramePacket) -> VisionResult:
        return VisionResult(
            frame.frame_id,
            frame.capture_timestamp,
            time.monotonic(),
            found=True,
            target_state=TargetState.CANDIDATE,
            target_class=200,
            center_x=16,
            center_y=12,
            confidence=900,
            image_width=32,
            image_height=24,
        )

    @staticmethod
    def get_runtime_status() -> dict[str, float]:
        return {
            "preprocess_ms": 1.0,
            "inference_ms": 2.0,
            "postprocess_ms": 3.0,
            "total_ms": 6.0,
        }

    @staticmethod
    def draw_debug(image, _result):
        return image.copy()


class _Tracker:
    @staticmethod
    def update(result: VisionResult) -> VisionResult:
        return result


class _Preview:
    def __init__(self) -> None:
        self.submitted = 0

    def submit_frame(self, _image) -> None:
        self.submitted += 1

    def get_statistics(self) -> dict[str, int | float]:
        return {
            "preview_fps": 10.0 if self.submitted else 0.0,
            "preview_encode_ms": 1.2 if self.submitted else 0.0,
            "preview_encode_median_ms": 1.1 if self.submitted else 0.0,
            "preview_encode_p95_ms": 1.4 if self.submitted else 0.0,
            "preview_submitted_count": self.submitted,
            "preview_encoded_count": self.submitted,
            "preview_overwritten_count": 0,
        }


def test_yolo_profiles_record_vision_loop_and_non_yolo_profiles_use_zero(
    monkeypatch, tmp_path: Path
) -> None:
    clock = _TickClock()
    def slow_unsupported_resource(*_args):
        clock()
        clock()
        return None

    monkeypatch.setattr(benchmark, "_read_rss_kib", slow_unsupported_resource)
    monkeypatch.setattr(benchmark, "_read_temperature_c", slow_unsupported_resource)
    monkeypatch.setattr(benchmark, "_run_vcgencmd", slow_unsupported_resource)
    runner = benchmark.HardwareBenchmark(
        _FrameCamera(clock), _Detector(), _Tracker(), _Preview(),
        warmup_seconds=0, duration_seconds=0.02, output_dir=tmp_path, clock=clock,
    )
    summaries = {}
    for profile in benchmark.PROFILE_NAMES:
        summaries[profile] = runner._run_period(
            profile,
            0.02,
            collect=True,
            model_loaded_during_profile="yolo" in profile,
        )
    for profile in ("camera_only", "camera_preview"):
        assert summaries[profile]["vision_loop_median_ms"] == 0.0
        assert summaries[profile]["vision_loop_p95_ms"] == 0.0
    for profile in ("camera_yolo", "camera_yolo_preview"):
        assert summaries[profile]["vision_loop_median_ms"] > 0.0
        assert summaries[profile]["vision_loop_p95_ms"] > 0.0
    grouped_samples = {
        profile: [row for row in runner.samples if row["profile"] == profile]
        for profile in benchmark.PROFILE_NAMES
    }
    assert all(row["vision_loop_ms"] == 0.0 for row in grouped_samples["camera_only"])
    assert all(row["vision_loop_ms"] == 0.0 for row in grouped_samples["camera_preview"])
    assert all(row["vision_loop_ms"] > 0.0 for row in grouped_samples["camera_yolo"])
    assert all(row["vision_loop_ms"] > 0.0 for row in grouped_samples["camera_yolo_preview"])
    for profile in ("camera_yolo", "camera_yolo_preview"):
        capture_samples = [row["capture_to_result_ms"] for row in grouped_samples[profile]]
        assert summaries[profile]["capture_to_result_median_ms"] == round(
            statistics.median(capture_samples), 3
        )
        assert len(set(capture_samples)) == 1
    assert {
        row["capture_to_result_ms"] for row in grouped_samples["camera_yolo"]
    } == {
        row["capture_to_result_ms"] for row in grouped_samples["camera_yolo_preview"]
    }
    assert all(row["capture_to_result_ms"] == 0.0 for row in grouped_samples["camera_only"])
    assert all(row["capture_to_result_ms"] == 0.0 for row in grouped_samples["camera_preview"])


def test_report_contains_required_profile_table_and_execution_log(tmp_path: Path) -> None:
    summary = {name: index for index, name in enumerate(benchmark.REPORT_COLUMNS)}
    summary["profile"] = "camera_yolo"
    payload = {
        "hardware_benchmark_executed": True,
        "hardware_benchmark_required": False,
        "platform": "Linux",
        "parameters": {"duration_seconds": 30},
        "profiles": [summary],
    }
    benchmark._write_outputs(
        tmp_path, payload, [summary], [],
        ["profile started: camera_yolo", "profile completed: camera_yolo"],
    )
    report = (tmp_path / "VISION_BASELINE_REPORT.md").read_text(encoding="utf-8")
    assert "| " + " | ".join(benchmark.REPORT_COLUMNS) + " |" in report
    assert "| camera_yolo |" in report
    log = (tmp_path / "execution.log").read_text(encoding="utf-8")
    assert "platform=Linux" in log
    assert 'parameters={"duration_seconds": 30}' in log
    assert "profile started: camera_yolo" in log
    assert "profile completed: camera_yolo" in log
    assert "output_path=" in log


def test_hardware_zip_requires_current_successful_yolo_preview_annotation(
    tmp_path: Path,
) -> None:
    stale = tmp_path / benchmark.ANNOTATED_IMAGE_NAME
    stale.write_bytes(b"old")
    summary = {
        "profile": "camera_only",
        "annotated_image_generated": False,
    }
    payload = {
        "hardware_benchmark_executed": True,
        "hardware_benchmark_required": False,
        "platform": "Linux",
        "parameters": {},
        "profiles": [summary],
    }
    archive_path = benchmark._write_outputs(tmp_path, payload, [summary], [])
    with zipfile.ZipFile(archive_path) as archive:
        assert benchmark.ANNOTATED_IMAGE_NAME not in archive.namelist()
