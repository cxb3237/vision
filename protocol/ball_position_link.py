"""Four-byte Raspberry Pi to MCU steel-ball position link."""

from __future__ import annotations

from dataclasses import dataclass
import struct


BALL_POSITION_SOF = bytes((0xA5, 0x5A))
BALL_POSITION_PACKET_SIZE = 4
BALL_POSITION_MIN_MM = -125
BALL_POSITION_MAX_MM = 125


def crc8_atm(data: bytes | bytearray | memoryview) -> int:
    """Return CRC-8/ATM (poly=0x07, init=0x00, no reflection/xorout)."""

    crc = 0
    for value in data:
        crc ^= int(value)
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


@dataclass(frozen=True, slots=True)
class BallPositionPacket:
    """Decoded signed lateral position in millimetres."""

    x_mm: int

    def __post_init__(self) -> None:
        if isinstance(self.x_mm, bool) or not isinstance(self.x_mm, int):
            raise TypeError("x_mm must be an integer")
        if not BALL_POSITION_MIN_MM <= self.x_mm <= BALL_POSITION_MAX_MM:
            raise ValueError("x_mm must be in -125..125")

    def encode(self) -> bytes:
        return encode_ball_position(self.x_mm)


def encode_ball_position(x_mm: int) -> bytes:
    packet = BallPositionPacket(x_mm)
    prefix = BALL_POSITION_SOF + struct.pack("b", packet.x_mm)
    return prefix + bytes((crc8_atm(prefix),))


def decode_ball_position(data: bytes | bytearray | memoryview) -> BallPositionPacket:
    raw = bytes(data)
    if len(raw) != BALL_POSITION_PACKET_SIZE:
        raise ValueError("ball position packet must contain exactly 4 bytes")
    if raw[:2] != BALL_POSITION_SOF:
        raise ValueError("invalid ball position header")
    if crc8_atm(raw[:3]) != raw[3]:
        raise ValueError("invalid ball position CRC")
    return BallPositionPacket(struct.unpack("b", raw[2:3])[0])


class BallPositionParser:
    """Noise-tolerant parser for partial and concatenated position packets."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.good_count = 0
        self.crc_error_count = 0

    def reset(self) -> None:
        self._buffer.clear()
        self.good_count = 0
        self.crc_error_count = 0

    def feed(self, data: bytes | bytearray | memoryview) -> list[BallPositionPacket]:
        if data:
            self._buffer.extend(data)
        packets: list[BallPositionPacket] = []
        while True:
            start = self._buffer.find(BALL_POSITION_SOF)
            if start < 0:
                self._buffer[:] = b"\xA5" if self._buffer.endswith(b"\xA5") else b""
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < BALL_POSITION_PACKET_SIZE:
                break
            candidate = bytes(self._buffer[:BALL_POSITION_PACKET_SIZE])
            try:
                packet = decode_ball_position(candidate)
            except ValueError as exc:
                if "CRC" in str(exc):
                    self.crc_error_count += 1
                del self._buffer[0]
                continue
            packets.append(packet)
            self.good_count += 1
            del self._buffer[:BALL_POSITION_PACKET_SIZE]
        return packets


BallPositionStreamParser = BallPositionParser
