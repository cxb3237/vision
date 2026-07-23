"""HSV 颜色目标检测，含候选匹配和多帧状态确认。"""

from __future__ import annotations

import math
import time
from typing import Any

import cv2
import numpy as np

from core.models import DetectorConfig, FramePacket, TargetState, VisionResult
from detectors.base_detector import BaseDetector


class ColorDetector(BaseDetector):
    """从一个或多个 HSV 区间提取颜色目标并维持短期状态。"""

    def __init__(
        self,
        color: dict[str, Any],
        config: DetectorConfig | None = None,
        target_class: int = 1,
        temporal_tracking: bool = True,
    ) -> None:
        self.color = color
        self.config = config or DetectorConfig.from_color_config(color)
        self.target_class = target_class
        self.temporal_tracking = temporal_tracking
        self.reset()

    @staticmethod
    def _kernel_size(value: int) -> int:
        if value <= 0:
            return 0
        return value if value % 2 else value + 1

    def initialize(self) -> None:
        """重置时序状态；本检测器无外部资源。"""

        self.reset()

    def reset(self) -> None:
        """清除命中、丢失计数和历史位置。"""

        self._hits = 0
        self._misses = 0
        self._last_position: tuple[int, int] | None = None
        self._state = TargetState.NONE

    def _create_mask(self, image: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for hsv_range in self.color["ranges"]:
            lower = np.asarray(hsv_range["lower"], dtype=np.uint8)
            upper = np.asarray(hsv_range["upper"], dtype=np.uint8)
            mask = cv2.bitwise_or(mask, cv2.inRange(hsv, lower, upper))
        open_size = self._kernel_size(self.config.morph_open)
        close_size = self._kernel_size(self.config.morph_close)
        if open_size:
            kernel = np.ones((open_size, open_size), dtype=np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        if close_size:
            kernel = np.ones((close_size, close_size), dtype=np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        return mask

    def _find_candidates(self, mask: np.ndarray) -> list[dict[str, Any]]:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[dict[str, Any]] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if not self.config.min_area <= area <= self.config.max_area:
                continue
            moments = cv2.moments(contour)
            if moments["m00"] == 0:
                continue
            x, y, width, height = cv2.boundingRect(contour)
            candidates.append(
                {
                    "area": area,
                    "center": (
                        int(moments["m10"] / moments["m00"]),
                        int(moments["m01"] / moments["m00"]),
                    ),
                    "bbox": (x, y, width, height),
                    "contour": contour,
                }
            )
        return candidates

    def _choose_candidate(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not candidates:
            return None
        if not self.temporal_tracking:
            return max(candidates, key=lambda item: item["area"])
        if self._last_position is None:
            return max(candidates, key=lambda item: item["area"])
        chosen = min(
            candidates,
            key=lambda item: math.dist(item["center"], self._last_position),
        )
        if math.dist(chosen["center"], self._last_position) > self.config.max_jump_px:
            return None
        return chosen

    def _empty_result(self, frame: FramePacket, now: float) -> VisionResult:
        if not self.temporal_tracking:
            height, width = frame.image.shape[:2]
            return VisionResult(
                frame.frame_id,
                frame.capture_timestamp,
                now,
                target_state=TargetState.NONE,
                processing_delay_ms=max(0, round((now - frame.capture_timestamp) * 1000)),
                image_width=width,
                image_height=height,
            )
        self._hits = 0
        self._misses += 1
        if self._last_position is None:
            self._state = TargetState.NONE
        elif self._misses < self.config.lost_frames:
            self._state = TargetState.OCCLUDED
        else:
            self._state = TargetState.LOST
            self._last_position = None
        height, width = frame.image.shape[:2]
        return VisionResult(
            frame.frame_id,
            frame.capture_timestamp,
            now,
            target_state=self._state,
            processing_delay_ms=max(0, round((now - frame.capture_timestamp) * 1000)),
            image_width=width,
            image_height=height,
        )

    def _confidence(
        self,
        area: float,
        bbox: tuple[int, int, int, int],
        image_area: int,
        center: tuple[int, int],
    ) -> int:
        _, _, width, height = bbox
        fill_score = min(1.0, area / max(width * height, 1))
        expected = math.sqrt(self.config.min_area * self.config.max_area)
        area_score = min(area, expected) / max(area, expected)
        frame_score = min(1.0, area / max(image_area * 0.02, 1.0))
        stability_score = min(1.0, self._hits / max(self.config.confirm_frames, 1))
        if self._last_position is None:
            position_score = 0.5
        else:
            distance = math.dist(center, self._last_position)
            position_score = max(0.0, 1.0 - distance / self.config.max_jump_px)
        combined = (
            0.30 * fill_score
            + 0.20 * area_score
            + 0.15 * frame_score
            + 0.20 * stability_score
            + 0.15 * position_score
        )
        return max(0, min(1000, round(combined * 1000)))

    def process(self, frame: FramePacket) -> VisionResult:
        """检测颜色目标；输入必须是非空 BGR 三通道图像。"""

        image = frame.image
        if not isinstance(image, np.ndarray) or image.size == 0:
            raise ValueError("颜色检测输入图像为空")
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("颜色检测输入必须是 BGR 三通道图像")
        mask = self._create_mask(image)
        candidate = self._choose_candidate(self._find_candidates(mask))
        now = time.monotonic()
        if candidate is None:
            return self._empty_result(frame, now)

        center = candidate["center"]
        bbox = candidate["bbox"]
        area = candidate["area"]
        self._hits = self._hits + 1 if self.temporal_tracking else 1
        self._misses = 0
        confidence = self._confidence(area, bbox, image.shape[0] * image.shape[1], center)
        self._last_position = center if self.temporal_tracking else None
        self._state = TargetState.CANDIDATE
        if self.temporal_tracking and self._hits >= self.config.confirm_frames:
            self._state = TargetState.LOCKED
        x, y, width, height = bbox
        return VisionResult(
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            process_timestamp=now,
            found=True,
            target_state=self._state,
            target_class=self.target_class,
            center_x=center[0],
            center_y=center[1],
            error_x_px=center[0] - image.shape[1] // 2,
            error_y_px=center[1] - image.shape[0] // 2,
            bbox_x=x,
            bbox_y=y,
            bbox_width=width,
            bbox_height=height,
            area_px=area,
            confidence=confidence,
            processing_delay_ms=max(0, round((now - frame.capture_timestamp) * 1000)),
            image_width=image.shape[1],
            image_height=image.shape[0],
        )

    def draw_debug(self, image: np.ndarray, result: VisionResult) -> np.ndarray:
        """绘制中心、目标框、误差线及状态信息，不修改原图。"""

        output = image.copy()
        height, width = output.shape[:2]
        image_center = (width // 2, height // 2)
        cv2.drawMarker(output, image_center, (255, 255, 255), cv2.MARKER_CROSS, 16, 2)
        if result.found:
            target_center = (result.center_x, result.center_y)
            cv2.rectangle(
                output,
                (result.bbox_x, result.bbox_y),
                (result.bbox_x + result.bbox_width, result.bbox_y + result.bbox_height),
                (0, 255, 0),
                2,
            )
            cv2.drawMarker(output, target_center, (0, 255, 255), cv2.MARKER_CROSS, 14, 2)
            cv2.line(output, image_center, target_center, (255, 255, 0), 1)
        try:
            state_name = TargetState(result.target_state).name
        except ValueError:
            state_name = str(result.target_state)
        lines = (
            f"state={state_name} class={result.target_class}",
            f"area={result.area_px:.0f} confidence={result.confidence}",
            f"error=({result.error_x_px},{result.error_y_px})",
        )
        for index, text in enumerate(lines):
            cv2.putText(
                output,
                text,
                (10, 24 + index * 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
        return output
