"""VMC-Link 协议实现。"""

from protocol.vmc_link import (
    DetectorID,
    ResultFlags,
    ResultState,
    VMCLinkParser,
    VMCLinkResult,
    decode_result_packet,
    encode_result_packet,
    result_to_vmc_link,
)

__all__ = [
    "DetectorID",
    "ResultFlags",
    "ResultState",
    "VMCLinkParser",
    "VMCLinkResult",
    "decode_result_packet",
    "encode_result_packet",
    "result_to_vmc_link",
]
