from __future__ import annotations

import csv
from pathlib import Path

import cv2
import numpy as np

from tools.extract_steel_ball_hard_frames import (
    CATEGORIES,
    MANIFEST_FIELDS,
    classify_rows,
    extract_hard_frames,
    main,
)


FIELDS = (
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
)


def make_rows() -> list[dict[str, str]]:
    rows = []
    for index in range(10):
        rows.append(
            {
                "frame_index": str(index),
                "timestamp_ms": str(index * 100),
                "motion_level": "fast" if index in {2, 3} else "slow",
                "baseline_detected": "1" if index in {0, 1, 4, 5} else "0",
                "baseline_confidence": "0.45" if index in {4, 5} else "0.80",
                "candidate_detected": "0" if index in {2, 3, 6, 7, 8} else "1",
                "candidate_roi_geometry_valid": "0" if index == 9 else "1",
                "candidate_roi_accepted_count": "0" if index in {0, 1, 2, 3, 6, 7, 8, 9} else "1",
                "candidate_roi_reason": "outside_corridor" if index in {0, 1} else "accepted",
                "candidate_raw_reference_roi_accepted": "0" if index in {0, 1} else ("1" if index in {4, 5} else ""),
                "candidate_low_conf_inside_roi_count": "1" if index in {4, 5} else "0",
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_video(path: Path) -> None:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (80, 60))
    assert writer.isOpened()
    try:
        for index in range(10):
            frame = np.zeros((60, 80, 3), dtype=np.uint8)
            # Frames 0/1 and 4/5 are deliberately identical for visual dedup tests.
            if index not in {0, 1, 4, 5}:
                cv2.circle(frame, (10 + index * 5, 30), 4, (255, 255, 255), -1)
            writer.write(frame)
    finally:
        writer.release()


def test_classification_covers_required_categories() -> None:
    result = classify_rows(make_rows(), max_per_category=3)
    assert [row["frame_index"] for row in result["roi_outside_review"]] == ["0", "1"]
    assert [row["frame_index"] for row in result["fast_no_output_review"]] == ["2", "3"]
    assert [row["frame_index"] for row in result["low_conf_inside_roi_review"]] == ["4", "5"]
    assert [row["frame_index"] for row in result["geometry_invalid_review"]] == ["9"]
    assert result["long_no_output_review"]


def test_extract_deduplicates_caps_and_writes_manifest(tmp_path: Path) -> None:
    video = tmp_path / "source.avi"
    source_csv = tmp_path / "frames.csv"
    output = tmp_path / "hard"
    write_video(video)
    write_csv(source_csv, make_rows())
    counts = extract_hard_frames(
        video,
        source_csv,
        output,
        max_per_category=2,
        minimum_interval_ms=0,
        duplicate_mad_threshold=1.0,
    )
    assert set(counts) == set(CATEGORIES)
    assert all(count <= 2 for count in counts.values())
    assert counts["roi_outside_review"] == 1
    with (output / "manifest.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        manifest = list(reader)
        assert tuple(reader.fieldnames or ()) == MANIFEST_FIELDS
    assert manifest
    assert (output / "README.md").is_file()
    assert not list(output.rglob("*.txt"))
    assert not list(output.rglob("*.labels"))


def test_missing_video_returns_nonzero(tmp_path: Path) -> None:
    csv_path = tmp_path / "frames.csv"
    write_csv(csv_path, make_rows())
    assert main(
        [
            "--video",
            str(tmp_path / "missing.mp4"),
            "--frames-csv",
            str(csv_path),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    ) != 0


def test_missing_csv_fields_are_reported(tmp_path: Path, capsys) -> None:
    video = tmp_path / "source.avi"
    bad_csv = tmp_path / "bad.csv"
    write_video(video)
    bad_csv.write_text("frame_index,timestamp_ms\n0,0\n", encoding="utf-8")
    code = main(
        [
            "--video",
            str(video),
            "--frames-csv",
            str(bad_csv),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert code != 0
    assert "缺少字段" in capsys.readouterr().err
