# 钢球视觉定位与 UART2 ASCII 通信

本工程只服务于车载平衡滚球系统：树莓派 5 从一个摄像头采集画面，使用部署好的 YOLO-NCNN 模型检测钢球，把横向像素坐标线性映射到 `-125..125 mm`，再通过 UART2 向 MSPM0 发送 ASCII 命令。本地网页显示实时标注、位置、帧率、UART/MCU 状态，并提供必要的摄像头参数和位置下发启停操作。

## 运行架构

```text
CameraService (latest frame)
  -> SteelBallYoloNcnnDetector
  -> TargetTracker
  -> pixel X -> -125..125 mm
  -> BallUartClient (latest-only)
  -> MSPM0

Touch UI <- 状态快照 / 最新 JPEG <- VisionRuntime
Touch UI -> Python API -> 运行时命令队列
```

网页不会使用 Web Serial，也不会直接打开串口。摄像头、NCNN、网页和 UART 均由同一个 Python 进程协调；摄像头始终只有一个 `CameraService` 实例。

## 接线与串口参数

- 树莓派 GPIO14/TX -> MSPM0 PA24/RX
- 树莓派 GPIO15/RX <- MSPM0 PA23/TX
- 两端 GND 必须共地
- 电平：3.3 V TTL
- 参数：`9600 baud, 8N1, ASCII, 无流控`
- 行尾：`CRLF` (`\r\n`)

不要接 RS-232 电平；TX/RX 必须交叉。

## BALL 命令

树莓派只会发送：

```text
BALL START
BALL STOP
BALL POS <整数 -125..125>
BALL INVALID
BALL PING
BALL STATUS
```

每行实际以 `\r\n` 结束。位置结果为 latest-only：视觉线程提交不阻塞，未发送的旧位置会被新位置覆盖，不会积压历史帧。未进入比赛模式、MCU 尚未 READY 或位置尚未有效标定时不发送 `BALL POS`。比赛模式下识别失败或未标定时发送 `BALL INVALID`。

以下任一精确回复可建立 READY：

```text
READY BALL UART2 9600
OK C=BALL_PING
```

普通文本、`OK P`、`OK I` 或其他 `OK C=...` 不会误触发 READY。链路超过 `link_timeout_s` 没有有效回复后会回到握手状态，丢弃旧位置并周期发送 PING；比赛输出意图会保留，重新 READY 后先发送 START，再恢复位置。

## 像素到毫米标定

在 [config/mission.yaml](config/mission.yaml) 的 `ball_uart` 中设置：

```yaml
calibrated: false
left_endpoint_px: 72
right_endpoint_px: 568
servo_side: right
```

现场标定流程：固定最终比赛分辨率和机位，把球依次放到导轨有效左、右端，从网页读取像素 X，写入两个端点；确认数值有限、互不相同且都在实际图像宽度内，再按安装方向设置 `servo_side: left|right`，最后把 `calibrated` 改为 `true` 并重启。`left_endpoint_px` 映射为 `-125 mm`，`right_endpoint_px` 映射为 `+125 mm`，中点映射为 `0 mm`，范围外会夹紧。

`calibrated: false` 是安全默认值：网页会显示具体未标定/参数错误原因，视觉识别和预览仍运行，但 UART 禁止发送 `BALL POS`。不要把默认端点当作正式标定；更换分辨率、摄像头位置或导轨后必须重新标定。

## 正式运行

安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Raspberry Pi NCNN 部署环境使用：
pip install -r requirements-rpi-ncnn.txt
sudo apt install v4l-utils
sudo usermod -aG dialout "$USER"
```

加入 `dialout` 后必须重新登录，新的串口组权限才会生效。

调试模式（检测和网页照常运行，但不下发位置）：

```bash
python3 app.py --touch-ui --headless --serial-port /dev/ttyAMA0
```

启动后立即进入比赛输出：

```bash
python3 app.py --touch-ui --headless --serial-port /dev/ttyAMA0 --competition-mode
```

本地 OpenCV 窗口：

```bash
python3 app.py --display --no-serial
```

`--serial-debug` 会记录 UART 打开/关闭、PING、READY 来源、START/STOP、错误、重连和限频后的位置发送。`send_rate_hz: 50` 只是最大 UART 发送频率，不会重发旧位置；实际位置更新频率不会高于有效视觉结果频率。网页状态分别显示 `camera_fps`、`vision_fps`、`position_tx_hz` 和 `invalid_tx_hz`，不要把 50 Hz 上限当成当前实测值。

## 独立 UART 硬件检查

```bash
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 monitor
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 ping
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 status
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 start
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 pos 0
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 pos 35
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 invalid
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 stop
```

工具固定使用 ASCII、8N1、无流控，默认 9600，并打印 TX/RX 原文。`pos` 超出 `-125..125` 会返回非零退出码。

钢球 NCNN 单图离线推理复用正式配置、正式 NCNN runtime 和正式检测器：

```bash
python3 -m tools.steel_ball_ncnn_offline \
  --image test.jpg \
  --config config/steel_ball_ncnn.yaml \
  --output annotated.jpg
```

输出包含是否识别、中心 X/Y、置信度和推理耗时。当前 Python 环境没有 `ncnn` 时会明确报错并返回非零退出码。

摄像头 V4L2 配置检查：

```bash
python3 -m tools.camera_profile_check --device 0 --camera-config config/camera.yaml --apply
```

## 网页字段与操作

网页显示钢球像素 X、毫米位置、标定错误、摄像头/识别/预览 FPS、位置/无效包实际发送频率、UART 是否打开、MCU READY、UART 状态机、MCU 状态字段、最近 UART 错误和最近发送位置。按钮“启用位置下发”进入比赛模式并发送 START；停止时先禁用输出、清除旧位置并发送 STOP。摄像头参数通过 Python 后端应用，网页本身不访问硬件。

网页具有比赛模式和停止程序等控制接口，因此服务端只接受 `127.0.0.1`、`localhost` 或 `::1`，不会监听 `0.0.0.0` 或局域网/公网地址。HTTP 请求体上限为 64 KiB。

## systemd 与 kiosk

```bash
sudo bash deploy/install_touch_ui.sh --user "$USER" --project-dir "$PWD" --start
systemctl status vision-touch.service
journalctl -u vision-touch.service -f
```

浏览器 kiosk 使用项目专用 profile，只访问本机 URL。退出 kiosk 不会停止视觉后端；需要重新打开时运行：

```bash
deploy/start_kiosk.sh &
```

## 常见故障

- 串口被占用：停止旧服务，并用 `lsof /dev/ttyAMA0` 检查占用。
- 没有权限：执行 `sudo usermod -aG dialout "$USER"`，然后重新登录。
- 无 READY：检查 9600/8N1、TX/RX 交叉、共地和 MCU UART2。
- MCU READY 在树莓派启动前错过：客户端会发 `BALL PING`，`OK C=BALL_PING` 可恢复握手。
- PING 有回复但不发位置：确认网页已启用位置下发、MCU READY、标定状态有效，且检测到了钢球。
- 没识别到球：检查模型是否加载、画面曝光、摄像头视野以及网页中的识别错误。
- 反向运动：切换 `servo_side`，不要交换毫米端点来掩盖安装方向。

## 测试

```bash
python -m compileall -q .
python -m pytest -q
python -m tools.touch_ui_selftest
node --check touch_ui_web/app.js
node --check touch_ui_web/control_scheduler.js
```

Windows 可导入全部生产模块并运行无硬件测试；V4L2、串口、systemd 和 kiosk 测试均使用 mock 或静态检查。真实摄像头、真实 UART 电气连接、MCU 复位恢复、实际位置发送频率和树莓派 kiosk 必须在 Raspberry Pi 5 上现场验证。

旧多检测器、旧二进制通信、训练数据和历史录制/标定工具已从部署工程移除；模型权重位于 `models/steel_ball/best_ncnn_model/`，本次清理不修改模型参数或二进制权重。
