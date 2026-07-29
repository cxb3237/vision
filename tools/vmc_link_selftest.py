"""Hardware-free self-test for the four-byte steel-ball position link."""

from __future__ import annotations

import sys

from protocol.ball_position_link import (
    BallPositionParser,
    decode_ball_position,
    encode_ball_position,
)


def run_selftest() -> None:
    vectors = {
        -125: bytes.fromhex("A5 5A 83 86"),
        0: bytes.fromhex("A5 5A 00 06"),
        125: bytes.fromhex("A5 5A 7D 72"),
    }
    for x_mm, expected in vectors.items():
        assert encode_ball_position(x_mm) == expected
        assert decode_ball_position(expected).x_mm == x_mm

    parser = BallPositionParser()
    assert parser.feed(b"noise\xAA\x55\xA5") == []
    parsed = parser.feed(vectors[-125][1:] + vectors[0] + vectors[125])
    assert [packet.x_mm for packet in parsed] == [-125, 0, 125]

    corrupt = bytearray(vectors[0])
    corrupt[-1] ^= 0x01
    parsed = parser.feed(corrupt + encode_ball_position(37))
    assert [packet.x_mm for packet in parsed] == [37]
    assert parser.crc_error_count == 1


def main() -> int:
    try:
        run_selftest()
    except Exception as exc:
        print(f"Ball position link selftest: FAIL - {exc}", file=sys.stderr)
        return 1
    print("Ball position link selftest: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
