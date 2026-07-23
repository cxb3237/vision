"""基于轮廓与可选 Hough 复核的已知直径钢球检测器。"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

import cv2
import numpy as np

from core.models import (
    CalibrationConfig,
    FramePacket,
    SteelBallConfig,
    TargetState,
    VisionResult,
)
from detectors.base_detector import BaseDetector


@dataclass(frozen=True, slots=True)
class BallCandidate:
    """钢球轮廓的完整几何指标。"""

    contour: np.ndarray
    area: float
    perimeter: float
    bbox: tuple[int, int, int, int]
    center: tuple[float, float]
    enclosing_radius: float
    equivalent_diameter: float
    circularity: float
    aspect_ratio: float
    hough_verified: bool


@dataclass(frozen=True, slots=True)
class SteelBallDebugData:
    """供实时调参工具读取的最近一帧中间结果。"""

    enhanced: np.ndarray
    mask: np.ndarray
    candidate_count: int
    processing_ms: float
    selected_diameter_px: float | None = None
    selected_circularity: float | None = None
    selected_aspect_ratio: float | None = None
    selected_area_px: float | None = None
    selected_hough_verified: bool | None = None
    rejected_by_area: int = 0
    rejected_by_diameter: int = 0
    rejected_by_circularity: int = 0
    rejected_by_aspect_ratio: int = 0
    rejected_by_hough: int = 0


def estimate_distance_mm(
    fx_px: float | None,
    known_diameter_mm: float,
    diameter_px: float,
) -> int:
    """按 fx*真实直径/像素直径估算距离，无效时返回协议未知值。"""

    values = (fx_px, known_diameter_mm, diameter_px)
    try:
        numeric_values = tuple(float(value) for value in values if value is not None)
    except (TypeError, ValueError):
        return 0xFFFF
    if len(numeric_values) != 3 or any(not math.isfinite(value) for value in numeric_values):
        return 0xFFFF
    fx_value, known_value, diameter_value = numeric_values
    if fx_value <= 0 or known_value <= 0 or diameter_value <= 0:
        return 0xFFFF
    distance = fx_value * known_value / diameter_value
    if not math.isfinite(distance) or distance <= 0:
        return 0xFFFF
    return min(0xFFFE, max(0, round(distance)))


class SteelBallDetector(BaseDetector):
    """检测直径 10 mm 钢球并维护确认、丢失与重新捕获状态。"""

    def __init__(
        self,
        config: SteelBallConfig,
        calibration: CalibrationConfig | None = None,
    ) -> None:
        self.config = config
        self.calibration = calibration
        self.target_class = config.target_class
        self._debug: SteelBallDebugData | None = None
        self._last_candidate: BallCandidate | None = None
        self._rejection_counts = self._empty_rejection_counts()
        self.reset()

    @staticmethod
    def _empty_rejection_counts() -> dict[str, int]:
        return {
            "area": 0,
            "diameter": 0,
            "circularity": 0,
            "aspect_ratio": 0,
            "hough": 0,
        }

    def initialize(self) -> None:
        """重置检测时序状态。"""

        self.reset()

    def reset(self) -> None:
        """清除确认、丢失和历史位置。"""

        self._hits = 0
        self._misses = 0
        self._last_position: tuple[float, float] | None = None
        self._last_candidate = None

    @staticmethod
    def _odd_kernel(value: int, minimum: int = 1) -> int:
        if value <= 0:
            return 0
        normalized = max(minimum, int(value))
        return normalized if normalized % 2 else normalized + 1

    def _resolve_roi(self, image: np.ndarray) -> tuple[int, int, int, int]:
        height, width = image.shape[:2]
        if self.config.roi is None:
            return 0, 0, width, height
        x, y, roi_width, roi_height = self.config.roi
        left = max(0, x)
        top = max(0, y)
        right = min(width, x + roi_width)
        bottom = min(height, y + roi_height)
        if right <= left or bottom <= top:
            raise ValueError("钢球 ROI 与输入图像没有有效交集")
        return left, top, right - left, bottom - top

    def preprocess(self, roi_image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """执行灰度、CLAHE、滤波、阈值及形态学预处理。"""

        gray = cv2.cvtColor(roi_image, cv2.COLOR_BGR2GRAY)
        enhanced = gray
        if self.config.clahe_enabled:
            tile = max(1, int(self.config.clahe_tile_grid_size))
            clahe = cv2.createCLAHE(
                clipLimit=float(self.config.clahe_clip_limit),
                tileGridSize=(tile, tile),
            )
            enhanced = clahe.apply(enhanced)
        gaussian = self._odd_kernel(self.config.gaussian_kernel)
        if gaussian > 1:
            enhanced = cv2.GaussianBlur(enhanced, (gaussian, gaussian), 0)
        binary_type = cv2.THRESH_BINARY_INV if self.config.invert else cv2.THRESH_BINARY
        if self.config.threshold_mode == "adaptive":
            block_size = self._odd_kernel(self.config.adaptive_block_size, minimum=3)
            mask = cv2.adaptiveThreshold(
                enhanced,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                binary_type,
                block_size,
                float(self.config.adaptive_c),
            )
        else:
            _, mask = cv2.threshold(
                enhanced,
                int(self.config.threshold),
                255,
                binary_type,
            )
        for operation, size in (
            (cv2.MORPH_OPEN, self.config.morph_open),
            (cv2.MORPH_CLOSE, self.config.morph_close),
        ):
            kernel_size = self._odd_kernel(size)
            if kernel_size > 0:
                kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
                mask = cv2.morphologyEx(mask, operation, kernel)
        return enhanced, mask

    def _hough_circles(self, enhanced: np.ndarray) -> list[tuple[float, float, float]]:
        if not self.config.hough_enabled:
            return []
        circles = cv2.HoughCircles(
            enhanced,
            cv2.HOUGH_GRADIENT,
            dp=float(self.config.hough_dp),
            minDist=float(self.config.hough_min_dist),
            param1=float(self.config.hough_param1),
            param2=float(self.config.hough_param2),
            minRadius=int(self.config.hough_min_radius),
            maxRadius=int(self.config.hough_max_radius),
        )
        if circles is None:
            return []
        return [tuple(map(float, circle)) for circle in circles[0]]

    @staticmethod
    def _matches_hough(
        center: tuple[float, float],
        radius: float,
        circles: list[tuple[float, float, float]],
    ) -> bool:
        for hough_x, hough_y, hough_radius in circles:
            center_tolerance = max(radius, hough_radius) * 0.75
            radius_tolerance = max(radius, hough_radius) * 0.50
            if (
                math.dist(center, (hough_x, hough_y)) <= center_tolerance
                and abs(radius - hough_radius) <= radius_tolerance
            ):
                return True
        return False

    def extract_candidates(
        self,
        mask: np.ndarray,
        enhanced: np.ndarray,
        roi_offset: tuple[int, int],
    ) -> list[BallCandidate]:
        """提取轮廓指标并按配置范围过滤候选。"""

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        hough_circles = self._hough_circles(enhanced)
        candidates: list[BallCandidate] = []
        rejections = self._empty_rejection_counts()
        offset_x, offset_y = roi_offset
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if not math.isfinite(area) or not self.config.min_area_px <= area <= self.config.max_area_px:
                rejections["area"] += 1
                continue
            perimeter = float(cv2.arcLength(contour, True))
            if not math.isfinite(perimeter) or perimeter <= 0:
                rejections["circularity"] += 1
                continue
            x, y, width, height = cv2.boundingRect(contour)
            (center_x, center_y), radius = cv2.minEnclosingCircle(contour)
            equivalent_diameter = math.sqrt(4.0 * area / math.pi) if area > 0 else 0.0
            circularity = 4.0 * math.pi * area / (perimeter * perimeter)
            aspect_ratio = width / max(height, 1)
            if not math.isfinite(equivalent_diameter) or not (
                self.config.min_diameter_px
                <= equivalent_diameter
                <= self.config.max_diameter_px
            ):
                rejections["diameter"] += 1
                continue
            if not math.isfinite(circularity) or circularity < self.config.min_circularity:
                rejections["circularity"] += 1
                continue
            if not math.isfinite(aspect_ratio) or not (
                self.config.min_aspect_ratio
                <= aspect_ratio
                <= self.config.max_aspect_ratio
            ):
                rejections["aspect_ratio"] += 1
                continue
            if not all(math.isfinite(value) for value in (center_x, center_y, radius)):
                rejections["diameter"] += 1
                continue
            hough_verified = self._matches_hough(
                (center_x, center_y),
                radius,
                hough_circles,
            )
            if self.config.hough_enabled and not hough_verified:
                rejections["hough"] += 1
                continue
            global_contour = contour.copy()
            global_contour[:, :, 0] += offset_x
            global_contour[:, :, 1] += offset_y
            candidates.append(
                BallCandidate(
                    contour=global_contour,
                    area=area,
                    perimeter=perimeter,
                    bbox=(x + offset_x, y + offset_y, width, height),
                    center=(center_x + offset_x, center_y + offset_y),
                    enclosing_radius=float(radius),
                    equivalent_diameter=equivalent_diameter,
                    circularity=min(1.0, max(0.0, circularity)),
                    aspect_ratio=aspect_ratio,
                    hough_verified=hough_verified,
                )
            )
        self._rejection_counts = rejections
        return candidates

    def _size_score(self, candidate: BallCandidate) -> float:
        minimum = self.config.min_diameter_px
        maximum = self.config.max_diameter_px
        midpoint = (minimum + maximum) / 2.0
        half_span = max((maximum - minimum) / 2.0, 1.0)
        return max(0.0, 1.0 - abs(candidate.equivalent_diameter - midpoint) / half_span)

    def _choose_candidate(self, candidates: list[BallCandidate]) -> BallCandidate | None:
        if not candidates:
            return None
        if self._last_position is not None:
            nearby = [
                candidate
                for candidate in candidates
                if math.dist(candidate.center, self._last_position) <= self.config.max_jump_px
            ]
            if not nearby:
                return None
            return min(
                nearby,
                key=lambda candidate: (
                    math.dist(candidate.center, self._last_position),
                    -candidate.circularity,
                    -self._size_score(candidate),
                ),
            )
        return max(
            candidates,
            key=lambda candidate: (
                candidate.circularity,
                self._size_score(candidate),
                candidate.area,
            ),
        )

    def _confidence(self, candidate: BallCandidate) -> int:
        circularity_score = max(
            0.0,
            (candidate.circularity - self.config.min_circularity)
            / max(1.0 - self.config.min_circularity, 0.001),
        )
        aspect_center = (self.config.min_aspect_ratio + self.config.max_aspect_ratio) / 2.0
        aspect_half_span = max(
            (self.config.max_aspect_ratio - self.config.min_aspect_ratio) / 2.0,
            0.001,
        )
        aspect_score = max(
            0.0,
            1.0 - abs(candidate.aspect_ratio - aspect_center) / aspect_half_span,
        )
        stability_score = min(1.0, self._hits / max(self.config.confirm_frames, 1))
        base_score = (
            0.40 * circularity_score
            + 0.25 * self._size_score(candidate)
            + 0.15 * aspect_score
            + 0.15 * stability_score
        )
        if self.config.hough_enabled:
            confidence = 1000.0 * (
                base_score + 0.05 * float(candidate.hough_verified)
            )
        else:
            confidence = 1000.0 * base_score / 0.95
        return max(0, min(1000, round(confidence)))

    def _fx(self) -> float | None:
        calibration = self.calibration
        if calibration is None or not calibration.calibrated:
            return None
        try:
            return float(calibration.camera_matrix[0][0])
        except (IndexError, TypeError, ValueError):
            return None

    def _missing_result(
        self,
        frame: FramePacket,
        now: float,
        processing_ms: float,
    ) -> VisionResult:
        self._hits = 0
        self._misses += 1
        if self._last_position is None:
            state = TargetState.NONE
        elif self._misses < self.config.lost_frames:
            state = TargetState.OCCLUDED
        else:
            state = TargetState.LOST
            self._last_position = None
            self._last_candidate = None
        return VisionResult(
            frame.frame_id,
            frame.capture_timestamp,
            now,
            target_state=state,
            processing_delay_ms=max(0, round(processing_ms)),
            image_width=frame.image.shape[1],
            image_height=frame.image.shape[0],
        )

    def process(self, frame: FramePacket) -> VisionResult:
        """检测一帧钢球并返回统一 VisionResult，不修改输入图像。"""

        image = frame.image
        if not isinstance(image, np.ndarray) or image.size == 0:
            raise ValueError("钢球检测输入图像为空")
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("钢球检测输入必须是 BGR 三通道图像")
        process_start = time.monotonic()
        roi_x, roi_y, roi_width, roi_height = self._resolve_roi(image)
        roi_image = image[roi_y : roi_y + roi_height, roi_x : roi_x + roi_width]
        enhanced, mask = self.preprocess(roi_image)
        candidates = self.extract_candidates(mask, enhanced, (roi_x, roi_y))
        selected = self._choose_candidate(candidates)
        now = time.monotonic()
        processing_ms = (now - process_start) * 1000.0
        self._debug = SteelBallDebugData(
            enhanced=enhanced.copy(),
            mask=mask.copy(),
            candidate_count=len(candidates),
            processing_ms=processing_ms,
            selected_diameter_px=(selected.equivalent_diameter if selected else None),
            selected_circularity=(selected.circularity if selected else None),
            selected_aspect_ratio=(selected.aspect_ratio if selected else None),
            selected_area_px=(selected.area if selected else None),
            selected_hough_verified=(selected.hough_verified if selected else None),
            rejected_by_area=self._rejection_counts["area"],
            rejected_by_diameter=self._rejection_counts["diameter"],
            rejected_by_circularity=self._rejection_counts["circularity"],
            rejected_by_aspect_ratio=self._rejection_counts["aspect_ratio"],
            rejected_by_hough=self._rejection_counts["hough"],
        )
        if selected is None:
            return self._missing_result(frame, now, processing_ms)
        self._hits += 1
        self._misses = 0
        self._last_position = selected.center
        self._last_candidate = selected
        state = (
            TargetState.LOCKED
            if self._hits >= self.config.confirm_frames
            else TargetState.CANDIDATE
        )
        center_x = round(selected.center[0])
        center_y = round(selected.center[1])
        bbox_x, bbox_y, bbox_width, bbox_height = selected.bbox
        distance = estimate_distance_mm(
            self._fx(),
            self.config.known_diameter_mm,
            selected.equivalent_diameter,
        )
        return VisionResult(
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            process_timestamp=now,
            found=True,
            target_state=state,
            target_class=self.target_class,
            center_x=center_x,
            center_y=center_y,
            error_x_px=center_x - image.shape[1] // 2,
            error_y_px=center_y - image.shape[0] // 2,
            bbox_x=bbox_x,
            bbox_y=bbox_y,
            bbox_width=bbox_width,
            bbox_height=bbox_height,
            area_px=selected.area,
            distance_mm=distance,
            confidence=self._confidence(selected),
            processing_delay_ms=max(0, round(processing_ms)),
            image_width=image.shape[1],
            image_height=image.shape[0],
        )

    def get_debug_data(self) -> SteelBallDebugData | None:
        """返回最近增强图、掩膜、候选数和耗时的安全副本。"""

        if self._debug is None:
            return None
        return SteelBallDebugData(
            enhanced=self._debug.enhanced.copy(),
            mask=self._debug.mask.copy(),
            candidate_count=self._debug.candidate_count,
            processing_ms=self._debug.processing_ms,
            selected_diameter_px=self._debug.selected_diameter_px,
            selected_circularity=self._debug.selected_circularity,
            selected_aspect_ratio=self._debug.selected_aspect_ratio,
            selected_area_px=self._debug.selected_area_px,
            selected_hough_verified=self._debug.selected_hough_verified,
            rejected_by_area=self._debug.rejected_by_area,
            rejected_by_diameter=self._debug.rejected_by_diameter,
            rejected_by_circularity=self._debug.rejected_by_circularity,
            rejected_by_aspect_ratio=self._debug.rejected_by_aspect_ratio,
            rejected_by_hough=self._debug.rejected_by_hough,
        )

    def draw_debug(self, image: np.ndarray, result: VisionResult) -> np.ndarray:
        """绘制 ROI、外接圆、中心、直径、圆度、置信度和状态。"""

        output = image.copy()
        roi_x, roi_y, roi_width, roi_height = self._resolve_roi(image)
        cv2.rectangle(
            output,
            (roi_x, roi_y),
            (roi_x + roi_width - 1, roi_y + roi_height - 1),
            (255, 180, 0),
            1,
        )
        candidate = self._last_candidate if result.found else None
        if candidate is not None:
            center = (round(candidate.center[0]), round(candidate.center[1]))
            cv2.circle(output, center, round(candidate.enclosing_radius), (0, 255, 0), 2)
            cv2.drawMarker(output, center, (0, 255, 255), cv2.MARKER_CROSS, 14, 2)
            diameter_text = f"diameter={candidate.equivalent_diameter:.1f}px"
            circularity_text = f"circularity={candidate.circularity:.3f}"
        else:
            diameter_text = "diameter=--"
            circularity_text = "circularity=--"
        try:
            state_name = TargetState(result.target_state).name
        except ValueError:
            state_name = str(result.target_state)
        lines = (
            diameter_text,
            circularity_text,
            f"confidence={result.confidence} state={state_name}",
            f"distance={result.distance_mm if result.distance_mm != 0xFFFF else '--'} mm",
            (
                "reject "
                f"area={self._debug.rejected_by_area if self._debug else 0} "
                f"diam={self._debug.rejected_by_diameter if self._debug else 0} "
                f"circ={self._debug.rejected_by_circularity if self._debug else 0} "
                f"aspect={self._debug.rejected_by_aspect_ratio if self._debug else 0} "
                f"hough={self._debug.rejected_by_hough if self._debug else 0}"
            ),
        )
        for index, text in enumerate(lines):
            cv2.putText(
                output,
                text,
                (12, 26 + index * 24),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
        return output
