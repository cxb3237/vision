# 比赛网站审计

## 页面与开机关系

Nginx 仅在 `192.168.50.1:80` 提供 `web_competition`。页面包含实时画面、摄像头/录像状态、计时、文件名、写入帧、丢帧、实际帧率、剩余空间、开始/停止、列表、刷新、回放和下载；后端不可达时保留静态页并显示中文错误。

`config/competition_ui.yaml` 默认启用且只允许 `127.0.0.1:8000`。原 `vision-touch.service` 不增加参数；`--touch-ui` 会在唯一视觉进程内自动创建媒体服务和 HTTP 服务。显式 `--competition-ui` 仍可使用，但分支逻辑不会重复创建。

## API与安全

- `GET /healthz`
- `GET /api/status`
- `GET /api/preview.mjpg`
- `POST /api/recording/start`
- `POST /api/recording/stop`
- `GET /api/recordings`
- `GET|HEAD /recordings/<safe-name>.mp4`

录像响应支持单段 `Range: bytes=...`，返回 206、`Content-Range`、`Accept-Ranges`、正确长度和 `video/mp4`。`?download=1` 增加 attachment 响应头。

服务仅接受安全 MP4 文件名，拒绝 `..`、绝对/反斜杠路径、符号链接和目录外解析结果。请求体上限 64 KiB；负长度、非法长度和非法 JSON 返回 400，超限返回 413，启停仅接受空对象。API、流和录像均不缓存；MJPEG 禁止 Nginx 缓冲。

比赛页面没有摄像头参数、UART、位置下发、NCNN参数、运行时停止或命令执行入口。
