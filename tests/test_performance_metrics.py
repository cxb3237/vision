from __future__ import annotations

import math

import pytest

from core.performance_metrics import RollingRate, RollingSamples


def test_rolling_rate_empty_and_single_sample_are_safe() -> None:
    rate = RollingRate(window_seconds=2.0, clock=lambda: 1.0)
    assert rate.rate() == 0.0
    rate.record(1.0)
    assert rate.rate(1.0) == 0.0


def test_rolling_rate_uses_only_recent_window() -> None:
    rate = RollingRate(window_seconds=2.0)
    for timestamp in (0.0, 0.5, 1.0, 2.1):
        rate.record(timestamp)
    assert rate.event_count == 3
    assert rate.rate(2.1) == pytest.approx(1.25)


def test_rolling_rate_reset_and_backward_time() -> None:
    rate = RollingRate()
    rate.record(10.0)
    rate.record(11.0)
    assert rate.rate(11.0) == 1.0
    rate.reset()
    assert rate.rate(11.0) == 0.0
    rate.record(5.0)
    rate.record(4.0)
    assert rate.event_count == 1


def test_rolling_samples_summary_and_bound() -> None:
    samples = RollingSamples(max_samples=5)
    for value in range(1, 7):
        samples.add(value)
    summary = samples.summary()
    assert summary["count"] == 5
    assert summary["last"] == 6
    assert summary["median"] == 4
    assert summary["p95"] == pytest.approx(5.8)
    assert summary["min"] == 2
    assert summary["max"] == 6


def test_rolling_samples_ignores_non_finite_values_and_resets() -> None:
    samples = RollingSamples()
    assert not samples.add(math.nan)
    assert not samples.add(math.inf)
    assert not samples.add(-math.inf)
    assert samples.summary()["count"] == 0
    samples.add(2.0)
    samples.reset()
    assert samples.summary() == {
        "count": 0,
        "last": 0.0,
        "mean": 0.0,
        "median": 0.0,
        "p95": 0.0,
        "min": 0.0,
        "max": 0.0,
    }

