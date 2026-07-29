"""Formal steel-ball detector backed by the lightweight NCNN runtime."""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Sequence

import cv2
import numpy as np

from core.models import FramePacket, SteelBallNcnnConfig, TargetState, VisionResult
from core.performance_metrics import RollingSamples
from detectors.base_detector import BaseDetector
from inference.steel_ball_ncnn_runtime import SteelBallNcnnRuntime


LOG = logging.getLogger(__name__)
ERROR_LOG_INTERVAL_S = 5.0


def _valid_detection(
    detection: dict[str, Any], image_width: int, image_height: int
) -> dict[str, Any] | None:
    """Return a clipped detection or None; never trust backend coordinates blindly."""

    try:
        confidence = float(detection["confidence"])
        x1 = int(round(float(detection["x1"])))
        y1 = int(round(float(detection["y1"])))
        x2 = int(round(float(detection["x2"])))
        y2 = int(round(float(detection["y2"])))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return None
    x1 = min(max(x1, 0), image_width)
    y1 = min(max(y1, 0), image_height)
    x2 = min(max(x2, 0), image_width)
    y2 = min(max(y2, 0), image_height)
    if x2 <= x1 or y2 <= y1:
        return None
    center_x = int(round((x1 + x2) / 2.0))
    center_y = int(round((y1 + y2) / 2.0))
    return {
        "class_id": 0,
        "class_name": "steel_ball",
        "confidence": confidence,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "center_x": center_x,
        "center_y": center_y,
        "width": x2 - x1,
        "height": y2 - y1,
        "area": (x2 - x1) * (y2 - y1),
    }


def select_primary_detection(
    detections: Sequence[dict[str, Any]],
    image_width: int,
    image_height: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Select one target deterministically while retaining every valid box for preview."""

    valid = [
        candidate
        for detection in detections
        if (candidate := _valid_detection(detection, image_width, image_height)) is not None
    ]
    if not valid:
        return None, []
    best_confidence = max(item["confidence"] for item in valid)
    confidence_group = [
        item for item in valid if best_confidence - item["confidence"] < 0.05
    ]
    image_center_x = image_width / 2.0
    image_center_y = image_height / 2.0
    selected = min(
        confidence_group,
        key=lambda item: (
            -item["area"],
            math.hypot(
                item["center_x"] - image_center_x,
                item["center_y"] - image_center_y,
            ),
            item["x1"],
            item["y1"],
        ),
    )
    return selected, valid


class SteelBallYoloNcnnDetector(BaseDetector):
    """Single-frame NCNN candidate detector; temporal state belongs to TargetTracker."""

    backend_name = "ncnn"

    def __init__(
        self,
        config: SteelBallNcnnConfig,
        *,
        runtime_factory: Callable[..., Any] = SteelBallNcnnRuntime,
    ) -> None:
        if config.backend != "ncnn":
            raise ValueError("SteelBallYoloNcnnDetector requires backend=ncnn")
        self.config = config
        self.target_class = int(config.target_class)
        self.runtime = runtime_factory(
            model_dir=config.model_path,
            imgsz=config.imgsz,
            conf_threshold=config.conf_threshold,
            iou_threshold=config.iou_threshold,
            max_det=config.max_det,
            num_threads=config.num_threads,
        )
        self.model_loaded = False
        self.detector_error = ""
        self._initialized = False
        self._closed = False
        self._shape_logged = False
        self._last_error_log_at = float("-inf")
        self._detections: list[dict[str, Any]] = []
        self._selected: dict[str, Any] | None = None
        self._last_timings = {
            "preprocess": 0.0,
            "inference": 0.0,
            "postprocess": 0.0,
            "total": 0.0,
        }
        self._timing_history = {
            name: RollingSamples(max_samples=120)
            for name in ("preprocess", "inference", "postprocess", "total")
        }

    def initialize(self) -> None:
        """Load the one NCNN model once; repeated initialize calls are harmless."""

        if self._initialized and self.model_loaded:
            return
        LOG.info("Loading steel-ball YOLO-NCNN model: %s", self.config.model_path)
        try:
            self.runtime.load()
            names = getattr(self.runtime, "class_names", {0: "steel_ball"})
            if names.get(0) != "steel_ball":
                raise RuntimeError(f"NCNN class 0 must be steel_ball, got {names.get(0)!r}")
        except Exception as exc:
            self.model_loaded = False
            self.detector_error = str(exc)
            LOG.error("Steel-ball YOLO-NCNN model load failed: %s", exc)
            self._initialized = True
            return
        self.model_loaded = True
        self.detector_error = ""
        self._initialized = True
        self._closed = False
        LOG.info("Steel-ball YOLO-NCNN model loaded: %s", self.config.model_path)

    def reset(self) -> None:
        """Clear per-frame debug state; model ownership and tracking stay separate."""

        self._detections = []
        self._selected = None
        self.detector_error = ""

    def close(self) -> None:
        if self._closed:
            return
        self.runtime.close()
        self.model_loaded = False
        self._closed = True

    @staticmethod
    def _empty_result(frame: FramePacket, process_timestamp: float) -> VisionResult:
        image = frame.image
        height = int(image.shape[0]) if isinstance(image, np.ndarray) and image.ndim >= 2 else 0
        width = int(image.shape[1]) if isinstance(image, np.ndarray) and image.ndim >= 2 else 0
        return VisionResult(
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            process_timestamp=process_timestamp,
            found=False,
            target_state=TargetState.NONE,
            image_width=width,
            image_height=height,
        )

    def _record_error(self, message: str, exc: Exception | None = None) -> None:
        self.detector_error = message
        now = time.monotonic()
        if now - self._last_error_log_at < ERROR_LOG_INTERVAL_S:
            return
        self._last_error_log_at = now
        if exc is None:
            LOG.warning("Steel-ball YOLO-NCNN inference unavailable: %s", message)
        else:
            LOG.warning(
                "Steel-ball YOLO-NCNN inference failed: %s", message, exc_info=exc
            )

    def process(self, frame: FramePacket) -> VisionResult:
        """Infer current BGR frame only; no camera, queue, tracker or UART access."""

        image = frame.image
        now = time.monotonic()
        if (
            not isinstance(image, np.ndarray)
            or image.size == 0
            or image.ndim != 3
            or image.shape[2] != 3
        ):
            self._detections = []
            self._selected = None
            self._record_error("empty or invalid BGR input frame")
            return self._empty_result(frame, now)
        if not self.model_loaded:
            self._detections = []
            self._selected = None
            self._record_error(self.detector_error or "NCNN model is not loaded")
            return self._empty_result(frame, now)

        try:
            prediction = self.runtime.predict(image)
            raw_detections = prediction.get("detections", [])
            if not isinstance(raw_detections, list):
                raise ValueError("NCNN detections must be a list")
            selected, detections = select_primary_detection(
                raw_detections, image.shape[1], image.shape[0]
            )
            timings = prediction.get("timings_ms", {})
            self._last_timings = {
                name: max(0.0, float(timings.get(name, 0.0)))
                for name in ("preprocess", "inference", "postprocess", "total")
            }
            for name, value in self._last_timings.items():
                self._timing_history[name].add(value)
            self._detections = detections
            self._selected = selected
            self.detector_error = ""
            if self.config.debug_tensor_shapes and not self._shape_logged:
                LOG.info(
                    "Steel-ball NCNN shapes input=%s outputs=%s",
                    prediction.get("input_tensor_shape"),
                    prediction.get("output_tensor_shapes"),
                )
                self._shape_logged = True
        except Exception as exc:
            self._detections = []
            self._selected = None
            self._record_error(str(exc), exc)
            return self._empty_result(frame, time.monotonic())

        process_timestamp = time.monotonic()
        if selected is None:
            result = self._empty_result(frame, process_timestamp)
            result.processing_delay_ms = max(0, round(self._last_timings["total"]))
            return result

        center_x = int(selected["center_x"])
        center_y = int(selected["center_y"])
        return VisionResult(
            frame_id=frame.frame_id,
            capture_timestamp=frame.capture_timestamp,
            process_timestamp=process_timestamp,
            found=True,
            target_state=TargetState.NONE,
            target_class=self.target_class,
            center_x=center_x,
            center_y=center_y,
            error_x_px=center_x - image.shape[1] // 2,
            error_y_px=center_y - image.shape[0] // 2,
            bbox_x=int(selected["x1"]),
            bbox_y=int(selected["y1"]),
            bbox_width=int(selected["width"]),
            bbox_height=int(selected["height"]),
            area_px=float(selected["area"]),
            distance_mm=0xFFFF,
            confidence=int(np.clip(round(float(selected["confidence"]) * 1000.0), 0, 1000)),
            processing_delay_ms=max(0, round(self._last_timings["total"])),
            image_width=image.shape[1],
            image_height=image.shape[0],
        )

    def get_runtime_status(self) -> dict[str, Any]:
        summaries = {
            name: samples.summary() for name, samples in self._timing_history.items()
        }
        total_ms = self._last_timings["total"]
        return {
            "steel_ball_backend": self.backend_name,
            "model_loaded": self.model_loaded,
            "model_path": str(self.config.model_path),
            "inference_ms": round(self._last_timings["inference"], 3),
            "preprocess_ms": round(self._last_timings["preprocess"], 3),
            "postprocess_ms": round(self._last_timings["postprocess"], 3),
            "total_ms": round(total_ms, 3),
            "inference_median_ms": round(float(summaries["inference"]["median"]), 3),
            "inference_p95_ms": round(float(summaries["inference"]["p95"]), 3),
            "ncnn_total_median_ms": round(float(summaries["total"]["median"]), 3),
            "ncnn_total_p95_ms": round(float(summaries["total"]["p95"]), 3),
            "estimated_fps": round(1000.0 / total_ms, 2) if total_ms > 0.0 else 0.0,
            "detection_count": len(self._detections),
            "selected_target_confidence": (
                round(float(self._selected["confidence"]), 6) if self._selected else 0.0
            ),
            "ncnn_threads": int(self.config.num_threads),
            "detector_error": self.detector_error,
        }

    def draw_debug(self, image: np.ndarray, result: VisionResult) -> np.ndarray:
        """Draw every valid NCNN box while highlighting the selected target."""

        output = image.copy()
        selected_box = None
        if self._selected is not None:
            selected_box = (
                self._selected["x1"],
                self._selected["y1"],
                self._selected["x2"],
                self._selected["y2"],
            )
        for detection in self._detections:
            box = (
                detection["x1"],
                detection["y1"],
                detection["x2"],
                detection["y2"],
            )
            is_selected = box == selected_box
            color = (0, 255, 0) if is_selected else (0, 210, 255)
            thickness = 3 if is_selected else 2
            cv2.rectangle(output, box[:2], box[2:], color, thickness)
            center = (detection["center_x"], detection["center_y"])
            cv2.circle(output, center, 4, color, -1)
            cv2.putText(
                output,
                f"steel_ball {detection['confidence']:.3f}",
                (detection["x1"], max(18, detection["y1"] - 7)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
                cv2.LINE_AA,
            )
        status = self.get_runtime_status()
        state_name = TargetState(int(result.target_state)).name
        lines = (
            f"YOLO-NCNN model={'loaded' if self.model_loaded else 'error'}",
            f"detections={len(self._detections)} selected={status['selected_target_confidence']:.3f}",
            f"inference={status['inference_ms']:.1f}ms total={status['total_ms']:.1f}ms",
            f"state={state_name} threads={self.config.num_threads}",
        )
        for index, text in enumerate(lines):
            cv2.putText(
                output,
                text,
                (12, 26 + index * 23),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 255, 0) if not self.detector_error else (0, 0, 255),
                1,
                cv2.LINE_AA,
            )
        return output
