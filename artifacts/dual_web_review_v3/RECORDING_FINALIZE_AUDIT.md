# 录像完成审计

停止流程为 POST stop 后进入 STOPPING/FINALIZING，页面每 300 ms 查询状态，最多等待 15 秒。只有达到 IDLE 才刷新录像列表；ERROR 时显示错误后刷新；超时给出明确提示且不提前猜测文件释放时间。控制器使用单个 running 标志和单 timer，刷新页面后如果状态仍为 STOPPING 会继续等待，finalizing 期间停止按钮禁用。

列表显示文件名、时长、分辨率、FPS、实际 codec、大小和 completed。非 H.264/avc1 显示黄色兼容性提示，仍保留 mp4v 回退。
