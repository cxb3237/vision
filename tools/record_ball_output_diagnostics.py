"""Record the runtime status endpoint without accessing camera or UART hardware."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
import statistics
import time
from typing import Any
from urllib.request import urlopen


FIELDS = (
    "host_timestamp", "elapsed_ms", "camera_online", "camera_fps", "vision_fps",
    "model_loaded", "pipe_roi_geometry_valid", "ball_position_mm",
    "position_measurement_mm", "position_estimate_mm", "position_output_mm",
    "position_velocity_mm_s", "position_measurement_age_ms",
    "position_estimator_state", "position_output_source", "position_slew_limited",
    "position_last_output_step_mm", "uart_output_tx_hz", "position_tx_hz",
    "invalid_tx_hz", "uart_tx_jitter_ms", "uart_tx_jitter_p95_ms",
    "uart_tx_deadline_miss_count", "last_uart_error",
)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _number(rows: list[dict[str, Any]], name: str) -> list[float]:
    result = []
    for row in rows:
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            result.append(float(value))
    return result


def _summary(rows: list[dict[str, Any]], duration_s: float, errors: int) -> dict[str, Any]:
    states = Counter(str(row.get("position_estimator_state") or "UNKNOWN") for row in rows)
    rates = _number(rows, "uart_output_tx_hz")
    jitters = _number(rows, "uart_tx_jitter_ms")
    outputs = _number(rows, "position_output_mm")
    camera_rates = _number(rows, "camera_fps")
    vision_rates = _number(rows, "vision_fps")
    sample_count = len(rows)
    ratios = {state: states[state] / sample_count if sample_count else 0.0 for state in ("MEASURED", "PREDICTED", "HELD", "LOST")}
    return {
        "duration_s": duration_s,
        "valid_sample_count": sample_count,
        "error_count": errors,
        "estimator_state_counts": dict(states),
        "estimator_state_ratios": ratios,
        "uart_output_hz_mean": statistics.fmean(rates) if rates else 0.0,
        "uart_output_hz_min": min(rates, default=0.0),
        "uart_output_hz_max": max(rates, default=0.0),
        "uart_jitter_ms_p50": _percentile(jitters, 0.50),
        "uart_jitter_ms_p95": _percentile(jitters, 0.95),
        "uart_jitter_ms_max": max(jitters, default=0.0),
        "maximum_adjacent_output_step_mm": max((abs(b - a) for a, b in zip(outputs, outputs[1:])), default=0.0),
        "slew_limited_samples": sum(bool(row.get("position_slew_limited")) for row in rows),
        "position_tx_count": int(rows[-1].get("position_tx_count", 0)) if rows else 0,
        "invalid_tx_count": int(rows[-1].get("invalid_tx_count", 0)) if rows else 0,
        "uart_errors": sum(bool(row.get("last_uart_error")) for row in rows),
        "camera_fps_mean": statistics.fmean(camera_rates) if camera_rates else 0.0,
        "vision_fps_mean": statistics.fmean(vision_rates) if vision_rates else 0.0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/api/status")
    parser.add_argument("--duration", type=float, default=40.0)
    parser.add_argument("--sample-rate", type=float, default=50.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=0.5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.duration <= 0 or args.sample_rate <= 0 or args.timeout <= 0:
        build_parser().error("duration, sample-rate and timeout must be positive")
    period = 1.0 / args.sample_rate
    started = time.monotonic()
    deadline = started
    rows: list[dict[str, Any]] = []
    errors = 0
    try:
        while time.monotonic() - started < args.duration:
            now = time.monotonic()
            if now < deadline:
                time.sleep(min(deadline - now, period))
            sampled = time.monotonic()
            try:
                with urlopen(args.url, timeout=args.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                status = payload.get("status", payload)
                row = {name: status.get(name) for name in FIELDS}
                row["host_timestamp"] = time.time()
                row["elapsed_ms"] = (sampled - started) * 1000.0
                row["position_tx_count"] = status.get("position_tx_count", 0)
                row["invalid_tx_count"] = status.get("invalid_tx_count", 0)
                rows.append(row)
            except Exception:
                errors += 1
            deadline += period
            if deadline <= sampled:
                deadline += (math.floor((sampled - deadline) / period) + 1) * period
    except KeyboardInterrupt:
        pass
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=(*FIELDS, "position_tx_count", "invalid_tx_count"))
            writer.writeheader()
            writer.writerows(rows)
        elapsed = time.monotonic() - started
        (args.output.parent / "summary.json").write_text(
            json.dumps(_summary(rows, elapsed, errors), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
