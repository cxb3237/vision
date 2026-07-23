"""Pure mapping and atomic persistence tests for the steel-ball tuner."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from core.config_loader import load_steel_ball_config
from core.models import SteelBallConfig
from tools import steel_ball_tuner
from tools.steel_ball_tuner import normalize_tuner_values, save_steel_ball_config_atomic


def test_area_range_is_normalized() -> None:
    config = normalize_tuner_values(
        SteelBallConfig(), {"min_area": 500, "max_area": 100}, 640, 480
    )
    assert config.min_area_px == 500
    assert config.max_area_px == 500


def test_aspect_range_is_normalized() -> None:
    config = normalize_tuner_values(
        SteelBallConfig(),
        {"min_aspect_x100": 150, "max_aspect_x100": 50},
        640,
        480,
    )
    assert config.min_aspect_ratio == 1.5
    assert config.max_aspect_ratio == 1.5


def test_roi_is_clipped_to_image_boundaries() -> None:
    config = normalize_tuner_values(
        SteelBallConfig(),
        {"roi_enable": 1, "roi_x": 630, "roi_y": 470, "roi_w": 100, "roi_h": 100},
        640,
        480,
    )
    assert config.roi == [630, 470, 10, 10]


def test_disabled_roi_is_none() -> None:
    config = normalize_tuner_values(
        SteelBallConfig(roi=[1, 2, 3, 4]), {"roi_enable": 0}, 640, 480
    )
    assert config.roi is None


def test_kernel_and_adaptive_block_values_are_odd() -> None:
    config = normalize_tuner_values(
        SteelBallConfig(),
        {"gaussian": 4, "adaptive_block": 2, "morph_open": 6, "morph_close": 0},
        640,
        480,
    )
    assert config.gaussian_kernel == 5
    assert config.adaptive_block_size == 3
    assert config.morph_open == 7
    assert config.morph_close == 0


def test_confirmation_and_loss_counts_are_at_least_one() -> None:
    config = normalize_tuner_values(
        SteelBallConfig(), {"confirm_frames": 0, "lost_frames": 0}, 640, 480
    )
    assert config.confirm_frames == 1
    assert config.lost_frames == 1


def test_all_fields_round_trip_through_yaml(tmp_path) -> None:
    config = normalize_tuner_values(
        SteelBallConfig(known_diameter_mm=10.0, target_class=123),
        {
            "clahe_enable": 1,
            "clahe_clip_x10": 37,
            "clahe_tile": 5,
            "threshold_mode": 1,
            "threshold": 143,
            "invert": 1,
            "adaptive_block": 31,
            "adaptive_c+50": 42,
            "gaussian": 7,
            "morph_open": 5,
            "morph_close": 9,
            "min_diameter": 20,
            "max_diameter": 80,
            "min_area": 200,
            "max_area": 4000,
            "min_circularity": 81,
            "min_aspect_x100": 85,
            "max_aspect_x100": 120,
            "max_jump": 90,
            "confirm_frames": 4,
            "lost_frames": 6,
            "roi_enable": 1,
            "roi_x": 10,
            "roi_y": 20,
            "roi_w": 300,
            "roi_h": 200,
            "hough_enable": 1,
            "hough_dp_x10": 15,
            "hough_min_dist": 25,
            "hough_param1": 110,
            "hough_param2": 24,
            "hough_min_radius": 8,
            "hough_max_radius": 50,
        },
        640,
        480,
    )
    output = tmp_path / "steel_ball.yaml"
    save_steel_ball_config_atomic(config, output)
    assert asdict(load_steel_ball_config(output)) == asdict(config)


def test_atomic_save_failure_preserves_existing_file(tmp_path, monkeypatch) -> None:
    output = tmp_path / "steel_ball.yaml"
    output.write_text("original: true\n", encoding="utf-8")

    def fail_dump(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(steel_ball_tuner.yaml, "safe_dump", fail_dump)
    with pytest.raises(RuntimeError, match="simulated"):
        save_steel_ball_config_atomic(SteelBallConfig(), output)
    assert output.read_text(encoding="utf-8") == "original: true\n"
    assert not (tmp_path / ".steel_ball.yaml.tmp").exists()
