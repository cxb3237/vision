"""唯一摄像头、检测器、串口和触摸状态的可复用视觉运行时。"""

from __future__ import annotations

from collections import deque
from copy import deepcopy
import logging
import threading
import time
from types import MappingProxyType
from typing import Any, Callable

import cv2

from core.fault_manager import Fault, FaultManager
from core.models import CameraConfig, VisionResult
from core.state_machine import VisionMode
from detectors.digit_detector import DigitDetector
from detectors.steel_ball_detector import SteelBallDetector
from drivers.v4l2_controls import (
    apply_v4l2_controls,
    query_v4l2_control_info,
    read_v4l2_controls,
)
from protocol.vmc_messages import Heartbeat, MessageType
from touch_ui.api import ALLOWED_CONTROLS
from touch_ui.frame_stream import LatestFrameStream
from touch_ui.models import CommandStatus, CommandType, RuntimeCommand, TouchUIConfig
from touch_ui.runtime_config import RuntimeConfigStore
from touch_ui.state_store import StateStore


LOG = logging.getLogger(__name__)


class RuntimeCommandQueue:
    """有界命令队列；同一摄像头参数只保留最新请求。"""

    def __init__(self, maxsize: int = 64) -> None:
        if maxsize <= 0:
            raise ValueError("命令队列容量必须为正数")
        self.maxsize = maxsize
        self._lock = threading.Lock()
        self._items: deque[RuntimeCommand] = deque()

    def put(self, command: RuntimeCommand) -> list[RuntimeCommand]:
        superseded: list[RuntimeCommand] = []
        with self._lock:
            key = command.coalesce_key
            if key is not None:
                retained: deque[RuntimeCommand] = deque()
                while self._items:
                    old = self._items.popleft()
                    if old.coalesce_key == key:
                        superseded.append(old)
                    else:
                        retained.append(old)
                self._items = retained
            while len(self._items) >= self.maxsize:
                superseded.append(self._items.popleft())
            self._items.append(command)
        return superseded

    def drain(self, maximum: int = 16) -> list[RuntimeCommand]:
        result: list[RuntimeCommand] = []
        with self._lock:
            while self._items and len(result) < maximum:
                result.append(self._items.popleft())
        return result

    def snapshot(self) -> list[RuntimeCommand]:
        with self._lock:
            return list(self._items)


class VisionRuntime:
    """视觉进程的唯一资源所有者和触摸界面命令执行边界。"""

    def __init__(
        self,
        *,
        args: Any,
        mission: dict[str, Any],
        detector: Any,
        camera_service: Any,
        serial_service: Any,
        detector_id: str | int,
        camera_calibrated: bool,
        control_processor: Any,
        control_handler: Callable[[Any, Any, int], int],
        display_handler: Callable[[Any, Any, VisionResult | None, Any], tuple[bool, Any]],
        save_debug_frame: Callable[[Any], Any],
        peer_alive_checker: Callable[[dict[str, Any], float, float], bool] | None = None,
        detector_factory: Callable[[str], Any] | None = None,
        current_detector_name: str = "color",
        camera_config: CameraConfig | None = None,
        touch_config: TouchUIConfig | None = None,
        initial_competition_mode: bool = False,
        command_queue_size: int = 64,
    ) -> None:
        self.args = args
        self.mission = mission
        self.detector = detector
        self.camera_service = camera_service
        self.serial_service = serial_service
        self.detector_id = detector_id
        self.camera_calibrated = bool(camera_calibrated)
        self.control_processor = control_processor
        self.control_handler = control_handler
        self.display_handler = display_handler
        self.save_debug_frame = save_debug_frame
        self.peer_alive_checker = peer_alive_checker
        self.detector_factory = detector_factory
        self.current_detector_name = current_detector_name
        self.camera_config = camera_config
        self.touch_config = touch_config
        self.command_queue = RuntimeCommandQueue(command_queue_size)
        self.state_store = StateStore(
            {
                "runtime_running": False,
                "detector": current_detector_name,
                "target_class": 0,
                "state": "NONE",
                "confidence": 0,
                "center_x": -1,
                "center_y": -1,
                "error_x": 0,
                "error_y": 0,
                "fps": 0.0,
                "camera_online": False,
                "serial_online": False,
                "vmc_tx_count": 0,
                "mode": control_processor.state_machine.mode.name,
                "runtime_modified": False,
                "competition_mode": bool(initial_competition_mode),
                "last_error": "",
                "camera_controls": {},
                "ui": {
                    "status_poll_interval_ms": (
                        touch_config.status_poll_interval_ms if touch_config else 250
                    ),
                    "parameter_debounce_ms": (
                        touch_config.parameter_debounce_ms if touch_config else 150
                    ),
                    "exit_competition_hold_ms": (
                        touch_config.exit_competition_hold_ms if touch_config else 3000
                    ),
                },
            }
        )
        preview = touch_config
        self.frame_stream = LatestFrameStream(
            max_fps=preview.preview_max_fps if preview else 10,
            jpeg_quality=preview.jpeg_quality if preview else 80,
            max_width=preview.preview_max_width if preview else 960,
        )
        self.persistence = RuntimeConfigStore(touch_config) if touch_config else None
        self._base_controls = self._configured_controls(camera_config)
        self.startup_actual_controls = MappingProxyType({})
        self.baseline_controls = MappingProxyType(dict(self._base_controls))
        self._runtime_overrides: dict[str, int] = {}
        self._modified_controls: set[str] = set()
        self._restoring_controls = False
        self._stop_event = threading.Event()
        self._started = False
        self._camera_started = False
        self._serial_started = False
        self._preview_started = False
        self._lifecycle_lock = threading.Lock()
        self._last_camera_online: bool | None = None
        self._last_serial_online: bool | None = None
        self._last_camera_reconnects: int | None = None

    @staticmethod
    def _configured_controls(config: CameraConfig | None) -> dict[str, int]:
        profile = config.v4l2_controls if config is not None else None
        if not isinstance(profile, dict):
            return {}
        return {
            name: int(value)
            for name, value in profile.items()
            if name not in {"enabled", "strict"}
            and value is not None
            and name in ALLOWED_CONTROLS
        }

    def start(self) -> None:
        """启动唯一检测器、CameraService、串口和预览编码线程。"""

        with self._lifecycle_lock:
            if self._started:
                return
            self._stop_event.clear()
            try:
                self.detector.initialize()
                self.camera_service.start()
                self._camera_started = True
                self.serial_service.start()
                self._serial_started = bool(self.serial_service.enabled)
                if self.touch_config is not None:
                    self.frame_stream.start()
                    self._preview_started = True
                self._started = True
                self.state_store.update(runtime_running=True)
                LOG.info("VisionRuntime已启动，detector=%s", self.current_detector_name)
            except Exception:
                self._stop_resources_locked()
                raise

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop(self) -> None:
        """幂等停止，先结束视觉循环，再依次关闭预览、串口和摄像头。"""

        self._stop_event.set()
        with self._lifecycle_lock:
            self._stop_resources_locked()

    def _stop_resources_locked(self) -> None:
        if self._preview_started:
            self.frame_stream.stop()
            self._preview_started = False
        if self._serial_started:
            self.serial_service.stop()
            self._serial_started = False
        if self._camera_started:
            self.camera_service.stop()
            self._camera_started = False
        self._started = False
        self.state_store.update(runtime_running=False, camera_online=False, serial_online=False)
        LOG.info("VisionRuntime已停止")

    def submit_command(
        self,
        command_type: CommandType | str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        command = RuntimeCommand.create(command_type, payload)
        competition = bool(self.state_store.snapshot().get("competition_mode"))
        if competition and command.command_type not in {CommandType.EXIT_COMPETITION}:
            raise RuntimeError("比赛模式禁止修改运行参数")
        self.state_store.add_command(command)
        superseded = self.command_queue.put(command)
        for old in superseded:
            self.state_store.set_command_status(
                old.command_id, CommandStatus.FAILED, "已被同一参数的更新请求替代"
            )
        LOG.info("触摸命令已排队: %s id=%s", command.command_type.value, command.command_id)
        return command.command_id

    def get_status_snapshot(self) -> dict[str, Any]:
        return self.state_store.snapshot()

    def get_latest_preview_jpeg(self) -> bytes | None:
        return self.frame_stream.get_latest_jpeg(placeholder=True)

    def get_runtime_config_snapshot(self) -> dict[str, Any]:
        snapshot = self.state_store.snapshot()
        return {
            "controls": deepcopy(snapshot.get("camera_controls", {})),
            "modified": bool(snapshot.get("runtime_modified", False)),
            "override_file_active": bool(
                self.persistence and self.persistence.config.camera_override_file.exists()
            ),
        }

    def _refresh_camera_controls(self) -> None:
        names = sorted(ALLOWED_CONTROLS)
        device = self.camera_config.device if self.camera_config is not None else 0
        info = query_v4l2_control_info(device, names)
        if not self.startup_actual_controls:
            startup_actual = {
                name: int(item["actual"])
                for name, item in info.items()
                if item.get("supported") and isinstance(item.get("actual"), int)
            }
            baseline = dict(startup_actual)
            baseline.update(self._base_controls)
            self.startup_actual_controls = MappingProxyType(startup_actual)
            self.baseline_controls = MappingProxyType(baseline)
        requested = dict(self._base_controls)
        requested.update(self._runtime_overrides)
        for name in names:
            item = info[name]
            item["requested"] = requested.get(name)
            item["last_success"] = None
            item["setter_supported"] = name in ALLOWED_CONTROLS
            item["mismatch"] = (
                item.get("actual") is not None
                and item.get("requested") is not None
                and item["actual"] != item["requested"]
            )
            item["disabled"] = not item["supported"] or not item.get("writable", False)
        self.state_store.update(camera_controls=info)

    def _validate_control(self, name: str, value: int) -> dict[str, Any]:
        if name not in ALLOWED_CONTROLS:
            raise ValueError(f"不允许的摄像头控制: {name}")
        controls = self.state_store.snapshot().get("camera_controls", {})
        info = controls.get(name, {})
        if info and not info.get("supported", False):
            raise ValueError(info.get("error") or f"摄像头不支持{name}")
        minimum, maximum = info.get("minimum"), info.get("maximum")
        if minimum is not None and value < minimum or maximum is not None and value > maximum:
            raise ValueError(f"{name}超出范围{minimum}..{maximum}")
        if not self._restoring_controls and name == "white_balance_temperature":
            auto = controls.get("white_balance_automatic", {}).get("actual")
            if auto not in (None, 0):
                raise ValueError("自动白平衡开启时不能设置手动色温")
        if not self._restoring_controls and name == "exposure_absolute":
            auto = controls.get("exposure_auto", {}).get("actual")
            if auto not in (None, 1):
                raise ValueError("自动曝光开启时不能设置手动曝光")
        return info

    def _apply_camera_control(self, name: str, value: int) -> None:
        info = self._validate_control(name, value)
        device = self.camera_config.device if self.camera_config is not None else 0
        result = apply_v4l2_controls(device, {name: value}, strict=False).get(name, {})
        actual = read_v4l2_controls(device, [name]).get(name)
        success = bool(result.get("success")) and actual is not None
        error = result.get("error")
        mismatch = actual is not None and actual != value
        updated = dict(info)
        updated.update(
            {
                "name": name,
                "requested": value,
                "actual": actual,
                "last_success": success,
                "mismatch": mismatch,
                "error": error,
                "disabled": not updated.get("supported", False),
            }
        )
        self.state_store.update_nested("camera_controls", {name: updated})
        if not success:
            raise RuntimeError(error or f"{name}设置失败或无法回读")
        if not self._restoring_controls:
            self._runtime_overrides[name] = value
            self._modified_controls.add(name)
            self.state_store.update(runtime_modified=True)
        if not self._restoring_controls and name in {
            "white_balance_automatic",
            "exposure_auto",
            "focus_auto",
        }:
            self._refresh_camera_controls()
        LOG.info("V4L2现场参数 %s requested=%s actual=%s", name, value, actual)

    def _switch_detector(self, name: str) -> None:
        if self.detector_factory is None:
            raise RuntimeError("当前运行时未配置检测器工厂")
        old = self.detector
        new_detector = self.detector_factory(name)
        new_detector.initialize()
        self.detector = new_detector
        self.current_detector_name = name
        self.detector_id = name
        self.control_processor.supported_target_class = int(
            getattr(new_detector, "target_class", 0)
        )
        self.control_processor.reset_callback = getattr(new_detector, "reset", None)
        self.control_processor.tracker.reset()
        if hasattr(old, "reset"):
            old.reset()
        self.state_store.update(detector=name, last_error="")
        LOG.info("检测器已切换: %s", name)

    def _set_competition(self, enabled: bool) -> None:
        self.state_store.update(competition_mode=enabled)
        if self.persistence is not None:
            self.persistence.save_ui_state(enabled, self.current_detector_name)
        LOG.info("%s比赛模式", "进入" if enabled else "退出")

    @staticmethod
    def _ordered_control_names(values: dict[str, int]) -> list[str]:
        ordered = list(values)

        def place(first: str, second: str) -> None:
            if first not in ordered or second not in ordered:
                return
            ordered.remove(first)
            ordered.insert(ordered.index(second), first)

        if "white_balance_automatic" in values:
            if values["white_balance_automatic"] == 0:
                place("white_balance_automatic", "white_balance_temperature")
            else:
                place("white_balance_temperature", "white_balance_automatic")
        if "exposure_auto" in values:
            if values["exposure_auto"] == 1:
                place("exposure_auto", "exposure_absolute")
            else:
                place("exposure_absolute", "exposure_auto")
        if "focus_auto" in values:
            if values["focus_auto"] == 0:
                place("focus_auto", "focus_absolute")
            else:
                place("focus_absolute", "focus_auto")
        return ordered

    def _restore_controls_with_rollback(
        self,
        values: dict[str, int],
        *,
        record_as_runtime: bool = True,
    ) -> None:
        controls = self.state_store.snapshot().get("camera_controls", {})
        previous = {
            name: controls.get(name, {}).get("actual")
            for name in values
        }
        applied: list[str] = []
        ordered_names = self._ordered_control_names(values)
        old_overrides = dict(self._runtime_overrides)
        old_modified = set(self._modified_controls)
        old_restoring = self._restoring_controls
        self._restoring_controls = True
        try:
            for name in ordered_names:
                self._apply_camera_control(name, values[name])
                applied.append(name)
        except Exception:
            rollback_values = {
                name: int(previous[name])
                for name in values
                if isinstance(previous.get(name), int)
            }
            for name in self._ordered_control_names(rollback_values):
                try:
                    self._apply_camera_control(name, rollback_values[name])
                except Exception:
                    LOG.exception("恢复失败后的V4L2回滚也失败: %s", name)
            self._runtime_overrides = old_overrides
            self._modified_controls = old_modified
            raise
        else:
            if record_as_runtime:
                self._runtime_overrides.update(values)
                self._modified_controls.update(values)
        finally:
            self._restoring_controls = old_restoring

    def _execute_command(self, command: RuntimeCommand) -> None:
        self.state_store.set_command_status(command.command_id, CommandStatus.APPLYING)
        if command.command_type in {
            CommandType.RESTORE_LAST_GOOD,
            CommandType.RESTORE_BASELINE,
        }:
            LOG.info("参数恢复请求开始: %s id=%s", command.command_type.value, command.command_id)
        try:
            if (
                self.state_store.snapshot().get("competition_mode")
                and command.command_type != CommandType.EXIT_COMPETITION
            ):
                raise RuntimeError("比赛模式禁止该运行命令")
            if command.command_type == CommandType.SET_CAMERA_CONTROL:
                self._apply_camera_control(
                    str(command.payload["name"]), int(command.payload["value"])
                )
            elif command.command_type == CommandType.SELECT_DETECTOR:
                self._switch_detector(str(command.payload["detector"]))
            elif command.command_type == CommandType.SAVE_RUNTIME:
                if self.persistence is None:
                    raise RuntimeError("未启用现场配置存储")
                self.persistence.save_camera_override(self._runtime_overrides)
                self.persistence.save_ui_state(
                    bool(self.state_store.snapshot().get("competition_mode")),
                    self.current_detector_name,
                )
                self.state_store.update(runtime_modified=False)
            elif command.command_type == CommandType.RESTORE_LAST_GOOD:
                if self.persistence is None:
                    raise RuntimeError("未启用现场配置存储")
                values = self.persistence.load_camera_override()
                self._restore_controls_with_rollback(values)
                self.state_store.update(runtime_modified=False)
            elif command.command_type == CommandType.RESTORE_BASELINE:
                missing = sorted(self._modified_controls - self.baseline_controls.keys())
                if missing:
                    raise RuntimeError("缺少启动基准值: " + ", ".join(missing))
                baseline_values = {
                    name: int(self.baseline_controls[name])
                    for name in self._modified_controls
                }
                controls_before = self.state_store.snapshot().get("camera_controls", {})
                actual_before = {
                    name: int(controls_before[name]["actual"])
                    for name in baseline_values
                    if isinstance(controls_before.get(name, {}).get("actual"), int)
                }
                old_overrides = dict(self._runtime_overrides)
                old_modified = set(self._modified_controls)
                restored = False
                try:
                    self._restore_controls_with_rollback(
                        baseline_values,
                        record_as_runtime=False,
                    )
                    restored = True
                    if self.persistence is not None:
                        self.persistence.restore_baseline()
                except Exception:
                    if restored:
                        try:
                            self._restore_controls_with_rollback(
                                actual_before,
                                record_as_runtime=False,
                            )
                        except Exception:
                            LOG.exception("停用override失败后的完整V4L2状态回滚也失败")
                    self._runtime_overrides = old_overrides
                    self._modified_controls = old_modified
                    raise
                self._runtime_overrides.clear()
                self._modified_controls.clear()
                self.state_store.update(runtime_modified=False)
            elif command.command_type == CommandType.ENTER_COMPETITION:
                self._set_competition(True)
            elif command.command_type == CommandType.EXIT_COMPETITION:
                self._set_competition(False)
            self.state_store.update(last_error="")
            self.state_store.set_command_status(command.command_id, CommandStatus.APPLIED)
            if command.command_type in {
                CommandType.RESTORE_LAST_GOOD,
                CommandType.RESTORE_BASELINE,
            }:
                LOG.info("参数恢复成功: %s id=%s", command.command_type.value, command.command_id)
        except Exception as exc:
            message = str(exc)
            self.state_store.update(last_error=message)
            self.state_store.set_command_status(command.command_id, CommandStatus.FAILED, message)
            LOG.warning("触摸命令失败 %s: %s", command.command_type.value, message)

    def process_pending_commands(self) -> None:
        for command in self.command_queue.drain():
            self._execute_command(command)

    def _load_runtime_state(self) -> None:
        if self.persistence is None or self.touch_config is None:
            return
        if self.touch_config.restore_runtime_overrides:
            try:
                values = self.persistence.load_camera_override()
            except ValueError as exc:
                self.state_store.update(last_error=str(exc))
            else:
                try:
                    self._restore_controls_with_rollback(values)
                    self.state_store.update(runtime_modified=False)
                except Exception as exc:
                    self.state_store.update(last_error=f"启动恢复现场参数失败: {exc}")
                    LOG.warning("启动恢复现场参数失败，继续使用当前参数: %s", exc)

    def _update_result_state(
        self,
        result: VisionResult | None,
        fps: float,
        serial_stats: dict[str, Any],
        camera_stats: dict[str, Any],
    ) -> None:
        found = bool(result and result.found)
        target_state = int(result.target_state) if result is not None else 0
        state_names = {0: "NONE", 1: "CANDIDATE", 2: "LOCKED", 3: "LOST", 4: "OCCLUDED"}
        camera_online = bool(
            camera_stats.get("frames_ok", camera_stats.get("frames_captured", 0))
            or result is not None
        )
        serial_online = bool(serial_stats.get("port_open", False))
        if camera_online != self._last_camera_online:
            LOG.info("摄像头状态: %s", "ONLINE" if camera_online else "OFFLINE")
            self._last_camera_online = camera_online
        if serial_online != self._last_serial_online:
            LOG.info("串口状态: %s", "ONLINE" if serial_online else "OFFLINE")
            self._last_serial_online = serial_online
        self.state_store.update(
            target_class=int(result.target_class) if found and result else 0,
            state=state_names.get(target_state, "NONE"),
            confidence=int(result.confidence) if found and result else 0,
            center_x=int(result.center_x) if found and result else -1,
            center_y=int(result.center_y) if found and result else -1,
            error_x=int(result.error_x_px) if found and result else 0,
            error_y=int(result.error_y_px) if found and result else 0,
            fps=round(fps, 2),
            camera_online=camera_online,
            serial_online=serial_online,
            vmc_tx_count=int(serial_stats.get("tx_count", 0)),
            mode=self.control_processor.state_machine.mode.name,
        )

    def _reapply_overrides_after_reconnect(self, camera_stats: dict[str, Any]) -> None:
        reconnects = int(camera_stats.get("reconnects", 0))
        previous = self._last_camera_reconnects
        self._last_camera_reconnects = reconnects
        if previous is None or reconnects <= previous or not self._runtime_overrides:
            return
        try:
            self._restore_controls_with_rollback(self._runtime_overrides)
            LOG.info("摄像头重连后已重新应用现场V4L2参数")
        except Exception as exc:
            message = f"摄像头重连后恢复现场参数失败: {exc}"
            self.state_store.update(last_error=message)
            LOG.warning(message)

    def run_forever(self) -> int:
        """在调用线程运行视觉循环，Web线程只能读取快照和投递命令。"""

        if not self._started:
            self.start()
        if self.touch_config is not None:
            self._refresh_camera_controls()
            self._load_runtime_state()
        started = time.monotonic()
        last_heartbeat = float("-inf")
        last_statistics = started
        last_frame_seen = started
        last_frame_id: int | None = None
        service_sequence = 0
        processed = 0
        process_time_total = 0.0
        display = (bool(getattr(self.args, "display", False)) or bool(self.mission["display"])) and not bool(
            getattr(self.args, "headless", False)
        )
        faults = FaultManager()
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                self.process_pending_commands()
                if hasattr(self.serial_service, "raise_if_failed"):
                    self.serial_service.raise_if_failed()
                service_sequence = self.control_handler(
                    self.serial_service, self.control_processor, service_sequence
                )
                serial_stats = self.serial_service.get_statistics()
                peer_alive = (
                    self.peer_alive_checker(
                        serial_stats,
                        now,
                        self.mission["serial_link_timeout_ms"] / 1000.0,
                    )
                    if self.peer_alive_checker is not None
                    else bool(serial_stats.get("port_open", False))
                )
                if self.serial_service.enabled and not peer_alive:
                    faults.set_fault(Fault.SERIAL_LINK_DOWN)
                else:
                    faults.clear_fault(Fault.SERIAL_LINK_DOWN)
                if now - last_heartbeat >= 1.0 / self.mission["heartbeat_hz"]:
                    heartbeat = Heartbeat(
                        uptime_ms=int((now - started) * 1000) & 0xFFFFFFFF,
                        system_state=1 if faults.fault_bits() else 0,
                        active_mode=int(self.control_processor.state_machine.mode),
                        fault_bits=faults.fault_bits(),
                        rx_good_count=int(serial_stats.get("rx_good_count", 0)) & 0xFFFF,
                        rx_crc_error_count=int(serial_stats.get("rx_crc_error_count", 0)) & 0xFFFF,
                    )
                    self.serial_service.send_packet(
                        MessageType.HEARTBEAT, 0, service_sequence, heartbeat.pack()
                    )
                    service_sequence = (service_sequence + 1) & 0xFF
                    last_heartbeat = now

                frame = self.camera_service.get_latest_frame()
                if frame is None:
                    if hasattr(self.camera_service, "is_finished") and self.camera_service.is_finished():
                        break
                    if now - last_frame_seen > self.mission["camera_frame_timeout_ms"] / 1000.0:
                        faults.set_fault(Fault.CAMERA_FRAME_TIMEOUT)
                        self.state_store.update(camera_online=False)
                        if self._last_camera_online is not False:
                            LOG.warning("摄像头状态: OFFLINE（等待重连）")
                            self._last_camera_online = False
                    time.sleep(0.005)
                    continue
                if frame.frame_id == last_frame_id:
                    time.sleep(0.001)
                    continue
                last_frame_id = frame.frame_id
                last_frame_seen = now
                faults.clear_fault(Fault.CAMERA_FRAME_TIMEOUT)
                result: VisionResult | None = None
                if self.control_processor.state_machine.mode in (VisionMode.SEARCH, VisionMode.TRACK):
                    process_start = time.monotonic()
                    try:
                        detected = self.detector.process(frame)
                        result = (
                            detected
                            if isinstance(self.detector, (SteelBallDetector, DigitDetector))
                            else self.control_processor.tracker.update(detected)
                        )
                        faults.clear_fault(Fault.DETECTOR_FAILED)
                    except Exception as exc:
                        faults.set_fault(Fault.DETECTOR_FAILED)
                        self.state_store.update(last_error=str(exc))
                        LOG.exception("检测器处理失败")
                    process_time_total += time.monotonic() - process_start
                    processed += 1
                    if result is not None:
                        self.serial_service.publish_result(
                            result,
                            self.detector_id,
                            camera_calibrated=self.camera_calibrated,
                        )

                annotated = None
                if self.touch_config is not None:
                    try:
                        annotated = (
                            self.detector.draw_debug(frame.image, result)
                            if result is not None
                            else frame.image.copy()
                        )
                        self.frame_stream.submit_frame(annotated)
                    except Exception as exc:
                        self.state_store.update(last_error=f"预览生成失败: {exc}")
                        LOG.warning("预览生成失败，视觉继续运行: %s", exc)
                if display:
                    keep_running, annotated = self.display_handler(
                        frame.image, self.detector, result, self.control_processor
                    )
                    if not keep_running:
                        break
                if self.mission["save_debug_frames"]:
                    if annotated is None:
                        annotated = (
                            self.detector.draw_debug(frame.image, result)
                            if result is not None
                            else frame.image.copy()
                        )
                    self.save_debug_frame(annotated)
                camera_stats = self.camera_service.get_statistics()
                if self.touch_config is not None:
                    self._reapply_overrides_after_reconnect(camera_stats)
                fps = processed / max(now - started, 0.001)
                self._update_result_state(result, fps, serial_stats, camera_stats)
                if now - last_statistics >= self.mission["statistics_interval_s"]:
                    LOG.info(
                        "mode=%s camera_fps=%.2f vision_fps=%.2f avg_process_ms=%.2f "
                        "camera_failed=%s port_open=%s faults=0x%04X",
                        self.control_processor.state_machine.mode.name,
                        float(camera_stats.get("actual_fps", 0.0)),
                        fps,
                        1000 * process_time_total / max(processed, 1),
                        camera_stats.get("frames_failed", 0),
                        serial_stats.get("port_open", False),
                        faults.fault_bits(),
                    )
                    last_statistics = now
            return 0
        finally:
            self.stop()
            if display:
                cv2.destroyAllWindows()
