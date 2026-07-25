"""部署前检查0～9数字模板，不访问摄像头或其他硬件。"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2


def find_missing_digit_templates(template_root: str | Path) -> list[int]:
    """返回没有至少一张可解码PNG模板的数字标签。"""

    root = Path(template_root)
    missing: list[int] = []
    for digit in range(10):
        directory = root / str(digit)
        candidates = (
            sorted(directory.iterdir()) if directory.is_dir() else []
        )
        valid = any(
            path.is_file()
            and path.suffix.lower() == ".png"
            and cv2.imread(str(path), cv2.IMREAD_GRAYSCALE) is not None
            for path in candidates
        )
        if not valid:
            missing.append(digit)
    return missing


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查部署所需的数字PNG模板")
    parser.add_argument(
        "--template-root",
        default="data/digits/templates",
        help="0～9模板目录根路径",
    )
    parser.add_argument(
        "--detector",
        required=True,
        choices=("color", "shape", "steel_ball", "digit"),
        help="最终启动检测器",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    missing = find_missing_digit_templates(args.template_root)
    if args.detector != "digit":
        if missing:
            print("提示：数字模板尚未齐全，缺少数字: " + ", ".join(map(str, missing)))
        return 0
    if missing:
        print("数字检测器无法部署，缺少有效PNG模板: " + ", ".join(map(str, missing)))
        return 1
    print("数字模板部署检查通过：0～9每类至少一张有效PNG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
