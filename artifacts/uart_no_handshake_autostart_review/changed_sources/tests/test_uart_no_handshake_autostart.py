from __future__ import annotations

from pathlib import Path
import threading
import time

from core.config_loader import load_mission_config
from drivers.ball_uart_client import BallUartClient, encode_command, encode_position
from touch_ui.models import load_touch_ui_config


ROOT = Path(__file__).resolve().parents[1]
START = encode_command("START")
STOP = encode_command("STOP")
INVALID = encode_command("INVALID")
PING = encode_command("PING")


class SilentSerial:
    def __init__(self, *, disconnect_once: bool = False) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self.disconnect_once = disconnect_once

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def read(self, _size: int = 128) -> bytes:
        if self.disconnect_once and START in self.written:
            self.disconnect_once = False
            raise OSError("simulated disconnect")
        time.sleep(0.001)
        return b""

    def close(self) -> None:
        self.closed = True


def wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("condition timed out")


def test_silent_mcu_gets_start_positions_invalid_and_stop_without_ping() -> None:
    fake = SilentSerial()
    client = BallUartClient(
        serial_factory=lambda **_kwargs: fake,
        send_rate_hz=1000,
    )
    client.send_start()
    client.start()
    wait_until(lambda: START in fake.written)
    for value in (12, 10, 8):
        assert client.publish_ball_position(value)
        wait_until(lambda value=value: encode_position(value) in fake.written)
    assert client.send_invalid()
    wait_until(lambda: INVALID in fake.written)
    client.close()

    assert fake.written == [
        START,
        encode_position(12),
        encode_position(10),
        encode_position(8),
        INVALID,
        STOP,
    ]
    assert PING not in fake.written


def test_reconnect_sends_one_start_per_connection_and_no_stale_position() -> None:
    first = SilentSerial(disconnect_once=True)
    second = SilentSerial()
    second_open_allowed = threading.Event()
    calls = 0

    def factory(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        assert second_open_allowed.wait(1.0)
        return second

    client = BallUartClient(
        serial_factory=factory,
        reconnect_interval_s=0.005,
        send_rate_hz=1000,
    )
    client.send_start()
    client.start()
    wait_until(lambda: START in first.written)
    wait_until(lambda: first.closed)
    client.publish_ball_position(99)
    second_open_allowed.set()
    wait_until(lambda: START in second.written)
    assert first.written.count(START) == 1
    assert second.written.count(START) == 1
    assert encode_position(99) not in second.written
    client.publish_ball_position(8)
    wait_until(lambda: encode_position(8) in second.written)
    client.close()
    assert PING not in first.written + second.written


def test_latest_only_and_user_stop_are_independent_of_replies() -> None:
    client = BallUartClient()
    client._thread = threading.current_thread()
    client.send_start()
    client._next_outbound(time.monotonic(), 0.0)
    client.publish_ball_position(12)
    client.publish_ball_position(10)
    client.publish_ball_position(8)
    data, _ = client._next_outbound(time.monotonic(), 0.0)
    assert data == encode_position(8)
    client.send_stop()
    stop, _ = client._next_outbound(time.monotonic(), 0.0)
    assert stop == STOP
    assert client._desired_running is False
    assert client._latest_position is None
    client._thread = None


def test_default_configs_enable_no_handshake_boot_output() -> None:
    uart = load_mission_config()["ball_uart"]
    touch = load_touch_ui_config()
    assert uart["port"] == "/dev/ttyAMA0"
    assert uart["baudrate"] == 9600
    assert uart["wait_ready"] is False
    assert touch.startup_competition_mode is True


def test_single_hardware_owner_and_single_boot_service() -> None:
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    deploy_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "deploy").rglob("*")
        if path.is_file() and path.suffix in {".sh", ".service", ".template", ".py"}
    )
    assert app_source.count("BallUartClient(") == 1
    assert app_source.count("CameraService(") == 1
    assert "camera-uart.service" not in deploy_sources
    assert "serial.service" not in deploy_sources
    assert "tools.test_ball_uart" not in deploy_sources


def test_vision_touch_service_is_boot_enabled_by_both_installers() -> None:
    service = (ROOT / "deploy/vision-touch.service.template").read_text(encoding="utf-8")
    touch_installer = (ROOT / "deploy/install_touch_ui.sh").read_text(encoding="utf-8")
    tablet_installer = (ROOT / "deploy/install_tablet_web.sh").read_text(encoding="utf-8")
    assert "WantedBy=multi-user.target" in service
    assert "Restart=on-failure" in service and "RestartSec=3" in service
    assert "--serial-port /dev/ttyAMA0" in service
    assert "--baudrate 9600" in service
    assert "--serial-rate 50" in service
    assert "--headless" in service
    for installer in (touch_installer, tablet_installer):
        assert "systemctl daemon-reload" in installer
        assert "systemctl enable vision-touch.service" in installer
        assert "systemctl is-enabled --quiet" in installer
