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

VisionRuntime -> 127.0.0.1:8765（原有状态、画面与控制 API）
camera-debug-web.service -> 127.0.0.1:8081（静态页面与白名单代理）
Nginx 192.168.50.1:8080 -> 平板调试网站
Nginx 192.168.50.1:80   -> 比赛图传、录像、回放与下载网站
```

网页不会使用 Web Serial，也不会直接打开串口。摄像头、NCNN 和 UART 仍只由原有视觉主进程协调，摄像头始终只有一个 `CameraService` 实例。独立调试站只代理原有本机接口，不打开摄像头、不加载第二份 NCNN 模型，也不接触 UART。热点、调试站和 Nginx 都是新增的独立服务；它们失败不会级联停止视觉主服务。

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

## 平板热点与固定网站

Raspberry Pi 使用 NetworkManager 创建固定热点：

- 热点名称：`cxb`
- 热点密码：`123@chenzi`
- 无线接口：`wlan0`
- 树莓派地址：`192.168.50.1/24`
- 比赛网站：[http://192.168.50.1/](http://192.168.50.1/)
- 调试网站：[http://192.168.50.1:8080/](http://192.168.50.1:8080/)

TCP 80 的比赛网站提供实时摄像头画面、录像状态与计时、文件名、写入/丢弃帧数、实际帧率、剩余空间，以及录像开始、停止、列表、浏览器 Range 回放和下载。比赛页不提供摄像头参数、UART、位置下发、模型或系统控制。录像复用视觉主进程唯一的 `CameraService`，按 `FramePacket.capture_timestamp` 重采样到容器帧率；输入偏慢时复制最近帧、偏快时丢弃多余帧，从而使视频时长接近真实经过时间。

调试站使用顶部关键状态、可滚动详细信息和固定底部操作栏；在 1024×600、1280×800 和 1920×1080 横屏下，“摄像头参数”和“启用/停止位置下发”始终可见。主状态只有在已请求输出、UART 在线、MCU READY 且 `position_tx_hz > 0` 时才显示“位置下发运行中”。未标定时前端禁用“启用位置下发”，后端也会拒绝该操作。

在树莓派工程根目录安装：

```bash
sudo apt install network-manager nginx
sudo bash deploy/install_tablet_web.sh --user "$USER" --project-dir "$PWD"
```

安装脚本会幂等更新唯一的 `cxb-hotspot` 连接，删除旧的 `~/.config/autostart/vision-touch-kiosk.desktop`，安装 `cxb-hotspot.service`、`camera-debug-web.service` 和专用 Nginx 路由，并在启动前执行 `nginx -t`。本工程专用的 `nginx.service.d/camera-tablet-hotspot.conf` 声明 `After=`/`Wants=cxb-hotspot.service`，避免 Nginx 在 `192.168.50.1` 尚未配置时过早绑定失败；卸载脚本只删除这一 drop-in，不修改系统原始 `nginx.service`。

`config/competition_ui.yaml` 默认 `enabled: true`。原有 `vision-touch.service` 的 ExecStart 完全不变：`app.py --touch-ui` 会在同一进程内自动启动仅监听 `127.0.0.1:8000` 的比赛后端，并复用唯一摄像头。比赛后端、录像目录或 Nginx 故障只记录错误，不会停止视觉识别、UART或位置控制主链路。

树莓派不再开机自动打开 Chromium，也不再需要桌面自动登录、显示器或本地 kiosk。平板连接 Wi-Fi `cxb`，输入密码 `123@chenzi`，然后用 Chrome/Edge 打开上述固定地址即可。

查看状态和日志：

```bash
systemctl status vision-touch.service
journalctl -u vision-touch.service -f
systemctl status cxb-hotspot.service camera-debug-web.service nginx.service
journalctl -u cxb-hotspot.service -u camera-debug-web.service -u nginx.service -f
```

手动重启新增服务：

```bash
sudo systemctl restart cxb-hotspot.service
sudo systemctl restart camera-debug-web.service
sudo systemctl restart nginx.service
```

只卸载本次新增的热点、调试站和 Nginx 路由：

```bash
sudo bash deploy/uninstall_tablet_web.sh --user "$USER" --project-dir "$PWD"
```

卸载不会删除视觉主服务、工程、模型、录像、其他 Wi-Fi 连接或其他 Nginx 网站。

原有视觉主服务若尚未安装，仍使用原安装命令：

```bash
sudo bash deploy/install_touch_ui.sh --user "$USER" --project-dir "$PWD" --start
systemctl status vision-touch.service
journalctl -u vision-touch.service -f
```

## 常见故障

- 串口被占用：停止旧服务，并用 `lsof /dev/ttyAMA0` 检查占用。
- 没有权限：执行 `sudo usermod -aG dialout "$USER"`，然后重新登录。
- 无 READY：检查 9600/8N1、TX/RX 交叉、共地和 MCU UART2。
- MCU READY 在树莓派启动前错过：客户端会发 `BALL PING`，`OK C=BALL_PING` 可恢复握手。
- PING 有回复但不发位置：确认网页已启用位置下发、MCU READY、标定状态有效，且检测到了钢球。
- 没识别到球：检查模型是否加载、画面曝光、摄像头视野以及网页中的识别错误。
- 反向运动：切换 `servo_side`，不要交换毫米端点来掩盖安装方向。
- 平板找不到 `cxb`：检查 `systemctl status NetworkManager cxb-hotspot.service` 和 `journalctl -u cxb-hotspot.service`，确认 `wlan0` 存在且未被其他热点或客户端连接占用。
- 热点密码错误：忘记旧网络后重新连接，密码严格为 `123@chenzi`。
- 能连热点但网页打不开：先 `ping 192.168.50.1`，再检查 `systemctl status nginx camera-debug-web`；客户端应关闭移动网络代理/VPN 后重试。
- `192.168.50.1` 不通：运行 `ip address show wlan0`，应看到 `192.168.50.1/24`；再运行 `deploy/hotspot/hotspot_healthcheck.sh`。
- TCP 80 不通：检查 `vision-touch.service` 中的比赛后端日志、`curl http://127.0.0.1:8000/healthz` 和 Nginx；8000 是内部端口，不应从平板直接访问。
- TCP 80 正常但 8080 不通：检查 `camera-debug-web.service` 以及 `curl http://127.0.0.1:8081/debug/healthz`。8081 和 8765 都是内部端口，不应从平板直接访问。
- 调试页显示“视觉主服务未连接”：检查 `vision-touch.service`；代理会继续运行，视觉主服务恢复后页面会自动恢复轮询。
- 摄像头画面不刷新：先确认视觉主服务在线，再检查 `/api/preview.mjpg` 和摄像头日志；不要启动第二个采集程序。
- NetworkManager 未运行：执行 `sudo systemctl enable --now NetworkManager` 后重新启动 `cxb-hotspot.service`。

## 测试

```bash
python -m compileall -q .
python -m pytest -q -rs
python -m tools.touch_ui_selftest
node --check web_debug/static/app.js
node --check web_debug/static/control_scheduler.js
node --check web_competition/app.js
node --test tests/js/*.test.js
bash -n deploy/install_tablet_web.sh
bash -n deploy/uninstall_tablet_web.sh
bash -n deploy/hotspot/install_hotspot.sh
bash -n deploy/hotspot/uninstall_hotspot.sh
bash -n deploy/hotspot/hotspot_healthcheck.sh
```

Windows 可导入全部生产模块并运行无硬件测试；V4L2、串口、NetworkManager、systemd 和 Nginx 相关自动测试使用 mock 或静态检查。真实热点创建与重启恢复、DHCP、多型号平板连接、Nginx 绑定、实时 MJPEG、真实摄像头、UART 电气连接、MCU 复位恢复及实际位置发送频率必须在 Raspberry Pi 5 上现场验证。

旧多检测器、旧二进制通信、训练数据和历史录制/标定工具已从部署工程移除；模型权重位于 `models/steel_ball/best_ncnn_model/`，本次清理不修改模型参数或二进制权重。
