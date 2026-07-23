"""视觉任务状态机和协议共用的模式枚举。"""

from enum import IntEnum


class VisionMode(IntEnum):
    """视觉工作模式；枚举值同时用于 VMC-Link 协议。"""

    IDLE = 0
    SEARCH = 1
    TRACK = 2
    RECOGNIZE = 3
    MEASURE = 4
    AIM = 5
    CALIBRATION = 6
    RETURN_CENTER = 7
    FAULT = 255


class VisionStateMachine:
    """拒绝本机不支持的运动控制模式并维护当前模式。"""

    def __init__(self, mode: VisionMode = VisionMode.IDLE) -> None:
        self.mode = mode

    def set_mode(self, mode: int | VisionMode) -> bool:
        """设置模式；非法或不支持的模式返回 ``False``。"""

        try:
            requested = VisionMode(mode)
        except (TypeError, ValueError):
            return False
        if requested in (VisionMode.AIM, VisionMode.RETURN_CENTER):
            return False
        self.mode = requested
        return True

    def enter_fault(self) -> None:
        """进入故障模式。"""

        self.mode = VisionMode.FAULT
