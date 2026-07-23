"""SteelBallDetector 的合成图像和时序状态测试。"""

from dataclasses import replace
import time

import cv2
import numpy as np

from core.models import CalibrationConfig, FramePacket, SteelBallConfig, TargetState
from detectors.steel_ball_detector import SteelBallDetector, estimate_distance_mm
from app import build_argument_parser as build_app_parser, create_detector
from core.config_loader import load_color_config, load_mission_config
from tools.replay_test import build_argument_parser as build_replay_parser


def detector_config(**overrides) -> SteelBallConfig:
    values = {
        "clahe_enabled": False,
        "gaussian_kernel": 3,
        "threshold_mode": "fixed",
        "threshold": 100,
        "invert": False,
        "morph_open": 0,
        "morph_close": 0,
        "min_diameter_px": 15.0,
        "max_diameter_px": 60.0,
        "min_area_px": 100.0,
        "max_area_px": 3000.0,
        "min_circularity": 0.75,
        "min_aspect_ratio": 0.80,
        "max_aspect_ratio": 1.20,
        "confirm_frames": 2,
        "lost_frames": 2,
        "max_jump_px": 60.0,
        "hough_enabled": False,
    }
    values.update(overrides)
    return SteelBallConfig(**values)


def packet(image: np.ndarray, frame_id: int = 1) -> FramePacket:
    return FramePacket(frame_id, time.monotonic(), image)


def blank() -> np.ndarray:
    return np.zeros((240, 320, 3), np.uint8)


def circle_image(center=(160, 120), radius=20) -> np.ndarray:
    image = blank()
    cv2.circle(image, center, radius, (255, 255, 255), -1)
    return image


def test_single_circle_is_detected() -> None:
    result = SteelBallDetector(detector_config()).process(packet(circle_image()))
    assert result.found
    assert abs(result.center_x - 160) <= 1
    assert result.target_state == TargetState.CANDIDATE
    assert result.distance_mm == 0xFFFF


def test_too_small_circle_is_filtered() -> None:
    detector = SteelBallDetector(detector_config())
    result = detector.process(packet(circle_image(radius=4)))
    assert not result.found
    assert detector.get_debug_data().rejected_by_area >= 1


def test_too_large_circle_is_filtered() -> None:
    result = SteelBallDetector(detector_config()).process(packet(circle_image(radius=50)))
    assert not result.found


def test_ellipse_is_filtered_by_aspect_ratio() -> None:
    image = blank()
    cv2.ellipse(image, (160, 120), (30, 12), 0, 0, 360, (255, 255, 255), -1)
    result = SteelBallDetector(detector_config()).process(packet(image))
    assert not result.found


def test_irregular_contour_is_filtered_by_circularity() -> None:
    image = blank()
    points = np.array(
        [
            [160, 80],
            [170, 108],
            [200, 108],
            [176, 126],
            [185, 158],
            [160, 138],
            [135, 158],
            [144, 126],
            [120, 108],
            [150, 108],
        ],
        np.int32,
    )
    cv2.fillPoly(image, [points], (255, 255, 255))
    detector = SteelBallDetector(detector_config())
    result = detector.process(packet(image))
    assert not result.found
    assert detector.get_debug_data().rejected_by_circularity >= 1


def test_multiple_targets_prefer_candidate_near_previous_center() -> None:
    detector = SteelBallDetector(detector_config())
    detector.process(packet(circle_image((60, 120), 16), 1))
    image = blank()
    cv2.circle(image, (65, 120), 16, (255, 255, 255), -1)
    cv2.circle(image, (240, 120), 20, (255, 255, 255), -1)
    result = detector.process(packet(image, 2))
    assert result.found
    assert result.center_x < 100


def test_continuous_frames_enter_locked() -> None:
    detector = SteelBallDetector(detector_config(confirm_frames=2))
    first = detector.process(packet(circle_image(), 1))
    second = detector.process(packet(circle_image(), 2))
    assert first.target_state == TargetState.CANDIDATE
    assert second.target_state == TargetState.LOCKED


def test_missing_frames_enter_lost() -> None:
    detector = SteelBallDetector(detector_config(lost_frames=2))
    detector.process(packet(circle_image(), 1))
    occluded = detector.process(packet(blank(), 2))
    lost = detector.process(packet(blank(), 3))
    assert occluded.target_state == TargetState.OCCLUDED
    assert lost.target_state == TargetState.LOST


def test_far_target_can_be_reacquired_after_lost() -> None:
    detector = SteelBallDetector(detector_config(lost_frames=2, max_jump_px=30))
    detector.process(packet(circle_image((50, 120), 18), 1))
    detector.process(packet(blank(), 2))
    detector.process(packet(blank(), 3))
    reacquired = detector.process(packet(circle_image((270, 120), 18), 4))
    assert reacquired.found
    assert reacquired.center_x > 250
    assert reacquired.target_state == TargetState.CANDIDATE


def test_input_image_is_not_modified() -> None:
    image = circle_image()
    before = image.copy()
    detector = SteelBallDetector(detector_config())
    result = detector.process(packet(image))
    rendered = detector.draw_debug(image, result)
    assert np.array_equal(image, before)
    assert not np.array_equal(rendered, image)


def test_known_fx_and_diameters_produce_correct_distance() -> None:
    assert estimate_distance_mm(800.0, 10.0, 40.0) == 200
    calibration = CalibrationConfig(
        calibrated=True,
        image_width=320,
        image_height=240,
        camera_matrix=[[800.0, 0.0, 160.0], [0.0, 800.0, 120.0], [0.0, 0.0, 1.0]],
        distortion_coefficients=[0.0] * 5,
    )
    result = SteelBallDetector(detector_config(), calibration).process(packet(circle_image()))
    assert result.found
    assert 190 <= result.distance_mm <= 210


def test_invalid_distance_inputs_return_unknown() -> None:
    assert estimate_distance_mm(float("nan"), 10.0, 40.0) == 0xFFFF
    assert estimate_distance_mm(800.0, 0.0, 40.0) == 0xFFFF
    assert estimate_distance_mm(800.0, 10.0, float("inf")) == 0xFFFF
    assert estimate_distance_mm("invalid", 10.0, 40.0) == 0xFFFF


def test_roi_excludes_targets_outside_region() -> None:
    config = detector_config(roi=[0, 0, 120, 240])
    detector = SteelBallDetector(config)
    assert not detector.process(packet(circle_image((250, 120), 20))).found
    assert detector.process(packet(circle_image((60, 120), 20), 2)).found


def test_app_and_replay_accept_steel_ball_detector() -> None:
    app_args = build_app_parser().parse_args(["--detector", "steel_ball"])
    replay_args = build_replay_parser().parse_args(
        ["--input", "sample.mp4", "--detector", "steel_ball"]
    )
    detector = create_detector(
        "steel_ball",
        "red",
        load_color_config(),
        load_mission_config(),
    )
    assert app_args.detector == "steel_ball"
    assert replay_args.detector == "steel_ball"
    assert isinstance(detector, SteelBallDetector)


def test_debug_reports_rejection_reasons() -> None:
    image = blank()
    cv2.circle(image, (50, 60), 4, (255, 255, 255), -1)
    cv2.ellipse(image, (160, 120), (30, 12), 0, 0, 360, (255, 255, 255), -1)
    detector = SteelBallDetector(
        detector_config(min_area_px=1, min_diameter_px=15, min_circularity=0.1)
    )
    detector.process(packet(image))
    debug = detector.get_debug_data()
    assert debug is not None
    assert debug.rejected_by_diameter >= 1
    assert debug.rejected_by_aspect_ratio >= 1


def test_nonfinite_perimeter_is_safely_rejected(monkeypatch) -> None:
    detector = SteelBallDetector(detector_config())
    monkeypatch.setattr(cv2, "arcLength", lambda *args: float("nan"))
    result = detector.process(packet(circle_image()))
    debug = detector.get_debug_data()
    assert not result.found
    assert debug is not None
    assert debug.rejected_by_circularity >= 1


def test_hough_disabled_does_not_change_confidence() -> None:
    detector = SteelBallDetector(detector_config(hough_enabled=False))
    result = detector.process(packet(circle_image()))
    candidate = detector._last_candidate
    assert candidate is not None
    baseline = detector._confidence(candidate)
    altered = replace(candidate, hough_verified=not candidate.hough_verified)
    assert detector._confidence(altered) == baseline


def test_hough_rejection_is_reported_when_enabled(monkeypatch) -> None:
    detector = SteelBallDetector(detector_config(hough_enabled=True))
    monkeypatch.setattr(detector, "_hough_circles", lambda enhanced: [])
    result = detector.process(packet(circle_image()))
    debug = detector.get_debug_data()
    assert not result.found
    assert debug is not None
    assert debug.rejected_by_hough >= 1
