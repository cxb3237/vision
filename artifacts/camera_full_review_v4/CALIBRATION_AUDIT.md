# 位置标定安全审计

## 为什么未标定时禁止 POS

默认像素端点不能代表现场摄像头、分辨率、导轨和安装方向。把默认值直接映射为毫米会向 MCU 提供看似合法但物理错误的位置，因此 `calibrated: false` 时视觉仍检测并显示像素坐标，但比赛输出只允许 INVALID，不允许 BALL POS。

## 参数验证

`VisionRuntime._ball_calibration_status(image_width)` 检查：

- `calibrated` 必须显式为 true 才可能生效；
- 左右端点必须能转换为有限数值；
- 左右端点不能相同；
- `servo_side` 只能是 `left` 或 `right`；
- 实际图像宽度已知时，两端点都必须位于 `0..width-1`。

错误不会终止视觉线程。状态快照提供：

- `ball_position_calibrated`
- `ball_position_calibration_error`
- `left_endpoint_px`
- `right_endpoint_px`
- `servo_side`

网页使用这些字段显示“已标定”或具体中文错误。

## 现场标定步骤

1. 固定比赛分辨率、焦距、机位、导轨和视野。
2. 球放在导轨有效左端，网页读取像素 X，记录为 `left_endpoint_px`。
3. 球放在导轨有效右端，记录为 `right_endpoint_px`。
4. 确认端点有限、不同且都在实际图像宽度内。
5. 根据舵机安装方向设置 `servo_side: right` 或 `left`。
6. 先保持 `calibrated: false` 做网页检查；确认无误后改为 `true` 并重启。
7. 进入比赛模式前依次验证左端、中点、右端的毫米符号和范围。
8. 分辨率、机位或导轨变化后重新标定。

对应测试位于 `tests/test_vision_runtime.py`，覆盖未标定、合法标定、相同端点、越界端点和非法方向。

