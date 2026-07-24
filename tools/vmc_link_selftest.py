"""VMC-Link v1 无硬件自检。"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from protocol.crc16 import crc16_ccitt_false
from protocol.vmc_link import (
    RESULT_PACKET_SIZE,
    DetectorID,
    VMCLinkParser,
    decode_result_packet,
    encode_result_packet,
)


@dataclass
class _SampleResult:
    capture_timestamp: float = 12.345
    found: bool = True
    target_state: int = 2
    target_class: int = 1
    center_x: int = 400
    center_y: int = 180
    bbox_width: int = 80
    bbox_height: int = 60
    confidence: int = 875
    distance_mm: int = 0xFFFF
    image_width: int = 640
    image_height: int = 480


def _result(state: int, found: bool, target_class: int = 1) -> _SampleResult:
    return _SampleResult(found=found, target_state=state, target_class=target_class)


def run_selftest() -> None:
    assert crc16_ccitt_false(b"123456789") == 0x29B1
    encoded: list[bytes] = []
    cases = (
        (DetectorID.COLOR, 1, True, 1),
        (DetectorID.SHAPE, 2, True, 2),
        (DetectorID.STEEL_BALL, 4, False, 0),
        (DetectorID.DIGIT, 3, False, 108),
    )
    for sequence, (detector, state, found, target_class) in enumerate(cases):
        packet = encode_result_packet(
            _result(state, found, target_class),
            sequence,
            detector,
            camera_calibrated=True,
        )
        assert len(packet) == RESULT_PACKET_SIZE
        assert decode_result_packet(packet).sequence == sequence
        encoded.append(packet)

    parser = VMCLinkParser()
    assert parser.feed(b"noise\xAA") == []
    first = encoded[0]
    assert parser.feed(first[1:12]) == []
    parsed = parser.feed(first[12:] + encoded[1] + encoded[2])
    assert [item.sequence for item in parsed] == [0, 1, 2]

    bad = bytearray(encoded[0])
    bad[20] ^= 0x40
    parsed = parser.feed(bad + encoded[3])
    assert [item.sequence for item in parsed] == [3]
    assert parser.crc_error_count == 1

    wrapped = [
        decode_result_packet(encode_result_packet(_result(2, True), seq))
        for seq in (0xFFFF, 0x0000)
    ]
    assert [item.sequence for item in wrapped] == [0xFFFF, 0]


def main() -> int:
    try:
        run_selftest()
    except Exception as exc:
        print(f"VMC-Link selftest: FAIL - {exc}", file=sys.stderr)
        return 1
    print("VMC-Link selftest: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
