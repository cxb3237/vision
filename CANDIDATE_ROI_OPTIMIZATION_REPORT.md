# Candidate 钢球模型第一阶段 ROI 优化实现报告

## 1. 工程与任务起点

- 工程：`E:\NUEDC\camera`
- 分支：`main`
- 起始提交：`42413a3 feat(vision): add offline video model comparison`
- 任务开始前 `git status --short`：`?? MODEL_INTEGRATION_REPORT.md`

`MODEL_INTEGRATION_REPORT.md` 是用户在本任务前已有的未跟踪文件，本任务未修改。

## 2. 红蓝端点与坐标链路审计

- 端点检测模块：`detectors/pipe_marker_detector.py` 中的 `PipeMarkerDetector`。
- 右端点：`config/pipe_mapping.yaml` 的 `marker_a`，名称 `right_blue`，返回蓝色最大轮廓质心。
- 左端点：同配置的 `marker_b`，名称 `left_red`，返回红色最大轮廓质心。
- 坐标空间：直接在摄像头原始 BGR 帧上计算，属于原始图像坐标（部署配置为 640×480），不是 416×416 模型坐标。
- 原链路保存位置：`VisionRuntime` 每帧把 A/B 坐标写入 `VisionResult.marker_a_x/y`、`marker_b_x/y` 和状态快照。
- 原状态特性：没有跨线程端点状态、平滑、最小轴长判断或失效保持；本次新增轻量线程安全 `PipeCorridorFilter`，只保存最近有效轴线并按超时失效。
- 现有 Runtime：`SteelBallNcnnRuntime.predict()` 返回全部置信度过滤和 NMS 后、已映射回原图的检测框列表。
- 原 Detector：在 `select_primary_detection()` 中从全部有效框选择最终目标。
- 跟踪与 UART：`TargetTracker` 使用 Detector 的 `VisionResult`；`VisionRuntime` 仅在最终结果和动态端点都有效时计算毫米位置并交给现有 UART。UART 格式和代码未修改。
- 离线视频工具：继续实例化正式 `SteelBallYoloNcnnDetector`，因此复用相同 ROI 逻辑。

## 3. 水管宽度来源

工程原先没有管宽、半宽或管径像素配置。用户补充实物约为：外径 20 mm、内径 15 mm、管长 250 mm。当前改用与轴长同步变化的物理比例：

```text
effective_half_width_px = current_axis_length_px × corridor_half_width_ratio
corridor_half_width_ratio = 10 mm 外半径 / 250 mm 标记跨度 = 0.04
```

每帧使用当前有效红蓝端点轴长；端点暂失时使用保持轴线自身长度，因此不再依赖旧标定的固定 496 px 跨度。496 px 轴长时实际半宽为 19.84 px，400 px 时为 16 px。项目资料不能独立确认 20 mm 外径和 250 mm 标记端点跨度，这两个硬件尺寸及 0.04 比例均标记为“待硬件确认”。固定 `corridor_half_width_px` 只在 ratio 未设置或不大于 0 时回退。

## 4. ROI 几何与状态

新增 `core/pipe_corridor.py`：

- `PipeAxis`
- `PipeCorridorConfig`
- `PipeCorridorDecision`
- `PipeCorridorFilter`

设左红端点 `P0`、右蓝端点 `P1`、框中心 `C`、`V=P1-P0`：

```text
t = ((C-P0) dot V) / (V dot V)
distance = abs(cross(V, C-P0)) / length(V)
margin_t = end_margin_px / length(V)
```

接受条件为轴有效且足够长、`-margin_t <= t <= 1+margin_t`、`distance <= effective_half_width_px`。稳定原因码包括 `accepted`、`geometry_missing`、`geometry_invalid`、`axis_too_short`、`before_left_end`、`after_right_end`、`outside_corridor`。

模块处理重合端点、NaN/无穷、短轴、非法图像、越界框和端点顺序交换。当前有效端点更新轴线；端点暂失最多保持 250 ms；超时后 `require_valid_geometry=true` 会安全地输出无目标，不永久使用旧轴线。状态和调试快照输出 `effective_half_width_px`，叠加文字显示当前实际半宽。

## 5. 推理链路插入位置

准确顺序为：

```text
NCNN 推理 → 解码 → 置信度过滤/NMS → 原图坐标还原
→ 对所有候选框做水管走廊过滤
→ 复用 select_primary_detection() 选择最终目标
→ TargetTracker → VisionResult → 毫米映射 → UART
```

最高置信度框在 ROI 外时不会遮蔽 ROI 内的次高候选。ROI 关闭时仍直接把原候选交给原选择函数，行为兼容。

- 未修改 `inference/steel_ball_ncnn_runtime.py`，未改变解码、NMS 或 letterbox 数学。
- 修改正式 Detector，以便在最终选择前过滤全部 NMS 后候选。
- 修改 `VisionRuntime` 仅用于复用 Detector 已检测的当前端点，避免 ROI 开启时重复颜色检测；UART 和控制逻辑未改。
- 未修改网页前端；Detector 状态字典新增向后兼容的 ROI 字段，现有状态接口会自动携带它们。

## 6. 配置档案与正式配置

- Baseline：未修改，SHA256 仍为 `AC46C693568E78CD7D96815C7BDEA22F6A79B632936906E2DC1602C4AE668C76`。
- Candidate Raw：未修改，conf=0.40，省略 `pipe_roi` 等效关闭，SHA256 仍为 `E8FD30817DB85EC0CA27AD27B46C71FB84867980FFD4E04FE423105DB97D3F1F`。
- Candidate ROI：`config/model_profiles/steel_ball_candidate_roi.yaml`，仍使用 `candidate_ncnn_model`，conf=0.40，ROI 开启，ratio=0.04，hold=250 ms。
- Candidate ROI Strict：新增 `config/model_profiles/steel_ball_candidate_roi_strict.yaml`，与 Candidate ROI 完全相同，仅 conf=0.50。
- 正式配置：任务起点和终点均为 baseline，SHA256 `AC46C693568E78CD7D96815C7BDEA22F6A79B632936906E2DC1602C4AE668C76`；未执行实际切换。

`switch_steel_ball_model.py` 保留 `status/baseline/candidate/candidate-roi/validate`，新增 `candidate-roi-strict`；status 通过完整配置内容区分共享同一模型目录的三个 Candidate profile。四套 profile 均通过生产 Runtime 的静态模型图、metadata 和配置验证。

## 7. 视频比较扩展

`compare_steel_ball_models_video.py` 保持原 CLI，支持 `--baseline-name` 和 `--candidate-name`。第一轮以 Candidate 0.40/ROI关闭对 Candidate ROI 0.40/ROI开启，隔离 ROI 效果；第二轮以 Candidate ROI 0.40 对 Candidate ROI Strict 0.50，隔离阈值效果。不能把同时改变 ROI 与阈值的结果称为单独 ROI 效果。CSV 为两侧增加 raw 数量、ROI 几何状态、接受/拒绝数量、ROI 最终选择和原因。

summary/report 新增 ROI 有效/无效帧、raw/accepted/rejected 总数、ROI 外输出比例、各运动等级接受比例、几何失效无输出、阈值变化和仅 ROI 变化的可归因帧。标注视频显示原始框、拒绝框、轴线、走廊边界、ROI 状态和自定义配置名称。自动结果只说明输出变化，不声称真实准确率提高。

## 8. 困难帧提取

新增 `tools/extract_steel_ball_hard_frames.py`，输出：

- `roi_outside_review/`
- `fast_no_output_review/`
- `long_no_output_review/`
- `low_conf_inside_roi_review/`
- `geometry_invalid_review/`
- `manifest.csv`
- `README.md`

同类帧使用最小时间间隔和 64×36 灰度 MAD 去重，最长无输出区间均匀抽样，保存原始解码分辨率。工具不生成 YOLO 标签、空标签或真值结论；所有训练标签必须人工审核。

## 9. 文件变更

新增：

- `config/model_profiles/steel_ball_candidate_roi.yaml`
- `config/model_profiles/steel_ball_candidate_roi_strict.yaml`
- `core/pipe_corridor.py`
- `tools/extract_steel_ball_hard_frames.py`
- `tests/test_pipe_corridor.py`
- `tests/test_steel_ball_roi_filter.py`
- `tests/test_extract_steel_ball_hard_frames.py`
- `CANDIDATE_ROI_OPTIMIZATION_REPORT.md`

修改：

- `.gitignore`
- `README.md`
- `app.py`
- `core/config_loader.py`
- `core/models.py`
- `core/vision_runtime.py`
- `detectors/pipe_marker_detector.py`
- `detectors/steel_ball_yolo_ncnn_detector.py`
- `docs/STEEL_BALL_MODELS.md`
- `models/steel_ball/README.md`
- `tools/compare_steel_ball_models_video.py`
- `tools/switch_steel_ball_model.py`
- `tools/validate_steel_ball_models.py`
- `tests/test_compare_steel_ball_models_video.py`
- `tests/test_config_loader.py`

## 10. 测试结果

- `python -m compileall core detectors inference tools`：通过。
- 本轮指定几何、ROI、困难帧、视频比较和配置测试：46 passed。
- Runtime、Detector、app、VisionRuntime 和动态端点映射回归：60 passed。
- 动态比例测试覆盖：496 px→19.84 px、400 px→16 px、轴长变化同步、保持轴线自身长度、固定宽度回退、双宽度无效报错、四 profile 内容/status/validate。
- 四套 profile 静态生产模型验证：全部 PASS。
- `switch_steel_ball_model.py status`：`active_profile: baseline`。
- `git diff --check`：通过。

Windows 工作区未找到 `compare_source.mp4`，因此没有伪造真实视频结果，只运行了合成短视频、FakeDetector 和几何测试。Windows 测试耗时不用于判断树莓派性能。

## 11. 模型文件 SHA256（修改前后相同）

| 模型 | 文件 | SHA256 |
|---|---|---|
| baseline | model.ncnn.param | `ADDFCB27466473B6ACBFC352251C27785668347369951C0AF0DB6CF3A124E24F` |
| baseline | model.ncnn.bin | `1B2BC2887F395906FB6D7D9287CEE2A753453234BA6EE19C70F6EF9D937E0BD5` |
| baseline | metadata.yaml | `37FB33EC3095EB13DC1B516E29850C49744160643EA866CE65C0A4CF5CB0EC9D` |
| baseline | model_ncnn.py | `01F6B725DEF4F8C42CE727AE8B61A68AC0741E43E574CA010306125270F1A642` |
| candidate | model.ncnn.param | `ADA13CCDB83C916F6262123B7B423195BBF5015CDC043B427F205D753F8398E9` |
| candidate | model.ncnn.bin | `B9893B56F4E309B33F321EC907340F1588380F62E1B123294C0B7C56B32D67E8` |
| candidate | metadata.yaml | `BE0ABBF052A7BD786B1BFE9A52800B5A8D9D634D0AFBEE2F9B45D73A35D8174A` |
| candidate | model_ncnn.py | `34E6D7615DDE479AD9EC83D571EF41A02299C869B863BEEA4B58E6C937D76303` |

## 12. Raspberry Pi 命令（未在 Windows 执行）

拉取后验证和切换：

```bash
cd /home/clb/Desktop/camera
.venv/bin/python tools/switch_steel_ball_model.py validate
.venv/bin/python tools/switch_steel_ball_model.py candidate-roi
sudo systemctl restart vision-touch.service
sleep 8
.venv/bin/python tools/switch_steel_ball_model.py status
```

第一轮比较 ROI 效果（两侧 conf=0.40）：

```bash
.venv/bin/python tools/compare_steel_ball_models_video.py \
  --video data/recordings/competition/compare_source.mp4 \
  --baseline-config config/model_profiles/steel_ball_candidate.yaml \
  --baseline-name candidate_raw \
  --candidate-config config/model_profiles/steel_ball_candidate_roi.yaml \
  --candidate-name candidate_roi \
  --write-videos
```

第二轮比较阈值效果（两侧 ROI 相同）：

```bash
.venv/bin/python tools/compare_steel_ball_models_video.py \
  --video data/recordings/competition/compare_source.mp4 \
  --baseline-config config/model_profiles/steel_ball_candidate_roi.yaml \
  --baseline-name candidate_roi_040 \
  --candidate-config config/model_profiles/steel_ball_candidate_roi_strict.yaml \
  --candidate-name candidate_roi_strict_050 \
  --write-videos
```

提取困难帧：

```bash
LATEST=$(find runs/model_compare -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | head -n 1 | cut -d' ' -f2-)
.venv/bin/python tools/extract_steel_ball_hard_frames.py \
  --video data/recordings/competition/compare_source.mp4 \
  --frames-csv "$LATEST/frames.csv" \
  --output-dir runs/hard_frames/compare_source \
  --max-per-category 120
```

恢复原始 Candidate 或 Baseline：

```bash
.venv/bin/python tools/switch_steel_ball_model.py candidate
sudo systemctl restart vision-touch.service

.venv/bin/python tools/switch_steel_ball_model.py baseline
sudo systemctl restart vision-touch.service
```

## 13. 已知限制与人工复核

- 约 20 mm 外径和约 250 mm 标记端点跨度来自用户口述，项目资料不能独立确认，均为“待硬件确认”；0.04 比例必须在最终机位上通过叠加画面复核。
- 颜色端点检测仍沿用原有 HSV 最大轮廓方法；高光、遮挡或颜色干扰可能使几何暂时失效。
- 自动统计不能区分正确检测与 ROI 外背景输出，也不能给出准确率或 mAP。
- 阈值影响和 ROI 影响只能在可归因帧上拆分；最终决策必须查看 side-by-side 视频和困难帧。
- 困难帧不是训练真值，必须人工判断和标注。

## 14. 推荐 Git 命令与执行声明

建议只暂存本任务文件，不包含用户已有的 `MODEL_INTEGRATION_REPORT.md`：

```bash
git add .gitignore README.md app.py core/config_loader.py core/models.py core/vision_runtime.py core/pipe_corridor.py detectors/pipe_marker_detector.py detectors/steel_ball_yolo_ncnn_detector.py docs/STEEL_BALL_MODELS.md models/steel_ball/README.md config/model_profiles/steel_ball_candidate_roi.yaml config/model_profiles/steel_ball_candidate_roi_strict.yaml tools/compare_steel_ball_models_video.py tools/extract_steel_ball_hard_frames.py tools/switch_steel_ball_model.py tools/validate_steel_ball_models.py tests/test_compare_steel_ball_models_video.py tests/test_config_loader.py tests/test_pipe_corridor.py tests/test_steel_ball_roi_filter.py tests/test_extract_steel_ball_hard_frames.py CANDIDATE_ROI_OPTIMIZATION_REPORT.md
git commit -m "feat: add candidate pipe ROI filtering and hard-frame review"
```

本任务未执行 `git commit`，未执行 `git push`，未启动摄像头、串口、网页服务或 systemd。
