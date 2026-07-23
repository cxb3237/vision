"""视觉检测器公共接口。"""

from abc import ABC, abstractmethod

import numpy as np

from core.models import FramePacket, VisionResult


class BaseDetector(ABC):
    """所有检测器必须实现的生命周期和处理接口。"""

    @abstractmethod
    def initialize(self) -> None:
        """初始化或重新初始化检测器。"""

    @abstractmethod
    def process(self, frame: FramePacket) -> VisionResult:
        """处理一帧且不修改输入图像。"""

    @abstractmethod
    def reset(self) -> None:
        """清除检测器的时序状态。"""

    def draw_debug(self, image: np.ndarray, result: VisionResult) -> np.ndarray:
        """返回带调试标注的图像副本。"""

        return image.copy()
