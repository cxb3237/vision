"""Asynchronous ASCII UART client for the MSPM0 steel-ball controller."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import logging
import threading
import time
from typing import Any, Callable


LOG = logging.getLogger(__name__)
READY_LINE = "READY BALL UART2 9600"
STATUS_FIELDS = frozenset({"S", "F", "EN", "X", "V", "E", "RQ", "AP", "PW", "AGE", "ST", "AC", "RJ"})


class BallUartState(str, Enum):
    CLOSED = "串口未打开"
    WAITING_READY = "等待 MCU READY"
    READY = "MCU 已就绪"
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
        ping_interval_s: float = 1.0,
        status_interval_s: float = 1.0,
        line_ending: str = "\r\n",
        wait_ready: bool = True,
        left_endpoint_px: int = 72,
        right_endpoint_px: int = 568,
        servo_side: str = "right",
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        if baudrate <= 0 or timeout_s <= 0 or write_timeout_s <= 0:
            raise ValueError("UART baudrate and timeouts must be positive")
        if send_rate_hz <= 0 or reconnect_interval_s <= 0:
            raise ValueError("UART rates and reconnect interval must be positive")
        if left_endpoint_px == right_endpoint_px:
            raise ValueError("ball UART pixel endpoints must differ")
        if servo_side not in {"left", "right"}:
            raise ValueError("servo_side must be left or right")
        if line_ending != "\r\n":
            raise ValueError("line_ending must be CRLF")
        self.port, self.baudrate, self.enabled = port, int(baudrate), bool(enabled)
        self.timeout_s, self.write_timeout_s = float(timeout_s), float(write_timeout_s)
        self.reconnect_interval_s = float(reconnect_interval_s)
        self.send_rate_hz = float(send_rate_hz)
        self.ping_interval_s, self.status_interval_s = float(ping_interval_s), float(status_interval_s)
        self.wait_ready = bool(wait_ready)
        self.line_ending = line_ending
        self.left_endpoint_px, self.right_endpoint_px = int(left_endpoint_px), int(right_endpoint_px)
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
        self._position_tx_count = 0
        self._position_replacements = 0
        self._invalid_tx_count = 0
        self._control_tx_count = 0
        self._reconnects = 0

    def pixel_x_to_mm(self, x_px: int | float) -> int:
        ratio = (float(x_px) - self.left_endpoint_px) / (self.right_endpoint_px - self.left_endpoint_px)
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
            if data in {encode_command("START"), encode_command("STOP")}:
                self._control = deque(item for item in self._control if item != data)
            if critical:
                self._control.appendleft(data)
            elif len(self._control) < 32:
                self._control.append(data)
            else:
                return False
        return True

    def send_start(self) -> bool:
        self._desired_running = True
        self._stop_sent.clear()
        self._state = BallUartState.START_REQUESTED
        return self._queue_control(encode_command("START"), critical=True)

    def send_stop(self) -> bool:
        self._desired_running = False
        self.discard_pending_ball_position()
        return self._queue_control(encode_command("STOP"), critical=True)

    def send_ping(self) -> bool:
        return self._queue_control(encode_command("PING"))

    def request_status(self) -> bool:
        return self._queue_control(encode_command("STATUS"))

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

    def get_message(self, timeout: float = 0.0) -> None:
        del timeout
        return None

    def send_packet(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def raise_if_failed(self) -> None:
        return None

    def _next_outbound(self, now: float, next_position: float) -> tuple[bytes | None, float]:
        with self._outbound_lock:
            if self._control:
                for index, item in enumerate(self._control):
                    if self._ready or item in {encode_command("PING"), encode_command("STOP")}:
                        del self._control[index]
                        return item, next_position
            if self._ready and self._desired_running and self._latest_position is not None and now >= next_position:
                _position, data = self._latest_position
                self._latest_position = None
                return data, now + 1.0 / self.send_rate_hz
        return None, next_position

    def _handle_line(self, raw_line: bytes) -> None:
        try:
            text = raw_line.decode("ascii", errors="strict").strip("\r\n")
        except UnicodeDecodeError:
            return
        if not text:
            return
        reply = parse_reply(text)
        self._last_rx_at = time.monotonic()
        if reply.kind == "ready":
            self._ready = True
            self._state = BallUartState.READY
            LOG.info("MSPM0 READY: %s", text)
            if self._desired_running:
                self._state = BallUartState.START_REQUESTED
                self._queue_control(encode_command("START"), critical=True)
        elif reply.kind == "status":
            self._last_status = reply.fields
            enabled = reply.fields.get("EN")
            if enabled == 1:
                self._state = BallUartState.RUNNING
            elif enabled == 0:
                self._state = BallUartState.STOPPED
        elif reply.kind == "ok":
            if text == "OK C=BALL_START":
                self._state = BallUartState.RUNNING
                LOG.info("MSPM0 steel-ball loop started")
            elif text == "OK C=BALL_STOP":
                self._state = BallUartState.STOPPED
                LOG.info("MSPM0 steel-ball loop stopped")
        elif reply.kind == "error":
            self._last_error = text
            if text != self._last_logged_error:
                LOG.warning("MSPM0 UART error: %s", text)
                self._last_logged_error = text

    def _run(self) -> None:
        next_position = next_ping = next_status = 0.0
        rx_buffer = bytearray()
        while not self._stop_event.is_set():
            if not self.is_connected():
                try:
                    opened = self._open()
                    with self._serial_lock:
                        self._serial = opened
                    self._opened_at = time.monotonic()
                    self._ready = not self.wait_ready
                    self._state = BallUartState.READY if self._ready else BallUartState.WAITING_READY
                    self.discard_pending_ball_position()
                    LOG.info("steel-ball UART opened: %s @ %d 8N1", self.port, self.baudrate)
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
                if not self._ready and now >= next_ping:
                    self.send_ping()
                    next_ping = now + self.ping_interval_s
                if self._ready and now >= next_status:
                    self.request_status()
                    next_status = now + self.status_interval_s
                data, next_position = self._next_outbound(now, next_position)
                if data is not None:
                    serial_handle.write(data)
                    self._control_tx_count += not data.startswith(b"BALL POS") and data != b"BALL INVALID\r\n"
                    if data.startswith(b"BALL POS"):
                        self._position_tx_count += 1
                        self._last_sent_position = int(data.split()[2])
                    elif data == b"BALL INVALID\r\n":
                        self._invalid_tx_count += 1
                    elif data == b"BALL STOP\r\n":
                        self._stop_sent.set()
                chunk = serial_handle.readline()
                if chunk:
                    rx_buffer.extend(chunk)
                    while b"\n" in rx_buffer:
                        raw, _, remainder = rx_buffer.partition(b"\n")
                        rx_buffer[:] = remainder
                        self._handle_line(raw + b"\n")
            except Exception as exc:
                if data in {encode_command("START"), encode_command("STOP")}:
                    self._queue_control(data, critical=True)
                self._state = BallUartState.FAULT
                self._last_error = str(exc)
                if self._last_error != self._last_logged_error:
                    LOG.warning("steel-ball UART disconnected: %s", exc)
                    self._last_logged_error = self._last_error
                self._ready = False
                self._reconnects += 1
                self._close_serial()
                rx_buffer.clear()
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

    def get_statistics(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.is_running(),
            "connected": self.is_connected(),
            "port_open": self.is_connected(),
            "port_opened_monotonic": self._opened_at,
            "last_rx_monotonic": self._last_rx_at,
            "last_valid_packet_monotonic": self._last_rx_at if self._ready else None,
            "mcu_ready": self._ready,
            "uart_state": self._state.value,
            "mcu_status": dict(self._last_status),
            "last_uart_error": self._last_error,
            "last_sent_position_mm": self._last_sent_position,
            "position_tx_count": self._position_tx_count,
            "position_replacements": self._position_replacements,
            "invalid_tx_count": self._invalid_tx_count,
            "control_tx_count": self._control_tx_count,
            "reconnects": self._reconnects,
            "rx_good_count": 0,
            "rx_crc_error_count": 0,
            "vmc_tx_count": self._position_tx_count,
        }
