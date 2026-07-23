"""直接探测 USB 摄像头的命令行工具。"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import platform
import time

import cv2


def build_argument_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""

    parser = argparse.ArgumentParser(description="探测 USB 摄像头并输出实际采集参数")
    parser.add_argument("--device", default="0", help="设备编号或路径，默认 0")
    parser.add_argument("--width", type=int, default=640, help="请求宽度")
    parser.add_argument("--height", type=int, default=480, help="请求高度")
    parser.add_argument("--fps", type=float, default=30.0, help="请求帧率")
    parser.add_argument("--fourcc", default="MJPG", help="请求 FOURCC，默认 MJPG")
    parser.add_argument(
        "--seconds",
        type=float,
        default=10.0,
        help="测试时长，到时自动退出；0 表示运行到 q 或 Ctrl+C",
    )
    parser.add_argument("--display", action="store_true", help="显示实时画面，可按 q 退出")
    parser.add_argument("--save", help="退出时保存最后一个成功帧")
    return parser


def decode_fourcc(value: float) -> str:
    """将 OpenCV 数值 FOURCC 转为可读字符串。"""

    encoded = int(value)
    return "".join(chr((encoded >> (8 * index)) & 0xFF) for index in range(4)).rstrip("\x00")


def _open_capture(device: str | int) -> cv2.VideoCapture:
    api = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY
    capture = cv2.VideoCapture(device, api)
    if not capture.isOpened() and api != cv2.CAP_ANY:
        capture.release()
        capture = cv2.VideoCapture(device, cv2.CAP_ANY)
    return capture


def main(argv: list[str] | None = None) -> int:
    """运行摄像头探测并返回进程退出码。"""

    args = build_argument_parser().parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds < 0:
        raise SystemExit("--seconds 必须为有限非负数")
    if args.width <= 0 or args.height <= 0:
        raise SystemExit("--width 和 --height 必须为正整数")
    if not math.isfinite(args.fps) or args.fps <= 0:
        raise SystemExit("--fps 必须为有限正数")
    if len(args.fourcc) != 4:
        raise SystemExit("--fourcc 必须恰好为 4 个字符")
    device: str | int = int(args.device) if str(args.device).isdigit() else args.device
    capture = _open_capture(device)
    start = time.monotonic()
    frames = 0
    failures = 0
    last_image = None
    try:
        if not capture.isOpened():
            print(f"无法打开摄像头: {device}")
            return 2
        if len(args.fourcc) == 4:
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*args.fourcc))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        capture.set(cv2.CAP_PROP_FPS, args.fps)
        while args.seconds == 0 or time.monotonic() - start < args.seconds:
            ok, image = capture.read()
            if not ok or image is None:
                failures += 1
                time.sleep(0.005)
                continue
            frames += 1
            last_image = image
            if args.display:
                cv2.imshow("camera probe", image)
                if (cv2.waitKey(1) & 0xFF) == ord("q"):
                    break
        if args.save and last_image is not None:
            output = Path(args.save)
            output.parent.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(output), last_image):
                print(f"保存图像失败: {output}")
                return 3
        elapsed = max(time.monotonic() - start, 0.001)
        print(f"device: {device}")
        print(
            "actual: "
            f"{int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
            f"{int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))} "
            f"fps={capture.get(cv2.CAP_PROP_FPS):.2f} "
            f"fourcc={decode_fourcc(capture.get(cv2.CAP_PROP_FOURCC)) or 'unknown'}"
        )
        print(
            f"frames_ok: {frames}, frames_failed: {failures}, "
            f"measured_fps: {frames / elapsed:.2f}"
        )
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        capture.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
