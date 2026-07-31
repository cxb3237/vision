from __future__ import annotations

import csv
import json

from tools import record_ball_output_diagnostics, simulate_ball_position_estimator


def test_simulation_covers_all_runtime_output_states(tmp_path) -> None:
    output = tmp_path / "simulation.csv"
    assert simulate_ball_position_estimator.main(["--output", str(output)]) == 0
    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    states = {row["state"] for row in rows}
    assert {"MEASURED", "PREDICTED", "HELD", "LOST"} <= states
    assert any(row["send_invalid"] == "True" for row in rows)


def test_diagnostic_summary_reports_rates_states_jitter_and_steps() -> None:
    rows = [
        {
            "position_estimator_state": "MEASURED",
            "uart_output_tx_hz": 49.0,
            "uart_tx_jitter_ms": 0.5,
            "position_output_mm": 0.0,
            "position_slew_limited": False,
            "position_tx_count": 10,
            "invalid_tx_count": 1,
            "camera_fps": 30.0,
            "vision_fps": 25.0,
            "last_uart_error": "",
        },
        {
            "position_estimator_state": "PREDICTED",
            "uart_output_tx_hz": 51.0,
            "uart_tx_jitter_ms": 1.5,
            "position_output_mm": 12.0,
            "position_slew_limited": True,
            "position_tx_count": 11,
            "invalid_tx_count": 1,
            "camera_fps": 30.0,
            "vision_fps": 25.0,
            "last_uart_error": "",
        },
    ]
    summary = record_ball_output_diagnostics._summary(rows, 1.0, 0)
    assert summary["estimator_state_counts"] == {"MEASURED": 1, "PREDICTED": 1}
    assert summary["uart_output_hz_mean"] == 50.0
    assert summary["maximum_adjacent_output_step_mm"] == 12.0
    assert summary["slew_limited_samples"] == 1


def test_diagnostic_writer_saves_partial_results_on_keyboard_interrupt(
    tmp_path, monkeypatch
) -> None:
    output = tmp_path / "test.csv"
    monkeypatch.setattr(
        record_ball_output_diagnostics,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert record_ball_output_diagnostics.main(
        ["--duration", "0.1", "--sample-rate", "50", "--output", str(output)]
    ) == 0
    assert output.is_file()
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["valid_sample_count"] == 0
