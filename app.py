"""Raspberry Pi steel-ball NCNN vision and ASCII UART application."""

from __future__ import annotations

import argparse
from dataclasses import replace
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import threading
from typing import Any

import cv2
from competition_ui.models import CompetitionUIConfig,CompetitionUIConfigError,load_competition_ui_config
from competition_ui.server import CompetitionUIServer

from core.config_loader import (
    ConfigError,
    load_camera_config,
    load_mission_config,
    load_pipe_mapping_config,
    load_steel_ball_ncnn_config,
)
from core.vision_runtime import VisionRuntime
from detectors.pipe_marker_detector import PipeMarkerDetector
from detectors.steel_ball_yolo_ncnn_detector import SteelBallYoloNcnnDetector
from detectors.target_tracker import TargetTracker
from drivers.ball_uart_client import BallUartClient
from drivers.camera_service import CameraService
from touch_ui.models import (
    TouchUIConfig,
    TouchUIConfigError,
    load_touch_ui_config,
    validate_loopback_host,
)
from touch_ui.server import TouchUIServer


LOG = logging.getLogger(__name__)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="钢球 NCNN 视觉定位与 UART2 ASCII 通信")
    parser.add_argument("--mission-config", default="config/mission.yaml")
    parser.add_argument("--camera-config", default="config/camera.yaml")
    parser.add_argument("--steel-ball-ncnn-config", default="config/steel_ball_ncnn.yaml")
    parser.add_argument("--pipe-mapping-config", default="config/pipe_mapping.yaml")
    parser.add_argument("--touch-config", default="config/touch_ui.yaml")
    parser.add_argument("--mode", choices=("track",), default="track", help=argparse.SUPPRESS)
    parser.add_argument("--display", action="store_true", help="显示 OpenCV 调试窗口")
    parser.add_argument("--touch-ui", action="store_true", help="启动本地触摸网页")
    parser.add_argument("--touch-host", default=None, help="覆盖网页监听地址")
    parser.add_argument("--touch-port", type=int, default=None, help="覆盖网页监听端口")
    parser.add_argument("--competition-ui", action="store_true")
    parser.add_argument("--competition-config", default="config/competition_ui.yaml")
    parser.add_argument("--competition-host", default=None)
    parser.add_argument("--competition-port", type=int, default=None)
    parser.add_argument("--headless", action="store_true", help="禁止 OpenCV 窗口")
    parser.add_argument("--serial-port", help="覆盖 UART 设备并启用 UART")
    parser.add_argument("--baudrate", type=int, help="覆盖 UART 波特率")
    parser.add_argument("--serial-rate", type=float, help="覆盖位置最大发送频率")
    parser.add_argument("--serial-debug", action="store_true", help="记录限频后的 ASCII UART 收发")
    parser.add_argument("--no-serial", action="store_true", help="禁用 UART 硬件")
    parser.add_argument("--competition-mode", action="store_true", help="启动后立即发送钢球位置")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def validate_ui_arguments(args: argparse.Namespace) -> None:
    if args.touch_ui and args.display:
        raise ConfigError("--touch-ui 不能与 --display 同时使用")
    if args.touch_port is not None and not 1 <= args.touch_port <= 65535:
        raise ConfigError("--touch-port 必须在 1..65535 范围内")
    if args.touch_host is not None and not args.touch_host.strip():
        raise ConfigError("--touch-host 不能为空")
    if args.competition_port is not None and not 1 <= args.competition_port <= 65535:
        raise ConfigError("--competition-port 必须在 1..65535 范围内")
    if args.competition_host is not None and not args.competition_host.strip():
        raise ConfigError("--competition-host 不能为空")


def resolve_touch_ui_config(args: argparse.Namespace, *, project_root: str | Path | None = None) -> TouchUIConfig:
    config = load_touch_ui_config(args.touch_config, project_root=project_root)
    host = config.host if args.touch_host is None else args.touch_host.strip()
    port = config.port if args.touch_port is None else args.touch_port
    try:
        host = validate_loopback_host(host)
    except TouchUIConfigError as exc:
        raise ConfigError(str(exc)) from exc
    if not 1 <= port <= 65535:
        raise ConfigError("触摸网页 port 必须在 1..65535 范围内")
    return replace(config, host=host, port=port)


def configure_touch_logging(log_path: str | Path = "logs/touch_ui.log") -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    root = logging.getLogger()
    if any(isinstance(item, RotatingFileHandler) and Path(item.baseFilename).resolve() == resolved for item in root.handlers):
        return
    handler = RotatingFileHandler(resolved, maxBytes=1_048_576, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)

def resolve_competition_ui_config(
    args: argparse.Namespace,
    *,
    project_root: str | Path | None = None,
) -> CompetitionUIConfig:
    config = load_competition_ui_config(args.competition_config, project_root=project_root)
    host = config.host if args.competition_host is None else args.competition_host.strip()
    port = config.port if args.competition_port is None else args.competition_port
    if host != "127.0.0.1":
        raise ConfigError("比赛后端只允许监听 127.0.0.1")
    if not 1 <= port <= 65535:
        raise ConfigError("比赛网页 port 必须在 1..65535 范围内")
    return replace(config, host=host, port=port)


def resolve_ball_uart_settings(args: argparse.Namespace, mission: dict[str, Any]) -> dict[str, Any]:
    profile = mission["ball_uart"]
    baudrate = profile["baudrate"] if args.baudrate is None else args.baudrate
    send_rate = profile["send_rate_hz"] if args.serial_rate is None else args.serial_rate
    if baudrate <= 0:
        raise ConfigError("--baudrate 必须为正整数")
    if send_rate <= 0:
        raise ConfigError("--serial-rate 必须为正数")
    return {
        "enabled": bool(profile["enabled"] and not args.no_serial) or bool(args.serial_port and not args.no_serial),
        "port": args.serial_port or profile["port"],
        "baudrate": int(baudrate),
        "send_rate_hz": float(send_rate),
        "timeout_s": float(profile["timeout_s"]),
        "write_timeout_s": float(profile["write_timeout_s"]),
        "reconnect_interval_s": float(profile["reconnect_interval_s"]),
        "debug_position_interval_s": float(profile["debug_position_interval_s"]),
        "statistics_interval_s": float(profile["statistics_interval_s"]),
        "debug": bool(args.serial_debug),
        "line_ending": profile["line_ending"],
        "wait_ready": bool(profile["wait_ready"]),
        "left_endpoint_px": profile["left_endpoint_px"],
        "right_endpoint_px": profile["right_endpoint_px"],
        "servo_side": profile["servo_side"],
    }


def create_detector(
    config_path: str | Path = "config/steel_ball_ncnn.yaml",
    *,
    pipe_marker_detector: Any = None,
) -> SteelBallYoloNcnnDetector:
    return SteelBallYoloNcnnDetector(
        load_steel_ball_ncnn_config(config_path),
        pipe_marker_detector=pipe_marker_detector,
    )


def _handle_display(image: Any, detector: Any, result: Any) -> tuple[bool, Any]:
    shown = detector.draw_debug(image, result)
    cv2.imshow("steel-ball vision", shown)
    key = cv2.waitKey(1) & 0xFF
    return key != ord("q"), shown


def run_application(
    args: argparse.Namespace,
    mission: dict[str, Any],
    detector: Any,
    camera_service: Any,
    ball_uart: Any,
    *,
    pipe_marker_detector: Any = None,
    pipe_mapping_config: dict[str, Any] | None = None,
    camera_config: Any = None,
    touch_config: TouchUIConfig | None = None,
    competition_config: CompetitionUIConfig | None = None,
    initial_competition_mode: bool = False,
) -> int:
    tracker = TargetTracker(
        alpha=mission["smoothing_alpha"],
        max_jump_px=mission["max_jump_px"],
        confirm_frames=mission["confirm_frames"],
        lost_frames=mission["lost_frames"],
    )
    runtime = VisionRuntime(
        args=args,
        mission=mission,
        detector=detector,
        camera_service=camera_service,
        ball_uart=ball_uart,
        tracker=tracker,
        display_handler=_handle_display,
        pipe_marker_detector=pipe_marker_detector,
        pipe_mapping_config=pipe_mapping_config,
        camera_config=camera_config,
        touch_config=touch_config,
        competition_config=competition_config,
        initial_competition_mode=initial_competition_mode,
    )
    web = TouchUIServer(runtime, touch_config) if touch_config else None
    competition_web = None
    if competition_config is not None:
        try:
            competition_web = CompetitionUIServer(runtime, competition_config)
        except Exception:
            LOG.exception("比赛网页初始化失败；视觉识别、UART和调试网页继续运行")
    old_signals: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_signals[signum] = signal.signal(signum, lambda *_: runtime.request_stop())
    try:
        runtime.start()
        if web:
            try:web.start()
            except OSError:LOG.exception("调试网页启动失败；其他服务继续")
        if competition_web:
            try:
                competition_web.start()
            except Exception:
                LOG.exception("比赛网页启动失败；视觉识别、UART和调试网页继续运行")
        return runtime.run_forever()
    finally:
        if competition_web:
            competition_web.stop()
        if web:
            web.stop()
        if getattr(runtime, "_started", False):
            runtime.stop()
        cv2.destroyAllWindows()
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        validate_ui_arguments(args)
        mission = load_mission_config(args.mission_config)
        camera_config = load_camera_config(args.camera_config)
        touch_config = None
        competition_config = None
        if args.touch_ui:
            args.headless = True
            touch_config = resolve_touch_ui_config(args)
            configure_touch_logging()
        if args.competition_ui:
            args.headless = True
            competition_config = resolve_competition_ui_config(args)
        elif args.touch_ui:
            try:
                automatic_competition_config = resolve_competition_ui_config(args)
                if automatic_competition_config.enabled:
                    competition_config = automatic_competition_config
                    LOG.info("触摸模式已按配置自动启用比赛网站后端")
            except (CompetitionUIConfigError, ConfigError, OSError):
                LOG.exception("比赛网站配置加载失败；视觉识别、UART和调试网页继续运行")
        pipe_mapping_config = load_pipe_mapping_config(args.pipe_mapping_config)
        pipe_marker_detector = PipeMarkerDetector(pipe_mapping_config)
        detector = create_detector(args.steel_ball_ncnn_config)
        if hasattr(detector, "pipe_marker_detector"):
            detector.pipe_marker_detector = pipe_marker_detector
        camera = CameraService(camera_config)
        uart_settings = resolve_ball_uart_settings(args, mission)
        uart = BallUartClient(uart_settings.pop("port"), uart_settings.pop("baudrate"), **uart_settings)
        return run_application(
            args,
            mission,
            detector,
            camera,
            uart,
            pipe_marker_detector=pipe_marker_detector,
            pipe_mapping_config=pipe_mapping_config,
            camera_config=camera_config,
            touch_config=touch_config,
            competition_config=competition_config,
            initial_competition_mode=bool(
                args.competition_mode
                or getattr(touch_config, "startup_competition_mode", False)
            ),
        )
    except (ConfigError,TouchUIConfigError,CompetitionUIConfigError,ValueError,OSError) as exc:
        LOG.error("启动失败: %s", exc)
        return 2
    except Exception:
        LOG.exception("未处理的应用错误")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
