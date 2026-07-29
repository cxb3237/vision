from __future__ import annotations

import json
import time

import numpy as np
import pytest

from core.models import FramePacket, SteelBallNcnnConfig, TargetState
from detectors.base_detector import BaseDetector
from detectors.steel_ball_yolo_ncnn_detector import SteelBallYoloNcnnDetector, select_primary_detection
from detectors.target_tracker import TargetTracker


def detection(confidence=0.9, box=(10, 20, 50, 60)):
    x1, y1, x2, y2 = box
    return {"class_id": 0, "class_name": "steel_ball", "confidence": confidence, "x1": x1, "y1": y1, "x2": x2, "y2": y2}


class FakeRuntime:
    class_names = {0: "steel_ball"}

    def __init__(self, predictions=None):
        self.predictions = list(predictions or [[detection()]])
        self.loads = self.predicts = self.closes = 0

    def load(self):
        self.loads += 1

    def predict(self, _image):
        value = self.predictions[min(self.predicts, len(self.predictions) - 1)]
        self.predicts += 1
        return {"detections": value, "timings_ms": {"preprocess": 1, "inference": 5, "postprocess": 1, "total": 7}}

    def close(self):
        self.closes += 1


def make_detector(fake: FakeRuntime) -> SteelBallYoloNcnnDetector:
    detector = SteelBallYoloNcnnDetector(SteelBallNcnnConfig(), runtime_factory=lambda **_: fake)
    detector.initialize()
    return detector


def packet(frame_id=1):
    return FramePacket(frame_id, time.monotonic(), np.zeros((120, 160, 3), np.uint8))


def test_detector_interface_model_load_once_and_result_fields() -> None:
    fake = FakeRuntime()
    detector = make_detector(fake)
    detector.initialize()
    result = detector.process(packet())
    assert isinstance(detector, BaseDetector)
    assert fake.loads == 1
    assert result.found and result.target_class == 100 and result.confidence == 900


def test_selection_is_stable_and_prefers_area_within_confidence_tolerance() -> None:
    small = detection(0.90, (70, 50, 90, 70))
    large = detection(0.86, (20, 20, 100, 100))
    selected, valid = select_primary_detection([small, large], 160, 120)
    assert len(valid) == 2 and selected["area"] == 6400


def test_tracker_lost_and_reacquisition() -> None:
    detector = make_detector(FakeRuntime([[detection()], [], [], [detection(box=(100, 40, 130, 70))]]))
    tracker = TargetTracker(confirm_frames=1, lost_frames=2, max_jump_px=200)
    assert tracker.update(detector.process(packet(1))).target_state == TargetState.LOCKED
    tracker.update(detector.process(packet(2)))
    assert tracker.update(detector.process(packet(3))).target_state == TargetState.LOST
    assert tracker.update(detector.process(packet(4))).found


def test_debug_drawing_does_not_modify_input_and_status_serializes() -> None:
    detector = make_detector(FakeRuntime([[detection(), detection(0.85, (90, 30, 130, 70))]]))
    frame = packet()
    before = frame.image.copy()
    result = detector.process(frame)
    annotated = detector.draw_debug(frame.image, result)
    assert np.array_equal(frame.image, before)
    assert not np.array_equal(annotated, before)
    json.dumps(detector.get_runtime_status())
