# Raspberry Pi + MCU 手工测试

1. 停止视觉服务：`sudo systemctl stop vision-touch.service`。
2. 用 DAPLink 或逻辑分析仪监听树莓派 GPIO14/TX，确认 3.3 V TTL、9600、8N1。
3. 在 camera 根目录手动启动：`.venv/bin/python app.py --touch-ui --headless --serial-port /dev/ttyAMA0 --baudrate 9600 --serial-rate 50 --serial-debug`。
4. 不向树莓派回复任何内容。
5. 确认首先收到 `BALL START\r\n`。
6. 确认没有收到 `BALL PING\r\n`。
7. 放置钢球和 A/B 端点标记。
8. 确认收到范围为 `-125..125` 的整数 `BALL POS`。
9. 移走钢球或任一端点标记。
10. 确认收到 `BALL INVALID\r\n`。
11. 按 Ctrl+C 退出。
12. 确认收到 `BALL STOP\r\n`。
13. 安装并 enable：`sudo bash deploy/install_touch_ui.sh --user "$USER" --project-dir "$PWD" --start`。
14. 确认：`systemctl is-enabled vision-touch.service` 返回 `enabled`。
15. 重启树莓派：`sudo reboot`。
16. 全程不发送任何 MCU 回复，确认开机后自动收到 START 和位置流。
17. 制造一次串口断开/恢复，确认新连接只收到一个 START，且不补发断线期间旧位置。
18. 在网页主动停止位置下发，确认收到 STOP 且不会自动重新 START。
19. 重启视觉进程，确认启动配置再次自动发送 START。
20. 查看日志：`journalctl -u vision-touch.service -f`，确认无 PING/READY 等待刷屏。
