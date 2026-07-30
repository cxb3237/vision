# 热点开机审计

## 固定配置

- NetworkManager 连接名：`cxb-hotspot`
- SSID：`cxb`
- 密码：`123@chenzi`
- 接口：`wlan0`
- IPv4：`192.168.50.1/24`，shared 模式
- `connection.autoconnect=yes`

`deploy/systemd/cxb-hotspot.service` 是 `Type=oneshot`、`RemainAfterExit=yes`，依赖 NetworkManager，安装目标为 `multi-user.target`。启动先幂等创建/更新连接并激活，再由 `hotspot_healthcheck.sh` 同时验证连接处于 active 且 `wlan0` 拥有固定地址。

## Enable与启动顺序

`deploy/install_tablet_web.sh` 明确执行：

1. 写入热点与调试站 unit；
2. 写入 `/etc/systemd/system/nginx.service.d/camera-tablet-hotspot.conf`；
3. drop-in 声明 `After=cxb-hotspot.service` 与 `Wants=cxb-hotspot.service`；
4. `systemctl daemon-reload`；
5. 分别 enable 热点、调试站、Nginx；
6. 先 restart 热点，再 restart 调试站和 Nginx；
7. 使用 `systemctl is-enabled --quiet` 检查三项；
8. 再次执行热点健康检查。

因此重启后的预期流程是 NetworkManager → 热点配置与健康检查 → Nginx 绑定 `192.168.50.1`。这些服务都没有成为 `vision-touch.service` 的 Required 依赖，热点或网站故障不会停止视觉主服务。

卸载脚本只删除 `camera-tablet-hotspot.conf`，不会修改系统原始 `nginx.service`。

## 尚需树莓派验证

当前 Windows/WSL 环境没有无线网卡和 Nginx，尚未真实验证 systemctl enabled 状态、重启后 SSID、DHCP、地址绑定及故障隔离。
