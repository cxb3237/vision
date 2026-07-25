"""小车视觉主程序；默认无 GUI 且不访问串口硬件。"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import replace
import logging
from logging.handlers import RotatingFileHandler
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
from core.vision_runtime import VisionRuntime
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
from touch_ui.models import TouchUIConfig, TouchUIConfigError, load_touch_ui_config
from touch_ui.runtime_config import RuntimeConfigStore
from touch_ui.server import TouchUIServer


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
    parser.add_argument("--touch-ui", action="store_true", help="启动本地触摸Web界面")
    parser.add_argument("--touch-host", default=None, help="覆盖触摸Web服务绑定地址")
    parser.add_argument("--touch-port", type=int, default=None, help="覆盖触摸Web服务端口")
    parser.add_argument("--touch-config", default="config/touch_ui.yaml")
    parser.add_argument("--headless", action="store_true", help="禁止所有OpenCV窗口")
    parser.add_argument(
        "--competition-mode", action="store_true", help="启动时直接进入比赛锁定界面"
    )
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


def create_camera_source(
    args: argparse.Namespace,
    mission: dict[str, Any],
    camera_config=None,
):
    """创建真实摄像头服务或可结束的模拟视频源。"""

    if args.video:
        loop = args.video_loop or bool(mission["video_loop"])
        return MockCamera(args.video, loop=loop)
    return CameraService(camera_config or load_camera_config(args.camera_config))


def validate_ui_arguments(args: argparse.Namespace) -> None:
    if bool(getattr(args, "touch_ui", False)) and bool(getattr(args, "display", False)):
        raise ConfigError("--touch-ui不能与--display同时使用")
    port = getattr(args, "touch_port", None)
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise ConfigError("--touch-port必须在1..65535范围内")
    host = getattr(args, "touch_host", None)
    if host is not None and (not isinstance(host, str) or not host.strip()):
        raise ConfigError("--touch-host必须为非空地址")


def resolve_touch_ui_config(
    args: argparse.Namespace,
    *,
    project_root: str | Path | None = None,
) -> TouchUIConfig:
    """按“命令行明确值 > YAML”合并并验证触摸服务地址。"""

    config = load_touch_ui_config(args.touch_config, project_root=project_root)
    host = config.host if args.touch_host is None else args.touch_host
    port = config.port if args.touch_port is None else args.touch_port
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("触摸Web服务host必须为非空地址")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("触摸Web服务port必须在1..65535范围内")
    return replace(config, host=host.strip(), port=port)


def configure_touch_logging(log_path: str | Path = "logs/touch_ui.log") -> None:
    """增加有限大小的触摸界面轮转日志，不改变现有控制台日志。"""

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    root = logging.getLogger()
    if any(
        isinstance(handler, RotatingFileHandler)
        and Path(getattr(handler, "baseFilename", "")).resolve() == resolved
        for handler in root.handlers
    ):
        return
    handler = RotatingFileHandler(
        resolved,
        maxBytes=1_048_576,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)


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
    detector_factory: Any | None = None,
    current_detector_name: str = "color",
    camera_config: Any | None = None,
    touch_config: TouchUIConfig | None = None,
    initial_competition_mode: bool = False,
) -> int:
    """创建唯一VisionRuntime并运行；旧命令行模式仍经过同一安全生命周期。"""

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
    runtime = VisionRuntime(
        args=args,
        mission=mission,
        detector=detector,
        camera_service=camera_source,
        serial_service=serial_service,
        detector_id=detector_id,
        camera_calibrated=camera_calibrated,
        control_processor=processor,
        control_handler=_handle_control_messages,
        display_handler=_handle_display,
        save_debug_frame=_save_debug_frame,
        peer_alive_checker=is_peer_alive,
        detector_factory=detector_factory,
        current_detector_name=current_detector_name,
        camera_config=camera_config,
        touch_config=touch_config,
        initial_competition_mode=initial_competition_mode,
    )
    touch_server = TouchUIServer(runtime, touch_config) if touch_config is not None else None
    old_sigint = None
    old_sigterm = None
    if threading.current_thread() is threading.main_thread():
        old_sigint = signal.signal(signal.SIGINT, lambda *_: runtime.request_stop())
        old_sigterm = signal.signal(signal.SIGTERM, lambda *_: runtime.request_stop())
    try:
        runtime.start()
        if touch_server is not None:
            try:
                touch_server.start()
            except Exception:
                LOG.exception("触摸界面启动失败；视觉和串口继续运行")
        return runtime.run_forever()
    finally:
        if touch_server is not None:
            touch_server.stop()
        runtime.stop()
        if old_sigint is not None:
            signal.signal(signal.SIGINT, old_sigint)
        if old_sigterm is not None:
            signal.signal(signal.SIGTERM, old_sigterm)


def main(argv: list[str] | None = None) -> int:
    """加载配置、创建组件并把错误转换为清晰的非零退出码。"""

    args = build_argument_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        validate_ui_arguments(args)
        mission = load_mission_config(
            args.mission_config,
            colors_path=args.colors_config,
        )
        colors = load_color_config(args.colors_config)
        touch_config: TouchUIConfig | None = None
        restored_ui_state: dict[str, Any] = {}
        if args.touch_ui:
            args.headless = True
            touch_config = resolve_touch_ui_config(args)
            configure_touch_logging()
            if touch_config.restore_runtime_overrides:
                try:
                    restored_ui_state = RuntimeConfigStore(touch_config).load_ui_state()
                except ValueError as exc:
                    LOG.warning("忽略无效的触摸UI运行状态: %s", exc)
        detector_name = args.detector or mission["detector"]
        if args.touch_ui and args.detector is None and touch_config is not None:
            restored_detector = restored_ui_state.get("detector")
            detector_name = (
                restored_detector
                if restored_detector in {"color", "shape", "steel_ball", "digit"}
                else touch_config.startup_detector
            )
        detector = create_detector(
            detector_name,
            args.target or mission["target_color"],
            colors,
            mission,
            args.shapes_config,
            args.steel_ball_config,
            args.calibration_config,
            args.digit_config,
        )
        camera_config = load_camera_config(args.camera_config)
        camera_source = create_camera_source(args, mission, camera_config)
        serial_settings = resolve_serial_settings(args, mission)
        serial_service = SerialService(
            serial_settings.pop("port"),
            serial_settings.pop("baudrate"),
            **serial_settings,
        )
        calibration = load_calibration_config(args.calibration_config)

        def detector_factory(name: str):
            return create_detector(
                name,
                args.target or mission["target_color"],
                colors,
                mission,
                args.shapes_config,
                args.steel_ball_config,
                args.calibration_config,
                args.digit_config,
            )

        competition_mode = bool(args.competition_mode)
        if touch_config is not None and not competition_mode:
            if touch_config.restore_runtime_overrides:
                competition_mode = bool(
                    restored_ui_state.get(
                        "competition_mode", touch_config.startup_competition_mode
                    )
                )
            else:
                competition_mode = touch_config.startup_competition_mode
        return run_application(
            args,
            mission,
            detector,
            camera_source,
            serial_service,
            detector_id=detector_name,
            camera_calibrated=calibration.calibrated,
            detector_factory=detector_factory,
            current_detector_name=detector_name,
            camera_config=camera_config,
            touch_config=touch_config,
            initial_competition_mode=competition_mode,
        )
    except (ConfigError, TouchUIConfigError, ValueError, OSError) as exc:
        LOG.error("启动失败: %s", exc)
        return 2
    except Exception:
        LOG.exception("未处理的应用错误")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
