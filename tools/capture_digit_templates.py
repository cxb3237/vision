"""使用 CameraService 交互式采集 0～9 原始 ROI 与归一化模板。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import re
import time

import cv2

from core.config_loader import load_camera_config, load_digit_config, resolve_config_path
from core.models import DigitConfig, FramePacket
from detectors.digit_detector import DigitDetector
from drivers.camera_service import CameraService


WINDOW = "Digit Template Capture"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="采集单个印刷数字 0～9 模板")
    parser.add_argument("--device", help="摄像头编号或设备路径；默认使用 camera.yaml")
    parser.add_argument("--camera-config", default="config/camera.yaml")
    parser.add_argument("--digit-config", default="config/digit.yaml")
    parser.add_argument("--output-root", default="data/digits/templates")
    parser.add_argument("--box-width", type=int, default=240)
    parser.add_argument("--box-height", type=int, default=320)
    parser.add_argument("--min-blur", type=float, default=60.0)
    return parser


def _parse_device(value: str | None) -> str | int | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def next_template_index(directory: Path, digit: int) -> int:
    """返回不会覆盖现有 ``digit_NNNN`` 文件的新编号。"""

    pattern = re.compile(rf"^{digit}_(\d+)(?:_raw)?$")
    indexes = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = pattern.match(path.stem)
            if match:
                indexes.append(int(match.group(1)))
    return max(indexes, default=0) + 1


def _template_count(directory: Path) -> int:
    return sum(
        1
        for path in directory.glob("*.*")
        if path.is_file() and not path.stem.endswith("_raw") and path.suffix.lower() in {".png", ".jpg"}
    )


def build_capture_digit_config(config: DigitConfig, output_root: Path) -> DigitConfig:
    """创建不启用正式 ROI、且允许从输出目录加载模板的采集配置副本。"""

    capture_data = asdict(config)
    capture_data["roi"] = {
        **capture_data["roi"],
        "enabled": False,
        "x": 0,
        "y": 0,
    }
    capture_data["matching"] = {
        **capture_data["matching"],
        "template_root": str(output_root),
    }
    return DigitConfig(**capture_data)


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.box_width <= 0 or args.box_height <= 0 or args.min_blur < 0:
        raise SystemExit("采集框宽高必须为正数，min-blur 不能为负数")
    camera_config = load_camera_config(
        args.camera_config,
        {"device": _parse_device(args.device)},
    )
    output_root = resolve_config_path(args.output_root)
    directories = {digit: output_root / str(digit) for digit in range(10)}
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    digit_config = load_digit_config(args.digit_config)
    detector = DigitDetector(
        build_capture_digit_config(digit_config, output_root),
        require_complete_templates=False,
    )
    counts = {digit: _template_count(directory) for digit, directory in directories.items()}
    selected_digit = 0
    last_frame_id: int | None = None
    last_saved: list[tuple[Path, Path, int]] = []
    camera = CameraService(camera_config)
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
            image = frame.image
            image_height, image_width = image.shape[:2]
            box_width = min(args.box_width, image_width)
            box_height = min(args.box_height, image_height)
            left = (image_width - box_width) // 2
            top = (image_height - box_height) // 2
            roi_image = image[top : top + box_height, left : left + box_width]
            detector.process(FramePacket(frame.frame_id, frame.capture_timestamp, roi_image))
            debug = detector.get_debug_data()
            display = image.copy()
            ready = debug is not None and debug.normalized_digit is not None
            blur_score = 0.0
            if debug is not None and debug.candidate_bbox is not None:
                x, y, width, height = debug.candidate_bbox
                gray = cv2.cvtColor(roi_image[y : y + height, x : x + width], cv2.COLOR_BGR2GRAY)
                if gray.size:
                    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            ready = bool(ready and blur_score >= args.min_blur)
            color = (0, 255, 0) if ready else (0, 0, 255)
            cv2.rectangle(display, (left, top), (left + box_width, top + box_height), color, 2)
            missing = [str(digit) for digit, count in counts.items() if count < 10]
            lines = (
                f"label={selected_digit} ready={'YES' if ready else 'NO'} blur={blur_score:.1f}/{args.min_blur:g}",
                "counts " + " ".join(f"{digit}:{counts[digit]}" for digit in range(10)),
                f"need >=10: {','.join(missing) if missing else 'complete'}",
                "0-9: label  SPACE/S: save  D: delete last  Q: quit",
            )
            for index, text in enumerate(lines):
                cv2.putText(
                    display,
                    text,
                    (10, 26 + index * 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.52,
                    color if index == 0 else (0, 255, 255),
                    1,
                    cv2.LINE_AA,
                )
            cv2.imshow(WINDOW, display)
            if debug is not None:
                cv2.imshow("Digit Template Mask", debug.mask)
                if debug.normalized_digit is not None:
                    cv2.imshow("Digit Template Normalized", debug.normalized_digit)
            key = cv2.waitKey(1) & 0xFF
            if ord("0") <= key <= ord("9"):
                selected_digit = key - ord("0")
            elif key in (ord(" "), ord("s")):
                if not ready or debug is None or debug.normalized_digit is None:
                    print("NOT SAVED: 未找到合格数字候选或图像过于模糊")
                    continue
                directory = directories[selected_digit]
                index = next_template_index(directory, selected_digit)
                stem = f"{selected_digit}_{index:04d}"
                raw_path = directory / f"{stem}_raw.jpg"
                template_path = directory / f"{stem}.png"
                if not cv2.imwrite(str(raw_path), roi_image):
                    raise RuntimeError(f"保存原始数字 ROI 失败: {raw_path}")
                if not cv2.imwrite(str(template_path), debug.normalized_digit):
                    raw_path.unlink(missing_ok=True)
                    raise RuntimeError(f"保存归一化数字模板失败: {template_path}")
                last_saved.append((raw_path, template_path, selected_digit))
                counts[selected_digit] += 1
                print(f"SAVED digit={selected_digit} raw={raw_path.name} template={template_path.name}")
            elif key == ord("d"):
                if not last_saved:
                    print("NOT DELETED: 本次运行尚未保存模板")
                    continue
                raw_path, template_path, digit = last_saved.pop()
                raw_path.unlink(missing_ok=True)
                template_path.unlink(missing_ok=True)
                counts[digit] = max(0, counts[digit] - 1)
                print(f"DELETED digit={digit} {template_path.name}")
            elif key == ord("q"):
                break
        return 0
    finally:
        camera.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
