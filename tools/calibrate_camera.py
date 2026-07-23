"""使用棋盘格图片标定相机并计算真实重投影误差。"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
import yaml


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


def build_argument_parser() -> argparse.ArgumentParser:
    """创建相机标定参数解析器。"""

    parser = argparse.ArgumentParser(description="从棋盘格图片生成相机内参")
    parser.add_argument("--images", required=True, help="棋盘格图片目录")
    parser.add_argument("--cols", type=int, required=True, help="棋盘格内角点列数")
    parser.add_argument("--rows", type=int, required=True, help="棋盘格内角点行数")
    parser.add_argument("--square-size-mm", type=float, required=True, help="方格边长毫米")
    parser.add_argument("--min-images", type=int, default=8, help="最少有效图片数，默认 8")
    parser.add_argument("--output", default="config/calibration.yaml", help="输出 YAML")
    parser.add_argument("--visualization-dir", help="保存角点可视化图片的目录")
    parser.add_argument(
        "--warning-error",
        type=float,
        default=1.0,
        help="平均重投影误差超过此像素值时提示复核，默认 1.0",
    )
    return parser


def calculate_reprojection_error(
    object_points: list[np.ndarray],
    image_points: list[np.ndarray],
    rotation_vectors: tuple[np.ndarray, ...],
    translation_vectors: tuple[np.ndarray, ...],
    camera_matrix: np.ndarray,
    distortion: np.ndarray,
) -> float:
    """计算所有有效图片的平均单点欧氏重投影误差。"""

    total_error = 0.0
    total_points = 0
    for objects, observed, rotation, translation in zip(
        object_points,
        image_points,
        rotation_vectors,
        translation_vectors,
    ):
        projected, _ = cv2.projectPoints(
            objects,
            rotation,
            translation,
            camera_matrix,
            distortion,
        )
        total_error += cv2.norm(observed, projected, cv2.NORM_L2) ** 2
        total_points += len(projected)
    return float(np.sqrt(total_error / max(total_points, 1)))


def main(argv: list[str] | None = None) -> int:
    """运行标定并保存 RMS 与真实平均重投影误差。"""

    args = build_argument_parser().parse_args(argv)
    if (
        args.cols <= 0
        or args.rows <= 0
        or args.square_size_mm <= 0
        or args.min_images <= 0
        or args.warning_error <= 0
    ):
        raise SystemExit("棋盘参数和 min-images 必须为正数")
    image_dir = Path(args.images)
    files = (
        sorted(
            path
            for path in image_dir.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        )
        if image_dir.is_dir()
        else []
    )
    if not files:
        raise SystemExit(f"未找到标定图片: {image_dir}")
    object_template = np.zeros((args.cols * args.rows, 3), np.float32)
    object_template[:, :2] = np.mgrid[0 : args.cols, 0 : args.rows].T.reshape(-1, 2)
    object_template[:, :2] *= args.square_size_mm
    object_points: list[np.ndarray] = []
    image_points: list[np.ndarray] = []
    image_size: tuple[int, int] | None = None
    visualization_dir = Path(args.visualization_dir) if args.visualization_dir else None
    if visualization_dir:
        visualization_dir.mkdir(parents=True, exist_ok=True)
    criteria = (
        cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER,
        30,
        0.001,
    )
    for path in files:
        image = cv2.imread(str(path))
        if image is None:
            print(f"FAIL {path.name}: 无法读取")
            continue
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        current_size = gray.shape[::-1]
        found, corners = cv2.findChessboardCorners(gray, (args.cols, args.rows))
        if not found:
            print(f"FAIL {path.name}: 未找到完整棋盘角点")
            continue
        if image_size is not None and current_size != image_size:
            raise SystemExit(
                f"有效图片分辨率不一致: {path.name}={current_size}, expected={image_size}"
            )
        image_size = current_size
        refined = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
        object_points.append(object_template.copy())
        image_points.append(refined)
        print(f"OK   {path.name}: {len(refined)} corners")
        if visualization_dir:
            rendered = image.copy()
            cv2.drawChessboardCorners(rendered, (args.cols, args.rows), refined, True)
            cv2.imwrite(str(visualization_dir / path.name), rendered)
    print(f"有效图片: {len(object_points)}/{len(files)}")
    if len(object_points) < args.min_images or image_size is None:
        raise SystemExit(f"有效棋盘图片少于 {args.min_images} 张，拒绝输出标定结果")
    rms, matrix, distortion, rotations, translations = cv2.calibrateCamera(
        object_points,
        image_points,
        image_size,
        None,
        None,
    )
    reprojection_error = calculate_reprojection_error(
        object_points,
        image_points,
        rotations,
        translations,
        matrix,
        distortion,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "calibrated": True,
        "image_width": image_size[0],
        "image_height": image_size[1],
        "camera_matrix": matrix.tolist(),
        "distortion_coefficients": distortion.flatten().tolist(),
        "reprojection_error": reprojection_error,
        "rms_error": float(rms),
    }
    with output.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
    print(f"RMS={rms:.6f}, mean_reprojection_error={reprojection_error:.6f}, output={output}")
    if reprojection_error > args.warning_error:
        print(
            "WARNING: 平均重投影误差超过用户设定的提示阈值 "
            f"{args.warning_error:.3f}px，请人工检查角点覆盖和图片质量"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
