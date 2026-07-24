# MSPM0 VMC-Link v1 参考解析器

`vmc_link.c/.h` 是不依赖具体 HAL、无 `malloc` 的标准 C 实现。将两个文件加入
MSPM0 工程，在 UART 接收中断或 DMA 字节处理循环中逐字节调用：

```c
static vmc_link_parser_t parser;
static vmc_link_result_t latest_result;

void vision_uart_init(void) {
    vmc_link_parser_init(&parser);
}

void vision_uart_rx_byte(uint8_t byte) {
    if (vmc_link_feed_byte(&parser, byte, &latest_result)) {
        /* CRC 已通过；在主循环中消费 latest_result。 */
    }
}
```

DMA 接收时，对本次新增的每个字节依次调用 `vmc_link_feed_byte()` 即可。解析器内部
缓存固定为34字节，支持半包、连续包、帧头搜索、CRC错误和重新同步。

## 电气连接

- 树莓派 TX 接 MSPM0 RX；树莓派 RX 接 MSPM0 TX，TX/RX 必须交叉。
- 两块板必须共地（GND连接GND）。
- 使用3.3V UART逻辑，不要把5V串口电平直接连接到任一设备。
- 两端波特率、数据位、停止位和校验设置必须一致；推荐115200、8-N-1。
