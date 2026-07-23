"""使用主程序摄像头配置的实时或静态 HSV 调参工具。"""

from __future__ import annotations

import argparse
from pathlib import Path
import time

import cv2
import numpy as np
import yaml

from core.config_loader import load_camera_config, load_color_config, resolve_config_path
from drivers.camera_service import CameraService


def build_argument_parser() -> argparse.ArgumentParser:
    """创建 HSV 调参参数解析器。"""

    parser = argparse.ArgumentParser(description="用滑条实时编辑 colors.yaml")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="静态图片")
    source.add_argument("--device", help="摄像头编号或设备路径")
    parser.add_argument("--color", required=True, help="要编辑的颜色名称")
    parser.add_argument("--range-index", type=int, default=0, help="HSV 区间编号，默认 0")
    parser.add_argument("--config", default="config/colors.yaml", help="颜色配置路径")
    parser.add_argument(
        "--camera-config",
        default="config/camera.yaml",
        help="实时模式使用的摄像头配置",
    )
    return parser


def create_live_camera(device: str, camera_config: str) -> CameraService:
    """按 camera.yaml 创建与主程序一致的实时 CameraService。"""

    parsed_device: str | int = int(device) if str(device).isdigit() else device
    config = load_camera_config(camera_config, {"device": parsed_device})
    return CameraService(config)


def _noop(_: int) -> None:
    return None


def _read_controls() -> tuple[list[int], list[int], int, int, int, int]:
    names = ("lower_h", "lower_s", "lower_v", "upper_h", "upper_s", "upper_v")
    values = [cv2.getTrackbarPos(name, "controls") for name in names]
    return (
        values[:3],
        values[3:],
        cv2.getTrackbarPos("morph_open", "controls"),
        cv2.getTrackbarPos("morph_close", "controls"),
        cv2.getTrackbarPos("min_area", "controls"),
        cv2.getTrackbarPos("max_area", "controls"),
    )


def _save_atomic(config: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    """运行 GUI 调参；实时模式仅处理不同 frame_id 的最新帧。"""

    args = build_argument_parser().parse_args(argv)
    config = load_color_config(args.config)
    if args.color not in config:
        raise SystemExit(f"颜色不存在: {args.color}")
    color = config[args.color]
    if not 0 <= args.range_index < len(color["ranges"]):
        raise SystemExit(f"range-index 超出范围 0..{len(color['ranges']) - 1}")
    selected_range = color["ranges"][args.range_index]
    camera: CameraService | None = None
    static_image = None
    last_frame_id: int | None = None
    try:
        if args.image:
            static_image = cv2.imread(args.image)
            if static_image is None:
                raise SystemExit(f"无法读取图片: {args.image}")
        else:
            camera = create_live_camera(args.device, args.camera_config)
            camera.start()
        try:
            cv2.namedWindow("controls")
            for name, value, maximum in zip(
                ("lower_h", "lower_s", "lower_v", "upper_h", "upper_s", "upper_v"),
                selected_range["lower"] + selected_range["upper"],
                (179, 255, 255, 179, 255, 255),
            ):
                cv2.createTrackbar(name, "controls", value, maximum, _noop)
            cv2.createTrackbar("morph_open", "controls", color["morph_open"], 31, _noop)
            cv2.createTrackbar("morph_close", "controls", color["morph_close"], 31, _noop)
            slider_max = max(1000, min(int(color["max_area"] * 2), 10_000_000))
            cv2.createTrackbar("min_area", "controls", int(color["min_area"]), slider_max, _noop)
            cv2.createTrackbar("max_area", "controls", int(color["max_area"]), slider_max, _noop)
        except cv2.error as exc:
            raise SystemExit(f"无法创建 GUI 窗口；请在桌面环境运行: {exc}") from exc
        while True:
            if camera is not None:
                frame = camera.get_latest_frame(copy_image=True)
                if frame is None or frame.frame_id == last_frame_id:
                    time.sleep(0.005)
                    continue
                last_frame_id = frame.frame_id
                image = frame.image
            else:
                assert static_image is not None
                image = static_image
            lower, upper, morph_open, morph_close, min_area, max_area = _read_controls()
            mask = cv2.inRange(
                cv2.cvtColor(image, cv2.COLOR_BGR2HSV),
                np.asarray(lower, np.uint8),
                np.asarray(upper, np.uint8),
            )
            for operation, size in ((cv2.MORPH_OPEN, morph_open), (cv2.MORPH_CLOSE, morph_close)):
                if size > 0:
                    size = size if size % 2 else size + 1
                    mask = cv2.morphologyEx(mask, operation, np.ones((size, size), np.uint8))
            annotated = image.copy()
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                area = cv2.contourArea(contour)
                if min_area <= area <= max_area:
                    cv2.drawContours(annotated, [contour], -1, (0, 255, 0), 2)
            if camera is not None:
                statistics = camera.get_statistics()
                status = (
                    f"{image.shape[1]}x{image.shape[0]} "
                    f"capture_fps={float(statistics['actual_fps']):.2f}"
                )
                cv2.putText(
                    annotated,
                    status,
                    (10, 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (255, 255, 255),
                    1,
                    cv2.LINE_AA,
                )
            cv2.imshow("original", image)
            cv2.imshow("mask", mask)
            cv2.imshow("detected", annotated)
            key = cv2.waitKey(20) & 0xFF
            if key == ord("s"):
                if min_area > max_area:
                    print("拒绝保存：min_area 不能大于 max_area")
                    continue
                color["ranges"][args.range_index] = {"lower": lower, "upper": upper}
                color["morph_open"] = morph_open
                color["morph_close"] = morph_close
                color["min_area"] = min_area
                color["max_area"] = max_area
                output = resolve_config_path(args.config)
                _save_atomic(config, output)
                print(f"已原子保存: {output}")
            if key == ord("q"):
                break
        return 0
    finally:
        if camera is not None:
            camera.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
