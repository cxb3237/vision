# 热点安装顺序审计

旧风险是安装早期执行 `nmcli connection up cxb-hotspot`，通过 wlan0 SSH 安装时会在 systemd/Nginx 尚未完成前断线。

现在第一阶段调用 `install_hotspot.sh --no-activate`，只创建/更新连接、固定参数和 autoconnect，不改变活跃 wlan0。第二阶段备份本项目管理的系统文件，生成 unit、Nginx 配置与 drop-in，执行 `nginx -t`、daemon-reload、enable 检查并启动内部调试服务。上述任一步失败均在热点激活前退出，并恢复本项目管理的文件及服务状态，不删除其他 Wi-Fi 连接。

只有所有前置步骤完成且视觉主服务模板哈希一致后，才执行 `systemctl restart cxb-hotspot.service`。脚本明确提醒 wlan0 SSH 将断开。最终激活失败时保留已校验配置并给出 `sudo systemctl restart cxb-hotspot.service` 恢复命令。
