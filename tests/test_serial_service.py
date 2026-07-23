"""SerialService 使用 fake serial 的安全收发测试。"""

from collections import deque
import threading
import time

from drivers.serial_service import SerialService
from protocol.vmc_messages import Ack, Flags, Heartbeat, MessageType
from protocol.vmc_protocol import VmcStreamParser, encode_packet


class FakeSerial:
    def __init__(self, initial: bytes = b"") -> None:
        self.incoming = deque(initial)
        self.written: list[bytes] = []
        self.closed = False
        self.bad_fd = False
        self.lock = threading.Lock()

    def read(self, size: int) -> bytes:
        time.sleep(0.002)
        with self.lock:
            if self.closed:
                self.bad_fd = True
                raise OSError("Bad file descriptor")
            data = bytes(self.incoming.popleft() for _ in range(min(size, len(self.incoming))))
            return data

    def write(self, data: bytes) -> int:
        with self.lock:
            if self.closed:
                self.bad_fd = True
                raise OSError("Bad file descriptor")
            self.written.append(data)
            return len(data)

    def close(self) -> None:
        with self.lock:
            self.closed = True


class SlowReadSerial(FakeSerial):
    """故意忽略配置的短 timeout，每次读取阻塞 0.1 秒。"""

    def __init__(self, initial: bytes = b"") -> None:
        super().__init__(initial)
        self.read_entered = threading.Event()

    def read(self, size: int) -> bytes:
        self.read_entered.set()
        time.sleep(0.1)
        return super().read(size)


def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("等待 fake serial 状态超时")


def test_disabled_service_does_not_open_or_start() -> None:
    opened = False

    def factory(*_args, **_kwargs):
        nonlocal opened
        opened = True
        return FakeSerial()

    service = SerialService("unused", enabled=False, serial_factory=factory)
    service.start()
    service.stop()
    assert not opened
    assert not service.is_running()
    assert not service.send_packet(1)


def test_receive_parse_send_and_safe_stop() -> None:
    incoming = encode_packet(7, 2, 9, b"hello")
    fake = FakeSerial(incoming)
    service = SerialService("fake", serial_factory=lambda *_args, **_kwargs: fake)
    service.start()
    message = None
    deadline = time.monotonic() + 1
    while message is None and time.monotonic() < deadline:
        message = service.get_message(timeout=0.02)
    assert message is not None and message.payload == b"hello"
    assert service.send_packet(8, payload=b"world")
    wait_until(lambda: bool(fake.written))
    service.stop()
    assert fake.closed
    assert not fake.bad_fd
    assert service.get_statistics()["tx_count"] == 1


def test_repeated_start_stop_opens_new_handles() -> None:
    handles: list[FakeSerial] = []

    def factory(*_args, **_kwargs):
        handle = FakeSerial()
        handles.append(handle)
        return handle

    service = SerialService("fake", serial_factory=factory)
    for _ in range(2):
        service.start()
        wait_until(service.is_running)
        service.stop()
    assert len(handles) == 2
    assert all(handle.closed and not handle.bad_fd for handle in handles)


def test_slow_read_does_not_limit_30_hz_enqueue_throughput() -> None:
    fake = SlowReadSerial()
    service = SerialService(
        "fake",
        serial_factory=lambda *_args, **_kwargs: fake,
        queue_size=64,
    )
    service.start()
    assert fake.read_entered.wait(1.0)
    started = time.monotonic()
    sequence = 0
    while time.monotonic() - started < 3.0:
        service.send_packet(
            MessageType.VISION_TARGET,
            int(Flags.STREAM),
            sequence,
            b"target",
        )
        if sequence % 3 == 0:
            service.send_packet(MessageType.HEARTBEAT, 0, sequence, bytes(12))
        sequence = (sequence + 1) & 0xFF
        time.sleep(1.0 / 30.0)
    time.sleep(0.3)
    statistics = service.get_statistics()
    service.stop()
    assert statistics["tx_queue_drops"] == 0
    assert statistics["tx_count"] >= 25


def test_ack_is_sent_before_pending_normal_and_stream_packets() -> None:
    fake = SlowReadSerial()
    service = SerialService("fake", serial_factory=lambda *_args, **_kwargs: fake)
    service.start()
    assert fake.read_entered.wait(1.0)
    assert service.send_packet(MessageType.HEARTBEAT, payload=bytes(12))
    assert service.send_packet(
        MessageType.VISION_TARGET,
        int(Flags.STREAM),
        payload=b"latest",
    )
    assert service.send_packet(MessageType.ACK, payload=Ack(1, 2, 0, 0).pack())
    wait_until(lambda: len(fake.written) >= 3)
    parser = VmcStreamParser()
    message_types = [parser.feed(data)[0].message_type for data in fake.written[:3]]
    service.stop()
    assert message_types[0] == MessageType.ACK


def test_peer_receive_timestamps_are_recorded_separately_from_port_open() -> None:
    heartbeat = encode_packet(
        MessageType.HEARTBEAT,
        0,
        1,
        Heartbeat(1, 0, 0, 0, 0, 0).pack(),
    )
    fake = FakeSerial(heartbeat)
    service = SerialService("fake", serial_factory=lambda *_args, **_kwargs: fake)
    service.start()
    wait_until(lambda: service.get_statistics()["last_heartbeat_monotonic"] is not None)
    statistics = service.get_statistics()
    service.stop()
    assert statistics["port_open"]
    assert statistics["last_rx_monotonic"] is not None
    assert statistics["last_valid_packet_monotonic"] is not None


def test_stopped_service_rejects_old_outbound_packets() -> None:
    service = SerialService("fake", serial_factory=lambda: FakeSerial())
    assert not service.send_packet(MessageType.HEARTBEAT, payload=bytes(12))
    service.start()
    service.stop()
    assert not service.send_packet(MessageType.HEARTBEAT, payload=bytes(12))
