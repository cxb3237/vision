"""Manual Raspberry Pi/MSPM0 ASCII UART diagnostic utility."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.ball_uart_client import encode_command, encode_position


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MSPM0钢球ASCII UART测试")
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--baudrate", type=int, default=9600)
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outbound = command_bytes(args)
    except (TypeError, ValueError) as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2
    import serial

    try:
        with serial.Serial(
            port=args.port, baudrate=args.baudrate, bytesize=8, parity="N",
            stopbits=1, timeout=0.02, write_timeout=0.05,
            xonxoff=False, rtscts=False, dsrdtr=False,
        ) as connection:
            if outbound is not None:
                connection.write(outbound)
            deadline = None if args.command == "monitor" else time.monotonic() + 2.0
            while deadline is None or time.monotonic() < deadline:
                line = connection.readline()
                if not line:
                    continue
                print(line.decode("ascii", errors="replace").rstrip("\r\n"), flush=True)
    except (OSError, serial.SerialException) as exc:
        print(f"UART失败: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
