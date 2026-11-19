# 两条主线共用 sim-engine 常驻服务

当前服务已包含[框内安全地面采样优化](visible-region-mask-sampling-20261114.md)：
从原框内安全像素选取有界候选，修复固定网格漏采。该页记录最新部署身份；
下文旧服务身份及测试数字作为历史记录保留。

当前默认生产入口仍是 R2R chunk 与短指令自动生成；两者通过
`nav_data.collection.client` 共用同一套 HTTP 任务队列。nav-train 的默认生产链路不再直接
实例化 Habitat、管理 GPU 仿真进程或跨仓库加载模拟器代码。R2R 全量准备所需的
NavMesh 分段长度、初始方向也通过只读 `reference_geometry` 任务查询。

sim-engine 负责 spawn 常驻 worker、有界场景缓存、四视图 RGB-D、框内目标规划、
专家执行和原始文件写入。观察完成就释放 worker，等待大模型不占仿真进程。
执行前恢复这条任务的起点，并逐像素校验起始正前图。R2R 专家执行与语言标注
独立进行；自由轨迹收到模型的框后再执行。逐帧操作在服务内部完成。

先在采集主机用冻结的 sim-engine 包启动：

```bash
python -m sim_engine serve-collection \
  --config examples/collection.train.v1.json --port 18812
```

该配置位于 sim-engine 仓库。两份 nav-train 主线配置新增了 `collection_service`：

```json
{
  "url": "http://127.0.0.1:18812",
  "shared_root": "/data/nav_storage/sim-engine/collections/train-v1",
  "dataset": "train_pilot",
  "camera": "visible_88cm_hfov120",
  "max_pending": 64,
  "poll_interval_s": 0.1
}
```

URL 必须与实际服务一致，`shared_root` 必须在客户端以相同绝对路径可读。
运行前检查服务训练数据集、相机、物理参数与冻结配置是否匹配。重新准备一份
新目录后，使用原来的两个生产命令；已有完成目录和历史冻结快照不会升级。
旧 `simulation_workers` 字段只供历史配置/CPU 测试适配，真实仿真并发由
sim-engine 的 `collection.worker_gpus` 控制。

nav-train 继续持有模型 profile、每端点槽位、有界模型队列和 PointNav 标签逻辑。
多个 nav-train 进程共用模型端点时，应分配各自的槽位预算；服务器容量全局共享，
模型槽位不是跨客户端的全局限流。当前默认配置没有经过同条件吞吐压测。

自由任务模型只看当前四图；R2R 模型只看局部指令和当前四图。目标、参考路径及
验收指标不进入模型请求。自由任务沿用画框主线、当前可见地面和框内调整规则，
没有 8m 上限。服务在移动前保存 `selected-goal.json`，nav-train 据此生成
`pointnav-target.json`、`pointnav-outcome.json`、`pointnav-frames.jsonl`，目标不会
被实际停止点替换。原始 RGB-D/状态/动作以链接保留，避免再次复制。

任务、服务、worker 都有生成的所有权身份。任务与目标提交不自动重试；HTTP
响应丢失、服务崩溃或移动结果不确定时保留租约与尝试证据。相同 ID 的完成结果
可读取，换内容会冲突。服务重启不会接管旧的不完整任务。客户端硬退出后仍在
等待目标的任务，需要按 ID 显式取消以释放容量。

每次采集都应冻结两个仓库的代码、配置、资产来源与许可证。服务健康响应和
任务回执记录其实现身份；客户端在 `started.json` 中保存服务身份。所有输出
仍是 `audit_only` / `pending_review`，不自动导出到训练。

## 2026-11-14 落地验证

A100 上完成两条自由轨迹和一条 R2R 的真实 Habitat＋现有模型联调，三条均为
`completed_audit`，无碰撞，原始 RGB-D/状态/动作完整。最终距离误差分别为
3.08 cm、1.65 cm、0.57 cm；两个自由任务的实际停止均投影在原框内，PointNav
目标与逐帧标签齐全。一个 worker 跨三条轨迹及一次只读几何查询，场景只加载一次。
两条自由任务总耗时 14.14 s，R2R 单条 4.68 s；这是小样本功能验证，没有进行
同条件吞吐对照，不能据此推算全量速度。测试用首版 R2R manifest 的步数参数
曾在提交前被配置一致性检查拒绝，修正后使用新 owner/目录执行，没有重试移动。

首次基础服务在 A100 `http://127.0.0.1:18812` 就绪时，配置为4个常驻 worker，GPU
0/2/3/4，共享容量128。服务 owner 为 `d31e84004b294d1899037309e9399738`，
冻结部署位于 `/data/nav_storage/sim-engine/deployments/collection-service-9e46a1f4f130`。
单 worker 联调服务已按 owner/PID/启动时间校验后关闭；其他服务未启停。

新主线快照已准备，尚未启动：
`/data/nav_storage/nav-train/collections/short-instructions-server-mainline-20261114-109eac84a87e`。
含16个起始视角、两条主线配置与当前冻结源码；旧快照和已完成数据未更改。

验证：sim-engine **89 passed**；nav-train **1140 passed、12 skipped**；harness
**311 passed、4 subtests passed**；相应 Ruff、mypy 与前端 build 通过。
CPU 测试的显式假后端注入仍保留，真实生产默认入口只走服务。
详细记录：[原生联调](nav-train/docs/evidence/collection-service-20261114.native.json)、
[服务健康与实现身份](nav-train/docs/evidence/collection-service-20261114.health.json)、
[验证及部署目录](nav-train/docs/evidence/collection-service-20261114.validation.json)。

## 场景缓存扩容

上一版服务将每个 worker 的场景缓存从1个增加到4个，四个 worker 最多保留16个
场景/相机实例，按需加载，超限按 LRU 关闭旧实例。worker 数量和执行并发不变。
同一场景可能出现在多个 worker 中，16是实例容量，并不保证16个不同场景常驻。

A100 上用四个主线训练场景实测：单 worker 保留四场景时 RSS 为4.31 GiB，
GPU显存增量约4.11 GiB；四场景分别切回后，四视图像素完全一致，没有重新加载。
缓存命中的四视图观察和写入耗时0.258–0.326 s，首次为4.38–8.86 s。
扩容前主机可用内存约736 GiB，GPU 0/2/3/4各剩约22–25 GB显存，当前余量足够。
这是四个场景的实测，不代表所有场景的峰值或完整采集吞吐。

该版冻结部署为 `/data/nav_storage/sim-engine/deployments/collection-cache4-dcc923dc5e55`；
端口18812、共享目录和客户端配置不变。空闲旧服务按所有权检查后关闭，历史冻结
快照和完成记录保留。扩容没有发起模型请求或移动。sim-engine 的91项测试和Ruff通过。
详见 [缓存容量实测](nav-train/docs/evidence/collection-cache4-20261114.native.json)。

## 当前配置：16个 worker，每个缓存1个场景

按用户的并发需求，服务已调整为16个独立 worker，GPU 0/2/3/4各4个，每个缓存1个
场景。仍最多保留16个场景实例，但仿真操作并发从4提升到16；不要求16个不同场景。
sim-engine 会优先把请求交给已缓存该场景的空闲 worker，缓存命中的 worker 忙时
允许其他 worker 加载该场景并行处理。数值库线程限制为每进程1个，避免线程过量。

真实测试的16个实例合计 RSS 约18.63 GiB，四卡显存增量分别约2.01/3.47/4.81/4.17 GiB，
各卡仍有17.52–22.33 GiB空闲显存，当前资源足够。测试覆盖四个主线训练场景、16个
起始视角，并观察到16路同时渲染。首次16条观察共25.67秒；重复一批共0.529秒，
全部缓存命中且四视图像素一致。这不是完整轨迹/模型调用吞吐测试，也不是所有
场景的峰值内存上界。两批观察均在移动前取消，没有模型请求或训练导出。

服务 owner：`ddf3f404c6b64875822dbcfbfd110e67`；冻结部署：
`/data/nav_storage/sim-engine/deployments/collection-workers16-1a0dddb328aa`。
端口18812、共享根目录 `train-v1`、队列容量128和客户端配置不变，原准备快照可继续用。
sim-engine 93项测试、Ruff及4项 nav-train 客户端测试通过。
证据：[原生测试](nav-train/docs/evidence/collection-workers16-20261114.native.json)、
[资源与部署摘要](nav-train/docs/evidence/collection-workers16-20261114.validation.json)、
[服务健康](nav-train/docs/evidence/collection-workers16-20261114.health.json)。

## 两主线并发端到端验证

当前16 worker服务已实测两条主线同时运行：16条R2R全部完成，16条自由任务完成13条、
移动前剔除3条，合计31.53秒；32次模型调用全部完成，无碰撞、崩溃或超时。
684帧录制和13条自由任务的252帧PointNav标签通过校验。仿真操作并发峰值16、模型
请求峰值18，但实际rollout峰值为6，尚未验证打满16路轨迹执行的稳态吞吐。
剔除样本中有一例框内仅6个安全像素被网格漏采，详细原因和范围限制见
[并发端到端测试报告](collection-concurrent-e2e-20261114.md)。

固定16个不同场景的1024次自由指令尝试已完成，耗时18分38秒、750条几何与录制
完成，调度换景0次，异常后同场景重建2次。静态批次后半段受规划长尾和空闲worker
影响；下一步建议验证滚动场景窗口。配置、失败分类及十万条耗时更新见
[固定16场景吞吐实验](fixed16-scenes-throughput-20261114.md)。

## 滚动场景调度

sim-engine现支持冻结的 `collection.scene_schedule`：每项是dataset、scene_id、camera
及attempts预算。预算计入全部已接受尝试，包括后续剔除、取消或失败；不按成功数
自动补齐。一个场景预算完成，且观察、等待目标、执行任务全部排空后，由服务将
同一个worker绑定到下一场景，无整批等待。worker死亡不迁移或重放其任务。

nav-train共用HTTP客户端读取服务当前窗口，按场景轮转准入；
`collection_service.per_scene_pending` 默认8，另受全局 `max_pending` 限制。
客户端候选必须覆盖整个尚未开始的冻结schedule，逐场景数量匹配attempts；一个
控制器独占该schedule。场景切换、worker进程与缓存均由sim-engine管理。
两条主线复用同一准入逻辑；默认18812服务没有自动升级或改配置。

已完成61场景、相同976个起点/朝向的16/32 worker滚动对照，32 worker完整耗时
9分10.9秒、几何完成产出4489条/小时，较16 worker提高29.1%；资源余量足够。
配置、失败与标签审计、完整曲线见[滚动窗口实测](rolling-workers-throughput-20261114.md)。

全量自由指令已按100000条几何/录制完成的成功配额启动，使用新NavMesh起点池、
有限备用候选和32 worker滚动窗口。启动身份、进度路径、磁盘保护和请求证据格式见
[十万条生产记录](free-instruction-production-100k-20261114.md)。

## 独立规划服务（2026-11-14）

规划池、NavMesh资产包、HTTP部署、计算预算及进程管理都实现在sim-engine。
nav-train仍只负责候选、模型提案、回执读取和训练标签派生，无新增规划/仿真依赖。
自由任务和R2R共用异步阶段；`planning_budget_exhausted`是独立终态，并保留
`planning_outcome`。派生目录链接规划回执与一次性执行凭据以便追溯。

滚动窗口中单个仿真worker不可用时，客户端继续健康场景，并把故障场景的未领取
候选保留为未开始。只有所有worker不可用或服务身份变更等情况才中止该客户端。
停止收尾不再为未提交的几十万个备用候选创建目录。

部署与原生验证见[sim-engine规划服务](../../sim-engine/docs/planning-service.md)。
本轮只实现及隔离测试，没有重启旧十万条生产。

实现验证：sim-engine 130 passed，nav-train 1147 passed / 12 skipped；
A100隔离原生链路与旧长尾预算退出验证通过，见[验证证据](nav-train/docs/evidence/planning-split-20261114.validation.json)。
