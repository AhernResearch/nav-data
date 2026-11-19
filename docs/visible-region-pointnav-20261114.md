# 视觉画框主线同时保存 PointNav 目标

> 2026-11-14清理说明：本文保留历史结论；实验代码和原始实验产物已按用户要求删除。当前入口、生产数据保留范围与文档证据位置见 [两条生产主线](navigation-data-mainlines.md)。

保留当前“四图选框 → 可见安全地面交集 → 框内选点和规划预演 → 一次专家执行”的采集主线。同一条实际轨迹保存语言任务和固定坐标目标，分别供后续语言导航、PointNav 审核使用。新增标签不改变模型提示、目标选择、控制器或实际动作，不重新请求模型或重跑移动。

## 新采集的记录

`run_visual_task_demo.py` 在目标通过可达性和距离检查后、实际移动前创建 `pointnav-target.json`，记录：

- 场景相对路径、初始世界位置和旋转；世界系 `Habitat_XYZ_Y_up`、单位米、四元数 `xyzw`。
- **系统最终选定的 NavMesh 目标**，及来源相机、原框、采样像素、深度命中点。原框和深度命中点继续保留，供审核定位过程。
- PointNav 几何验收半径采用本次执行的 `goal_tolerance_m`，当前为 **0.12m**；另存专家控制器目标半径 **0.08m**，两者含义分别记录。
- `pending_review` 准入状态。这个文件是私有几何信息，现有任务提案模型仍只接收起始四图。

执行结束后创建 `pointnav-outcome.json`，另存最后实际观测位姿、STOP 回执、实际终止原因、到固定目标的测地误差、欧氏误差、碰撞和原框内停止检查。实际停止点不替换原目标。执行返回但记录校验失败时，结果仍标为失败或不确定；执行没有返回时保留目标和既有命令日志，不生成成功结果。

记录完整时创建 `pointnav-frames.jsonl`：每帧用实际机器人位姿与同一个固定世界目标计算 `goal_base_forward_left_up_m`、`goal_distance_planar_m`、`goal_bearing_left_rad`，并保持原始帧号和时间。下游坐标型 PointNav 输入可取 `goal_base_forward_left_up_m[:2]`。机器人前方为局部 `-Z`、左方为局部 `-X`，方位角左正；逐帧重新变换，不沿用起点的相对向量。平面欧氏距离不能替代最终测地验收距离。

episode 的基础字段沿用 [Habitat NavigationEpisode](https://github.com/facebookresearch/habitat-lab/blob/main/habitat-lab/habitat/tasks/nav/nav.py) 的 `episode_id / scene_id / start_position / start_rotation / goals`。我们的相对目标笛卡尔表示明确为“前、左、上”；Habitat 的二维 CARTESIAN PointGoalSensor 使用“前、右”，不能直接交换使用。二维 POLAR 方位角同为左正。复用到具体 Habitat 任务时仍需显式配置成功距离与 STOP 判定，不能假设 episode 的 `goals.radius` 自动修改全局成功度量。

## 已有记录的离线整理

使用新模块，只读取完成采集目录，在另一个新目录创建候选包：

```bash
PYTHONPATH=src .venv/bin/python -m nav_data.tasks.pointnav（或 nav-data inspect） \
  --source-run outputs/planner_chunks/visible-region-test-64746fcd38e5 \
  --output outputs/planner_chunks/NEW_UNIQUE_POINTNAV_AUDIT
```

导出器验证 train 来源、冻结配置及源码哈希、完成证据（如存在）、实际起点与配置一致、预定目标与 NavMesh/执行目标一致、实际帧与命令/媒体校验、STOP 和终点验收。它不把停止点反推为任务目标，也不对失败轨迹补帧或移动重试。所有候选先验证，再创建新目录；已有输出拒绝覆盖。

包内 `episodes.audit.json` 是基础 PointNav episode 列表；每条有目标、停止结果、逐帧相对目标和派生来源。RGB、深度、原始命令继续引用源记录，不复制视频或重复采集。`manifest.json` 保存源证据、输出与转换代码哈希。源文件已有逐帧媒体 SHA256；生成候选前逐帧实际验证媒体。

语言语义审核结论单独保留。例如文字方向有问题的完成轨迹仍可成为坐标任务候选，但语言标签不会自动获得批准，PointNav 也仍需下游几何、来源和数据划分审核。候选包是 `audit_only / pending_review`，不是已批准的训练数据。

本次结果：

- 原采集：`outputs/planner_chunks/visible-region-test-64746fcd38e5`。
- 新候选包：`outputs/planner_chunks/visible-region-pointnav-audit-9c003c578a18`。
- **13 个 episode、262 帧**；3 条移动前排除记录作为排除原因留档。
- 原目录 **1201 个文件**导出前后 SHA256 全部一致。历史 attempt、源码快照和完成凭证均未改动。
- 新模型请求、移动命令、训练导出均为 0。

## 下一次采集的配置

已另行准备 `outputs/planner_chunks/visible-region-pointnav-config-6a0233bd1fe0/config.json`，使用当前主线和包含 PointNav 模块的新源码快照。状态为 **prepared_not_launched**；老配置、老快照不替换。后续使用这份新快照执行时会自动存三类 PointNav 文件，无需再改模型任务。

## 验证

新增 16 项测试覆盖固定目标与停止点分离、前/左符号及旋转、train 来源限制、实际录制检查、目标/起点/阈值不一致拒绝、输出不可覆盖、短轨迹不补帧、语言审核隔离，以及已有 PointNav 记录的一致性。

- nav-train：**1137 passed、12 skipped**。
- harness：**311 passed、4 subtests passed**；ruff、mypy、F821/F823、前端 build 均通过。
- 新增和修改的 Python 文件 F821/F823 检查通过。
- 没有修改前端流程或启动模型、仿真服务、正式 benchmark、训练。
