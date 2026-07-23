"""第二轮 HSV、颜色类别、录制和去畸变测试。"""

import time

import numpy as np
import pytest

from app import create_detector
from core.config_loader import load_color_config, load_mission_config
from core.models import CalibrationConfig, ColorClass, FramePacket
from tools.hsv_tuner import create_live_camera
from tools import record_dataset
from tools.record_dataset import validate_video_resolution
from tools.undistort_test import validate_calibration_resolution


def test_hsv_live_source_uses_camera_yaml_with_device_override() -> None:
    camera = create_live_camera("3", "config/camera.yaml")
    assert camera.config.device == 3
    assert camera.config.width == 640
    assert camera.config.fourcc == "MJPG"
    assert camera.config.gain is None


def test_color_class_mapping_is_stable_and_detector_uses_it() -> None:
    expected = {
        "red": 1,
        "green": 2,
        "blue": 3,
        "yellow": 4,
        "black": 5,
        "white": 6,
    }
    colors = load_color_config()
    mission = load_mission_config()
    for name, value in expected.items():
        assert int(ColorClass.from_name(name)) == value
        detector = create_detector("color", name, colors, mission)
        assert detector.target_class == value
        assert not detector.temporal_tracking


def test_calibration_resolution_mismatch_is_rejected() -> None:
    calibration = CalibrationConfig(
        calibrated=True,
        image_width=640,
        image_height=480,
        camera_matrix=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
        distortion_coefficients=[0.0] * 5,
    )
    with pytest.raises(ValueError, match="分辨率"):
        validate_calibration_resolution(np.zeros((240, 320, 3), np.uint8), calibration)


def test_video_resolution_change_is_rejected() -> None:
    frame = FramePacket(2, time.monotonic(), np.zeros((240, 320, 3), np.uint8))
    with pytest.raises(RuntimeError, match="分辨率"):
        validate_video_resolution(frame, (640, 480))


class NoFrameCamera:
    instances = []

    def __init__(self, _config) -> None:
        self.stopped = False
        self.instances.append(self)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True

    def get_latest_frame(self, copy_image: bool = False):
        return None

    def get_statistics(self):
        return {"device_reported_fps": 30.0, "actual_fps": 0.0}


def test_recording_first_frame_timeout_stops_camera(tmp_path, monkeypatch) -> None:
    NoFrameCamera.instances.clear()
    monkeypatch.setattr(record_dataset, "CameraService", NoFrameCamera)
    with pytest.raises(TimeoutError, match="首帧"):
        record_dataset.main(
            [
                "--images",
                str(tmp_path / "images"),
                "--startup-timeout",
                "0.01",
                "--frame-timeout",
                "0.01",
            ]
        )
    assert NoFrameCamera.instances[0].stopped
