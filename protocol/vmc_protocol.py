"""无阻塞流式 VMC-Link 编解码。"""

from __future__ import annotations

from dataclasses import dataclass

from protocol.crc16 import crc16_ccitt_false


SOF = b"\xAA\x55"
VERSION = 0x10
MAX_PAYLOAD = 64
HEADER_SIZE = 8
TRAILER_SIZE = 2


@dataclass(slots=True)
class VmcPacket:
    """解析后的 VMC-Link 数据包。"""

    message_type: int
    flags: int
    sequence: int
    payload: bytes


def encode_packet(message_type: int, flags: int, sequence: int, payload: bytes) -> bytes:
    """编码一个完整 VMC-Link 数据包。"""

    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload 长度不能超过 {MAX_PAYLOAD}")
    for name, value in (
        ("message_type", message_type),
        ("flags", flags),
        ("sequence", sequence),
    ):
        if not 0 <= int(value) <= 0xFF:
            raise ValueError(f"{name} 必须在 0..255 范围内")
    body = bytes((VERSION, message_type, flags, sequence))
    body += len(payload).to_bytes(2, "little") + payload
    return SOF + body + crc16_ccitt_false(body).to_bytes(2, "little")


class VmcStreamParser:
    """支持半包、粘包、噪声和 CRC 错误恢复的流式解析器。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.good_count = 0
        self.crc_error_count = 0
        self.length_error_count = 0
        self.dropped_bytes = 0

    def feed(self, data: bytes) -> list[VmcPacket]:
        """输入任意长度字节并返回本次解析出的完整包。"""

        self._buffer.extend(data)
        packets: list[VmcPacket] = []
        while True:
            position = self._buffer.find(SOF)
            if position < 0:
                keep = 1 if self._buffer[-1:] == SOF[:1] else 0
                self.dropped_bytes += len(self._buffer) - keep
                del self._buffer[: len(self._buffer) - keep]
                break
            if position:
                self.dropped_bytes += position
                del self._buffer[:position]
            if len(self._buffer) < HEADER_SIZE:
                break
            if self._buffer[2] != VERSION:
                self.dropped_bytes += 1
                del self._buffer[0]
                continue
            payload_length = int.from_bytes(self._buffer[6:8], "little")
            if payload_length > MAX_PAYLOAD:
                self.length_error_count += 1
                self.dropped_bytes += 1
                del self._buffer[0]
                continue
            total = HEADER_SIZE + payload_length + TRAILER_SIZE
            if len(self._buffer) < total:
                break
            body = bytes(self._buffer[2 : HEADER_SIZE + payload_length])
            expected_crc = int.from_bytes(
                self._buffer[HEADER_SIZE + payload_length : total],
                "little",
            )
            if crc16_ccitt_false(body) != expected_crc:
                self.crc_error_count += 1
                self.dropped_bytes += 1
                del self._buffer[0]
                continue
            packets.append(VmcPacket(body[1], body[2], body[3], body[6:]))
            self.good_count += 1
            del self._buffer[:total]
        return packets
