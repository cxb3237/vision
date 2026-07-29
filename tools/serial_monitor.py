"""Monitor or simulate the four-byte steel-ball position stream."""

from __future__ import annotations

import argparse
import time

from protocol.ball_position_link import BallPositionParser, encode_ball_position


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="钢球4字节位置包串口监视器")
    parser.add_argument("--port", help="串口设备，例如 /dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--simulate", action="store_true", help="使用内存模拟，不访问串口")
    parser.add_argument("--rate", type=float, default=20.0, help="模拟发送频率 Hz")
    parser.add_argument("--count", type=int, default=0, help="模拟包数；0 表示持续运行")
    return parser


class MonitorStatistics:
    def __init__(self) -> None:
        self.started: float | None = None
        self.count = 0

    def record(self, x_mm: int) -> str:
        now = time.monotonic()
        if self.started is None:
            self.started = now
        self.count += 1
        elapsed = max(now - self.started, 1e-6)
        frequency = 0.0 if self.count < 2 else (self.count - 1) / elapsed
        return f"x_mm={x_mm:+d} CRC=OK rate={frequency:.1f}Hz count={self.count}"


def _run_simulation(rate: float, count: int) -> None:
    if rate <= 0 or count < 0:
        raise ValueError("rate 必须为正数且 count 不能为负数")
    parser = BallPositionParser()
    statistics = MonitorStatistics()
    generated = 0
    while count == 0 or generated < count:
        x_mm = -125 + generated % 251
        for packet in parser.feed(encode_ball_position(x_mm)):
            print(statistics.record(packet.x_mm), flush=True)
        generated += 1
        time.sleep(1.0 / rate)


def _run_serial(port: str, baudrate: int) -> None:
    import serial

    parser = BallPositionParser()
    statistics = MonitorStatistics()
    previous_crc_errors = 0
    with serial.serial_for_url(port, baudrate, timeout=0.1) as connection:
        while True:
            data = connection.read(256)
            for packet in parser.feed(data):
                print(statistics.record(packet.x_mm), flush=True)
            if parser.crc_error_count != previous_crc_errors:
                print(f"CRC errors={parser.crc_error_count}", flush=True)
                previous_crc_errors = parser.crc_error_count


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if not args.simulate and not args.port:
        raise SystemExit("必须提供 --port，或使用 --simulate")
    try:
        if args.simulate:
            _run_simulation(args.rate, args.count)
        else:
            _run_serial(args.port, args.baudrate)
    except KeyboardInterrupt:
        print("\nmonitor stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
