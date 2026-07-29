from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pytest
import yaml

from app import create_detector
from core.config_loader import (
    ConfigError,
    load_color_config,
    load_mission_config,
    load_steel_ball_ncnn_config,
)
from core.models import (
    BallPositionMappingConfig,
    FramePacket,
    SteelBallNcnnConfig,
    TargetState,
)
from detectors.base_detector import BaseDetector
from detectors.steel_ball_detector import SteelBallDetector
from detectors.steel_ball_yolo_ncnn_detector import (
    SteelBallYoloNcnnDetector,
    select_primary_detection,
)
from detectors.target_tracker import TargetTracker
from protocol.vmc_link import DetectorID, normalize_detector_id, result_to_vmc_link
from tests.test_vision_runtime_touch import FakeCamera, FakeSerial, _runtime


def _detection(
    confidence: float = 0.9,
    box: tuple[int, int, int, int] = (10, 20, 50, 60),
) -> dict[str, object]:
    x1, y1, x2, y2 = box
    return {
        "class_id": 0,
        "class_name": "steel_ball",
        "confidence": confidence,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
    }


class FakeNcnnRuntime:
    def __init__(self, predictions=None, *, load_error: Exception | None = None) -> None:
        self.class_names = {0: "steel_ball"}
        self.load_count = 0
        self.predict_count = 0
        self.close_count = 0
        self.load_error = load_error
        self.predictions = list(predictions or [[_detection()]])

    def load(self) -> None:
        self.load_count += 1
        if self.load_error is not None:
            raise self.load_error

    def predict(self, _image):
        index = min(self.predict_count, len(self.predictions) - 1)
        detections = self.predictions[index]
        self.predict_count += 1
        return {
            "detections": detections,
            "input_tensor_shape": [1, 3, 416, 416],
            "output_tensor_shapes": [[5, 3549]],
            "timings_ms": {
                "preprocess": 1.25,
                "inference": 37.5,
                "postprocess": 0.75,
                "total": 39.5,
            },
        }

    def close(self) -> None:
        self.close_count += 1


def _config(**overrides) -> SteelBallNcnnConfig:
    values = {
        "model_path": "models/steel_ball/best_ncnn_model",
        "imgsz": 416,
        "conf_threshold": 0.4,
        "iou_threshold": 0.6,
        "max_det": 30,
        "num_threads": 4,
        "target_class": 100,
    }
    values.update(overrides)
    return SteelBallNcnnConfig(**values)


def _detector(fake: FakeNcnnRuntime, **config_overrides) -> SteelBallYoloNcnnDetector:
    detector = SteelBallYoloNcnnDetector(
        _config(**config_overrides), runtime_factory=lambda **_kwargs: fake
    )
    detector.initialize()
    return detector


def _packet(image=None, frame_id: int = 1) -> FramePacket:
    if image is None:
        image = np.zeros((120, 160, 3), dtype=np.uint8)
    return FramePacket(frame_id, time.monotonic(), image)


def test_detector_implements_common_interface() -> None:
    assert isinstance(_detector(FakeNcnnRuntime()), BaseDetector)


def test_model_loads_once_and_not_per_frame() -> None:
    fake = FakeNcnnRuntime()
    detector = _detector(fake)
    detector.initialize()
    detector.process(_packet(frame_id=1))
    detector.process(_packet(frame_id=2))
    assert fake.load_count == 1
    assert fake.predict_count == 2


def test_empty_frame_returns_no_target_without_inference() -> None:
    fake = FakeNcnnRuntime()
    detector = _detector(fake)
    result = detector.process(_packet(np.empty((0, 0, 3), dtype=np.uint8)))
    assert not result.found
    assert fake.predict_count == 0
    assert detector.get_runtime_status()["detector_error"]


def test_confidence_has_strict_priority_outside_tolerance() -> None:
    high = _detection(0.90, (120, 80, 140, 100))
    large = _detection(0.84, (20, 20, 100, 100))
    selected, _ = select_primary_detection([large, high], 160, 120)
    assert selected["confidence"] == pytest.approx(0.90)


def test_area_is_secondary_within_confidence_tolerance() -> None:
    small = _detection(0.90, (70, 50, 90, 70))
    large = _detection(0.86, (20, 20, 100, 100))
    selected, _ = select_primary_detection([small, large], 160, 120)
    assert selected["area"] == 6400


def test_center_and_coordinates_make_selection_stable() -> None:
    edge = _detection(0.90, (0, 0, 20, 20))
    center = _detection(0.90, (70, 50, 90, 70))
    first, _ = select_primary_detection([edge, center], 160, 120)
    second, _ = select_primary_detection([center, edge], 160, 120)
    assert first == second
    assert first["center_x"] == 80 and first["center_y"] == 60


def test_confidence_converts_to_permille() -> None:
    detector = _detector(FakeNcnnRuntime([[ _detection(0.8764) ]]))
    result = detector.process(_packet())
    assert result.confidence == 876
    assert result_to_vmc_link(result, detector_id="steel_ball_yolo_ncnn").confidence_permille == 876


@pytest.mark.parametrize(
    ("box", "sign"),
    [((0, 30, 20, 60), -1), ((140, 30, 160, 60), 1)],
)
def test_error_x_sign_matches_project_convention(box, sign) -> None:
    result = _detector(FakeNcnnRuntime([[_detection(0.9, box)]])).process(_packet())
    assert result.error_x_px * sign > 0


def test_bbox_is_clipped_and_legal() -> None:
    result = _detector(
        FakeNcnnRuntime([[_detection(0.9, (-20, -10, 190, 150))]])
    ).process(_packet())
    assert result.found
    assert (result.bbox_x, result.bbox_y) == (0, 0)
    assert result.bbox_width == 160 and result.bbox_height == 120


def test_no_detection_flows_through_existing_target_tracker() -> None:
    detector = _detector(FakeNcnnRuntime([[ _detection() ], [], []]))
    tracker = TargetTracker(confirm_frames=1, lost_frames=2)
    locked = tracker.update(detector.process(_packet(frame_id=1)))
    occluded = tracker.update(detector.process(_packet(frame_id=2)))
    lost = tracker.update(detector.process(_packet(frame_id=3)))
    assert locked.target_state == TargetState.LOCKED
    assert occluded.target_state == TargetState.OCCLUDED
    assert lost.target_state == TargetState.LOST


def test_debug_mode_runs_ncnn_and_preview_without_publish(tmp_path: Path) -> None:
    fake = FakeNcnnRuntime([[ _detection() ]])
    detector = _detector(fake)
    serial = FakeSerial(online=True)
    runtime = _runtime(
        tmp_path,
        detector=detector,
        camera=FakeCamera([_packet()], finished=True),
        serial=serial,
        detector_id="steel_ball_yolo_ncnn",
    )
    assert runtime.run_forever() == 0
    status = runtime.get_status_snapshot()
    assert fake.predict_count == 1 and serial.published == []
    assert status["steel_ball_backend"] == "ncnn"
    assert status["detection_count"] == 1
    assert status["total_ms"] == 39.5
    assert not status["vision_output_enabled"]


def test_competition_mode_uses_existing_publish_gate(tmp_path: Path) -> None:
    detector = _detector(
        FakeNcnnRuntime([[_detection()]]),
        position_mapping=BallPositionMappingConfig(
            calibrated=True,
            x_minus_125_px=0,
            x_plus_125_px=100,
        ),
    )
    serial = FakeSerial(online=True)
    runtime = _runtime(
        tmp_path,
        detector=detector,
        camera=FakeCamera([_packet()], finished=True),
        serial=serial,
        detector_id="steel_ball_yolo_ncnn",
        initial_competition_mode=True,
    )
    runtime.run_forever()
    assert len(serial.published) == 1
    assert serial.published == [-50]


def test_model_load_failure_keeps_camera_runtime_alive(tmp_path: Path) -> None:
    fake = FakeNcnnRuntime(load_error=RuntimeError("missing model.ncnn.bin"))
    detector = _detector(fake)
    camera = FakeCamera([_packet()], finished=True)
    runtime = _runtime(
        tmp_path,
        detector=detector,
        camera=camera,
        detector_id="steel_ball_yolo_ncnn",
    )
    assert runtime.run_forever() == 0
    assert camera.start_count == camera.stop_count == 1
    assert "missing model.ncnn.bin" in runtime.get_status_snapshot()["detector_error"]


def test_formal_import_path_does_not_import_training_frameworks() -> None:
    code = (
        "import sys; import detectors.steel_ball_yolo_ncnn_detector; "
        "print(int('torch' in sys.modules), int('ultralytics' in sys.modules))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "0 0"


def test_classical_and_ncnn_detectors_remain_selectable() -> None:
    colors = load_color_config()
    mission = load_mission_config()
    classical = create_detector("steel_ball_classical", "red", colors, mission)
    ncnn = create_detector("steel_ball_yolo_ncnn", "red", colors, mission)
    legacy = create_detector("steel_ball", "red", colors, mission)
    assert isinstance(classical, SteelBallDetector)
    assert isinstance(legacy, SteelBallDetector)
    assert isinstance(ncnn, SteelBallYoloNcnnDetector)


def test_both_steel_ball_names_keep_protocol_detector_id() -> None:
    assert normalize_detector_id("steel_ball_classical") == DetectorID.STEEL_BALL
    assert normalize_detector_id("steel_ball_yolo_ncnn") == DetectorID.STEEL_BALL


def test_status_snapshot_is_json_serializable() -> None:
    detector = _detector(FakeNcnnRuntime([[ _detection() ]]))
    detector.process(_packet())
    status = detector.get_runtime_status()
    assert status["model_loaded"]
    assert status["ncnn_threads"] == 4
    json.dumps(status)


def test_draw_debug_keeps_all_boxes_and_does_not_modify_input() -> None:
    detections = [_detection(0.91, (10, 20, 50, 60)), _detection(0.85, (90, 30, 130, 70))]
    detector = _detector(FakeNcnnRuntime([detections]))
    image = np.zeros((120, 160, 3), dtype=np.uint8)
    before = image.copy()
    result = detector.process(_packet(image))
    annotated = detector.draw_debug(image, result)
    assert detector.get_runtime_status()["detection_count"] == 2
    assert np.array_equal(image, before)
    assert not np.array_equal(annotated, before)


def test_camera_service_and_detector_have_no_frame_fifo() -> None:
    from core.models import CameraConfig
    from drivers.camera_service import CameraService

    service = CameraService(CameraConfig())
    detector = _detector(FakeNcnnRuntime())
    assert hasattr(service, "_latest")
    assert not hasattr(service, "_frame_queue")
    assert not hasattr(detector, "_frame_queue")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("imgsz", 0),
        ("conf_threshold", 1.1),
        ("iou_threshold", -0.1),
        ("max_det", 101),
        ("num_threads", 5),
    ],
)
def test_ncnn_config_rejects_out_of_range_values(tmp_path: Path, field: str, value) -> None:
    data = {
        "backend": "ncnn",
        "model_path": "models/steel_ball/best_ncnn_model",
        "imgsz": 416,
        "conf_threshold": 0.4,
        "iou_threshold": 0.6,
        "max_det": 30,
        "num_threads": 4,
        "target_class": 100,
        "fallback_to_classical": False,
        "debug_shapes": False,
    }
    data[field] = value
    path = tmp_path / "steel_ball_ncnn.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=field):
        load_steel_ball_ncnn_config(path)
