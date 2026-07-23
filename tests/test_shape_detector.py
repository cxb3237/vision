"""ShapeDetector 合成图像测试。"""

import time

import cv2
import numpy as np
import pytest

from core.models import FramePacket
from detectors.shape_detector import SHAPES, ShapeDetector


def packet(image: np.ndarray) -> FramePacket:
    return FramePacket(1, time.monotonic(), image)


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("triangle", SHAPES["triangle"]),
        ("rectangle", SHAPES["rectangle"]),
        ("square", SHAPES["square"]),
        ("circle", SHAPES["circle"]),
    ],
)
def test_synthetic_shapes(shape: str, expected: int) -> None:
    image = np.zeros((240, 320, 3), np.uint8)
    if shape == "triangle":
        cv2.fillPoly(image, [np.array([[160, 30], [80, 190], [240, 190]])], (255, 255, 255))
    elif shape == "rectangle":
        cv2.rectangle(image, (50, 70), (270, 170), (255, 255, 255), -1)
    elif shape == "square":
        cv2.rectangle(image, (90, 50), (230, 190), (255, 255, 255), -1)
    else:
        cv2.circle(image, (160, 120), 70, (255, 255, 255), -1)
    result = ShapeDetector().process(packet(image))
    assert result.found
    assert result.target_class == expected
    assert 0 <= result.confidence <= 1000


def test_no_target_and_invalid_image() -> None:
    detector = ShapeDetector()
    assert not detector.process(packet(np.zeros((100, 100, 3), np.uint8))).found
    with pytest.raises(ValueError):
        detector.process(packet(np.zeros((0, 0, 3), np.uint8)))


def test_draw_debug_changes_copy_not_input() -> None:
    image = np.zeros((200, 200, 3), np.uint8)
    cv2.circle(image, (100, 100), 50, (255, 255, 255), -1)
    before = image.copy()
    detector = ShapeDetector()
    result = detector.process(packet(image))
    rendered = detector.draw_debug(image, result)
    assert np.array_equal(image, before)
    assert not np.array_equal(rendered, image)
