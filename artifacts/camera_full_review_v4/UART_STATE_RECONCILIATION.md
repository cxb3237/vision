# UART STATUS 意图校正

实现位置：`drivers/ball_uart_client.py`，类 `BallUartClient`，方法 `_reconcile_enabled_state`、`send_start`、`send_stop`、`_reconcile_control_ack`。

| desired_running | MCU EN | 本地状态/动作 |
|---|---:|---|
| true | 1 | 确认 RUNNING；删除冗余 START/STOP，不重复发送 START。 |
| true | 0 | 标记 START_REQUESTED；删除过期 STOP；高优先级去重排入 START。 |
| false | 0 | 确认 STOPPED；删除冗余 START/STOP，不重复发送 STOP。 |
| false | 1 | 保持最终停止意图；删除过期 START和待发 POS/INVALID；最高优先级去重排入 STOP。 |

## 限频与去重

- `control_reconcile_interval_s` 默认 0.5 秒，仅限制 STATUS 自动纠正，不限制用户主动 START/STOP。
- `_last_control_reconcile_at` 分别记录 START 和 STOP 最近纠正排队时间。
- 若相同纠正命令已经在队列中，只保留一个，不更新限频时间，也不会被后续错误 STATUS 误删。
- 用户意图反转时，`send_start`/`send_stop` 在同一 `_outbound_lock` 内删除全部旧 START/STOP，再插入唯一最终命令。
- STOP 自动纠正和用户 STOP 都清除 latest-only 位置槽。
- READY 门控和原有 latest-only 发送策略不变。

## 测试证据

- `test_status_desired_running_true_enabled_zero_queues_one_start`
- `test_status_desired_running_true_enabled_one_does_not_repeat_start`
- `test_status_desired_running_false_enabled_one_queues_stop_and_clears_position`
- `test_status_desired_running_false_enabled_zero_does_not_repeat_stop`
- `test_wrong_status_is_rate_limited_and_can_retry_after_interval`
- `test_user_intent_reversal_removes_old_status_correction`
- `test_status_and_late_acks_never_override_final_intent`
- 原有 START/STOP 最终意图、写失败反转、关闭 STOP Event 竞态测试继续通过。

