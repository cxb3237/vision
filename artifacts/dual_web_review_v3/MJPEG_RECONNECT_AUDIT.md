# MJPEG 重连审计

比赛站和调试站各使用独立的小型 `MjpegReconnectController`。初次连接直接设置带 `ts` 时间戳的 `/api/preview.mjpg`；失败后按 1、2、4、5、5…秒退避。`retryTimer` 非空时拒绝创建第二个计时器；同一 `<img>` 的 `src` 替换保证一次只有一个有效流连接。`load` 清理计时器、重置退避到 1 秒并标记在线；`error` 标记离线并排队重连；页面重新可见时只在流离线时触发连接；卸载时移除 timer、监听器和 src。

状态轮询不会重写 `img.src`。对应 Node 测试覆盖两站点的退避上限、单 timer、load 重置、start 幂等和 stop 清理。
