"""复用 CameraService 的单个数字检测实时调参工具。"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
import time

import cv2
import numpy as np
import yaml

from core.config_loader import load_camera_config, load_digit_config, resolve_config_path
from core.models import DigitConfig
from detectors.digit_detector import DigitDetector
from drivers.camera_service import CameraService


CONTROL_WINDOW = "Digit Controls"
CONTROL_MAXIMUMS: dict[str, int] = {}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="实时调节单个印刷数字识别参数")
    parser.add_argument("--device", help="覆盖 camera.yaml 中的设备编号或路径")
    parser.add_argument("--camera-config", default="config/camera.yaml")
    parser.add_argument("--digit-config", default="config/digit.yaml")
    return parser


def _parse_device(value: str | None) -> str | int | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def _noop(_: int) -> None:
    return None


def _odd_or_zero(value: int, minimum: int = 1) -> int:
    value = max(0, int(value))
    if value == 0:
        return 0
    value = max(minimum, value)
    return value if value % 2 else value + 1


def normalize_tuner_values(
    base: DigitConfig,
    values: Mapping[str, int],
) -> DigitConfig:
    """将滑动条整数转换为完整且合法的 DigitConfig。"""

    data = asdict(base)

    def value(name: str, fallback: int) -> int:
        return int(values.get(name, fallback))

    mode_index = min(2, max(0, value(
        "threshold_mode", {"fixed": 0, "otsu": 1, "adaptive": 2}[base.preprocess["threshold_mode"]
        ],
    )))
    preprocess = data["preprocess"]
    preprocess.update(
        {
            "threshold_mode": ("fixed", "otsu", "adaptive")[mode_index],
            "fixed_threshold": min(255, max(0, value("fixed_threshold", preprocess["fixed_threshold"]))),
            "adaptive_block_size": _odd_or_zero(
                max(3, value("adaptive_block_size", preprocess["adaptive_block_size"])),
                3,
            ),
            "adaptive_c": float(value("adaptive_c+50", round(preprocess["adaptive_c"] + 50)) - 50),
            "use_clahe": bool(value("clahe_enable", int(preprocess["use_clahe"]))),
            "clahe_clip_limit": max(
                0.1,
                value("clahe_clip_x10", round(preprocess["clahe_clip_limit"] * 10)) / 10.0,
            ),
            "gaussian_kernel": _odd_or_zero(value("gaussian_kernel", preprocess["gaussian_kernel"])),
            "morph_open": _odd_or_zero(value("morph_open", preprocess["morph_open"])),
            "morph_close": _odd_or_zero(value("morph_close", preprocess["morph_close"])),
        }
    )
    candidate = data["candidate"]
    minimum_area = max(1, value("min_area", round(candidate["min_area_px"])))
    candidate.update(
        {
            "min_area_px": float(minimum_area),
            "min_height_px": max(1, value("min_height", candidate["min_height_px"])),
            "min_aspect_ratio": max(
                0.01,
                value("min_aspect_x100", round(candidate["min_aspect_ratio"] * 100)) / 100.0,
            ),
        }
    )
    candidate["max_area_px"] = max(float(minimum_area), float(candidate["max_area_px"]))
    candidate["max_aspect_ratio"] = max(
        candidate["min_aspect_ratio"],
        value("max_aspect_x100", round(candidate["max_aspect_ratio"] * 100)) / 100.0,
    )
    matching = data["matching"]
    matching["min_score"] = min(
        1.0,
        max(0.0, value("min_score_x100", round(matching["min_score"] * 100)) / 100.0),
    )
    matching["min_score_margin"] = min(
        1.0,
        max(
            0.0,
            value("min_margin_x100", round(matching["min_score_margin"] * 100)) / 100.0,
        ),
    )
    return DigitConfig(**data)


def _control_specs(config: DigitConfig, image_area: int) -> tuple[tuple[str, int, int], ...]:
    mode = {"fixed": 0, "otsu": 1, "adaptive": 2}[config.preprocess["threshold_mode"]]
    return (
        ("threshold_mode", mode, 2),
        ("fixed_threshold", config.preprocess["fixed_threshold"], 255),
        ("adaptive_block_size", config.preprocess["adaptive_block_size"], 99),
        ("adaptive_c+50", round(config.preprocess["adaptive_c"] + 50), 100),
        ("clahe_enable", int(config.preprocess["use_clahe"]), 1),
        ("clahe_clip_x10", round(config.preprocess["clahe_clip_limit"] * 10), 200),
        ("gaussian_kernel", config.preprocess["gaussian_kernel"], 31),
        ("morph_open", config.preprocess["morph_open"], 31),
        ("morph_close", config.preprocess["morph_close"], 31),
        ("min_area", round(config.candidate["min_area_px"]), max(image_area, 1)),
        ("min_height", config.candidate["min_height_px"], 1000),
        ("min_aspect_x100", round(config.candidate["min_aspect_ratio"] * 100), 500),
        ("max_aspect_x100", round(config.candidate["max_aspect_ratio"] * 100), 500),
        ("min_score_x100", round(config.matching["min_score"] * 100), 100),
        ("min_margin_x100", round(config.matching["min_score_margin"] * 100), 100),
    )


def _create_controls(config: DigitConfig, image_area: int) -> None:
    cv2.namedWindow(CONTROL_WINDOW)
    CONTROL_MAXIMUMS.clear()
    for name, raw_value, maximum in _control_specs(config, image_area):
        maximum = max(1, int(maximum))
        initial = min(maximum, max(0, int(raw_value)))
        CONTROL_MAXIMUMS[name] = maximum
        cv2.createTrackbar(name, CONTROL_WINDOW, initial, maximum, _noop)


def _read_controls(base: DigitConfig) -> DigitConfig:
    values = {
        name: cv2.getTrackbarPos(name, CONTROL_WINDOW)
        for name in CONTROL_MAXIMUMS
    }
    return normalize_tuner_values(base, values)


def _sync_controls(config: DigitConfig, image_area: int) -> None:
    for name, value, _ in _control_specs(config, image_area):
        if name in CONTROL_MAXIMUMS:
            cv2.setTrackbarPos(
                name,
                CONTROL_WINDOW,
                min(CONTROL_MAXIMUMS[name], max(0, int(value))),
            )


def save_digit_config_atomic(config: DigitConfig, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(asdict(config), stream, allow_unicode=True, sort_keys=False)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    camera_config = load_camera_config(
        args.camera_config,
        {"device": _parse_device(args.device)},
    )
    base_config = load_digit_config(args.digit_config)
    detector = DigitDetector(base_config, require_complete_templates=True)
    camera = CameraService(camera_config)
    last_frame_id: int | None = None
    controls_created = False
    image_area = 1
    try:
        camera.start()
        while True:
            frame = camera.get_latest_frame(copy_image=False)
            if frame is None or frame.frame_id == last_frame_id:
                if (cv2.waitKey(5) & 0xFF) == ord("q"):
                    break
                time.sleep(0.002)
                continue
            last_frame_id = frame.frame_id
            image_area = frame.image.shape[0] * frame.image.shape[1]
            if not controls_created:
                _create_controls(base_config, image_area)
                controls_created = True
            current_config = _read_controls(base_config)
            detector.config = current_config
            result = detector.process(frame)
            debug = detector.get_debug_data()
            annotated = detector.draw_debug(frame.image, result)
            if debug is not None:
                score_text = " ".join(
                    f"{digit}:{debug.digit_scores[digit]:.2f}" for digit in range(10)
                )
                cv2.putText(
                    annotated,
                    score_text,
                    (10, annotated.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 255, 255),
                    1,
                    cv2.LINE_AA,
                )
                cv2.imshow("Digit Enhanced", debug.enhanced)
                cv2.imshow("Digit Mask", debug.mask)
                normalized = (
                    debug.normalized_digit
                    if debug.normalized_digit is not None
                    else np.zeros(
                        (
                            current_config.normalization["height"],
                            current_config.normalization["width"],
                        ),
                        np.uint8,
                    )
                )
                cv2.imshow("Digit Normalized", normalized)
            cv2.imshow("Digit Original", frame.image)
            cv2.imshow("Digit Candidates", annotated)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("s"):
                output = resolve_config_path(args.digit_config)
                save_digit_config_atomic(current_config, output)
                base_config = current_config
                print(f"已保存数字配置: {output}")
            elif key == ord("r"):
                base_config = load_digit_config(args.digit_config)
                detector = DigitDetector(base_config, require_complete_templates=True)
                _sync_controls(base_config, image_area)
                print(f"已重新加载数字配置: {resolve_config_path(args.digit_config)}")
            elif key == ord("q"):
                break
        return 0
    finally:
        camera.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
