# 钢球 NCNN 双模型管理

## 为什么保留双模型

正式 baseline 与待评估 candidate 分目录保存，避免覆盖已验证权重，也让测试结果、模型来源和回退路径都可追踪。当前正式配置始终是 `config/steel_ball_ncnn.yaml`；profile 是可复用快照。

## 正式现场推荐与连续输出

现场推荐 profile 为 `steel_ball_candidate_roi_strict.yaml`：Candidate 模型、`conf_threshold=0.50`、ROI 开启。管道必须贴左红、右蓝端点标记，背景应避免大面积相似红蓝色。ROI 几何短时失效时不回退到整幅图任意检测框，位置估计器只在 300 ms 上限内预测/衰减保持；长期无可靠几何或钢球测量时输出 `BALL INVALID`。

50 Hz UART 输出不改变模型、NMS 或 ROI 几何。视觉线程用 `FramePacket.capture_timestamp` 更新连续估计器，串口唯一 worker 独立按 20 ms 周期采样。详见 [BALL_POSITION_ESTIMATOR.md](BALL_POSITION_ESTIMATOR.md)。

## 公平 A/B 测试

第一轮只允许改变 `model_path`。两份 profile 的 `imgsz`、`conf_threshold`、`iou_threshold`、`max_det`、`num_threads`、`target_class` 完全一致；任务层的 `confirm_frames`、`lost_frames`、`smoothing_alpha` 也不得随模型一起调整。使用相同视频、光照、摄像头参数和统计窗口比较：

- 动态检测率；
- 最长连续漏检帧数；
- 平均置信度；
- P95 推理耗时；
- `capture_to_result` 延迟；
- 误检数量。

## 操作命令

```bash
python tools/switch_steel_ball_model.py validate
python tools/switch_steel_ball_model.py status
python tools/validate_steel_ball_models.py
python tools/switch_steel_ball_model.py candidate
python tools/switch_steel_ball_model.py baseline
```

切换工具先用正式 Runtime 加载目标模型，再通过临时文件和 `os.replace` 原子更新配置。它不运行 `sudo`，也不会自动重启 systemd。部署到树莓派并确认配置后，手动执行：

```bash
sudo systemctl restart vision-touch.service
```

## 回退与晋级

出现加载失败、延迟回退、连续漏检或误检增加时，执行 `python tools/switch_steel_ball_model.py baseline`，检查 `status` 后手动重启服务。切换前配置备份位于被 Git 忽略的 `runtime_backups/`。

candidate 只有在固定数据集和现场测试均优于或不劣于 baseline、指标与模型 SHA256 已归档后，才能通过一次单独、可审查的变更晋级。应使用训练日期或实验编号等稳定名称，不使用“最终版”“绝对最好”等不可追踪命名。

# 双模型同视频离线比较

`tools/compare_steel_ball_models_video.py` 按指定顺序分别加载 baseline 和 candidate，模型不会长期同时驻留内存。每个模型都重新打开同一视频并处理相同源帧索引；第二遍解码数量或索引不同会直接失败。工具不会修改 `config/steel_ball_ncnn.yaml`，也不会访问摄像头、串口或网页服务。

Windows 示例：

```powershell
cd E:\NUEDC\camera
python tools/compare_steel_ball_models_video.py `
  --video "E:\recordings\compare_source.mp4" `
  --write-videos
```

Raspberry Pi 示例：

```bash
cd /home/clb/Desktop/camera
.venv/bin/python tools/compare_steel_ball_models_video.py \
  --video "/实际录像路径/compare_source.mp4" \
  --write-videos
```

完整参数示例：

```bash
python tools/compare_steel_ball_models_video.py \
  --video path/to/compare_source.mp4 \
  --baseline-config config/model_profiles/steel_ball_baseline.yaml \
  --candidate-config config/model_profiles/steel_ball_candidate.yaml \
  --output-dir runs/model_compare/test_001 \
  --warmup 5 \
  --frame-step 1 \
  --max-frames 0 \
  --progress-every 50 \
  --order baseline,candidate \
  --write-videos
```

`--no-write-videos` 只生成 `frames.csv`、`summary.json`、`report.md` 和 `run_manifest.json`。未指定输出目录时，结果位于 `runs/model_compare/<视频名>_<时间戳>/`。启用视频后还会生成 baseline、candidate 单画面标注视频和 `comparison_side_by_side` 双画面视频；MP4 编码器不可用时自动回退到 MJPG AVI。

`motion_score` 是相邻帧缩小灰度图的平均绝对差，仅用于按当前视频分位数分成 static、slow、fast，并不是钢球物理速度。近重复帧只单独统计，不会从两个模型的输入中删除。自动结果反映检测输出连续性和耗时，不能在没有人工标注时解释为准确率、误检率或 mAP；最终必须人工查看双画面视频。

## Candidate Raw 与 Candidate ROI

四套 profile 的用途如下：

- Baseline：`steel_ball_baseline.yaml`，原正式模型，conf=0.40，ROI 默认关闭。
- Candidate Raw：`steel_ball_candidate.yaml`，Candidate 原模型，conf=0.40，未配置 `pipe_roi`，等效于 ROI 关闭。保留它用于隔离阈值和几何过滤带来的变化。
- Candidate ROI：`steel_ball_candidate_roi.yaml`，同一 Candidate 模型，conf=0.40，ROI 开启，用于隔离比较 ROI 效果。
- Candidate ROI Strict：`steel_ball_candidate_roi_strict.yaml`，同一 Candidate 模型，ROI 参数完全相同，仅把 conf 提高到 0.50，用于隔离比较阈值效果。

现有 `PipeMarkerDetector` 在原始 BGR 画面中检测右侧蓝色 A 端点和左侧红色 B 端点。水管走廊在原始图像坐标中计算：检测框中心投影到红蓝端点轴线，投影比例必须位于带端部余量的区间，中心到轴线的垂直距离不得超过走廊半宽。过滤发生在 NCNN 解码、NMS 和原图坐标还原之后，正式目标选择、跟踪与 UART 之前。

走廊优先使用动态比例 `corridor_half_width_ratio: 0.04`：当前实际半宽等于当前有效轴长乘以 0.04；使用保持的旧轴线时，也按该轴线自身长度计算。0.04 来自用户提供的约 10 mm 外半径 / 约 250 mm 标记端点物理跨度，不依赖旧的 496 px 固定跨度。项目资料不能独立确认 20 mm 外径和 250 mm 标记跨度，因此该物理比例标记为“待硬件确认”，现场必须核对。`corridor_half_width_px` 保留为可选固定回退，只有 ratio 未设置或不大于 0 时才使用。

端点当前帧有效时更新轴线；暂时丢失时最多保持 250 ms，超时后 Candidate ROI 默认不输出钢球目标。ROI 关闭时不执行过滤，行为保持兼容。

切换与验证：

```bash
python tools/switch_steel_ball_model.py validate
python tools/switch_steel_ball_model.py candidate-roi
python tools/switch_steel_ball_model.py candidate-roi-strict
python tools/switch_steel_ball_model.py status
```

工具只原子替换配置并创建 `runtime_backups/` 备份，不自动重启服务。

第一轮只比较 ROI 效果（两侧均为 conf=0.40）：

```bash
python tools/compare_steel_ball_models_video.py \
  --video path/to/compare_source.mp4 \
  --baseline-config config/model_profiles/steel_ball_candidate.yaml \
  --baseline-name candidate_raw \
  --candidate-config config/model_profiles/steel_ball_candidate_roi.yaml \
  --candidate-name candidate_roi \
  --write-videos
```

第二轮只比较阈值效果（两侧均启用相同 ROI）：

```bash
python tools/compare_steel_ball_models_video.py \
  --video path/to/compare_source.mp4 \
  --baseline-config config/model_profiles/steel_ball_candidate_roi.yaml \
  --baseline-name candidate_roi_040 \
  --candidate-config config/model_profiles/steel_ball_candidate_roi_strict.yaml \
  --candidate-name candidate_roi_strict_050 \
  --write-videos
```

不得把同时改变 ROI 和置信度阈值的两份配置比较结果称为单独 ROI 效果。

提取待人工复核困难帧：

```bash
python tools/extract_steel_ball_hard_frames.py \
  --video path/to/compare_source.mp4 \
  --frames-csv runs/model_compare/<run>/frames.csv \
  --output-dir runs/hard_frames/compare_source \
  --max-per-category 120
```

输出包含 ROI 外检测输出、fast 无最终输出、最长连续无输出、ROI 内低置信候选和几何失效代表帧。它们不是自动真值；所有训练标签必须人工审核和标注，工具不会创建 YOLO 标签或空标签文件。
