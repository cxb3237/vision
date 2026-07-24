"""VMC-Link v1 固定结果协议测试。"""

from dataclasses import asdict
import struct

import pytest

from core.models import TargetState, VisionResult
from protocol.crc16 import crc16_ccitt_false
from protocol.vmc_link import (
    RESULT_PACKET_SIZE,
    DetectorID,
    ResultFlags,
    ResultState,
    VMCLinkParser,
    decode_result_packet,
    encode_result_packet,
)


def result(**overrides) -> VisionResult:
    values = {
        "frame_id": 10,
        "capture_timestamp": 1.25,
        "process_timestamp": 1.30,
        "found": True,
        "target_state": TargetState.LOCKED,
        "target_class": 3,
        "center_x": 400,
        "center_y": 120,
        "bbox_width": 80,
        "bbox_height": 60,
        "confidence": 850,
        "distance_mm": 500,
        "image_width": 640,
        "image_height": 480,
    }
    values.update(overrides)
    return VisionResult(**values)


def test_known_crc_vector() -> None:
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_packet_is_fixed_34_bytes_and_little_endian() -> None:
    encoded = encode_result_packet(
        result(target_class=0x1234, center_x=-2),
        sequence=0x4567,
        detector_id=DetectorID.SHAPE,
        timestamp_ms=0x89ABCDEF,
    )
    assert len(encoded) == RESULT_PACKET_SIZE == 34
    assert encoded[:7] == b"\xAA\x55\x01\x01\x1B\x67\x45"
    assert encoded[7:11] == b"\xEF\xCD\xAB\x89"
    assert encoded[13:15] == b"\x34\x12"
    assert struct.unpack_from("<h", encoded, 15)[0] == -2


def test_round_trip_and_flags() -> None:
    decoded = decode_result_packet(
        encode_result_packet(
            result(),
            sequence=7,
            detector_id="color",
            camera_calibrated=True,
        )
    )
    assert decoded.sequence == 7
    assert decoded.detector_id == DetectorID.COLOR
    assert decoded.state == ResultState.LOCKED
    assert decoded.error_x_permille == 250
    assert decoded.error_y_permille == -500
    assert decoded.flags == int(
        ResultFlags.FOUND
        | ResultFlags.LOCKED
        | ResultFlags.DISTANCE_VALID
        | ResultFlags.CAMERA_CALIBRATED
    )


def test_no_target_fields_and_target_state_mapping() -> None:
    decoded = decode_result_packet(
        encode_result_packet(
            result(found=False, target_state=TargetState.OCCLUDED),
            detector_id=DetectorID.DIGIT,
        )
    )
    assert decoded.state == ResultState.OCCLUDED
    assert decoded.target_class == 0
    assert (decoded.center_x_px, decoded.center_y_px) == (-1, -1)
    assert decoded.distance_mm == 0xFFFF
    assert decoded.flags == 0


def test_fields_are_safely_clipped() -> None:
    decoded = decode_result_packet(
        encode_result_packet(
            result(
                center_x=999_999,
                center_y=-999_999,
                bbox_width=999_999,
                bbox_height=-5,
                confidence=999_999,
                distance_mm=999_999,
            )
        )
    )
    assert decoded.center_x_px == 32767
    assert decoded.center_y_px == -32768
    assert decoded.error_x_permille == 1000
    assert decoded.error_y_permille == -1000
    assert decoded.bbox_width_px == 0xFFFF
    assert decoded.bbox_height_px == 0
    assert decoded.confidence_permille == 1000
    assert decoded.distance_mm == 0xFFFF

    negative_distance = decode_result_packet(
        encode_result_packet(result(distance_mm=-1))
    )
    assert negative_distance.distance_mm == 0xFFFF


def test_crc_error_is_rejected() -> None:
    encoded = bytearray(encode_result_packet(result()))
    encoded[20] ^= 0x01
    with pytest.raises(ValueError, match="CRC"):
        decode_result_packet(encoded)


def test_parser_noise_fragments_multiple_and_crc_recovery() -> None:
    first = encode_result_packet(result(), sequence=1)
    second = encode_result_packet(result(target_class=104), sequence=2, detector_id="digit")
    bad = bytearray(first)
    bad[-1] ^= 0x80
    parser = VMCLinkParser()
    assert parser.feed(b"garbage" + first[:9]) == []
    parsed = parser.feed(first[9:] + bad + second)
    assert [packet.sequence for packet in parsed] == [1, 2]
    assert parser.crc_error_count == 1


def test_parser_accepts_one_byte_at_a_time() -> None:
    parser = VMCLinkParser()
    packets = []
    for byte in encode_result_packet(result(), sequence=22):
        packets.extend(parser.feed(byte))
    assert [packet.sequence for packet in packets] == [22]


def test_digit_classes_and_sequence_wrap() -> None:
    for digit in range(10):
        packet = decode_result_packet(
            encode_result_packet(
                result(target_class=100 + digit),
                sequence=0xFFFF + digit,
                detector_id="digit",
            )
        )
        assert packet.target_class == 100 + digit
        assert packet.sequence == (0xFFFF + digit) & 0xFFFF


def test_encoding_does_not_modify_vision_result() -> None:
    source = result()
    before = asdict(source)
    encode_result_packet(source, 3, "steel_ball", camera_calibrated=True)
    assert asdict(source) == before
