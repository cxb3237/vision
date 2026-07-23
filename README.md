# 全国大学生电子设计竞赛小车视觉模块

本工程面向 Raspberry Pi 4B 和普通 Windows/Linux 开发机，使用 USB UVC 摄像头完成
HSV 颜色检测、传统几何形状检测、目标时序跟踪、标定、录像和离线回放。可选的
VMC-Link V1.0 串口链路向 MSPM0G3507 发送视觉结果；MSPM0 始终拥有电机闭环和最终控制权。

当前不包含云台/GPIO 控制、云平台、神经网络数字识别或单目测距。`RECOGNIZE`、`MEASURE`、
`AIM` 和 `RETURN_CENTER` 控制请求会返回 `UNSUPPORTED`。`CALIBRATION` 是不发送普通
`VISION_TARGET` 的被动模式。正常视觉结果只来自 `SEARCH` 和 `TRACK`。

## 目录

- `core/`：共享模型、YAML 配置校验、视觉模式和线程安全故障位。
- `drivers/`：只保留最新帧的摄像头线程和有限队列串口线程。
- `detectors/`：HSV 颜色检测、传统形状检测和目标跟踪器。
- `protocol/`：CRC-16/CCITT-FALSE、VMC-Link 消息和流式解析器。
- `tools/`：探测、去重录制、回放、HSV 调参、标定、去畸变和模拟器。
- `config/`：摄像头、颜色、形状、任务和标定参数。
- `tests/`：只使用合成数据和 fake 对象，不访问真实硬件。

## 安装

Python 版本要求 3.10 或更高。

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / Raspberry Pi OS
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

无桌面 Raspberry Pi OS 可以把 `opencv-python` 替换为 `opencv-python-headless`，不要同时安装
二者。可使用 `ls /dev/video*` 或 `v4l2-ctl --list-devices` 查找摄像头设备。

## 常用命令

```bash
# 到 10 秒自动退出；--seconds 0 表示一直运行到 q 或 Ctrl+C
python -m tools.camera_probe --device 0 --width 640 --height 480 --fps 30 --seconds 10

# 录制视频或按最小时间间隔保存图片；两种模式都会按 frame_id 去重
python -m tools.record_dataset --output data/recordings/test.mp4 --seconds 30 --startup-timeout 5 --frame-timeout 2
python -m tools.record_dataset --images data/images --interval 0.5 --max-frames 100

# GUI 实时调参；--device 持续读摄像头，--image 使用静态图片
python -m tools.hsv_tuner --device 0 --camera-config config/camera.yaml --color red --range-index 0
python -m tools.hsv_tuner --image data/samples/test.jpg --color red --range-index 1

# --speed 1 是原速，0 是最快处理；空格或 p 暂停，n 单帧前进
python -m tools.replay_test --input data/recordings/test.mp4 --detector color --target red --speed 1 --display

# 无串口运行；视频源不循环时会在文件结束后自动退出
python app.py --mode search --detector color --target red --no-serial
python app.py --mode track --video data/recordings/test.mp4 --display --no-serial

# 标定要求默认至少 8 张有效、同分辨率棋盘图
python -m tools.capture_calibration --device 0 --camera-config config/camera.yaml --output-dir data/calibration/images --cols 9 --rows 6 --max-images 25
python -m tools.calibrate_camera --images data/calibration/images --cols 9 --rows 6 --square-size-mm 25
python -m tools.undistort_test --input data/samples/test.jpg --alpha 0 --display

# MSPM0 控制台或串口模拟
python -m tools.mock_mspm0 --console
python -m tools.mock_mspm0 --port loop:// --mode track
```

所有命令的当前参数以各自的 `--help` 为准。

### 交互式标定图片采集

`tools.capture_calibration` 复用正式主程序的 `CameraService` 和 `camera.yaml`，因此分辨率、
FOURCC、FPS、曝光、增益、白平衡及缓冲设置与 `app.py` 一致。示例：

```bash
python -m tools.capture_calibration \
  --device 0 \
  --camera-config config/camera.yaml \
  --output-dir data/calibration/images \
  --cols 9 \
  --rows 6 \
  --max-images 25
```

按键说明：

- `Space` 或 `S`：保存通过完整角点、清晰度、棋盘面积和重复帧检查的图片。
- `D`：删除本次运行期间刚保存的最后一张；不会删除启动前已有图片。
- `Q`：退出并释放摄像头。

默认清晰度阈值为拉普拉斯方差 80，棋盘角点外接矩形最小占比为 8%。可通过
`--min-blur` 和 `--min-board-area-ratio` 调整。`--force-save` 只跳过清晰度和面积阈值，
仍要求找到全部内部角点并拒绝重复 `frame_id`。图片按 `calib_0001.jpg` 连续编号，详细记录
增量写入同目录的 `metadata.jsonl`。达到 `--max-images` 后程序只提示数量足够，不会自动退出。

## 摄像头和串口生命周期

`CameraService` 的采集线程是唯一创建、读取、重连和释放 `VideoCapture` 的位置；`stop()`
只发停止事件并等待线程退出。`SerialService` 同样由单一 I/O 线程拥有串口句柄，主线程发送
只进入有限队列，因此停止时不会关闭一个仍在后台读取的句柄。

OpenCV 属性编号 14 是 `CAP_PROP_GAIN`。部分 UVC 摄像头不支持手动增益，默认配置使用
`gain: null` 完全跳过该属性，避免反复产生“不支持参数 14”的警告。`exposure`、
`brightness` 和 `contrast` 也可以设为 `null`。

串口默认关闭，默认端口为 `/dev/serial0`。可在 `config/mission.yaml` 启用，使用 `--serial`
显式启用，或使用 `--serial-port` 覆盖端口并同时启用；`--no-serial` 优先级最高，会确保程序
完全不访问串口硬件。端口打开只表示 `port_open`，程序依据最近收到的有效对端包判断
`peer_alive` 和 `SERIAL_LINK_DOWN`。

串口发送将 ACK/URGENT 放入关键队列，普通消息批量发送，流式 `VISION_TARGET` 只保留最新包。
`VISION_CONTROL` 只有带 `ACK_REQ` 时才要求回复。重复的 `SEQ + request_id` 返回缓存结果，不会
再次切换模式或重置 Tracker。

## VMC-Link V1.0 冻结负载

| 负载 | 格式 | 字节数 |
| --- | --- | ---: |
| `VISION_TARGET` | `<IBBHhhhhHHHHH>` | 26 |
| `HEARTBEAT` | `<IBBHHH>` | 12 |
| `ACK` | `<BBBB>` | 4 |
| `VISION_CONTROL` | `<HBBHhhH>` | 12 |

`ACK` 字段依次是 `acked_type`、`acked_seq`、`result`、`detail`。`VISION_CONTROL` 字段依次是
`request_id uint16`、`mode uint8`、`options uint8`、`target_class uint16`、`param1 int16`、
`param2 int16`、`timeout_ms uint16`。

稳定颜色类别为：UNKNOWN=0、RED=1、GREEN=2、BLUE=3、YELLOW=4、BLACK=5、WHITE=6。
应用中的 SEARCH 和 TRACK 均由 `TargetTracker` 统一负责确认、丢失、跳变和位置平滑。

录制元数据使用增量 `metadata.jsonl`，避免长时间录制把全部记录保留在内存。每帧分别记录
容器 FPS 和实时采集 FPS；两者含义不同。视频帧尺寸在录制过程中发生变化会立即报错停止。

## 建议调试顺序

```bash
python -m pytest -q
python -m tools.camera_probe --device 0 --seconds 10
python -m tools.record_dataset --output data/recordings/test.mp4 --seconds 10
python -m tools.hsv_tuner --device 0 --color red
python -m tools.replay_test --input data/recordings/test.mp4 --detector color --target red --speed 1 --display
python app.py --mode track --video data/recordings/test.mp4 --display --no-serial
python -m tools.mock_mspm0 --console
# 最后再在确认串口权限与接线后启用真实 MSPM0 串口
```

主程序显示模式支持：`q` 退出、`s` 保存当前调试帧、`i` 切换 IDLE、`t` 切换 TRACK。
真实硬件上的最终分辨率、FPS、FOURCC、曝光支持情况和串口稳定性仍需在 Raspberry Pi 上验证。
