from __future__ import annotations

import logging
from pathlib import Path
import threading
import time

import pytest

from core.config_loader import ConfigError, load_mission_config
from drivers.ball_uart_client import (
    READY_LINE,
    BallUartClient,
    encode_command,
    encode_position,
    parse_reply,
)


@pytest.mark.parametrize(
    ("position", "expected"),
    [(35, b"BALL POS 35\r\n"), (-125, b"BALL POS -125\r\n"), (125, b"BALL POS 125\r\n"), (0, b"BALL POS 0\r\n")],
)
def test_position_command_format(position, expected) -> None:
    assert encode_position(position) == expected


@pytest.mark.parametrize("position", [-126, 126, True, 1.5])
def test_position_rejects_invalid_values(position) -> None:
    with pytest.raises((TypeError, ValueError)):
        encode_position(position)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("INVALID", b"BALL INVALID\r\n"),
        ("START", b"BALL START\r\n"),
        ("STOP", b"BALL STOP\r\n"),
        ("PING", b"BALL PING\r\n"),
        ("STATUS", b"BALL STATUS\r\n"),
    ],
)
def test_control_command_formats(command, expected) -> None:
    assert encode_command(command) == expected


def test_ready_requires_exact_complete_line() -> None:
    assert parse_reply(READY_LINE).kind == "ready"
    for text in ("READY", "READY BALL UART2 115200", "xREADY BALL UART2 9600", ""):
        assert parse_reply(text).kind != "ready"


@pytest.mark.parametrize("line", ["OK P", "OK I", "OK C=BALL_START"])
def test_ok_replies(line) -> None:
    assert parse_reply(line).kind == "ok"


def test_error_and_status_parsing_ignore_unknown_fields() -> None:
    error = parse_reply("ERR C=BALL_RANGE,MIN=-125,MAX=125")
    assert error.kind == "error" and error.fields == {"C": "BALL_RANGE", "MIN": -125, "MAX": 125}
    status = parse_reply("BALL S=2,F=0,EN=1,X=5,V=-12,E=5,RQ=800,AP=600,PW=1504,AGE=18,ST=0,AC=25,RJ=1,NEW=99")
    assert status.kind == "status"
    assert status.fields["S"] == 2 and status.fields["V"] == -12
    assert "NEW" not in status.fields


def test_pixel_mapping_direction_and_clamp() -> None:
    right = BallUartClient(left_endpoint_px=72, right_endpoint_px=568)
    left = BallUartClient(left_endpoint_px=72, right_endpoint_px=568, servo_side="left")
    assert right.pixel_x_to_mm(72) == -125
    assert right.pixel_x_to_mm(320) == 0
    assert right.pixel_x_to_mm(568) == 125
    assert left.pixel_x_to_mm(568) == -125
    assert right.pixel_x_to_mm(9999) == 125


class FakeSerial:
    def __init__(self, lines=(), *, fail_read=False) -> None:
        self.lines = list(lines)
        self.written: list[bytes] = []
        self.closed = False
        self.fail_read = fail_read

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def readline(self) -> bytes:
        if self.fail_read:
            self.fail_read = False
            raise OSError("UART disconnected")
        if self.lines:
            return self.lines.pop(0)
        time.sleep(0.002)
        return b""

    def close(self) -> None:
        self.closed = True


def wait_until(predicate, timeout=1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition timed out")


def test_latest_position_overwrites_without_waiting_for_reply() -> None:
    fake = FakeSerial([b"READY BALL UART2 9600\r\n"])
    client = BallUartClient(serial_factory=lambda **_kwargs: fake, send_rate_hz=50)
    client.start()
    wait_until(client.is_ready)
    client.send_start()
    for value in range(20):
        assert client.publish_ball_position(value)
    wait_until(lambda: any(item.startswith(b"BALL POS") for item in fake.written))
    client.close()
    positions = [item for item in fake.written if item.startswith(b"BALL POS")]
    assert positions[-1] == b"BALL POS 19\r\n"
    assert client.get_statistics()["position_replacements"] >= 19


def test_start_stop_survive_full_normal_control_queue() -> None:
    client = BallUartClient()
    with client._outbound_lock:
        client._control.extend([encode_command("PING")] * 32)
    assert client.send_start()
    assert client.send_stop()
    with client._outbound_lock:
        assert encode_command("START") in client._control
        assert encode_command("STOP") in client._control


def test_close_attempts_stop() -> None:
    fake = FakeSerial([b"READY BALL UART2 9600\r\n"])
    client = BallUartClient(serial_factory=lambda **_kwargs: fake)
    client.start()
    wait_until(client.is_ready)
    client.close()
    assert b"BALL STOP\r\n" in fake.written


def test_invalid_frame_uses_latest_slot() -> None:
    fake = FakeSerial([b"READY BALL UART2 9600\r\n"])
    client = BallUartClient(serial_factory=lambda **_kwargs: fake)
    client.start()
    wait_until(client.is_ready)
    client.send_start()
    client.publish_ball_position(20)
    client.send_invalid()
    wait_until(lambda: b"BALL INVALID\r\n" in fake.written)
    client.close()
    assert b"BALL POS 20\r\n" not in fake.written


def test_status_and_error_update_state_without_duplicate_error_logs(caplog) -> None:
    client = BallUartClient()
    with caplog.at_level(logging.WARNING):
        client._handle_line(b"ERR C=BALL_ARG\r\n")
        client._handle_line(b"ERR C=BALL_ARG\r\n")
    client._handle_line(b"BALL S=2,F=0,EN=1,X=5,V=-12,E=5,RQ=800,AP=600,PW=1504,AGE=18,ST=0,AC=25,RJ=1,EXTRA=7\r\n")
    stats = client.get_statistics()
    assert stats["last_uart_error"] == "ERR C=BALL_ARG"
    assert stats["mcu_status"]["EN"] == 1
    assert sum("BALL_ARG" in record.message for record in caplog.records) == 1


def test_serial_factory_receives_exact_9600_8n1_settings() -> None:
    captured = {}
    fake = FakeSerial()

    def factory(**kwargs):
        captured.update(kwargs)
        return fake

    client = BallUartClient(serial_factory=factory)
    client.start()
    wait_until(lambda: bool(captured))
    client.close()
    assert captured == {
        "port": "/dev/ttyAMA0", "baudrate": 9600, "bytesize": 8,
        "parity": "N", "stopbits": 1, "timeout": 0.02,
        "write_timeout": 0.05, "xonxoff": False, "rtscts": False,
        "dsrdtr": False,
    }


def test_read_failure_reconnects_and_waits_for_new_ready() -> None:
    handles = [FakeSerial(fail_read=True), FakeSerial([b"READY BALL UART2 9600\r\n"])]
    client = BallUartClient(
        serial_factory=lambda **_kwargs: handles.pop(0),
        reconnect_interval_s=0.01,
    )
    client.start()
    wait_until(client.is_ready)
    assert client.get_statistics()["reconnects"] >= 1
    client.close()


def test_position_submission_does_not_log_each_frame(caplog) -> None:
    client = BallUartClient()
    client._thread = threading.current_thread()
    with caplog.at_level(logging.INFO):
        for position in range(-50, 50):
            client.publish_ball_position(position)
    client._thread = None
    assert not caplog.records


def test_mission_ball_uart_defaults_match_mcu_contract() -> None:
    config = load_mission_config()["ball_uart"]
    assert config["port"] == "/dev/ttyAMA0"
    assert config["baudrate"] == 9600
    assert config["send_rate_hz"] == 50
    assert config["line_ending"] == "\r\n"


@pytest.mark.parametrize(
    "override",
    [
        {"left_endpoint_px": 10, "right_endpoint_px": 10},
        {"servo_side": "up"},
        {"line_ending": "\n"},
    ],
)
def test_invalid_ball_uart_configuration_is_rejected(override) -> None:
    base = dict(load_mission_config()["ball_uart"])
    base.update(override)
    with pytest.raises(ConfigError, match="ball_uart"):
        load_mission_config(overrides={"ball_uart": base})


def test_runtime_entrypoint_uses_ascii_client_not_binary_serial_service() -> None:
    root = Path(__file__).parents[1]
    app_source = (root / "app.py").read_text(encoding="utf-8")
    client_source = (root / "drivers/ball_uart_client.py").read_text(encoding="utf-8")
    assert "BallUartClient(" in app_source
    assert "SerialService(" not in app_source
    assert "encode_ball_position" not in client_source
    assert "A5" not in client_source and "AA 55" not in client_source
