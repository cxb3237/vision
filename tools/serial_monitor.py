"""监视或模拟 VMC-Link v1 固定视觉结果流。"""

from __future__ import annotations

import argparse
import time

from core.models import TargetState, VisionResult
from protocol.vmc_link import DetectorID, VMCLinkParser, VMCLinkResult, encode_result_packet


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VMC-Link v1 串口监视器")
    parser.add_argument("--port", help="串口设备，例如 /dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--simulate", action="store_true", help="使用内存模拟，不访问串口")
    parser.add_argument("--rate", type=float, default=20.0, help="模拟发送频率 Hz")
    parser.add_argument("--count", type=int, default=0, help="模拟包数；0 表示持续运行")
    return parser


def _format_packet(packet: VMCLinkResult, hz: float, lost: int) -> str:
    return (
        f"seq={packet.sequence:5d} detector={packet.detector_id} state={packet.state} "
        f"class={packet.target_class} center=({packet.center_x_px},{packet.center_y_px}) "
        f"error=({packet.error_x_permille},{packet.error_y_permille})permille "
        f"confidence={packet.confidence_permille} distance={packet.distance_mm}mm "
        f"flags=0x{packet.flags:02X} CRC=OK rate={hz:.1f}Hz lost={lost}"
    )


class MonitorStatistics:
    def __init__(self) -> None:
        self.started: float | None = None
        self.count = 0
        self.lost = 0
        self.last_sequence: int | None = None

    def record(self, packet: VMCLinkResult) -> str:
        now = time.monotonic()
        if self.started is None:
            self.started = now
        if self.last_sequence is not None:
            missing = (packet.sequence - self.last_sequence - 1) & 0xFFFF
            if missing < 0x8000:
                self.lost += missing
        self.last_sequence = packet.sequence
        self.count += 1
        elapsed = max(now - self.started, 1e-6)
        frequency = 0.0 if self.count < 2 else (self.count - 1) / elapsed
        return _format_packet(packet, frequency, self.lost)


def _simulation_packet(sequence: int) -> bytes:
    digit = sequence % 10
    result = VisionResult(
        frame_id=sequence,
        capture_timestamp=time.monotonic(),
        process_timestamp=time.monotonic(),
        found=True,
        target_state=TargetState.LOCKED,
        target_class=100 + digit,
        center_x=320 + digit,
        center_y=240,
        bbox_width=64,
        bbox_height=96,
        confidence=900,
        image_width=640,
        image_height=480,
    )
    return encode_result_packet(result, sequence, DetectorID.DIGIT)


def _run_simulation(rate: float, count: int) -> None:
    if rate <= 0 or count < 0:
        raise ValueError("rate 必须为正数且 count 不能为负数")
    parser = VMCLinkParser()
    statistics = MonitorStatistics()
    sequence = 0
    generated = 0
    while count == 0 or generated < count:
        for packet in parser.feed(_simulation_packet(sequence)):
            print(statistics.record(packet), flush=True)
        sequence = (sequence + 1) & 0xFFFF
        generated += 1
        time.sleep(1.0 / rate)


def _run_serial(port: str, baudrate: int) -> None:
    import serial

    parser = VMCLinkParser()
    statistics = MonitorStatistics()
    previous_crc_errors = 0
    with serial.serial_for_url(port, baudrate, timeout=0.1) as connection:
        while True:
            data = connection.read(256)
            for packet in parser.feed(data):
                print(statistics.record(packet), flush=True)
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
