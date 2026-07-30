# 开机自启动审计

## 唯一主服务

服务名：`vision-touch.service`

模板：`deploy/vision-touch.service.template`

```text
ExecStart=@PROJECT_DIR@/.venv/bin/python app.py --mode track --serial-port /dev/ttyAMA0 --baudrate 9600 --serial-rate 50 --touch-ui --headless
Restart=on-failure
RestartSec=3
WantedBy=multi-user.target
```

该进程同时持有唯一摄像头、唯一 NCNN detector 和唯一 BallUartClient。没有新增 camera-uart.service、serial.service 或第二个 Python 进程。

## Enable 检查

`deploy/install_touch_ui.sh` 执行：

```bash
systemctl daemon-reload
systemctl enable vision-touch.service
systemctl is-enabled --quiet vision-touch.service
```

`deploy/install_tablet_web.sh` 也明确 enable 并通过 `systemctl is-enabled --quiet` 检查主服务，但不重启、不停止、不创建第二份主服务 unit。

## 开机执行顺序

```text
树莓派开机
→ multi-user.target 拉起 vision-touch.service
→ app.py 创建唯一 CameraService、NCNN detector、BallUartClient
→ 打开摄像头并加载模型
→ 启动配置自动设置位置输出意图
→ 打开 /dev/ttyAMA0 @ 9600 8N1
→ BALL START
→ 有效结果 BALL POS / 无效结果 BALL INVALID
→ 异常退出由 systemd 3 秒后重启
→ 正常 stop 尽最大努力 BALL STOP
```

未创建第二个串口服务，因此不存在两个 systemd unit 同时占用 `/dev/ttyAMA0` 的设计路径。
