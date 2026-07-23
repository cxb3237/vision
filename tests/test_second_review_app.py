"""第二轮主程序模式、链路和启动清理测试。"""

from argparse import Namespace
import time

import numpy as np

from app import ControlProcessor, _handle_control_messages, is_peer_alive, run_application
from core.config_loader import load_mission_config
from core.models import FramePacket
from core.state_machine import VisionMode, VisionStateMachine
from detectors.target_tracker import TargetTracker
from protocol.vmc_messages import AckResult, Flags, MessageType, VisionControl
from protocol.vmc_protocol import VmcPacket


class CountingTracker(TargetTracker):
    def __init__(self) -> None:
        self.reset_count = 0
        super().__init__()

    def reset(self) -> None:
        self.reset_count += 1
        super().reset()


def test_peer_timeout_detected_while_port_remains_open() -> None:
    statistics = {
        "port_open": True,
        "port_opened_monotonic": 1.0,
        "last_valid_packet_monotonic": 2.0,
        "last_heartbeat_monotonic": 2.0,
    }
    assert is_peer_alive(statistics, 2.5, 1.0)
    assert not is_peer_alive(statistics, 3.1, 1.0)


def test_unsupported_modes_and_target_class_return_unsupported() -> None:
    processor = ControlProcessor(
        VisionStateMachine(),
        TargetTracker(),
        supported_target_class=1,
    )
    recognize = VisionControl(1, VisionMode.RECOGNIZE)
    wrong_color = VisionControl(2, VisionMode.TRACK, target_class=2)
    assert processor.process(1, recognize)[0] == AckResult.UNSUPPORTED
    assert processor.process(2, wrong_color)[0] == AckResult.UNSUPPORTED
    assert processor.state_machine.mode == VisionMode.IDLE


def test_duplicate_control_is_idempotent_and_track_transition_resets_once() -> None:
    tracker = CountingTracker()
    processor = ControlProcessor(VisionStateMachine(), tracker, supported_target_class=1)
    baseline = tracker.reset_count
    control = VisionControl(42, VisionMode.TRACK, target_class=1)
    first = processor.process(9, control)
    second = processor.process(9, control)
    assert first == second == (AckResult.OK, 0)
    assert tracker.reset_count == baseline + 1
    search = VisionControl(43, VisionMode.SEARCH, target_class=1)
    assert processor.process(10, search) == (AckResult.OK, 0)
    assert tracker.reset_count == baseline + 2


class ControlQueueSerial:
    def __init__(self, packets) -> None:
        self.packets = list(packets)
        self.sent = []

    def get_message(self):
        return self.packets.pop(0) if self.packets else None

    def send_packet(self, *args):
        self.sent.append(args)
        return True


def test_control_ack_is_only_required_when_ack_req_flag_is_set() -> None:
    processor = ControlProcessor(VisionStateMachine(), TargetTracker(), 1)
    no_ack = VmcPacket(
        MessageType.VISION_CONTROL,
        0,
        1,
        VisionControl(1, VisionMode.SEARCH, target_class=1).pack(),
    )
    needs_ack = VmcPacket(
        MessageType.VISION_CONTROL,
        int(Flags.ACK_REQ),
        2,
        VisionControl(2, VisionMode.TRACK, target_class=1).pack(),
    )
    serial = ControlQueueSerial([no_ack, needs_ack])
    next_sequence = _handle_control_messages(serial, processor, 7)
    assert next_sequence == 8
    assert len(serial.sent) == 1
    assert serial.sent[0][0] == MessageType.ACK


class FakeCamera:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def get_latest_frame(self):
        return FramePacket(1, time.monotonic(), np.zeros((10, 10, 3), np.uint8))

    def get_statistics(self):
        return {"actual_fps": 0.0, "frames_failed": 0}


class FailingSerial:
    enabled = True

    def start(self) -> None:
        raise RuntimeError("serial start failed")

    def stop(self) -> None:
        raise AssertionError("未成功启动的串口不应 stop")


class FakeDetector:
    target_class = 1

    def initialize(self) -> None:
        return None


def test_camera_is_stopped_if_serial_start_fails() -> None:
    camera = FakeCamera()
    args = Namespace(mode="idle", display=False)
    mission = load_mission_config()
    try:
        run_application(args, mission, FakeDetector(), camera, FailingSerial())
    except RuntimeError as exc:
        assert str(exc) == "serial start failed"
    else:
        raise AssertionError("应传播串口启动异常")
    assert camera.started and camera.stopped
