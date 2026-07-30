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
