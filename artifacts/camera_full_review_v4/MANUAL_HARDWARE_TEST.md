# Raspberry Pi 5 / MSPM0 手工硬件测试

以下步骤本轮未在 Windows 开发机执行，必须在树莓派 5 现场按顺序验证。

1. 停止视觉服务：`sudo systemctl stop vision-touch.service`。
2. 检查串口占用：`lsof /dev/ttyAMA0`，确认没有其他进程持有端口。
3. 打开监视：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 monitor`。
4. 复位 MCU，确认看到 `READY BALL UART2 9600`；Ctrl+C 退出 monitor。
5. PING：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 ping`，确认 `OK C=BALL_PING`。
6. STATUS：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 status`，确认收到 `BALL S=...`。
7. START：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 start`。
8. 连续三次 POS 0：重复运行 `python3 -m tools.test_ball_uart --port /dev/ttyAMA0 pos 0` 三次。
9. 再次 STATUS，确认 `EN=1`。
10. POS +35：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 pos 35`。
11. POS -35：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 pos -35`。
12. INVALID：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 invalid`。
13. STOP：`python3 -m tools.test_ball_uart --port /dev/ttyAMA0 stop`。
14. 按 README 完成像素端点标定并设置 `calibrated: true`。
15. 启动完整程序：`python3 app.py --touch-ui --headless --serial-port /dev/ttyAMA0`。
16. 网页先检查调试识别不下发；进入比赛模式确认 START 和 POS；退出确认 STOP 且不再发送 POS。
17. 摄像头断开测试：拔出摄像头，超过 1 秒网页应显示离线；重接并恢复出帧后自动在线。
18. MCU 复位恢复测试：比赛模式运行中复位 MCU，确认客户端重新 PING/READY、按最终意图 START，并且只发送复位后的新位置。
19. 查看网页 `camera_fps`、`vision_fps`、`position_tx_hz`、`invalid_tx_hz`，记录实测值，不把 50 Hz 上限当成实测值。

