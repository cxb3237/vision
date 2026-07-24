"""固定视觉结果发布通道的无硬件测试。"""

import threading
import time

from argparse import Namespace

import pytest

from app import resolve_serial_settings
from core.config_loader import ConfigError, load_mission_config
from core.models import TargetState, VisionResult
from drivers.serial_service import SerialService
from protocol.vmc_link import decode_result_packet


class FakeSerial:
    def __init__(self, fail_read_once: bool = False) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self.fail_read_once = fail_read_once
        self.lock = threading.Lock()

    def read(self, _size: int) -> bytes:
        time.sleep(0.002)
        if self.fail_read_once:
            self.fail_read_once = False
            raise OSError("disconnected")
        return b""

    def write(self, data: bytes) -> int:
        with self.lock:
            if self.closed:
                raise OSError("Bad file descriptor")
            self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        self.closed = True


class BlockingReadSerial(FakeSerial):
    def __init__(self) -> None:
        super().__init__()
        self.read_entered = threading.Event()
        self.release_read = threading.Event()

    def read(self, _size: int) -> bytes:
        self.read_entered.set()
        self.release_read.wait(1.0)
        return b""

    def close(self) -> None:
        self.release_read.set()
        super().close()


def vision(frame_id: int) -> VisionResult:
    return VisionResult(
        frame_id=frame_id,
        capture_timestamp=time.monotonic(),
        process_timestamp=time.monotonic(),
        found=True,
        target_state=TargetState.LOCKED,
        target_class=100 + frame_id % 10,
        center_x=frame_id,
        center_y=20,
        image_width=100,
        image_height=100,
        confidence=900,
    )


def wait_until(predicate, timeout: float = 1.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("等待串口模拟状态超时")


def test_latest_result_replaces_old_pending_result() -> None:
    fake = BlockingReadSerial()
    service = SerialService(
        "fake",
        serial_factory=lambda *_args, **_kwargs: fake,
        send_rate_hz=20,
        queue_size=2,
    )
    service.start()
    assert fake.read_entered.wait(1.0)
    for frame_id in range(1, 7):
        service.publish_result(vision(frame_id), "digit")
    fake.release_read.set()
    wait_until(lambda: len(fake.written) >= 1)
    service.stop()
    assert len(fake.written) == 1
    assert decode_result_packet(fake.written[0]).center_x_px == 6
    assert service.get_statistics()["result_replacements"] == 5


def test_result_sequence_wraps_at_uint16() -> None:
    fake = FakeSerial()
    service = SerialService(
        "fake",
        serial_factory=lambda *_args, **_kwargs: fake,
        send_rate_hz=100,
    )
    service.start()
    wait_until(lambda: service.get_statistics()["port_open"])
    with service._result_lock:
        service._result_sequence = 0xFFFF
    service.publish_result(vision(1), "color")
    wait_until(lambda: len(fake.written) >= 1)
    service.publish_result(vision(2), "color")
    wait_until(lambda: len(fake.written) >= 2)
    service.stop()
    assert [decode_result_packet(item).sequence for item in fake.written[:2]] == [0xFFFF, 0]


def test_open_failure_non_strict_keeps_worker_alive() -> None:
    attempts = 0

    def factory(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("missing port")

    service = SerialService(
        "missing",
        serial_factory=factory,
        reconnect_delay=0.01,
        strict=False,
    )
    service.start()
    wait_until(lambda: attempts >= 2)
    assert service.is_running()
    service.stop()


def test_open_failure_strict_raises() -> None:
    service = SerialService(
        "missing",
        serial_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("missing port")
        ),
        reconnect_delay=0.01,
        strict=True,
    )
    try:
        service.start()
    except RuntimeError as exc:
        assert "严格模式" in str(exc)
    else:
        raise AssertionError("strict=true 应传播串口打开失败")
    assert not service.is_running()


def test_disconnect_reconnects_to_new_handle() -> None:
    handles: list[FakeSerial] = []

    def factory(*_args, **_kwargs):
        handle = FakeSerial(fail_read_once=not handles)
        handles.append(handle)
        return handle

    service = SerialService(
        "fake",
        serial_factory=factory,
        reconnect_delay=0.01,
    )
    service.start()
    wait_until(lambda: len(handles) >= 2)
    assert handles[0].closed
    assert service.get_statistics()["reconnects"] >= 1
    service.stop()


def _args(**overrides) -> Namespace:
    values = {
        "serial": False,
        "serial_port": None,
        "baudrate": None,
        "serial_rate": None,
        "serial_debug": False,
        "no_serial": False,
        "video": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_no_serial_always_disables_hardware() -> None:
    mission = load_mission_config(overrides={"serial_enabled": True})
    assert not resolve_serial_settings(_args(no_serial=True), mission)["enabled"]


def test_video_replay_defaults_to_no_serial_but_explicit_port_enables_it() -> None:
    mission = load_mission_config(overrides={"serial_enabled": True})
    assert not resolve_serial_settings(_args(video="demo.mp4"), mission)["enabled"]
    settings = resolve_serial_settings(
        _args(video="demo.mp4", serial_port="loop://"), mission
    )
    assert settings["enabled"] and settings["port"] == "loop://"


def test_cli_serial_values_override_configuration() -> None:
    mission = load_mission_config()
    settings = resolve_serial_settings(
        _args(serial=True, baudrate=230400, serial_rate=30), mission
    )
    assert settings["baudrate"] == 230400
    assert settings["send_rate_hz"] == 30


def test_default_mission_creates_64_slot_control_queues() -> None:
    mission = load_mission_config()
    settings = resolve_serial_settings(_args(), mission)
    service = SerialService(
        settings.pop("port"),
        settings.pop("baudrate"),
        **settings,
    )
    assert service._queue_size == 64
    assert service._receive_queue.maxsize == 64
    assert service._critical_queue.maxsize == 64
    assert service._send_queue.maxsize == 64


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"baudrate": 0}, "baudrate"),
        ({"baudrate": -1}, "baudrate"),
        ({"serial_rate": 0}, "serial-rate"),
        ({"serial_rate": -0.1}, "serial-rate"),
    ],
)
def test_zero_and_negative_cli_serial_values_are_rejected(overrides, message) -> None:
    with pytest.raises(ConfigError, match=message):
        resolve_serial_settings(_args(**overrides), load_mission_config())
