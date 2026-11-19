# 导航数据生产的两条主线

当前实现已切换为 **sim-engine 常驻采集服务**：两个生产入口共用
`src/nav_data/collection/client.py`，只提交任务、调用模型、读取回执和生成训练标签。
Habitat worker、场景缓存、四视图 RGB-D、选点、规划、专家执行与原始录制都在
sim-engine。下文旧快照和“共享并发基建”一节记录迁移前的实现，不是当前部署入口。
当前启动方式、边界和验证见 [常驻采集服务](collection-service.md)。

2026-11-14 按用户要求收敛：保留 R2R chunk 数据生产、短指令自动生成两条链路。中间实验代码与原始实验产物已清理；历史结论、报告和摘要证据保留在文档中。本页是当前维护入口，历史报告中的旧脚本和输出路径不再作为运行入口。

| 主线 | 代码入口 | 固定配置 |
|---|---|---|
| R2R chunk 数据生产 | `nav_data.planner_chunks.pool`；规划片段标注 `nav_data.planner_chunks.annotation` | [`r2r_chunk.mainline.json`](../annotations/planner_chunks/r2r_chunk.mainline.json) |
| 短指令自动生成，同时记录 PointNav 目标 | `nav-data planner-chunk-pool`、`nav_data.tasks.short_instructions` | [`short_instructions.mainline.json`](../annotations/planner_chunks/short_instructions.mainline.json) |

两条链路共用场景后端、路径规划、专家控制器、录制与记录校验。模型调用、环境执行和队列调度保持分离。所有输出仍需下游审核，不自动进入训练，不自动重试移动。

## 1. R2R chunk 数据生产

从冻结的 FG R2R train chunk 和连续 train 几何准备任务，按距离合并短段，生成起点/中途扰动，进行软引导点规划和移动前安全筛选。随后并行执行专家轨迹和当前四图的剩余指令标注。模型不接收目标点、参考路径或几何验收指标。

在能访问固定场景和依赖的采集环境中，使用当前包：

```bash
nav-data planner-chunk-pool \
  --config annotations/planner_chunks/r2r_chunk.mainline.json \
  --episodes /data/nav_storage/organized/v0002/ce_r2r_expanded_candidates/evidence/train.json.gz \
  --fg-train /data/nav_storage/nav-train/planner-pilots/pose-production-55670d6e8045/FGR2R_train.json \
  --output /data/nav_storage/nav-train/prepared/NEW_UNIQUE_RUN

nav-data planner-chunk-pool \
  --manifest-dir /data/nav_storage/nav-train/prepared/NEW_UNIQUE_RUN
```

当前固定配置取自已完成的全量补跑 manifest，保留其实际安全、执行与并发参数。准备和执行使用新目录，不把历史完成目录升级成新版本。来源标注工具 `nav_data/planner_chunks/annotation.py`、`nav_data/planner_chunks/pool.py`、协议及单元测试夹具继续保留；旧的本地 full_prepare/replenish 管线已删除。

已完成的最终覆盖索引位于 A100：

```text
/data/nav_storage/nav-train/planner-pilots/pose-replenish-3325605aa77e/prepared/coverage-summary.json
/data/nav_storage/nav-train/planner-pilots/pose-replenish-3325605aa77e/prepared/final-coverage-index/
```

| 最终状态 | 槽位数 |
|---|---:|
| completed_audit | 126,625 |
| excluded_before_movement | 8,922 |
| failed_or_uncertain | 45 |
| 合计 | 135,592 |

这不是原始全部指令的完整覆盖，也不是语言或训练准入。输入关联范围及处理规则见 [生产说明](pose-production-pipeline.md)、[全量启动和完成记录](pose-full-production-20261113.md)、[去重补跑说明](pose-replenishment-20261113.md)。

最终索引直接或间接引用以下四个根目录，作为生产数据和来源依赖保留，路径与内容未改动：

```text
pose-production-55670d6e8045
pose-replenish-3325605aa77e
pose-sparse-fix-c17527865e6a
pose-comfort-policy-ec641cf32698
```

后两个虽然沿用了历史试验名称，其中的有效真实记录已被正式生产复用，删除会破坏最终索引。冻结源码、来源清单、模型/动作证据和不确定记录随生产数据保留。

2026-11-15 的[轨迹长度分布对比](nav-train/docs/evidence/free-distribution-20261115-484a46fcd393/viewer/trajectory-comparison/report.md)
比较了固定的92,167条自由指令实采轨迹、126,625条最终成功chunk、4,675条官方完整train路径，
并单列原始FG切片。统计区分实际连续执行与离散参考路径、start/mid起点、零位移切片以及场景权重；
同目录的`index.html`支持按61个场景筛选，输入哈希与校验结果随报告保留。

[R2R chunk 空间覆盖报告](nav-train/docs/evidence/free-distribution-20261115-484a46fcd393/viewer/chunk-coverage/report.md)
沿用相同NavMesh、132个高度带和0.5米栅格口径：chunk实际轨迹覆盖36.19%，自由采集89.17%，
并集90.60%。相邻`index.html`提供61个完整场景的起点/轨迹覆盖与重叠平面图，统计包含未访问高度带。

## 2. 短指令自动生成

2026-11-15 全量续产采用已通过端到端测试的 `collection-v5`：32个仿真worker、
32个独立CPU规划进程、64个模型槽位，继承32466条完整记录，补足总计100000条。
实际部署、交接与监控路径见 [v5全量生产记录](free-instruction-v5-production-20261115.md)。

当前四图自由选可见地面停止框 → 原框与深度安全地面取交集 → 框内选点和路径/控制器预演 → 一次实际专家执行。允许当前可见范围内任意距离，没有8m上限；当前层地面约束与可见地面支持要求不变。

正式入口已从 `annotations/planner_chunks/experiments/` 移至包内。移除了模型直接选点、最近安全点修复、mask overlay prompt、强制相机对照、旧提案复用和旧 demo 图像比较代码。实际主线的画框 prompt、mask 内部几何筛选、规划、控制与录制规则保持不变。

```bash
PYTHONPATH=src .venv/bin/python -m nav-data planner-chunk-pool \
  --source-config annotations/planner_chunks/short_instructions.mainline.json \
  --output outputs/collections/NEW_UNIQUE_RUN
```

准备命令不请求模型、不启动移动。在固定仿真环境中，用冻结包执行：

```bash
PYTHONPATH=outputs/collections/NEW_UNIQUE_RUN/source/src \
  python -m nav_data.tasks.short_instructions \
  --root outputs/collections/NEW_UNIQUE_RUN
```

实际运行仍需具有 Habitat 的 Python、固定场景与 NavMesh、原冻结模拟器和已配置的模型端点。A100 所需的额外 Python 依赖已从旧压测目录提升至 `/data/nav_storage/nav-train/runtime/python-vendor`，应加入 `PYTHONPATH`；包、分发元数据及许可证完整保留，`runtime/vendor-provenance.json` 记录来源哈希。没有启停现有服务。

已准备一份当前主线快照，未发起新采集：

- 本地：`outputs/collections/short-instructions-mainline-20261114`。
- A100：`/data/nav_storage/nav-train/collections/short-instructions-mainline-20261114`。
- A100 快照内 `source/src` 同时包含两条主线实现；`mainline-inputs/` 保存两份固定配置。
- 状态 `prepared_not_launched`。源码、配置哈希校验，以及两个生产入口、全量准备、短指令准备、PointNav 的五个独立 CLI 加载检查通过，未启动 Simulator。

每条短指令保留原始任务、框、目标与专家录制，同时保存 `pointnav-target.json`、`pointnav-outcome.json`、`pointnav-frames.jsonl`。固定目标不替换为实际停止点；PointNav 几何验收沿用本次执行的12cm阈值，专家控制器8cm目标半径另存。坐标约定与审核要求见 [PointNav记录协议](visible-region-pointnav-20261114.md)。

## 实验清理与文档

2026-11-15 补充清理已结束实验的原始媒体、旧2B中间checkpoint和遗留安装临时文件，
共142025个文件、29.33 GiB；JSON回执、摘要、冻结源码及所有正式生产数据保留。
详见 [A100本次清理与空间核验](a100-experiment-cleanup-20261115.md)。

本轮删除本地66项、A100 24项，清单计106,235个文件、13,416,074,879字节（约13.42GB逻辑大小，不等于文件系统实际回收块数）。删除范围包括中间实验目录、旧原始请求/响应、demo RGB-D、对照/压测快照、图表生成脚本、旧配置和废弃选点/overlay实现；31份摘要/迁移证据归入 [`docs/evidence/planner_chunks/`](nav-train/docs/evidence/planner_chunks/)。

未清理正式生产数据、其被复用的历史记录、当前模型部署身份、基础场景/模型资产、继承许可证、来源 provenance、主线回归测试及无关训练/数据源工作。清理前后分别校验本地55个、远端229个保留文件，哈希一致；远端待删路径没有活动进程依赖。

逐项目录、数量、保留哈希和执行结果：

- [本地清单](nav-train/docs/evidence/collection-cleanup-20261114.local-plan.json) / [结果](nav-train/docs/evidence/collection-cleanup-20261114.local-result.json)。
- [A100清单](nav-train/docs/evidence/collection-cleanup-20261114.remote-plan.json) / [结果](nav-train/docs/evidence/collection-cleanup-20261114.remote-result.json)。
- [主线配置与准备快照](nav-train/docs/evidence/collection-mainlines-20261114.json) / [A100独立加载验证](nav-train/docs/evidence/collection-mainlines-20261114.remote-prepared.json)。

清理后的 nav-train 全量测试 **1126 passed、12 skipped**；比上一版少11项，来自删除的最近点/overlay对照分支测试。主线相关测试保留并更新为正式模块入口。harness 的311项测试和4项subtest、ruff、mypy、F821/F823及前端build均通过。本轮没有新模型请求、移动、正式benchmark或训练导出。


## 共享并发基建（2026-11-14）

两条主线现已共用 `nav_data/collection/pool.py` 的 `ModelPool`：所有端点共享有界队列，每个 profile 固定槽位，中断时关闭；场景进程、背压、超时与所有权由 sim-engine 采集服务负责。

| 责任 | R2R adapter：`nav_data/planner_chunks/pool.py` | 自由轨迹 adapter：`nav_data/tasks/short_instructions.py` |
|---|---|---|
| 模型调用顺序 | 当前四图就绪后提交标注，和同条专家执行重叠 | 当前四图就绪后提交提案，收到完成结果后才选点、规划和执行 |
| 模型可见输入 | 局部指令＋当前四图 | 仅当前四图 |
| 环境生命周期 | 按场景复用后端，每条独立重置 | 同样按场景复用后端，每条独立重置 |
| 任务完成判定 | 几何结果和模型标注汇合 | 框内目标、几何执行、实际停止与录制检查 |
| 轨迹与目标文件 | 保留原R2R目录布局和字段 | 保留原自由任务目录布局及三个PointNav文件 |

### 共享的调度行为

- 显式 `spawn`，不继承父进程的CUDA/GL状态；同一scene整批交给一个worker，较大批次优先。场景队列也有容量限制，由独立投递线程按需填充，避免提前序列化全部场景。
- 实际启动worker数不超过场景数。每个worker生成独立instance UUID，记录owner、PID、GPU；看门狗只终止本次创建的子进程。
- 所有端点槽位从同一个有界模型队列取任务，空闲端点继续取下一条。队列只传worker/candidate身份，图像在真正取得槽位后读取。拒绝重复endpoint造成的隐蔽超配。
- 模型等待心跳不作为仿真停滞；待投递队列的背压时间也不计为worker无进展。HTTP超时、worker超时、磁盘保留阈值与中断均保留证据，不自动重新分配已领取的scene或重试动作。
- `model-finished.json` / `execution-finished.json` 在对应结果持久化后创建。汇总读取完成标记后的记录，进程中断留下的半截文件保持原样并归入失败/不确定。
- 旧的 `planner_chunk_pool.run_queue()` 和 R2R `replenish.run_annotation_only()` 也改用相同 `ModelPool`，取消重复的线程池与批次等待实现。快速端点可立即继续工作，普通模型异常保留失败结果。
- 汇总阶段对结果文件只读一遍，同时累计耗时和场景复用统计；准备目录检查也避免对每个空候选目录逐项发起文件系统查询。

模型请求仍使用原有HTTP调用和tool-call解析，各主线适配器保留自己的Prompt、输入白名单及几何规则，没有引入模型名称分支。

### 第二主线的实际效率改动

`SceneBackend.observe(..., capture_depth=True)` 在同一次观察中保存四方向RGB、深度和传感器位姿。原来先渲染四幅RGB、再渲染四次读取深度，现在初始观察只需四次渲染。R2R默认观察行为保持原样。自由轨迹不再每条新建/关闭Simulator，执行预算只修改当前候选，下一条恢复冻结配置并重置状态。

两条主线使用相同配置字段：`simulation_workers`、`model_profiles`、`ready_queue_size`、`candidate_timeout_s`、`request_timeout_s`、`minimum_free_disk_bytes`。当前两份主线配置都设置最多16个worker、四个模型端点各16槽位，共64个模型槽位；旧自由任务的单个 `gpu/profile` 配置仍可解析，新主线配置已使用统一字段。

这些是运行上限。自由轨迹目前每个活动worker只处理一条任务，因此实际模型在途数还受活动worker和场景数限制；当前16个验证姿态只有5个scene，最多启动5个worker。64槽位不表示始终有64个请求在途。

共享的是同一套实现和配置口径，每次run拥有自己的进程池和模型池。两个独立run同时启动时需要拆分总worker和端点槽位预算；本次没有增加跨run常驻资源服务。

### 冻结快照与验证

最终快照为 `outputs/collections/short-instructions-concurrency-mainline-20261114-r1`，A100对应 `/data/nav_storage/nav-train/collections/short-instructions-concurrency-mainline-20261114-r1`。owner为 `a11862e645a04f7597642d677acae579`，140个源码/资源文件冻结。两条主线的代码均在快照包中；`mainline-inputs/` 保存两份主线配置。R2R的普通准备、全量准备和去重补跑也已把共享模块加入 `code_sha256`，旧完成生产目录不改动。

状态仍为 **prepared_not_launched**。本次验证包含：

- nav-train全量：**1142 passed、12 skipped**。新增16项测试，覆盖端点实际并发与背压、中断时只保留在途调用、worker/model顺序、跨进程场景复用、模型排队期间的看门狗、半截文件处理、真实自由任务入口的固定目标与失败不移动、RGB-D一次渲染及旧/新配置兼容。
- R2R原有18项流水线测试继续通过，包括同条轨迹标注/执行重叠、worker崩溃不重派、实际动作不重试、磁盘阈值和源配置防篡改。
- harness：311 passed、4 subtests passed，ruff、mypy、F821/F823与前端build通过；新增共享模块及测试额外通过完整ruff检查。
- A100冻结哈希及五个CLI独立加载验证通过；仅检查加载，不创建Simulator、不发送HTTP、不移动或控制服务。

没有新吞吐实测，不能据此承诺16倍加速。起点列表仍是既有16个固定姿态；此前确认的NavMesh起点采样应接入这套基建，随后用首批生产数据测量吞吐、内存、队列等待、语义质量和磁盘占用。

[实现与测试证据](nav-train/docs/evidence/collection-concurrency-20261114.validation.json)；[A100最终快照](nav-train/docs/evidence/collection-concurrency-20261114.remote-ready.json)。
