"""使用标定参数检查静态图片或实时摄像头去畸变效果。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import time

import cv2
import numpy as np

from core.config_loader import load_calibration_config, load_camera_config
from core.models import CalibrationConfig
from drivers.camera_service import CameraService


LOG = logging.getLogger(__name__)


def build_argument_parser() -> argparse.ArgumentParser:
    """创建去畸变测试参数解析器。"""

    parser = argparse.ArgumentParser(description="显示并保存原图/去畸变对比")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="输入图片")
    source.add_argument("--device", help="摄像头编号或设备路径")
    parser.add_argument("--config", default="config/calibration.yaml", help="标定 YAML")
    parser.add_argument("--camera-config", default="config/camera.yaml", help="摄像头 YAML")
    parser.add_argument("--alpha", type=float, default=0.0, help="有效像素与视野权衡 0..1")
    parser.add_argument("--display", action="store_true", help="显示原图和结果对比")
    parser.add_argument("--output", default="undistorted.jpg", help="结果图片路径")
    return parser


def validate_calibration_resolution(image: np.ndarray, calibration: CalibrationConfig) -> None:
    """默认严格拒绝与标定分辨率不同的输入。"""

    actual = (image.shape[1], image.shape[0])
    expected = (calibration.image_width, calibration.image_height)
    if actual != expected:
        raise ValueError(f"输入分辨率与标定不一致: expected={expected}, actual={actual}")


def _undistort(
    image: np.ndarray,
    matrix: np.ndarray,
    distortion: np.ndarray,
    alpha: float,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    height, width = image.shape[:2]
    new_matrix, roi = cv2.getOptimalNewCameraMatrix(
        matrix,
        distortion,
        (width, height),
        alpha,
        (width, height),
    )
    return cv2.undistort(image, matrix, distortion, None, new_matrix), roi


def main(argv: list[str] | None = None) -> int:
    """运行去畸变检查并安全停止 CameraService 和窗口。"""

    args = build_argument_parser().parse_args(argv)
    if not 0 <= args.alpha <= 1:
        raise SystemExit("--alpha 必须在 0..1 范围内")
    calibration = load_calibration_config(args.config)
    if not calibration.calibrated:
        raise SystemExit("相机尚未标定")
    matrix = np.asarray(calibration.camera_matrix, dtype=float)
    distortion = np.asarray(calibration.distortion_coefficients, dtype=float)
    camera: CameraService | None = None
    last_output = None
    try:
        if args.input:
            image = cv2.imread(args.input)
            if image is None:
                raise SystemExit(f"输入图片不存在或不可读: {args.input}")
            validate_calibration_resolution(image, calibration)
            last_output, _ = _undistort(image, matrix, distortion, args.alpha)
            if args.display:
                cv2.imshow("original | undistorted", np.hstack((image, last_output)))
                cv2.waitKey(0)
        else:
            device: str | int = int(args.device) if str(args.device).isdigit() else args.device
            config = load_camera_config(
                args.camera_config,
                {
                    "device": device,
                    "width": calibration.image_width,
                    "height": calibration.image_height,
                },
            )
            camera = CameraService(config)
            camera.start()
            deadline = time.monotonic() + 5.0
            last_frame_id: int | None = None
            while True:
                frame = camera.get_latest_frame(copy_image=True)
                if frame is None or frame.frame_id == last_frame_id:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("等待标定分辨率摄像头帧超过 5 秒")
                    time.sleep(0.005)
                    continue
                last_frame_id = frame.frame_id
                image = frame.image
                validate_calibration_resolution(image, calibration)
                last_output, _ = _undistort(image, matrix, distortion, args.alpha)
                statistics = camera.get_statistics()
                LOG.info(
                    "实际摄像头分辨率=%dx%d FPS=%.2f",
                    image.shape[1],
                    image.shape[0],
                    float(statistics["actual_fps"]),
                )
                if not args.display:
                    break
                cv2.imshow("original | undistorted", np.hstack((image, last_output)))
                if (cv2.waitKey(1) & 0xFF) == ord("q"):
                    break
        if last_output is None:
            return 2
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(output), last_output):
            raise RuntimeError(f"保存去畸变图片失败: {output}")
        print(f"已保存: {output}")
        return 0
    finally:
        if camera is not None:
            camera.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    raise SystemExit(main())
