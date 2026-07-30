"""Manual Raspberry Pi/MSPM0 ASCII UART diagnostic utility."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.ball_uart_client import encode_command, encode_position


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MSPM0钢球ASCII UART测试")
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--timeout", type=float, default=2.0, help="等待预期回复的秒数")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("ping", "status", "start", "invalid", "stop", "monitor"):
        subparsers.add_parser(command)
    position = subparsers.add_parser("pos")
    position.add_argument("position_mm", type=int)
    return parser


def command_bytes(args: argparse.Namespace) -> bytes | None:
    if args.command == "monitor":
        return None
    if args.command == "pos":
        return encode_position(args.position_mm)
    return encode_command(args.command.upper())


def expected_reply(command: str) -> tuple[str, Callable[[str], bool]]:
    expectations = {
        "ping": ("OK C=BALL_PING", lambda line: line == "OK C=BALL_PING"),
        "status": ("BALL S=...", lambda line: line.startswith("BALL S=")),
        "start": ("OK C=BALL_START", lambda line: line == "OK C=BALL_START"),
        "stop": ("OK C=BALL_STOP", lambda line: line == "OK C=BALL_STOP"),
        "pos": ("OK P", lambda line: line == "OK P"),
        "invalid": ("OK I", lambda line: line == "OK I"),
    }
    return expectations[command]


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.baudrate <= 0 or args.timeout <= 0:
        print("参数错误: baudrate 和 timeout 必须为正数", file=sys.stderr)
        return 2
    try:
        outbound = command_bytes(args)
    except (TypeError, ValueError) as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2
    try:
        import serial
    except ImportError as exc:
        print(f"UART失败: 缺少 pyserial: {exc}", file=sys.stderr)
        return 5

    try:
        with serial.Serial(
            port=args.port, baudrate=args.baudrate, bytesize=8, parity="N",
            stopbits=1, timeout=0.02, write_timeout=0.05,
            xonxoff=False, rtscts=False, dsrdtr=False,
        ) as connection:
            if outbound is not None:
                written = connection.write(outbound)
                if written is not None and written != len(outbound):
                    raise OSError(f"串口短写: {written}/{len(outbound)} bytes")
                print(f"TX: {outbound.decode('ascii').rstrip()}", flush=True)
            deadline = None if args.command == "monitor" else time.monotonic() + args.timeout
            expected_text, matches_expected = (
                ("", lambda _line: False)
                if args.command == "monitor"
                else expected_reply(args.command)
            )
            buffer = bytearray()
            while deadline is None or time.monotonic() < deadline:
                chunk = connection.read(128)
                if not chunk:
                    time.sleep(0.001)
                    continue
                for byte in chunk:
                    if byte in (0x0D, 0x0A):
                        if buffer:
                            line = buffer.decode("ascii", errors="replace")
                            print(f"RX: {line}", flush=True)
                            buffer.clear()
                            if line.startswith("ERR C="):
                                return 4
                            if args.command != "monitor" and matches_expected(line):
                                return 0
                    elif len(buffer) < 256:
                        buffer.append(byte)
                    else:
                        buffer.clear()
                        print("RX: <line too long; discarded>", flush=True)
            if args.command != "monitor":
                print(f"RX TIMEOUT: expected {expected_text}", file=sys.stderr, flush=True)
                return 3
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"UART失败: {exc}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
