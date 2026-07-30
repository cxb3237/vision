"""Single-purpose steel-ball NCNN vision runtime."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import logging
import math
import threading
import time
from types import MappingProxyType
from typing import Any, Callable
from competition_ui.media_service import CompetitionMediaService
from competition_ui.models import CompetitionUIConfig

from core.models import CameraConfig, VisionResult
from core.performance_metrics import RollingRate, RollingSamples
from detectors.pipe_marker_detector import compute_ball_position_mm
from drivers.v4l2_controls import apply_v4l2_controls, query_v4l2_control_info, read_v4l2_controls
from touch_ui.api import ALLOWED_CONTROLS
from touch_ui.frame_stream import LatestFrameStream
from touch_ui.models import CommandStatus, CommandType, RuntimeCommand, TouchUIConfig
from touch_ui.runtime_config import RuntimeConfigStore
from touch_ui.state_store import StateStore


LOG = logging.getLogger(__name__)


class RuntimeCommandQueue:
    """Bounded command queue which coalesces edits to the same camera control."""

    def __init__(self, maxsize: int = 64) -> None:
        if maxsize <= 0:
            raise ValueError("command queue size must be positive")
        self.maxsize = maxsize
        self._lock = threading.Lock()
        self._items: deque[RuntimeCommand] = deque()

    def put(self, command: RuntimeCommand) -> list[RuntimeCommand]:
        removed: list[RuntimeCommand] = []
        with self._lock:
            if command.coalesce_key is not None:
                keep: deque[RuntimeCommand] = deque()
                for old in self._items:
                    if old.coalesce_key == command.coalesce_key:
                        removed.append(old)
                    else:
                        keep.append(old)
                self._items = keep
            while len(self._items) >= self.maxsize:
                removed.append(self._items.popleft())
            self._items.append(command)
        return removed

    def drain(self, maximum: int = 16) -> list[RuntimeCommand]:
        result: list[RuntimeCommand] = []
        with self._lock:
            while self._items and len(result) < maximum:
                result.append(self._items.popleft())
        return result


class VisionRuntime:
    """Own the one camera, one NCNN detector and one ASCII ball UART client."""

    def __init__(
        self,
        *,
        args: Any,
        mission: dict[str, Any],
        detector: Any,
        camera_service: Any,
        ball_uart: Any,
        tracker: Any,
        display_handler: Callable[[Any, Any, VisionResult], tuple[bool, Any]],
        pipe_marker_detector: Any = None,
        pipe_mapping_config: dict[str, Any] | None = None,
        camera_config: CameraConfig | None = None,
        touch_config: TouchUIConfig | None = None,
        competition_config: CompetitionUIConfig | None = None,
        initial_competition_mode: bool = False,
        command_queue_size: int = 64,
    ) -> None:
        self.args = args
        self.mission = mission
        self.detector = detector
        self.camera_service = camera_service
        self.ball_uart = ball_uart
        self.tracker = tracker
        self.display_handler = display_handler
        self.pipe_marker_detector = pipe_marker_detector
        self.pipe_mapping_config = pipe_mapping_config
        self.camera_config = camera_config
        self.touch_config = touch_config
        self.competition_config = competition_config
        self.command_queue = RuntimeCommandQueue(command_queue_size)
        self.persistence = RuntimeConfigStore(touch_config) if touch_config else None
        self.frame_stream = LatestFrameStream(
            max_fps=touch_config.preview_max_fps if touch_config else 10,
            jpeg_quality=touch_config.jpeg_quality if touch_config else 80,
            max_width=touch_config.preview_max_width if touch_config else 960,
        )
        self.competition_media_service = (
            CompetitionMediaService(camera_service, competition_config)
            if competition_config
            else None
        )
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._started = False
        self._camera_started = False
        self._uart_started = False
        self._preview_started = False
        self._competition_media_started = False
        self._last_frame_id: int | None = None
        self._vision_rate = RollingRate(window_seconds=2.0, max_events=512)
        self._capture_to_result = RollingSamples(max_samples=120)
        self._runtime_overrides: dict[str, int] = {}
        self._modified_controls: set[str] = set()
        self._base_controls = self._configured_controls(camera_config)
        self.startup_actual_controls = MappingProxyType({})
        self.baseline_controls = MappingProxyType(dict(self._base_controls))
        competition = bool(initial_competition_mode)
        calibration = self._ball_calibration_status(
            camera_config.width if camera_config is not None else None
        )
        self.state_store = StateStore(
            {
                "runtime_running": False,
                "detector": "steel_ball_yolo_ncnn",
                "state": "NONE",
                "confidence": 0,
                "center_x": -1,
                "center_y": -1,
                "ball_x_px": None,
                "ball_x_mm": None,
                "ball_position_mm": None,
                "marker_a": None,
                "marker_b": None,
                **calibration,
                "camera_online": False,
                "latest_frame_age_s": None,
                "camera_fps": 0.0,
                "vision_fps": 0.0,
                "fps": 0.0,
                "serial_online": False,
                "uart_port_open": False,
                "mcu_ready": False,
                "uart_state": "串口未打开",
                "mcu_status": {},
                "last_uart_error": "",
                "last_sent_position_mm": None,
                "position_tx_count": 0,
                "position_tx_hz": 0.0,
                "invalid_tx_count": 0,
                "invalid_tx_hz": 0.0,
                "competition_mode": competition,
                "vision_output_enabled": competition,
                "runtime_modified": False,
                "camera_controls": {},
                "preview_fps": 0.0,
                "preview_overwritten_count": 0,
                "capture_to_result_ms": 0.0,
                "capture_to_result_p95_ms": 0.0,
                "vision_processed_count": 0,
                "vision_skipped_camera_frames": 0,
                "last_error": "",
                "ui": {
                    "status_poll_interval_ms": touch_config.status_poll_interval_ms if touch_config else 250,
                    "parameter_debounce_ms": touch_config.parameter_debounce_ms if touch_config else 150,
                    "exit_competition_hold_ms": touch_config.exit_competition_hold_ms if touch_config else 3000,
                },
            }
        )

    def _ball_calibration_status(self, image_width: int | None = None) -> dict[str, Any]:
        profile = self.mission.get("ball_uart", {})
        if self.pipe_mapping_config is not None:
            enabled = bool(self.pipe_mapping_config.get("enabled", False))
            return {
                "ball_position_calibrated": enabled,
                "ball_position_calibration_error": (
                    "" if enabled else "动态水管坐标映射已禁用"
                ),
                "left_endpoint_px": profile.get("left_endpoint_px"),
                "right_endpoint_px": profile.get("right_endpoint_px"),
                "servo_side": profile.get("servo_side"),
            }
        configured = bool(profile.get("calibrated", False))
        left = profile.get("left_endpoint_px")
        right = profile.get("right_endpoint_px")
        servo_side = profile.get("servo_side")
        error = ""
        try:
            left_value = float(left)
            right_value = float(right)
        except (TypeError, ValueError, OverflowError):
            left_value = right_value = float("nan")
        if not configured:
            error = "未标定：请完成现场端点标定并设置 calibrated=true"
        elif not math.isfinite(left_value) or not math.isfinite(right_value):
            error = "标定端点必须为有限数值"
        elif left_value == right_value:
            error = "左右标定端点不能相同"
        elif servo_side not in {"left", "right"}:
            error = "servo_side 只能是 left 或 right"
        elif image_width is not None and (
            image_width <= 0
            or not 0.0 <= left_value < image_width
            or not 0.0 <= right_value < image_width
        ):
            error = f"标定端点必须位于图像宽度 0..{max(0, image_width - 1)} 内"
        return {
            "ball_position_calibrated": configured and not error,
            "ball_position_calibration_error": error,
            "left_endpoint_px": left,
            "right_endpoint_px": right,
            "servo_side": servo_side,
        }

    @staticmethod
    def _configured_controls(config: CameraConfig | None) -> dict[str, int]:
        profile = config.v4l2_controls if config else None
        if not isinstance(profile, dict):
            return {}
        return {
            name: int(value)
            for name, value in profile.items()
            if name not in {"enabled", "strict"}
            and name in ALLOWED_CONTROLS
            and value is not None
        }

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            try:
                self.detector.initialize()
                self.camera_service.start()
                self._camera_started = True
                self.ball_uart.start()
                self._uart_started = bool(self.ball_uart.enabled)
                if self.touch_config:
                    self.frame_stream.start()
                    self._preview_started = True
                if self.competition_media_service:
                    try:
                        self.competition_media_service.start()
                        self._competition_media_started = True
                    except Exception:
                        LOG.exception(
                            "比赛媒体服务启动失败；视觉识别、UART和调试网页继续运行"
                        )
                if self.state_store.snapshot()["competition_mode"]:
                    self.ball_uart.discard_pending_ball_position()
                    self.ball_uart.send_start()
                self._refresh_camera_controls()
                self._started = True
                self.state_store.update(runtime_running=True, **self._detector_status())
            except Exception:
                self._stop_resources()
                raise

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lifecycle_lock:
            self._stop_resources()

    def _stop_resources(self) -> None:
        if self._competition_media_started:
            try:
                self.competition_media_service.stop()
            except Exception:
                LOG.exception("比赛媒体服务停止时发生错误")
            self._competition_media_started = False
        if self._preview_started:
            self.frame_stream.stop()
            self._preview_started = False
        if self._uart_started:
            self.ball_uart.close()
            self._uart_started = False
        if self._camera_started:
            self.camera_service.stop()
            self._camera_started = False
        self.detector.close()
        self._started = False
        self.state_store.update(runtime_running=False, camera_online=False, serial_online=False)

    def submit_command(self, command_type: CommandType | str, payload: dict[str, Any] | None = None) -> str:
        command = RuntimeCommand.create(command_type, payload)
        competition = bool(self.state_store.snapshot().get("competition_mode"))
        if competition and command.command_type != CommandType.EXIT_COMPETITION:
            raise RuntimeError("比赛模式禁止修改运行参数")
        self.state_store.add_command(command)
        for old in self.command_queue.put(command):
            self.state_store.set_command_status(old.command_id, CommandStatus.FAILED, "已被更新请求替代")
        return command.command_id

    def get_status_snapshot(self) -> dict[str, Any]:
        return self.state_store.snapshot()

    def get_latest_preview_jpeg(self) -> bytes | None:
        return self.frame_stream.get_latest_jpeg(placeholder=True)

    def get_runtime_config_snapshot(self) -> dict[str, Any]:
        snapshot = self.state_store.snapshot()
        return {
            "controls": deepcopy(snapshot.get("camera_controls", {})),
            "modified": bool(snapshot.get("runtime_modified")),
            "override_file_active": bool(self.persistence and self.persistence.config.camera_override_file.exists()),
        }

    def _refresh_camera_controls(self) -> None:
        device = self.camera_config.device if self.camera_config else 0
        info = query_v4l2_control_info(device, sorted(ALLOWED_CONTROLS))
        if not self.startup_actual_controls:
            startup = {
                name: int(item["actual"])
                for name, item in info.items()
                if item.get("supported") and isinstance(item.get("actual"), int)
            }
            baseline = dict(startup)
            baseline.update(self._base_controls)
            self.startup_actual_controls = MappingProxyType(startup)
            self.baseline_controls = MappingProxyType(baseline)
        requested = dict(self._base_controls)
        requested.update(self._runtime_overrides)
        for name, item in info.items():
            item["requested"] = requested.get(name)
            item["setter_supported"] = name in ALLOWED_CONTROLS
            item["mismatch"] = item.get("actual") is not None and item.get("requested") is not None and item["actual"] != item["requested"]
            item["disabled"] = not item.get("supported") or not item.get("writable", False)
        self.state_store.update(camera_controls=info)

    def _set_camera_control(self, name: str, value: int) -> None:
        if name not in ALLOWED_CONTROLS:
            raise ValueError(f"不允许的摄像头控制: {name}")
        device = self.camera_config.device if self.camera_config else 0
        result = apply_v4l2_controls(device, {name: value}, strict=False).get(name, {})
        actual = read_v4l2_controls(device, [name]).get(name)
        success = bool(result.get("success")) and actual is not None
        current = self.state_store.snapshot().get("camera_controls", {}).get(name, {})
        updated = dict(current)
        updated.update(requested=value, actual=actual, last_success=success, mismatch=actual is not None and actual != value, error=result.get("error"))
        self.state_store.update_nested("camera_controls", {name: updated})
        if not success:
            raise RuntimeError(result.get("error") or f"{name} 设置失败")
        self._runtime_overrides[name] = value
        self._modified_controls.add(name)
        self.state_store.update(runtime_modified=True)
        if name in {"white_balance_automatic", "exposure_auto", "focus_auto"}:
            self._refresh_camera_controls()

    def _restore_controls(self, values: dict[str, int]) -> None:
        before = read_v4l2_controls(self.camera_config.device if self.camera_config else 0, list(values))
        applied: list[str] = []
        try:
            for name, value in values.items():
                self._set_camera_control(name, int(value))
                applied.append(name)
        except Exception:
            rollback = {name: before[name] for name in applied if before.get(name) is not None}
            if rollback:
                apply_v4l2_controls(self.camera_config.device if self.camera_config else 0, rollback, strict=False)
            raise

    def _set_competition(self, enabled: bool) -> None:
        if enabled:
            calibration = self.state_store.snapshot()
            if not calibration.get("ball_position_calibrated", False):
                reason = calibration.get("ball_position_calibration_error") or "位置尚未标定"
                raise RuntimeError(f"未标定，禁止启用位置下发：{reason}")
            self.ball_uart.discard_pending_ball_position()
            self.state_store.update(competition_mode=True, vision_output_enabled=True)
            self.ball_uart.send_start()
            LOG.info("比赛模式位置输出已启用")
        else:
            self.state_store.update(competition_mode=False, vision_output_enabled=False)
            self.ball_uart.discard_pending_ball_position()
            self.ball_uart.send_stop()
            LOG.info("比赛模式位置输出已禁用")
        if self.persistence:
            self.persistence.save_ui_state(enabled)

    def process_pending_commands(self) -> None:
        for command in self.command_queue.drain():
            self.state_store.set_command_status(command.command_id, CommandStatus.APPLYING)
            try:
                if command.command_type == CommandType.SET_CAMERA_CONTROL:
                    self._set_camera_control(str(command.payload["name"]), int(command.payload["value"]))
                elif command.command_type == CommandType.SAVE_RUNTIME:
                    if not self.persistence:
                        raise RuntimeError("触摸运行配置未启用")
                    self.persistence.save_camera_override(self._runtime_overrides)
                elif command.command_type == CommandType.RESTORE_LAST_GOOD:
                    if not self.persistence:
                        raise RuntimeError("触摸运行配置未启用")
                    self._restore_controls(self.persistence.load_camera_override())
                    self._refresh_camera_controls()
                elif command.command_type == CommandType.RESTORE_BASELINE:
                    values = {name: self.baseline_controls[name] for name in self._modified_controls if name in self.baseline_controls}
                    self._restore_controls(values)
                    self._runtime_overrides.clear()
                    self._modified_controls.clear()
                    if self.persistence:
                        self.persistence.restore_baseline()
                    self.state_store.update(runtime_modified=False)
                    self._refresh_camera_controls()
                elif command.command_type == CommandType.ENTER_COMPETITION:
                    self._set_competition(True)
                elif command.command_type == CommandType.EXIT_COMPETITION:
                    self._set_competition(False)
                self.state_store.set_command_status(command.command_id, CommandStatus.APPLIED)
            except Exception as exc:
                self.state_store.set_command_status(command.command_id, CommandStatus.FAILED, str(exc))
                self.state_store.update(last_error=str(exc))

    def _detector_status(self) -> dict[str, Any]:
        try:
            return dict(self.detector.get_runtime_status())
        except Exception as exc:
            return {"detector_error": str(exc)}

    def _update_service_status(self) -> None:
        camera = self.camera_service.get_statistics()
        uart = self.ball_uart.get_statistics()
        preview = self.frame_stream.get_statistics()
        latest_frame_age = camera.get("latest_frame_age_s")
        camera_running = bool(camera.get("running", False))
        if hasattr(self.camera_service, "is_running"):
            camera_running = bool(self.camera_service.is_running())
        age_is_valid = (
            isinstance(latest_frame_age, (int, float))
            and not isinstance(latest_frame_age, bool)
            and math.isfinite(float(latest_frame_age))
            and float(latest_frame_age) >= 0.0
        )
        camera_online = (
            camera_running
            and age_is_valid
            and float(latest_frame_age)
            < float(self.mission.get("camera_online_timeout_s", 1.0))
        )
        self.state_store.update(
            camera_online=camera_online,
            latest_frame_age_s=float(latest_frame_age) if age_is_valid else None,
            camera_fps=float(camera.get("actual_fps", 0.0)),
            uart_port_open=bool(uart.get("connected")),
            # UART transport availability and MCU readiness are distinct states.
            serial_online=bool(uart.get("connected")),
            mcu_ready=bool(uart.get("mcu_ready")),
            uart_state=uart.get("uart_state", "串口未打开"),
            mcu_status=uart.get("mcu_status", {}),
            last_uart_error=uart.get("last_uart_error", ""),
            last_sent_position_mm=uart.get("last_sent_position_mm"),
            position_tx_count=int(uart.get("position_tx_count", 0)),
            position_tx_hz=float(uart.get("position_tx_hz", 0.0)),
            invalid_tx_count=int(uart.get("invalid_tx_count", 0)),
            invalid_tx_hz=float(uart.get("invalid_tx_hz", 0.0)),
            preview_fps=float(preview.get("preview_fps", 0.0)),
            preview_overwritten_count=int(preview.get("preview_overwritten_count", 0)),
            **self._detector_status(),
        )

    def run_forever(self) -> int:
        if not self._started:
            raise RuntimeError("VisionRuntime must be started before run_forever")
        try:
            while not self._stop_event.is_set():
                self.process_pending_commands()
                frame = self.camera_service.get_latest_frame(copy_image=False)
                if frame is None or frame.frame_id == self._last_frame_id:
                    self._update_service_status()
                    self._stop_event.wait(0.002)
                    continue
                if self._last_frame_id is not None and frame.frame_id > self._last_frame_id + 1:
                    skipped = frame.frame_id - self._last_frame_id - 1
                    current = self.state_store.snapshot().get("vision_skipped_camera_frames", 0)
                    self.state_store.update(vision_skipped_camera_frames=current + skipped)
                self._last_frame_id = frame.frame_id
                result = self.tracker.update(self.detector.process(frame))
                marker_a = marker_b = None
                if self.pipe_marker_detector is not None:
                    marker_a, marker_b = self.pipe_marker_detector.detect(frame.image)
                result.marker_a_x = marker_a[0] if marker_a is not None else None
                result.marker_a_y = marker_a[1] if marker_a is not None else None
                result.marker_b_x = marker_b[0] if marker_b is not None else None
                result.marker_b_y = marker_b[1] if marker_b is not None else None
                if result.found and marker_a is not None and marker_b is not None:
                    result.ball_position_mm = compute_ball_position_mm(
                        marker_a,
                        marker_b,
                        (result.center_x, result.center_y),
                        float(self.pipe_mapping_config["marker_a"]["position_mm"]),
                        float(self.pipe_mapping_config["marker_b"]["position_mm"]),
                    )
                now = time.monotonic()
                latency = max(0.0, (now - frame.capture_timestamp) * 1000.0)
                self._capture_to_result.add(latency)
                self._vision_rate.record(now)
                x_px = int(result.center_x) if result.found else None
                image_width = int(frame.image.shape[1]) if frame.image.ndim >= 2 else 0
                calibration = self._ball_calibration_status(image_width)
                x_mm = None
                if self.pipe_mapping_config is not None:
                    x_mm = result.ball_position_mm
                elif x_px is not None and calibration["ball_position_calibrated"]:
                    try:
                        x_mm = self.ball_uart.pixel_x_to_mm(x_px)
                    except (TypeError, ValueError, OverflowError) as exc:
                        calibration["ball_position_calibrated"] = False
                        calibration["ball_position_calibration_error"] = str(exc)
                competition = bool(self.state_store.snapshot().get("competition_mode"))
                if competition:
                    if x_mm is None:
                        self.ball_uart.send_invalid()
                    else:
                        self.ball_uart.publish_ball_position(int(round(x_mm)))
                else:
                    self.ball_uart.discard_pending_ball_position()
                summary = self._capture_to_result.summary()
                processed = int(self.state_store.snapshot().get("vision_processed_count", 0)) + 1
                self.state_store.update(
                    state=getattr(result.target_state, "name", str(result.target_state)),
                    confidence=int(result.confidence),
                    center_x=x_px if x_px is not None else -1,
                    center_y=int(result.center_y) if result.found else -1,
                    ball_x_px=x_px,
                    ball_x_mm=round(x_mm, 1) if x_mm is not None else None,
                    ball_position_mm=round(x_mm, 1) if x_mm is not None else None,
                    marker_a=(
                        {"x": marker_a[0], "y": marker_a[1]}
                        if marker_a is not None
                        else None
                    ),
                    marker_b=(
                        {"x": marker_b[0], "y": marker_b[1]}
                        if marker_b is not None
                        else None
                    ),
                    vision_fps=self._vision_rate.rate(now),
                    fps=self._vision_rate.rate(now),
                    capture_to_result_ms=latency,
                    capture_to_result_p95_ms=float(summary["p95"]),
                    vision_processed_count=processed,
                    **calibration,
                    **self._detector_status(),
                )
                annotated = None
                if self._preview_started or (bool(getattr(self.args, "display", False)) and not bool(getattr(self.args, "headless", False))):
                    annotated = self.detector.draw_debug(frame.image, result)
                if self._preview_started and annotated is not None:
                    self.frame_stream.submit_frame(annotated)
                if bool(getattr(self.args, "display", False)) and not bool(getattr(self.args, "headless", False)):
                    keep_running, _ = self.display_handler(frame.image, self.detector, result)
                    if not keep_running:
                        break
                self._update_service_status()
            return 0
        finally:
            self.stop()
