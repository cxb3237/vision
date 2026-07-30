# Camera Full Review v4

## 基线与范围

- 基础提交：`e25195c1763457f7cb942b38a0011f2d01c80f2e`
- 当前分支：`feature/ball-position-link`
- 本轮仅修改树莓派视觉端；未修改模型权重、MSPM0 固件或 MCU 协议，未执行 commit、push、merge。
- 工作区开始时已有大量未提交清理改动和删除项；本轮完整保留，并在 `GIT_STATUS.txt`、`CHANGED_FILES.txt`、`DELETED_FILES.txt` 和 `GIT_DIFF.patch` 中如实记录。

## 本轮修改

1. `BallUartClient` 根据最终 `desired_running` 校正 MCU STATUS 的 `EN`，纠正 START/STOP 高优先级、去重、清除相反命令并按 0.5 秒默认间隔限频；STOP 校正同时清除待发位置。
2. UART 独立测试工具严格等待每类命令的预期回复，超时、ERR、参数错误和串口失败分别返回稳定非零退出码。
3. 位置标定改为 `calibrated: false` 安全默认；未标定或参数错误禁止 `BALL POS`，网页状态暴露具体错误及端点/方向。
4. 摄像头在线改为“采集线程运行且最新帧龄小于 `camera_online_timeout_s`”，状态增加 `latest_frame_age_s`。
5. `camera_profile_check` 不再让未传入的 `--device` 以 `None` 覆盖 YAML，并拒绝非法设备参数。
6. `steel_ball_ncnn_offline` 已实现真实单图推理，复用正式配置加载器、正式 NCNN runtime 和正式检测器；缺少 ncnn 时明确失败。
7. 触摸网页仅允许 `127.0.0.1`、`localhost`、`::1`；请求体拒绝负数/非法 Content-Length，64 KiB 以上返回 413，并设置读取超时。
8. Raspberry Pi NCNN 依赖加入 `pyserial`，安装脚本检查 `serial`、`cv2`、`yaml` 可导入。
9. 状态增加 `position_tx_hz`、`invalid_tx_hz`；文档明确 50 Hz 是最大发送上限，实际位置频率不高于有效视觉结果频率。

## STATUS 意图校正设计

四种组合、限频和测试证据见 `UART_STATE_RECONCILIATION.md`。迟到 ACK 与 STATUS 只用于确认或触发纠正，不能改变用户最终意图。

## UART 工具退出码

- `0`：收到预期回复；monitor 被 Ctrl+C 正常终止。
- `2`：参数或配置错误。
- `3`：预期回复超时。
- `4`：收到 `ERR C=...`。
- `5`：pyserial 缺失、串口打开、读取或写入失败。

详见 `UART_TOOL_AUDIT.md`。

## 标定安全设计

`calibrated=false`、非有限端点、相同端点、非法 `servo_side`、端点越过实际图像宽度都会让 `ball_position_calibrated=false`。视觉与网页继续运行；比赛模式发 INVALID 而不发 POS。详见 `CALIBRATION_AUDIT.md`。

## 摄像头在线判断

`camera_online = camera_service.is_running() and latest_frame_age_s is finite and 0 <= age < camera_online_timeout_s`。历史 `frames_ok` 不再参与在线判定，恢复出帧后状态自动恢复。

## 网页监听限制

配置加载、命令行覆盖和服务构造三层均限制回环地址。HTTP 请求体最大 64 KiB；负数或非法长度返回 400，超限返回 413，读取设置 2 秒超时。

## 实际发送频率

`send_rate_hz=50` 仅是 UART latest-only 通道的最大频率。客户端不会为了凑 50 Hz 重发旧位置；网页和状态接口分别报告相机、识别、位置发送和 INVALID 发送的实际滚动频率。

## 搜索审计

- 生产代码未发现 `VMC`、`vmc_`、`CRC16`、`BALL_POSITION_SOF`、`hybrid_v10`。
- 未发现旧 `ColorDetector`、`ShapeDetector`、`DigitDetector`。
- `Web Serial` 仅出现在 README 的否定说明，`navigator.serial` 仅出现在“必须不存在”的前端测试。
- 未发现 `ball_position_calibrated.*True`、`frames_ok > 0` 或生产配置中的 `0.0.0.0`。
- `115200` 仅在测试中作为错误 READY 行，验证不会误识别为当前 9600 协议。

## 验证结果

- Python：`219 passed, 7 skipped`。
- JavaScript：语法检查通过；`node --test` 10/10 通过。
- Shell：三个部署脚本 `bash -n` 均通过。
- `git diff --check` 通过。
- 7 个跳过项是 5 个 Linux kiosk 运行时测试、1 个系统 PATH 无 Node 的 pytest 包装测试、1 个需显式启用的 Playwright 几何测试；独立使用内置 Node 的测试已通过。

完整命令、输出、退出码和分类见 `TEST_RESULTS.txt`。

## 已知风险与未做硬件测试

- 当前 Windows 环境未连接真实 Raspberry Pi 摄像头、`/dev/ttyAMA0` 或 MSPM0，未验证电气连接、设备拔插、MCU 复位、实际帧率和现场标定精度。
- `::1` 的配置和脚本语法已测试，但 Raspberry Pi 浏览器、IPv6 回环监听仍需现场验证。
- 当前工作区的广泛删除和清理是任务开始前已经存在的未提交状态；本轮未恢复、覆盖或提交这些改动。
- 真实硬件步骤见 `MANUAL_HARDWARE_TEST.md`。

