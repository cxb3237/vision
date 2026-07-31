"""Thread-safe one-dimensional steel-ball position and velocity estimator."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import threading
import time


class BallEstimateState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    MEASURED = "MEASURED"
    PREDICTED = "PREDICTED"
    HELD = "HELD"
    LOST = "LOST"


@dataclass(frozen=True, slots=True)
class BallPositionEstimatorConfig:
    enabled: bool = True
    alpha: float = 0.85
    beta: float = 0.08
    measurement_fresh_ms: float = 90.0
    prediction_horizon_ms: float = 150.0
    hold_horizon_ms: float = 300.0
    velocity_decay_tau_ms: float = 120.0
    max_speed_mm_s: float = 600.0
    max_output_slew_mm_s: float = 600.0
    minimum_measurement_dt_ms: float = 5.0
    maximum_measurement_dt_ms: float = 500.0
    position_min_mm: float = -125.0
    position_max_mm: float = 125.0
    measurement_gate_enabled: bool = True
    measurement_gate_base_mm: float = 20.0
    measurement_gate_speed_factor: float = 1.5
    measurement_reacquire_confirmations: int = 2


@dataclass(frozen=True, slots=True)
class BallPositionMeasurement:
    position_mm: float
    capture_timestamp: float
    confidence: float = 1.0
    roi_valid: bool = True


@dataclass(frozen=True, slots=True)
class BallPositionEstimate:
    state: BallEstimateState
    measurement_mm: float | None
    estimate_mm: float | None
    output_mm: float | None
    velocity_mm_s: float
    measurement_age_ms: float | None
    prediction_age_ms: float | None
    output_source: str
    slew_limited: bool
    last_output_step_mm: float
    max_output_step_mm: float


class BallPositionEstimator:
    """Alpha-beta estimator sampled independently from the vision frame rate."""

    def __init__(self, config: BallPositionEstimatorConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.position_mm: float | None = None
            self.velocity_mm_s = 0.0
            self.last_measurement_timestamp: float | None = None
            self.last_update_timestamp: float | None = None
            self.last_output_position_mm: float | None = None
            self._last_sample_timestamp: float | None = None
            self._last_output_was_valid = False
            self._pending_reacquire_position: float | None = None
            self._pending_reacquire_count = 0
            self.measurement_count = 0
            self.prediction_count = 0
            self.rejected_measurement_count = 0
            self.output_slew_limited_count = 0
            self.reacquisition_count = 0
            self.last_output_step_mm = 0.0
            self.max_output_step_mm = 0.0
            self._last_slew_limited = False

    def _reject(self) -> bool:
        self.rejected_measurement_count += 1
        return False

    def _initialize(self, measurement: BallPositionMeasurement) -> bool:
        self.position_mm = self._clamp_position(measurement.position_mm)
        self.velocity_mm_s = 0.0
        self.last_measurement_timestamp = measurement.capture_timestamp
        self.last_update_timestamp = measurement.capture_timestamp
        self.measurement_count += 1
        self._pending_reacquire_position = None
        self._pending_reacquire_count = 0
        return True

    def update_measurement(
        self,
        position_mm: float,
        capture_timestamp: float,
        *,
        confidence: float = 1.0,
        roi_valid: bool = True,
    ) -> bool:
        """Apply a reliable vision measurement using its capture timestamp."""

        try:
            measurement = BallPositionMeasurement(
                float(position_mm), float(capture_timestamp), float(confidence), bool(roi_valid)
            )
        except (TypeError, ValueError, OverflowError):
            with self._lock:
                return self._reject()
        with self._lock:
            if (
                not self.config.enabled
                or not measurement.roi_valid
                or not all(
                    math.isfinite(value)
                    for value in (
                        measurement.position_mm,
                        measurement.capture_timestamp,
                        measurement.confidence,
                    )
                )
                or not 0.0 <= measurement.confidence <= 1.0
                or not self.config.position_min_mm
                <= measurement.position_mm
                <= self.config.position_max_mm
            ):
                return self._reject()
            if self.last_measurement_timestamp is None or self.position_mm is None:
                return self._initialize(measurement)

            dt = measurement.capture_timestamp - self.last_measurement_timestamp
            if dt <= 0.0 or dt * 1000.0 < self.config.minimum_measurement_dt_ms:
                return self._reject()
            if (
                dt * 1000.0 > self.config.maximum_measurement_dt_ms
                or dt * 1000.0 > self.config.hold_horizon_ms
            ):
                if self.config.measurement_gate_enabled:
                    gate_dt = min(
                        dt,
                        self.config.maximum_measurement_dt_ms / 1000.0,
                    )
                    allowed = (
                        self.config.measurement_gate_base_mm
                        + abs(self.velocity_mm_s)
                        * gate_dt
                        * self.config.measurement_gate_speed_factor
                    )
                    if abs(measurement.position_mm - self.position_mm) > allowed:
                        if (
                            self._pending_reacquire_position is not None
                            and abs(
                                measurement.position_mm
                                - self._pending_reacquire_position
                            )
                            <= self.config.measurement_gate_base_mm
                        ):
                            self._pending_reacquire_count += 1
                        else:
                            self._pending_reacquire_count = 1
                        self._pending_reacquire_position = measurement.position_mm
                        if (
                            self._pending_reacquire_count
                            < self.config.measurement_reacquire_confirmations
                        ):
                            return self._reject()
                return self._initialize(measurement)

            predicted = self.position_mm + self.velocity_mm_s * dt
            residual = measurement.position_mm - predicted
            if self.config.measurement_gate_enabled:
                confidence_factor = 0.5 + 0.5 * measurement.confidence
                allowed = (
                    self.config.measurement_gate_base_mm * confidence_factor
                    + abs(self.velocity_mm_s)
                    * dt
                    * self.config.measurement_gate_speed_factor
                )
                if abs(residual) > allowed:
                    candidate_tolerance = max(
                        self.config.measurement_gate_base_mm,
                        abs(self.velocity_mm_s) * dt,
                    )
                    if (
                        self._pending_reacquire_position is not None
                        and abs(measurement.position_mm - self._pending_reacquire_position)
                        <= candidate_tolerance
                    ):
                        self._pending_reacquire_count += 1
                    else:
                        self._pending_reacquire_count = 1
                    self._pending_reacquire_position = measurement.position_mm
                    if (
                        self._pending_reacquire_count
                        < self.config.measurement_reacquire_confirmations
                    ):
                        return self._reject()
                    return self._initialize(measurement)

            self._pending_reacquire_position = None
            self._pending_reacquire_count = 0
            self.position_mm = self._clamp_position(
                predicted + self.config.alpha * residual
            )
            velocity = self.velocity_mm_s + self.config.beta * residual / dt
            self.velocity_mm_s = max(
                -self.config.max_speed_mm_s,
                min(self.config.max_speed_mm_s, velocity),
            )
            if (
                self.position_mm <= self.config.position_min_mm
                and self.velocity_mm_s < 0.0
            ) or (
                self.position_mm >= self.config.position_max_mm
                and self.velocity_mm_s > 0.0
            ):
                self.velocity_mm_s = 0.0
            self.last_measurement_timestamp = measurement.capture_timestamp
            self.last_update_timestamp = measurement.capture_timestamp
            self.measurement_count += 1
            return True

    def _clamp_position(self, value: float) -> float:
        return max(self.config.position_min_mm, min(self.config.position_max_mm, value))

    def _raw_estimate(self, now_timestamp: float) -> tuple[BallEstimateState, float | None, float, float | None]:
        if self.position_mm is None or self.last_measurement_timestamp is None:
            return BallEstimateState.UNINITIALIZED, None, 0.0, None
        age_ms = max(0.0, (now_timestamp - self.last_measurement_timestamp) * 1000.0)
        age_s = age_ms / 1000.0
        if age_ms <= self.config.measurement_fresh_ms:
            return BallEstimateState.MEASURED, self.position_mm, self.velocity_mm_s, age_ms
        if age_ms <= self.config.prediction_horizon_ms:
            position = self.position_mm + self.velocity_mm_s * age_s
            return BallEstimateState.PREDICTED, self._clamp_position(position), self.velocity_mm_s, age_ms
        if age_ms <= self.config.hold_horizon_ms:
            prediction_s = self.config.prediction_horizon_ms / 1000.0
            hold_s = max(0.0, age_s - prediction_s)
            tau_s = self.config.velocity_decay_tau_ms / 1000.0
            decay = math.exp(-hold_s / tau_s)
            position = (
                self.position_mm
                + self.velocity_mm_s * prediction_s
                + self.velocity_mm_s * tau_s * (1.0 - decay)
            )
            return BallEstimateState.HELD, self._clamp_position(position), self.velocity_mm_s * decay, age_ms
        return BallEstimateState.LOST, None, 0.0, age_ms

    def sample_output(self, now_timestamp: float | None = None) -> BallPositionEstimate:
        """Return one nonblocking output snapshot and apply output-only slew limiting."""

        now = time.monotonic() if now_timestamp is None else float(now_timestamp)
        with self._lock:
            state, estimate, velocity, age_ms = self._raw_estimate(now)
            output = estimate
            slew_limited = False
            max_step = 0.0
            step = 0.0
            if output is not None:
                if self.last_output_position_mm is not None and self._last_sample_timestamp is not None:
                    actual_period_s = max(0.0, now - self._last_sample_timestamp)
                    max_step = self.config.max_output_slew_mm_s * actual_period_s
                    raw_step = output - self.last_output_position_mm
                    step = max(-max_step, min(max_step, raw_step))
                    slew_limited = abs(step - raw_step) > 1e-9
                    output = self._clamp_position(self.last_output_position_mm + step)
                elif self.last_output_position_mm is not None:
                    output = self.last_output_position_mm
                    slew_limited = abs(estimate - output) > 1e-9
                if not self._last_output_was_valid and self.last_output_position_mm is not None:
                    self.reacquisition_count += 1
                self.last_output_position_mm = output
                self.last_output_step_mm = step
                self.max_output_step_mm = max_step
                self._last_output_was_valid = True
            else:
                self.last_output_step_mm = 0.0
                self.max_output_step_mm = 0.0
                self._last_output_was_valid = False
            self._last_sample_timestamp = now
            if state in {BallEstimateState.PREDICTED, BallEstimateState.HELD}:
                self.prediction_count += 1
            if slew_limited:
                self.output_slew_limited_count += 1
            self._last_slew_limited = slew_limited
            source = state.value if output is not None else "INVALID"
            return BallPositionEstimate(
                state,
                self.position_mm,
                estimate,
                output,
                velocity,
                age_ms,
                max(0.0, age_ms - self.config.measurement_fresh_ms) if age_ms is not None else None,
                source,
                slew_limited,
                self.last_output_step_mm,
                self.max_output_step_mm,
            )

    def get_status(self, now_timestamp: float | None = None) -> dict[str, object]:
        now = time.monotonic() if now_timestamp is None else float(now_timestamp)
        with self._lock:
            state, estimate, velocity, age_ms = self._raw_estimate(now)
            return {
                "position_estimator_enabled": self.config.enabled,
                "position_estimator_state": state.value,
                "position_measurement_mm": self.position_mm,
                "position_estimate_mm": estimate,
                "position_output_mm": self.last_output_position_mm if self._last_output_was_valid else None,
                "position_velocity_mm_s": velocity,
                "position_measurement_age_ms": age_ms,
                "position_prediction_age_ms": (
                    max(0.0, age_ms - self.config.measurement_fresh_ms)
                    if age_ms is not None
                    else None
                ),
                "position_measurement_count": self.measurement_count,
                "position_prediction_count": self.prediction_count,
                "position_rejected_measurement_count": self.rejected_measurement_count,
                "position_reacquisition_count": self.reacquisition_count,
                "position_slew_limited": self._last_slew_limited,
                "position_slew_limited_count": self.output_slew_limited_count,
                "position_last_output_step_mm": self.last_output_step_mm,
                "position_output_source": state.value if estimate is not None else "INVALID",
            }
