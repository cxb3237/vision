# 钢球 NCNN 双模型管理

## 为什么保留双模型

正式 baseline 与待评估 candidate 分目录保存，避免覆盖已验证权重，也让测试结果、模型来源和回退路径都可追踪。当前正式配置始终是 `config/steel_ball_ncnn.yaml`；profile 是可复用快照。

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
