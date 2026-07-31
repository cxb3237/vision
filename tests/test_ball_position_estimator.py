from __future__ import annotations

import math

import pytest

from core.ball_position_estimator import (
    BallEstimateState,
    BallPositionEstimator,
    BallPositionEstimatorConfig,
)


def make_estimator(**overrides) -> BallPositionEstimator:
    values = {
        name: field.default
        for name, field in BallPositionEstimatorConfig.__dataclass_fields__.items()
    }
    values.update(overrides)
    return BallPositionEstimator(BallPositionEstimatorConfig(**values))


def test_first_measurement_initializes_estimator() -> None:
    estimator = make_estimator()
    assert estimator.update_measurement(12.0, 10.0, confidence=0.9)
    sample = estimator.sample_output(10.0)
    assert sample.state is BallEstimateState.MEASURED
    assert sample.output_mm == 12.0
    assert estimator.measurement_count == 1


def test_stationary_measurements_remain_stable() -> None:
    estimator = make_estimator()
    for index in range(10):
        assert estimator.update_measurement(5.0, 20.0 + index * 0.02)
    sample = estimator.sample_output(20.18)
    assert sample.estimate_mm == pytest.approx(5.0)
    assert sample.velocity_mm_s == pytest.approx(0.0)


def test_uniform_motion_produces_positive_bounded_velocity() -> None:
    estimator = make_estimator(measurement_gate_enabled=False)
    for index in range(12):
        estimator.update_measurement(index * 2.0, 30.0 + index * 0.02)
    assert 0.0 < estimator.velocity_mm_s <= estimator.config.max_speed_mm_s


@pytest.mark.parametrize(
    ("age_ms", "state", "has_position"),
    [
        (100, BallEstimateState.PREDICTED, True),
        (200, BallEstimateState.HELD, True),
        (500, BallEstimateState.LOST, False),
    ],
)
def test_measurement_age_selects_output_state(age_ms, state, has_position) -> None:
    estimator = make_estimator()
    estimator.update_measurement(0.0, 40.0)
    sample = estimator.sample_output(40.0 + age_ms / 1000.0)
    assert sample.state is state
    assert (sample.output_mm is not None) is has_position
    assert sample.output_source == (state.value if has_position else "INVALID")


def test_held_velocity_decays() -> None:
    estimator = make_estimator(measurement_gate_enabled=False)
    estimator.update_measurement(0.0, 50.0)
    estimator.update_measurement(10.0, 50.02)
    initial_velocity = estimator.velocity_mm_s
    held = estimator.sample_output(50.20)
    assert held.state is BallEstimateState.HELD
    assert 0.0 < held.velocity_mm_s < initial_velocity


def test_position_and_velocity_are_physically_bounded() -> None:
    estimator = make_estimator(measurement_gate_enabled=False, max_speed_mm_s=100.0)
    estimator.update_measurement(120.0, 60.0)
    estimator.update_measurement(125.0, 60.01)
    sample = estimator.sample_output(60.15)
    assert -125.0 <= sample.estimate_mm <= 125.0
    assert abs(estimator.velocity_mm_s) <= 100.0


def test_rejects_reversed_timestamps_nan_and_out_of_range() -> None:
    estimator = make_estimator()
    assert estimator.update_measurement(0.0, 70.0)
    assert not estimator.update_measurement(1.0, 69.0)
    assert not estimator.update_measurement(math.nan, 70.1)
    assert not estimator.update_measurement(126.0, 70.2)
    assert estimator.rejected_measurement_count == 3


def test_single_outlier_is_gated_but_two_consistent_measurements_reacquire() -> None:
    estimator = make_estimator()
    estimator.update_measurement(0.0, 80.0)
    assert not estimator.update_measurement(100.0, 80.1, confidence=1.0)
    assert estimator.position_mm == 0.0
    assert estimator.update_measurement(101.0, 80.2, confidence=1.0)
    assert estimator.position_mm == 101.0


def test_invalid_roi_measurement_is_rejected() -> None:
    estimator = make_estimator()
    assert not estimator.update_measurement(0.0, 90.0, roi_valid=False)
    assert estimator.sample_output(90.0).state is BallEstimateState.UNINITIALIZED


def test_output_slew_is_limited_using_actual_period() -> None:
    estimator = make_estimator(measurement_gate_enabled=False)
    estimator.update_measurement(0.0, 100.0)
    assert estimator.sample_output(100.0).output_mm == 0.0
    estimator.update_measurement(100.0, 100.02)
    sample = estimator.sample_output(100.02)
    assert sample.slew_limited is True
    assert sample.output_mm == pytest.approx(12.0)
    assert sample.max_output_step_mm == pytest.approx(12.0)


def test_reacquisition_after_invalid_is_slew_limited() -> None:
    estimator = make_estimator()
    estimator.update_measurement(0.0, 110.0)
    estimator.sample_output(110.0)
    assert estimator.sample_output(110.5).output_mm is None
    assert not estimator.update_measurement(100.0, 110.5)
    assert estimator.sample_output(110.52).output_mm is None
    assert estimator.update_measurement(101.0, 110.54)
    sample = estimator.sample_output(110.54)
    assert sample.output_mm == pytest.approx(12.0)
    assert sample.slew_limited
    assert estimator.reacquisition_count == 1


def test_reset_returns_to_uninitialized() -> None:
    estimator = make_estimator()
    estimator.update_measurement(4.0, 120.0)
    estimator.reset()
    assert estimator.sample_output(121.0).state is BallEstimateState.UNINITIALIZED
    assert estimator.measurement_count == 0
