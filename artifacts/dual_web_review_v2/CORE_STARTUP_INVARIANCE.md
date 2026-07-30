# 视觉主服务启动不变性

开工前后 `deploy/vision-touch.service.template` SHA-256 均为：

`0f6e077e0dff49f52b6502c4a289d11b5893a383367fc6f94b1dd56fa7ed1557`

ExecStart 仍为：

`.venv/bin/python app.py --mode track --serial-port /dev/ttyAMA0 --baudrate 9600 --serial-rate 50 --touch-ui --headless`

比赛网站通过 `config/competition_ui.yaml enabled: true` 和 `app.py` 的触摸模式启动路径接入，不修改 systemd 命令，不启动第二个 app.py。部署安装器只读取主服务模板做前后哈希保护，不安装、enable、restart、stop 或删除 `vision-touch.service`。
