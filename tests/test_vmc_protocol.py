"""VMC-Link 编解码和流恢复测试。"""

from protocol.vmc_messages import (
    Ack,
    AckResult,
    ACK_SIZE,
    HEARTBEAT_SIZE,
    VISION_TARGET_SIZE,
    VISION_CONTROL_SIZE,
    VisionControl,
    VisionTarget,
)
from protocol.vmc_protocol import VmcStreamParser, encode_packet


def test_roundtrip_and_fragments() -> None:
    encoded = encode_packet(1, 2, 3, b"abc")
    parser = VmcStreamParser()
    assert not parser.feed(encoded[:4])
    packets = parser.feed(encoded[4:])
    assert packets[0].payload == b"abc"


def test_noise_sticky_and_crc() -> None:
    encoded = encode_packet(1, 0, 1, b"x")
    parser = VmcStreamParser()
    assert len(parser.feed(b"xx" + encoded + encoded)) == 2
    bad = bytearray(encoded)
    bad[-1] ^= 1
    assert len(parser.feed(bad + encoded)) == 1
    assert parser.crc_error_count == 1


def test_oversize_and_target() -> None:
    parser = VmcStreamParser()
    assert not parser.feed(b"\xaa\x55\x10\x01\x00\x00\x41\x00")
    target = VisionTarget(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)
    assert VisionTarget.unpack(target.pack()) == target


def test_target_payload_size_is_26_bytes() -> None:
    target = VisionTarget(1, 2, 3, 4, 5, 6)
    assert VISION_TARGET_SIZE == 26
    assert len(target.pack()) == 26


def test_ack_and_control_round_trip() -> None:
    ack = Ack(0x40, 7, AckResult.UNSUPPORTED, 9)
    control = VisionControl(22, 2, 1, 300, -12, 34, 500)
    assert Ack.unpack(ack.pack()) == ack
    assert VisionControl.unpack(control.pack()) == control
    assert ACK_SIZE == len(ack.pack()) == 4
    assert VISION_CONTROL_SIZE == len(control.pack()) == 12
    assert HEARTBEAT_SIZE == 12


def test_payload_unpack_rejects_wrong_exact_lengths() -> None:
    import pytest

    for payload_type, good_size in (
        (Ack, ACK_SIZE),
        (VisionControl, VISION_CONTROL_SIZE),
        (VisionTarget, VISION_TARGET_SIZE),
    ):
        with pytest.raises(ValueError):
            payload_type.unpack(bytes(good_size - 1))
        with pytest.raises(ValueError):
            payload_type.unpack(bytes(good_size + 1))


def test_pack_rejects_out_of_range_fields_as_value_error() -> None:
    import pytest

    with pytest.raises(ValueError):
        Ack(256, 0, 0, 0).pack()
    with pytest.raises(ValueError):
        VisionControl(1, 1, target_class=70000).pack()


def test_parser_recovers_valid_packet_after_bad_length_and_noise() -> None:
    parser = VmcStreamParser()
    bad_length = b"\xaa\x55\x10\x01\x00\x00\x41\x00"
    valid = encode_packet(3, 0, 5, b"ok")
    packets = parser.feed(b"noise" + bad_length + valid)
    assert packets[-1].payload == b"ok"
    assert parser.length_error_count == 1
