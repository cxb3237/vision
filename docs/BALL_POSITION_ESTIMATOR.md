# 钢球位置连续估计与固定 50 Hz 输出

## 设计边界

正式视觉链路保持 Candidate ROI Strict：Candidate NCNN、置信度 0.50、左红右蓝端点和动态水管 ROI。没有使用整幅图像补帧、视频插帧或光流模型；这些方法增加算力、时延和错误传播，而控制器真正需要的是一维管轴位置的短时连续反馈。

视觉线程只产生不规则到达的可靠测量。`BallPositionEstimator` 保存位置、速度和测量采集时间；唯一持有串口的 `BallUartClient` worker 每 20 ms 非阻塞采样一次。网页轮询只读取快照，不驱动估计器或 UART 调度。

## α-β 更新

对捕获时间戳间隔 `dt`：

```text
x_pred = x_previous + v_previous * dt
residual = measurement - x_pred
x_new = x_pred + alpha * residual
v_new = v_previous + beta * residual / dt
```

位置固定限制在 `-125..125 mm`，速度限制为 `±max_speed_mm_s`。倒退时间戳、过小 `dt`、NaN/Inf、越界位置、无效 ROI 和非法置信度会被拒绝。单次超门限测量不会改变输出；连续两次彼此一致的新测量可重捕获，最终输出仍受斜率限制。

## 输出状态

- `UNINITIALIZED`：尚无有效测量，发送 `BALL INVALID`。
- `MEASURED`：测量年龄不超过 90 ms，发送估计位置。
- `PREDICTED`：90–150 ms，以最后位置和速度外推，发送位置。预测值不是新的真实测量。
- `HELD`：150–300 ms，速度按 `exp(-hold_time/tau)` 衰减，位置逐渐停止，发送位置。
- `LOST`：超过 300 ms，发送 `BALL INVALID`，不能永久伪造旧位置。

ROI 当前几何失效时不采用整幅图检测结果。短时失效由 PREDICTED/HELD 覆盖；超过 300 ms 自然进入 LOST。控制器应把 `BALL INVALID` 解释为没有可靠视觉反馈。

## 固定 deadline 与带宽

调度采用累计绝对 deadline：`next_deadline += period`。延迟时跳过已错过周期，不突发补发；START/STOP 等控制命令优先并占用当前时隙，后续位置时隙仍回到原绝对节拍。STOP 后不再输出位置，重新进入比赛模式会 reset 并在首个测量前输出 INVALID。

最长位置帧 `BALL POS -125\r\n` 为 15 字节。9600 baud、8N1 每字节 10 bit，因此 50 Hz 最坏占用为 `15 × 10 × 50 / 9600 = 0.78125`，低于配置加载上限 85%。协议保持：

```text
BALL POS <integer_mm>\r\n
BALL INVALID\r\n
BALL START\r\n
BALL STOP\r\n
```

## 默认调参

配置位于 `config/mission.yaml -> ball_uart.position_estimator`。默认 `alpha=0.85`、`beta=0.08`、fresh/prediction/hold 为 90/150/300 ms、速度衰减常数 120 ms、最大速度和输出斜率均为 600 mm/s。输出斜率使用相邻实际采样时间差，20 ms 时最大约 12 mm。门控为 `20 mm + abs(v) × dt × 1.5`，重捕获确认数为 2。

## Windows 算法验证

```powershell
cd E:\NUEDC\camera
python tools/simulate_ball_position_estimator.py --output runs/ball_estimator_simulation.csv
```

该工具覆盖静止、100 mm/s、100/200/500 ms 丢失、重捕获、单帧异常和两端边界，不访问摄像头或串口，不代表真实视觉性能。

## 树莓派诊断与验收

部署前确认正式 profile，随后重启服务：

```bash
cd /home/clb/Desktop/camera
python tools/switch_steel_ball_model.py candidate-roi-strict
python tools/switch_steel_ball_model.py status
sudo systemctl restart vision-touch.service
```

记录 40 秒状态：

```bash
python tools/record_ball_output_diagnostics.py \
  --url http://127.0.0.1:8765/api/status \
  --duration 40 --sample-rate 50 \
  --output runs/ball_output_test/test.csv
```

验收时确认端点为左红右蓝、背景没有大面积相似色；查看 `summary.json` 中状态比例、输出 Hz、jitter P95、最大相邻步长和错误。用逻辑分析仪或 MCU 计数复核 9600 8N1、约 50 Hz，遮挡约 100 ms 应为预测位置、约 200 ms 应为衰减保持、超过 300 ms 应为 INVALID。网页采样频率不能作为 UART 物理频率的唯一证据。
