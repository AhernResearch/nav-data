# 冻结 release 格式

这是 nav-data 与 nav-train 之间唯一的接口。nav-data 负责产出，nav-train 只读消费；
两侧都不 import 对方代码。

## 目录

```
<release>/
  manifest.json          # 入口：文件哈希、生产者哈希、创建时间、schema
  episodes.jsonl.gz      # 每行一个 episode 摘要，按 (scene, uid) 排序
  summary.json           # 清点汇总（状态、计数、时长、审核分布）
  result.json            # 机器可读结论（passed / training_ready 等）
  scene_summary.csv      # 按场景汇总
  episodes.csv           # 逐 episode 明细
  review-sample.csv      # 待人工复核样本
  source-snapshot.json   # 来源快照与目录完整性
  frozen-entries.jsonl.gz# 与压缩目录的逐条对应
  structural-errors.json # 结构错误（为空表示全量通过）
  deep-media-audit.json  # 抽检媒体的解码与哈希结果
  deep-sample-selection.json
```

## manifest.json

| 字段 | 含义 |
|---|---|
| `schema` | 固定为 `nav_train.free_collection_inventory.v1`（历史名，保留以保证已发布身份不变） |
| `owner` | 本次发布的 owner |
| `files_sha256` | 除 `progress.json` / `manifest.json` / `result.json` 外所有文件的 sha256 |
| `source_snapshot_sha256` | 来源快照哈希 |
| `producer_sha256` | 生产实现哈希 |
| `training_interface` | 本期为 `deferred_by_user` |
| `training_ready` | 本期为 `false` |
| `created_utc` | 创建时间 |

读取方必须先校验 `files_sha256` 中每个文件的实际哈希，再使用内容。哈希不符属于来源
损坏，应停止而不是降级读取。

## episodes.jsonl.gz

每行一个 JSON 对象，字段见 `episodes.csv` 的表头，关键项：

| 字段 | 含义 |
|---|---|
| `uid` | 完整录制身份（跨集合不可复用） |
| `case_id` / `run` | 客户端任务身份与来源运行 |
| `scene` / `source_split` | 场景与来源划分 |
| `split_assignment` | 发布时固定为 `unassigned`（split 由 nav-train 负责） |
| `training_admission` | 固定为 `pending_review` |
| `semantic_admission` | 语义审核状态 |
| `frames` / `duration_s` | 帧数与录制时长 |
| `instruction` / `target_view` | 指令与目标视图 |
| `path_m` / `goal_planar_m` / `final_error_m` | 路径与终点几何 |
| `technical_qc` / `deep_media_qc` | 结构检查与媒体抽检结论 |
| `metadata` / `metadata_sha256` | 压缩 episode 元数据路径与哈希 |
| `recording_fingerprint` / `exact_episode_fingerprint` | 去重指纹 |

## 硬约束

1. **划分不由发布决定。** `split_assignment` 与训练样本选择属于 nav-train；发布只冻结
   来源、身份与质量。
2. **审核状态原样透传。** 结构检查通过不代表指令语义正确。
3. **训练不可直接使用。** `training_ready=false` 与 `training_admission=pending_review`
   是发布结论的一部分，读取方不得覆盖。
4. **schema 字符串不变。** `nav_train.*` 前缀属于已发布数据的身份，改名会使历史 release
   与 evidence 失效。
