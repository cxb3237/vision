"""VisionRuntime和本地Web服务的无硬件集成测试。"""

from __future__ import annotations

from argparse import Namespace
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
from types import MappingProxyType
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np
import pytest

import app
import core.vision_runtime as runtime_module
import touch_ui.server as touch_server_module
from app import ControlProcessor, _handle_control_messages, _handle_display, _save_debug_frame
from core.config_loader import ConfigError, load_camera_config, load_mission_config
from core.models import FramePacket, TargetState, VisionResult
from core.state_machine import VisionMode, VisionStateMachine
from core.vision_runtime import RuntimeCommandQueue, VisionRuntime
from detectors.target_tracker import TargetTracker
from touch_ui.frame_stream import LatestFrameStream
from touch_ui.models import CommandType, RuntimeCommand, load_touch_ui_config
from touch_ui.server import TouchUIServer


PROJECT_CONFIG = Path(__file__).resolve().parents[1] / "config/touch_ui.yaml"


class FakeDetector:
    target_class = 1

    def __init__(self, *, fail_initialize: bool = False) -> None:
        self.initialize_count = 0
        self.reset_count = 0
        self.fail_initialize = fail_initialize

    def initialize(self) -> None:
        self.initialize_count += 1
        if self.fail_initialize:
            raise RuntimeError("detector init failed")

    def reset(self) -> None:
        self.reset_count += 1

    def process(self, frame: FramePacket) -> VisionResult:
        return VisionResult(
            frame.frame_id,
            frame.capture_timestamp,
            time.monotonic(),
            found=True,
            target_state=TargetState.LOCKED,
            target_class=1,
            center_x=10,
            center_y=10,
            confidence=900,
            image_width=frame.image.shape[1],
            image_height=frame.image.shape[0],
        )

    def draw_debug(self, image, _result):
        return image.copy()


class FakeCamera:
    def __init__(self, frames=None, finished: bool = False) -> None:
        self.frames = list(frames or [])
        self.finished = finished
        self.start_count = 0
        self.stop_count = 0

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def get_latest_frame(self):
        if self.frames:
            return self.frames.pop(0)
        return None

    def is_finished(self) -> bool:
        return self.finished and not self.frames

    def get_statistics(self):
        return {"frames_captured": 1, "frames_failed": 0, "actual_fps": 30.0}


class FakeSerial:
    enabled = False

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.published = []

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def get_message(self):
        return None

    def get_statistics(self):
        return {"port_open": False, "tx_count": len(self.published)}

    def send_packet(self, *_args, **_kwargs):
        return False

    def publish_result(self, result, detector_id, **_kwargs):
        self.published.append((result, detector_id))
        return True


def _touch_config(tmp_path: Path):
    return load_touch_ui_config(PROJECT_CONFIG, project_root=tmp_path)


def _runtime(
    tmp_path: Path,
    *,
    detector=None,
    camera=None,
    serial=None,
    detector_factory=None,
    touch=True,
    args=None,
) -> VisionRuntime:
    detector = detector or FakeDetector()
    camera = camera or FakeCamera(finished=True)
    serial = serial or FakeSerial()
    mission = load_mission_config()
    processor = ControlProcessor(
        VisionStateMachine(VisionMode.TRACK), TargetTracker(), supported_target_class=1
    )
    return VisionRuntime(
        args=args or Namespace(mode="track", display=False, headless=True),
        mission=mission,
        detector=detector,
        camera_service=camera,
        serial_service=serial,
        detector_id="color",
        camera_calibrated=False,
        control_processor=processor,
        control_handler=_handle_control_messages,
        display_handler=_handle_display,
        save_debug_frame=_save_debug_frame,
        detector_factory=detector_factory,
        current_detector_name="color",
        camera_config=load_camera_config(),
        touch_config=_touch_config(tmp_path) if touch else None,
    )


def test_touch_and_display_conflict_is_rejected() -> None:
    args = app.build_argument_parser().parse_args(["--touch-ui", "--display"])
    with pytest.raises(ConfigError, match="不能与--display"):
        app.validate_ui_arguments(args)


def test_non_touch_arguments_remain_compatible() -> None:
    args = app.build_argument_parser().parse_args(["--mode", "track", "--no-serial"])
    app.validate_ui_arguments(args)
    assert not args.touch_ui and args.no_serial and args.mode == "track"


def test_touch_port_uses_yaml_when_cli_is_omitted(tmp_path) -> None:
    config_path = tmp_path / "touch_ui.yaml"
    config_path.write_text(
        PROJECT_CONFIG.read_text(encoding="utf-8").replace("port: 8765", "port: 9000"),
        encoding="utf-8",
    )
    args = app.build_argument_parser().parse_args(
        ["--touch-ui", "--touch-config", str(config_path)]
    )
    assert app.resolve_touch_ui_config(args, project_root=tmp_path).port == 9000


def test_touch_port_cli_explicitly_overrides_yaml(tmp_path) -> None:
    config_path = tmp_path / "touch_ui.yaml"
    config_path.write_text(
        PROJECT_CONFIG.read_text(encoding="utf-8").replace("port: 8765", "port: 9000"),
        encoding="utf-8",
    )
    args = app.build_argument_parser().parse_args(
        [
            "--touch-ui",
            "--touch-config",
            str(config_path),
            "--touch-port",
            "8765",
        ]
    )
    assert app.resolve_touch_ui_config(args, project_root=tmp_path).port == 8765


def test_create_camera_source_constructs_only_one_camera_service(monkeypatch) -> None:
    created = []

    class CountingCamera:
        def __init__(self, config) -> None:
            created.append(config)

    monkeypatch.setattr(app, "CameraService", CountingCamera)
    args = Namespace(video=None, camera_config="config/camera.yaml")
    source = app.create_camera_source(args, load_mission_config(), load_camera_config())
    assert isinstance(source, CountingCamera) and len(created) == 1


def test_runtime_start_stop_is_idempotent_and_owns_single_resources(tmp_path) -> None:
    detector, camera, serial = FakeDetector(), FakeCamera(), FakeSerial()
    runtime = _runtime(tmp_path, detector=detector, camera=camera, serial=serial)
    runtime.start()
    runtime.start()
    runtime.stop()
    runtime.stop()
    assert detector.initialize_count == 1
    assert camera.start_count == camera.stop_count == 1
    assert serial.start_count == 1 and serial.stop_count == 0


def test_headless_prevents_display_handler(tmp_path) -> None:
    frame = FramePacket(1, time.monotonic(), np.zeros((30, 40, 3), np.uint8))
    camera = FakeCamera([frame], finished=True)
    runtime = _runtime(
        tmp_path,
        camera=camera,
        touch=False,
        args=Namespace(mode="track", display=True, headless=True),
    )
    called = []
    runtime.display_handler = lambda *_args: (called.append(True) or (True, None))
    assert runtime.run_forever() == 0
    assert not called and camera.stop_count == 1


def test_command_queue_coalesces_same_parameter_only() -> None:
    queue = RuntimeCommandQueue(maxsize=4)
    first = RuntimeCommand.create(CommandType.SET_CAMERA_CONTROL, {"name": "gain", "value": 1})
    second = RuntimeCommand.create(CommandType.SELECT_DETECTOR, {"detector": "digit"})
    latest = RuntimeCommand.create(CommandType.SET_CAMERA_CONTROL, {"name": "gain", "value": 8})
    assert queue.put(first) == []
    queue.put(second)
    superseded = queue.put(latest)
    assert superseded == [first]
    assert queue.drain() == [second, latest]


def test_competition_mode_is_checked_in_backend(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    enter = RuntimeCommand.create(CommandType.ENTER_COMPETITION)
    runtime._execute_command(enter)
    with pytest.raises(RuntimeError, match="比赛模式"):
        runtime.submit_command(CommandType.SELECT_DETECTOR, {"detector": "digit"})
    exit_id = runtime.submit_command(CommandType.EXIT_COMPETITION)
    runtime.process_pending_commands()
    assert runtime.get_status_snapshot()["commands"][exit_id]["status"] == "APPLIED"
    assert not runtime.get_status_snapshot()["competition_mode"]


def test_detector_switch_failure_keeps_old_detector(tmp_path) -> None:
    old = FakeDetector()
    runtime = _runtime(
        tmp_path,
        detector=old,
        detector_factory=lambda _name: FakeDetector(fail_initialize=True),
    )
    command = RuntimeCommand.create(CommandType.SELECT_DETECTOR, {"detector": "digit"})
    runtime.state_store.add_command(command)
    runtime._execute_command(command)
    assert runtime.detector is old and runtime.current_detector_name == "color"
    assert runtime.state_store.command_snapshot(command.command_id)["status"] == "FAILED"


def test_camera_apply_records_requested_actual_mismatch(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    runtime.state_store.update(
        camera_controls={
            "brightness": {
                "supported": True,
                "minimum": 0,
                "maximum": 100,
                "actual": 10,
            }
        }
    )
    monkeypatch.setattr(
        runtime_module,
        "apply_v4l2_controls",
        lambda *_args, **_kwargs: {
            "brightness": {"success": True, "error": None}
        },
    )
    monkeypatch.setattr(
        runtime_module, "read_v4l2_controls", lambda *_args, **_kwargs: {"brightness": 19}
    )
    command = RuntimeCommand.create(
        CommandType.SET_CAMERA_CONTROL, {"name": "brightness", "value": 20}
    )
    runtime.state_store.add_command(command)
    runtime._execute_command(command)
    info = runtime.get_runtime_config_snapshot()["controls"]["brightness"]
    assert info["requested"] == 20 and info["actual"] == 19 and info["mismatch"]
    assert runtime.state_store.command_snapshot(command.command_id)["status"] == "APPLIED"


def test_restore_failure_rolls_back_already_applied_controls(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    current = {"brightness": 10, "contrast": 20}

    def fake_apply(name: str, value: int) -> None:
        if name == "contrast" and value == 40:
            raise RuntimeError("contrast failed")
        current[name] = value

    monkeypatch.setattr(runtime, "_apply_camera_control", fake_apply)
    runtime.state_store.update(
        camera_controls={
            name: {"supported": True, "actual": value}
            for name, value in current.items()
        }
    )
    with pytest.raises(RuntimeError, match="contrast failed"):
        runtime._restore_controls_with_rollback(
            {"brightness": 30, "contrast": 40}
        )
    assert current == {"brightness": 10, "contrast": 20}


def test_restore_baseline_includes_startup_control_absent_from_yaml(
    tmp_path, monkeypatch
) -> None:
    runtime = _runtime(tmp_path)
    assert "gain" not in runtime._base_controls
    current = {"gain": 7}

    def fake_query(_device, names):
        return {
            name: {
                "name": name,
                "supported": name == "gain",
                "minimum": 0 if name == "gain" else None,
                "maximum": 255 if name == "gain" else None,
                "step": 1 if name == "gain" else None,
                "default": 0 if name == "gain" else None,
                "actual": current["gain"] if name == "gain" else None,
                "error": None if name == "gain" else "unsupported",
            }
            for name in names
        }

    def fake_apply(_device, controls, strict=False):
        name, value = next(iter(controls.items()))
        current[name] = value
        return {name: {"success": True, "error": None}}

    monkeypatch.setattr(runtime_module, "query_v4l2_control_info", fake_query)
    monkeypatch.setattr(runtime_module, "apply_v4l2_controls", fake_apply)
    monkeypatch.setattr(
        runtime_module,
        "read_v4l2_controls",
        lambda _device, names: {name: current.get(name) for name in names},
    )
    runtime._refresh_camera_controls()
    runtime._apply_camera_control("gain", 20)
    assert current["gain"] == 20 and runtime.startup_actual_controls["gain"] == 7
    command = RuntimeCommand.create(CommandType.RESTORE_BASELINE)
    runtime.state_store.add_command(command)
    runtime._execute_command(command)
    assert current["gain"] == 7
    assert not runtime._runtime_overrides and not runtime._modified_controls
    assert runtime.state_store.command_snapshot(command.command_id)["status"] == "APPLIED"


def test_restore_exposure_order_depends_on_target_auto_state(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    calls = []
    monkeypatch.setattr(
        runtime,
        "_apply_camera_control",
        lambda name, value: calls.append((name, value)),
    )
    runtime.state_store.update(
        camera_controls={
            "exposure_auto": {"supported": True, "actual": 3},
            "exposure_absolute": {"supported": True, "actual": 120},
        }
    )
    runtime._restore_controls_with_rollback(
        {"exposure_absolute": 200, "exposure_auto": 1}
    )
    assert calls[:2] == [("exposure_auto", 1), ("exposure_absolute", 200)]
    calls.clear()
    runtime._restore_controls_with_rollback(
        {"exposure_auto": 3, "exposure_absolute": 120}
    )
    assert calls[:2] == [("exposure_absolute", 120), ("exposure_auto", 3)]


@pytest.mark.parametrize(
    ("command_type", "target_values"),
    [
        (
            CommandType.RESTORE_BASELINE,
            {
                "brightness": 0,
                "gain": 7,
                "white_balance_automatic": 0,
                "white_balance_temperature": 5000,
                "exposure_auto": 1,
                "exposure_absolute": 120,
            },
        ),
        (
            CommandType.RESTORE_LAST_GOOD,
            {
                "brightness": 6,
                "gain": 9,
                "white_balance_automatic": 0,
                "white_balance_temperature": 4800,
                "exposure_auto": 1,
                "exposure_absolute": 160,
            },
        ),
    ],
)
def test_restore_rebuild_source_has_requested_actual_and_clean_state(
    tmp_path, monkeypatch, command_type, target_values
) -> None:
    runtime = _runtime(tmp_path)
    current = {name: value + 1 for name, value in target_values.items()}
    runtime.state_store.update(
        camera_controls={
            name: {
                "name": name,
                "supported": True,
                "minimum": 0,
                "maximum": 10000,
                "step": 1,
                "requested": value,
                "actual": value,
                "mismatch": False,
            }
            for name, value in current.items()
        },
        runtime_modified=True,
    )
    runtime._runtime_overrides = dict(current)
    runtime._modified_controls = set(current)

    def fake_apply(_device, controls, strict=False):
        name, value = next(iter(controls.items()))
        current[name] = value
        return {name: {"success": True, "error": None}}

    monkeypatch.setattr(runtime_module, "apply_v4l2_controls", fake_apply)
    monkeypatch.setattr(
        runtime_module,
        "read_v4l2_controls",
        lambda _device, names: {name: current[name] for name in names},
    )
    if command_type == CommandType.RESTORE_BASELINE:
        runtime.baseline_controls = MappingProxyType(dict(target_values))
    else:
        runtime.persistence.save_camera_override(target_values)

    command = RuntimeCommand.create(command_type)
    runtime.state_store.add_command(command)
    runtime._execute_command(command)
    camera = runtime.get_runtime_config_snapshot()
    for name, value in target_values.items():
        assert camera["controls"][name]["requested"] == value
        assert camera["controls"][name]["actual"] == value
    assert not camera["modified"]
    assert runtime.state_store.command_snapshot(command.command_id)["status"] == "APPLIED"


def test_camera_reconnect_reapplies_runtime_overrides(tmp_path, monkeypatch) -> None:
    runtime = _runtime(tmp_path)
    runtime._runtime_overrides = {"brightness": 30}
    reapplied = []
    monkeypatch.setattr(
        runtime,
        "_restore_controls_with_rollback",
        lambda values: reapplied.append(dict(values)),
    )
    runtime._reapply_overrides_after_reconnect({"reconnects": 0})
    runtime._reapply_overrides_after_reconnect({"reconnects": 1})
    assert reapplied == [{"brightness": 30}]


def test_request_stop_models_ctrl_c_and_systemd_shutdown(tmp_path) -> None:
    camera = FakeCamera(finished=False)
    runtime = _runtime(tmp_path, camera=camera, touch=False)
    thread = threading.Thread(target=runtime.run_forever)
    thread.start()
    deadline = time.monotonic() + 1
    while camera.start_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    runtime.request_stop()
    thread.join(1.0)
    assert not thread.is_alive() and camera.stop_count == 1


class ServerRuntime:
    def __init__(self) -> None:
        self.frame_stream = LatestFrameStream()
        self.state = {"runtime_running": True, "competition_mode": False}
        self.stop_requests = 0

    def get_status_snapshot(self):
        return dict(self.state)

    def get_runtime_config_snapshot(self):
        return {"controls": {}}

    def get_latest_preview_jpeg(self):
        return self.frame_stream.get_latest_jpeg()

    def submit_command(self, *_args, **_kwargs):
        return "id"

    def request_stop(self):
        self.stop_requests += 1


def test_web_server_never_creates_video_capture_and_disconnect_is_harmless(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        cv2,
        "VideoCapture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("forbidden")),
    )
    runtime = ServerRuntime()
    config = replace(_touch_config(tmp_path), host="127.0.0.1", port=0)
    server = TouchUIServer(runtime, config)
    server.start()
    port = server._server.server_address[1]
    with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
        payload = json.loads(response.read())
    server.stop()
    assert payload["ok"] and runtime.state["runtime_running"]


def _post_json(url: str, body: dict):
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.loads(response.read())


def test_runtime_stop_endpoint_is_parameterless_and_idempotent(tmp_path) -> None:
    runtime = ServerRuntime()
    config = replace(_touch_config(tmp_path), host="127.0.0.1", port=0)
    server = TouchUIServer(runtime, config)
    server.start()
    port = server._server.server_address[1]
    status, first = _post_json(f"http://127.0.0.1:{port}/api/runtime/stop", {})
    _, second = _post_json(f"http://127.0.0.1:{port}/api/runtime/stop", {})
    deadline = time.monotonic() + 1
    while runtime.stop_requests == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    server.stop()
    assert status == 202 and first["status"] == "STOPPING"
    assert second["status"] == "ALREADY_STOPPING"
    assert runtime.stop_requests == 1


def test_runtime_stop_and_kiosk_exit_reject_client_commands_or_pid(tmp_path) -> None:
    runtime = ServerRuntime()
    config = replace(_touch_config(tmp_path), host="127.0.0.1", port=0)
    server = TouchUIServer(runtime, config)
    server.start()
    port = server._server.server_address[1]
    for endpoint, body in (
        ("/api/runtime/stop", {"command": "shutdown"}),
        ("/api/kiosk/exit", {"pid": 1}),
    ):
        with pytest.raises(HTTPError) as raised:
            _post_json(f"http://127.0.0.1:{port}{endpoint}", body)
        assert raised.value.code == 400
    server.stop()


def test_kiosk_exit_does_not_stop_vision_backend(tmp_path, monkeypatch) -> None:
    runtime = ServerRuntime()
    config = replace(_touch_config(tmp_path), host="127.0.0.1", port=0)
    monkeypatch.setattr(touch_server_module, "exit_kiosk", lambda _path: 4321)
    server = TouchUIServer(runtime, config)
    server.start()
    port = server._server.server_address[1]
    status, body = _post_json(f"http://127.0.0.1:{port}/api/kiosk/exit", {})
    server.stop()
    assert status == 200 and body["status"] == "EXITING"
    assert runtime.stop_requests == 0


def test_runtime_stop_closes_enabled_camera_and_serial_once(tmp_path) -> None:
    class EnabledSerial(FakeSerial):
        enabled = True

    camera = FakeCamera(finished=False)
    serial = EnabledSerial()
    runtime = _runtime(tmp_path, camera=camera, serial=serial, touch=False)
    thread = threading.Thread(target=runtime.run_forever)
    thread.start()
    deadline = time.monotonic() + 1
    while camera.start_count == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    runtime.request_stop()
    runtime.request_stop()
    thread.join(1)
    assert not thread.is_alive()
    assert camera.stop_count == 1 and serial.stop_count == 1
