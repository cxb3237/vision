"""录制帧去重逻辑测试。"""

import time

import numpy as np

from core.models import FramePacket
from tools.record_dataset import FrameDeduplicator


def test_same_frame_id_is_not_accepted_twice() -> None:
    deduplicator = FrameDeduplicator()
    frame = FramePacket(1, time.monotonic(), np.zeros((2, 2, 3), np.uint8))
    assert deduplicator.accept(frame)
    assert not deduplicator.accept(frame)


def test_only_new_frame_ids_are_accepted() -> None:
    deduplicator = FrameDeduplicator()
    for frame_id, expected in zip((1, 1, 2, 2, 3), (True, False, True, False, True)):
        frame = FramePacket(frame_id, 0.0, np.zeros((1, 1, 3), np.uint8))
        assert deduplicator.accept(frame) is expected
