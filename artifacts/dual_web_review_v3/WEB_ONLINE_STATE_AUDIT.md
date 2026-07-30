# 网站在线状态审计

比赛页分别维护 `backendOnline`、`cameraOnline`、`streamOnline` 和 `mediaWorkerAlive`。提示优先级为：后端未连接；媒体服务故障；摄像头离线；视频流连接/重连；全部正常时隐藏覆盖层。状态 API 成功不会直接把流标记在线，只有 MJPEG `load` 才能完成该转换。

调试页同样分离视觉后端、摄像头和调试视频流状态。串口状态、摄像头状态和视频流状态不互相替代。
