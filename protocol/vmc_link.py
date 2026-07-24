"""VMC-Link v1 固定 34 字节视觉结果协议。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum, IntFlag
import math
import struct
from typing import TYPE_CHECKING, Iterable

from protocol.crc16 import crc16_ccitt_false

if TYPE_CHECKING:
    from core.models import VisionResult


SOF = b"\xAA\x55"
VERSION = 1
RESULT_MESSAGE_TYPE = 0x01
RESULT_PAYLOAD_LENGTH = 27
RESULT_PACKET_SIZE = 34
_RESULT_BODY_FORMAT = "<BBBHI BBHhhhhHHHHB".replace(" ", "")
_RESULT_BODY_SIZE = struct.calcsize(_RESULT_BODY_FORMAT)


class DetectorID(IntEnum):
    NONE = 0
    COLOR = 1
    SHAPE = 2
    STEEL_BALL = 3
    DIGIT = 4


class ResultState(IntEnum):
    NONE = 0
    CANDIDATE = 1
    LOCKED = 2
    OCCLUDED = 3
    LOST = 4


class ResultFlags(IntFlag):
    NONE = 0
    FOUND = 1 << 0
    LOCKED = 1 << 1
    DISTANCE_VALID = 1 << 2
    CAMERA_CALIBRATED = 1 << 3


_TARGET_STATE_TO_WIRE = {
    0: ResultState.NONE,
    1: ResultState.CANDIDATE,
    2: ResultState.LOCKED,
    4: ResultState.OCCLUDED,
    3: ResultState.LOST,
}


@dataclass(slots=True, frozen=True)
class VMCLinkResult:
    """解码后的固定视觉结果包。"""

    sequence: int
    timestamp_ms: int
    detector_id: int
    state: int
    target_class: int
    center_x_px: int
    center_y_px: int
    error_x_permille: int
    error_y_permille: int
    bbox_width_px: int
    bbox_height_px: int
    confidence_permille: int
    distance_mm: int
    flags: int
    crc16: int = 0
    crc_valid: bool = True

    @property
    def version(self) -> int:
        return VERSION

    @property
    def message_type(self) -> int:
        return RESULT_MESSAGE_TYPE

    @property
    def payload_length(self) -> int:
        return RESULT_PAYLOAD_LENGTH

    def with_sequence(self, sequence: int) -> "VMCLinkResult":
        return replace(self, sequence=int(sequence) & 0xFFFF, crc16=0, crc_valid=True)


def _integer(value: object, default: int = 0) -> int:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(numeric):
        return default
    return int(round(numeric))


def _clip(value: object, minimum: int, maximum: int, default: int = 0) -> int:
    return min(max(_integer(value, default), minimum), maximum)


def normalize_detector_id(detector: str | int | DetectorID) -> int:
    """把检测器名称或编号转换为协议枚举，未知值安全映射到 none。"""

    if isinstance(detector, str):
        normalized = detector.strip().lower().replace("-", "_")
        names = {
            "none": DetectorID.NONE,
            "color": DetectorID.COLOR,
            "shape": DetectorID.SHAPE,
            "steel_ball": DetectorID.STEEL_BALL,
            "digit": DetectorID.DIGIT,
        }
        return int(names.get(normalized, DetectorID.NONE))
    try:
        return int(DetectorID(int(detector)))
    except (TypeError, ValueError):
        return int(DetectorID.NONE)


def _wire_state(target_state: object) -> int:
    return int(_TARGET_STATE_TO_WIRE.get(_integer(target_state), ResultState.NONE))


def _axis_error_permille(center: int, extent: int) -> int:
    if extent <= 0:
        return 0
    half = extent / 2.0
    return _clip((center - half) / half * 1000.0, -1000, 1000)


def result_to_vmc_link(
    result: VisionResult,
    sequence: int = 0,
    detector_id: str | int | DetectorID = DetectorID.NONE,
    *,
    timestamp_ms: int | None = None,
    camera_calibrated: bool = False,
) -> VMCLinkResult:
    """创建协议快照；不会修改传入的 ``VisionResult``。"""

    found = bool(result.found)
    state = _wire_state(result.target_state)
    flags = ResultFlags.NONE
    if found:
        flags |= ResultFlags.FOUND
    if found and state == ResultState.LOCKED:
        flags |= ResultFlags.LOCKED
    if camera_calibrated:
        flags |= ResultFlags.CAMERA_CALIBRATED

    if timestamp_ms is None:
        timestamp_ms = _integer(float(result.capture_timestamp) * 1000.0)

    if not found:
        return VMCLinkResult(
            sequence=int(sequence) & 0xFFFF,
            timestamp_ms=int(timestamp_ms) & 0xFFFFFFFF,
            detector_id=normalize_detector_id(detector_id),
            state=state,
            target_class=0,
            center_x_px=-1,
            center_y_px=-1,
            error_x_permille=0,
            error_y_permille=0,
            bbox_width_px=0,
            bbox_height_px=0,
            confidence_permille=0,
            distance_mm=0xFFFF,
            flags=int(flags),
        )

    center_x = _clip(result.center_x, -32768, 32767)
    center_y = _clip(result.center_y, -32768, 32767)
    raw_distance = _integer(result.distance_mm, 0xFFFF)
    distance = raw_distance if 0 <= raw_distance < 0xFFFF else 0xFFFF
    if distance != 0xFFFF:
        flags |= ResultFlags.DISTANCE_VALID
    return VMCLinkResult(
        sequence=int(sequence) & 0xFFFF,
        timestamp_ms=int(timestamp_ms) & 0xFFFFFFFF,
        detector_id=normalize_detector_id(detector_id),
        state=state,
        target_class=_clip(result.target_class, 0, 0xFFFF),
        center_x_px=center_x,
        center_y_px=center_y,
        error_x_permille=_axis_error_permille(center_x, _integer(result.image_width)),
        error_y_permille=_axis_error_permille(center_y, _integer(result.image_height)),
        bbox_width_px=_clip(result.bbox_width, 0, 0xFFFF),
        bbox_height_px=_clip(result.bbox_height, 0, 0xFFFF),
        confidence_permille=_clip(result.confidence, 0, 1000),
        distance_mm=distance,
        flags=int(flags),
    )


def _safe_packet(packet: VMCLinkResult) -> VMCLinkResult:
    return VMCLinkResult(
        sequence=_integer(packet.sequence) & 0xFFFF,
        timestamp_ms=_integer(packet.timestamp_ms) & 0xFFFFFFFF,
        detector_id=normalize_detector_id(packet.detector_id),
        state=_clip(packet.state, 0, 4),
        target_class=_clip(packet.target_class, 0, 0xFFFF),
        center_x_px=_clip(packet.center_x_px, -32768, 32767, -1),
        center_y_px=_clip(packet.center_y_px, -32768, 32767, -1),
        error_x_permille=_clip(packet.error_x_permille, -1000, 1000),
        error_y_permille=_clip(packet.error_y_permille, -1000, 1000),
        bbox_width_px=_clip(packet.bbox_width_px, 0, 0xFFFF),
        bbox_height_px=_clip(packet.bbox_height_px, 0, 0xFFFF),
        confidence_permille=_clip(packet.confidence_permille, 0, 1000),
        distance_mm=_clip(packet.distance_mm, 0, 0xFFFF, 0xFFFF),
        flags=_clip(packet.flags, 0, 0x0F),
    )


def encode_result_packet(
    result: VisionResult | VMCLinkResult,
    sequence: int | None = None,
    detector_id: str | int | DetectorID = DetectorID.NONE,
    *,
    timestamp_ms: int | None = None,
    camera_calibrated: bool = False,
) -> bytes:
    """编码为严格 34 字节的小端 VMC-Link v1 视觉结果包。"""

    if not isinstance(result, VMCLinkResult) and hasattr(result, "found"):
        packet = result_to_vmc_link(
            result,
            0 if sequence is None else sequence,
            detector_id,
            timestamp_ms=timestamp_ms,
            camera_calibrated=camera_calibrated,
        )
    elif isinstance(result, VMCLinkResult):
        packet = result.with_sequence(sequence) if sequence is not None else result
    elif not isinstance(result, VMCLinkResult):
        raise TypeError("result 必须是 VisionResult 或 VMCLinkResult")
    packet = _safe_packet(packet)
    body = struct.pack(
        _RESULT_BODY_FORMAT,
        VERSION,
        RESULT_MESSAGE_TYPE,
        RESULT_PAYLOAD_LENGTH,
        packet.sequence,
        packet.timestamp_ms,
        packet.detector_id,
        packet.state,
        packet.target_class,
        packet.center_x_px,
        packet.center_y_px,
        packet.error_x_permille,
        packet.error_y_permille,
        packet.bbox_width_px,
        packet.bbox_height_px,
        packet.confidence_permille,
        packet.distance_mm,
        packet.flags,
    )
    encoded = SOF + body + struct.pack("<H", crc16_ccitt_false(body))
    if len(encoded) != RESULT_PACKET_SIZE:
        raise AssertionError(f"VMC-Link 编码长度错误: {len(encoded)}")
    return encoded


def decode_result_packet(data: bytes | bytearray | memoryview) -> VMCLinkResult:
    """验证帧头、常量、长度和 CRC 后解码一个结果包。"""

    packet = bytes(data)
    if len(packet) != RESULT_PACKET_SIZE:
        raise ValueError(f"VMC-Link 结果包必须为 {RESULT_PACKET_SIZE} 字节")
    if packet[:2] != SOF:
        raise ValueError("VMC-Link 帧头错误")
    body = packet[2:32]
    version, message_type, payload_length, *values = struct.unpack(
        _RESULT_BODY_FORMAT, body
    )
    if version != VERSION:
        raise ValueError(f"不支持的 VMC-Link 版本: {version}")
    if message_type != RESULT_MESSAGE_TYPE:
        raise ValueError(f"不是视觉结果包: 0x{message_type:02X}")
    if payload_length != RESULT_PAYLOAD_LENGTH:
        raise ValueError(f"视觉结果 payload_length 错误: {payload_length}")
    expected_crc = struct.unpack("<H", packet[32:34])[0]
    actual_crc = crc16_ccitt_false(body)
    if actual_crc != expected_crc:
        raise ValueError(
            f"VMC-Link CRC 错误: received=0x{expected_crc:04X} calculated=0x{actual_crc:04X}"
        )
    return VMCLinkResult(*values, crc16=expected_crc, crc_valid=True)


class VMCLinkParser:
    """支持噪声、半包、粘包和 CRC 错误恢复的增量解析器。"""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.good_count = 0
        self.crc_error_count = 0
        self.header_error_count = 0
        self.dropped_bytes = 0

    def feed(
        self,
        data: int | bytes | bytearray | memoryview | Iterable[int],
    ) -> list[VMCLinkResult]:
        if isinstance(data, int):
            if not 0 <= data <= 0xFF:
                raise ValueError("单字节输入必须在 0..255 范围内")
            self._buffer.append(data)
        else:
            try:
                self._buffer.extend(data)
            except (TypeError, ValueError) as exc:
                raise TypeError("data 必须是一个字节或字节序列") from exc

        results: list[VMCLinkResult] = []
        while True:
            position = self._buffer.find(SOF)
            if position < 0:
                keep = 1 if self._buffer[-1:] == SOF[:1] else 0
                discarded = len(self._buffer) - keep
                self.dropped_bytes += discarded
                if discarded:
                    del self._buffer[:discarded]
                break
            if position:
                self.dropped_bytes += position
                del self._buffer[:position]
            if len(self._buffer) < 5:
                break
            if (
                self._buffer[2] != VERSION
                or self._buffer[3] != RESULT_MESSAGE_TYPE
                or self._buffer[4] != RESULT_PAYLOAD_LENGTH
            ):
                self.header_error_count += 1
                self.dropped_bytes += 1
                del self._buffer[0]
                continue
            if len(self._buffer) < RESULT_PACKET_SIZE:
                break
            candidate = bytes(self._buffer[:RESULT_PACKET_SIZE])
            try:
                result = decode_result_packet(candidate)
            except ValueError as exc:
                if "CRC" in str(exc):
                    self.crc_error_count += 1
                else:
                    self.header_error_count += 1
                self.dropped_bytes += 1
                del self._buffer[0]
                continue
            results.append(result)
            self.good_count += 1
            del self._buffer[:RESULT_PACKET_SIZE]
        return results
