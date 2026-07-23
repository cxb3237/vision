"""离线回放颜色或形状检测器并输出性能统计。"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import time

import cv2

from core.config_loader import load_color_config, load_shape_config
from core.models import ColorClass, FramePacket
from detectors.color_detector import ColorDetector
from detectors.shape_detector import ShapeDetector


def build_argument_parser() -> argparse.ArgumentParser:
    """创建回放工具参数解析器。"""

    parser = argparse.ArgumentParser(description="回放视频并验证视觉检测器")
    parser.add_argument("--input", required=True, help="输入视频")
    parser.add_argument("--detector", choices=("color", "shape"), default="color")
    parser.add_argument("--target", default="red", help="colors.yaml 中的目标颜色")
    parser.add_argument("--config", default="config/colors.yaml", help="颜色配置路径")
    parser.add_argument("--shape-config", default="config/shapes.yaml", help="形状配置路径")
    parser.add_argument("--display", action="store_true", help="显示调试画面")
    parser.add_argument("--speed", type=float, default=1.0, help="相对原视频速度；0 表示最快")
    parser.add_argument("--loop", action="store_true", help="循环回放")
    parser.add_argument("--pause", action="store_true", help="启动时暂停")
    parser.add_argument("--output", help="保存带标注的视频")
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行离线回放并保证释放读写器与窗口。"""

    args = build_argument_parser().parse_args(argv)
    if not math.isfinite(args.speed) or args.speed < 0:
        raise SystemExit("--speed 必须为有限非负数")
    if args.pause and not args.display:
        raise SystemExit("--pause 必须与 --display 一起使用")
    capture = cv2.VideoCapture(args.input)
    writer: cv2.VideoWriter | None = None
    frame_count = 0
    detected_count = 0
    processing_times: list[float] = []
    start = time.monotonic()
    paused = args.pause
    try:
        if not capture.isOpened():
            print(f"无法打开输入视频: {args.input}")
            return 2
        input_fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(input_fps) or input_fps <= 0:
            input_fps = 30.0
        if args.detector == "color":
            colors = load_color_config(args.config)
            if args.target not in colors:
                print(f"目标颜色不存在: {args.target}; 可选: {', '.join(colors)}")
                return 2
            color_class = ColorClass.from_name(args.target)
            if color_class == ColorClass.UNKNOWN:
                print(f"目标颜色没有稳定协议类别: {args.target}")
                return 2
            detector = ColorDetector(colors[args.target], target_class=int(color_class))
        else:
            detector = ShapeDetector(config=load_shape_config(args.shape_config))
        detector.initialize()
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
        while True:
            if paused and args.display:
                key = cv2.waitKey(0) & 0xFF
                if key == ord("q"):
                    break
                if key in (ord(" "), ord("p")):
                    paused = False
                    continue
                if key != ord("n"):
                    continue
            frame_start = time.monotonic()
            ok, image = capture.read()
            if not ok or image is None:
                if args.loop and frame_count > 0:
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    detector.reset()
                    continue
                break
            packet = FramePacket(frame_count + 1, time.monotonic(), image)
            process_start = time.monotonic()
            result = detector.process(packet)
            processing_times.append(time.monotonic() - process_start)
            frame_count += 1
            detected_count += int(result.found)
            annotated = detector.draw_debug(image, result)
            if args.output:
                if writer is None:
                    height, width = image.shape[:2]
                    writer = cv2.VideoWriter(
                        args.output,
                        cv2.VideoWriter_fourcc(*"mp4v"),
                        input_fps,
                        (width, height),
                    )
                    if not writer.isOpened():
                        writer.release()
                        writer = None
                        print(f"无法创建输出视频: {args.output}")
                        return 3
                writer.write(annotated)
            if args.display:
                cv2.imshow("replay", annotated)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                if key in (ord(" "), ord("p")):
                    paused = True
            if args.speed > 0:
                target_period = 1.0 / (input_fps * args.speed)
                delay = target_period - (time.monotonic() - frame_start)
                if delay > 0:
                    time.sleep(delay)
    finally:
        capture.release()
        if writer is not None:
            writer.release()
        cv2.destroyAllWindows()
    wall_time = max(time.monotonic() - start, 0.001)
    algorithm_time = sum(processing_times)
    print(f"total_frames={frame_count}")
    print(f"detected_frames={detected_count}")
    print(f"detection_rate={detected_count / max(frame_count, 1):.4f}")
    print(f"average_detection_ms={1000 * algorithm_time / max(frame_count, 1):.3f}")
    print(f"maximum_detection_ms={1000 * max(processing_times, default=0.0):.3f}")
    print(f"algorithm_fps={frame_count / max(algorithm_time, 0.001):.3f}")
    print(f"wall_clock_fps={frame_count / wall_time:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
