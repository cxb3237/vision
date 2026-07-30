"""CPU-only NCNN runtime for the exported steel-ball YOLO model.

The exported model was inspected before this runtime was implemented:

* input blob: ``in0``
* output blob: ``out0``
* input: CHW float32 RGB, 416 x 416, values normalized once to [0, 1]
* output: ``(5, 3549)`` raw Detect tensor containing ``xywh`` followed by
  the single ``steel_ball`` class score

The model does not contain confidence filtering or NMS.  This module has no
dependency on PyTorch or Ultralytics and imports ``ncnn`` lazily in ``load`` so
that ordinary unit tests can run on hosts where the NCNN wheel is unavailable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import importlib
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Sequence

import cv2
import numpy as np
import yaml


DEFAULT_NUM_THREADS = min(4, os.cpu_count() or 1)


class SteelBallNcnnError(RuntimeError):
    """Raised when the NCNN model cannot be loaded or interpreted safely."""


@dataclass(frozen=True)
class LetterboxTransform:
    """Geometry needed to map model coordinates back to the source image."""

    original_width: int
    original_height: int
    input_width: int
    input_height: int
    scale: float
    pad_left: int
    pad_top: int
    pad_right: int
    pad_bottom: int


def _target_size(imgsz: int | Sequence[int]) -> tuple[int, int]:
    if isinstance(imgsz, int):
        width = height = imgsz
    else:
        values = tuple(int(value) for value in imgsz)
        if len(values) != 2:
            raise ValueError(f"imgsz must contain two values, got {values!r}")
        height, width = values
    if width <= 0 or height <= 0:
        raise ValueError(f"imgsz must be positive, got {(height, width)!r}")
    return width, height


def letterbox(
    image_bgr: np.ndarray,
    imgsz: int | Sequence[int] = 416,
) -> tuple[np.ndarray, LetterboxTransform]:
    """Resize without distortion and pad with the NCNN export's value 114."""

    if not isinstance(image_bgr, np.ndarray) or image_bgr.size == 0:
        raise ValueError("image_bgr must be a non-empty NumPy array")
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ValueError(f"image_bgr must have shape HxWx3, got {image_bgr.shape}")

    source_height, source_width = image_bgr.shape[:2]
    if source_width <= 0 or source_height <= 0:
        raise ValueError(f"image_bgr has invalid shape {image_bgr.shape}")
    input_width, input_height = _target_size(imgsz)
    scale = min(input_width / source_width, input_height / source_height)
    resized_width = max(1, min(input_width, int(round(source_width * scale))))
    resized_height = max(1, min(input_height, int(round(source_height * scale))))

    if (resized_width, resized_height) == (source_width, source_height):
        resized = image_bgr.copy()
    else:
        resized = cv2.resize(
            image_bgr,
            (resized_width, resized_height),
            interpolation=cv2.INTER_LINEAR,
        )

    horizontal_padding = input_width - resized_width
    vertical_padding = input_height - resized_height
    pad_left = horizontal_padding // 2
    pad_right = horizontal_padding - pad_left
    pad_top = vertical_padding // 2
    pad_bottom = vertical_padding - pad_top
    padded = cv2.copyMakeBorder(
        resized,
        pad_top,
        pad_bottom,
        pad_left,
        pad_right,
        cv2.BORDER_CONSTANT,
        value=(114, 114, 114),
    )
    transform = LetterboxTransform(
        original_width=source_width,
        original_height=source_height,
        input_width=input_width,
        input_height=input_height,
        scale=scale,
        pad_left=pad_left,
        pad_top=pad_top,
        pad_right=pad_right,
        pad_bottom=pad_bottom,
    )
    return padded, transform


def prepare_input(
    image_bgr: np.ndarray,
    imgsz: int | Sequence[int] = 416,
) -> tuple[np.ndarray, LetterboxTransform]:
    """Return contiguous CHW RGB float32 input, normalized exactly once."""

    padded, transform = letterbox(image_bgr, imgsz)
    rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
    tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1), dtype=np.float32)
    tensor *= np.float32(1.0 / 255.0)
    return tensor, transform


def restore_boxes(
    boxes_xyxy: np.ndarray,
    transform: LetterboxTransform,
) -> np.ndarray:
    """Map letterbox xyxy boxes to source pixels without mutating the input."""

    boxes = np.asarray(boxes_xyxy, dtype=np.float32)
    if boxes.size == 0:
        return np.empty((0, 4), dtype=np.float32)
    if boxes.ndim != 2 or boxes.shape[1] != 4:
        raise ValueError(f"boxes_xyxy must have shape Nx4, got {boxes.shape}")
    if transform.scale <= 0:
        raise ValueError(f"letterbox scale must be positive, got {transform.scale}")

    restored = boxes.copy()
    restored[:, [0, 2]] -= float(transform.pad_left)
    restored[:, [1, 3]] -= float(transform.pad_top)
    restored /= float(transform.scale)
    restored[:, [0, 2]] = np.clip(
        restored[:, [0, 2]], 0.0, float(transform.original_width)
    )
    restored[:, [1, 3]] = np.clip(
        restored[:, [1, 3]], 0.0, float(transform.original_height)
    )
    return restored


def nms_xyxy(
    boxes_xyxy: np.ndarray,
    scores: np.ndarray,
    iou_threshold: float = 0.60,
    max_det: int = 30,
) -> np.ndarray:
    """Perform stable single-class NMS and return selected source indices."""

    boxes = np.asarray(boxes_xyxy, dtype=np.float32)
    confidences = np.asarray(scores, dtype=np.float32).reshape(-1)
    if boxes.size == 0:
        return np.empty((0,), dtype=np.int64)
    if boxes.ndim != 2 or boxes.shape[1] != 4:
        raise ValueError(f"boxes_xyxy must have shape Nx4, got {boxes.shape}")
    if len(boxes) != len(confidences):
        raise ValueError(
            f"box and score counts differ: {len(boxes)} boxes, {len(confidences)} scores"
        )
    if not 0.0 <= iou_threshold <= 1.0:
        raise ValueError(f"iou_threshold must be in [0, 1], got {iou_threshold}")
    if max_det <= 0:
        raise ValueError(f"max_det must be positive, got {max_det}")

    widths = boxes[:, 2] - boxes[:, 0]
    heights = boxes[:, 3] - boxes[:, 1]
    valid = np.isfinite(boxes).all(axis=1) & np.isfinite(confidences)
    valid &= (widths > 0.0) & (heights > 0.0)
    valid_indices = np.flatnonzero(valid)
    if valid_indices.size == 0:
        return np.empty((0,), dtype=np.int64)

    valid_boxes = boxes[valid_indices]
    valid_scores = confidences[valid_indices]
    order = np.argsort(-valid_scores, kind="stable")
    areas = np.maximum(0.0, valid_boxes[:, 2] - valid_boxes[:, 0]) * np.maximum(
        0.0, valid_boxes[:, 3] - valid_boxes[:, 1]
    )
    selected_local: list[int] = []

    while order.size and len(selected_local) < max_det:
        current = int(order[0])
        selected_local.append(current)
        if order.size == 1:
            break
        remaining = order[1:]
        x1 = np.maximum(valid_boxes[current, 0], valid_boxes[remaining, 0])
        y1 = np.maximum(valid_boxes[current, 1], valid_boxes[remaining, 1])
        x2 = np.minimum(valid_boxes[current, 2], valid_boxes[remaining, 2])
        y2 = np.minimum(valid_boxes[current, 3], valid_boxes[remaining, 3])
        intersection = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        union = areas[current] + areas[remaining] - intersection
        iou = np.divide(
            intersection,
            union,
            out=np.zeros_like(intersection, dtype=np.float32),
            where=union > 0.0,
        )
        order = remaining[iou <= iou_threshold]

    return valid_indices[np.asarray(selected_local, dtype=np.int64)]


def _output_diagnostics(outputs: Sequence[np.ndarray]) -> str:
    details: list[str] = []
    for index, value in enumerate(outputs):
        array = np.asarray(value)
        finite = array[np.isfinite(array)] if array.size else np.asarray([], dtype=np.float32)
        minimum = float(finite.min()) if finite.size else None
        maximum = float(finite.max()) if finite.size else None
        sample = array.reshape(-1)[:12].tolist()
        details.append(
            f"output[{index}]: shape={array.shape}, dtype={array.dtype}, "
            f"min={minimum}, max={maximum}, sample={sample}"
        )
    return f"output_count={len(outputs)}; " + "; ".join(details)


def decode_raw_detect_output(
    outputs: Sequence[np.ndarray],
    *,
    class_count: int,
    conf_threshold: float,
    iou_threshold: float,
    max_det: int,
    input_width: int,
    input_height: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decode the inspected raw ``(4 + nc, anchors)`` Detect output.

    Direct NCNN extraction returns ``(5, 3549)`` for the current single-class
    export.  The generated wrapper adds only a leading batch dimension, so that
    exact ``(1, 5, anchors)`` representation is accepted too.  Other layouts
    are rejected instead of being guessed.
    """

    if len(outputs) != 1:
        raise SteelBallNcnnError(
            "Unsupported NCNN output layout: " + _output_diagnostics(outputs)
        )
    if class_count <= 0:
        raise ValueError(f"class_count must be positive, got {class_count}")
    if not 0.0 <= conf_threshold <= 1.0:
        raise ValueError(f"conf_threshold must be in [0, 1], got {conf_threshold}")

    raw = np.asarray(outputs[0])
    expected_channels = 4 + class_count
    if raw.ndim == 3 and raw.shape[0] == 1:
        raw = raw[0]
    if raw.ndim != 2 or raw.shape[0] != expected_channels:
        raise SteelBallNcnnError(
            "Unsupported NCNN output layout: expected one raw Detect tensor "
            f"with shape ({expected_channels}, anchors) or "
            f"(1, {expected_channels}, anchors); {_output_diagnostics(outputs)}"
        )

    predictions = np.asarray(raw.T, dtype=np.float32)
    if predictions.size == 0:
        return (
            np.empty((0, 4), dtype=np.float32),
            np.empty((0,), dtype=np.float32),
            np.empty((0,), dtype=np.int64),
        )

    # The inspected model has class 0 == steel_ball.  Retain only that score;
    # there is no objectness term in this export.
    scores = predictions[:, 4]
    xywh = predictions[:, :4]
    finite = np.isfinite(xywh).all(axis=1) & np.isfinite(scores)
    valid = finite & (scores >= conf_threshold) & (xywh[:, 2] > 0.0) & (xywh[:, 3] > 0.0)
    xywh = xywh[valid]
    scores = scores[valid]
    if xywh.size == 0:
        return (
            np.empty((0, 4), dtype=np.float32),
            np.empty((0,), dtype=np.float32),
            np.empty((0,), dtype=np.int64),
        )

    boxes = np.empty((len(xywh), 4), dtype=np.float32)
    boxes[:, 0] = xywh[:, 0] - xywh[:, 2] * 0.5
    boxes[:, 1] = xywh[:, 1] - xywh[:, 3] * 0.5
    boxes[:, 2] = xywh[:, 0] + xywh[:, 2] * 0.5
    boxes[:, 3] = xywh[:, 1] + xywh[:, 3] * 0.5
    boxes[:, [0, 2]] = np.clip(boxes[:, [0, 2]], 0.0, float(input_width))
    boxes[:, [1, 3]] = np.clip(boxes[:, [1, 3]], 0.0, float(input_height))
    valid_box = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
    boxes = boxes[valid_box]
    scores = scores[valid_box]
    if boxes.size == 0:
        return (
            np.empty((0, 4), dtype=np.float32),
            np.empty((0,), dtype=np.float32),
            np.empty((0,), dtype=np.int64),
        )

    selected = nms_xyxy(boxes, scores, iou_threshold=iou_threshold, max_det=max_det)
    return boxes[selected], scores[selected], np.zeros(len(selected), dtype=np.int64)


def calculate_error_x_permille(center_x: float, image_width: int) -> int:
    """Return horizontal center error clamped to -1000..1000."""

    if image_width <= 0:
        raise ValueError(f"image_width must be positive, got {image_width}")
    value = ((float(center_x) - image_width / 2.0) / (image_width / 2.0)) * 1000.0
    return int(np.clip(round(value), -1000, 1000))


def detections_from_model_boxes(
    boxes_xyxy: np.ndarray,
    scores: np.ndarray,
    transform: LetterboxTransform,
) -> list[dict[str, Any]]:
    """Restore, validate and serialize detections in original-image pixels."""

    restored = restore_boxes(boxes_xyxy, transform)
    confidences = np.asarray(scores, dtype=np.float32).reshape(-1)
    if len(restored) != len(confidences):
        raise ValueError("restored box count does not match confidence count")
    detections: list[dict[str, Any]] = []
    for box, confidence in zip(restored, confidences, strict=True):
        x1 = int(np.clip(round(float(box[0])), 0, transform.original_width))
        y1 = int(np.clip(round(float(box[1])), 0, transform.original_height))
        x2 = int(np.clip(round(float(box[2])), 0, transform.original_width))
        y2 = int(np.clip(round(float(box[3])), 0, transform.original_height))
        if x2 <= x1 or y2 <= y1 or not math.isfinite(float(confidence)):
            continue
        center_x = int(round((x1 + x2) / 2.0))
        center_y = int(round((y1 + y2) / 2.0))
        detections.append(
            {
                "class_id": 0,
                "class_name": "steel_ball",
                "confidence": float(confidence),
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "center_x": center_x,
                "center_y": center_y,
                "width": x2 - x1,
                "height": y2 - y1,
                "error_x_permille": calculate_error_x_permille(
                    center_x, transform.original_width
                ),
            }
        )
    return detections


def _read_metadata(path: Path) -> tuple[dict[str, Any], dict[int, str]]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SteelBallNcnnError(f"Failed to read NCNN metadata {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SteelBallNcnnError(f"NCNN metadata must be a mapping: {path}")
    names_value = loaded.get("names")
    if isinstance(names_value, list):
        names = {index: str(name) for index, name in enumerate(names_value)}
    elif isinstance(names_value, dict):
        try:
            names = {int(index): str(name) for index, name in names_value.items()}
        except (TypeError, ValueError) as exc:
            raise SteelBallNcnnError(
                f"NCNN metadata names contains a non-integer key: {path}"
            ) from exc
    else:
        raise SteelBallNcnnError(f"NCNN metadata names must be a list or mapping: {path}")
    if names.get(0) != "steel_ball":
        raise SteelBallNcnnError(
            f"NCNN metadata class 0 must be 'steel_ball', got {names.get(0)!r}: {path}"
        )
    task = loaded.get("task")
    if task != "detect":
        raise SteelBallNcnnError(
            f"NCNN metadata task must be 'detect', got {task!r}: {path}"
        )
    return loaded, names


def _parse_param_graph(path: Path) -> tuple[list[str], list[str]]:
    """Read graph blob names from the actual NCNN param file."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SteelBallNcnnError(f"Failed to read NCNN param file {path}: {exc}") from exc

    generated: list[str] = []
    consumed: set[str] = set()
    inputs: list[str] = []
    for line in lines[2:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        layer_type = fields[0]
        try:
            bottom_count = int(fields[2])
            top_count = int(fields[3])
        except ValueError as exc:
            raise SteelBallNcnnError(f"Malformed NCNN param line in {path}: {line}") from exc
        start = 4
        bottoms = fields[start : start + bottom_count]
        tops = fields[start + bottom_count : start + bottom_count + top_count]
        consumed.update(bottoms)
        generated.extend(tops)
        if layer_type.endswith("Input"):
            inputs.extend(tops)
    outputs = [name for name in generated if name not in consumed]
    if len(inputs) != 1 or len(outputs) != 1:
        raise SteelBallNcnnError(
            f"Expected one input and one output in {path}, got inputs={inputs}, outputs={outputs}"
        )
    return inputs, outputs


def _parse_wrapper_nodes(path: Path) -> tuple[list[str], list[str]]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SteelBallNcnnError(f"Failed to read generated wrapper {path}: {exc}") from exc
    inputs = re.findall(r"\.input\(\s*[\"']([^\"']+)[\"']", source)
    outputs = re.findall(r"\.extract\(\s*[\"']([^\"']+)[\"']", source)
    return inputs, outputs


class SteelBallNcnnRuntime:
    """Reusable one-model CPU NCNN runtime for offline steel-ball images."""

    def __init__(
        self,
        model_dir: str | Path,
        imgsz: int = 416,
        conf_threshold: float = 0.40,
        iou_threshold: float = 0.60,
        max_det: int = 30,
        num_threads: int = DEFAULT_NUM_THREADS,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.imgsz = int(imgsz)
        self.conf_threshold = float(conf_threshold)
        self.iou_threshold = float(iou_threshold)
        self.max_det = int(max_det)
        self.num_threads = int(num_threads)
        if self.imgsz <= 0:
            raise ValueError(f"imgsz must be positive, got {self.imgsz}")
        if not 0.0 <= self.conf_threshold <= 1.0:
            raise ValueError("conf_threshold must be in [0, 1]")
        if not 0.0 <= self.iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be in [0, 1]")
        if self.max_det <= 0:
            raise ValueError("max_det must be positive")
        if self.num_threads <= 0:
            raise ValueError("num_threads must be positive")

        self._net: Any | None = None
        self._ncnn: Any | None = None
        self.metadata: dict[str, Any] = {}
        self.class_names: dict[int, str] = {}
        self.input_names: list[str] = []
        self.output_names: list[str] = []

    @property
    def is_loaded(self) -> bool:
        return self._net is not None

    def _model_paths(self) -> dict[str, Path]:
        return {
            "metadata": self.model_dir / "metadata.yaml",
            "param": self.model_dir / "model.ncnn.param",
            "bin": self.model_dir / "model.ncnn.bin",
            "wrapper": self.model_dir / "model_ncnn.py",
        }

    def load(self) -> None:
        """Validate and load the NCNN model exactly once."""

        if self.is_loaded:
            return
        paths = self._model_paths()
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise SteelBallNcnnError("Missing NCNN model file(s): " + ", ".join(missing))

        metadata, names = _read_metadata(paths["metadata"])
        metadata_imgsz = metadata.get("imgsz")
        if isinstance(metadata_imgsz, (list, tuple)) and len(metadata_imgsz) == 2:
            exported_height, exported_width = (int(value) for value in metadata_imgsz)
            if (exported_height, exported_width) != (self.imgsz, self.imgsz):
                raise SteelBallNcnnError(
                    f"Requested imgsz {self.imgsz} does not match exported model size "
                    f"{metadata_imgsz}: {paths['metadata']}"
                )
        else:
            raise SteelBallNcnnError(
                f"NCNN metadata imgsz must contain height and width: {paths['metadata']}"
            )

        param_inputs, param_outputs = _parse_param_graph(paths["param"])
        wrapper_inputs, wrapper_outputs = _parse_wrapper_nodes(paths["wrapper"])
        if wrapper_inputs != param_inputs or wrapper_outputs != param_outputs:
            raise SteelBallNcnnError(
                "Generated wrapper and NCNN param node names differ: "
                f"param inputs={param_inputs}, outputs={param_outputs}; "
                f"wrapper inputs={wrapper_inputs}, outputs={wrapper_outputs}; "
                f"wrapper={paths['wrapper']}"
            )

        try:
            ncnn_module = importlib.import_module("ncnn")
        except ImportError as exc:
            raise SteelBallNcnnError(
                "The 'ncnn' Python module is unavailable in the current interpreter; "
                f"model={self.model_dir}"
            ) from exc

        net = ncnn_module.Net()
        net.opt.use_vulkan_compute = False
        net.opt.num_threads = self.num_threads
        param_code = int(net.load_param(str(paths["param"])))
        if param_code != 0:
            if hasattr(net, "clear"):
                net.clear()
            raise SteelBallNcnnError(
                f"ncnn load_param failed with code {param_code}: {paths['param']}"
            )
        model_code = int(net.load_model(str(paths["bin"])))
        if model_code != 0:
            if hasattr(net, "clear"):
                net.clear()
            raise SteelBallNcnnError(
                f"ncnn load_model failed with code {model_code}: {paths['bin']}"
            )

        self.metadata = metadata
        self.class_names = names
        self.input_names = param_inputs
        self.output_names = param_outputs
        self._ncnn = ncnn_module
        self._net = net

    def _run_ncnn(self, tensor_chw: np.ndarray) -> list[np.ndarray]:
        if self._net is None or self._ncnn is None:
            raise SteelBallNcnnError("NCNN runtime is not loaded; call load() before predict()")
        extractor = self._net.create_extractor()
        input_mat = self._ncnn.Mat(tensor_chw).clone()
        input_code = int(extractor.input(self.input_names[0], input_mat))
        if input_code != 0:
            raise SteelBallNcnnError(
                f"ncnn Extractor.input failed with code {input_code}: "
                f"blob={self.input_names[0]}, shape={tensor_chw.shape}, model={self.model_dir}"
            )
        outputs: list[np.ndarray] = []
        for output_name in self.output_names:
            extract_code, output_mat = extractor.extract(output_name)
            extract_code = int(extract_code)
            if extract_code != 0:
                raise SteelBallNcnnError(
                    f"ncnn Extractor.extract failed with code {extract_code}: "
                    f"blob={output_name}, model={self.model_dir}"
                )
            outputs.append(np.asarray(output_mat, dtype=np.float32).copy())
        return outputs

    def predict(self, image_bgr: np.ndarray) -> dict[str, Any]:
        """Run preprocessing, NCNN inference and postprocessing on one image."""

        if not self.is_loaded:
            raise SteelBallNcnnError("NCNN runtime is not loaded; call load() before predict()")
        start_total = time.perf_counter()
        start = time.perf_counter()
        tensor, transform = prepare_input(image_bgr, self.imgsz)
        preprocess_ms = (time.perf_counter() - start) * 1000.0

        start = time.perf_counter()
        outputs = self._run_ncnn(tensor)
        inference_ms = (time.perf_counter() - start) * 1000.0

        start = time.perf_counter()
        boxes, scores, _class_ids = decode_raw_detect_output(
            outputs,
            class_count=len(self.class_names),
            conf_threshold=self.conf_threshold,
            iou_threshold=self.iou_threshold,
            max_det=self.max_det,
            input_width=self.imgsz,
            input_height=self.imgsz,
        )
        detections = detections_from_model_boxes(boxes, scores, transform)
        postprocess_ms = (time.perf_counter() - start) * 1000.0
        total_ms = (time.perf_counter() - start_total) * 1000.0

        return {
            "detections": detections,
            "detection_count": len(detections),
            "input_tensor_shape": [1, *tensor.shape],
            "output_tensor_shapes": [list(np.asarray(output).shape) for output in outputs],
            "output_tensor_diagnostics": _output_diagnostics(outputs),
            "model_class_names": {
                str(index): name for index, name in sorted(self.class_names.items())
            },
            "letterbox": asdict(transform),
            "timings_ms": {
                "preprocess": preprocess_ms,
                "inference": inference_ms,
                "postprocess": postprocess_ms,
                "total": total_ms,
            },
        }

    def close(self) -> None:
        """Release the model without touching cameras, serial ports or services."""

        net = self._net
        self._net = None
        self._ncnn = None
        if net is not None and hasattr(net, "clear"):
            net.clear()

    def __enter__(self) -> "SteelBallNcnnRuntime":
        self.load()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

