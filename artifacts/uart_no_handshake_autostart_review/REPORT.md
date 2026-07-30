# UART 无握手与开机自启动审查报告

## 结论

- 生产 `BallUartClient` 不再主动发送 `BALL PING`，也不再周期发送 `BALL STATUS`。
- 串口打开后即具备发送许可，不等待 READY、PING ACK 或其他 MCU 回复。
- 启动输出意图在打开串口前设置；每次新连接排队且最多排队一个 `BALL START`。
- POS/INVALID 不依赖 MCU 回复，继续采用 latest-only 和现有最大频率限制。
- 串口重连时丢弃断线期间的旧位置，重新发送一个 START，随后只发送新视觉结果。
- 主动停止发送 STOP 并清空位置；程序关闭、SIGINT、SIGTERM 和 systemd stop 会尽最大努力发送 STOP。
- `config/touch_ui.yaml` 的唯一启动开关 `startup.competition_mode` 已设为 `true`，只在进程启动时应用一次。
- 开机仍只使用 `vision-touch.service`，没有创建第二个摄像头或串口服务。

## 生产序列

```text
UART OPEN /dev/ttyAMA0 9600 8N1
BALL START\r\n
BALL POS <int -125..125>\r\n   或   BALL INVALID\r\n
...
BALL STOP\r\n
```

自动化测试记录的完整静默 MCU 序列为：

```text
BALL START\r\n
BALL POS 12\r\n
BALL POS 10\r\n
BALL POS 8\r\n
BALL INVALID\r\n
BALL STOP\r\n
```

该原始写入列表中没有 `BALL PING`。

## 修改范围

- UART：`drivers/ball_uart_client.py`
- 启动意图：`core/vision_runtime.py`、`app.py`
- 配置：`config/mission.yaml`、`config/touch_ui.yaml`、`core/config_loader.py`
- systemd 安装检查：`deploy/install_touch_ui.sh`、`deploy/install_tablet_web.sh`
- 测试：UART、配置、启动与部署相关测试
- 文档：`README.md`

未修改 MCU 协议、摄像头、识别、动态端点检测、映射公式、模型、网页、热点、Nginx 或录像逻辑。

## 自动检查

- `python -m compileall -q .`：通过。
- `python -m pytest -q -rs`：267 passed，2 skipped。
- `node --test tests/js/*.test.js`：19 passed，7 skipped，0 failed；跳过项均因 Playwright 未安装。
- `bash -n deploy/install_touch_ui.sh`：通过。
- `bash -n deploy/install_tablet_web.sh`：通过。
- `git diff --check`：通过。

## 未完成的现场验证

当前环境不是 Raspberry Pi，未连接真实 `/dev/ttyAMA0`、DAPLink 或 MSPM0。因此尚未完成真实 3.3 V TTL 波形、MCU 复位、树莓派重启后 systemd 实际拉起，以及真实摄像头位置流测试；步骤见 `MANUAL_TEST.md`。
