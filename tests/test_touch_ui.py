"""触摸界面配置、API、状态、预览和现场保存的无硬件测试。"""

from __future__ import annotations

from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import threading
import time

import cv2
import numpy as np
import pytest
import yaml

from touch_ui.api import TouchAPI
from touch_ui.frame_stream import LatestFrameStream
from touch_ui.models import (
    CommandStatus,
    CommandType,
    RuntimeCommand,
    TouchUIConfigError,
    load_touch_ui_config,
)
from touch_ui.runtime_config import RuntimeConfigStore
from touch_ui.server import TouchUIServer
from touch_ui.state_store import StateStore


PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "config/touch_ui.yaml"


def touch_config(tmp_path: Path):
    return load_touch_ui_config(PROJECT_CONFIG, project_root=tmp_path)


class FakeRuntime:
    def __init__(self) -> None:
        self.state = {
            "runtime_running": True,
            "competition_mode": False,
            "vision_output_enabled": False,
            "detector": "steel_ball_yolo_ncnn",
        }
        self.config = {
            "controls": {
                "brightness": {
                    "supported": True,
                    "minimum": 0,
                    "maximum": 100,
                    "actual": 20,
                },
                "gamma": {"supported": False, "error": "not supported"},
                "white_balance_automatic": {"supported": True, "actual": 1},
                "white_balance_temperature": {
                    "supported": True,
                    "minimum": 2800,
                    "maximum": 6500,
                    "actual": 5000,
                },
                "exposure_auto": {"supported": True, "actual": 3},
                "exposure_absolute": {
                    "supported": True,
                    "minimum": 1,
                    "maximum": 1000,
                    "actual": 100,
                },
            }
        }
        self.commands: list[tuple[CommandType, dict]] = []

    def get_status_snapshot(self):
        return json.loads(json.dumps(self.state))

    def get_runtime_config_snapshot(self):
        return json.loads(json.dumps(self.config))

    def submit_command(self, command_type, payload=None):
        command_type = CommandType(command_type)
        self.commands.append((command_type, dict(payload or {})))
        return f"command-{len(self.commands)}"


def test_touch_config_loads_and_creates_only_runtime_directories(tmp_path) -> None:
    config = touch_config(tmp_path)
    assert config.host == "127.0.0.1" and config.port == 8765
    assert config.runtime_directory.is_dir()
    assert config.backup_directory.is_dir()
    assert not (tmp_path / "dev/video0").exists()


def test_touch_config_rejects_specific_invalid_field(tmp_path) -> None:
    data = yaml.safe_load(PROJECT_CONFIG.read_text(encoding="utf-8"))
    data["preview"]["jpeg_quality"] = 101
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(TouchUIConfigError, match="preview.jpeg_quality"):
        load_touch_ui_config(path, project_root=tmp_path)


def test_touch_config_rejects_absolute_runtime_path(tmp_path) -> None:
    data = yaml.safe_load(PROJECT_CONFIG.read_text(encoding="utf-8"))
    data["runtime"]["directory"] = str(tmp_path.resolve())
    path = tmp_path / "bad-path.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(TouchUIConfigError, match="相对路径"):
        load_touch_ui_config(path, project_root=tmp_path)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "8.8.8.8"])
def test_touch_config_rejects_non_loopback_host(tmp_path, host) -> None:
    data = yaml.safe_load(PROJECT_CONFIG.read_text(encoding="utf-8"))
    data["server"]["host"] = host
    path = tmp_path / "bad-host.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(TouchUIConfigError, match="回环"):
        load_touch_ui_config(path, project_root=tmp_path)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_touch_config_accepts_only_supported_loopback_hosts(tmp_path, host) -> None:
    data = yaml.safe_load(PROJECT_CONFIG.read_text(encoding="utf-8"))
    data["server"]["host"] = host
    path = tmp_path / "loopback.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_touch_ui_config(path, project_root=tmp_path).host == host


@pytest.mark.parametrize(("content_length", "expected_status"), [("-1", 400), ("65537", 413)])
def test_http_rejects_invalid_or_oversized_content_length(
    tmp_path, content_length, expected_status
) -> None:
    config = replace(touch_config(tmp_path), host="127.0.0.1", port=0)
    server = TouchUIServer(FakeRuntime(), config)
    server.start()
    try:
        assert server._server is not None
        port = int(server._server.server_address[1])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.putrequest("POST", "/api/competition/enter")
        connection.putheader("Content-Length", content_length)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == expected_status
        response.read()
        connection.close()
    finally:
        server.stop()


def test_state_snapshot_is_thread_safe_and_serializable() -> None:
    store = StateStore({"count": 0, "nested": {"value": 0}})

    def writer(offset: int) -> None:
        for value in range(100):
            store.update(count=offset + value, nested={"value": value})

    threads = [threading.Thread(target=writer, args=(index * 100,)) for index in range(4)]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        json.dumps(store.snapshot())
    for thread in threads:
        thread.join()
    snapshot = store.snapshot()
    snapshot["nested"]["value"] = -1
    assert store.snapshot()["nested"]["value"] != -1


def test_command_status_has_required_lifecycle_values() -> None:
    store = StateStore()
    command = RuntimeCommand.create(CommandType.SAVE_RUNTIME)
    store.add_command(command)
    store.set_command_status(command.command_id, CommandStatus.APPLYING)
    store.set_command_status(command.command_id, CommandStatus.APPLIED)
    assert store.command_snapshot(command.command_id)["status"] == "APPLIED"


def test_camera_patch_only_queues_command() -> None:
    runtime = FakeRuntime()
    status, body = TouchAPI(runtime).patch_camera({"controls": {"brightness": 42}})
    assert status == 202 and body["command_id"] == "command-1"
    assert runtime.commands == [
        (CommandType.SET_CAMERA_CONTROL, {"name": "brightness", "value": 42})
    ]


def test_unsupported_and_out_of_range_controls_are_rejected() -> None:
    api = TouchAPI(FakeRuntime())
    assert api.patch_camera({"controls": {"gamma": 50}})[1]["error_code"] == "UNSUPPORTED_CONTROL"
    assert api.patch_camera({"controls": {"brightness": 101}})[1]["error_code"] == "OUT_OF_RANGE"


def test_auto_controls_disable_manual_values() -> None:
    api = TouchAPI(FakeRuntime())
    assert api.patch_camera({"controls": {"white_balance_temperature": 5000}})[1]["error_code"] == "AUTO_CONTROL_ACTIVE"
    assert api.patch_camera({"controls": {"exposure_absolute": 120}})[1]["error_code"] == "AUTO_CONTROL_ACTIVE"


def test_competition_mode_blocks_modifications_but_exit_is_queued() -> None:
    runtime = FakeRuntime()
    runtime.state["competition_mode"] = True
    api = TouchAPI(runtime)
    assert api.patch_camera({"controls": {"brightness": 30}})[1]["error_code"] == "COMPETITION_MODE"
    assert not hasattr(api, "select_detector")
    assert api.command(CommandType.EXIT_COMPETITION)[0] == 202


def test_latest_frame_stream_encodes_only_latest_pending_frame() -> None:
    stream = LatestFrameStream(max_fps=20, jpeg_quality=95, max_width=320)
    black = np.zeros((80, 120, 3), np.uint8)
    white = np.full_like(black, 255)
    stream.submit_frame(black)
    stream.submit_frame(white)
    stream.start()
    deadline = time.monotonic() + 1
    while stream.encoded_count < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    jpeg = stream.get_latest_jpeg(placeholder=False)
    stream.stop()
    decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.mean() > 240
    assert stream.encoded_count == 1


def test_preview_rate_is_limited_without_strict_timing_assertion() -> None:
    stream = LatestFrameStream(max_fps=5, jpeg_quality=70, max_width=320)
    stream.start()
    started = time.monotonic()
    while time.monotonic() - started < 0.45:
        stream.submit_frame(np.zeros((40, 60, 3), np.uint8))
        time.sleep(0.01)
    stream.stop()
    assert 1 <= stream.encoded_count <= 4


def test_preview_recovers_after_placeholder_without_recreating_source() -> None:
    stream = LatestFrameStream(max_fps=20)
    placeholder = stream.get_latest_jpeg()
    stream.start()
    stream.submit_frame(np.full((50, 80, 3), 180, np.uint8))
    deadline = time.monotonic() + 1
    while stream.encoded_count < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    recovered = stream.get_latest_jpeg()
    stream.stop()
    assert recovered and recovered != placeholder


def test_preview_statistics_count_submissions_and_pending_overwrites() -> None:
    stream = LatestFrameStream(max_fps=20)
    frame = np.zeros((40, 60, 3), np.uint8)
    stream.submit_frame(frame)
    stream.submit_frame(frame)
    statistics = stream.get_statistics()
    assert statistics["preview_submitted_count"] == 2
    assert statistics["preview_overwritten_count"] == 1
    assert statistics["preview_pending"] is True


def test_preview_statistics_measure_encoding_rate_age_and_size() -> None:
    stream = LatestFrameStream(max_fps=30)
    stream.start()
    for value in (10, 20):
        stream.submit_frame(np.full((40, 60, 3), value, np.uint8))
        deadline = time.monotonic() + 1
        expected = value // 10
        while stream.encoded_count < expected and time.monotonic() < deadline:
            time.sleep(0.005)
    statistics = stream.get_statistics()
    stream.stop()
    assert statistics["preview_encoded_count"] >= 2
    assert statistics["preview_fps"] > 0
    assert statistics["preview_encode_ms"] >= 0
    assert statistics["preview_encode_p95_ms"] >= statistics["preview_encode_median_ms"]
    assert statistics["preview_age_ms"] >= 0
    assert statistics["preview_jpeg_bytes"] > 0


def test_preview_get_statistics_is_thread_safe_under_submission() -> None:
    stream = LatestFrameStream(max_fps=30)
    stream.start()
    frame = np.zeros((20, 30, 3), np.uint8)
    threads = [threading.Thread(target=lambda: [stream.submit_frame(frame) for _ in range(20)]) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statistics = stream.get_statistics()
    stream.stop()
    assert statistics["preview_submitted_count"] == 60
    assert statistics["preview_overwritten_count"] >= 1


def test_preview_clear_buffers_is_rejected_while_encoder_is_running() -> None:
    stream = LatestFrameStream(max_fps=20)
    stream.start()
    try:
        with pytest.raises(RuntimeError, match="thread to be stopped"):
            stream.reset_statistics(clear_buffers=True)
    finally:
        stream.stop()


def test_preview_clear_buffers_after_stop_removes_pending_and_jpeg() -> None:
    stream = LatestFrameStream(max_fps=20)
    stream.start()
    stream.submit_frame(np.full((40, 60, 3), 90, np.uint8))
    deadline = time.monotonic() + 1
    while stream.encoded_count < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    stream.stop()
    stream.submit_frame(np.zeros((40, 60, 3), np.uint8))
    assert stream.get_latest_jpeg(placeholder=False) is not None
    assert stream.get_statistics()["preview_pending"] is True
    stream.reset_statistics(clear_buffers=True)
    statistics = stream.get_statistics()
    assert stream.get_latest_jpeg(placeholder=False) is None
    assert statistics["preview_pending"] is False
    assert statistics["preview_jpeg_bytes"] == 0
    assert statistics["preview_submitted_count"] == 0
    assert statistics["preview_encoded_count"] == 0
    assert statistics["preview_overwritten_count"] == 0


def test_preview_warmup_counts_do_not_cross_clean_measurement_boundary() -> None:
    stream = LatestFrameStream(max_fps=30)
    frame = np.zeros((40, 60, 3), np.uint8)
    stream.start()
    stream.submit_frame(frame)
    deadline = time.monotonic() + 1
    while stream.encoded_count < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    stream.stop()
    assert stream.encoded_count >= 1
    stream.reset_statistics(clear_buffers=True)
    stream.start()
    stream.submit_frame(frame)
    deadline = time.monotonic() + 1
    while stream.encoded_count < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    stream.stop()
    statistics = stream.get_statistics()
    assert statistics["preview_submitted_count"] == 1
    assert statistics["preview_encoded_count"] == 1


def test_runtime_save_is_atomic_and_creates_backup(tmp_path) -> None:
    store = RuntimeConfigStore(touch_config(tmp_path))
    store.save_camera_override({"brightness": 10})
    store.save_camera_override({"brightness": 20})
    assert store.load_camera_override() == {"brightness": 20}
    assert list(store.config.backup_directory.glob("camera_override-*.yaml"))


def test_ui_state_never_persists_competition_output_as_enabled(tmp_path) -> None:
    store = RuntimeConfigStore(touch_config(tmp_path))
    store.save_ui_state(True)
    state = store.load_ui_state()
    assert state["competition_mode"] is False
    assert "detector" not in state


def test_atomic_save_failure_preserves_previous_file(tmp_path, monkeypatch) -> None:
    store = RuntimeConfigStore(touch_config(tmp_path))
    store.save_camera_override({"brightness": 10})
    before = store.config.camera_override_file.read_bytes()
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        store.save_camera_override({"brightness": 99})
    assert store.config.camera_override_file.read_bytes() == before


def test_restore_baseline_removes_override_only(tmp_path) -> None:
    baseline = tmp_path / "config/camera.yaml"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("device: 0\n", encoding="utf-8")
    before = baseline.read_bytes()
    store = RuntimeConfigStore(touch_config(tmp_path))
    store.save_camera_override({"brightness": 10})
    assert store.restore_baseline()
    assert not store.config.camera_override_file.exists()
    assert baseline.read_bytes() == before
