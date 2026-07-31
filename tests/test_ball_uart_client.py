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
    BallUartState,
    estimated_tx_utilization,
    encode_command,
    encode_position,
    parse_reply,
    worst_case_position_frame_bytes,
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

    def read(self, _size: int = 128) -> bytes:
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


def _queued_intent_commands(client: BallUartClient) -> list[bytes]:
    with client._outbound_lock:
        return [item for item in client._control if item in {encode_command("START"), encode_command("STOP")}]


def test_start_then_stop_keeps_only_final_stop_intent() -> None:
    client = BallUartClient()
    with client._outbound_lock:
        client._control.extend([encode_command("PING")] * 32)
    assert client.send_start()
    assert client.send_stop()
    assert _queued_intent_commands(client) == [encode_command("STOP")]
    command, _ = client._next_outbound(time.monotonic(), 0.0)
    assert command == encode_command("STOP")
    assert encode_command("START") not in client._control


def test_stop_then_start_keeps_only_final_start_intent() -> None:
    client = BallUartClient()
    client._ready = True
    client.send_stop()
    client.send_start()
    assert _queued_intent_commands(client) == [encode_command("START")]
    command, _ = client._next_outbound(time.monotonic(), 0.0)
    assert command == encode_command("START")


def test_repeated_start_and_stop_are_each_deduplicated() -> None:
    client = BallUartClient()
    client.send_start()
    client.send_start()
    assert _queued_intent_commands(client) == [encode_command("START")]
    client.send_stop()
    client.send_stop()
    assert _queued_intent_commands(client) == [encode_command("STOP")]


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


def test_late_control_acks_cannot_override_final_intent() -> None:
    client = BallUartClient()
    client.send_start()
    client.send_stop()
    client._handle_line(b"OK C=BALL_START")
    assert client._state != BallUartState.RUNNING
    assert _queued_intent_commands(client) == [encode_command("STOP")]
    client.send_start()
    client._handle_line(b"OK C=BALL_STOP")
    assert client._state == BallUartState.START_REQUESTED
    assert _queued_intent_commands(client) == [encode_command("START")]


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
        "parity": "N", "stopbits": 1, "timeout": 0.005,
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
    wait_until(lambda: client.get_statistics()["reconnects"] >= 1)
    wait_until(client.is_ready)
    client.close()


def test_historical_ping_ack_is_diagnostic_and_does_not_requeue_start() -> None:
    client = BallUartClient()
    client.send_start()
    client.feed_received(b"OK C=BALL_PING\r\n")
    assert not client.is_ready()
    with client._outbound_lock:
        assert list(client._control).count(encode_command("START")) == 1


def test_other_ok_lines_do_not_establish_ready() -> None:
    client = BallUartClient()
    client.feed_received(b"OK P\rOK I\nOK C=BALL_START\r\n")
    assert not client.is_ready()


def test_cr_lf_crlf_partial_sticky_and_non_ascii_are_safe() -> None:
    client = BallUartClient()
    client.feed_received(b"READY BALL ")
    assert not client.is_ready()
    client.feed_received(b"UART2 9600\rOK P\nOK I\r\n\xff\r")
    stats = client.get_statistics()
    assert stats["mcu_ready"] is True
    assert stats["ok_position_rx_count"] == 1
    assert stats["ok_invalid_rx_count"] == 1
    assert stats["decode_error_count"] == 1


def test_overlong_line_is_discarded_and_parser_recovers() -> None:
    client = BallUartClient()
    client.feed_received(b"X" * 300 + b"\rREADY BALL UART2 9600\n")
    assert client.get_statistics()["line_overflow_count"] == 1
    assert client.is_ready()


def test_absent_replies_do_not_discard_position_or_stop_output() -> None:
    client = BallUartClient()
    client._thread = threading.current_thread()
    client.send_start()
    client.publish_ball_position(12)
    first, next_at = client._next_outbound(time.monotonic(), 0.0)
    second, _ = client._next_outbound(next_at, next_at)
    assert first == encode_command("START")
    assert second == encode_position(12)
    assert client._desired_running is True
    client._thread = None


def test_recovered_link_sends_start_before_new_position() -> None:
    client = BallUartClient()
    client._thread = threading.current_thread()
    client.send_start()
    client.feed_received(b"OK C=BALL_PING\r")
    client.publish_ball_position(8)
    first, next_at = client._next_outbound(time.monotonic(), 0.0)
    second, _ = client._next_outbound(next_at, next_at)
    assert first == encode_command("START")
    assert second == encode_position(8)
    client._thread = None


def test_close_attempts_stop_even_while_waiting_ready() -> None:
    fake = FakeSerial()
    client = BallUartClient(serial_factory=lambda **_kwargs: fake)
    client.start()
    wait_until(client.is_connected)
    client.close()
    assert encode_command("STOP") in fake.written


def _start_stop_writes(written: list[bytes]) -> list[bytes]:
    return [item for item in written if item in {encode_command("START"), encode_command("STOP")}]


class StartDuringStopWriteSerial(FakeSerial):
    def __init__(self, lines=()) -> None:
        super().__init__(lines)
        self.client: BallUartClient | None = None
        self.stop_write_entered = threading.Event()
        self.flipped_to_start = False

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        if data == encode_command("STOP") and not self.flipped_to_start:
            self.flipped_to_start = True
            self.stop_write_entered.set()
            assert self.client is not None
            self.client.send_start()
            time.sleep(0.02)
        return len(data)


def test_stale_stop_write_does_not_satisfy_later_close_stop() -> None:
    fake = StartDuringStopWriteSerial([b"READY BALL UART2 9600\r\n"])
    client = BallUartClient(serial_factory=lambda **_kwargs: fake)
    fake.client = client
    client.start()
    wait_until(client.is_ready)

    client.send_stop()
    assert fake.stop_write_entered.wait(1.0)
    wait_until(lambda: encode_command("START") in fake.written)
    assert not client._stop_sent.is_set()

    client.close()
    assert _start_stop_writes(fake.written)[-1] == encode_command("STOP")
    assert client._ready is False
    assert client._state == BallUartState.CLOSED


def test_close_sends_new_stop_after_historical_stop_then_start() -> None:
    fake = FakeSerial([b"READY BALL UART2 9600\r\n"])
    client = BallUartClient(serial_factory=lambda **_kwargs: fake)
    client.start()
    wait_until(client.is_ready)

    client.send_stop()
    wait_until(lambda: encode_command("STOP") in fake.written)
    wait_until(client._stop_sent.is_set)
    client.send_start()
    wait_until(lambda: encode_command("START") in fake.written)

    client.close()
    control_writes = _start_stop_writes(fake.written)
    assert control_writes.count(encode_command("STOP")) >= 2
    assert control_writes[-1] == encode_command("STOP")
    assert client._ready is False
    assert client._state == BallUartState.CLOSED


class FlipIntentOnWriteSerial(FakeSerial):
    def __init__(self, lines, fail_command: bytes, flip) -> None:
        super().__init__(lines)
        self.fail_command = fail_command
        self.flip = flip
        self.failed = False

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        if data == self.fail_command and not self.failed:
            self.failed = True
            self.flip()
            raise OSError("simulated write failure")
        return len(data)


def _stop_worker_without_changing_intent(client: BallUartClient) -> None:
    client._stop_event.set()
    assert client._thread is not None
    client._thread.join(1.0)
    client._thread = None


def test_failed_start_is_not_requeued_after_user_switches_to_stop() -> None:
    holder = {}
    first = FlipIntentOnWriteSerial(
        [b"READY BALL UART2 9600\r\n"],
        encode_command("START"),
        lambda: holder["client"].send_stop(),
    )
    second = FakeSerial()
    handles = [first, second]
    client = BallUartClient(
        serial_factory=lambda **_kwargs: handles.pop(0),
        reconnect_interval_s=0.01,
    )
    holder["client"] = client
    client.start()
    wait_until(client.is_ready)
    client.send_start()
    wait_until(lambda: encode_command("STOP") in second.written)
    _stop_worker_without_changing_intent(client)
    assert encode_command("START") not in second.written


def test_failed_stop_is_not_requeued_after_user_switches_to_start() -> None:
    holder = {}
    first = FlipIntentOnWriteSerial(
        [b"READY BALL UART2 9600\r\n"],
        encode_command("STOP"),
        lambda: holder["client"].send_start(),
    )
    second = FakeSerial([b"READY BALL UART2 9600\r\n"])
    handles = [first, second]
    client = BallUartClient(
        serial_factory=lambda **_kwargs: handles.pop(0),
        reconnect_interval_s=0.01,
    )
    holder["client"] = client
    client.start()
    wait_until(client.is_ready)
    client.send_stop()
    wait_until(lambda: encode_command("START") in second.written)
    _stop_worker_without_changing_intent(client)
    assert encode_command("STOP") not in second.written


def test_serial_debug_is_visible_at_info_without_position_or_ack_flood(caplog) -> None:
    class ReplyingSerial(FakeSerial):
        def write(self, data: bytes) -> int:
            self.written.append(bytes(data))
            replies = {
                encode_command("PING"): b"OK C=BALL_PING\r\n",
                encode_command("START"): b"OK C=BALL_START\r\n",
                encode_command("STOP"): b"OK C=BALL_STOP\r\n",
                encode_command("STATUS"): b"BALL S=2,EN=1\r\n",
            }
            if data.startswith(b"BALL POS"):
                self.lines.append(b"OK P\r\n")
            elif data in replies:
                self.lines.append(replies[data])
            return len(data)

    fake = ReplyingSerial()
    client = BallUartClient(
        serial_factory=lambda **_kwargs: fake,
        debug=True,
        debug_position_interval_s=0.2,
        send_rate_hz=1000,
    )
    with caplog.at_level(logging.INFO, logger="drivers.ball_uart_client"):
        client.start()
        wait_until(client.is_ready)
        client.send_start()
        wait_until(lambda: encode_command("START") in fake.written)
        wait_until(lambda: client._state == BallUartState.RUNNING)
        for value in range(100):
            client.publish_ball_position((value % 100) - 50)
            client.feed_received(b"OK P\r\n")
        wait_until(lambda: any(item.startswith(b"BALL POS") for item in fake.written))
        client.send_stop()
        wait_until(lambda: encode_command("STOP") in fake.written)
        wait_until(lambda: client._state == BallUartState.STOPPED)
        client.close()
    messages = [record.getMessage() for record in caplog.records]
    joined = "\n".join(messages)
    assert "UART TX BALL PING" not in joined
    assert "waiting for READY" not in joined
    assert "UART OPEN /dev/ttyAMA0 9600 8N1" in joined
    assert "UART TX BALL START" in joined
    assert "UART RX OK C=BALL_START" in joined
    assert "UART TX BALL STOP" in joined
    assert "UART RX OK C=BALL_STOP" in joined
    assert sum("UART TX BALL POS" in item for item in messages) <= 1
    assert not any(item == "UART RX OK P" for item in messages)


def test_position_submission_does_not_log_each_frame(caplog) -> None:
    client = BallUartClient()
    client._thread = threading.current_thread()
    with caplog.at_level(logging.INFO):
        for position in range(-50, 50):
            client.publish_ball_position(position)
    client._thread = None
    assert not caplog.records


def _clear_start_stop_controls(client: BallUartClient) -> None:
    with client._outbound_lock:
        client._control = type(client._control)(
            item
            for item in client._control
            if item not in {encode_command("START"), encode_command("STOP")}
        )


def test_status_enabled_zero_is_diagnostic_and_does_not_requeue_start() -> None:
    client = BallUartClient()
    client.send_start()
    _clear_start_stop_controls(client)
    client._handle_line(b"BALL EN=0")
    with client._outbound_lock:
        assert encode_command("START") not in client._control
        assert encode_command("STOP") not in client._control
    assert client._desired_running is True
    assert client.get_statistics()["mcu_status"]["EN"] == 0


def test_status_enabled_one_is_diagnostic_and_does_not_change_stop_intent() -> None:
    client = BallUartClient()
    client.send_stop()
    _clear_start_stop_controls(client)
    client._handle_line(b"BALL EN=1")
    with client._outbound_lock:
        assert encode_command("START") not in client._control
        assert encode_command("STOP") not in client._control
    assert client._desired_running is False


def test_status_and_late_acks_never_override_final_intent() -> None:
    client = BallUartClient()
    client.send_stop()
    client._handle_line(b"OK C=BALL_START\r\n")
    assert client._desired_running is False
    with client._outbound_lock:
        assert encode_command("STOP") in client._control
    client.send_start()
    client._handle_line(b"OK C=BALL_STOP\r\n")
    assert client._desired_running is True
    with client._outbound_lock:
        assert encode_command("START") in client._control


def test_mission_ball_uart_defaults_match_mcu_contract() -> None:
    config = load_mission_config()["ball_uart"]
    assert config["port"] == "/dev/ttyAMA0"
    assert config["baudrate"] == 9600
    assert config["send_rate_hz"] == 50
    assert config["line_ending"] == "\r\n"


@pytest.mark.parametrize(
    "override",
    [
        {"line_ending": "\n"},
        {"wait_ready": True},
    ],
)
def test_invalid_ball_uart_configuration_is_rejected(override) -> None:
    base = dict(load_mission_config()["ball_uart"])
    base.update(override)
    with pytest.raises(ConfigError, match="ball_uart"):
        load_mission_config(overrides={"ball_uart": base})


def test_runtime_entrypoint_uses_ascii_client_not_removed_binary_uart() -> None:
    root = Path(__file__).parents[1]
    app_source = (root / "app.py").read_text(encoding="utf-8")
    client_source = (root / "drivers/ball_uart_client.py").read_text(encoding="utf-8")
    assert "BallUartClient(" in app_source
    assert "SerialService(" not in app_source
    assert "encode_ball_position" not in client_source
    assert "A5" not in client_source and "AA 55" not in client_source


def test_9600_8n1_50hz_bandwidth_uses_actual_maximum_frame() -> None:
    assert worst_case_position_frame_bytes() == len(b"BALL POS -125\r\n") == 15
    assert estimated_tx_utilization(9600, 50) == pytest.approx(0.78125)
    assert estimated_tx_utilization(9600, 50) < 0.85


def test_continuous_provider_produces_about_500_outputs_in_ten_seconds() -> None:
    client = BallUartClient(continuous_output=True)
    client._thread = threading.current_thread()
    client.set_output_provider(lambda _now: None)
    client.send_start()
    data, deadline = client._next_outbound(100.0, 0.0)
    assert data == encode_command("START")
    outputs = []
    for _ in range(500):
        data, deadline = client._next_outbound(deadline, deadline)
        outputs.append(data)
    client._thread = None
    assert outputs == [encode_command("INVALID")] * 500


def test_continuous_provider_repeats_current_state_without_new_publication() -> None:
    client = BallUartClient(continuous_output=True)
    client.set_output_provider(lambda _now: 7)
    client.send_start()
    first, deadline = client._next_outbound(200.0, 0.0)
    second, deadline = client._next_outbound(deadline, deadline)
    third, _ = client._next_outbound(deadline, deadline)
    assert first == encode_command("START")
    assert second == third == encode_position(7)


def test_missed_deadlines_are_skipped_without_catchup_burst() -> None:
    client = BallUartClient(continuous_output=True)
    client.set_output_provider(lambda _now: 1)
    client.send_start()
    _, deadline = client._next_outbound(300.0, 0.0)
    delayed = deadline + 0.105
    output, next_deadline = client._next_outbound(delayed, deadline)
    immediate, same_deadline = client._next_outbound(delayed, next_deadline)
    assert output == encode_position(1)
    assert immediate is None and same_deadline == next_deadline
    assert client.get_statistics()["uart_tx_deadline_miss_count"] >= 5


def test_provider_is_called_outside_uart_outbound_lock() -> None:
    client = BallUartClient(continuous_output=True)
    lock_was_free = []

    def provider(_now):
        acquired = client._outbound_lock.acquire(blocking=False)
        lock_was_free.append(acquired)
        if acquired:
            client._outbound_lock.release()
        return 0

    client.set_output_provider(provider)
    client.send_start()
    _, deadline = client._next_outbound(400.0, 0.0)
    assert client._next_outbound(deadline, deadline)[0] == encode_position(0)
    assert lock_was_free == [True]


def test_stop_suppresses_continuous_provider_output() -> None:
    client = BallUartClient(continuous_output=True)
    client.set_output_provider(lambda _now: 3)
    client.send_start()
    _, deadline = client._next_outbound(500.0, 0.0)
    client.send_stop()
    stop, deadline = client._next_outbound(deadline, deadline)
    output, _ = client._next_outbound(deadline, deadline)
    assert stop == encode_command("STOP")
    assert output is None


def test_combined_output_rate_and_jitter_statistics_are_reported() -> None:
    fake = FakeSerial()
    client = BallUartClient(
        serial_factory=lambda **_kwargs: fake,
        continuous_output=True,
        send_rate_hz=50,
    )
    client.set_output_provider(lambda _now: None)
    client.send_start()
    client.start()
    wait_until(lambda: client.get_statistics()["output_tx_count"] >= 8)
    client.close()
    stats = client.get_statistics()
    assert stats["output_tx_count"] == stats["position_tx_count"] + stats["invalid_tx_count"]
    assert stats["output_tx_hz"] > 30.0
    assert stats["uart_output_period_ms"] == pytest.approx(20.0)
    assert stats["uart_tx_jitter_ms"] >= 0.0
    assert stats["uart_tx_jitter_p95_ms"] >= 0.0
