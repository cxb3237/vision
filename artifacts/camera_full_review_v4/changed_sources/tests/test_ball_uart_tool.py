from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest

from tools import test_ball_uart


class FakeSerial:
    def __init__(self, replies: list[bytes] | None = None) -> None:
        self.replies = list(replies or [])
        self.written: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def read(self, _size: int) -> bytes:
        return self.replies.pop(0) if self.replies else b""


def install_fake_serial(monkeypatch, fake_or_error) -> None:
    def factory(**_kwargs):
        if isinstance(fake_or_error, Exception):
            raise fake_or_error
        return fake_or_error

    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=factory))


@pytest.mark.parametrize(
    ("argv", "reply"),
    [
        (["ping"], b"OK C=BALL_PING\r\n"),
        (["status"], b"OK C=BALL_STATUS\r\nBALL S=2,EN=0\r\n"),
        (["start"], b"OK C=BALL_START\r\n"),
        (["stop"], b"OK C=BALL_STOP\r\n"),
        (["pos", "0"], b"OK P\r\n"),
        (["invalid"], b"OK I\r\n"),
    ],
)
def test_each_uart_command_requires_its_expected_reply(monkeypatch, argv, reply) -> None:
    fake = FakeSerial([reply])
    install_fake_serial(monkeypatch, fake)
    assert test_ball_uart.main(["--timeout", "0.05", *argv]) == 0
    assert fake.written


def test_no_reply_returns_timeout(monkeypatch, capsys) -> None:
    install_fake_serial(monkeypatch, FakeSerial())
    assert test_ball_uart.main(["--timeout", "0.01", "ping"]) == 3
    assert "RX TIMEOUT: expected OK C=BALL_PING" in capsys.readouterr().err


def test_err_reply_returns_four(monkeypatch) -> None:
    install_fake_serial(monkeypatch, FakeSerial([b"ERR C=BALL_START,M=BUSY\r\n"]))
    assert test_ball_uart.main(["--timeout", "0.05", "start"]) == 4


def test_status_without_status_line_times_out(monkeypatch) -> None:
    install_fake_serial(monkeypatch, FakeSerial([b"OK C=BALL_STATUS\r\n"]))
    assert test_ball_uart.main(["--timeout", "0.01", "status"]) == 3


@pytest.mark.parametrize("value", ["-126", "126", "1.5"])
def test_position_out_of_range_or_non_integer_returns_two(monkeypatch, value) -> None:
    install_fake_serial(monkeypatch, FakeSerial())
    assert test_ball_uart.main(["pos", value]) == 2


def test_serial_open_failure_returns_five(monkeypatch) -> None:
    install_fake_serial(monkeypatch, OSError("port busy"))
    assert test_ball_uart.main(["ping"]) == 5
