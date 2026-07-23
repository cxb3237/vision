"""基于多模板匹配与多帧投票的单个印刷数字 0～9 检测器。"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import time

import cv2
import numpy as np

from core.config_loader import resolve_config_path
from core.models import DigitConfig, FramePacket, TargetState, VisionResult
from detectors.base_detector import BaseDetector


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DigitCandidate:
    contour: np.ndarray
    bbox: tuple[int, int, int, int]
    area: float
    aspect_ratio: float
    center: tuple[float, float]


@dataclass(frozen=True, slots=True)
class DigitDebugData:
    enhanced: np.ndarray
    mask: np.ndarray
    normalized_digit: np.ndarray | None
    candidate_bbox: tuple[int, int, int, int] | None
    raw_digit: int | None
    candidate_digit: int | None
    locked_digit: int | None
    detected_digit: int | None
    voted_digit: int | None
    best_score: float
    second_score: float
    score_margin: float
    digit_scores: dict[int, float]
    recent_votes: tuple[int | None, ...]
    processing_ms: float


def normalize_binary_digit(binary: np.ndarray, normalization: dict) -> np.ndarray:
    """保持长宽比将白色前景数字缩放、填充并可选按矩居中。"""

    width = int(normalization["width"])
    height = int(normalization["height"])
    padding = int(normalization["padding_px"])
    canvas = np.zeros((height, width), dtype=np.uint8)
    source = np.asarray(binary, dtype=np.uint8)
    if source.ndim == 3:
        source = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    source = np.where(source > 0, 255, 0).astype(np.uint8)
    points = cv2.findNonZero(source)
    if points is None:
        return canvas
    x, y, source_width, source_height = cv2.boundingRect(points)
    cropped = source[y : y + source_height, x : x + source_width]
    available_width = width - 2 * padding
    available_height = height - 2 * padding
    scale = min(available_width / source_width, available_height / source_height)
    resized_width = max(1, min(available_width, round(source_width * scale)))
    resized_height = max(1, min(available_height, round(source_height * scale)))
    resized = cv2.resize(
        cropped,
        (resized_width, resized_height),
        interpolation=cv2.INTER_NEAREST,
    )
    left = (width - resized_width) // 2
    top = (height - resized_height) // 2
    canvas[top : top + resized_height, left : left + resized_width] = resized
    if normalization.get("center_by_moments", False):
        moments = cv2.moments(canvas, binaryImage=True)
        if moments["m00"] > 0:
            center_x = moments["m10"] / moments["m00"]
            center_y = moments["m01"] / moments["m00"]
            transform = np.float32(
                [[1, 0, width / 2.0 - center_x], [0, 1, height / 2.0 - center_y]]
            )
            canvas = cv2.warpAffine(
                canvas,
                transform,
                (width, height),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
    return canvas


class DigitDetector(BaseDetector):
    """识别单个数字，并在最近帧窗口中投票确认、丢失和重新捕获。"""

    target_class = 0

    def __init__(
        self,
        config: DigitConfig,
        require_complete_templates: bool = True,
    ) -> None:
        self.config = config
        self.require_complete_templates = require_complete_templates
        self.templates: dict[int, list[np.ndarray]] = {digit: [] for digit in range(10)}
        pixel_count = int(config.normalization["width"]) * int(config.normalization["height"])
        self._template_labels = np.empty(0, dtype=np.int16)
        self._template_binary_matrix = np.empty((0, pixel_count), dtype=np.float32)
        self._template_foreground_counts = np.empty(0, dtype=np.float32)
        self._template_normalized_matrix = np.empty((0, pixel_count), dtype=np.float32)
        self._debug: DigitDebugData | None = None
        self.load_templates()
        self.reset()

    def initialize(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._votes: deque[int | None] = deque(
            maxlen=int(self.config.tracking["vote_window"])
        )
        self._candidate_digit: int | None = None
        self._locked_digit: int | None = None
        self._candidate_hits = 0
        self._misses = 0

    def load_templates(self) -> None:
        """加载每个数字目录下的全部非 raw 模板并统一归一化。"""

        pixel_count = int(self.config.normalization["width"]) * int(
            self.config.normalization["height"]
        )
        self._template_labels = np.empty(0, dtype=np.int16)
        self._template_binary_matrix = np.empty((0, pixel_count), dtype=np.float32)
        self._template_foreground_counts = np.empty(0, dtype=np.float32)
        self._template_normalized_matrix = np.empty((0, pixel_count), dtype=np.float32)
        root = resolve_config_path(self.config.matching["template_root"])
        if not root.is_dir():
            raise FileNotFoundError(f"数字模板根目录不存在: {root}")
        loaded: dict[int, list[np.ndarray]] = {digit: [] for digit in range(10)}
        for digit in range(10):
            directory = root / str(digit)
            if not directory.is_dir():
                raise FileNotFoundError(f"数字 {digit} 模板目录不存在: {directory}")
            for path in sorted(directory.iterdir()):
                if (
                    not path.is_file()
                    or path.suffix.lower() not in IMAGE_EXTENSIONS
                    or path.stem.endswith("_raw")
                ):
                    continue
                image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if image is None:
                    LOG.warning("无法读取数字模板文件: %s", path)
                    continue
                _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                if np.count_nonzero(binary) > binary.size / 2:
                    binary = cv2.bitwise_not(binary)
                loaded[digit].append(normalize_binary_digit(binary, self.config.normalization))
        self.templates = loaded
        missing = [digit for digit, templates in loaded.items() if not templates]
        if self.require_complete_templates and missing:
            raise ValueError("缺少数字模板: " + ", ".join(map(str, missing)))
        labels: list[int] = []
        flattened: list[np.ndarray] = []
        for digit, templates in loaded.items():
            for template in templates:
                labels.append(digit)
                flattened.append((template.reshape(-1) > 0).astype(np.float32))
        if not flattened:
            return
        binary_matrix = np.stack(flattened).astype(np.float32, copy=False)
        centered = binary_matrix - binary_matrix.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(centered, axis=1, keepdims=True)
        normalized_matrix = np.divide(
            centered,
            norms,
            out=np.zeros_like(centered, dtype=np.float32),
            where=norms > np.finfo(np.float32).eps,
        )
        self._template_labels = np.asarray(labels, dtype=np.int16)
        self._template_binary_matrix = binary_matrix
        self._template_foreground_counts = binary_matrix.sum(axis=1, dtype=np.float32)
        self._template_normalized_matrix = normalized_matrix

    def _resolve_roi(self, image: np.ndarray) -> tuple[int, int, int, int]:
        image_height, image_width = image.shape[:2]
        roi = self.config.roi
        if not roi["enabled"]:
            return 0, 0, image_width, image_height
        x = min(max(0, int(roi["x"])), image_width - 1)
        y = min(max(0, int(roi["y"])), image_height - 1)
        width = min(int(roi["width"]), image_width - x)
        height = min(int(roi["height"]), image_height - y)
        if width <= 0 or height <= 0:
            raise ValueError("数字 ROI 与输入图像没有有效交集")
        return x, y, width, height

    @staticmethod
    def _odd_or_zero(value: int, minimum: int = 1) -> int:
        if value <= 0:
            return 0
        value = max(minimum, int(value))
        return value if value % 2 else value + 1

    def preprocess(self, roi_image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """执行灰度增强、三种阈值方式及形态学处理。"""

        gray = cv2.cvtColor(roi_image, cv2.COLOR_BGR2GRAY)
        enhanced = gray
        settings = self.config.preprocess
        if settings["use_clahe"]:
            enhanced = cv2.createCLAHE(
                clipLimit=float(settings["clahe_clip_limit"]),
                tileGridSize=(8, 8),
            ).apply(enhanced)
        gaussian = self._odd_or_zero(int(settings["gaussian_kernel"]))
        if gaussian > 1:
            enhanced = cv2.GaussianBlur(enhanced, (gaussian, gaussian), 0)
        binary_type = cv2.THRESH_BINARY_INV if settings["invert"] else cv2.THRESH_BINARY
        mode = settings["threshold_mode"]
        if mode == "adaptive":
            block_size = self._odd_or_zero(int(settings["adaptive_block_size"]), 3)
            mask = cv2.adaptiveThreshold(
                enhanced,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                binary_type,
                block_size,
                float(settings["adaptive_c"]),
            )
        else:
            threshold_flags = binary_type | (cv2.THRESH_OTSU if mode == "otsu" else 0)
            threshold_value = 0 if mode == "otsu" else int(settings["fixed_threshold"])
            _, mask = cv2.threshold(enhanced, threshold_value, 255, threshold_flags)
        for operation, key in (
            (cv2.MORPH_OPEN, "morph_open"),
            (cv2.MORPH_CLOSE, "morph_close"),
        ):
            kernel_size = self._odd_or_zero(int(settings[key]))
            if kernel_size > 1:
                kernel = np.ones((kernel_size, kernel_size), np.uint8)
                mask = cv2.morphologyEx(mask, operation, kernel)
        return enhanced, mask

    def find_candidates(
        self,
        mask: np.ndarray,
        roi_offset: tuple[int, int] = (0, 0),
    ) -> list[DigitCandidate]:
        """按面积、宽高比、高度与 ROI 边界距离过滤外部轮廓。"""

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        settings = self.config.candidate
        margin = int(settings["border_margin_px"])
        mask_height, mask_width = mask.shape[:2]
        offset_x, offset_y = roi_offset
        candidates: list[DigitCandidate] = []
        for contour in contours:
            area = float(cv2.contourArea(contour))
            x, y, width, height = cv2.boundingRect(contour)
            aspect_ratio = width / max(height, 1)
            if not settings["min_area_px"] <= area <= settings["max_area_px"]:
                continue
            if not settings["min_aspect_ratio"] <= aspect_ratio <= settings["max_aspect_ratio"]:
                continue
            if height < settings["min_height_px"]:
                continue
            if (
                x < margin
                or y < margin
                or x + width > mask_width - margin
                or y + height > mask_height - margin
            ):
                continue
            global_contour = contour.copy()
            global_contour[:, :, 0] += offset_x
            global_contour[:, :, 1] += offset_y
            candidates.append(
                DigitCandidate(
                    contour=global_contour,
                    bbox=(x + offset_x, y + offset_y, width, height),
                    area=area,
                    aspect_ratio=aspect_ratio,
                    center=(x + offset_x + width / 2.0, y + offset_y + height / 2.0),
                )
            )
        return candidates

    @staticmethod
    def _select_candidate(
        candidates: list[DigitCandidate],
        roi: tuple[int, int, int, int],
    ) -> DigitCandidate | None:
        if not candidates:
            return None
        roi_x, roi_y, roi_width, roi_height = roi
        roi_center = (roi_x + roi_width / 2.0, roi_y + roi_height / 2.0)
        maximum_area = max(candidate.area for candidate in candidates)
        maximum_distance = max(math.hypot(roi_width, roi_height) / 2.0, 1.0)

        def combined_score(candidate: DigitCandidate) -> float:
            area_score = max(0.0, min(1.0, candidate.area / maximum_area))
            distance = math.dist(candidate.center, roi_center)
            center_score = max(0.0, 1.0 - distance / maximum_distance)
            return 0.45 * area_score + 0.55 * center_score * math.sqrt(area_score)

        return max(candidates, key=combined_score)

    def normalize_candidate(
        self,
        mask: np.ndarray,
        candidate: DigitCandidate,
        roi_offset: tuple[int, int],
    ) -> np.ndarray:
        offset_x, offset_y = roi_offset
        x, y, width, height = candidate.bbox
        local_x = x - offset_x
        local_y = y - offset_y
        cropped = mask[local_y : local_y + height, local_x : local_x + width]
        return normalize_binary_digit(cropped, self.config.normalization)

    @staticmethod
    def _template_score(candidate: np.ndarray, template: np.ndarray) -> tuple[float, float]:
        """标量参考实现；正式评分使用预计算矩阵批处理。"""

        candidate_foreground = (candidate.reshape(-1) > 0).astype(np.float32)
        template_foreground = (template.reshape(-1) > 0).astype(np.float32)
        intersection = float(np.dot(candidate_foreground, template_foreground))
        union = float(
            candidate_foreground.sum(dtype=np.float32)
            + template_foreground.sum(dtype=np.float32)
            - intersection
        )
        iou = intersection / union if union else 0.0
        candidate_centered = candidate_foreground - candidate_foreground.mean()
        template_centered = template_foreground - template_foreground.mean()
        denominator = float(
            np.linalg.norm(candidate_centered) * np.linalg.norm(template_centered)
        )
        correlation = (
            float(np.dot(candidate_centered, template_centered) / denominator)
            if denominator > np.finfo(np.float32).eps
            else 0.0
        )
        if not math.isfinite(iou) or not math.isfinite(correlation):
            iou = 0.0 if not math.isfinite(iou) else iou
            correlation = 0.0
        return iou, max(0.0, min(1.0, correlation))

    def score_digit(self, normalized: np.ndarray) -> dict[int, float]:
        """用 NumPy 矩阵运算批量计算全部模板并返回各数字最佳分。"""

        iou_weight = float(self.config.matching["iou_weight"])
        correlation_weight = float(self.config.matching["correlation_weight"])
        weight_sum = iou_weight + correlation_weight
        scores = {digit: 0.0 for digit in range(10)}
        if self._template_labels.size == 0:
            return scores
        candidate = (normalized.reshape(-1) > 0).astype(np.float32)
        if candidate.shape[0] != self._template_binary_matrix.shape[1]:
            raise ValueError(
                "归一化数字尺寸与模板不一致: "
                f"candidate={candidate.shape[0]}, template={self._template_binary_matrix.shape[1]}"
            )
        intersections = self._template_binary_matrix @ candidate
        candidate_foreground_count = float(candidate.sum(dtype=np.float32))
        unions = (
            self._template_foreground_counts
            + candidate_foreground_count
            - intersections
        )
        ious = np.divide(
            intersections,
            unions,
            out=np.zeros_like(intersections, dtype=np.float32),
            where=unions > 0,
        )
        candidate_centered = candidate - candidate.mean(dtype=np.float32)
        candidate_norm = float(np.linalg.norm(candidate_centered))
        if candidate_norm > np.finfo(np.float32).eps:
            candidate_unit = candidate_centered / candidate_norm
            correlations = self._template_normalized_matrix @ candidate_unit
        else:
            correlations = np.zeros_like(intersections, dtype=np.float32)
        correlations = np.nan_to_num(correlations, nan=0.0, posinf=0.0, neginf=0.0)
        correlations = np.clip(correlations, 0.0, 1.0)
        template_scores = (
            iou_weight * ious + correlation_weight * correlations
        ) / weight_sum
        template_scores = np.nan_to_num(template_scores, nan=0.0, posinf=0.0, neginf=0.0)
        for digit in range(10):
            matches = template_scores[self._template_labels == digit]
            if matches.size:
                scores[digit] = float(matches.max())
        return scores

    def classify_digit(
        self,
        normalized: np.ndarray,
    ) -> tuple[int | None, float, float, float, dict[int, float]]:
        scores = self.score_digit(normalized)
        ranking = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        best_digit, best_score = ranking[0]
        second_score = ranking[1][1]
        margin = best_score - second_score
        if (
            best_score < self.config.matching["min_score"]
            or margin < self.config.matching["min_score_margin"]
        ):
            return None, best_score, second_score, margin, scores
        return best_digit, best_score, second_score, margin, scores

    def _vote_winner(self) -> int | None:
        valid_votes = [digit for digit in self._votes if digit is not None]
        if not valid_votes:
            return None
        counts = Counter(valid_votes)
        return max(
            counts,
            key=lambda digit: (
                counts[digit],
                max(i for i, value in enumerate(self._votes) if value == digit),
            ),
        )

    def _update_recognized_state(self, raw_digit: int) -> TargetState:
        """用当前单帧类别推进候选；旧锁定类别绝不替代当前 bbox。"""

        self._votes.append(raw_digit)
        self._misses = 0
        if raw_digit == self._locked_digit:
            self._candidate_digit = raw_digit
            self._candidate_hits = int(self.config.tracking["confirm_frames"])
            return TargetState.LOCKED
        if raw_digit != self._candidate_digit:
            self._candidate_digit = raw_digit
            self._candidate_hits = 1
        else:
            self._candidate_hits += 1
        if (
            self._candidate_hits >= self.config.tracking["confirm_frames"]
            and self._vote_winner() == raw_digit
        ):
            self._locked_digit = raw_digit
            return TargetState.LOCKED
        return TargetState.CANDIDATE

    def _update_missing_state(self) -> TargetState:
        """将丢失作为投票窗口中的一帧，并在 LOST 时清空全部类别状态。"""

        had_target = self._candidate_digit is not None or self._locked_digit is not None
        self._votes.append(None)
        self._candidate_digit = None
        self._candidate_hits = 0
        self._misses += 1
        if not had_target:
            return TargetState.NONE
        if self._misses < self.config.tracking["lost_frames"]:
            return TargetState.OCCLUDED
        self._locked_digit = None
        self._votes.clear()
        return TargetState.LOST

    def _missing_result(
        self,
        frame: FramePacket,
        process_time: float,
        processing_ms: float,
        state: TargetState,
    ) -> VisionResult:
        return VisionResult(
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            process_timestamp=process_time,
            target_state=state,
            processing_delay_ms=max(0, round(processing_ms)),
            image_width=frame.image.shape[1],
            image_height=frame.image.shape[0],
        )

    def process(self, frame: FramePacket) -> VisionResult:
        """处理一帧且不修改输入图像。"""

        image = frame.image
        if not isinstance(image, np.ndarray) or image.size == 0:
            raise ValueError("数字检测输入图像为空")
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("数字检测输入必须是 BGR 三通道图像")
        started = time.monotonic()
        roi_x, roi_y, roi_width, roi_height = self._resolve_roi(image)
        roi_image = image[roi_y : roi_y + roi_height, roi_x : roi_x + roi_width]
        enhanced, mask = self.preprocess(roi_image)
        candidates = self.find_candidates(mask, (roi_x, roi_y))
        candidate = self._select_candidate(candidates, (roi_x, roi_y, roi_width, roi_height))
        normalized = None
        raw_digit = None
        best_score = 0.0
        second_score = 0.0
        margin = 0.0
        scores = {digit: 0.0 for digit in range(10)}
        if candidate is not None:
            normalized = self.normalize_candidate(mask, candidate, (roi_x, roi_y))
            raw_digit, best_score, second_score, margin, scores = self.classify_digit(
                normalized
            )
        process_time = time.monotonic()
        processing_ms = (process_time - started) * 1000.0
        if raw_digit is None or candidate is None:
            state = self._update_missing_state()
        else:
            state = self._update_recognized_state(raw_digit)
        voted_digit = self._vote_winner()
        self._debug = DigitDebugData(
            enhanced=enhanced.copy(),
            mask=mask.copy(),
            normalized_digit=normalized.copy() if normalized is not None else None,
            candidate_bbox=candidate.bbox if candidate is not None else None,
            raw_digit=raw_digit,
            candidate_digit=self._candidate_digit,
            locked_digit=self._locked_digit,
            detected_digit=raw_digit,
            voted_digit=voted_digit,
            best_score=best_score,
            second_score=second_score,
            score_margin=margin,
            digit_scores=dict(scores),
            recent_votes=tuple(self._votes),
            processing_ms=processing_ms,
        )
        if raw_digit is None or candidate is None:
            return self._missing_result(frame, process_time, processing_ms, state)

        x, y, width, height = candidate.bbox
        center_x = round(candidate.center[0])
        center_y = round(candidate.center[1])
        return VisionResult(
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            process_timestamp=process_time,
            found=True,
            target_state=state,
            target_class=100 + raw_digit,
            center_x=center_x,
            center_y=center_y,
            error_x_px=center_x - image.shape[1] // 2,
            error_y_px=center_y - image.shape[0] // 2,
            bbox_x=x,
            bbox_y=y,
            bbox_width=width,
            bbox_height=height,
            area_px=candidate.area,
            confidence=max(0, min(1000, round(scores[raw_digit] * 1000))),
            processing_delay_ms=max(0, round(processing_ms)),
            image_width=image.shape[1],
            image_height=image.shape[0],
        )

    def get_debug_data(self) -> DigitDebugData | None:
        if self._debug is None:
            return None
        return DigitDebugData(
            enhanced=self._debug.enhanced.copy(),
            mask=self._debug.mask.copy(),
            normalized_digit=(
                self._debug.normalized_digit.copy()
                if self._debug.normalized_digit is not None
                else None
            ),
            candidate_bbox=self._debug.candidate_bbox,
            raw_digit=self._debug.raw_digit,
            candidate_digit=self._debug.candidate_digit,
            locked_digit=self._debug.locked_digit,
            detected_digit=self._debug.detected_digit,
            voted_digit=self._debug.voted_digit,
            best_score=self._debug.best_score,
            second_score=self._debug.second_score,
            score_margin=self._debug.score_margin,
            digit_scores=dict(self._debug.digit_scores),
            recent_votes=self._debug.recent_votes,
            processing_ms=self._debug.processing_ms,
        )

    def draw_debug(self, image: np.ndarray, result: VisionResult) -> np.ndarray:
        output = image.copy()
        roi_x, roi_y, roi_width, roi_height = self._resolve_roi(image)
        cv2.rectangle(
            output,
            (roi_x, roi_y),
            (roi_x + roi_width - 1, roi_y + roi_height - 1),
            (255, 160, 0),
            1,
        )
        debug = self._debug
        if debug is None:
            return output
        if debug.candidate_bbox is not None:
            x, y, width, height = debug.candidate_bbox
            cv2.rectangle(output, (x, y), (x + width, y + height), (0, 255, 0), 2)
        try:
            state = TargetState(result.target_state).name
        except ValueError:
            state = str(result.target_state)
        lines = (
            f"raw={debug.raw_digit if debug.raw_digit is not None else '--'} "
            f"candidate={debug.candidate_digit if debug.candidate_digit is not None else '--'} "
            f"locked={debug.locked_digit if debug.locked_digit is not None else '--'}",
            f"best={debug.best_score:.3f} second={debug.second_score:.3f} "
            f"margin={debug.score_margin:.3f}",
            f"state={state} votes={list(debug.recent_votes)}",
        )
        for index, text in enumerate(lines):
            cv2.putText(
                output,
                text,
                (12, 26 + 24 * index),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
        if debug.normalized_digit is not None:
            thumbnail = cv2.cvtColor(debug.normalized_digit, cv2.COLOR_GRAY2BGR)
            thumb_height, thumb_width = thumbnail.shape[:2]
            x = max(0, output.shape[1] - thumb_width - 8)
            y = 8
            visible_height = min(thumb_height, output.shape[0] - y)
            visible_width = min(thumb_width, output.shape[1] - x)
            if visible_height > 0 and visible_width > 0:
                output[y : y + visible_height, x : x + visible_width] = thumbnail[
                    :visible_height, :visible_width
                ]
                cv2.rectangle(
                    output,
                    (x, y),
                    (x + visible_width - 1, y + visible_height - 1),
                    (0, 255, 255),
                    1,
                )
        return output
