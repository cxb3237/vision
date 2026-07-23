"""YAML 配置加载和校验测试。"""

import pytest

from core.config_loader import (
    ConfigError,
    load_camera_config,
    load_color_config,
    load_mission_config,
    load_calibration_config,
    load_shape_config,
)


def test_configs() -> None:
    assert "red" in load_color_config()
    assert load_camera_config().width == 640


def test_missing(tmp_path) -> None:
    with pytest.raises(ConfigError):
        load_camera_config(tmp_path / "no.yaml")


def test_bad(tmp_path) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("device: 0", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_camera_config(path)


def test_optional_camera_properties_are_none() -> None:
    config = load_camera_config()
    assert config.gain is None
    assert config.exposure is None


def test_mission_has_valid_tracker_and_timing_fields() -> None:
    mission = load_mission_config()
    assert mission["max_jump_px"] > 0
    assert 0 < mission["smoothing_alpha"] <= 1


def test_invalid_calibrated_matrix_is_rejected(tmp_path) -> None:
    path = tmp_path / "calibration.yaml"
    path.write_text(
        """calibrated: true
image_width: 640
image_height: 480
camera_matrix: [[1, 0], [0, 1]]
distortion_coefficients: [0, 0, 0, 0, 0]
reprojection_error: 0.2
rms_error: 0.3
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_calibration_config(path)


def test_shape_config_is_loaded_from_yaml() -> None:
    config = load_shape_config()
    assert config.canny_low < config.canny_high
    assert config.min_area <= config.max_area
