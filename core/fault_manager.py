"""线程安全的故障位管理，供心跳上报。"""

from enum import IntEnum
import threading


class Fault(IntEnum):
    """系统故障位定义。"""

    CAMERA_OPEN_FAILED = 0
    CAMERA_FRAME_TIMEOUT = 1
    CAMERA_RECONNECTING = 2
    SERIAL_OPEN_FAILED = 3
    SERIAL_LINK_DOWN = 4
    CONFIG_INVALID = 5
    DETECTOR_FAILED = 6
    CALIBRATION_INVALID = 7


class FaultManager:
    """跨线程安全维护活动故障集合。"""

    def __init__(self) -> None:
        self._faults: set[Fault] = set()
        self._lock = threading.Lock()

    def set_fault(self, fault: Fault) -> None:
        """设置故障位。"""

        with self._lock:
            self._faults.add(Fault(fault))

    def clear_fault(self, fault: Fault) -> None:
        """清除故障位。"""

        with self._lock:
            self._faults.discard(Fault(fault))

    def has_fault(self, fault: Fault) -> bool:
        """返回指定故障是否活动。"""

        with self._lock:
            return Fault(fault) in self._faults

    def fault_bits(self) -> int:
        """返回协议使用的故障位掩码。"""

        with self._lock:
            return sum(1 << int(item) for item in self._faults)

    def get_active_faults(self) -> list[Fault]:
        """返回按位号排序的故障快照。"""

        with self._lock:
            return sorted(self._faults, key=int)
