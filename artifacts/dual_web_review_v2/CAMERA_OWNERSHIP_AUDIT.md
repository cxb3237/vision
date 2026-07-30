# 摄像头与模型所有权审计

`app.py` 仍只构造一次 `CameraService(camera_config)`。`VisionRuntime` 保有该对象，调试预览由主循环把已标注图交给 `LatestFrameStream`；比赛媒体服务接收同一个对象，并只调用 `get_latest_frame(copy_image=True)`；录像器只接收 `frame_id`、`capture_timestamp` 和图像副本。

对 `web_competition`、`competition_ui` 和 `web_debug` 的全局搜索结果：

- 没有 `cv2.VideoCapture` 调用；唯一文字匹配位于 `web_debug/README.md`，用于声明不会打开 VideoCapture。
- 没有 `CameraService(` 构造。
- 没有 `serial.Serial`、`BallUartClient` 或 `navigator.serial`。
- 没有检测器构造或模型加载。

因此主进程仍只有一个摄像头句柄和一次 NCNN 初始化；两个网站与录像都是帧消费者。比赛媒体或 HTTP 服务启动失败由各自边界捕获，主视觉、UART和调试网页继续运行。
