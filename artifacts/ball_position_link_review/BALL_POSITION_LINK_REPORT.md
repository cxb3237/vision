# 钢球专用网页与4字节位置协议审核报告

## 修改前检查

- 当前分支：`feature/ball-position-link`
- 基线提交：`325696e Add vision performance baseline instrumentation`
- 父分支：`feature/vision-performance-baseline`
- 父分支与当前分支 merge-base：`325696e919821137678cd88c7d82f54aee17cc97`
- 修改前 `git status --short`：无输出，工作区干净

## 实现结论

- 旧34字节结果包已停止用于 `SerialService` 和 `VisionRuntime` 的运行时发送。
- 新钢球位置包固定4字节，仅包含有符号横向位置 `x_mm`。
- 无目标、未标定、位置下发关闭或映射无效时不发包，并清除待发旧位置。
- MCU→树莓派的通用 `AA 55` 控制帧、心跳和ACK协议未修改。
- `SerialService` 继续保留关键队列优先级、普通队列和最新流式消息机制；钢球位置使用独立最新值单槽。
- 网页固定为钢球YOLO-NCNN界面，历史保存的其他检测器不会覆盖触摸模式启动选择。
- 网页已移除检测器选择、类别、置信度、模式、Center Y、重复比赛面板和检测覆盖文字。
- color、shape、digit、steel_ball_classical等后端源码和后端检测器工厂未删除。
- YOLO/NCNN模型文件及 `imgsz=416`、`conf=0.40`、`iou=0.60`、`num_threads=4`均未修改。
- CameraService参数与生命周期、TargetTracker行为和钢球目标选择规则均未修改。
- 串口波特率115200、预览FPS和JPEG质量均未修改。
- 未执行 `git add`、`git commit` 或 `git push`。

## 运行时数据流

1. 钢球YOLO-NCNN产生目标结果，原TargetTracker保持原行为。
2. `VisionRuntime`读取有效目标的中心X。
3. 已标定时用两个像素端点线性映射到-125..125 mm，四舍五入并限幅。
4. 位置下发启用时调用 `publish_ball_position(x_mm)`；否则清空待发位置。
5. 串口线程按配置频率消费最新位置，关键ACK/URGENT包仍优先。
6. 消费后不会重复发送旧位置；新位置可以覆盖尚未消费的位置。

## 平台验证说明

Windows无硬件测试、Python编译、JavaScript语法、自检和MinGW严格C编译均通过。真实UART时序、
树莓派NCNN运行、实际两点标定、MSPM0 UART接收和800×480触摸实机显示仍需在Raspberry Pi 5
与目标MSPM0硬件上联合验证。
