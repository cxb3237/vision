# 全国大学生电子设计竞赛小车视觉模块

本工程面向 Raspberry Pi 4B 和普通 Windows/Linux 开发机，使用 USB UVC 摄像头完成
HSV 颜色检测、传统几何形状检测、单个印刷数字识别、直径 10 mm 钢球检测、目标时序跟踪、标定、录像和离线回放。可选的
VMC-Link V1.0 串口链路向 MSPM0G3507 发送视觉结果；MSPM0 始终拥有电机闭环和最终控制权。

当前不包含云台/GPIO 控制、云平台、神经网络数字识别或单目测距。`RECOGNIZE`、`MEASURE`、
`AIM` 和 `RETURN_CENTER` 控制请求会返回 `UNSUPPORTED`。`CALIBRATION` 是不发送普通
`VISION_TARGET` 的被动模式。正常视觉结果只来自 `SEARCH` 和 `TRACK`。

## 目录

- `core/`：共享模型、YAML 配置校验、视觉模式和线程安全故障位。
- `drivers/`：只保留最新帧的摄像头线程和有限队列串口线程。
- `detectors/`：HSV 颜色、传统形状、钢球检测和目标跟踪器。
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

Linux / Raspberry Pi 需要安装 V4L2 控制工具，才能自动应用白平衡、亮度等硬件参数：

```bash
sudo apt install v4l-utils
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

# 直径 10 mm 钢球实时检测、调参和离线回放
python app.py --mode track --detector steel_ball --display --no-serial
python -m tools.steel_ball_tuner --device 0 --config config/steel_ball.yaml
python -m tools.replay_test --input data/recordings/steel_ball.mp4 --detector steel_ball --display

# 无串口运行；视频源不循环时会在文件结束后自动退出
python app.py --mode search --detector color --target red --no-serial
python app.py --mode track --video data/recordings/test.mp4 --display --no-serial

# 标定要求默认至少 8 张有效、同分辨率棋盘图
python -m tools.capture_calibration --device 0 --camera-config config/camera.yaml --output-dir data/calibration/images --cols 9 --rows 6 --max-images 25
python -m tools.calibrate_camera --images data/calibration/images --cols 9 --rows 6 --square-size-mm 24 --visualization-dir data/calibration/visualized
python -m tools.undistort_test --input data/samples/test.jpg --alpha 0 --frame-timeout 5 --display

# MSPM0 控制台或串口模拟
python -m tools.mock_mspm0 --console
python -m tools.mock_mspm0 --port loop:// --mode track
```

所有命令的当前参数以各自的 `--help` 为准。

### 单个印刷数字 0～9 识别

数字检测首版面向白色或浅色背景上的单个黑色印刷数字，不依赖 Tesseract 或大型神经网络。
`DigitDetector` 会执行候选提取、保持比例归一化、0～9 多模板 IoU/相关系数匹配、分差拒识和
最近多帧投票。识别结果类别为数字 0～9 对应的 `100～109`，未知数字为 `0`。

1. 分别采集 0～9 模板。按数字键选择标签，`Space/S` 保存，`D` 删除本次最近保存，`Q` 退出。
每个数字建议至少采集 10 张，覆盖轻微位置、距离、笔画和光照变化：

```bash
python3 -m tools.capture_digit_templates \
  --device 0 \
  --camera-config config/camera.yaml \
  --digit-config config/digit.yaml \
  --output-root data/digits/templates
```

采集模式允许模板目录暂时为空，并会先创建 0～9 子目录；正式 `app.py`、离线回放和调参工具则
要求 0～9 每类至少有一张可读模板，缺少时会明确列出对应数字，避免静默运行在不可识别状态。

2. 调节阈值、CLAHE、形态学、面积/高度/宽高比、最低分数和最低分差。按 `S` 原子保存，
按 `R` 从磁盘重载并同步滑动条，按 `Q` 退出：

```bash
python3 -m tools.digit_tuner \
  --device 0 \
  --camera-config config/camera.yaml \
  --digit-config config/digit.yaml
```

3. 实时识别：

```bash
python3 app.py \
  --mode track \
  --detector digit \
  --digit-config config/digit.yaml \
  --no-serial \
  --display
```

4. 先录像以便复现现场问题：

```bash
python3 -m tools.record_dataset \
  --output data/recordings/digit_demo.mp4 \
  --seconds 30
```

5. 离线回放相同检测器和模板：

```bash
python3 -m tools.replay_test \
  --input data/recordings/digit_demo.mp4 \
  --detector digit \
  --digit-config config/digit.yaml \
  --speed 1 \
  --display
```

常见问题：候选找不到时先查看二值掩膜并放宽面积、高度或宽高比；`6/9`、`1/7` 混淆时应增加
对应字体与拍摄姿态的模板，并提高 `min_score_margin`。光照变化优先使用稳定漫射光并开启 CLAHE；
数字倾斜时补采同角度模板，首版不会自动做透视矫正。每类模板不足 10 张时采集工具会持续提示，
模板过少通常比单纯降低匹配阈值更容易造成误识别。

### Linux V4L2 摄像头参数

`config/camera.yaml` 的可选 `v4l2_controls` 段用于设置白平衡、工频、背光补偿、亮度、对比度、
饱和度、色调、Gamma 和锐度。`enabled: false` 会完全跳过；值为 `null` 的单项不会设置；
`strict: false` 只警告不支持的项目，`strict: true` 会让严格设置失败终止本次采集启动。

可先应用配置并回读摄像头实际值：

```bash
python3 -m tools.camera_profile_check \
  --device 0 \
  --camera-config config/camera.yaml \
  --apply
```

摄像头拔插或系统重启后，部分 V4L2 参数可能恢复默认值。因此正式程序的 `CameraService` 会在
第一次打开摄像头、断线重连以及同一服务对象重新启动时自动重新应用配置。Windows 和 macOS
会安全跳过 V4L2 命令，原有 OpenCV 分辨率、FPS、FOURCC、曝光等设置仍会继续执行。
当 V4L2 中启用了 gain、brightness、contrast 或自动白平衡控制时，这些项目以 V4L2 为唯一
权威来源，OpenCV 不会再次写入同一属性；最终回读值不一致时会按 `strict` 选择警告或失败。

### 直径 10 mm 钢球检测

`SteelBallDetector` 使用 `config/steel_ball.yaml` 配置 ROI、CLAHE、滤波、固定/自适应阈值、
正反二值化、形态学、像素直径、面积、圆度、宽高比和可选 Hough 圆复核。它自行维护
`CANDIDATE`、`LOCKED`、`OCCLUDED`、`LOST` 和远处重新捕获状态，不直接访问摄像头或串口。

实时检测：

```bash
python app.py --mode track --detector steel_ball --display --no-serial
```

实时调参会复用 `CameraService`，收到第一张有效帧并确认实际分辨率后才创建控制窗口，显示原图、
增强灰度图、二值掩膜、最终候选，以及面积、直径、圆度、宽高比和 Hough 各类拒绝统计：

```bash
python -m tools.steel_ball_tuner \
  --device 0 \
  --camera-config config/camera.yaml \
  --config config/steel_ball.yaml \
  --calibration-config config/calibration.yaml
```

滑动条包括阈值/自适应块大小与 C、Gaussian 和开闭运算、CLAHE 开关/clip/tile、面积与直径范围、
圆度与宽高比范围、Hough 参数、最大跳变、确认/丢失帧数及 ROI 开关和矩形范围。按 `S` 将全部
字段原子保存到 `config/steel_ball.yaml`，按 `R` 从磁盘重载配置并同步所有滑动条，按 `Q` 退出。

建议先使用 10 mm 钢球、深色哑光背景和柔和漫射光，在 20～50 cm 距离开始调节。初次调参先
关闭 Hough，待轮廓检测稳定后再开启复核，避免高光和阴影掩盖真正的过滤原因。

离线回放：

```bash
python -m tools.replay_test \
  --input data/recordings/steel_ball.mp4 \
  --detector steel_ball \
  --steel-ball-config config/steel_ball.yaml \
  --calibration-config config/calibration.yaml \
  --display
```

当 `calibration.yaml` 已标定且焦距 `fx` 有效时，距离按
`fx × known_diameter_mm / diameter_px` 估算；未标定或像素直径无效时协议值为 `0xFFFF`。

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

### 相机标定与去畸变

下面的标定命令兼容 OpenCV 5.0，会将角点统一成相同的二维 `float64` 形状后计算单点 RMS
重投影误差：

```bash
python3 -m tools.calibrate_camera \
  --images data/calibration/images \
  --cols 9 \
  --rows 6 \
  --square-size-mm 24 \
  --visualization-dir data/calibration/visualized
```

实时去畸变示例：

```bash
python3 -m tools.undistort_test \
  --device 0 \
  --config config/calibration.yaml \
  --camera-config config/camera.yaml \
  --alpha 0 \
  --frame-timeout 5 \
  --display
```

带 `--display` 时窗口会一直运行到按 `Q`。`--frame-timeout` 只在连续指定秒数没有收到新的
`frame_id` 时触发；收到新帧就重新计时。输入或摄像头分辨率必须与标定分辨率一致。

`config/calibration.example.yaml` 是受版本控制的未标定模板；真实标定结果写入本地
`config/calibration.yaml`，该文件被 Git 忽略，因此拉取代码不会覆盖本机参数。更换摄像头、焦距、
分辨率或安装结构后必须重新标定。若本地文件尚不存在，配置加载器会自动读取模板；显式指定的
其他配置文件不存在时仍会报错。

## 摄像头和串口生命周期

`CameraService` 的采集线程是唯一创建、读取、重连和释放 `VideoCapture` 的位置；`stop()`
只发停止事件并等待线程退出。`SerialService` 同样由单一 I/O 线程拥有串口句柄，主线程发送
只进入有限队列，因此停止时不会关闭一个仍在后台读取的句柄。

OpenCV 属性编号 14 是 `CAP_PROP_GAIN`。部分 UVC 摄像头不支持手动增益，默认配置使用
`gain: null` 完全跳过该属性，避免反复产生“不支持参数 14”的警告。`exposure`、
`brightness` 和 `contrast` 也可以设为 `null`。

串口默认关闭，默认端口为 `/dev/serial0`。可在 `config/mission.yaml` 启用，使用 `--serial`
显式启用，或使用 `--serial-port` 覆盖端口并同时启用；`--no-serial` 优先级最高，会确保程序
完全不访问串口硬件。`--baudrate`、`--serial-rate` 和 `--serial-debug` 分别覆盖波特率、固定
结果包发送频率和十六进制调试日志。视频文件回放默认禁用真实串口，只有显式传入 `--serial`
或 `--serial-port` 才会启用。端口打开只表示 `port_open`，程序依据最近收到的有效对端包判断
`peer_alive` 和 `SERIAL_LINK_DOWN`。

串口发送将 ACK/URGENT 放入关键队列，普通消息批量发送；VMC-Link v1固定结果通道只保留最新
一帧，并由串口线程按默认20Hz发送，因此摄像头和检测主循环不会等待串口写操作。
`VISION_CONTROL` 只有带 `ACK_REQ` 时才要求回复。重复的 `SEQ + request_id` 返回缓存结果，不会
再次切换模式或重置 Tracker。

## VMC-Link v1 固定视觉结果协议

树莓派向MSPM0发送的视觉结果固定为34字节。所有多字节整数均为小端；CRC使用
CRC-16/CCITT-FALSE（多项式`0x1021`、初值`0xFFFF`、xorout `0x0000`），计算范围为偏移2的
`version`至偏移31的`flags`，不包括帧头和CRC字段。

| 偏移 | 长度 | 类型 | 字段 | 约定 |
| ---: | ---: | --- | --- | --- |
| 0 | 1 | uint8 | SOF1 | `0xAA` |
| 1 | 1 | uint8 | SOF2 | `0x55` |
| 2 | 1 | uint8 | version | `1` |
| 3 | 1 | uint8 | msg_type | `0x01` |
| 4 | 1 | uint8 | payload_length | `27` |
| 5 | 2 | uint16 | sequence | 每个实际发送包加1，`65535→0` |
| 7 | 4 | uint32 | timestamp_ms | 采集时间毫秒 |
| 11 | 1 | uint8 | detector_id | none/color/shape/steel_ball/digit=`0/1/2/3/4` |
| 12 | 1 | uint8 | state | NONE/CANDIDATE/LOCKED/OCCLUDED/LOST=`0/1/2/3/4` |
| 13 | 2 | uint16 | target_class | 沿用检测器类别；数字为`100～109` |
| 15 | 2 | int16 | center_x_px | 无目标为`-1` |
| 17 | 2 | int16 | center_y_px | 无目标为`-1` |
| 19 | 2 | int16 | error_x_permille | 相对画面中心，裁剪至`-1000～1000` |
| 21 | 2 | int16 | error_y_permille | 下正上负，裁剪至`-1000～1000` |
| 23 | 2 | uint16 | bbox_width_px | 无目标为`0` |
| 25 | 2 | uint16 | bbox_height_px | 无目标为`0` |
| 27 | 2 | uint16 | confidence_permille | `0～1000` |
| 29 | 2 | uint16 | distance_mm | 未知为`65535` |
| 31 | 1 | uint8 | flags | bit0发现、bit1锁定、bit2距离有效、bit3已标定 |
| 32 | 2 | uint16 | crc16 | 小端CRC |

项目内部`TargetState`的枚举顺序保持不变，编码层会显式转换为上述线上状态。未发现目标时仍会
发送对应的NONE、OCCLUDED或LOST状态，同时清零类别、bbox、置信度和误差。

`config/mission.yaml`中的串口相关配置为：

```yaml
serial_enabled: false
serial_port: /dev/serial0
serial_baudrate: 115200
serial_send_rate_hz: 20
serial_reconnect_interval_s: 1.0
serial_queue_size: 64
serial_strict: false
```

`serial_queue_size`只用于双向控制、ACK和普通消息的收发队列。固定视觉结果不进入这些FIFO；
它始终通过`_latest_result`单槽只保存最新一帧，因此即使MSPM0暂时读取变慢，也不会积压旧结果
或拖慢检测线程。

实时发送示例：

```bash
python3 app.py --mode track --detector color --target red \
  --serial-port /dev/ttyUSB0 --baudrate 115200 --serial-rate 20
```

### 串口连接与调试

USB转串口适配器通常显示为`/dev/ttyUSB0`或`/dev/ttyACM0`；可用`ls -l /dev/serial/by-id/`
找到更稳定的设备名。GPIO UART可使用树莓派物理引脚8（GPIO14/TXD）连接MSPM0 RX，物理引脚10
（GPIO15/RXD）连接MSPM0 TX，物理引脚6连接GND。TX/RX必须交叉、两板必须共地，并且只能使用
3.3V逻辑电平。可通过`sudo raspi-config`启用UART并关闭串口登录控制台。

协议自检和监视命令：

```bash
python3 -m tools.vmc_link_selftest
python3 -m tools.serial_monitor --simulate
python3 -m tools.serial_monitor --port /dev/ttyUSB0 --baudrate 115200
```

MSPM0端可直接移植`mcu_reference/vmc_link.c`和`vmc_link.h`。该解析器无动态内存，适合在UART
接收中断中逐字节调用，或在DMA回调中遍历新增字节；接线与调用示例见
`mcu_reference/README.md`。

常见问题：

- 权限不足：执行`sudo usermod -aG dialout $USER`后重新登录，不要长期用root绕过权限。
- 设备名变化：优先使用`/dev/serial/by-id/`链接，或配置udev固定名称。
- CRC错误或乱码：确认两端都是115200、8-N-1，且没有把文本日志写入同一UART。
- 持续收不到包：确认TX/RX已经交叉、两端共地、UART已启用且使用3.3V电平。
- 偶发断开：查看供电和USB线，服务会按`serial_reconnect_interval_s`自动重连。

## 兼容的双向控制负载

工程原有的可变长控制帧接口继续保留，用于心跳、ACK和`VISION_CONTROL`，不会影响固定34字节
视觉结果编码器和解析器：

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

## 触摸屏现场调试界面

第一版触摸界面是视觉进程内的本地Web服务。`VisionRuntime`独占一个`CameraService`、当前检测器
和VMC-Link串口服务；网页线程只能读取不可变状态快照、读取最新JPEG缓存以及向有界命令队列
投递请求，不会创建第二个摄像头或直接执行V4L2操作。前端为项目内的原生HTML/CSS/JavaScript，
无CDN、Node.js、数据库或互联网依赖。

界面采用固定`100dvh`的一屏布局：横屏左侧始终显示完整、等比例的摄像头预览，右侧为紧凑状态
与参数side dock；竖屏时预览在上、dock在下。页面主体不滚动，参数列表只在dock内部滚动。参数
把手位于dock固定标题栏的独立48×48按钮槽内，点击或向左/向右拖动超过约40%可打开/关闭抽屉；
打开和关闭时都不会覆盖状态文字、参数按钮或摄像头有效区域。

### 安装与手动启动

在Raspberry Pi 5项目目录内创建虚拟环境并安装已有依赖：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
sudo apt install v4l-utils chromium curl
```

GPIO UART使用`/dev/ttyAMA0`时，推荐启动命令为：

```bash
python3 app.py \
  --mode track \
  --detector digit \
  --digit-config config/digit.yaml \
  --serial-port /dev/ttyAMA0 \
  --baudrate 115200 \
  --serial-rate 20 \
  --touch-ui \
  --headless
```

然后在同机Chromium访问：[http://127.0.0.1:8765](http://127.0.0.1:8765)。`--touch-ui`和
`--display`不能同时使用，`--headless`确保不创建任何OpenCV窗口。默认只监听回环地址；如需局域网
访问，应明确评估现场网络后再使用`--touch-host`覆盖。

### 页面和现场参数

主页显示标注预览、检测器、类别、状态、置信度、中心和误差、FPS、摄像头/串口状态及VMC发送
计数。切换数字、颜色、形状或钢球检测器只替换运行时检测器，不会重启摄像头和串口；新检测器
初始化失败时保留旧检测器。

摄像头参数页根据`v4l2-ctl --list-ctrls-menus`实际结果，只显示当前摄像头支持且可写的控制项；
不支持、只读、当前不可写、范围无效或缺少枚举选项的控制不会占用小屏幕空间。主面板使用中文
名称和中文枚举值，正常状态只显示当前值与加减/滑动控件；仅在应用中、写入失败或回读不一致时
显示紧凑诊断信息。Windows明确显示“当前平台不支持V4L2”。

触摸滑动条采用方向锁定：上下移动优先滚动参数列表，只有明确横向拖动才改变参数，触摸单击轨道
不会跳值；鼠标仍可点击或拖动轨道。加减按钮单击调整一个step，长按400毫秒后每150毫秒连续
精调，松开或取消立即停止。所有修改仍进入原有命令队列，并由视觉线程按自动控制优先顺序调用
现有V4L2模块。自动白平衡开启时隐藏白平衡色温，自动曝光开启时隐藏曝光时间；切换到手动模式并
完成后端回读后，对应参数立即重新显示。

“保存现场参数”把override原子写入`runtime/camera_override.yaml`，UI状态写入
`runtime/touch_ui_state.yaml`，并在`runtime/backups/`保留有限数量备份。它不会覆盖
`config/camera.yaml`、标定文件、检测器配置或数字模板。“恢复上次有效参数”回读override；
“恢复基准参数”会把本次运行实际修改过的全部控制项恢复：启动时先保存摄像头所有受支持控制的
实际值，再由`config/camera.yaml`中明确配置的值覆盖形成基准。恢复成功后停用override，基础YAML
本身保持不变；因此即使`gain`等控制未写入基础YAML，也能恢复到程序启动时的实际值。

保存、恢复上次有效参数和恢复基准参数都会等待后端命令进入`APPLIED`，再重新读取
`GET /api/config/camera`并完整重建滑动条状态；`requested`、`actual`、范围、步进、支持状态和
`MISMATCH`不会沿用恢复前的浏览器本地对象。命令进入`FAILED`时保留当前界面值并显示后端错误。

比赛模式隐藏全部参数控件并由后端拒绝普通修改请求，摄像头、检测和VMC-Link继续运行。触摸屏需
长按右上角维护按钮约2秒才能打开维护菜单，比赛模式下入口仍保留。退出比赛模式、退出kiosk和
停止视觉服务都需要二次确认；普通点击不会执行危险操作。比赛模式用于防误触，不是身份认证。

### systemd和kiosk自动启动

这些脚本只应在Raspberry Pi Linux执行。Windows测试仅检查模板，不会调用`systemctl`或Chromium。

```bash
sudo bash deploy/install_touch_ui.sh \
  --user "$USER" \
  --project-dir "$(pwd)" \
  --start
```

安装脚本验证`.venv`和`app.py`，从`config/touch_ui.yaml`读取并验证本地host/port，渲染
`vision-touch.service`，把普通用户加入`video`和`dialout`组，启用systemd服务并生成带独立
`VISION_TOUCH_URL`环境变量的XDG桌面自启动文件。kiosk URL只允许`localhost`或`127.0.0.1`，
脚本不会执行YAML中的任意文本。组权限重新登录后生效。kiosk脚本等待配置端口的`/healthz`，
再启动Chromium。Alt+F4或浏览器正常退出后脚本随即正常结束，不会重新打开窗口；只有非零异常
退出才会延迟2秒重试，连续重试最多5次。桌面自动登录涉及现场
安全策略，脚本不会擅自开启，请在Raspberry Pi图形桌面设置中手动启用目标普通用户的自动登录。

`start_kiosk.sh`依次查找Chromium、Chrome和Firefox，并把浏览器PID原子写入
`runtime/kiosk.pid`。Chrome/Chromium额外使用项目专用的`runtime/chrome-profile`，不会复用或
修改用户普通Chrome配置，也能避免PID只指向短暂启动器。维护菜单“退出全屏界面”调用固定的
`POST /api/kiosk/exit`：后端只接受该
PID文件，验证当前用户、浏览器名称、`--kiosk`参数以及项目专用profile或本地URL后发送
`SIGTERM`，客户端不能提交PID或任意命令。显式退出会先原子写入
`runtime/kiosk.exit_requested`，因此脚本不会立即重启浏览器，视觉后端仍继续运行。需要手动重新
打开时执行`deploy/start_kiosk.sh &`，也可重新登录桌面由XDG autostart启动；脚本每次新启动都会
清除上次会话遗留的退出标志。

维护菜单“停止视觉程序”调用无参数的`POST /api/runtime/stop`。响应发回页面后，运行时异步请求
安全停止，由`VisionRuntime.stop`按既有生命周期释放预览线程、串口和唯一CameraService，正常
退出码为0。systemd使用`Restart=on-failure`：崩溃会重启，UI正常停止不会立即重启，系统下次
开机仍会因服务已enable而启动。

systemd模板不写死检测器。启动检测器按“命令行明确值 > `runtime/touch_ui_state.yaml`保存值 >
`config/touch_ui.yaml`的`startup.detector`”选择。最终检测器为`digit`时，安装脚本要求
`data/digits/templates/0`到`9`每类至少有一张能被OpenCV读取的PNG，否则列出缺失数字并拒绝
安装；不会生成虚假模板。数字模板必须在Raspberry Pi现场采集后同步进仓库或最终部署目录。
可单独执行部署检查：

```bash
python3 -m tools.check_digit_templates --detector digit
```

查看与控制：

```bash
systemctl status vision-touch.service
journalctl -u vision-touch.service -f
tail -f logs/touch_ui.log
sudo systemctl stop vision-touch.service
sudo bash deploy/uninstall_touch_ui.sh --user "$USER"
```

轮转日志`logs/touch_ui.log`记录启动停止、检测器切换、参数请求/回读、保存恢复、比赛模式和Web
错误，不逐帧写入。自检命令为：

```bash
python3 -m tools.touch_ui_selftest
```

常见问题：

- 摄像头被占用：停止其他使用`/dev/video*`的程序，确认系统中只有一个视觉服务。
- `/dev/ttyAMA0`权限不足：确认用户属于`dialout`组并重新登录；摄像头需要`video`组。
- 浏览器没有自动打开：确认图形桌面已自动登录，并检查XDG autostart和`start_kiosk.sh`日志。
- 页面有状态但没有画面：检查摄像头在线状态和`logs/touch_ui.log`；占位画面会在重连后自动恢复。
- 摄像头参数不支持：这是设备能力差异，禁用项不会被伪装成设置成功。
- 实际值与请求值不同：摄像头可能量化、拒绝或由自动控制改写；以页面回读实际值为准。
- 触摸坐标方向不正确：在Raspberry Pi桌面/Wayland显示设置中同时校正显示旋转与触摸映射。
- systemd服务循环重启：查看`journalctl -u vision-touch.service`，检查`.venv`、模板和配置。
- 桌面没有自动登录：安装脚本不会修改登录策略，需在图形桌面设置中手工启用。
- VMC-Link串口离线：确认`/dev/ttyAMA0`、波特率、TX/RX交叉、共地和3.3V电平。

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
