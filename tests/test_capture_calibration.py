"""交互式标定采集工具的无硬件、无 GUI 测试。"""

import cv2
import numpy as np

from tools.capture_calibration import (
    calculate_blur_score,
    calculate_board_area_ratio,
    evaluate_capture_quality,
    next_calibration_index,
)


def _sharp_checkerboard() -> np.ndarray:
    image = np.zeros((240, 320), np.uint8)
    square = 30
    for row in range(8):
        for column in range(10):
            if (row + column) % 2 == 0:
                start = (column * square, row * square)
                end = ((column + 1) * square, (row + 1) * square)
                cv2.rectangle(image, start, end, 255, -1)
    return image


def test_sharp_checkerboard_blur_score_exceeds_blurred_image() -> None:
    sharp = _sharp_checkerboard()
    blurred = cv2.GaussianBlur(sharp, (21, 21), 0)
    assert calculate_blur_score(sharp) > calculate_blur_score(blurred)


def test_board_area_ratio_uses_all_corner_bounding_rectangle() -> None:
    corners = np.array([[[10, 20]], [[50, 20]], [[10, 60]], [[50, 60]]], np.float32)
    ratio = calculate_board_area_ratio(corners, 100, 100)
    assert ratio == 0.16


def test_not_found_cannot_be_saved() -> None:
    result = evaluate_capture_quality(False, 200, 0.5, 1, None, 80, 0.08)
    assert not result.ready
    assert result.reason == "NOT FOUND"


def test_blurry_frame_cannot_be_saved() -> None:
    result = evaluate_capture_quality(True, 20, 0.5, 1, None, 80, 0.08)
    assert not result.ready
    assert result.reason == "TOO BLURRY"


def test_small_board_cannot_be_saved() -> None:
    result = evaluate_capture_quality(True, 200, 0.02, 1, None, 80, 0.08)
    assert not result.ready
    assert result.reason == "BOARD TOO SMALL"


def test_qualified_frame_can_be_saved() -> None:
    result = evaluate_capture_quality(True, 200, 0.2, 1, None, 80, 0.08)
    assert result.ready
    assert result.reason == "READY"


def test_same_frame_id_cannot_be_saved_twice() -> None:
    result = evaluate_capture_quality(True, 200, 0.2, 7, 7, 80, 0.08)
    assert not result.ready
    assert result.reason == "DUPLICATE FRAME"


def test_next_file_index_does_not_overwrite_existing_images(tmp_path) -> None:
    (tmp_path / "calib_0001.jpg").write_bytes(b"existing")
    (tmp_path / "calib_0025.jpg").write_bytes(b"existing")
    (tmp_path / "other.jpg").write_bytes(b"ignored")
    assert next_calibration_index(tmp_path) == 26
