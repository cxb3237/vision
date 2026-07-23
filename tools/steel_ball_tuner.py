"""使用 CameraService 的钢球检测实时参数调节工具。"""

from __future__ import annotations

import argparse
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


def _create_controls(config: SteelBallConfig) -> None:
    cv2.namedWindow(CONTROL_WINDOW)
    controls = (
        ("threshold_mode", int(config.threshold_mode == "adaptive"), 1),
        ("threshold", config.threshold, 255),
        ("invert", int(config.invert), 1),
        ("adaptive_block", config.adaptive_block_size, 99),
        ("adaptive_c+50", round(config.adaptive_c + 50), 100),
        ("gaussian", config.gaussian_kernel, 31),
        ("morph_open", config.morph_open, 31),
        ("morph_close", config.morph_close, 31),
        ("min_diameter", round(config.min_diameter_px), 300),
        ("max_diameter", round(config.max_diameter_px), 500),
        ("min_circularity", round(config.min_circularity * 100), 100),
        ("hough_enable", int(config.hough_enabled), 1),
        ("hough_dp_x10", round(config.hough_dp * 10), 50),
        ("hough_min_dist", round(config.hough_min_dist), 300),
        ("hough_param1", round(config.hough_param1), 300),
        ("hough_param2", round(config.hough_param2), 150),
        ("hough_min_radius", config.hough_min_radius, 200),
        ("hough_max_radius", config.hough_max_radius, 300),
    )
    for name, value, maximum in controls:
        cv2.createTrackbar(name, CONTROL_WINDOW, int(value), maximum, _noop)


def _read_config(base: SteelBallConfig) -> SteelBallConfig:
    block_size = max(3, cv2.getTrackbarPos("adaptive_block", CONTROL_WINDOW))
    block_size = block_size if block_size % 2 else block_size + 1
    gaussian = cv2.getTrackbarPos("gaussian", CONTROL_WINDOW)
    gaussian = gaussian if gaussian == 0 or gaussian % 2 else gaussian + 1
    minimum_diameter = max(1, cv2.getTrackbarPos("min_diameter", CONTROL_WINDOW))
    maximum_diameter = max(
        minimum_diameter,
        cv2.getTrackbarPos("max_diameter", CONTROL_WINDOW),
    )
    minimum_radius = cv2.getTrackbarPos("hough_min_radius", CONTROL_WINDOW)
    maximum_radius = max(
        minimum_radius,
        cv2.getTrackbarPos("hough_max_radius", CONTROL_WINDOW),
    )
    return replace(
        base,
        threshold_mode=(
            "adaptive"
            if cv2.getTrackbarPos("threshold_mode", CONTROL_WINDOW)
            else "fixed"
        ),
        threshold=cv2.getTrackbarPos("threshold", CONTROL_WINDOW),
        invert=bool(cv2.getTrackbarPos("invert", CONTROL_WINDOW)),
        adaptive_block_size=block_size,
        adaptive_c=float(cv2.getTrackbarPos("adaptive_c+50", CONTROL_WINDOW) - 50),
        gaussian_kernel=gaussian,
        morph_open=cv2.getTrackbarPos("morph_open", CONTROL_WINDOW),
        morph_close=cv2.getTrackbarPos("morph_close", CONTROL_WINDOW),
        min_diameter_px=float(minimum_diameter),
        max_diameter_px=float(maximum_diameter),
        min_circularity=max(
            0.01,
            cv2.getTrackbarPos("min_circularity", CONTROL_WINDOW) / 100.0,
        ),
        hough_enabled=bool(cv2.getTrackbarPos("hough_enable", CONTROL_WINDOW)),
        hough_dp=max(0.1, cv2.getTrackbarPos("hough_dp_x10", CONTROL_WINDOW) / 10.0),
        hough_min_dist=float(
            max(1, cv2.getTrackbarPos("hough_min_dist", CONTROL_WINDOW))
        ),
        hough_param1=float(max(1, cv2.getTrackbarPos("hough_param1", CONTROL_WINDOW))),
        hough_param2=float(max(1, cv2.getTrackbarPos("hough_param2", CONTROL_WINDOW))),
        hough_min_radius=minimum_radius,
        hough_max_radius=maximum_radius,
    )


def _save_atomic(config: SteelBallConfig, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(asdict(config), stream, allow_unicode=True, sort_keys=False)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    """运行实时调参，按 s 保存 YAML，按 q 退出。"""

    args = build_argument_parser().parse_args(argv)
    device = _parse_device(args.device)
    camera_config = load_camera_config(args.camera_config, {"device": device})
    base_config = load_steel_ball_config(args.config)
    calibration = load_calibration_config(args.calibration_config)
    camera = CameraService(camera_config)
    detector = SteelBallDetector(base_config, calibration)
    last_frame_id: int | None = None
    try:
        camera.start()
        _create_controls(base_config)
        while True:
            frame = camera.get_latest_frame(copy_image=False)
            if frame is None or frame.frame_id == last_frame_id:
                if (cv2.waitKey(5) & 0xFF) == ord("q"):
                    break
                time.sleep(0.002)
                continue
            last_frame_id = frame.frame_id
            current_config = _read_config(base_config)
            detector.config = current_config
            result = detector.process(frame)
            debug = detector.get_debug_data()
            annotated = detector.draw_debug(frame.image, result)
            if debug is not None:
                status = (
                    f"candidates={debug.candidate_count} "
                    f"target={'YES' if result.found else 'NO'} "
                    f"time={debug.processing_ms:.2f}ms"
                )
                cv2.putText(
                    annotated,
                    status,
                    (12, annotated.shape[0] - 16),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
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
                _save_atomic(current_config, output)
                base_config = current_config
                print(f"已保存钢球配置: {output}")
            elif key == ord("q"):
                break
        return 0
    finally:
        camera.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
