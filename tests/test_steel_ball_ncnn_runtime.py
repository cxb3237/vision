from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from inference.steel_ball_ncnn_runtime import (
    LetterboxTransform,
    SteelBallNcnnError,
    SteelBallNcnnRuntime,
    calculate_error_x_permille,
    decode_raw_detect_output,
    detections_from_model_boxes,
    letterbox,
    nms_xyxy,
    prepare_input,
    restore_boxes,
)
from tools.steel_ball_ncnn_offline import (
    annotate_image,
    make_benchmark,
    validate_source_argument,
)


def test_letterbox_preserves_aspect_ratio() -> None:
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    padded, transform = letterbox(image, 416)
    assert padded.shape == (416, 416, 3)
    assert transform.scale == pytest.approx(2.08)
    assert round(200 * transform.scale) == 416
    assert round(100 * transform.scale) == 208


def test_letterbox_records_padding() -> None:
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    _, transform = letterbox(image, 416)
    assert (transform.pad_left, transform.pad_right) == (0, 0)
    assert (transform.pad_top, transform.pad_bottom) == (104, 104)


def test_letterbox_coordinate_round_trip_within_one_pixel() -> None:
    image = np.zeros((273, 517, 3), dtype=np.uint8)
    _, transform = letterbox(image, 416)
    source = np.asarray([[31.0, 20.0, 411.0, 250.0]], dtype=np.float32)
    model = source.copy()
    model[:, [0, 2]] = model[:, [0, 2]] * transform.scale + transform.pad_left
    model[:, [1, 3]] = model[:, [1, 3]] * transform.scale + transform.pad_top
    restored = restore_boxes(model, transform)
    assert np.max(np.abs(restored - source)) <= 1.0


def test_prepare_input_is_chw_rgb_float32_normalized_once() -> None:
    image = np.full((10, 20, 3), (0, 128, 255), dtype=np.uint8)
    tensor, _ = prepare_input(image, 32)
    assert tensor.shape == (3, 32, 32)
    assert tensor.dtype == np.float32
    assert float(tensor.min()) >= 0.0
    assert float(tensor.max()) <= 1.0
    # A central unpadded pixel is RGB=(255,128,0).
    assert tensor[:, 16, 16].tolist() == pytest.approx([1.0, 128 / 255.0, 0.0])


def test_empty_raw_detections() -> None:
    output = np.empty((5, 0), dtype=np.float32)
    boxes, scores, classes = decode_raw_detect_output(
        [output],
        class_count=1,
        conf_threshold=0.4,
        iou_threshold=0.6,
        max_det=30,
        input_width=416,
        input_height=416,
    )
    assert boxes.shape == (0, 4)
    assert scores.shape == (0,)
    assert classes.shape == (0,)


def test_single_box_nms() -> None:
    boxes = np.asarray([[10, 10, 30, 30]], dtype=np.float32)
    selected = nms_xyxy(boxes, np.asarray([0.9], dtype=np.float32))
    assert selected.tolist() == [0]


def test_overlapping_boxes_are_suppressed() -> None:
    boxes = np.asarray([[10, 10, 30, 30], [11, 11, 31, 31]], dtype=np.float32)
    selected = nms_xyxy(boxes, np.asarray([0.9, 0.8], dtype=np.float32), 0.5)
    assert selected.tolist() == [0]


def test_non_overlapping_boxes_are_retained() -> None:
    boxes = np.asarray([[10, 10, 30, 30], [100, 100, 130, 130]], dtype=np.float32)
    selected = nms_xyxy(boxes, np.asarray([0.8, 0.9], dtype=np.float32), 0.5)
    assert selected.tolist() == [1, 0]


def test_low_confidence_raw_box_is_removed() -> None:
    output = np.asarray([[50], [50], [20], [20], [0.39]], dtype=np.float32)
    boxes, scores, _ = decode_raw_detect_output(
        [output],
        class_count=1,
        conf_threshold=0.4,
        iou_threshold=0.6,
        max_det=30,
        input_width=416,
        input_height=416,
    )
    assert boxes.size == 0
    assert scores.size == 0


def test_invalid_raw_boxes_are_removed() -> None:
    output = np.asarray(
        [[50, np.nan], [50, 20], [-1, 10], [20, 10], [0.9, 0.9]], dtype=np.float32
    )
    boxes, _, _ = decode_raw_detect_output(
        [output],
        class_count=1,
        conf_threshold=0.4,
        iou_threshold=0.6,
        max_det=30,
        input_width=416,
        input_height=416,
    )
    assert boxes.shape == (0, 4)


def test_error_x_permille_left_center_right() -> None:
    assert calculate_error_x_permille(0, 640) == -1000
    assert calculate_error_x_permille(320, 640) == 0
    assert calculate_error_x_permille(640, 640) == 1000
    assert calculate_error_x_permille(900, 640) == 1000


def test_missing_model_files_report_paths(tmp_path: Path) -> None:
    runtime = SteelBallNcnnRuntime(tmp_path)
    with pytest.raises(SteelBallNcnnError, match=r"model\.ncnn\.param"):
        runtime.load()


def _write_fake_model_dir(path: Path, class_name: str = "steel_ball") -> None:
    path.mkdir(parents=True)
    (path / "metadata.yaml").write_text(
        "task: detect\nimgsz: [416, 416]\nnames:\n  0: " + class_name + "\n",
        encoding="utf-8",
    )
    (path / "model.ncnn.param").write_text(
        "7767517\n2 2\nInput in0 0 1 in0\nNoop output 1 1 in0 out0\n",
        encoding="utf-8",
    )
    (path / "model.ncnn.bin").write_bytes(b"fake")
    (path / "model_ncnn.py").write_text(
        'ex.input("in0", value)\nex.extract("out0")\n', encoding="utf-8"
    )


def test_metadata_wrong_class_reports_file(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    _write_fake_model_dir(model_dir, "ball")
    runtime = SteelBallNcnnRuntime(model_dir)
    with pytest.raises(SteelBallNcnnError, match=r"class 0.*metadata\.yaml"):
        runtime.load()


def test_unknown_output_shape_has_diagnostics() -> None:
    output = np.arange(42, dtype=np.float32).reshape(6, 7)
    with pytest.raises(SteelBallNcnnError) as exc_info:
        decode_raw_detect_output(
            [output],
            class_count=1,
            conf_threshold=0.4,
            iou_threshold=0.6,
            max_det=30,
            input_width=416,
            input_height=416,
        )
    message = str(exc_info.value)
    assert "Unsupported NCNN output layout" in message
    assert "shape=(6, 7)" in message
    assert "dtype=float32" in message
    assert "min=0.0" in message
    assert "max=41.0" in message
    assert "sample=" in message


def test_known_raw_output_decodes_xywh_and_score() -> None:
    output = np.asarray([[50], [60], [20], [40], [0.9]], dtype=np.float32)
    boxes, scores, classes = decode_raw_detect_output(
        [output],
        class_count=1,
        conf_threshold=0.4,
        iou_threshold=0.6,
        max_det=30,
        input_width=416,
        input_height=416,
    )
    assert boxes.tolist() == [[40.0, 40.0, 60.0, 80.0]]
    assert scores.tolist() == pytest.approx([0.9])
    assert classes.tolist() == [0]


def test_detection_json_is_serializable() -> None:
    transform = LetterboxTransform(100, 80, 100, 100, 1.0, 0, 10, 0, 10)
    detections = detections_from_model_boxes(
        np.asarray([[20, 20, 60, 70]], dtype=np.float32),
        np.asarray([0.75], dtype=np.float32),
        transform,
    )
    document = {"detections": detections, "detection_count": len(detections)}
    encoded = json.dumps(document)
    assert "steel_ball" in encoded


@pytest.mark.parametrize("source", ["0", "12", "/dev/video0", "/dev/video12"])
def test_tool_rejects_camera_device_sources(source: str) -> None:
    with pytest.raises(ValueError, match="local image files only"):
        validate_source_argument(source)


def test_tool_accepts_existing_local_image(tmp_path: Path) -> None:
    path = tmp_path / "test.jpg"
    assert cv2.imwrite(str(path), np.zeros((16, 16, 3), dtype=np.uint8))
    assert validate_source_argument(path) == path.resolve()


def test_annotation_does_not_modify_input() -> None:
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    before = image.copy()
    annotated = annotate_image(
        image,
        [
            {
                "x1": 10,
                "y1": 10,
                "x2": 30,
                "y2": 30,
                "center_x": 20,
                "center_y": 20,
                "confidence": 0.9,
            }
        ],
    )
    assert np.array_equal(image, before)
    assert not np.array_equal(annotated, before)


def test_no_detection_annotation_is_still_generated() -> None:
    image = np.zeros((80, 100, 3), dtype=np.uint8)
    annotated = annotate_image(image, [])
    assert annotated.shape == image.shape
    assert np.any(annotated != image)


def test_benchmark_contains_median_p95_and_fps() -> None:
    rows = [
        {"preprocess": 1.0, "inference": 8.0, "postprocess": 1.0, "total": 10.0},
        {"preprocess": 2.0, "inference": 16.0, "postprocess": 2.0, "total": 20.0},
    ]
    benchmark = make_benchmark(rows, warmup=2, repeat=2, threads=4)
    assert benchmark["summary_ms"]["total_ms"]["median"] == 15.0
    assert benchmark["summary_ms"]["total_ms"]["p95"] == pytest.approx(19.5)
    assert benchmark["estimated_fps"] == pytest.approx(1000 / 15)
    json.dumps(benchmark)


def test_load_is_idempotent_and_disables_vulkan(tmp_path: Path, monkeypatch) -> None:
    model_dir = tmp_path / "model"
    _write_fake_model_dir(model_dir)
    calls = {"param": 0, "model": 0}

    class FakeNet:
        def __init__(self) -> None:
            self.opt = SimpleNamespace(use_vulkan_compute=True, num_threads=0)

        def load_param(self, _path: str) -> int:
            calls["param"] += 1
            return 0

        def load_model(self, _path: str) -> int:
            calls["model"] += 1
            return 0


    fake_module = SimpleNamespace(Net=FakeNet)
    monkeypatch.setattr(
        "inference.steel_ball_ncnn_runtime.importlib.import_module",
        lambda name: fake_module if name == "ncnn" else None,
    )
    runtime = SteelBallNcnnRuntime(model_dir, num_threads=3)
    runtime.load()
    runtime.load()
    assert calls == {"param": 1, "model": 1}
    assert runtime._net.opt.use_vulkan_compute is False
    assert runtime._net.opt.num_threads == 3

