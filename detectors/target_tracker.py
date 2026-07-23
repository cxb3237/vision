"""目标时序确认、跳变拒绝和位置低通跟踪。"""

from __future__ import annotations

import math

from core.models import TargetState, VisionResult


class TargetTracker:
    """将单帧检测结果转换为字段一致的时序跟踪结果。"""

    def __init__(
        self,
        alpha: float = 0.45,
        max_jump_px: float = 160.0,
        confirm_frames: int = 3,
        lost_frames: int = 5,
    ) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha 必须在 (0, 1] 范围内")
        if max_jump_px <= 0 or confirm_frames <= 0 or lost_frames <= 0:
            raise ValueError("跟踪器阈值必须为正数")
        self.alpha = alpha
        self.max_jump_px = max_jump_px
        self.confirm_frames = confirm_frames
        self.lost_frames = lost_frames
        self.reset()

    def reset(self) -> None:
        """清空位置、命中数和丢失计数。"""

        self.pos: tuple[float, float] | None = None
        self.hits = 0
        self.misses = 0

    def _register_miss(self, result: VisionResult) -> VisionResult:
        had_target = self.pos is not None
        self.hits = 0
        self.misses += 1
        if not had_target:
            state = TargetState.NONE
        elif self.misses < self.lost_frames:
            state = TargetState.OCCLUDED
        else:
            state = TargetState.LOST
            self.pos = None
            self.hits = 0
        result.clear_target(state)
        return result

    def update(self, result: VisionResult) -> VisionResult:
        """更新跟踪状态并在平滑后同步重算像素误差。"""

        if not result.found:
            return self._register_miss(result)
        raw_position = (float(result.center_x), float(result.center_y))
        if self.pos is not None:
            jump = math.dist(raw_position, self.pos)
            if jump > self.max_jump_px:
                return self._register_miss(result)

        image_center_x = (
            result.image_width // 2
            if result.image_width > 0
            else result.center_x - result.error_x_px
        )
        image_center_y = (
            result.image_height // 2
            if result.image_height > 0
            else result.center_y - result.error_y_px
        )
        if self.pos is None:
            self.pos = raw_position
        else:
            self.pos = (
                self.alpha * raw_position[0] + (1.0 - self.alpha) * self.pos[0],
                self.alpha * raw_position[1] + (1.0 - self.alpha) * self.pos[1],
            )
        self.hits += 1
        self.misses = 0
        result.center_x = round(self.pos[0])
        result.center_y = round(self.pos[1])
        result.error_x_px = result.center_x - image_center_x
        result.error_y_px = result.center_y - image_center_y
        result.target_state = (
            TargetState.LOCKED if self.hits >= self.confirm_frames else TargetState.CANDIDATE
        )
        return result
