"""Asynchronous ASCII UART client for the MSPM0 steel-ball controller."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import logging
import math
import threading
import time
from typing import Any, Callable

from core.performance_metrics import RollingRate


LOG = logging.getLogger(__name__)
READY_LINE = "READY BALL UART2 9600"
STATUS_FIELDS = frozenset({"S", "F", "EN", "X", "V", "E", "RQ", "AP", "PW", "AGE", "ST", "AC", "RJ"})
START_COMMAND = b"BALL START\r\n"
STOP_COMMAND = b"BALL STOP\r\n"


class BallUartState(str, Enum):
    CLOSED = "串口未打开"
    READY = "串口已连接（无握手）"
    START_REQUESTED = "已请求闭环启动"
    RUNNING = "闭环运行中"
    STOPPED = "闭环已停止"
    FAULT = "串口故障"


@dataclass(frozen=True, slots=True)
class BallUartReply:
    kind: str
    line: str
    fields: dict[str, int | str]


def encode_position(position_mm: int) -> bytes:
    if isinstance(position_mm, bool) or not isinstance(position_mm, int):
        raise TypeError("position_mm must be an integer")
    if not -125 <= position_mm <= 125:
        raise ValueError("position_mm must be in -125..125")
    return f"BALL POS {position_mm}\r\n".encode("ascii")


def encode_command(command: str) -> bytes:
    allowed = {"INVALID", "START", "STOP", "PING", "STATUS"}
    if command not in allowed:
        raise ValueError(f"unsupported BALL command: {command}")
    return f"BALL {command}\r\n".encode("ascii")


def parse_reply(line: str) -> BallUartReply:
    text = line.strip("\r\n")
    if text == READY_LINE:
        return BallUartReply("ready", text, {})
    if text in {"OK P", "OK I"} or text.startswith("OK C="):
        return BallUartReply("ok", text, {})
    if text.startswith("ERR "):
        return BallUartReply("error", text, _parse_fields(text[4:]))
    if text.startswith("BALL "):
        return BallUartReply("status", text, _parse_fields(text[5:]))
    return BallUartReply("unknown", text, {})


def _parse_fields(payload: str) -> dict[str, int | str]:
    fields: dict[str, int | str] = {}
    for item in payload.split(","):
        if "=" not in item:
            continue
        name, raw = item.split("=", 1)
        name = name.strip()
        if name not in STATUS_FIELDS and name not in {"C", "M", "MIN", "MAX"}:
            continue
        raw = raw.strip()
        try:
            fields[name] = int(raw, 10)
        except ValueError:
            fields[name] = raw
    return fields


class BallUartClient:
    """Single-owner UART worker with priority controls and latest-only position."""

    def __init__(
        self,
        port: str = "/dev/ttyAMA0",
        baudrate: int = 9600,
        *,
        enabled: bool = True,
        timeout_s: float = 0.02,
        write_timeout_s: float = 0.05,
        reconnect_interval_s: float = 1.0,
        send_rate_hz: float = 50.0,
        debug_position_interval_s: float = 1.0,
        statistics_interval_s: float = 1.0,
        debug: bool = False,
        line_ending: str = "\r\n",
        wait_ready: bool = False,
        left_endpoint_px: int = 72,
        right_endpoint_px: int = 568,
        servo_side: str = "right",
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if baudrate <= 0 or timeout_s <= 0 or write_timeout_s <= 0:
            raise ValueError("UART baudrate and timeouts must be positive")
        if send_rate_hz <= 0 or reconnect_interval_s <= 0:
            raise ValueError("UART rates and reconnect interval must be positive")
        if line_ending != "\r\n":
            raise ValueError("line_ending must be CRLF")
        if wait_ready:
            raise ValueError("wait_ready must be false in no-handshake mode")
        self.port, self.baudrate, self.enabled = port, int(baudrate), bool(enabled)
        self.timeout_s, self.write_timeout_s = float(timeout_s), float(write_timeout_s)
        self.reconnect_interval_s = float(reconnect_interval_s)
        self.send_rate_hz = float(send_rate_hz)
        self.debug_position_interval_s = float(debug_position_interval_s)
        self.statistics_interval_s = float(statistics_interval_s)
        self.debug = bool(debug)
        self.wait_ready = False
        self.line_ending = line_ending
        self.left_endpoint_px, self.right_endpoint_px = left_endpoint_px, right_endpoint_px
        self.servo_side = servo_side
        self._serial_factory = serial_factory
        self._serial: Any | None = None
        self._serial_lock = threading.Lock()
        self._outbound_lock = threading.Lock()
        self._control: deque[bytes] = deque()
        self._latest_position: tuple[int | None, bytes] | None = None
        self._stop_event = threading.Event()
        self._stop_sent = threading.Event()
        self._thread: threading.Thread | None = None
        self._desired_running = False
        self._state = BallUartState.CLOSED
        self._ready = False
        self._last_status: dict[str, int | str] = {}
        self._last_error = ""
        self._last_logged_error = ""
        self._last_sent_position: int | None = None
        self._opened_at: float | None = None
        self._last_rx_at: float | None = None
        self._last_valid_rx_at: float | None = None
        self._rx_buffer = bytearray()
        self._discarding_line = False
        self._position_tx_count = 0
        self._position_replacements = 0
        self._invalid_tx_count = 0
        self._control_tx_count = 0
        self._reconnects = 0
        self._ok_position_rx_count = 0
        self._ok_invalid_rx_count = 0
        self._status_rx_count = 0
        self._unknown_rx_count = 0
        self._decode_error_count = 0
        self._line_overflow_count = 0
        self._last_position_debug_at = 0.0
        self._last_invalid_debug_at = 0.0
        self._position_tx_rate = RollingRate(window_seconds=2.0, max_events=256)
        self._invalid_tx_rate = RollingRate(window_seconds=2.0, max_events=256)

    def pixel_x_to_mm(self, x_px: int | float) -> int:
        left = float(self.left_endpoint_px)
        right = float(self.right_endpoint_px)
        if not math.isfinite(left) or not math.isfinite(right) or left == right:
            raise ValueError("ball UART calibration endpoints are invalid")
        if self.servo_side not in {"left", "right"}:
            raise ValueError("servo_side must be left or right")
        ratio = (float(x_px) - left) / (right - left)
        value = round(-125.0 + ratio * 250.0)
        if self.servo_side == "left":
            value = -value
        return max(-125, min(125, int(value)))

    def _open(self) -> Any:
        kwargs = dict(
            port=self.port, baudrate=self.baudrate, bytesize=8, parity="N", stopbits=1,
            timeout=self.timeout_s, write_timeout=self.write_timeout_s,
            xonxoff=False, rtscts=False, dsrdtr=False,
        )
        if self._serial_factory is not None:
            return self._serial_factory(**kwargs)
        import serial
        return serial.Serial(**kwargs)

    def start(self) -> None:
        if not self.enabled or self.is_running():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="ball-uart", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        if self.enabled and self.is_running():
            self.send_stop()
            self._stop_sent.wait(min(0.25, self.write_timeout_s + 0.15))
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None
        self._close_serial()
        self._ready = False
        self._state = BallUartState.CLOSED

    stop = close

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def is_ready(self) -> bool:
        return self._ready

    def is_connected(self) -> bool:
        with self._serial_lock:
            return self._serial is not None

    def _queue_control(self, data: bytes, *, critical: bool = False) -> bool:
        if not self.enabled:
            return False
        with self._outbound_lock:
            if data in {START_COMMAND, STOP_COMMAND}:
                expected = START_COMMAND if self._desired_running else STOP_COMMAND
                if data != expected:
                    return False
                self._control = deque(
                    item for item in self._control if item not in {START_COMMAND, STOP_COMMAND}
                )
            if critical:
                self._control.appendleft(data)
            elif len(self._control) < 32:
                self._control.append(data)
            else:
                return False
        return True

    def send_start(self) -> bool:
        with self._outbound_lock:
            self._desired_running = True
            self._stop_sent.clear()
            self._state = BallUartState.START_REQUESTED
            self._control = deque(
                item for item in self._control if item not in {START_COMMAND, STOP_COMMAND}
            )
            if not self.enabled:
                return False
            self._control.appendleft(START_COMMAND)
        return True

    def send_stop(self) -> bool:
        with self._outbound_lock:
            self._desired_running = False
            self._latest_position = None
            self._stop_sent.clear()
            self._control = deque(
                item for item in self._control if item not in {START_COMMAND, STOP_COMMAND}
            )
            if not self.enabled:
                return False
            self._control.appendleft(STOP_COMMAND)
        return True

    def send_invalid(self) -> bool:
        return self._publish_latest(None, encode_command("INVALID"))

    def publish_ball_position(self, position_mm: int) -> bool:
        return self._publish_latest(position_mm, encode_position(position_mm))

    def _publish_latest(self, position_mm: int | None, data: bytes) -> bool:
        if not self.enabled or not self.is_running():
            return False
        with self._outbound_lock:
            if self._latest_position is not None:
                self._position_replacements += 1
            self._latest_position = (position_mm, data)
        return True

    def discard_pending_ball_position(self) -> None:
        with self._outbound_lock:
            self._latest_position = None

    def get_status(self) -> dict[str, Any]:
        return self.get_statistics()

    def _next_outbound(self, now: float, next_position: float) -> tuple[bytes | None, float]:
        with self._outbound_lock:
            if self._control:
                return self._control.popleft(), next_position
            if self._desired_running and self._latest_position is not None and now >= next_position:
                _position, data = self._latest_position
                self._latest_position = None
                return data, now + 1.0 / self.send_rate_hz
        return None, next_position

    def _prepare_open_connection(self) -> None:
        """Drop stale output and queue at most one START for this connection."""

        with self._outbound_lock:
            self._latest_position = None
            if self._desired_running:
                self._control = deque(
                    item
                    for item in self._control
                    if item not in {START_COMMAND, STOP_COMMAND}
                )
                self._control.appendleft(START_COMMAND)
                self._state = BallUartState.START_REQUESTED
            else:
                self._control = deque(
                    item for item in self._control if item != START_COMMAND
                )
                self._state = BallUartState.READY

    def _mark_ready(self, source: str) -> None:
        """Record a diagnostic READY indication without changing send permission."""

        self._ready = True
        self._last_valid_rx_at = time.monotonic()
        self._last_error = ""
        LOG.info("UART RX diagnostic READY source=%s", source)

    def _reconcile_control_ack(self, acknowledged_running: bool) -> bool:
        """Record a matching ACK; replies never enqueue or gate output."""

        with self._outbound_lock:
            desired_running = self._desired_running
            if desired_running == acknowledged_running:
                self._state = (
                    BallUartState.RUNNING if acknowledged_running else BallUartState.STOPPED
                )
                return True
            self._state = (
                BallUartState.START_REQUESTED if desired_running else BallUartState.STOPPED
            )
            return False

    def _handle_line(self, raw_line: bytes) -> None:
        try:
            text = raw_line.decode("ascii", errors="strict").strip("\r\n")
        except UnicodeDecodeError:
            self._decode_error_count += 1
            return
        if not text:
            return
        reply = parse_reply(text)
        if self.debug and text not in {"OK P", "OK I"}:
            LOG.info("UART RX %s", text)
        if reply.kind == "ready":
            self._mark_ready("ready_line")
        elif reply.kind == "status":
            self._last_valid_rx_at = time.monotonic()
            self._status_rx_count += 1
            self._last_status = reply.fields
        elif reply.kind == "ok":
            self._last_valid_rx_at = time.monotonic()
            if text == "OK P":
                self._ok_position_rx_count += 1
            elif text == "OK I":
                self._ok_invalid_rx_count += 1
            elif text == "OK C=BALL_START":
                if self._reconcile_control_ack(True):
                    LOG.info("MSPM0 steel-ball loop started")
                else:
                    LOG.warning("late BALL_START ACK ignored; STOP remains the final intent")
            elif text == "OK C=BALL_STOP":
                if self._reconcile_control_ack(False):
                    LOG.info("MSPM0 steel-ball loop stopped")
                else:
                    LOG.warning("late BALL_STOP ACK ignored; START remains the final intent")
        elif reply.kind == "error":
            self._last_valid_rx_at = time.monotonic()
            self._last_error = text
            if text != self._last_logged_error:
                LOG.warning("MCU ERR %s", text)
                self._last_logged_error = text
        else:
            self._unknown_rx_count += 1

    def feed_received(self, data: bytes) -> None:
        """Feed arbitrary UART chunks; CR, LF and CRLF all terminate a line."""

        for byte in data:
            if byte in (0x0D, 0x0A):
                if self._discarding_line:
                    self._discarding_line = False
                    self._rx_buffer.clear()
                    continue
                if self._rx_buffer:
                    line = bytes(self._rx_buffer)
                    self._rx_buffer.clear()
                    self._handle_line(line)
                continue
            if len(self._rx_buffer) >= 256:
                self._rx_buffer.clear()
                self._discarding_line = True
                self._line_overflow_count += 1
                if self._line_overflow_count == 1 or self._line_overflow_count % 100 == 0:
                    LOG.warning("UART RX line exceeded 256 bytes and was discarded")
                continue
            self._rx_buffer.append(byte)

    def _run(self) -> None:
        next_position = next_statistics = 0.0
        while not self._stop_event.is_set():
            if not self.is_connected():
                try:
                    opened = self._open()
                    with self._serial_lock:
                        self._serial = opened
                    self._opened_at = time.monotonic()
                    self._ready = True
                    self._last_valid_rx_at = None
                    self._prepare_open_connection()
                    LOG.info("UART OPEN %s %d 8N1", self.port, self.baudrate)
                    self._last_logged_error = ""
                except Exception as exc:
                    self._state = BallUartState.FAULT
                    self._last_error = str(exc)
                    if self._last_error != self._last_logged_error:
                        LOG.warning("steel-ball UART open failed: %s", exc)
                        self._last_logged_error = self._last_error
                    self._reconnects += 1
                    self._stop_event.wait(self.reconnect_interval_s)
                    continue
            with self._serial_lock:
                serial_handle = self._serial
            if serial_handle is None:
                continue
            data: bytes | None = None
            try:
                now = time.monotonic()
                data, next_position = self._next_outbound(now, next_position)
                if data is not None:
                    serial_handle.write(data)
                    self._control_tx_count += not data.startswith(b"BALL POS") and data != b"BALL INVALID\r\n"
                    if data.startswith(b"BALL POS"):
                        self._position_tx_count += 1
                        self._position_tx_rate.record(now)
                        self._last_sent_position = int(data.split()[2])
                        if self.debug and now - self._last_position_debug_at >= self.debug_position_interval_s:
                            LOG.info("UART TX %s", data.decode("ascii").strip())
                            self._last_position_debug_at = now
                    elif data == b"BALL INVALID\r\n":
                        self._invalid_tx_count += 1
                        self._invalid_tx_rate.record(now)
                        if self.debug and now - self._last_invalid_debug_at >= self.debug_position_interval_s:
                            LOG.info("UART TX BALL INVALID")
                            self._last_invalid_debug_at = now
                    elif data == START_COMMAND:
                        with self._outbound_lock:
                            if self._desired_running:
                                self._state = BallUartState.RUNNING
                        if self.debug:
                            LOG.info("UART TX BALL START")
                    elif data == STOP_COMMAND:
                        with self._outbound_lock:
                            stop_is_current_intent = not self._desired_running
                            if stop_is_current_intent:
                                self._state = BallUartState.STOPPED
                        if stop_is_current_intent:
                            self._stop_sent.set()
                        if self.debug:
                            LOG.info("UART TX %s", data.decode("ascii").strip())
                    elif self.debug:
                        LOG.info("UART TX %s", data.decode("ascii").strip())
                if now >= next_statistics:
                    LOG.info(
                        "UART stats pos=%d invalid=%d ok_pos=%d ok_invalid=%d latest=%s replaced=%d",
                        self._position_tx_count, self._invalid_tx_count,
                        self._ok_position_rx_count, self._ok_invalid_rx_count,
                        self._last_sent_position, self._position_replacements,
                    )
                    next_statistics = now + self.statistics_interval_s
                chunk = serial_handle.read(128)
                if chunk:
                    self._last_rx_at = time.monotonic()
                    self.feed_received(chunk)
            except Exception as exc:
                if data in {START_COMMAND, STOP_COMMAND}:
                    self._queue_control(data, critical=True)
                self._state = BallUartState.FAULT
                self._last_error = str(exc)
                if self._last_error != self._last_logged_error:
                    LOG.warning("steel-ball UART disconnected: %s", exc)
                    self._last_logged_error = self._last_error
                self._ready = False
                self._reconnects += 1
                self._close_serial()
                self._rx_buffer.clear()
                self._stop_event.wait(self.reconnect_interval_s)
        self._close_serial()

    def _close_serial(self) -> None:
        with self._serial_lock:
            handle, self._serial = self._serial, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
            LOG.info("steel-ball UART closed: %s", self.port)

    def get_statistics(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.is_running(),
            "connected": self.is_connected(),
            "port_open": self.is_connected(),
            "port_opened_monotonic": self._opened_at,
            "last_rx_monotonic": self._last_rx_at,
            "last_valid_rx_monotonic": self._last_valid_rx_at,
            "mcu_ready": self._ready,
            "uart_state": self._state.value,
            "mcu_status": dict(self._last_status),
            "last_uart_error": self._last_error,
            "last_sent_position_mm": self._last_sent_position,
            "position_tx_count": self._position_tx_count,
            "position_tx_hz": self._position_tx_rate.rate(),
            "position_replacements": self._position_replacements,
            "invalid_tx_count": self._invalid_tx_count,
            "invalid_tx_hz": self._invalid_tx_rate.rate(),
            "control_tx_count": self._control_tx_count,
            "reconnects": self._reconnects,
            "ok_position_rx_count": self._ok_position_rx_count,
            "ok_invalid_rx_count": self._ok_invalid_rx_count,
            "status_rx_count": self._status_rx_count,
            "unknown_rx_count": self._unknown_rx_count,
            "decode_error_count": self._decode_error_count,
            "line_overflow_count": self._line_overflow_count,
        }
