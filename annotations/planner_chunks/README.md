# 导航数据生产

当前维护两条主线，统一入口见 [运行与数据目录](../../docs/navigation-data-mainlines.md)。

| 主线 | 入口 | 配置 |
|---|---|---|
| R2R chunk 数据生产 | `python -m nav_train.pose_pipeline.full_prepare` → `python -m nav_train.pose_pipeline run` | `r2r_chunk.mainline.json` |
| 短指令自动生成＋PointNav目标 | `python -m nav_train.pose_pipeline.prepare_short_instructions` → `python -m nav_train.pose_pipeline.short_instructions` | `short_instructions.mainline.json` |

共享实现位于 `src/nav_train/pose_pipeline/`，保留 `chunk_merging.py`、来源标注器及共享模型队列。固定协议、Prompt和最小回归夹具保留在本目录；来源许可证、NOTICE和provenance保留为 `source-*`。

中间实验代码和原始产物已按用户要求删除，不再使用 `experiments/` 或 `archive/` 作为入口。实验报告仍在 `docs/`，31份摘要/迁移证据归入 [docs/evidence/planner_chunks](../../docs/evidence/planner_chunks/)。

正式R2R数据、最终索引及其引用的既有记录保持原路径与内容；当前短指令主线已解除旧demo依赖。两条链路保留单次执行、失败/不确定证据及下游审核边界，不自动重试移动，不自动准入训练。

并发调度和模型队列统一复用 `src/nav_train/collection_concurrency.py`，配置字段与两种任务顺序见 [共享基建说明](../../docs/navigation-data-mainlines.md#共享并发基建2026-11-14)。
