# GS 自由指令数据生产

GS 场景使用现有 `short_instructions` 采集主线：四视图 RGB → 模型生成指令与
目标框 → GS RGB-D 反投影 → NavMesh 规划和预演 → 专家执行 → RGB-D/动作记录
→ 冻结目标及逐帧 PointNav 标签。

## 来源与配置

GS 采集配置必须明确使用 `simulation.backend=habitat_gs`，注册数据集的
`task=development`，并用 `scenes` 将 scene_id 映射到冻结的场景 manifest。
sim-engine 的 `collection.training_datasets` 是采集服务的操作允许列表；
列入其中不代表获得训练准入。

nav-train 的关键配置如下，其余相机、物理参数和几何参数必须与采集服务一致：

```json
{
  "response_mode": "json_schema",
  "model_profiles": [{
    "id": "gpu5-qwen27b",
    "model": "Qwen3.6-27B-MM",
    "endpoint": "http://127.0.0.1:8897/v1/chat/completions",
    "slots": 1
  }],
  "backend_manifest": {
    "config": {"split": "development"},
    "runtime": {
      "settings": {
        "simulation": {
          "backend": "habitat_gs",
          "datasets": {
            "gs_development": {
              "task": "development",
              "scenes": {"zhiyuan": "/frozen/scenes/zhiyuan/scene.json"}
            }
          }
        }
      }
    }
  },
  "collection_service": {"dataset": "gs_development"},
  "cases": [{
    "id": "unique-case-id",
    "episode": {"_dataset": "gs_development", "scene_id": "zhiyuan"}
  }]
}
```

这是字段示意，省略了地址、输出目录、起点、相机和物理参数，不能直接运行。
完整工作流仍由 `nav_data.tasks.short_instructions` 读取冻结的 `config.json`。
开发场景的 PointNav 目标保留 `source_split=development` 和
`training_admission=pending_review`；不能将其改名为普通 train 数据绕过来源校验。
现有 Habitat train 配置继续使用原有协议。

## 模型输出协议

`response_mode` 默认为 `tool_call`。对于没有配置工具调用解析器、但支持结构化
输出的模型服务，可显式设置 `json_schema`。两种协议使用相同字段、边界框约束
和校验器，原始请求及模型响应均保留；不根据模型名称自动切换，不失败重试。
模型仅接收四幅当前 RGB 图及任务生成提示，不接收 NavMesh、世界坐标或专家路径。

## GS 运行约束

- 可见区域选点需要 `simulation.depth_validation` 指向与实际 GS bindings 哈希匹配的
  深度单位、投影约定报告。该报告不代表扫描几何精度已经通过审核。
- 规划任务通过统一场景解析取得 manifest 内冻结的 NavMesh，不从 scene_id 推导文件名。
- 执行前使用与原观测相同的前视全景传感器和方向恢复图像，仍要求逐像素一致。
  不用主 RGB 传感器替代前视全景传感器，也不放宽像素误差阈值。
- 不可行、规划预算耗尽和不确定执行均保留独立回执。补足成功数量时使用新候选；
  已消费或不确定的执行不会重放。
- 产物包括模型指令/框、原始四图、冻结世界目标、规划证据、4 Hz RGB-D/状态/动作，
  以及 `pointnav-target.json`、`pointnav-outcome.json` 和 `pointnav-frames.jsonl`。

首批实测记录见 [GS 五条生产验证](gs-data-production-pilot-20261116.md)。
