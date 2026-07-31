# 钢球位置连续估计与固定 50 Hz UART 输出实现报告

## 1. 工程与起始状态

- 工程：`E:\NUEDC\camera`
- 分支：`main`
- 起始提交：`44256fe feat(vision): add candidate pipe ROI filtering and hard-frame review`
- 任务开始 `git status --short`：仅有用户原本未跟踪的 `MODEL_INTEGRATION_REPORT.md`。
- 该原有未跟踪文件未修改、未加入测试或提交范围。

## 2. 审计结果

UART 实际配置为 9600 baud、8N1、CRLF ASCII。保持的命令是 `BALL POS <integer_mm>\r\n`、`BALL INVALID\r\n`、`BALL START\r\n`、`BALL STOP\r\n`，位置范围为 -125..125 mm。最长位置帧 `BALL POS -125\r\n` 由编码函数计算为 15 字节，`BALL INVALID\r\n` 为 14 字节。

修改前只有 `BallUartClient` worker 持有并调用 `serial.write`，这一所有权正确且已保留。START/STOP 位于优先控制队列。原 `_latest_position` 被发送一次后立即清空；`VisionRuntime` 每个视觉帧有位置时调用 `publish_ball_position`、无位置时调用 `send_invalid`，所以标称 50 Hz 实际受视觉帧率和有效检测帧限制。毫米值在启用 pipe mapping 时由当前/短时保持的动态红蓝端点计算，否则才使用固定像素标定。`VisionResult` 和 `FramePacket` 均携带浮点 `capture_timestamp`（单调时钟秒）。competition mode 关闭时清旧位置并发送 STOP；控制命令优先于位置。

## 3. 带宽

8N1 每个 ASCII 字节占 10 bit：

```text
15 bytes × 10 bits × 50 Hz / 9600 baud = 0.78125
```

最坏发送占用为 78.125%，低于 85%。配置加载器现在要求 baudrate 严格为 9600、发送频率为正，且通过实际编码帧长度计算占用；超过 85% 直接失败，不自动改波特率或降频。

## 4. 修改后的固定输出架构

`VisionRuntime` 在每个可靠视觉结果到达时，只用该帧 `capture_timestamp` 更新线程安全 `BallPositionEstimator`。UART worker 获得非阻塞 provider，在绝对 20 ms deadline 采样一次并编码成现有 POS 或 INVALID。provider 不做磁盘、图像、网络或串口 I/O，也不在串口锁内调用。

调度采用 `next_deadline += period` 的等价绝对累计方式。延迟超过周期时直接跨过已错过 deadline，只发送当前一帧，不追赶补发。START/STOP 控制命令仍优先并占用一个时隙；断线重连清除旧 latest-only 状态、重发当前 START 意图，随后只采样 provider 最新状态。STOP 后不再采样输出。写失败不会排队或重放历史位置。

## 5. 估计器与状态转换

α-β 公式：

```text
x_pred = x_previous + v_previous * dt
residual = measurement - x_pred
x_new = x_pred + alpha * residual
v_new = v_previous + beta * residual / dt
```

第一帧直接初始化；倒退/过小时间戳、NaN/Inf、越界位置、无效 ROI 和非法置信度被拒绝；过大 dt 或长期 LOST 后以新测量重新初始化。位置限制为 -125..125 mm，速度限制为 ±600 mm/s，触及边界时禁止继续向外积分。

- UNINITIALIZED：INVALID。
- MEASURED：年龄 ≤90 ms，发送位置。
- PREDICTED：90–150 ms，按位置和速度短时外推，发送位置。
- HELD：150–300 ms，速度按指数衰减并逐渐停止，发送位置。
- LOST：>300 ms，发送 INVALID。

预测值只标为 PREDICTED/HELD，不会写回为真实测量。摄像头离线、模型异常或 ROI 长期无效时没有新测量，估计器按单调时间自然进入 LOST。

## 6. 输出斜率限制与异常门控

最终发送位置按实际相邻采样时间差计算 `max_step = 600 mm/s × actual_period_s`，常规 20 ms 时约 12 mm。限制只作用于输出快照，不破坏内部测量和速度记录。从 INVALID 恢复到位置会记录 reacquisition，并沿最后输出位置逐步靠近。

门控默认允许残差为 `20 mm + abs(velocity) × dt × 1.5`，基础部分再考虑置信度；ROI 无效直接拒绝。单个越门限异常标为 rejected，不改变连续输出；连续两次彼此一致的新测量允许重捕获，重捕获后仍受输出斜率限制。NCNN 解码和 NMS 未修改。

## 7. 配置默认值

`config/mission.yaml` 中 UART read timeout 从 20 ms 调整为 5 ms，以给 20 ms deadline 留出调度余量；接收仍支持任意分块以及 CR、LF、CRLF。新增 `continuous_output: true` 和嵌套 estimator 配置：alpha/beta 0.85/0.08，fresh/prediction/hold 90/150/300 ms，衰减常数 120 ms，最大速度/斜率 600 mm/s，测量 dt 5–500 ms，范围 -125..125 mm，门控 20 mm/1.5/连续 2 次。

## 8. ROI 行为

Candidate ROI Strict 保持 Candidate 模型、conf=0.50、ROI 开启、左红右蓝动态端点和比例走廊。ROI 几何短时失效时由估计器有限预测；长期无可靠测量进入 LOST。没有回退到整幅图任意检测框。正式测试必须贴左红、右蓝端点，背景应避免大面积相似红蓝色。

## 9. 状态与调试页

状态接口在保留原字段基础上新增 estimator state、测量/估计/输出位置、速度、测量/预测年龄、measurement/prediction/rejected/reacquisition/slew 计数、输出来源，以及 UART 最坏帧长度、占用率、输出周期/频率、deadline miss、当前 jitter 和 P95。调试页只显示这些快照与 ROI valid，不驱动调度。

## 10. 文件

修改：`README.md`、`app.py`、`config/mission.yaml`、`core/config_loader.py`、`core/vision_runtime.py`、`docs/STEEL_BALL_MODELS.md`、`drivers/ball_uart_client.py`、三个既有相关测试文件、`web_debug/static/index.html`、`web_debug/static/app.js`。

新增：`core/ball_position_estimator.py`、`docs/BALL_POSITION_ESTIMATOR.md`、`tests/test_ball_position_estimator.py`、`tests/test_ball_output_tools.py`、`tools/record_ball_output_diagnostics.py`、`tools/simulate_ball_position_estimator.py` 和本报告。

## 11. 诊断与仿真

状态记录器只使用标准库 urllib，每次记录真实主机采样时间；网络超时计数但不中止，Ctrl+C 仍保存 CSV 和同目录 `summary.json`。仿真工具不访问摄像头或串口，覆盖静止、100 mm/s、100/200/500 ms 丢失、重捕获、单帧异常与两端边界。

## 12. 测试结果

- `python -m compileall core drivers detectors inference tools`：通过。
- 新增及指定 UART、VisionRuntime、TargetTracker、配置、ROI、NCNN 回归：186 passed。
- 完整 Python 回归：342 passed, 2 skipped。
- `git diff --check`：通过。
- `node --check web_debug/static/app.js`：当前 Windows 环境未安装 Node，未执行；未把它计入 Python 测试成功数。

## 13. 模型与协议取证

- baseline param：`ADDFCB27466473B6ACBFC352251C27785668347369951C0AF0DB6CF3A124E24F`
- baseline bin：`1B2BC2887F395906FB6D7D9287CEE2A753453234BA6EE19C70F6EF9D937E0BD5`
- candidate param：`ADA13CCDB83C916F6262123B7B423195BBF5015CDC043B427F205D753F8398E9`
- candidate bin：`B9893B56F4E309B33F321EC907340F1588380F62E1B123294C0B7C56B32D67E8`

模型文件未修改。UART ASCII 命令、9600 baud、8N1 和 -125..125 mm 范围未变化。正式 `config/steel_ball_ncnn.yaml` 保持任务开始 SHA256 `AC46C693568E78CD7D96815C7BDEA22F6A79B632936906E2DC1602C4AE668C76`，当前指向 baseline；现场推荐 profile 仍为 Candidate ROI Strict。

## 14. 树莓派部署命令

```bash
cd /home/clb/Desktop/camera
python tools/switch_steel_ball_model.py candidate-roi-strict
python tools/switch_steel_ball_model.py status
sudo systemctl restart vision-touch.service
journalctl -u vision-touch.service -f
```

诊断：

```bash
python tools/record_ball_output_diagnostics.py \
  --url http://127.0.0.1:8765/api/status \
  --duration 40 --sample-rate 50 \
  --output runs/ball_output_test/test.csv
```

## 15. 树莓派验收步骤

1. 核对 Candidate ROI Strict、左红右蓝标记和背景颜色。
2. 核对启动先发 START，首个可靠测量前固定发送 INVALID。
3. 用逻辑分析仪或 MCU 计数确认 9600 8N1、约 50 Hz，无突发补帧。
4. 匀速移动检查 POS 连续性和相邻步长；遮挡约 100 ms 检查 PREDICTED，约 200 ms 检查 HELD，超过 300 ms 检查 INVALID。
5. 退出比赛模式确认 STOP 后无位置帧；重新进入确认 reset 和 INVALID 等待首测量。
6. 断开/恢复串口，确认重连只重发 START 与当前 provider 状态，不重放历史位置。
7. 检查 CSV/summary 中输出 Hz、jitter P95、deadline miss、slew/rejected/reacquisition 及错误。

## 16. 已知限制

Windows 自动测试使用 mock/纯算法，没有访问实体摄像头、UART 或 systemd。网页 HTTP 采样率不等于 UART 线上物理频率；最终电气节拍、Linux 调度 jitter、MCU 接收和现场遮挡效果必须在 Raspberry Pi 5 上验收。20 mm 外径与 250 mm 标记跨度仍为待硬件确认，不影响本任务未改动的 ROI 比例公式。

## 17. Git 操作

未执行 `git commit`，未执行 `git push`，也未执行 reset/clean/restore。
