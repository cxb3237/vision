"""Deterministic Windows-friendly estimator simulation; no camera or UART access."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.ball_position_estimator import BallPositionEstimator, BallPositionEstimatorConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/ball_estimator_simulation.csv"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    estimator = BallPositionEstimator(BallPositionEstimatorConfig())
    rows = []
    timestamp = 1000.0
    scenarios = [
        ("stationary", 0.6, lambda age: 0.0, True),
        ("constant_100mm_s", 0.8, lambda age: age * 100.0, True),
        ("lost_100ms", 0.1, lambda age: 80.0, False),
        ("lost_200ms", 0.2, lambda age: 80.0, False),
        ("lost_500ms", 0.5, lambda age: 80.0, False),
        ("reacquire", 0.2, lambda age: -40.0, True),
        ("single_outlier", 0.1, lambda age: 120.0 if age < 0.03 else -40.0, True),
        ("boundaries", 0.8, lambda age: -125.0 if age < 0.4 else 125.0, True),
    ]
    for name, duration, position, measured in scenarios:
        steps = round(duration / 0.02)
        for index in range(steps):
            age = index * 0.02
            timestamp += 0.02
            if measured and index % 2 == 0:
                estimator.update_measurement(position(age), timestamp, confidence=0.9, roi_valid=True)
            sample = estimator.sample_output(timestamp)
            rows.append({
                "scenario": name,
                "elapsed_ms": round((timestamp - 1000.0) * 1000.0, 3),
                "measurement_mm": estimator.position_mm,
                "estimate_mm": sample.estimate_mm,
                "output_mm": sample.output_mm,
                "state": sample.state.value,
                "measurement_age_ms": sample.measurement_age_ms,
                "slew_limited": sample.slew_limited,
                "send_invalid": sample.output_mm is None,
            })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    invalid = sum(bool(row["send_invalid"]) for row in rows)
    states = sorted({str(row["state"]) for row in rows})
    print(f"samples={len(rows)} invalid={invalid} states={','.join(states)} csv={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
