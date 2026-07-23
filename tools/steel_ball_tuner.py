"""使用 CameraService 的钢球检测实时参数调节工具。"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
import time

import cv2
import yaml

from core.config_loader import (
    load_calibration_config,
    load_camera_config,
    load_steel_ball_config,
    resolve_config_path,
)
from core.models import SteelBallConfig
from detectors.steel_ball_detector import SteelBallDetector
from drivers.camera_service import CameraService


CONTROL_WINDOW = "Steel Ball Controls"
CONTROL_MAXIMUMS: dict[str, int] = {}


def build_argument_parser() -> argparse.ArgumentParser:
    """创建钢球实时调参命令行解析器。"""

    parser = argparse.ArgumentParser(description="实时调节直径 10 mm 钢球检测参数")
    parser.add_argument("--device", help="覆盖 camera.yaml 中的设备编号或路径")
    parser.add_argument("--camera-config", default="config/camera.yaml")
    parser.add_argument("--config", default="config/steel_ball.yaml")
    parser.add_argument("--calibration-config", default="config/calibration.yaml")
    return parser


def _noop(_: int) -> None:
    return None


def _parse_device(value: str | None) -> str | int | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def _odd_or_zero(value: int) -> int:
    value = max(0, int(value))
    return value if value == 0 or value % 2 else value + 1


def normalize_tuner_values(
    base: SteelBallConfig,
    values: Mapping[str, int],
    image_width: int,
    image_height: int,
) -> SteelBallConfig:
    """将滑动条整数映射为经过边界修正的完整检测配置。"""

    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")

    def value(name: str, fallback: int) -> int:
        return int(values.get(name, fallback))

    adaptive_block = max(3, value("adaptive_block", base.adaptive_block_size))
    if adaptive_block % 2 == 0:
        adaptive_block += 1
    gaussian = _odd_or_zero(value("gaussian", base.gaussian_kernel))
    morph_open = _odd_or_zero(value("morph_open", base.morph_open))
    morph_close = _odd_or_zero(value("morph_close", base.morph_close))
    minimum_diameter = max(1, value("min_diameter", round(base.min_diameter_px)))
    maximum_diameter = max(
        minimum_diameter,
        value("max_diameter", round(base.max_diameter_px)),
    )
    minimum_area = max(1, value("min_area", round(base.min_area_px)))
    maximum_area = max(minimum_area, value("max_area", round(base.max_area_px)))
    minimum_aspect = max(
        0.01,
        value("min_aspect_x100", round(base.min_aspect_ratio * 100)) / 100.0,
    )
    maximum_aspect = max(
        minimum_aspect,
        value("max_aspect_x100", round(base.max_aspect_ratio * 100)) / 100.0,
    )
    minimum_radius = max(0, value("hough_min_radius", base.hough_min_radius))
    maximum_radius = max(
        minimum_radius,
        value("hough_max_radius", base.hough_max_radius),
    )

    roi: list[int] | None = None
    if value("roi_enable", int(base.roi is not None)):
        fallback_roi = base.roi or [0, 0, image_width, image_height]
        x = min(max(0, value("roi_x", fallback_roi[0])), image_width - 1)
        y = min(max(0, value("roi_y", fallback_roi[1])), image_height - 1)
        width = min(max(1, value("roi_w", fallback_roi[2])), image_width - x)
        height = min(max(1, value("roi_h", fallback_roi[3])), image_height - y)
        roi = [x, y, width, height]

    return replace(
        base,
        roi=roi,
        clahe_enabled=bool(value("clahe_enable", int(base.clahe_enabled))),
        clahe_clip_limit=max(
            0.1,
            value("clahe_clip_x10", round(base.clahe_clip_limit * 10)) / 10.0,
        ),
        clahe_tile_grid_size=max(1, value("clahe_tile", base.clahe_tile_grid_size)),
        threshold_mode=(
            "adaptive"
            if value("threshold_mode", int(base.threshold_mode == "adaptive"))
            else "fixed"
        ),
        threshold=min(255, max(0, value("threshold", base.threshold))),
        invert=bool(value("invert", int(base.invert))),
        adaptive_block_size=adaptive_block,
        adaptive_c=float(value("adaptive_c+50", round(base.adaptive_c + 50)) - 50),
        gaussian_kernel=gaussian,
        morph_open=morph_open,
        morph_close=morph_close,
        min_diameter_px=float(minimum_diameter),
        max_diameter_px=float(maximum_diameter),
        min_area_px=float(minimum_area),
        max_area_px=float(maximum_area),
        min_circularity=min(
            1.0,
            max(
                0.01,
                value("min_circularity", round(base.min_circularity * 100)) / 100.0,
            ),
        ),
        min_aspect_ratio=minimum_aspect,
        max_aspect_ratio=maximum_aspect,
        confirm_frames=max(1, value("confirm_frames", base.confirm_frames)),
        lost_frames=max(1, value("lost_frames", base.lost_frames)),
        max_jump_px=float(max(1, value("max_jump", round(base.max_jump_px)))),
        hough_enabled=bool(value("hough_enable", int(base.hough_enabled))),
        hough_dp=max(0.1, value("hough_dp_x10", round(base.hough_dp * 10)) / 10.0),
        hough_min_dist=float(max(1, value("hough_min_dist", round(base.hough_min_dist)))),
        hough_param1=float(max(1, value("hough_param1", round(base.hough_param1)))),
        hough_param2=float(max(1, value("hough_param2", round(base.hough_param2)))),
        hough_min_radius=minimum_radius,
        hough_max_radius=maximum_radius,
    )


def _control_specs(
    config: SteelBallConfig,
    image_width: int,
    image_height: int,
) -> tuple[tuple[str, int, int], ...]:
    roi = config.roi or [0, 0, image_width, image_height]
    image_area = image_width * image_height
    return (
        ("clahe_enable", int(config.clahe_enabled), 1),
        ("clahe_clip_x10", round(config.clahe_clip_limit * 10), 200),
        ("clahe_tile", config.clahe_tile_grid_size, 32),
        ("threshold_mode", int(config.threshold_mode == "adaptive"), 1),
        ("threshold", config.threshold, 255),
        ("invert", int(config.invert), 1),
        ("adaptive_block", config.adaptive_block_size, 99),
        ("adaptive_c+50", round(config.adaptive_c + 50), 100),
        ("gaussian", config.gaussian_kernel, 31),
        ("morph_open", config.morph_open, 31),
        ("morph_close", config.morph_close, 31),
        ("min_diameter", round(config.min_diameter_px), max(500, image_width, image_height)),
        ("max_diameter", round(config.max_diameter_px), max(500, image_width, image_height)),
        ("min_area", round(config.min_area_px), max(image_area, round(config.max_area_px))),
        ("max_area", round(config.max_area_px), max(image_area, round(config.max_area_px))),
        ("min_circularity", round(config.min_circularity * 100), 100),
        ("min_aspect_x100", round(config.min_aspect_ratio * 100), 500),
        ("max_aspect_x100", round(config.max_aspect_ratio * 100), 500),
        ("max_jump", round(config.max_jump_px), max(1000, image_width, image_height)),
        ("confirm_frames", config.confirm_frames, 100),
        ("lost_frames", config.lost_frames, 100),
        ("roi_enable", int(config.roi is not None), 1),
        ("roi_x", roi[0], max(1, image_width - 1)),
        ("roi_y", roi[1], max(1, image_height - 1)),
        ("roi_w", roi[2], image_width),
        ("roi_h", roi[3], image_height),
        ("hough_enable", int(config.hough_enabled), 1),
        ("hough_dp_x10", round(config.hough_dp * 10), 50),
        ("hough_min_dist", round(config.hough_min_dist), 300),
        ("hough_param1", round(config.hough_param1), 300),
        ("hough_param2", round(config.hough_param2), 150),
        ("hough_min_radius", config.hough_min_radius, max(200, image_width, image_height)),
        ("hough_max_radius", config.hough_max_radius, max(300, image_width, image_height)),
    )


def _create_controls(config: SteelBallConfig, image_width: int, image_height: int) -> None:
    cv2.namedWindow(CONTROL_WINDOW)
    CONTROL_MAXIMUMS.clear()
    for name, raw_value, maximum in _control_specs(config, image_width, image_height):
        maximum = max(1, int(maximum))
        initial = min(maximum, max(0, int(raw_value)))
        CONTROL_MAXIMUMS[name] = maximum
        cv2.createTrackbar(name, CONTROL_WINDOW, initial, maximum, _noop)


def _read_config(
    base: SteelBallConfig,
    image_width: int,
    image_height: int,
) -> SteelBallConfig:
    values = {
        name: cv2.getTrackbarPos(name, CONTROL_WINDOW)
        for name in CONTROL_MAXIMUMS
    }
    return normalize_tuner_values(base, values, image_width, image_height)


def _sync_controls(config: SteelBallConfig, image_width: int, image_height: int) -> None:
    for name, value, _ in _control_specs(config, image_width, image_height):
        if name in CONTROL_MAXIMUMS:
            cv2.setTrackbarPos(
                name,
                CONTROL_WINDOW,
                min(CONTROL_MAXIMUMS[name], max(0, int(value))),
            )


def save_steel_ball_config_atomic(config: SteelBallConfig, output: Path) -> None:
    """完整写出配置，并仅在写入成功后原子替换目标文件。"""

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            yaml.safe_dump(asdict(config), stream, allow_unicode=True, sort_keys=False)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()


_save_atomic = save_steel_ball_config_atomic


def _format_optional(value: float | None, digits: int = 1) -> str:
    return "--" if value is None else f"{value:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    """运行实时调参；s 保存、r 重载、q 退出。"""

    args = build_argument_parser().parse_args(argv)
    device = _parse_device(args.device)
    camera_config = load_camera_config(args.camera_config, {"device": device})
    base_config = load_steel_ball_config(args.config)
    calibration = load_calibration_config(args.calibration_config)
    camera = CameraService(camera_config)
    detector = SteelBallDetector(base_config, calibration)
    last_frame_id: int | None = None
    controls_created = False
    image_width = 0
    image_height = 0
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
            image_height, image_width = frame.image.shape[:2]
            if not controls_created:
                base_config = normalize_tuner_values(base_config, {}, image_width, image_height)
                detector.config = base_config
                _create_controls(base_config, image_width, image_height)
                controls_created = True
            current_config = _read_config(base_config, image_width, image_height)
            detector.config = current_config
            result = detector.process(frame)
            debug = detector.get_debug_data()
            annotated = detector.draw_debug(frame.image, result)
            if debug is not None:
                roi_text = str(current_config.roi or [0, 0, image_width, image_height])
                status = (
                    f"candidates={debug.candidate_count} found={'YES' if result.found else 'NO'} "
                    f"diam={_format_optional(debug.selected_diameter_px)}px "
                    f"circ={_format_optional(debug.selected_circularity, 3)} "
                    f"aspect={_format_optional(debug.selected_aspect_ratio, 3)} "
                    f"area={_format_optional(debug.selected_area_px)} "
                    f"hough={debug.selected_hough_verified if debug.selected_hough_verified is not None else '--'} "
                    f"time={debug.processing_ms:.2f}ms ROI={roi_text}"
                )
                rejected = (
                    f"rejected area={debug.rejected_by_area} diam={debug.rejected_by_diameter} "
                    f"circ={debug.rejected_by_circularity} aspect={debug.rejected_by_aspect_ratio} "
                    f"hough={debug.rejected_by_hough}   S:save R:reload Q:quit"
                )
                for index, text in enumerate((status, rejected)):
                    cv2.putText(
                        annotated,
                        text,
                        (12, annotated.shape[0] - 38 + index * 22),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        (0, 255, 255),
                        1,
                        cv2.LINE_AA,
                    )
                cv2.imshow("Steel Ball Enhanced", debug.enhanced)
                cv2.imshow("Steel Ball Mask", debug.mask)
            cv2.imshow("Steel Ball Original", frame.image)
            cv2.imshow("Steel Ball Candidates", annotated)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("s"):
                output = resolve_config_path(args.config)
                save_steel_ball_config_atomic(current_config, output)
                base_config = current_config
                print(f"已保存钢球配置: {output}")
            elif key == ord("r"):
                reloaded = load_steel_ball_config(args.config)
                base_config = normalize_tuner_values(reloaded, {}, image_width, image_height)
                detector.config = base_config
                _sync_controls(base_config, image_width, image_height)
                print(f"已重新加载钢球配置: {resolve_config_path(args.config)}")
            elif key == ord("q"):
                break
        return 0
    finally:
        camera.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
