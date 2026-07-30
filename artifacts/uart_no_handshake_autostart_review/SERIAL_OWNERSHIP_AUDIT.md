# 串口与硬件所有权审计

## `/dev/ttyAMA0` 所有者

生产环境只有 `app.py` 创建的一个 `BallUartClient` 调用 `serial.Serial`。默认设备 `/dev/ttyAMA0`、波特率 9600，8N1、无软硬件流控。

## 摄像头与模型所有者

- `app.py` 只有一个 `CameraService(...)` 创建点。
- CameraService 内部只有一个生产 `cv2.VideoCapture` 创建点。
- `app.py` 只有一个 NCNN steel-ball detector 工厂调用。
- 比赛媒体、调试代理、热点和 Nginx 不创建 CameraService、VideoCapture、BallUartClient 或 serial.Serial。

## 防止重复占用

- 开机主视觉服务只有 `vision-touch.service`。
- 未创建 `camera-uart.service`、`serial.service` 或任何辅助常驻 UART unit。
- `tools.test_ball_uart` 是手动诊断工具，没有被任何 systemd unit 或安装脚本启动。
- 使用诊断工具前必须停止主服务并确认设备已释放：

```bash
sudo systemctl stop vision-touch.service
lsof /dev/ttyAMA0
python3 -m tools.test_ball_uart --port /dev/ttyAMA0 monitor
```

诊断结束后：

```bash
sudo systemctl restart vision-touch.service
```
