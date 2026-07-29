from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.ball_position_mapping import pixel_x_to_mm
from core.config_loader import ConfigError, load_steel_ball_ncnn_config
from core.models import BallPositionMappingConfig
from protocol.ball_position_link import (
    BallPositionParser,
    decode_ball_position,
    encode_ball_position,
)


@pytest.mark.parametrize(
    ("x_mm", "hex_value"),
    [(-125, "A5 5A 83 86"), (0, "A5 5A 00 06"), (125, "A5 5A 7D 72")],
)
def test_required_wire_vectors(x_mm: int, hex_value: str) -> None:
    expected = bytes.fromhex(hex_value)
    assert encode_ball_position(x_mm) == expected
    assert decode_ball_position(expected).x_mm == x_mm


@pytest.mark.parametrize("x_mm", [-126, 126])
def test_out_of_range_position_is_rejected(x_mm: int) -> None:
    with pytest.raises(ValueError, match="-125..125"):
        encode_ball_position(x_mm)


def test_crc_error_is_rejected_and_parser_recovers() -> None:
    damaged = bytearray(encode_ball_position(-42))
    damaged[3] ^= 0x80
    with pytest.raises(ValueError, match="CRC"):
        decode_ball_position(damaged)
    parser = BallPositionParser()
    assert parser.feed(damaged + encode_ball_position(42))[0].x_mm == 42
    assert parser.crc_error_count == 1


def test_parser_handles_partial_and_concatenated_packets() -> None:
    parser = BallPositionParser()
    first = encode_ball_position(-1)
    assert parser.feed(first[:2]) == []
    packets = parser.feed(first[2:] + encode_ball_position(1))
    assert [packet.x_mm for packet in packets] == [-1, 1]


def test_parser_recovers_from_noise_and_ignores_aa55_frames() -> None:
    parser = BallPositionParser()
    generic_vmc_prefix = bytes.fromhex("AA 55 01 04 00 00 00 00")
    packets = parser.feed(b"noise" + generic_vmc_prefix + encode_ball_position(-100))
    assert [packet.x_mm for packet in packets] == [-100]


def test_signed_int8_round_trip() -> None:
    assert decode_ball_position(encode_ball_position(-37)).x_mm == -37
    assert decode_ball_position(encode_ball_position(37)).x_mm == 37


def mapping(*, calibrated: bool = True, reverse: bool = False) -> BallPositionMappingConfig:
    return BallPositionMappingConfig(
        calibrated=calibrated,
        x_minus_125_px=100 if not reverse else 500,
        x_plus_125_px=500 if not reverse else 100,
    )


def test_normal_and_reverse_two_point_mapping() -> None:
    assert [pixel_x_to_mm(x, mapping()) for x in (100, 300, 500)] == [-125, 0, 125]
    assert [pixel_x_to_mm(x, mapping(reverse=True)) for x in (500, 300, 100)] == [-125, 0, 125]


def test_mapping_clamps_and_uncalibrated_returns_none() -> None:
    assert pixel_x_to_mm(-1000, mapping()) == -125
    assert pixel_x_to_mm(1000, mapping()) == 125
    assert pixel_x_to_mm(300, mapping(calibrated=False)) is None


def _write_ncnn_config(path: Path, position_mapping: dict[str, object]) -> None:
    source = yaml.safe_load(
        (Path(__file__).parents[1] / "config/steel_ball_ncnn.yaml").read_text(
            encoding="utf-8"
        )
    )
    source["position_mapping"] = position_mapping
    path.write_text(yaml.safe_dump(source), encoding="utf-8")


def test_mapping_configuration_validation_and_reverse_support(tmp_path: Path) -> None:
    path = tmp_path / "steel_ball_ncnn.yaml"
    _write_ncnn_config(
        path,
        {"calibrated": True, "x_minus_125_px": 500, "x_plus_125_px": 100},
    )
    config = load_steel_ball_ncnn_config(path)
    assert config.position_mapping.calibrated
    assert config.position_mapping.x_minus_125_px == 500


@pytest.mark.parametrize(
    "bad_mapping",
    [
        {"calibrated": 1, "x_minus_125_px": 0, "x_plus_125_px": 639},
        {"calibrated": True, "x_minus_125_px": True, "x_plus_125_px": 639},
        {"calibrated": True, "x_minus_125_px": 10, "x_plus_125_px": 10},
    ],
)
def test_invalid_mapping_configuration_is_explicit(tmp_path: Path, bad_mapping) -> None:
    path = tmp_path / "steel_ball_ncnn.yaml"
    _write_ncnn_config(path, bad_mapping)
    with pytest.raises(ConfigError, match="position_mapping"):
        load_steel_ball_ncnn_config(path)


def test_web_is_steel_ball_only_and_formats_positions() -> None:
    root = Path(__file__).parents[1] / "touch_ui_web"
    html = (root / "index.html").read_text(encoding="utf-8")
    javascript = (root / "app.js").read_text(encoding="utf-8")
    assert "data-detector" not in html
    assert "competitionDock" not in html
    assert "DETECTOR_LABELS" not in javascript and "detectorLabel" not in javascript
    assert '"ballPosition"' in javascript
    assert '"-- mm"' in javascript
    assert '${Number(xMillimetres) > 0 ? "+" : ""}' in javascript
    assert "启用位置下发" in html and "停止位置下发" in javascript


def test_protocol_module_has_no_heavy_ml_imports() -> None:
    source = (
        Path(__file__).parents[1] / "protocol/ball_position_link.py"
    ).read_text(encoding="utf-8")
    assert "torch" not in source and "ultralytics" not in source
