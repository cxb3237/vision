# 双网站与热点启动顺序审查报告 v2

## 结论

- 固定热点配置保持为 `cxb` / `123@chenzi` / `wlan0` / `192.168.50.1/24` / `cxb-hotspot`。
- 安装脚本启用 `cxb-hotspot.service`、`camera-debug-web.service` 和 `nginx.service`，并安装只属于本工程的 Nginx systemd drop-in，使 Nginx 在热点健康检查完成后启动。
- `deploy/vision-touch.service.template` 未修改，SHA-256 始终为 `0f6e077e0dff49f52b6502c4a289d11b5893a383367fc6f94b1dd56fa7ed1557`。
- `app.py --touch-ui` 会读取 `config/competition_ui.yaml`；默认 `enabled: true` 时，在原进程中自动启动 `127.0.0.1:8000` 比赛后端。
- 调试预览、比赛预览和录像都从现有唯一 `CameraService` 读取最新帧；网站没有创建摄像头、加载模型或打开 UART。
- 比赛网站具备实时 MJPEG、录像启停、状态、列表、Range 回放和下载，且不暴露视觉控制、UART或系统操作。
- 录像以 `FramePacket.capture_timestamp` 为时间轴重采样，记录源帧、写入帧、复制帧和丢弃帧，临时文件仅在成功停止后原子改名。
- 调试网站将关键状态固定在上方、详细信息放入滚动区、高级信息折叠，并将摄像头参数与位置下发按钮固定在底部。

## 自动验证

- Python：250 passed，2 skipped。跳过项是未显式启用的浏览器几何测试；生产模块和新增后端、录像、部署静态测试均通过。
- JavaScript 语法：三个目标文件全部通过。
- Node：10 passed，7 skipped；跳过项需要本机 Playwright Chromium，测试源码已保留并覆盖指定横屏几何与状态场景。
- Shell：五个部署/热点脚本均通过 `bash -n`。
- `git diff --check`：通过。
- `nginx -t`：NOT RUN，当前 Windows/WSL 环境未安装 Nginx。
- `systemd-analyze verify`：已尝试；WSL 中源文件包含安装期占位符且缺少 Raspberry Pi 的 `nmcli`/目标路径，因此不能作为部署单元完成验证。语法与关键关系由自动测试覆盖，需在树莓派安装生成最终 unit 后复验。

## 未进行的硬件验证

没有在 Raspberry Pi 5 上真实执行热点重启、Nginx 绑定、摄像头 MJPEG、硬件编码、10 秒录像、Android 平板回放下载或断网持续录像。步骤见 `MANUAL_RPI_TEST.md`。

## Git边界

基线提交为 `ffc1a54511ee6932d454cbbe8cbc3087588d5c92`。现有未提交修改被保留；未执行 add、commit、merge 或 push。
