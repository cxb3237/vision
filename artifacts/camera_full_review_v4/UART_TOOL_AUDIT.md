# UART 独立工具审计

实现位置：`tools/test_ball_uart.py`；测试：`tests/test_ball_uart_tool.py`。

| 命令 | 必须收到的成功回复 |
|---|---|
| ping | `OK C=BALL_PING` |
| status | 以 `BALL S=` 开头的状态行；之前收到的 OK 或其他行不算成功 |
| start | `OK C=BALL_START` |
| stop | `OK C=BALL_STOP` |
| pos | `OK P` |
| invalid | `OK I` |
| monitor | 持续打印，不校验预期回复；Ctrl+C 返回 0 |

每次发送输出 `TX: ...`，每行接收输出 `RX: ...`。收到预期回复立即返回 0；超时输出 `RX TIMEOUT: expected ...` 并返回 3；收到 `ERR C=...` 立即返回 4；参数错误返回 2；串口/pyserial 错误返回 5。`pos` 通过 argparse 整数解析和 `encode_position` 双重限制为整数 `-125..125`。

测试覆盖所有命令成功、无回复、ERR、status 缺少状态行、POS 越界/浮点和串口打开失败。

