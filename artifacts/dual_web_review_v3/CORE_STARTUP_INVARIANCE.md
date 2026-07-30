# 视觉主服务启动不变性

- 修改前 SHA-256：`0f6e077e0dff49f52b6502c4a289d11b5893a383367fc6f94b1dd56fa7ed1557`
- 修改后 SHA-256：`0f6e077e0dff49f52b6502c4a289d11b5893a383367fc6f94b1dd56fa7ed1557`
- ExecStart：`.venv/bin/python app.py --mode track --serial-port /dev/ttyAMA0 --baudrate 9600 --serial-rate 50 --touch-ui --headless`

哈希一致，证明 `deploy/vision-touch.service.template` 未发生逐字节变化。新增热点、调试站和 Nginx 安装脚本不会安装、重启或停止视觉主服务。
