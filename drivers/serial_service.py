"""高吞吐串口后台服务；单一工作线程拥有句柄并处理收发。"""

from __future__ import annotations

from collections.abc import Callable
import inspect
import logging
import queue
import threading
import time
from typing import Any

from protocol.vmc_messages import Flags, MessageType
from protocol.vmc_protocol import VmcPacket, VmcStreamParser, encode_packet
from protocol.vmc_link import (
    DetectorID,
    VMCLinkResult,
    encode_result_packet,
    result_to_vmc_link,
)
from core.models import VisionResult


LOG = logging.getLogger(__name__)


class SerialService:
    """优先发送关键包、对流式目标只保留最新值并安全重连。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        enabled: bool = True,
        serial_factory: Callable[..., Any] | None = None,
        queue_size: int = 64,
        reconnect_delay: float = 1.0,
        read_timeout: float = 0.01,
        write_timeout: float = 0.1,
        send_batch_size: int = 64,
        send_rate_hz: float = 20.0,
        strict: bool = False,
        serial_debug: bool = False,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size 必须为正整数")
        if send_rate_hz <= 0:
            raise ValueError("send_rate_hz 必须为正数")
        self.port = port
        self.baudrate = baudrate
        self.enabled = enabled
        self.strict = strict
        self.serial_debug = serial_debug
        self._serial_factory = serial_factory
        self._reconnect_delay = reconnect_delay
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout
        self._send_batch_size = send_batch_size
        self._queue_size = queue_size
        self._serial: Any | None = None
        self._serial_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_event = threading.Event()
        self._startup_error: BaseException | None = None
        self._fatal_error: BaseException | None = None
        self._receive_queue: queue.Queue[VmcPacket] = queue.Queue(maxsize=queue_size)
        self._critical_queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._send_queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_size)
        self._stream_lock = threading.Lock()
        self._latest_stream: bytes | None = None
        self._result_lock = threading.Lock()
        self._latest_result: VMCLinkResult | None = None
        self._result_sequence = 0
        self._send_rate_hz = float(send_rate_hz)
        self._next_result_send = 0.0
        self._parser = VmcStreamParser()
        self._stats_lock = threading.Lock()
        self._reconnects = 0
        self._rx_bytes = 0
        self._tx_packets = 0
        self._tx_failures = 0
        self._rx_queue_drops = 0
        self._tx_queue_drops = 0
        self._stream_replacements = 0
        self._result_replacements = 0
        self._last_rx_monotonic: float | None = None
        self._last_valid_packet_monotonic: float | None = None
        self._last_heartbeat_monotonic: float | None = None
        self._port_opened_monotonic: float | None = None

    @staticmethod
    def _drain(queue_object: queue.Queue[Any]) -> None:
        while True:
            try:
                queue_object.get_nowait()
            except queue.Empty:
                return

    def _reset_runtime_state(self) -> None:
        self._drain(self._receive_queue)
        self._drain(self._critical_queue)
        self._drain(self._send_queue)
        with self._stream_lock:
            self._latest_stream = None
        with self._result_lock:
            self._latest_result = None
            self._result_sequence = 0
            self._next_result_send = 0.0
        self._parser = VmcStreamParser()
        self._startup_event.clear()
        self._startup_error = None
        self._fatal_error = None
        with self._stats_lock:
            self._last_rx_monotonic = None
            self._last_valid_packet_monotonic = None
            self._last_heartbeat_monotonic = None
            self._port_opened_monotonic = None

    def start(self) -> None:
        """启动 I/O 线程；重启会清除旧队列和解析器状态。"""

        if not self.enabled or self.is_running():
            return
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("旧串口线程尚未退出")
        self._reset_runtime_state()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="serial-io", daemon=True)
        self._thread.start()
        if self.strict:
            self._startup_event.wait(max(2.0, self._reconnect_delay + 0.5))
            if self._startup_error is not None:
                error = self._startup_error
                self.stop()
                raise RuntimeError(f"严格模式下串口打开失败: {error}") from error

    def _factory_kwargs(self) -> dict[str, float]:
        kwargs = {"timeout": self._read_timeout}
        if self._serial_factory is None:
            kwargs["write_timeout"] = self._write_timeout
            return kwargs
        try:
            parameters = inspect.signature(self._serial_factory).parameters
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if accepts_kwargs or "write_timeout" in parameters:
                kwargs["write_timeout"] = self._write_timeout
        except (TypeError, ValueError):
            kwargs["write_timeout"] = self._write_timeout
        return kwargs

    def _open_serial(self) -> Any:
        if self._serial_factory is not None:
            try:
                if not inspect.signature(self._serial_factory).parameters:
                    return self._serial_factory()
            except (TypeError, ValueError):
                pass
            return self._serial_factory(self.port, self.baudrate, **self._factory_kwargs())
        import serial

        return serial.serial_for_url(self.port, self.baudrate, **self._factory_kwargs())

    def _close_owned_serial(self) -> None:
        with self._serial_lock:
            serial_handle = self._serial
            self._serial = None
        if serial_handle is not None:
            try:
                serial_handle.close()
            except Exception:
                LOG.exception("关闭串口失败")

    def _drop_oldest_and_put(self, packet: VmcPacket) -> None:
        try:
            self._receive_queue.put_nowait(packet)
            return
        except queue.Full:
            pass
        try:
            self._receive_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._receive_queue.put_nowait(packet)
        except queue.Full:
            pass
        with self._stats_lock:
            self._rx_queue_drops += 1

    def _next_outbound(self) -> bytes | None:
        try:
            return self._critical_queue.get_nowait()
        except queue.Empty:
            pass
        now = time.monotonic()
        with self._result_lock:
            if self._latest_result is not None and now >= self._next_result_send:
                result = self._latest_result.with_sequence(self._result_sequence)
                self._latest_result = None
                self._result_sequence = (self._result_sequence + 1) & 0xFFFF
                self._next_result_send = now + 1.0 / self._send_rate_hz
                data = encode_result_packet(result, sequence=result.sequence)
                if self.serial_debug:
                    LOG.info("VMC-Link TX seq=%d %s", result.sequence, data.hex(" "))
                return data
        try:
            return self._send_queue.get_nowait()
        except queue.Empty:
            pass
        with self._stream_lock:
            data = self._latest_stream
            self._latest_stream = None
        return data

    def _flush_send_batch(self, serial_handle: Any) -> int:
        sent = 0
        while sent < self._send_batch_size and not self._stop_event.is_set():
            data = self._next_outbound()
            if data is None:
                break
            try:
                serial_handle.write(data)
            except Exception:
                with self._stats_lock:
                    self._tx_failures += 1
                raise
            sent += 1
            with self._stats_lock:
                self._tx_packets += 1
        return sent

    def _record_received(self, data: bytes, packets: list[VmcPacket]) -> None:
        now = time.monotonic()
        with self._stats_lock:
            self._rx_bytes += len(data)
            self._last_rx_monotonic = now
            if packets:
                self._last_valid_packet_monotonic = now
            if any(packet.message_type == MessageType.HEARTBEAT for packet in packets):
                self._last_heartbeat_monotonic = now

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if self._serial is None:
                    try:
                        opened = self._open_serial()
                        with self._serial_lock:
                            self._serial = opened
                        with self._stats_lock:
                            self._port_opened_monotonic = time.monotonic()
                        self._startup_event.set()
                        LOG.info("串口已打开: %s @ %d", self.port, self.baudrate)
                    except Exception as exc:
                        with self._stats_lock:
                            self._reconnects += 1
                        LOG.warning("串口打开失败: %s", exc)
                        if not self._startup_event.is_set():
                            self._startup_error = exc
                            self._startup_event.set()
                        if self.strict:
                            self._fatal_error = exc
                            self._stop_event.set()
                            break
                        self._stop_event.wait(self._reconnect_delay)
                        continue
                with self._serial_lock:
                    serial_handle = self._serial
                if serial_handle is None:
                    continue
                try:
                    self._flush_send_batch(serial_handle)
                    data = serial_handle.read(128)
                    if data:
                        packets = self._parser.feed(data)
                        self._record_received(data, packets)
                        for packet in packets:
                            self._drop_oldest_and_put(packet)
                except Exception as exc:
                    LOG.warning("串口读写失败，将安全重连: %s", exc)
                    self._close_owned_serial()
                    with self._stats_lock:
                        self._reconnects += 1
                        self._port_opened_monotonic = None
                    if self.strict:
                        self._fatal_error = exc
                        self._stop_event.set()
                        break
                    self._stop_event.wait(self._reconnect_delay)
        finally:
            self._startup_event.set()
            self._close_owned_serial()

    def stop(self, timeout: float = 2.0) -> None:
        """停止线程；停止后所有未发数据均被丢弃，不会留到下次启动。"""

        self._stop_event.set()
        thread = self._thread
        if thread is None:
            self._reset_runtime_state()
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            LOG.error("串口线程在 %.2f 秒内未退出", timeout)
            return
        self._thread = None
        self._reset_runtime_state()

    def is_running(self) -> bool:
        """返回串口工作线程是否存活。"""

        return bool(self._thread and self._thread.is_alive())

    def send_packet(
        self,
        message_type: int,
        flags: int = 0,
        sequence: int = 0,
        payload: bytes = b"",
    ) -> bool:
        """按关键、普通或最新流式策略非阻塞排队。"""

        if not self.enabled or not self.is_running() or self._stop_event.is_set():
            return False
        data = encode_packet(message_type, flags, sequence, payload)
        flag_set = Flags(flags)
        if message_type == MessageType.VISION_TARGET and Flags.STREAM in flag_set:
            with self._stream_lock:
                if self._latest_stream is not None:
                    with self._stats_lock:
                        self._stream_replacements += 1
                self._latest_stream = data
            return True
        target_queue = (
            self._critical_queue
            if message_type == MessageType.ACK or Flags.URGENT in flag_set
            else self._send_queue
        )
        try:
            target_queue.put_nowait(data)
            return True
        except queue.Full:
            with self._stats_lock:
                self._tx_queue_drops += 1
            LOG.warning("串口%s队列已满", "关键" if target_queue is self._critical_queue else "发送")
            return False

    def publish_result(
        self,
        result: VisionResult,
        detector_id: str | int | DetectorID,
        *,
        camera_calibrated: bool = False,
    ) -> bool:
        """非阻塞提交最新视觉结果；旧的未发送结果会被覆盖。"""

        if not self.enabled or not self.is_running() or self._stop_event.is_set():
            return False
        snapshot = result_to_vmc_link(
            result,
            sequence=0,
            detector_id=detector_id,
            camera_calibrated=camera_calibrated,
        )
        with self._result_lock:
            if self._latest_result is not None:
                with self._stats_lock:
                    self._result_replacements += 1
            self._latest_result = snapshot
        return True

    def raise_if_failed(self) -> None:
        """严格模式下把后台串口错误传播给主循环。"""

        if self.strict and self._fatal_error is not None:
            raise RuntimeError(f"严格模式串口故障: {self._fatal_error}") from self._fatal_error

    def get_message(self, timeout: float = 0.0) -> VmcPacket | None:
        """获取一个已解析消息；超时返回 ``None``。"""

        try:
            return self._receive_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_statistics(self) -> dict[str, int | float | bool | None]:
        """返回端口状态、peer 活跃时间和收发统计快照。"""

        with self._serial_lock:
            port_open = self._serial is not None
        with self._stats_lock:
            return {
                "enabled": self.enabled,
                "running": self.is_running(),
                "connected": port_open,
                "port_open": port_open,
                "port_opened_monotonic": self._port_opened_monotonic,
                "last_rx_monotonic": self._last_rx_monotonic,
                "last_valid_packet_monotonic": self._last_valid_packet_monotonic,
                "last_heartbeat_monotonic": self._last_heartbeat_monotonic,
                "reconnects": self._reconnects,
                "rx_bytes": self._rx_bytes,
                "rx_good_count": self._parser.good_count,
                "rx_crc_error_count": self._parser.crc_error_count,
                "tx_count": self._tx_packets,
                "tx_errors": self._tx_failures,
                "rx_queue_drops": self._rx_queue_drops,
                "tx_queue_drops": self._tx_queue_drops,
                "stream_replacements": self._stream_replacements,
                "result_replacements": self._result_replacements,
                "result_sequence": self._result_sequence,
                "critical_queue_size": self._critical_queue.qsize(),
                "send_queue_size": self._send_queue.qsize(),
            }
