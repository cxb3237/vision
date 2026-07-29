"""Small, thread-safe rolling performance statistics.

The helpers in this module intentionally keep only bounded scalar history.  They
do not retain camera frames or detection results.
"""

from __future__ import annotations

from collections import deque
import math
import statistics
import threading
import time
from typing import Callable


class RollingRate:
    """Measure an event rate over a recent monotonic time window."""

    def __init__(
        self,
        window_seconds: float = 2.0,
        max_events: int = 512,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_events < 2:
            raise ValueError("max_events must be at least 2")
        self.window_seconds = float(window_seconds)
        self.max_events = int(max_events)
        self._clock = clock
        self._events: deque[float] = deque(maxlen=self.max_events)
        self._lock = threading.Lock()

    def record(self, timestamp: float | None = None) -> None:
        value = self._clock() if timestamp is None else float(timestamp)
        if not math.isfinite(value):
            return
        with self._lock:
            if self._events and value < self._events[-1]:
                # A monotonic clock should never move backwards.  Resetting is
                # safer than reporting an artificial negative/huge rate.
                self._events.clear()
            self._events.append(value)
            self._prune_locked(value)

    def rate(self, now: float | None = None) -> float:
        value = self._clock() if now is None else float(now)
        if not math.isfinite(value):
            return 0.0
        with self._lock:
            if self._events and value < self._events[-1]:
                self._events.clear()
                return 0.0
            self._prune_locked(value)
            if len(self._events) < 2:
                return 0.0
            span = self._events[-1] - self._events[0]
            return (len(self._events) - 1) / span if span > 0.0 else 0.0

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0] < cutoff:
            self._events.popleft()


class RollingSamples:
    """Bounded finite scalar samples with stable summary statistics."""

    def __init__(self, max_samples: int = 120) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self.max_samples = int(max_samples)
        self._values: deque[float] = deque(maxlen=self.max_samples)
        self._lock = threading.Lock()

    def add(self, value: float) -> bool:
        sample = float(value)
        if not math.isfinite(sample):
            return False
        with self._lock:
            self._values.append(sample)
        return True

    def reset(self) -> None:
        with self._lock:
            self._values.clear()

    def values(self) -> tuple[float, ...]:
        with self._lock:
            return tuple(self._values)

    def summary(self) -> dict[str, float | int]:
        values = self.values()
        if not values:
            return {
                "count": 0,
                "last": 0.0,
                "mean": 0.0,
                "median": 0.0,
                "p95": 0.0,
                "min": 0.0,
                "max": 0.0,
            }
        ordered = sorted(values)
        return {
            "count": len(values),
            "last": values[-1],
            "mean": statistics.fmean(values),
            "median": statistics.median(values),
            "p95": self._percentile(ordered, 0.95),
            "min": ordered[0],
            "max": ordered[-1],
        }

    def last(self) -> float:
        return float(self.summary()["last"])

    def mean(self) -> float:
        return float(self.summary()["mean"])

    def median(self) -> float:
        return float(self.summary()["median"])

    def p95(self) -> float:
        return float(self.summary()["p95"])

    def minimum(self) -> float:
        return float(self.summary()["min"])

    def maximum(self) -> float:
        return float(self.summary()["max"])

    @staticmethod
    def _percentile(ordered: list[float], fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        rank = (len(ordered) - 1) * fraction
        lower = int(math.floor(rank))
        upper = int(math.ceil(rank))
        if lower == upper:
            return ordered[lower]
        weight = rank - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight
