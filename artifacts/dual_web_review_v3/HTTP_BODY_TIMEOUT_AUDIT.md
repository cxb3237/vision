# HTTP 请求体超时审计

比赛 HTTP 处理器仅在 POST 请求体读取期间临时设置约 2 秒 socket timeout，并在 finally 中恢复旧值，因此 GET、HEAD 和 MJPEG 长连接不受短超时影响。Content-Length 必须为整数，负数返回 400，超过 65536 返回 413；慢请求返回 408，不完整读取返回 400，二者关闭连接。非法 JSON 返回 400，合法空正文和 `{}` 均解析为空对象。响应只返回固定的公共错误文本，不泄露 Python 异常。
