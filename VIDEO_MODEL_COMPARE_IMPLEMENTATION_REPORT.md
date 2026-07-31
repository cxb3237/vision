# 钢球双模型同视频 A/B 对比工具实现报告

## 1. 文件变更

新增：

- `tools/compare_steel_ball_models_video.py`
- `tests/test_compare_steel_ball_models_video.py`
- `VIDEO_MODEL_COMPARE_IMPLEMENTATION_REPORT.md`

修改：

- `docs/STEEL_BALL_MODELS.md`
- `README.md`
- `.gitignore`

保留了任务开始前已存在、未跟踪的 `MODEL_INTEGRATION_REPORT.md`，没有改动它。

## 2. 实际 Detector 接口

配置由 `core.config_loader.load_steel_ball_ncnn_config(path)` 加载。工具实例化 `SteelBallYoloNcnnDetector(config)`，调用 `initialize()`，并检查 `model_loaded` 与 `detector_error`。单帧通过 `process(FramePacket(frame_id, capture_timestamp, image))` 推理；结束时始终调用 `close()`。

正式 Detector 内部调用 `SteelBallNcnnRuntime.predict()`，复用既有 letterbox、NCNN 推理、置信度过滤、NMS、坐标还原和 `select_primary_detection()` 多框目标选择逻辑。本工具没有复制或修改推理数学与目标筛选规则。

## 3. 单帧字段映射

- 有无输出：`VisionResult.found`
- 类别：正式 Detector 按配置写入 `VisionResult.target_class`；比较工具只比较最终钢球目标，不另做类别筛选
- 框：`bbox_x`、`bbox_y`、`bbox_width`、`bbox_height`，CSV 的 `x2/y2` 分别由 `x1 + width`、`y1 + height` 得到
- 中心：`center_x`、`center_y`
- 置信度：`VisionResult.confidence / 1000.0`
- 推理耗时：`detector.get_runtime_status()["inference_ms"]`
- 无输出：`detected=0`，坐标、中心、框尺寸和置信度字段留空；耗时仍保留

## 4. 视频处理方式

工具先验证视频宽高、标称 FPS 和首个有效帧，再按 `--order` 串行执行两遍。每遍都独立初始化一个模型、重复首个有效帧完成预热、重新打开源视频、流式解码和推理，随后立即释放 Detector。只保存逐帧统计字典，不保存原始视频帧，也不以多进程同时加载两套 NCNN 模型。

两遍必须得到完全相同的处理帧索引、视频元数据和实际解码帧数，否则任务失败。标注视频在两遍推理完成后第三次流式解码生成，因此可以使用最终运动分组，同时仍不把视频加载进内存。

## 5. 运动等级与重复帧

每个解码帧缩小为 64x36 灰度图，使用相邻帧平均绝对差作为 `motion_score`。分数只在第一遍计算一次，再共享给另一模型。非零分数的 P10、P25、P65 自适应形成 near-duplicate、static 和 slow/fast 边界；全零和大量重复值有显式退化处理。首帧没有前帧，不计为近重复帧。

该值只是画面运动强度代理，不是钢球物理速度。近重复帧仍由两个模型正常处理，只在报告中单独统计。

## 6. 连续无检测输出区间

对每个模型线性扫描 `detected` 序列，将相邻无输出行组成区间。记录处理帧数、源视频起止帧、起止时间、按采样时间间隔计算的持续毫秒数、平均 motion_score 和 static/slow/fast 数量。结果按区间长度降序排列，报告列出前 5 段，并明确称为“连续无检测输出区间”，不将其当作有真值的漏检结论。

## 7. 输出文件

- `frames.csv`：逐帧对齐数据、运动分组、两模型结果和同帧差异
- `summary.json`：输入哈希/元数据、阈值、完整模型指标、同帧指标、参数、版本、Git 信息和实际视频输出
- `report.md`：中文可审阅报告及结论限制
- `run_manifest.json`：输出目录内其他文件的相对路径、大小和 SHA256（manifest 不自哈希）
- `baseline_annotated.mp4` 或 `.avi`
- `candidate_annotated.mp4` 或 `.avi`
- `comparison_side_by_side.mp4` 或 `.avi`

VideoWriter 每次均检查 `isOpened()`，先尝试 `mp4v` MP4，失败后尝试 `MJPG` AVI，两种都失败时返回明确错误。Ctrl+C 会通过 `finally` 释放 Capture、Writer、Detector；若中断发生在模型推理遍，会保留 `<模型>_frames.partial.csv`，不生成正式 summary/report。

## 8. 测试命令与范围

```text
python -m compileall tools/compare_steel_ball_models_video.py
python -m pytest -p no:cacheprovider tests/test_compare_steel_ball_models_video.py -q
python -m pytest -p no:cacheprovider tests/test_config_loader.py tests/test_steel_ball_ncnn_runtime.py tests/test_steel_ball_yolo_ncnn_detector.py -q
git diff --check
```

新增测试使用临时目录、MJPG 合成短视频、FakeDetector 和固定输出，不加载真实 NCNN 模型。覆盖同帧对齐、CSV 字段、连续区间、单方输出计数、运动阈值、JSON、无效视频、VideoWriter 失败、max_frames、frame_step、反向加载顺序、三路视频和 Ctrl+C 临时 CSV。

## 9. Raspberry Pi 实际运行命令

```bash
cd /home/clb/Desktop/camera
.venv/bin/python tools/compare_steel_ball_models_video.py \
  --video "/实际录像路径/compare_source.mp4" \
  --write-videos
```

结果默认写入 `runs/model_compare/`。正式使用前应确认树莓派 OpenCV 的 `mp4v` 或 MJPG 编码支持，运行后人工查看 `comparison_side_by_side` 视频。

## 10. Git 与安全说明

任务开始时 `git status --short` 仅有用户已有的 `?? MODEL_INTEGRATION_REPORT.md`。本实现没有修改 `config/steel_ball_ncnn.yaml`、两套模型文件、Runtime 推理逻辑或摄像头参数；没有启动摄像头、串口、网页服务或 systemd；没有执行 commit、push、reset、clean、restore。

## 11. 最终验证结果

- `python -m compileall tools/compare_steel_ball_models_video.py`：通过。
- 新增测试：`10 passed in 1.13s`。
- 现有相关测试：`35 passed in 0.27s`。
- `git diff --check`：通过。
- 最终 `git status --short`：3 个跟踪文件修改，3 个本任务新增文件，以及任务开始前已有的未跟踪 `MODEL_INTEGRATION_REPORT.md`；详见最终回复。
