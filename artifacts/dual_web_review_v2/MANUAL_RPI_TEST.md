# Raspberry Pi 5 手工验收步骤

1. 在工程根目录执行 `sudo bash deploy/install_tablet_web.sh --user "$USER" --project-dir "$PWD"`。
2. 重启树莓派。
3. 在平板 Wi-Fi 列表查找 `cxb`。
4. 使用密码 `123@chenzi` 连接。
5. 确认平板获得地址并能访问/ ping `192.168.50.1`。
6. 打开 `http://192.168.50.1/`，确认比赛实时画面。
7. 打开 `http://192.168.50.1:8080/`，确认调试画面和固定底部按钮。
8. 在比赛网站点击开始录像。
9. 保持录像 10 秒。
10. 点击停止录像并等待状态回到未录像。
11. 用 `ffprobe` 或播放器检查视频时长接近 10 秒。
12. 在浏览器内回放并拖动进度条，确认 Range 请求有效。
13. 下载录像并检查文件可播放。
14. 录像期间断开平板 Wi-Fi。
15. 重新连接后确认树莓派录像仍继续且可停止。
16. 停止 `camera-debug-web.service`。
17. 确认比赛网站和视觉主程序继续工作。
18. 重启 `cxb-hotspot.service`。
19. 确认视觉主程序和UART位置链路没有退出。
20. 检查 `journalctl -b -u cxb-hotspot.service -u nginx.service -u camera-debug-web.service -u vision-touch.service`，确认热点健康检查先于 Nginx 正常绑定。
