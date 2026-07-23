"""DigitDetector 的合成模板、候选过滤、投票和集成测试。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import time

import cv2
import numpy as np
import pytest
import yaml

from app import create_detector as create_app_detector
from core.config_loader import ConfigError, load_color_config, load_digit_config, load_mission_config
from core.models import DigitConfig, FramePacket, TargetState
from detectors.digit_detector import DigitCandidate, DigitDetector, normalize_binary_digit
from tools.capture_digit_templates import build_capture_digit_config, next_template_index
from tools.replay_test import build_argument_parser as build_replay_parser
from tools.replay_test import create_detector as create_replay_detector


def _draw_glyph(digit: int, foreground: int = 255, background: int = 0) -> np.ndarray:
    image = np.full((160, 120), background, np.uint8)
    text = str(digit)
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 3.5
    thickness = 7
    (width, height), _ = cv2.getTextSize(text, font, scale, thickness)
    origin = ((image.shape[1] - width) // 2, (image.shape[0] + height) // 2)
    cv2.putText(image, text, origin, font, scale, foreground, thickness, cv2.LINE_AA)
    return image


def _scene(digit: int) -> np.ndarray:
    image = np.full((240, 320, 3), 255, np.uint8)
    glyph = _draw_glyph(digit, foreground=0, background=255)
    top = (image.shape[0] - glyph.shape[0]) // 2
    left = (image.shape[1] - glyph.shape[1]) // 2
    image[top : top + glyph.shape[0], left : left + glyph.shape[1]] = cv2.cvtColor(
        glyph, cv2.COLOR_GRAY2BGR
    )
    return image


def _packet(image: np.ndarray, frame_id: int = 1) -> FramePacket:
    return FramePacket(frame_id, time.monotonic(), image)


def _config(template_root: Path, **overrides) -> DigitConfig:
    config = DigitConfig(
        roi={"enabled": False, "x": 0, "y": 0, "width": 320, "height": 240},
        preprocess={
            "use_clahe": False,
            "clahe_clip_limit": 2.0,
            "threshold_mode": "otsu",
            "fixed_threshold": 120,
            "adaptive_block_size": 31,
            "adaptive_c": 5.0,
            "invert": True,
            "gaussian_kernel": 0,
            "morph_open": 0,
            "morph_close": 0,
        },
        candidate={
            "min_area_px": 50.0,
            "max_area_px": 100_000.0,
            "min_aspect_ratio": 0.1,
            "max_aspect_ratio": 1.3,
            "min_height_px": 20,
            "border_margin_px": 2,
        },
        normalization={
            "width": 64,
            "height": 96,
            "padding_px": 6,
            "center_by_moments": True,
        },
        matching={
            "template_root": str(template_root),
            "min_score": 0.45,
            "min_score_margin": 0.01,
            "iou_weight": 0.6,
            "correlation_weight": 0.4,
        },
        tracking={"confirm_frames": 3, "lost_frames": 2, "vote_window": 5},
    )
    for group, values in overrides.items():
        getattr(config, group).update(values)
    return config


@pytest.fixture
def template_root(tmp_path) -> Path:
    root = tmp_path / "templates"
    normalization = {"width": 64, "height": 96, "padding_px": 6, "center_by_moments": True}
    for digit in range(10):
        directory = root / str(digit)
        directory.mkdir(parents=True)
        normalized = normalize_binary_digit(_draw_glyph(digit), normalization)
        assert cv2.imwrite(str(directory / f"{digit}_0001.png"), normalized)
    return root


def _write_config(path: Path, config: DigitConfig) -> None:
    path.write_text(yaml.safe_dump(asdict(config), sort_keys=False), encoding="utf-8")


def test_template_directories_load_all_digits(template_root) -> None:
    detector = DigitDetector(_config(template_root))
    assert {digit: len(items) for digit, items in detector.templates.items()} == {
        digit: 1 for digit in range(10)
    }


@pytest.mark.parametrize("digit", range(10))
def test_basic_synthetic_digits_are_recognized(template_root, digit: int) -> None:
    result = DigitDetector(_config(template_root)).process(_packet(_scene(digit)))
    assert result.found
    assert result.target_class == 100 + digit


def test_blank_image_returns_not_found(template_root) -> None:
    result = DigitDetector(_config(template_root)).process(
        _packet(np.full((240, 320, 3), 255, np.uint8))
    )
    assert not result.found
    assert result.target_class == 0


def test_area_too_small_is_filtered(template_root) -> None:
    detector = DigitDetector(_config(template_root, candidate={"min_area_px": 50_000.0}))
    assert not detector.process(_packet(_scene(3))).found


def test_height_too_small_is_filtered(template_root) -> None:
    detector = DigitDetector(_config(template_root, candidate={"min_height_px": 200}))
    assert not detector.process(_packet(_scene(3))).found


def test_invalid_aspect_ratio_is_filtered(template_root) -> None:
    detector = DigitDetector(
        _config(
            template_root,
            candidate={"min_aspect_ratio": 1.25, "max_aspect_ratio": 1.3},
        )
    )
    assert not detector.process(_packet(_scene(1))).found


def test_score_below_threshold_returns_unknown(template_root, monkeypatch) -> None:
    detector = DigitDetector(_config(template_root, matching={"min_score": 0.8}))
    monkeypatch.setattr(detector, "score_digit", lambda image: {digit: 0.2 for digit in range(10)})
    result = detector.process(_packet(_scene(4)))
    assert not result.found
    assert result.target_class == 0


def test_small_score_margin_returns_unknown(template_root, monkeypatch) -> None:
    detector = DigitDetector(_config(template_root, matching={"min_score_margin": 0.1}))
    scores = {digit: 0.1 for digit in range(10)}
    scores.update({3: 0.8, 8: 0.75})
    monkeypatch.setattr(detector, "score_digit", lambda image: scores)
    assert not detector.process(_packet(_scene(3))).found


def test_continuous_frames_enter_locked(template_root) -> None:
    detector = DigitDetector(_config(template_root, tracking={"confirm_frames": 3}))
    results = [detector.process(_packet(_scene(2), frame_id)) for frame_id in range(1, 4)]
    assert results[-1].target_state == TargetState.LOCKED


def test_single_frame_error_never_replaces_locked_digit(template_root, monkeypatch) -> None:
    detector = DigitDetector(
        _config(template_root, tracking={"confirm_frames": 3, "vote_window": 5})
    )
    sequence = iter([3, 3, 3, 8, 3])

    def fake_classify(image):
        digit = next(sequence)
        scores = {value: 0.0 for value in range(10)}
        scores[digit] = 0.9
        return digit, 0.9, 0.1, 0.8, scores

    monkeypatch.setattr(detector, "classify_digit", fake_classify)
    results = [detector.process(_packet(_scene(3), frame_id)) for frame_id in range(1, 6)]
    assert results[2].target_state == TargetState.LOCKED
    assert results[3].target_state == TargetState.CANDIDATE
    assert results[3].target_class == 108
    assert results[-1].target_class == 103
    assert results[-1].target_state == TargetState.LOCKED


def test_missing_frames_enter_lost(template_root) -> None:
    detector = DigitDetector(
        _config(template_root, tracking={"confirm_frames": 1, "lost_frames": 2})
    )
    detector.process(_packet(_scene(5), 1))
    blank = np.full((240, 320, 3), 255, np.uint8)
    occluded = detector.process(_packet(blank, 2))
    lost = detector.process(_packet(blank, 3))
    assert occluded.target_state == TargetState.OCCLUDED
    assert lost.target_state == TargetState.LOST


def test_digit_can_be_reacquired_after_lost(template_root) -> None:
    detector = DigitDetector(
        _config(template_root, tracking={"confirm_frames": 1, "lost_frames": 2})
    )
    detector.process(_packet(_scene(5), 1))
    blank = np.full((240, 320, 3), 255, np.uint8)
    detector.process(_packet(blank, 2))
    detector.process(_packet(blank, 3))
    reacquired = detector.process(_packet(_scene(7), 4))
    assert reacquired.found
    assert reacquired.target_class == 107


def test_input_image_is_not_modified(template_root) -> None:
    image = _scene(6)
    before = image.copy()
    detector = DigitDetector(_config(template_root))
    result = detector.process(_packet(image))
    detector.draw_debug(image, result)
    assert np.array_equal(image, before)


def test_missing_template_root_has_clear_error(tmp_path) -> None:
    config = _config(tmp_path / "missing")
    path = tmp_path / "digit.yaml"
    _write_config(path, config)
    with pytest.raises(ConfigError, match="数字模板根目录不存在"):
        load_digit_config(path)


def test_app_can_create_digit_detector(template_root, tmp_path) -> None:
    path = tmp_path / "digit.yaml"
    _write_config(path, _config(template_root))
    detector = create_app_detector(
        "digit",
        "red",
        load_color_config(),
        load_mission_config(),
        digit_config=path,
    )
    assert isinstance(detector, DigitDetector)


def test_replay_can_create_digit_detector(template_root, tmp_path) -> None:
    path = tmp_path / "digit.yaml"
    _write_config(path, _config(template_root))
    args = build_replay_parser().parse_args(
        ["--input", "demo.mp4", "--detector", "digit", "--digit-config", str(path)]
    )
    assert isinstance(create_replay_detector(args), DigitDetector)


def test_template_numbering_does_not_overwrite_existing(tmp_path) -> None:
    directory = tmp_path / "3"
    directory.mkdir()
    (directory / "3_0001.png").write_bytes(b"x")
    (directory / "3_0004_raw.jpg").write_bytes(b"x")
    assert next_template_index(directory, 3) == 5


def test_locked_digit_switches_to_new_candidate_after_one_missing_frame(
    template_root, monkeypatch
) -> None:
    detector = DigitDetector(
        _config(
            template_root,
            tracking={"confirm_frames": 3, "lost_frames": 5, "vote_window": 5},
        )
    )
    sequence = iter([3, 3, 3, 8, 8, 8])

    def fake_classify(image):
        digit = next(sequence)
        scores = {value: 0.05 for value in range(10)}
        scores[digit] = 0.9
        return digit, 0.9, 0.05, 0.85, scores

    monkeypatch.setattr(detector, "classify_digit", fake_classify)
    for frame_id in range(1, 4):
        locked = detector.process(_packet(_scene(3), frame_id))
    assert locked.target_state == TargetState.LOCKED
    detector.process(_packet(np.full((240, 320, 3), 255, np.uint8), 4))
    first_eight = detector.process(_packet(_scene(8), 5))
    second_eight = detector.process(_packet(_scene(8), 6))
    third_eight = detector.process(_packet(_scene(8), 7))
    for result in (first_eight, second_eight):
        assert result.target_state == TargetState.CANDIDATE
        assert result.target_class == 108
        assert result.target_class != 103
    assert third_eight.target_state == TargetState.LOCKED
    assert third_eight.target_class == 108


def test_confidence_uses_score_for_output_target_class(template_root, monkeypatch) -> None:
    detector = DigitDetector(_config(template_root))
    scores = {digit: 0.1 for digit in range(10)}
    scores[8] = 0.62
    monkeypatch.setattr(
        detector,
        "classify_digit",
        lambda image: (8, 0.99, 0.2, 0.79, scores),
    )
    result = detector.process(_packet(_scene(8)))
    assert result.target_class == 108
    assert result.confidence == 620


def test_direct_digit_switch_never_combines_old_class_with_new_bbox(
    template_root, monkeypatch
) -> None:
    detector = DigitDetector(_config(template_root, tracking={"confirm_frames": 2}))
    sequence = iter([3, 3, 8])

    def fake_classify(image):
        digit = next(sequence)
        scores = {value: 0.0 for value in range(10)}
        scores[digit] = 0.9
        return digit, 0.9, 0.1, 0.8, scores

    monkeypatch.setattr(detector, "classify_digit", fake_classify)
    detector.process(_packet(_scene(3), 1))
    old_result = detector.process(_packet(_scene(3), 2))
    shifted_eight = cv2.warpAffine(
        _scene(8),
        np.float32([[1, 0, 45], [0, 1, 0]]),
        (320, 240),
        borderValue=(255, 255, 255),
    )
    switched = detector.process(_packet(shifted_eight, 3))
    assert old_result.target_class == 103
    assert switched.target_class == 108
    assert switched.target_state == TargetState.CANDIDATE
    assert switched.center_x != old_result.center_x


def test_lost_clears_all_digit_state_before_other_digit_reacquisition(
    template_root, monkeypatch
) -> None:
    detector = DigitDetector(
        _config(template_root, tracking={"confirm_frames": 2, "lost_frames": 2})
    )
    sequence = iter([3, 3, 8, 8])

    def fake_classify(image):
        digit = next(sequence)
        scores = {value: 0.0 for value in range(10)}
        scores[digit] = 0.9
        return digit, 0.9, 0.1, 0.8, scores

    monkeypatch.setattr(detector, "classify_digit", fake_classify)
    detector.process(_packet(_scene(3), 1))
    detector.process(_packet(_scene(3), 2))
    blank = np.full((240, 320, 3), 255, np.uint8)
    detector.process(_packet(blank, 3))
    lost = detector.process(_packet(blank, 4))
    debug = detector.get_debug_data()
    assert lost.target_state == TargetState.LOST
    assert debug is not None
    assert debug.candidate_digit is None
    assert debug.locked_digit is None
    assert debug.recent_votes == ()
    first = detector.process(_packet(_scene(8), 5))
    second = detector.process(_packet(_scene(8), 6))
    assert first.target_state == TargetState.CANDIDATE
    assert first.target_class == 108
    assert second.target_state == TargetState.LOCKED
    assert second.target_class == 108


def test_missing_frame_occupies_a_vote_window_slot(template_root) -> None:
    detector = DigitDetector(
        _config(template_root, tracking={"confirm_frames": 1, "lost_frames": 3, "vote_window": 3})
    )
    detector.process(_packet(_scene(2), 1))
    detector.process(_packet(np.full((240, 320, 3), 255, np.uint8), 2))
    debug = detector.get_debug_data()
    assert debug is not None
    assert debug.recent_votes[-1] is None


def test_batch_scores_match_scalar_reference(template_root) -> None:
    detector = DigitDetector(_config(template_root))
    candidate = normalize_binary_digit(_draw_glyph(6), detector.config.normalization)
    batch = detector.score_digit(candidate)
    reference = {digit: 0.0 for digit in range(10)}
    iou_weight = detector.config.matching["iou_weight"]
    correlation_weight = detector.config.matching["correlation_weight"]
    for digit, templates in detector.templates.items():
        for template in templates:
            candidate_fg = candidate > 0
            template_fg = template > 0
            intersection = np.count_nonzero(candidate_fg & template_fg)
            union = np.count_nonzero(candidate_fg | template_fg)
            iou = intersection / union if union else 0.0
            correlation = float(
                cv2.matchTemplate(
                    candidate.astype(np.float32) / 255.0,
                    template.astype(np.float32) / 255.0,
                    cv2.TM_CCOEFF_NORMED,
                )[0, 0]
            )
            correlation = max(0.0, min(1.0, correlation)) if np.isfinite(correlation) else 0.0
            score = (
                iou_weight * iou + correlation_weight * correlation
            ) / (iou_weight + correlation_weight)
            reference[digit] = max(reference[digit], score)
    assert batch == pytest.approx(reference, abs=1e-5)


def test_one_hundred_templates_are_scored_in_one_batch(template_root) -> None:
    normalization = {"width": 64, "height": 96, "padding_px": 6, "center_by_moments": True}
    for digit in range(10):
        directory = template_root / str(digit)
        for index in range(2, 11):
            shifted = np.roll(_draw_glyph(digit), index % 3 - 1, axis=1)
            template = normalize_binary_digit(shifted, normalization)
            assert cv2.imwrite(str(directory / f"{digit}_{index:04d}.png"), template)
    detector = DigitDetector(_config(template_root))
    assert detector._template_labels.size == 100
    scores = detector.score_digit(normalize_binary_digit(_draw_glyph(4), normalization))
    assert set(scores) == set(range(10))
    assert all(np.isfinite(score) for score in scores.values())


def test_empty_foreground_has_no_nan_scores(template_root) -> None:
    detector = DigitDetector(_config(template_root))
    scores = detector.score_digit(np.zeros((96, 64), np.uint8))
    assert set(scores) == set(range(10))
    assert all(np.isfinite(score) and score == 0.0 for score in scores.values())


def test_incomplete_template_counts_still_return_all_digits_when_allowed(tmp_path) -> None:
    root = tmp_path / "templates"
    for digit in range(10):
        (root / str(digit)).mkdir(parents=True)
    template = normalize_binary_digit(
        _draw_glyph(2),
        {"width": 64, "height": 96, "padding_px": 6, "center_by_moments": True},
    )
    cv2.imwrite(str(root / "2/2_0001.png"), template)
    detector = DigitDetector(_config(root), require_complete_templates=False)
    scores = detector.score_digit(template)
    assert set(scores) == set(range(10))
    assert scores[2] > 0
    assert all(scores[digit] == 0.0 for digit in range(10) if digit != 2)


def test_formal_mode_lists_missing_digit_templates(tmp_path) -> None:
    root = tmp_path / "templates"
    normalization = {"width": 64, "height": 96, "padding_px": 6, "center_by_moments": True}
    for digit in range(10):
        directory = root / str(digit)
        directory.mkdir(parents=True)
        if digit not in {0, 4, 7}:
            cv2.imwrite(
                str(directory / f"{digit}_0001.png"),
                normalize_binary_digit(_draw_glyph(digit), normalization),
            )
    with pytest.raises(ValueError, match=r"缺少数字模板: 0, 4, 7"):
        DigitDetector(_config(root), require_complete_templates=True)


def test_unreadable_template_logs_its_path(template_root, caplog) -> None:
    broken = template_root / "0/broken.png"
    broken.write_bytes(b"not an image")
    DigitDetector(_config(template_root))
    assert str(broken) in caplog.text


def test_capture_config_disables_formal_roi_without_mutating_source(template_root) -> None:
    source = _config(
        template_root,
        roi={"enabled": True, "x": 100, "y": 80, "width": 120, "height": 100},
    )
    capture = build_capture_digit_config(source, template_root)
    assert not capture.roi["enabled"]
    assert capture.roi["x"] == 0
    assert capture.roi["y"] == 0
    assert source.roi["enabled"]
    assert source.roi["x"] == 100


def test_center_candidate_beats_slightly_larger_edge_interference() -> None:
    contour = np.zeros((1, 1, 2), np.int32)
    center = DigitCandidate(contour, (140, 80, 40, 80), 900.0, 0.5, (160.0, 120.0))
    edge = DigitCandidate(contour, (260, 20, 45, 85), 1000.0, 0.53, (282.5, 62.5))
    selected = DigitDetector._select_candidate([edge, center], (0, 0, 320, 240))
    assert selected is center
