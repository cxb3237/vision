from pathlib import Path

import pytest
import yaml

from core.config_loader import ConfigError, load_camera_config, load_mission_config, load_steel_ball_ncnn_config


def test_production_configs_load() -> None:
    assert load_camera_config().width == 640
    mission = load_mission_config()
    assert mission["default_mode"] == "track"
    assert mission["ball_uart"]["baudrate"] == 9600
    assert mission["ball_uart"]["timeout_s"] == pytest.approx(0.005)
    assert mission["ball_uart"]["continuous_output"] is True
    assert mission["ball_uart"]["position_estimator"]["hold_horizon_ms"] == 300
    assert load_steel_ball_ncnn_config().backend == "ncnn"


def test_missing_config_is_clear(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_camera_config(tmp_path / "missing.yaml")


def test_unknown_mission_field_is_rejected(tmp_path: Path) -> None:
    data = yaml.safe_load(Path("config/mission.yaml").read_text(encoding="utf-8"))
    data["serial_queue_size"] = 64
    path = tmp_path / "mission.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="serial_queue_size"):
        load_mission_config(path)


@pytest.mark.parametrize(
    ("name", "value"),
    [("send_rate_hz", 0), ("wait_ready", True)],
)
def test_invalid_uart_calibration_or_rate_is_rejected(tmp_path: Path, name: str, value: object) -> None:
    data = yaml.safe_load(Path("config/mission.yaml").read_text(encoding="utf-8"))
    data["ball_uart"][name] = value
    path = tmp_path / "mission.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="ball_uart"):
        load_mission_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("baudrate", 9601), ("send_rate_hz", 55)],
)
def test_uart_contract_and_bandwidth_are_strict(tmp_path: Path, field: str, value: object) -> None:
    data = yaml.safe_load(Path("config/mission.yaml").read_text(encoding="utf-8"))
    data["ball_uart"][field] = value
    path = tmp_path / "mission.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="ball_uart"):
        load_mission_config(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alpha", 0),
        ("beta", 1.1),
        ("max_speed_mm_s", 0),
        ("max_output_slew_mm_s", -1),
        ("measurement_fresh_ms", 151),
    ],
)
def test_invalid_position_estimator_configuration_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    data = yaml.safe_load(Path("config/mission.yaml").read_text(encoding="utf-8"))
    data["ball_uart"]["position_estimator"][field] = value
    path = tmp_path / "mission.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="position_estimator"):
        load_mission_config(path)


def test_steel_ball_old_config_defaults_pipe_roi_disabled() -> None:
    config = load_steel_ball_ncnn_config("config/model_profiles/steel_ball_candidate.yaml")
    assert config.pipe_roi.enabled is False
    assert config.pipe_roi.corridor_half_width_px == 0.0
    assert config.pipe_roi.corridor_half_width_ratio == 0.0


def test_candidate_roi_strict_remains_candidate_confidence_point_five_with_roi() -> None:
    config = load_steel_ball_ncnn_config(
        "config/model_profiles/steel_ball_candidate_roi_strict.yaml"
    )
    assert "candidate_ncnn_model" in config.model_path
    assert config.conf_threshold == pytest.approx(0.50)
    assert config.pipe_roi.enabled is True


def test_steel_ball_pipe_roi_rejects_unknown_or_invalid_fields(tmp_path: Path) -> None:
    data = yaml.safe_load(Path("config/model_profiles/steel_ball_candidate.yaml").read_text(encoding="utf-8"))
    data["pipe_roi"] = {"enabled": True, "corridor_half_width_px": 0}
    path = tmp_path / "roi.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="corridor_half_width_px"):
        load_steel_ball_ncnn_config(path)
    data["pipe_roi"] = {"enabled": False, "unknown": 1}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown"):
        load_steel_ball_ncnn_config(path)


def test_steel_ball_pipe_roi_accepts_ratio_without_fixed_width(tmp_path: Path) -> None:
    data = yaml.safe_load(Path("config/model_profiles/steel_ball_candidate.yaml").read_text(encoding="utf-8"))
    data["pipe_roi"] = {
        "enabled": True,
        "corridor_half_width_ratio": 0.04,
        "corridor_half_width_px": 0,
    }
    path = tmp_path / "ratio.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = load_steel_ball_ncnn_config(path)
    assert config.pipe_roi.corridor_half_width_ratio == pytest.approx(0.04)
