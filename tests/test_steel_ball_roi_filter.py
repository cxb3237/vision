from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest
import yaml

from core.config_loader import ConfigError, load_steel_ball_ncnn_config
from core.models import FramePacket, SteelBallNcnnConfig
from core.pipe_corridor import PipeCorridorConfig
from detectors.steel_ball_yolo_ncnn_detector import SteelBallYoloNcnnDetector
import tools.switch_steel_ball_model as switch_tool
from tools.switch_steel_ball_model import build_parser
from tools.validate_steel_ball_models import PROFILE_PATHS, validate_profile


def detection(confidence, box):
    x1, y1, x2, y2 = box
    return {"confidence": confidence, "x1": x1, "y1": y1, "x2": x2, "y2": y2}


class FakeRuntime:
    class_names = {0: "steel_ball"}

    def __init__(self, predictions):
        self.predictions = predictions

    def load(self):
        pass

    def predict(self, _image):
        return {
            "detections": self.predictions,
            "timings_ms": {"preprocess": 1, "inference": 2, "postprocess": 1, "total": 4},
        }

    def close(self):
        pass


class FixedMarkers:
    def __init__(self, marker_a=(180, 50), marker_b=(20, 50)):
        self.markers = (marker_a, marker_b)

    def detect(self, _image):
        return self.markers


def make_detector(
    predictions,
    *,
    enabled=True,
    markers=None,
    require=True,
    ratio=0.0,
    fixed_width=10.0,
):
    cfg = SteelBallNcnnConfig(
        pipe_roi=PipeCorridorConfig(
            enabled=enabled,
            require_valid_geometry=require,
            hold_last_valid_ms=100,
            minimum_axis_length_px=20,
            corridor_half_width_ratio=ratio,
            corridor_half_width_px=fixed_width,
            end_margin_px=5,
        )
    )
    runtime = FakeRuntime(predictions)
    detector = SteelBallYoloNcnnDetector(
        cfg,
        runtime_factory=lambda **_: runtime,
        pipe_marker_detector=markers if markers is not None else FixedMarkers(),
    )
    detector.initialize()
    return detector


def packet():
    return FramePacket(1, 1.0, np.zeros((100, 200, 3), dtype=np.uint8))


def test_roi_filters_high_confidence_outside_before_formal_selection() -> None:
    detector = make_detector(
        [detection(0.95, (80, 75, 100, 95)), detection(0.80, (80, 42, 100, 58))]
    )
    result = detector.process(packet())
    assert result.found and result.confidence == 800 and result.center_y == 50
    status = detector.get_runtime_status()
    assert status["raw_detection_count"] == 2
    assert status["roi_accepted_count"] == 1
    assert status["roi_rejected_count"] == 1


def test_all_boxes_outside_returns_no_target() -> None:
    detector = make_detector([detection(0.9, (80, 75, 100, 95))])
    assert not detector.process(packet()).found
    assert detector.get_runtime_status()["pipe_roi_last_reason"] == "outside_corridor"


def test_roi_disabled_matches_existing_selection() -> None:
    predictions = [detection(0.95, (80, 75, 100, 95)), detection(0.80, (80, 42, 100, 58))]
    detector = make_detector(predictions, enabled=False)
    result = detector.process(packet())
    assert result.found and result.confidence == 950 and result.center_y == 85
    assert detector.get_runtime_status()["roi_rejected_count"] == 0


def test_invalid_geometry_fails_closed_when_required() -> None:
    detector = make_detector(
        [detection(0.9, (80, 42, 100, 58))], markers=FixedMarkers(None, None), require=True
    )
    assert not detector.process(packet()).found
    assert detector.get_runtime_status()["pipe_roi_last_reason"] == "geometry_missing"


def test_debug_overlay_does_not_modify_input_or_crash() -> None:
    detector = make_detector([detection(0.9, (80, 42, 100, 58))])
    detector.config.pipe_roi.debug_overlay = True
    frame = packet()
    result = detector.process(frame)
    annotated = detector.draw_debug(frame.image, result)
    assert annotated.shape == frame.image.shape
    assert np.count_nonzero(frame.image) == 0


def test_detector_status_tracks_effective_dynamic_half_width() -> None:
    markers = FixedMarkers((180, 50), (20, 50))
    detector = make_detector(
        [detection(0.9, (80, 47, 100, 53))],
        markers=markers,
        ratio=0.04,
        fixed_width=0,
    )
    detector.process(packet())
    assert detector.get_runtime_status()["effective_half_width_px"] == pytest.approx(6.4)
    markers.markers = ((120, 50), (20, 50))
    detector.process(packet())
    assert detector.get_runtime_status()["effective_half_width_px"] == pytest.approx(4.0)


def test_old_config_defaults_roi_off_and_profiles_are_distinct() -> None:
    baseline = load_steel_ball_ncnn_config("config/model_profiles/steel_ball_baseline.yaml")
    raw = load_steel_ball_ncnn_config("config/model_profiles/steel_ball_candidate.yaml")
    roi = load_steel_ball_ncnn_config("config/model_profiles/steel_ball_candidate_roi.yaml")
    strict = load_steel_ball_ncnn_config(
        "config/model_profiles/steel_ball_candidate_roi_strict.yaml"
    )
    assert not baseline.pipe_roi.enabled
    assert raw.conf_threshold == pytest.approx(0.40) and not raw.pipe_roi.enabled
    assert roi.conf_threshold == pytest.approx(0.40) and roi.pipe_roi.enabled
    assert roi.pipe_roi.corridor_half_width_ratio == pytest.approx(0.04)
    assert roi.pipe_roi.hold_last_valid_ms == 250
    assert strict.conf_threshold == pytest.approx(0.50) and strict.pipe_roi.enabled
    assert strict.pipe_roi.corridor_half_width_ratio == pytest.approx(0.04)
    assert strict.pipe_roi.hold_last_valid_ms == 250
    assert asdict(roi.pipe_roi) == asdict(strict.pipe_roi)
    for name in ("backend", "model_path", "imgsz", "iou_threshold", "max_det", "num_threads", "target_class"):
        assert getattr(roi, name) == getattr(strict, name)


def test_roi_disabled_allows_zero_width_but_enabled_rejects_it(tmp_path: Path) -> None:
    source = yaml.safe_load(Path("config/model_profiles/steel_ball_candidate.yaml").read_text(encoding="utf-8"))
    source["pipe_roi"] = {"enabled": False, "corridor_half_width_px": 0}
    path = tmp_path / "disabled.yaml"
    path.write_text(yaml.safe_dump(source), encoding="utf-8")
    assert not load_steel_ball_ncnn_config(path).pipe_roi.enabled
    source["pipe_roi"]["enabled"] = True
    path.write_text(yaml.safe_dump(source), encoding="utf-8")
    with pytest.raises(ConfigError, match="corridor_half_width_px"):
        load_steel_ball_ncnn_config(path)


def test_candidate_roi_switch_and_static_validation_are_available(tmp_path: Path, monkeypatch) -> None:
    assert build_parser().parse_args(["candidate-roi"]).command == "candidate-roi"
    assert (
        build_parser().parse_args(["candidate-roi-strict"]).command
        == "candidate-roi-strict"
    )
    assert set(PROFILE_PATHS) == {
        "baseline",
        "candidate",
        "candidate-roi",
        "candidate-roi-strict",
    }
    active = tmp_path / "active.yaml"
    monkeypatch.setattr(switch_tool, "ACTIVE_CONFIG", active)
    for profile, path in PROFILE_PATHS.items():
        active.write_bytes(path.read_bytes())
        assert switch_tool.active_profile()[0] == profile
        result = validate_profile(profile, load_runtime=False)
        assert result.error is None and result.loaded
