"""TargetTracker 状态和字段一致性测试。"""

import time

from core.models import TargetState, VisionResult
from detectors.target_tracker import TargetTracker


def result(found: bool, x: int = 100, y: int = 80) -> VisionResult:
    return VisionResult(
        1,
        time.monotonic(),
        time.monotonic(),
        found=found,
        center_x=x,
        center_y=y,
        error_x_px=x - 160,
        error_y_px=y - 120,
        bbox_x=x - 5,
        bbox_y=y - 5,
        bbox_width=10,
        bbox_height=10,
        area_px=100,
        confidence=800,
        image_width=320,
        image_height=240,
    )


def test_candidate_to_locked() -> None:
    tracker = TargetTracker(confirm_frames=2)
    assert tracker.update(result(True)).target_state == TargetState.CANDIDATE
    assert tracker.update(result(True)).target_state == TargetState.LOCKED


def test_short_miss_is_occluded_and_clears_public_fields() -> None:
    tracker = TargetTracker(confirm_frames=1, lost_frames=3)
    tracker.update(result(True))
    missing = tracker.update(result(False))
    assert missing.target_state == TargetState.OCCLUDED
    assert not missing.found
    assert missing.bbox_width == 0
    assert missing.confidence == 0
    assert tracker.hits == 0


def test_lost_clears_position_and_allows_far_reacquisition() -> None:
    tracker = TargetTracker(confirm_frames=2, lost_frames=2, max_jump_px=20)
    tracker.update(result(True, 20, 20))
    tracker.update(result(False))
    lost = tracker.update(result(False))
    assert lost.target_state == TargetState.LOST
    assert tracker.pos is None and tracker.hits == 0
    reacquired = tracker.update(result(True, 280, 200))
    assert reacquired.found
    assert reacquired.target_state == TargetState.CANDIDATE


def test_smoothing_recalculates_error_from_smoothed_center() -> None:
    tracker = TargetTracker(alpha=0.5, max_jump_px=200, confirm_frames=1)
    tracker.update(result(True, 100, 100))
    smoothed = tracker.update(result(True, 140, 120))
    assert (smoothed.center_x, smoothed.center_y) == (120, 110)
    assert smoothed.error_x_px == -40
    assert smoothed.error_y_px == -10


def test_large_jump_returns_consistent_missing_result() -> None:
    tracker = TargetTracker(max_jump_px=10, confirm_frames=1, lost_frames=2)
    tracker.update(result(True, 20, 20))
    jumped = tracker.update(result(True, 200, 200))
    assert not jumped.found
    assert jumped.target_state == TargetState.OCCLUDED
    assert jumped.center_x == jumped.center_y == 0
    assert jumped.area_px == 0
