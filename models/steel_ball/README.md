# Steel-ball NCNN models

本目录同时保留两套可追踪模型：

- baseline：`best_ncnn_model/`，当前正式模型，默认由 `config/steel_ball_ncnn.yaml` 使用。
- candidate：`candidate_ncnn_model/`，新训练模型，仅用于受控 A/B 验证。

禁止直接覆盖任一目录中的模型文件。每套模型都必须包含：

- `model.ncnn.param`：NCNN 网络结构和输入/输出节点。
- `model.ncnn.bin`：模型权重。
- `metadata.yaml`：任务、416×416 输入尺寸和单类 `steel_ball` 映射。
- `model_ncnn.py`：同批导出的节点参考；当前 Runtime 用它核对 param 的节点名。

验证两套模型：

```bash
python tools/validate_steel_ball_models.py
python tools/switch_steel_ball_model.py validate
```

查看与切换配置：

```bash
python tools/switch_steel_ball_model.py status
python tools/switch_steel_ball_model.py candidate
python tools/switch_steel_ball_model.py baseline
```

切换工具只原子替换配置并把旧配置备份到 `runtime_backups/`，不会修改模型或重启服务。测试结束后必须执行 `baseline` 恢复正式模型。
