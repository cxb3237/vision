"""MSPM0G3507 串口协议交互模拟器。"""

from __future__ import annotations

import argparse
import time

from core.state_machine import VisionMode
from protocol.vmc_messages import Flags, Heartbeat, MessageType, VisionControl, VisionTarget
from protocol.vmc_protocol import VmcStreamParser, encode_packet


def build_argument_parser() -> argparse.ArgumentParser:
    """创建 MSPM0 模拟器参数解析器。"""

    parser = argparse.ArgumentParser(description="持续发送心跳并读取视觉目标")
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--port", help="串口或 pyserial URL")
    destination.add_argument("--console", action="store_true", help="仅向控制台输出可读心跳")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--heartbeat-hz", type=float, default=1.0)
    parser.add_argument(
        "--mode",
        choices=[item.name.lower() for item in VisionMode if item != VisionMode.FAULT],
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """打开串口一次，周期发送心跳/控制并持续解析视觉目标。"""

    args = build_argument_parser().parse_args(argv)
    if args.heartbeat_hz <= 0 or args.baudrate <= 0:
        raise SystemExit("波特率和心跳频率必须为正数")
    serial_handle = None
    parser = VmcStreamParser()
    sequence = 0
    started = time.monotonic()
    next_heartbeat = started
    control_sent = False
    try:
        if args.port:
            import serial

            serial_handle = serial.serial_for_url(args.port, args.baudrate, timeout=0.05)
        while True:
            now = time.monotonic()
            if now >= next_heartbeat:
                uptime_ms = int((now - started) * 1000) & 0xFFFFFFFF
                heartbeat = Heartbeat(uptime_ms, 0, 0, 0, parser.good_count, parser.crc_error_count)
                packet = encode_packet(MessageType.HEARTBEAT, 0, sequence, heartbeat.pack())
                sequence = (sequence + 1) & 0xFF
                if serial_handle is not None:
                    serial_handle.write(packet)
                else:
                    print(f"HEARTBEAT uptime_ms={uptime_ms} sequence={(sequence - 1) & 0xFF}")
                next_heartbeat = now + 1.0 / args.heartbeat_hz
            if args.mode and not control_sent:
                mode = VisionMode[args.mode.upper()]
                control = VisionControl(request_id=1, mode=int(mode)).pack()
                packet = encode_packet(
                    MessageType.VISION_CONTROL,
                    int(Flags.ACK_REQ),
                    sequence,
                    control,
                )
                sequence = (sequence + 1) & 0xFF
                if serial_handle is not None:
                    serial_handle.write(packet)
                else:
                    print(f"VISION_CONTROL mode={mode.name}")
                control_sent = True
            if serial_handle is not None:
                for packet in parser.feed(serial_handle.read(256)):
                    if packet.message_type == MessageType.VISION_TARGET:
                        target = VisionTarget.unpack(packet.payload)
                        print(
                            "VISION_TARGET "
                            f"frame={target.frame_id} mode={target.vision_mode} "
                            f"state={target.target_state} class={target.target_class} "
                            f"error=({target.error_x_px},{target.error_y_px}) "
                            f"confidence={target.confidence}"
                        )
                    else:
                        print(
                            f"message type=0x{packet.message_type:02X} "
                            f"sequence={packet.sequence} payload={packet.payload.hex()}"
                        )
            time.sleep(0.005)
    except KeyboardInterrupt:
        return 0
    finally:
        if serial_handle is not None:
            serial_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
