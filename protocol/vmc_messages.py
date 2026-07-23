"""冻结的 VMC-Link V1.0 消息定义和负载打包。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, IntFlag
import struct

from core.models import VisionResult
from core.state_machine import VisionMode


class MessageType(IntEnum):
    """VMC-Link 消息类型。"""

    HELLO = 0x01
    HEARTBEAT = 0x02
    ACK = 0x03
    VISION_TARGET = 0x10
    VISION_EVENT = 0x11
    MOTION_REQUEST = 0x20
    VEHICLE_STATE = 0x30
    NAV_EVENT = 0x31
    VISION_CONTROL = 0x40


class AckResult(IntEnum):
    """协议规定的 ACK 结果码。"""

    OK = 0
    BUSY = 1
    INVALID_PARAMETER = 2
    UNSUPPORTED = 3
    DENIED_BY_STATE = 4
    TIMEOUT = 5
    INTERNAL_ERROR = 6
    DUPLICATE = 7


class Flags(IntFlag):
    """可合法组合的 VMC-Link 帧标志。"""

    NONE = 0
    ACK_REQ = 1
    RETRY = 2
    URGENT = 4
    STREAM = 8


VISION_TARGET_FORMAT = "<IBBHhhhhHHHHH"
HEARTBEAT_FORMAT = "<IBBHHH"
ACK_FORMAT = "<BBBB"
VISION_CONTROL_FORMAT = "<HBBHhhH"

VISION_TARGET_SIZE = struct.calcsize(VISION_TARGET_FORMAT)
HEARTBEAT_SIZE = struct.calcsize(HEARTBEAT_FORMAT)
ACK_SIZE = struct.calcsize(ACK_FORMAT)
VISION_CONTROL_SIZE = struct.calcsize(VISION_CONTROL_FORMAT)


def _check_length(data: bytes, expected: int, name: str) -> None:
    if len(data) != expected:
        raise ValueError(f"{name} 负载必须为 {expected} 字节，实际 {len(data)} 字节")


def _check_range(name: str, value: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须为整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}..{maximum} 范围内")
    return value


def _unpack_exact(fmt: str, data: bytes, size: int, name: str) -> tuple[int, ...]:
    _check_length(data, size, name)
    try:
        return struct.unpack(fmt, data)
    except struct.error as exc:
        raise ValueError(f"{name} 负载无法解包: {exc}") from exc


@dataclass(slots=True)
class VisionTarget:
    """固定 26 字节的视觉目标负载。"""

    frame_id: int
    vision_mode: int
    target_state: int
    target_class: int
    error_x_px: int
    error_y_px: int
    azimuth_cdeg: int = -32768
    elevation_cdeg: int = -32768
    distance_mm: int = 0xFFFF
    confidence: int = 0
    bbox_width_px: int = 0
    bbox_height_px: int = 0
    processing_delay_ms: int = 0

    def pack(self) -> bytes:
        """验证字段范围并打包为冻结协议负载。"""

        values = (
            _check_range("frame_id", self.frame_id, 0, 0xFFFFFFFF),
            _check_range("vision_mode", self.vision_mode, 0, 0xFF),
            _check_range("target_state", self.target_state, 0, 0xFF),
            _check_range("target_class", self.target_class, 0, 0xFFFF),
            _check_range("error_x_px", self.error_x_px, -32768, 32767),
            _check_range("error_y_px", self.error_y_px, -32768, 32767),
            _check_range("azimuth_cdeg", self.azimuth_cdeg, -32768, 32767),
            _check_range("elevation_cdeg", self.elevation_cdeg, -32768, 32767),
            _check_range("distance_mm", self.distance_mm, 0, 0xFFFF),
            _check_range("confidence", self.confidence, 0, 0xFFFF),
            _check_range("bbox_width_px", self.bbox_width_px, 0, 0xFFFF),
            _check_range("bbox_height_px", self.bbox_height_px, 0, 0xFFFF),
            _check_range("processing_delay_ms", self.processing_delay_ms, 0, 0xFFFF),
        )
        return struct.pack(VISION_TARGET_FORMAT, *values)

    @classmethod
    def unpack(cls, data: bytes) -> "VisionTarget":
        """从恰好 26 字节的负载解包。"""

        return cls(*_unpack_exact(VISION_TARGET_FORMAT, data, VISION_TARGET_SIZE, "VISION_TARGET"))

    @classmethod
    def from_result(cls, result: VisionResult, mode: int) -> "VisionTarget":
        """由通用视觉结果创建协议负载。"""

        return cls(
            result.frame_id & 0xFFFFFFFF,
            mode,
            int(result.target_state),
            result.target_class,
            result.error_x_px,
            result.error_y_px,
            distance_mm=result.distance_mm,
            confidence=result.confidence,
            bbox_width_px=result.bbox_width,
            bbox_height_px=result.bbox_height,
            processing_delay_ms=min(max(result.processing_delay_ms, 0), 0xFFFF),
        )


@dataclass(slots=True)
class Heartbeat:
    """固定 12 字节的视觉端心跳负载。"""

    uptime_ms: int
    system_state: int
    active_mode: int
    fault_bits: int
    rx_good_count: int
    rx_crc_error_count: int

    def pack(self) -> bytes:
        """验证字段并打包心跳，uptime 按 uint32 回绕。"""

        if isinstance(self.uptime_ms, bool) or not isinstance(self.uptime_ms, int):
            raise ValueError("uptime_ms 必须为整数")
        values = (
            _check_range("uptime_ms", self.uptime_ms & 0xFFFFFFFF, 0, 0xFFFFFFFF),
            _check_range("system_state", self.system_state, 0, 0xFF),
            _check_range("active_mode", self.active_mode, 0, 0xFF),
            _check_range("fault_bits", self.fault_bits, 0, 0xFFFF),
            _check_range("rx_good_count", self.rx_good_count, 0, 0xFFFF),
            _check_range("rx_crc_error_count", self.rx_crc_error_count, 0, 0xFFFF),
        )
        return struct.pack(HEARTBEAT_FORMAT, *values)

    @classmethod
    def unpack(cls, data: bytes) -> "Heartbeat":
        """从恰好 12 字节的负载解包。"""

        return cls(*_unpack_exact(HEARTBEAT_FORMAT, data, HEARTBEAT_SIZE, "HEARTBEAT"))


@dataclass(slots=True)
class Ack:
    """固定 4 字节的消息确认。"""

    acknowledged_type: int
    acknowledged_sequence: int
    result: int
    detail: int = 0

    def pack(self) -> bytes:
        """验证并打包 ACK。"""

        values = (
            _check_range("acknowledged_type", self.acknowledged_type, 0, 0xFF),
            _check_range("acknowledged_sequence", self.acknowledged_sequence, 0, 0xFF),
            _check_range("result", self.result, 0, 0xFF),
            _check_range("detail", self.detail, 0, 0xFF),
        )
        return struct.pack(ACK_FORMAT, *values)

    @classmethod
    def unpack(cls, data: bytes) -> "Ack":
        """从恰好 4 字节的负载解包。"""

        return cls(*_unpack_exact(ACK_FORMAT, data, ACK_SIZE, "ACK"))


@dataclass(slots=True)
class VisionControl:
    """固定 12 字节的视觉控制请求。"""

    request_id: int
    mode: int
    options: int = 0
    target_class: int = 0
    param1: int = 0
    param2: int = 0
    timeout_ms: int = 0

    def pack(self) -> bytes:
        """验证并打包视觉控制请求。"""

        values = (
            _check_range("request_id", self.request_id, 0, 0xFFFF),
            _check_range("mode", self.mode, 0, 0xFF),
            _check_range("options", self.options, 0, 0xFF),
            _check_range("target_class", self.target_class, 0, 0xFFFF),
            _check_range("param1", self.param1, -32768, 32767),
            _check_range("param2", self.param2, -32768, 32767),
            _check_range("timeout_ms", self.timeout_ms, 0, 0xFFFF),
        )
        return struct.pack(VISION_CONTROL_FORMAT, *values)

    @classmethod
    def unpack(cls, data: bytes) -> "VisionControl":
        """从恰好 12 字节的负载解包。"""

        return cls(
            *_unpack_exact(
                VISION_CONTROL_FORMAT,
                data,
                VISION_CONTROL_SIZE,
                "VISION_CONTROL",
            )
        )
