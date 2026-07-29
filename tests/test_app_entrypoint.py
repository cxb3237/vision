from pathlib import Path

import pytest

from app import (
    build_argument_parser,
    create_detector,
    resolve_ball_uart_settings,
    resolve_touch_ui_config,
)
from core.config_loader import ConfigError, load_mission_config
from detectors.steel_ball_yolo_ncnn_detector import SteelBallYoloNcnnDetector


def test_parser_exposes_only_current_detector_configuration() -> None:
    help_text = build_argument_parser().format_help()
    for removed in ("--detector", "--colors-config", "--shapes-config", "--digit-config", "--steel-ball-config", "--target"):
        assert removed not in help_text
    assert "--steel-ball-ncnn-config" in help_text


def test_fixed_detector_factory_creates_ncnn_detector() -> None:
    assert isinstance(create_detector(), SteelBallYoloNcnnDetector)


@pytest.mark.parametrize(("field", "value"), [("baudrate", 0), ("serial_rate", -1)])
def test_invalid_uart_cli_numbers_are_rejected(field: str, value: int) -> None:
    args = build_argument_parser().parse_args([])
    setattr(args, field, value)
    with pytest.raises(ConfigError):
        resolve_ball_uart_settings(args, load_mission_config())


def test_model_weight_files_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    model = root / "models/steel_ball/best_ncnn_model"
    assert (model / "model.ncnn.param").is_file()
    assert (model / "model.ncnn.bin").is_file()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5"])
def test_touch_host_cli_rejects_non_loopback_addresses(tmp_path, host) -> None:
    source = Path("config/touch_ui.yaml")
    target = tmp_path / "touch.yaml"
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    args = build_argument_parser().parse_args(
        ["--touch-config", str(target), "--touch-host", host]
    )
    with pytest.raises(ConfigError, match="回环"):
        resolve_touch_ui_config(args, project_root=tmp_path)
