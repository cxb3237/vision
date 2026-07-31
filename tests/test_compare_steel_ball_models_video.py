from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from tools.compare_steel_ball_models_video import (
    FRAME_FIELDS,
    classify_motion,
    main,
    missing_runs,
    open_video_writer,
    process_model_pass,
    run_comparison,
)


class FakeDetector:
    def __init__(self, config) -> None:
        self.config = config
        self.model_loaded = False
        self.detector_error = ""
        self.last_frame_id = 0
        self.closed = False

    def initialize(self) -> None:
        self.model_loaded = True

    def process(self, packet):
        self.last_frame_id = packet.frame_id
        if packet.frame_id < 0:
            found = False
        elif "candidate" in str(self.config.model_path):
            found = packet.frame_id not in {0, 2}
        else:
            found = packet.frame_id not in {1, 2}
        return SimpleNamespace(
            found=found,
            bbox_x=10 + max(0, packet.frame_id),
            bbox_y=12,
            bbox_width=20,
            bbox_height=18,
            center_x=20 + max(0, packet.frame_id),
            center_y=21,
            confidence=800 + (10 if "candidate" in str(self.config.model_path) else 0),
        )

    def get_runtime_status(self):
        return {"inference_ms": 2.0 if "candidate" in str(self.config.model_path) else 3.0}

    def close(self) -> None:
        self.closed = True


def write_config(path: Path, model_name: str) -> None:
    model_dir = path.parent / model_name
    model_dir.mkdir()
    path.write_text(
        "\n".join(
            [
                "backend: ncnn",
                f"model_path: {model_dir.as_posix()}",
                "imgsz: 416",
                "conf_threshold: 0.40",
                "iou_threshold: 0.60",
                "max_det: 30",
                "num_threads: 4",
                "target_class: 100",
                "debug_tensor_shapes: false",
            ]
        ),
        encoding="utf-8",
    )


def write_video(path: Path, frame_count: int = 8) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (96, 64)
    )
    assert writer.isOpened()
    try:
        for index in range(frame_count):
            frame = np.zeros((64, 96, 3), dtype=np.uint8)
            if index not in {0, 1}:
                cv2.circle(frame, (10 + index * 6, 32), 6, (255, 255, 255), -1)
            writer.write(frame)
    finally:
        writer.release()


@pytest.fixture
def comparison_inputs(tmp_path: Path):
    video = tmp_path / "source.avi"
    baseline = tmp_path / "baseline.yaml"
    candidate = tmp_path / "candidate.yaml"
    write_video(video)
    write_config(baseline, "baseline_model")
    write_config(candidate, "candidate_model")
    return video, baseline, candidate


def test_full_fake_workflow_aligns_frames_and_writes_complete_csv(
    comparison_inputs, tmp_path: Path
) -> None:
    video, baseline, candidate = comparison_inputs
    output = tmp_path / "result"
    summary = run_comparison(
        video,
        baseline,
        candidate,
        output,
        warmup=1,
        write_videos=False,
        progress_every=0,
        detector_factory=FakeDetector,
    )
    with (output / "frames.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert tuple(rows[0]) == FRAME_FIELDS
    assert [int(row["frame_index"]) for row in rows] == list(range(8))
    assert summary["baseline"]["processed_frames"] == 8
    assert {row["motion_level"] for row in rows} <= {"static", "slow", "fast"}
    assert json.loads((output / "summary.json").read_text(encoding="utf-8"))[
        "comparison"
    ] == summary["comparison"]
    assert (output / "report.md").is_file()
    assert (output / "run_manifest.json").is_file()


def test_baseline_only_and_candidate_only_counts(comparison_inputs, tmp_path: Path) -> None:
    video, baseline, candidate = comparison_inputs
    summary = run_comparison(
        video,
        baseline,
        candidate,
        tmp_path / "counts",
        warmup=0,
        write_videos=False,
        progress_every=0,
        detector_factory=FakeDetector,
    )
    assert summary["comparison"]["baseline_only_frames"] == 1
    assert summary["comparison"]["candidate_only_frames"] == 1
    assert summary["comparison"]["neither_detected_frames"] == 1


def test_missing_run_calculation_is_exact() -> None:
    rows = [
        {
            "frame_index": index,
            "timestamp_ms": index * 100.0,
            "baseline_detected": int(index not in {1, 2, 3, 6}),
            "motion_score": float(index),
            "motion_level": "slow",
        }
        for index in range(8)
    ]
    run = missing_runs(rows, "baseline", 10.0)[0]
    assert run["frames"] == 3
    assert (run["start_frame"], run["end_frame"]) == (1, 3)
    assert run["duration_ms"] == 300.0


def test_motion_thresholds_handle_zero_and_repeated_scores() -> None:
    thresholds = classify_motion([0, 0, 0, 1, 1, 1, 5, 9])
    assert thresholds["static_max"] <= thresholds["slow_max"]
    assert thresholds["near_duplicate"] >= thresholds["near_zero"]


def test_max_frames_and_frame_step_apply(comparison_inputs, tmp_path: Path) -> None:
    video, baseline, candidate = comparison_inputs
    output = tmp_path / "limited"
    summary = run_comparison(
        video,
        baseline,
        candidate,
        output,
        warmup=0,
        frame_step=2,
        max_frames=3,
        write_videos=False,
        progress_every=0,
        detector_factory=FakeDetector,
    )
    with (output / "frames.csv").open(encoding="utf-8-sig", newline="") as stream:
        indices = [int(row["frame_index"]) for row in csv.DictReader(stream)]
    assert indices == [0, 2, 4]
    assert summary["input_video"]["processed_frame_count"] == 3


def test_candidate_first_order_is_supported(comparison_inputs, tmp_path: Path) -> None:
    video, baseline, candidate = comparison_inputs
    summary = run_comparison(
        video,
        baseline,
        candidate,
        tmp_path / "reverse",
        warmup=0,
        write_videos=False,
        progress_every=0,
        order="candidate,baseline",
        detector_factory=FakeDetector,
    )
    assert summary["baseline"]["processed_frames"] == 8
    assert summary["candidate"]["processed_frames"] == 8


def test_invalid_video_returns_nonzero(tmp_path: Path) -> None:
    assert main(["--video", str(tmp_path / "missing.mp4"), "--no-write-videos"]) != 0


def test_video_writer_failure_reports_error(tmp_path: Path) -> None:
    class ClosedWriter:
        def isOpened(self):
            return False

        def release(self):
            pass

    with pytest.raises(OSError, match="VideoWriter"):
        open_video_writer(
            tmp_path,
            "broken",
            10.0,
            (64, 64),
            writer_factory=lambda *args: ClosedWriter(),
        )


def test_fake_workflow_can_write_three_videos(comparison_inputs, tmp_path: Path) -> None:
    video, baseline, candidate = comparison_inputs
    summary = run_comparison(
        video,
        baseline,
        candidate,
        tmp_path / "videos",
        warmup=0,
        max_frames=3,
        write_videos=True,
        progress_every=0,
        detector_factory=FakeDetector,
    )
    assert len(summary["video_outputs"]) == 3
    assert all(Path(item["path"]).stat().st_size > 0 for item in summary["video_outputs"])


def test_keyboard_interrupt_preserves_partial_csv(comparison_inputs, tmp_path: Path) -> None:
    video, baseline_path, _ = comparison_inputs
    from core.config_loader import load_steel_ball_ncnn_config

    class InterruptingDetector(FakeDetector):
        def process(self, packet):
            if packet.frame_id == 2:
                raise KeyboardInterrupt
            return super().process(packet)

    partial = tmp_path / "baseline_frames.partial.csv"
    with pytest.raises(KeyboardInterrupt):
        process_model_pass(
            "baseline",
            video,
            load_steel_ball_ncnn_config(baseline_path),
            warmup=0,
            frame_step=1,
            max_frames=0,
            progress_every=0,
            detector_factory=InterruptingDetector,
            calculate_motion=True,
            partial_csv_path=partial,
        )
    with partial.open(encoding="utf-8-sig", newline="") as stream:
        assert [int(row["frame_index"]) for row in csv.DictReader(stream)] == [0, 1]
