# 双网站可靠性审查 v3

## 结论

本轮完成比赛站与调试站 MJPEG 自动重连、三类在线状态分离、热点两阶段安装、Playwright 缺失降级、比赛媒体线程顶层异常隔离、HTTP 请求体超时、录像完成轮询和 codec 元数据显示。视觉主服务模板保持逐字节不变，未提交、未推送。

## 基线与范围

- 分支：`feature/ball-position-link`
- 基线/当前 HEAD：`ffc1a54511ee6932d454cbbe8cbc3087588d5c92`
- `deploy/vision-touch.service.template` SHA-256：`0f6e077e0dff49f52b6502c4a289d11b5893a383367fc6f94b1dd56fa7ed1557`
- 未修改 MCU、UART ASCII 协议、NCNN 权重、摄像头生命周期或视觉主服务启动参数。

## 自动验证

- Python：265 passed，2 skipped。
- JavaScript：19 passed，7 skipped，0 failed；跳过项均为缺少 Chromium 的浏览器布局测试。
- 缺少 Playwright 的干净模块环境：普通 JS 测试通过，7 个布局测试明确 SKIP，退出码 0。
- 五个部署 Shell 脚本均通过 `bash -n`。
- `git diff --check` 通过。

## 环境限制

未在本机执行真实热点切换、Nginx 实际配置加载、安装后 systemd unit、真实摄像头、真实录像或平板播放测试。原始 systemd 源文件带安装期占位符，当前 WSL 的直接 verify 不能代表树莓派安装后的 unit。
