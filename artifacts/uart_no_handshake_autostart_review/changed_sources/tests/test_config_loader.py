from pathlib import Path

import pytest
import yaml

from core.config_loader import ConfigError, load_camera_config, load_mission_config, load_steel_ball_ncnn_config


def test_production_configs_load() -> None:
    assert load_camera_config().width == 640
    mission = load_mission_config()
    assert mission["default_mode"] == "track"
    assert mission["ball_uart"]["baudrate"] == 9600
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
