"""检查并可选应用 camera.yaml 中的 Linux V4L2 摄像头参数。"""

from __future__ import annotations

import argparse

from core.config_loader import load_camera_config
from drivers.v4l2_controls import (
    V4L2ControlError,
    apply_v4l2_controls,
    is_v4l2_available,
    read_v4l2_controls,
    resolve_video_device,
)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查或应用 Linux V4L2 摄像头参数")
    parser.add_argument("--device", help="摄像头编号或 /dev/videoN；默认读取 camera.yaml")
    parser.add_argument("--camera-config", default="config/camera.yaml")
    parser.add_argument("--apply", action="store_true", help="检查前逐项应用配置")
    parser.add_argument("--strict", action="store_true", help="任一失败时返回非零退出码")
    return parser


def _parse_device(value: str | None) -> str | int | None:
    if value is None:
        return None
    return int(value) if value.isdigit() else value


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    config = load_camera_config(
        args.camera_config,
        {"device": _parse_device(args.device)},
    )
    profile = config.v4l2_controls or {}
    enabled = bool(profile.get("enabled", False))
    strict = bool(args.strict or profile.get("strict", False))
    requested = {
        name: value
        for name, value in profile.items()
        if name not in {"enabled", "strict"} and value is not None
    }
    device_path = resolve_video_device(config.device)
    available = is_v4l2_available()

    print(f"设备路径: {device_path}")
    print(f"v4l2-ctl 可用: {'YES' if available else 'NO'}")
    print(f"配置启用: {'YES' if enabled else 'NO'}  strict={'YES' if strict else 'NO'}")
    print(f"要求控制值: {requested}")

    if not enabled:
        print("实际控制值: {}")
        print("不支持或失败: []")
        print("最终结果: PASS（V4L2 控制已禁用）")
        return 0

    apply_results: dict = {}
    if args.apply:
        try:
            apply_results = apply_v4l2_controls(
                config.device,
                requested,
                strict=strict,
            )
        except V4L2ControlError as exc:
            print(f"应用失败: {exc}")
            print("最终结果: FAIL")
            return 1

    actual = read_v4l2_controls(config.device, list(requested))
    failures: dict[str, str] = {}
    for name, expected in requested.items():
        result = apply_results.get(name)
        if result is not None and not result["success"]:
            failures[name] = result["error"] or "设置失败"
        elif actual.get(name) is None:
            failures[name] = "不支持或无法读取"
        elif actual[name] != expected:
            failures[name] = f"期望 {expected}，实际 {actual[name]}"

    print(f"实际控制值: {actual}")
    print(f"不支持或失败: {failures}")
    passed = available and not failures
    print(f"最终结果: {'PASS' if passed else 'FAIL'}")
    return 1 if strict and not passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
