"""ColorDetector 合成颜色图像和状态测试。"""

import time

import cv2
import numpy as np

from core.config_loader import load_color_config
from core.models import DetectorConfig, FramePacket, TargetState
from detectors.color_detector import ColorDetector


def frame(image: np.ndarray, frame_id: int = 1) -> FramePacket:
    """创建带当前时间戳的测试帧。"""

    return FramePacket(frame_id, time.monotonic(), image)


def test_red_and_input_unchanged() -> None:
    image = np.zeros((200, 300, 3), np.uint8)
    cv2.rectangle(image, (110, 60), (190, 140), (0, 0, 255), -1)
    before = image.copy()
    detector = ColorDetector(
        load_color_config()["red"],
        DetectorConfig(confirm_frames=2),
    )
    result = detector.process(frame(image))
    assert result.found and result.center_x == 150
    assert np.array_equal(image, before)
    assert result.target_state == TargetState.CANDIDATE
    assert detector.process(frame(image, 2)).target_state == TargetState.LOCKED


def test_small_none_and_lost() -> None:
    detector = ColorDetector(
        load_color_config()["red"],
        DetectorConfig(lost_frames=2),
    )
    image = np.zeros((100, 100, 3), np.uint8)
    assert not detector.process(frame(image)).found
    assert detector.process(frame(image, 2)).target_state in (
        TargetState.NONE,
        TargetState.LOST,
    )


def test_nearest_candidate_is_selected_after_lock() -> None:
    config = DetectorConfig(min_area=50, confirm_frames=1, max_jump_px=80)
    detector = ColorDetector(load_color_config()["red"], config)
    first = np.zeros((200, 300, 3), np.uint8)
    cv2.circle(first, (80, 100), 20, (0, 0, 255), -1)
    detector.process(frame(first))
    second = np.zeros_like(first)
    cv2.circle(second, (90, 100), 15, (0, 0, 255), -1)
    cv2.circle(second, (230, 100), 35, (0, 0, 255), -1)
    result = detector.process(frame(second, 2))
    assert result.center_x == 90


def test_jump_lost_then_far_reacquisition() -> None:
    config = DetectorConfig(
        min_area=50,
        confirm_frames=1,
        lost_frames=2,
        max_jump_px=20,
    )
    detector = ColorDetector(load_color_config()["red"], config)
    left = np.zeros((200, 300, 3), np.uint8)
    right = np.zeros_like(left)
    cv2.circle(left, (40, 100), 15, (0, 0, 255), -1)
    cv2.circle(right, (250, 100), 15, (0, 0, 255), -1)
    detector.process(frame(left))
    assert detector.process(frame(right, 2)).target_state == TargetState.OCCLUDED
    assert detector.process(frame(right, 3)).target_state == TargetState.LOST
    reacquired = detector.process(frame(right, 4))
    assert reacquired.found and reacquired.center_x == 250


def test_confidence_is_bounded_and_red_uses_both_hue_ranges() -> None:
    detector = ColorDetector(
        load_color_config()["red"],
        DetectorConfig(min_area=50),
    )
    hsv = np.zeros((100, 200, 3), np.uint8)
    hsv[20:80, 20:80] = (2, 255, 255)
    hsv[20:80, 120:180] = (178, 255, 255)
    image = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    result = detector.process(frame(image))
    assert result.found
    assert 0 <= result.confidence <= 1000
