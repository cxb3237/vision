"""小车视觉主程序；默认无 GUI 且不访问串口硬件。"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import logging
from pathlib import Path
import signal
import threading
import time
from typing import Any

import cv2

from core.config_loader import (
    ConfigError,
    load_camera_config,
    load_calibration_config,
    load_color_config,
    load_digit_config,
    load_mission_config,
    load_shape_config,
    load_steel_ball_config,
)
from core.fault_manager import Fault, FaultManager
from core.models import ColorClass, DetectorConfig, VisionResult
from core.state_machine import VisionMode, VisionStateMachine
from detectors.base_detector import BaseDetector
from detectors.color_detector import ColorDetector
from detectors.digit_detector import DigitDetector
from detectors.shape_detector import ShapeDetector
from detectors.steel_ball_detector import SteelBallDetector
from detectors.target_tracker import TargetTracker
from drivers.camera_service import CameraService
from drivers.serial_service import SerialService
from protocol.vmc_messages import (
    Ack,
    AckResult,
    Flags,
    Heartbeat,
    MessageType,
    VisionControl,
)
from protocol.vmc_protocol import VmcPacket
from tools.mock_camera import MockCamera


LOG = logging.getLogger(__name__)
SUPPORTED_RUNTIME_MODES = {
    VisionMode.IDLE,
    VisionMode.SEARCH,
    VisionMode.TRACK,
    VisionMode.CALIBRATION,
}


def build_argument_parser() -> argparse.ArgumentParser:
    """创建主程序命令行解析器。"""

    parser = argparse.ArgumentParser(description="电子设计竞赛小车视觉模块")
    parser.add_argument("--mission-config", default="config/mission.yaml")
    parser.add_argument("--camera-config", default="config/camera.yaml")
    parser.add_argument("--colors-config", default="config/colors.yaml")
    parser.add_argument("--shapes-config", default="config/shapes.yaml")
    parser.add_argument("--steel-ball-config", default="config/steel_ball.yaml")
    parser.add_argument("--digit-config", default="config/digit.yaml")
    parser.add_argument("--calibration-config", default="config/calibration.yaml")
    parser.add_argument(
        "--mode",
        choices=("idle", "search", "track", "calibration", "recognize", "measure"),
    )
    parser.add_argument("--detector", choices=("color", "shape", "steel_ball", "digit"))
    parser.add_argument("--target", help="目标颜色名称")
    parser.add_argument("--video", help="用视频文件或图片目录替代真实摄像头")
    parser.add_argument("--video-loop", action="store_true", help="循环模拟视频源")
    parser.add_argument("--display", action="store_true", help="显示画面；q/s/i/t 可操作")
    parser.add_argument("--serial", action="store_true", help="明确启用串口")
    parser.add_argument("--serial-port", help="覆盖串口并同时启用串口")
    parser.add_argument("--baudrate", type=int, help="覆盖串口波特率")
    parser.add_argument("--serial-rate", type=float, help="覆盖视觉结果发送频率 Hz")
    parser.add_argument("--serial-debug", action="store_true", help="记录发送包十六进制")
    parser.add_argument("--no-serial", action="store_true", help="完全禁用串口硬件")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def create_detector(
    detector_name: str,
    target: str,
    colors: dict[str, dict[str, Any]],
    mission: dict[str, Any],
    shapes_config: str | Path = "config/shapes.yaml",
    steel_ball_config: str | Path = "config/steel_ball.yaml",
    calibration_config: str | Path = "config/calibration.yaml",
    digit_config: str | Path = "config/digit.yaml",
) -> BaseDetector:
    """创建检测器；数字与钢球自行维护专用时序状态。"""

    if detector_name == "shape":
        return ShapeDetector(config=load_shape_config(shapes_config))
    if detector_name == "steel_ball":
        return SteelBallDetector(
            load_steel_ball_config(steel_ball_config),
            load_calibration_config(calibration_config),
        )
    if detector_name == "digit":
        return DigitDetector(
            load_digit_config(digit_config),
            require_complete_templates=True,
        )
    if target not in colors:
        raise ConfigError(f"目标颜色不存在: {target}; 可选: {', '.join(colors)}")
    color_class = ColorClass.from_name(target)
    if color_class == ColorClass.UNKNOWN:
        raise ConfigError(f"颜色 {target} 没有稳定协议类别")
    config = DetectorConfig.from_color_config(
        colors[target],
        confirm_frames=mission["confirm_frames"],
        lost_frames=mission["lost_frames"],
        max_jump_px=mission["max_jump_px"],
        smoothing_alpha=mission["smoothing_alpha"],
    )
    return ColorDetector(
        colors[target],
        config,
        target_class=int(color_class),
        temporal_tracking=False,
    )


def create_camera_source(args: argparse.Namespace, mission: dict[str, Any]):
    """创建真实摄像头服务或可结束的模拟视频源。"""

    if args.video:
        loop = args.video_loop or bool(mission["video_loop"])
        return MockCamera(args.video, loop=loop)
    return CameraService(load_camera_config(args.camera_config))


def resolve_serial_settings(
    args: argparse.Namespace,
    mission: dict[str, Any],
) -> dict[str, Any]:
    """合并串口配置；命令行优先，视频回放必须显式启用串口。"""

    explicit = bool(getattr(args, "serial", False)) or bool(
        getattr(args, "serial_port", None)
    )
    enabled = explicit or (
        bool(mission["serial_enabled"]) and not bool(getattr(args, "video", None))
    )
    if bool(getattr(args, "no_serial", False)):
        enabled = False
    requested_baudrate = getattr(args, "baudrate", None)
    requested_send_rate = getattr(args, "serial_rate", None)
    baudrate = (
        mission["serial_baudrate"]
        if requested_baudrate is None
        else requested_baudrate
    )
    send_rate = (
        mission["serial_send_rate_hz"]
        if requested_send_rate is None
        else requested_send_rate
    )
    if baudrate <= 0:
        raise ConfigError("baudrate 必须为正整数")
    if send_rate <= 0:
        raise ConfigError("serial-rate 必须为正数")
    return {
        "enabled": enabled,
        "port": getattr(args, "serial_port", None) or mission["serial_port"],
        "baudrate": int(baudrate),
        "send_rate_hz": float(send_rate),
        "reconnect_delay": float(mission["serial_reconnect_interval_s"]),
        "queue_size": int(mission["serial_queue_size"]),
        "strict": bool(mission["serial_strict"]),
        "serial_debug": bool(getattr(args, "serial_debug", False)),
    }


def is_peer_alive(
    statistics: dict[str, Any],
    now: float,
    timeout_s: float,
) -> bool:
    """按有效对端包判断链路，端口刚打开时给予一个超时周期宽限。"""

    if not statistics.get("port_open", False):
        return False
    candidates = (
        statistics.get("last_heartbeat_monotonic"),
        statistics.get("last_valid_packet_monotonic"),
    )
    valid_times = [float(value) for value in candidates if value is not None]
    if valid_times:
        return now - max(valid_times) <= timeout_s
    opened = statistics.get("port_opened_monotonic")
    return opened is not None and now - float(opened) <= timeout_s


class ControlProcessor:
    """幂等处理 VISION_CONTROL，并确保模式切换重置 Tracker。"""

    def __init__(
        self,
        state_machine: VisionStateMachine,
        tracker: TargetTracker,
        supported_target_class: int = 0,
        cache_size: int = 128,
        reset_callback: Any | None = None,
    ) -> None:
        self.state_machine = state_machine
        self.tracker = tracker
        self.supported_target_class = supported_target_class
        self.cache_size = cache_size
        self.reset_callback = reset_callback
        self._results: OrderedDict[tuple[int, int], tuple[AckResult, int]] = OrderedDict()

    def set_mode(self, mode: VisionMode) -> bool:
        """切换模式，进入或离开 TRACK 时清理旧跟踪状态。"""

        old_mode = self.state_machine.mode
        changed = self.state_machine.set_mode(mode)
        if changed and old_mode != self.state_machine.mode:
            if old_mode == VisionMode.TRACK or self.state_machine.mode == VisionMode.TRACK:
                self.tracker.reset()
                if self.reset_callback is not None:
                    self.reset_callback()
        return changed

    def process(self, packet_sequence: int, control: VisionControl) -> tuple[AckResult, int]:
        """处理一次控制请求；重复 SEQ+request_id 返回缓存结果且无副作用。"""

        key = (packet_sequence, control.request_id)
        if key in self._results:
            return self._results[key]
        try:
            requested_mode = VisionMode(control.mode)
        except ValueError:
            result = (AckResult.INVALID_PARAMETER, 0)
        else:
            unsupported_modes = {
                VisionMode.RECOGNIZE,
                VisionMode.MEASURE,
                VisionMode.AIM,
                VisionMode.RETURN_CENTER,
                VisionMode.FAULT,
            }
            if control.options != 0:
                result = (AckResult.INVALID_PARAMETER, 1)
            elif requested_mode in unsupported_modes:
                result = (AckResult.UNSUPPORTED, 0)
            elif control.target_class not in (0, self.supported_target_class):
                result = (AckResult.UNSUPPORTED, 2)
            elif requested_mode not in SUPPORTED_RUNTIME_MODES:
                result = (AckResult.UNSUPPORTED, 0)
            elif self.set_mode(requested_mode):
                result = (AckResult.OK, 0)
                LOG.info("串口切换视觉模式: %s", self.state_machine.mode.name)
            else:
                result = (AckResult.INVALID_PARAMETER, 0)
        self._results[key] = result
        while len(self._results) > self.cache_size:
            self._results.popitem(last=False)
        return result


def _send_ack(
    serial_service: SerialService,
    packet: VmcPacket,
    result: AckResult,
    detail: int,
    sequence: int,
) -> int:
    payload = Ack(packet.message_type, packet.sequence, int(result), detail).pack()
    serial_service.send_packet(MessageType.ACK, int(Flags.URGENT), sequence, payload)
    return (sequence + 1) & 0xFF


def _handle_control_messages(
    serial_service: SerialService,
    processor: ControlProcessor,
    ack_sequence: int,
) -> int:
    while True:
        packet = serial_service.get_message()
        if packet is None:
            return ack_sequence
        if packet.message_type != MessageType.VISION_CONTROL:
            continue
        ack_requested = Flags.ACK_REQ in Flags(packet.flags)
        try:
            control = VisionControl.unpack(packet.payload)
            result, detail = processor.process(packet.sequence, control)
        except (ValueError, TypeError):
            result, detail = AckResult.INVALID_PARAMETER, 0
        if ack_requested:
            ack_sequence = _send_ack(
                serial_service,
                packet,
                result,
                detail,
                ack_sequence,
            )


def _handle_display(
    image,
    detector: BaseDetector,
    result: VisionResult | None,
    processor: ControlProcessor,
) -> tuple[bool, Any]:
    shown = detector.draw_debug(image, result) if result is not None else image.copy()
    cv2.putText(
        shown,
        f"mode={processor.state_machine.mode.name}",
        (10, shown.shape[0] - 12),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    cv2.imshow("vision", shown)
    key = cv2.waitKey(1) & 0xFF
    if key == ord("q"):
        return False, shown
    if key == ord("s"):
        _save_debug_frame(shown)
    elif key == ord("i"):
        processor.set_mode(VisionMode.IDLE)
    elif key == ord("t"):
        processor.set_mode(VisionMode.TRACK)
    return True, shown


def _save_debug_frame(image) -> Path:
    output = Path("data/debug") / f"frame_{time.time_ns()}.jpg"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output), image):
        raise OSError(f"保存调试帧失败: {output}")
    LOG.info("已保存调试帧: %s", output)
    return output


def run_application(
    args: argparse.Namespace,
    mission: dict[str, Any],
    detector: BaseDetector,
    camera_source,
    serial_service: SerialService,
    detector_id: str | int = 0,
    camera_calibrated: bool = False,
) -> int:
    """运行主循环；所有启动操作均受 finally 和成功标志保护。"""

    initial_mode = VisionMode[(args.mode or mission["default_mode"]).upper()]
    if initial_mode not in SUPPORTED_RUNTIME_MODES:
        raise ConfigError(f"当前版本不支持模式: {initial_mode.name}")
    state_machine = VisionStateMachine(initial_mode)
    tracker = TargetTracker(
        alpha=mission["smoothing_alpha"],
        max_jump_px=mission["max_jump_px"],
        confirm_frames=mission["confirm_frames"],
        lost_frames=mission["lost_frames"],
    )
    target_class = int(getattr(detector, "target_class", 0))
    processor = ControlProcessor(
        state_machine,
        tracker,
        target_class,
        reset_callback=getattr(detector, "reset", None),
    )
    faults = FaultManager()
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    old_sigint = signal.signal(signal.SIGINT, request_stop)
    old_sigterm = signal.signal(signal.SIGTERM, request_stop)
    started = time.monotonic()
    last_heartbeat = float("-inf")
    last_statistics = started
    last_frame_seen = started
    last_frame_id: int | None = None
    service_sequence = 0
    processed = 0
    process_time_total = 0.0
    display = args.display or bool(mission["display"])
    camera_started = False
    serial_started = False
    try:
        detector.initialize()
        camera_source.start()
        camera_started = True
        serial_service.start()
        serial_started = serial_service.enabled
        while not stop_event.is_set():
            now = time.monotonic()
            if hasattr(serial_service, "raise_if_failed"):
                serial_service.raise_if_failed()
            service_sequence = _handle_control_messages(
                serial_service,
                processor,
                service_sequence,
            )
            serial_stats = serial_service.get_statistics()
            peer_alive = is_peer_alive(
                serial_stats,
                now,
                mission["serial_link_timeout_ms"] / 1000.0,
            )
            if serial_service.enabled and not peer_alive:
                faults.set_fault(Fault.SERIAL_LINK_DOWN)
            else:
                faults.clear_fault(Fault.SERIAL_LINK_DOWN)
            if now - last_heartbeat >= 1.0 / mission["heartbeat_hz"]:
                heartbeat = Heartbeat(
                    uptime_ms=int((now - started) * 1000) & 0xFFFFFFFF,
                    system_state=1 if faults.fault_bits() else 0,
                    active_mode=int(state_machine.mode),
                    fault_bits=faults.fault_bits(),
                    rx_good_count=int(serial_stats["rx_good_count"]) & 0xFFFF,
                    rx_crc_error_count=int(serial_stats["rx_crc_error_count"]) & 0xFFFF,
                )
                serial_service.send_packet(
                    MessageType.HEARTBEAT,
                    0,
                    service_sequence,
                    heartbeat.pack(),
                )
                service_sequence = (service_sequence + 1) & 0xFF
                last_heartbeat = now

            frame = camera_source.get_latest_frame()
            if frame is None:
                if hasattr(camera_source, "is_finished") and camera_source.is_finished():
                    LOG.info("视频模拟源已结束")
                    break
                if now - last_frame_seen > mission["camera_frame_timeout_ms"] / 1000.0:
                    faults.set_fault(Fault.CAMERA_FRAME_TIMEOUT)
                time.sleep(0.005)
                continue
            if frame.frame_id == last_frame_id:
                time.sleep(0.001)
                continue
            last_frame_id = frame.frame_id
            last_frame_seen = now
            faults.clear_fault(Fault.CAMERA_FRAME_TIMEOUT)
            result: VisionResult | None = None
            if state_machine.mode in (VisionMode.SEARCH, VisionMode.TRACK):
                process_start = time.monotonic()
                try:
                    detected = detector.process(frame)
                    result = (
                        detected
                        if isinstance(detector, (SteelBallDetector, DigitDetector))
                        else tracker.update(detected)
                    )
                    faults.clear_fault(Fault.DETECTOR_FAILED)
                except Exception:
                    faults.set_fault(Fault.DETECTOR_FAILED)
                    LOG.exception("检测器处理失败")
                process_time_total += time.monotonic() - process_start
                processed += 1
                if result is not None:
                    serial_service.publish_result(
                        result,
                        detector_id,
                        camera_calibrated=camera_calibrated,
                    )
            annotated = None
            if display:
                keep_running, annotated = _handle_display(
                    frame.image,
                    detector,
                    result,
                    processor,
                )
                if not keep_running:
                    break
            if mission["save_debug_frames"]:
                if annotated is None:
                    annotated = (
                        detector.draw_debug(frame.image, result)
                        if result is not None
                        else frame.image.copy()
                    )
                _save_debug_frame(annotated)
            if now - last_statistics >= mission["statistics_interval_s"]:
                camera_stats = camera_source.get_statistics()
                LOG.info(
                    "mode=%s camera_fps=%.2f vision_fps=%.2f avg_process_ms=%.2f "
                    "camera_failed=%s port_open=%s peer_alive=%s faults=0x%04X",
                    state_machine.mode.name,
                    float(camera_stats.get("actual_fps", 0.0)),
                    processed / max(now - started, 0.001),
                    1000 * process_time_total / max(processed, 1),
                    camera_stats.get("frames_failed", 0),
                    serial_stats["port_open"],
                    peer_alive,
                    faults.fault_bits(),
                )
                last_statistics = now
        return 0
    finally:
        if serial_started:
            serial_service.stop()
        if camera_started:
            camera_source.stop()
        cv2.destroyAllWindows()
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)


def main(argv: list[str] | None = None) -> int:
    """加载配置、创建组件并把错误转换为清晰的非零退出码。"""

    args = build_argument_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        mission = load_mission_config(
            args.mission_config,
            colors_path=args.colors_config,
        )
        colors = load_color_config(args.colors_config)
        detector = create_detector(
            args.detector or mission["detector"],
            args.target or mission["target_color"],
            colors,
            mission,
            args.shapes_config,
            args.steel_ball_config,
            args.calibration_config,
            args.digit_config,
        )
        camera_source = create_camera_source(args, mission)
        serial_settings = resolve_serial_settings(args, mission)
        serial_service = SerialService(
            serial_settings.pop("port"),
            serial_settings.pop("baudrate"),
            **serial_settings,
        )
        calibration = load_calibration_config(args.calibration_config)
        return run_application(
            args,
            mission,
            detector,
            camera_source,
            serial_service,
            detector_id=args.detector or mission["detector"],
            camera_calibrated=calibration.calibrated,
        )
    except (ConfigError, ValueError, OSError) as exc:
        LOG.error("启动失败: %s", exc)
        return 2
    except Exception:
        LOG.exception("未处理的应用错误")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
