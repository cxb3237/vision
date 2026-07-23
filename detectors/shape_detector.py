"""基于轮廓逼近的传统几何形状检测。"""

from __future__ import annotations

import math
import time

import cv2
import numpy as np

from core.models import FramePacket, ShapeConfig, TargetState, VisionResult
from detectors.base_detector import BaseDetector


SHAPES = {"unknown": 0, "triangle": 1, "rectangle": 2, "square": 3, "circle": 4}
SHAPE_NAMES = {value: key for key, value in SHAPES.items()}


def classify_contour(
    contour: np.ndarray,
    approximation_factor: float = 0.04,
    square_ratio_tolerance: float = 0.15,
    circle_threshold: float = 0.70,
) -> str:
    """按顶点数、长宽比和圆度分类单个轮廓。"""

    perimeter = cv2.arcLength(contour, True)
    area = cv2.contourArea(contour)
    if perimeter <= 0 or area <= 0:
        return "unknown"
    vertices = len(cv2.approxPolyDP(contour, approximation_factor * perimeter, True))
    if vertices == 3:
        return "triangle"
    if vertices == 4:
        _, _, width, height = cv2.boundingRect(contour)
        ratio = width / max(height, 1)
        if abs(ratio - 1.0) <= square_ratio_tolerance:
            return "square"
        return "rectangle"
    circularity = 4.0 * math.pi * area / (perimeter * perimeter)
    return "circle" if circularity >= circle_threshold else "unknown"


class ShapeDetector(BaseDetector):
    """检测画面中面积最大的合规几何轮廓。"""

    def __init__(
        self,
        min_area: float = 300.0,
        max_area: float = 300_000.0,
        canny_low: int = 60,
        canny_high: int = 140,
        approximation_factor: float = 0.04,
        square_ratio_tolerance: float = 0.15,
        circle_threshold: float = 0.70,
        config: ShapeConfig | None = None,
    ) -> None:
        if config is not None:
            min_area = config.min_area
            max_area = config.max_area
            canny_low = config.canny_low
            canny_high = config.canny_high
            approximation_factor = config.approximation_factor
            square_ratio_tolerance = config.square_ratio_tolerance
            circle_threshold = config.circle_threshold
        if min_area <= 0 or max_area < min_area:
            raise ValueError("形状面积范围无效")
        if not 0 < approximation_factor < 1:
            raise ValueError("轮廓逼近系数必须在 (0, 1) 范围内")
        self.min_area = min_area
        self.max_area = max_area
        self.canny_low = canny_low
        self.canny_high = canny_high
        self.approximation_factor = approximation_factor
        self.square_ratio_tolerance = square_ratio_tolerance
        self.circle_threshold = circle_threshold

    def initialize(self) -> None:
        """无状态检测器无需初始化，此方法为安全 no-op。"""

    def reset(self) -> None:
        """无状态检测器无需重置，此方法为安全 no-op。"""

    def process(self, frame: FramePacket) -> VisionResult:
        """检测最大合规轮廓且不修改输入图像。"""

        image = frame.image
        if not isinstance(image, np.ndarray) or image.size == 0:
            raise ValueError("形状检测输入图像为空")
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("形状检测输入必须是 BGR 三通道图像")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, self.canny_low, self.canny_high)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        valid = [
            contour
            for contour in contours
            if self.min_area <= cv2.contourArea(contour) <= self.max_area
        ]
        now = time.monotonic()
        delay = max(0, round((now - frame.capture_timestamp) * 1000))
        if not valid:
            return VisionResult(
                frame.frame_id,
                frame.capture_timestamp,
                now,
                processing_delay_ms=delay,
                image_width=image.shape[1],
                image_height=image.shape[0],
            )
        contour = max(valid, key=cv2.contourArea)
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            return VisionResult(
                frame.frame_id,
                frame.capture_timestamp,
                now,
                processing_delay_ms=delay,
                image_width=image.shape[1],
                image_height=image.shape[0],
            )
        area = float(cv2.contourArea(contour))
        perimeter = cv2.arcLength(contour, True)
        x, y, width, height = cv2.boundingRect(contour)
        center_x = int(moments["m10"] / moments["m00"])
        center_y = int(moments["m01"] / moments["m00"])
        shape = classify_contour(
            contour,
            self.approximation_factor,
            self.square_ratio_tolerance,
            self.circle_threshold,
        )
        circularity = (
            min(1.0, 4.0 * math.pi * area / (perimeter * perimeter)) if perimeter else 0.0
        )
        fill = min(1.0, area / max(width * height, 1))
        area_quality = min(1.0, area / max(self.min_area * 4.0, 1.0))
        geometry_quality = circularity if shape == "circle" else fill
        confidence = round(1000 * (0.35 * area_quality + 0.65 * geometry_quality))
        confidence = max(0, min(1000, confidence))
        return VisionResult(
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            process_timestamp=now,
            found=True,
            target_state=TargetState.CANDIDATE,
            target_class=SHAPES[shape],
            center_x=center_x,
            center_y=center_y,
            error_x_px=center_x - image.shape[1] // 2,
            error_y_px=center_y - image.shape[0] // 2,
            bbox_x=x,
            bbox_y=y,
            bbox_width=width,
            bbox_height=height,
            area_px=area,
            confidence=confidence,
            processing_delay_ms=delay,
            image_width=image.shape[1],
            image_height=image.shape[0],
        )

    def draw_debug(self, image: np.ndarray, result: VisionResult) -> np.ndarray:
        """绘制形状框、中心、名称、面积、圆度近似质量和置信度。"""

        output = image.copy()
        if not result.found:
            return output
        start = (result.bbox_x, result.bbox_y)
        end = (result.bbox_x + result.bbox_width, result.bbox_y + result.bbox_height)
        cv2.rectangle(output, start, end, (0, 255, 0), 2)
        cv2.drawMarker(
            output,
            (result.center_x, result.center_y),
            (0, 255, 255),
            cv2.MARKER_CROSS,
            14,
            2,
        )
        shape_name = SHAPE_NAMES.get(result.target_class, "unknown")
        text = f"{shape_name} area={result.area_px:.0f} confidence={result.confidence}"
        cv2.putText(
            output,
            text,
            (result.bbox_x, max(20, result.bbox_y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 0),
            1,
            cv2.LINE_AA,
        )
        return output
