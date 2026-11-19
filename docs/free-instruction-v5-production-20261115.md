# 自由指令十万条：v5 与32个仿真worker续产

本轮按用户要求采用32个仿真worker，使用已通过真实端到端测试的
`collection-v5` 规划主线补足总计100000条完整记录。
运行状态以 A100 生产根目录中的 `status.json` 为准；本文不是完成报告。

生产 owner：`fe7bf17ab7b64a138e254f4b3f4c13c3`。

## 旧任务交接

旧独立规划生产 owner 为 `07b292f5809149419d11209f5dc9116b`，
经所有权、PID启动时间和命令行核对后主动停止。客户端完成中断封存，
模型线程为0，旧32个仿真和24个规划进程均已退出。
旧运行的 `controller-finished.json` 中中断错误和退出码属于本次主动交接，
不表示一次未解释的worker崩溃。

旧任务最终有30239条客户端确认的完整记录；加上更早已核验的2227条，
合计继承32466条，新增目标67534条。沿用原冻结起点池与逐场景总配额，
扣除各场景已确认成功数；旧服务领取、客户端租约及诊断观测起点全部排除。
没有完成客户端PointNav确认的服务端回执不计入成功。

历史完成目录、配置、失败和未决运动证据保持原样；新任务使用新owner、
请求身份、部署与输出根，不重放旧运动，不自动重启。
`reconciliation.json` 保存逐条继承记录与结果、回执、PointNav目标和结果哈希。

## 实际配置

| 项目 | 配置 |
| --- | --- |
| 仿真 | 32个worker，GPU 0/2/3/4各8个 |
| 场景 | 61个train场景，32场景滚动窗口，每个worker保留1个场景 |
| 规划 | `collection-v5`，32个独立CPU进程，每进程缓存最多4个NavMesh上下文 |
| 审计 | 4个线程 |
| 模型 | 18110–18113，`onh-qwen27b-nvfp4`，各16槽，总64 |
| 背压 | 总在途128，每场景pending最多8 |
| 单规划预算 | 120秒、80000次展开、2000000次逻辑查询 |

服务与客户端源码逐文件匹配
[真实端到端测试](planning-e2e-performance-20261115.md) 中的优化版快照，
包括API 2的必需C++扩展。配置、源码、编译产物、继承数据清单和启动脚本哈希
在新部署内冻结；使用SSH直接启动，避免远程后台工具的单文件100MiB限制。

用户在启动前要求增加规划并发，最终采用32个规划进程。最初准备的24进程配置
保留用于追溯，实际启动使用 `server/config.planning32.json` 和
`production-plan.planning32.json`；`planning-capacity-adjustment.json` 记录调整及哈希，
该调整发生于模型请求和运动均为0的阶段。

继续使用四张原始RGB让模型生成指令与框，目标仅在原框内的可见安全地面选择，
没有8m轨迹距离上限。目标在运动前冻结，实际RGB-D、世界状态和动作按4Hz保存，
同时保存 `pointnav-target.json`、`pointnav-outcome.json` 和逐帧
`pointnav-frames.jsonl`。所有记录保持 `audit_only / pending_review`。

## 监控与资源

每10秒更新状态，每5分钟保存不可变检查点；只有累计继承数与新增完整PointNav
成功数达到100000，且逐场景配额核对一致，才写入 `completion.json`。

保留100GiB磁盘、每张工作GPU至少6GiB空闲、主机64GiB可用内存。
1200秒无确认成功产出、全部仿真容量不可用或触及资源线时停止并保留证据。
启动前数据盘空闲约850GiB，是否足够完成剩余产量需结合新任务实际空间消耗判断；
启动全量不表示已经证明存储足以容纳全部剩余数据。不会自动删除历史记录。

原18812服务保留，GPU 1/5的无关服务不控制。新版接替18813，不启动正式benchmark，
没有训练导出。最新资源与队列信息随 `status.json` 和 `monitor.jsonl` 保存。

2026-11-15 在生产持续运行期间，另行清理闲置实验媒体等29.33 GiB；正式数据和
来源依赖不变。历史端到端实验媒体已删除，JSON证据和冻结源码保留。
空间仍有缺口，详见 [清理结果及容量核验](a100-experiment-cleanup-20261115.md)。

## 启动核验

2026-11-15 02:40（北京时间）的启动快照为 `running`：新增完整记录287条，
累计32753条；32个仿真与32个规划worker全部存活。旧生产所属59个进程均已退出，
18812原服务身份保持。新服务owner为 `c4388b85c9d74898acb6cbedda6b98b6`，
规划服务owner为 `014bbaa024704a26bba7388ea86cef1a`。

02:42结束启动核验时，守护状态确认新增709条、累计33175条，32+32个worker仍全部存活，
控制器、服务和客户端的PID启动时间均匹配所有权记录。该数字是检查快照，实际产量继续增长。

首批独立抽查5条、96帧：实际录制与逐帧PointNav变换通过，冻结目标一致，
零碰撞，停止点在原框内，规划摘要和一次性执行凭据对应，模型四图可无损还原。
全部74个服务源码/编译资源与154个客户端源码资源匹配端到端测试快照。
这是录制和几何核验，语义审核仍为 `pending_review`。

早期失败样本中，一条运动后的无效深度保留原始证据；另7条模型框超过原协议的
小区域限制，目标提交返回HTTP 422后在运动前取消。对保留提案调用冻结的
`validate_bbox` 均复现该限制；没有修改框、补跑模型或重试运动，没有worker丢失。

在初始32个活动场景各取一条已完成记录，实际分配空间均值10.85MiB、中位数
10.03MiB。该早期成功样本不含失败尝试和共享开销，不能保证剩余容量足够完成全量。
当时空闲约844GiB，扣除100GiB保留线后余量偏紧；守护程序继续按实际磁盘余量判断。
规划池的启动采样未见持续排队，尚不以这段冷启动窗口承诺全量吞吐。

[最终生产计划](nav-train/docs/evidence/free-instruction-v5-production-20261115/production-plan.planning32.json)、
[交接与源码匹配](nav-train/docs/evidence/free-instruction-v5-production-20261115/handoff-validation.json)、
[首批独立核验](nav-train/docs/evidence/free-instruction-v5-production-20261115/launch-audit.json)、
[启动验证](nav-train/docs/evidence/free-instruction-v5-production-20261115/startup-validation.json)、
[运行最终检查](nav-train/docs/evidence/free-instruction-v5-production-20261115/launch-final-check.json)、
[空间样本](nav-train/docs/evidence/free-instruction-v5-production-20261115/startup-storage-sample.json)、
[失败分类](nav-train/docs/evidence/free-instruction-v5-production-20261115/startup-rejection-classification.json)。

## 路径

生产根：
`/data/nav_storage/nav-train/collections/free-instructions-100k-v5-20261115-fe7bf17ab7b6`。

原始产物：
`/data/nav_storage/sim-engine/collections/free-v5-fe7bf17ab7b6`。

冻结部署：
`/data/nav_storage/sim-engine/deployments/free-v5-fe7bf17ab7b6`。

- `production-plan.planning32.json`：实际启动的剩余配额、候选上限与并发配置。
- `production-plan.json`：未启动的24规划进程准备版本，保留追溯。
- `reconciliation.json`：历史成功信用、排除依据与逐条哈希。
- `handoff-validation.json`：与端到端测试快照匹配及旧任务停止证据。
- `launch-provenance.json`：冻结计划、配置来源和启动脚本哈希。
- `status.json` / `monitor.jsonl`：实时状态与历史监控。
- `launch-audit.json`：首批独立录制、PointNav和执行凭据核验。
- `completion.json`：仅在补足100000条后生成。

旧任务说明见 [独立规划生产交接前记录](free-instruction-planning-production-20261114.md)。

## 后续停止检查（2026-11-15）

守护状态现为 `stopped_requires_inspection`。本轮确认完成12073条，加上继承32466条，
累计44539条，距100000条还差55461条。客户端中断记录为
`TimeoutError('timed out')`，控制器因目标未完成保留冻结摘要后退出。
没有证据确定超时发生在哪一次RPC；不能将其表述为已经查明的磁盘或规划故障。
当时磁盘仍有约798 GiB可用，未触发100 GiB保留线。

服务端曾报告12140条成功，比客户端确认数多67条；这些差异暂不计入产量。
全部未决提交、租约、运动和回执证据保留，未重试旧请求，尚未启动续跑。
后续Janus原始下载清理将可用空间提高到约1.11 TiB，详见
[清理与容量记录](a100-experiment-cleanup-20261115.md)。

[停止快照](nav-train/docs/evidence/free-instruction-v5-production-20261115/stopped-status-20261115.json)、
[客户端中断原因](nav-train/docs/evidence/free-instruction-v5-production-20261115/client-interrupted-20261115.json)。

## 超时定位与独立续产准备（2026-11-15）

会话续接后核对最后一次提交 `s24-p00486`：客户端写入提交证据后30.053秒
记录超时，服务端在提交后30.965秒才持久化同一request_id的请求，最终回执为
`cancelled_before_movement`。该时间关系强烈支持任务POST等待超时后服务端迟到领取；
旧日志没有原始调用栈，尚不能证明底层延迟的具体原因。核验时另观察到ext4数据盘
IO等待，不能据此认定它就是此前超时的原因。

客户端新增独立的 `collection_service.http_timeout_s`，默认仍为30秒，续产显式设为
120秒。超时错误包含HTTP方法、路径、实际等待时间和配置超时，运行中断证据保存
调用栈；没有增加请求或运动重试。模型请求超时仍为180秒。
相关客户端、并发和PointNav测试共47项通过。

新生产owner：`eff250bfbefc41c786e99ce17bea0bca`。
新生产根：
`/data/nav_storage/nav-train/collections/free-instructions-100k-v5-resume-20261115-eff250bfbefc`。
新部署：`/data/nav_storage/sim-engine/deployments/free-v5-resume-eff250bfbefc`。
继续采用32仿真、32规划、64模型槽。按原逐场景配额核对旧客户端成功、原始回执、
冻结PointNav目标、4Hz录制和逐帧标签；旧服务已领取及客户端持有租约的全部起点排除。
新身份只领取未使用起点，不重放旧运动，不把缺少客户端PointNav确认的服务端成功计入。

续产已启动。04:46北京时间的启动快照为 `running`：本轮新增140条，累计44679条；
32个仿真worker和32个规划worker存活，控制器、服务、客户端的所有权、PID启动时间
及命令行核对通过。磁盘约1.11TiB可用，原18812服务身份及空闲状态保持。
实时产量以新生产根的 `status.json` 为准；原生产及全部subtask数据保留。

启动独立抽查5条、114帧通过：完整RGB-D录制与4Hz、逐帧PointNav坐标变换、冻结目标、
规划摘要、一次性执行凭据和模型四图还原均通过；5条均无碰撞且停止点在原框内。
该检查不代替语义审核，全部数据仍为 `audit_only / pending_review`。

新运行仍出现一次15秒健康读取超时，随后守护恢复成功读取且产量继续增加；
不能据此承诺底层延迟已消除。监控状态中的旧 `health_error` 字段可能保留前一轮错误。
早期模型失败是目标提交HTTP 422，未重试运动。资源线、连续健康读取失败和无产出
停止规则继续生效，不自动重启。

[超时证据](nav-train/docs/evidence/free-instruction-v5-resume-20261115/previous-timeout-diagnosis.json)、
[测试记录](nav-train/docs/evidence/free-instruction-v5-resume-20261115/validation.json)、
[继承核验摘要](nav-train/docs/evidence/free-instruction-v5-resume-20261115/reconciliation-summary.json)、
[启动验证](nav-train/docs/evidence/free-instruction-v5-resume-20261115/startup-validation.json)、
[首批独立核验](nav-train/docs/evidence/free-instruction-v5-resume-20261115/launch-audit.json)、
[续产核验脚本](nav-train/docs/evidence/free-instruction-v5-resume-20261115/prepare_free_v5_production.py)、
[守护脚本](nav-train/docs/evidence/free-instruction-v5-resume-20261115/supervise_free_v5_planning32_production.py)。

## 磁盘等待与HTTP循环隔离后的交接（2026-11-15）

续产检查进一步观察到服务主线程等待ext4日志提交和磁盘页读取。采集服务及内嵌
规划池在事件循环中同步落盘的路径已改为有界线程池，保持领取、冻结目标、执行
派发、终态回执与场景切换的原有证据顺序。取消协程会等待已提交I/O结束，防止迟到
写入越过封存；新增 `claiming` 状态避免把领取中的半写入目录误报为旧生命周期失败。
健康检查暴露 `evidence_io`，区分磁盘积压和worker退出。

完整回归196项及Ruff通过；两项相同故障注入测试在旧版上导致健康读取超时，修复版
通过。A100独立运行四项故障测试通过，未调用模型或仿真GPU。详细证据见
[sim-engine采集服务记录](/home/holo/robots/sim-engine/docs/collection-service.md)。

在上述验证通过后主动停止 `eff250bfbefc41c786e99ce17bea0bca` 任务，核对owner、PID
启动时间与argv；旧任务模型线程为0，采集服务进程组已清空。旧运行最终客户端确认
完成2164条，与继承44539条合计46703条。全部失败及未决运动保留，不自动重放。

新owner：`6ca6ed773bdb40eea8a023c2127b1e01`。
新生产根：
`/data/nav_storage/nav-train/collections/free-instructions-100k-v5-io-20261115-6ca6ed773bdb`。
新部署：`/data/nav_storage/sim-engine/deployments/free-v5-io-6ca6ed773bdb`。
完成继承哈希、PointNav逐帧标签和场景配额核对，补足目标53297条；继续采用
32仿真、32规划、64模型槽、100GiB磁盘保留线，不自动重启，不做训练导出。
监控成功读取后会清除上一轮健康超时字段，避免将历史错误误当成当前故障。

新任务已启动。启动验证快照累计46914条，本轮新增211条，32仿真和32规划存活。
连续30次独立健康读取全部成功，最长115.6ms；首批5条、105帧录制及PointNav核验通过。
上述为启动窗口观测；实时产量以新根目录的状态及审计文件为准。
此次修复隔离已定位的故障传播路径，不承诺磁盘永久不可用时生产仍能继续。

## 09:44停止：inode耗尽（13:01北京时间核查）

当前生产已停止，不能将最后 `status.json` 中的 `running` 当成实时状态。
控制器与客户端原PID均已退出，18813拒绝连接，没有 `completion.json` 或
`controller-finished.json`。

最后控制器快照为09:44:11、累计92114条；稍后的客户端进度文件为09:44:17，
确认本轮45464条，加继承46703条，累计92167条（92.17%），剩余7833条。
这是最后客户端进度报告；尚未逐条复核退出时的所有产物，未决记录不计入完成。

控制器、客户端和采集服务日志均出现 `Errno 28: No space left on device`。
13:01检查确认 `/data/nav_storage` 所在文件系统45774848个inode已全部使用，
可用inode为0；当时仍有506765086720字节（约472GiB）可用空间。
现有磁盘保留线只检查可用字节，没有检查inode，因而没有提前停止；异常记录、
终态回执及停止摘要也因无法创建新文件而失败。日志保留了退出堆栈。

停止前1642次守护监控中没有健康读取超时。此次已查明的停止触发条件是inode耗尽；
既有I/O隔离修复不解决文件数量上限。未删除任何数据、未重放旧运动、尚未重启。
续产需要先补足inode容量并增加inode保留线，再核对退出时产物和剩余场景配额。

## 先释放inode，再转换成功episode（2026-11-15）

用户确认精简方案后，先清理六个显式命名的旧仿真实验任务目录，不处理正式生产、
subtask或其他数据集。删除73478个旧实验文件和7461个目录，释放80939个inode，
按删除文件原分配块计约0.94GiB。旧实验任务身份和清单合并保存在根分区的审计目录，
配置、源码、许可证及实验级摘要保留；确认候选任务无活动进程引用且不与生产继承记录相交。

清理审计：`/home/sari/robots-maintenance/inode-headroom-945fd73261c4`。
转换owner：`0d9c29af2cbd451697ad470c9d972e71`。
转换控制与日志：`/home/sari/robots-maintenance/episode-compaction-0d9c29af2cbd`。
控制文件放在另一文件系统，避免数据盘inode耗尽时再次丢失错误记录。

精简后的成功episode保留原地 `rgb/`、`depth/`、四张初始PNG及一个 `meta.json`。
必要相机参数、实际状态/动作、PointNav标签、冻结目标、指令与结果并入该文件；
规划候选、mask、预演路径、重复中间产物和阶段文件删除，客户端软链接与空case目录移除。
逐帧媒体字节不重编码，状态与PointNav值不重新生成，审核状态仍为 `pending_review`。
每个client根新增一个 `episodes.sqlite3`，保存case到规范数据的映射和公共模型请求模板。
这个数据库是最终数据的一部分，必须随数据保留，不是可清理缓存。

每批先校验客户端/服务端成功、原始目标、实际动作、标签与帧数，再写出并回读元数据、
提交并同步索引，之后按已核对inode/mtime的白名单删除旧产物。历史46703条继承数据
同时核对原冻结清单中的结果、回执、PointNav目标和结果哈希。未完成或未决记录保留。
64条完整验证、32条并发验证、8条公共模板验证累计104条真实episode通过；
验证覆盖逐帧RGB-D哈希与格式、PointNav、原始四图以及模型请求的精确还原。

批量任务已启动，按64条一批处理。批量阶段检查完整媒体文件集合并保留原哈希，
不重复解码和重哈希所有未修改的媒体；小批验证进行了完整的媒体读回。
进度见转换控制目录的 `bulk-progress.json`，仅 `bulk-result.json` 和
`conversion-finished.json` 成功后才视为本轮转换完成。没有启动新的采集或训练任务。

新读取入口为 `nav_data.release.compact.open_episode(client_root, case_id)`，
可以读取元数据、通过 `media_path()` 定位逐帧媒体，并通过 `model_request()` 还原原始输入。
`nav_data.tasks.recording.validate_recording` 和PointNav行读取支持该格式。
旧冻结脚本依赖的单独JSON/软链接路径会被退休，后续续产核对应使用新索引和原始哈希字段。
新采集客户端提供 `compact_completed_episodes` 开关；sim-engine源码新增独立inode保留线，
后续冻结部署需显式采用这些更新。

14:22北京时间快照：转换目标共92167条，累计转换2770条（含104条验证样本），
数据盘可用inode已恢复到274559个。批量控制器及转换进程正常，未写出错误或完成标记；
全量尚未完成，后台任务继续按批提交并在最后核对索引完整性及冻结配置。

[清理结果](nav-train/docs/evidence/episode-compaction-20261115/headroom-result.json)、
[验证记录](nav-train/docs/evidence/episode-compaction-20261115/validation.json)、
[转换进度快照](nav-train/docs/evidence/episode-compaction-20261115/progress-snapshot.json)。

14:52按用户要求直接切换128个工作线程，不继续做并发档位对比。旧4线程转换在核对
PID及启动标识后以SIGINT受控退出；旧目录的`bulk-exit.json`为-2及对应错误摘要
属于此次显式交接。新任务从SQLite已提交的9106条接续，剩余83061条，每批512条。
转换模块沿用已经验证的相同源码，仍先落盘元数据、提交索引，再删除核准旧文件。

新owner：`6354849a64ae4afbaf4090fc0ddaa019`；运行目录：
`/home/sari/robots-maintenance/episode-compaction128-6354849a64ae`。
后续查看该目录的`progress.json`、`error.json`及`result.json`，其中`result.json`
通过最终索引完整性及冻结配置核验后才表示转换完成。进程独立于终端会话运行。

14:54核验实际129个OS线程（128工作线程和主线程），新任务首批512条已完成，
累计9618/92167条；可用inode为890879个，任务继续运行且没有错误标记。
本次只切换已授权转换的并发设置，未重启采集。
[128线程进度与进程快照](nav-train/docs/evidence/episode-compaction-20261115/conversion128-snapshot.json)、
[接续脚本](nav-train/docs/evidence/episode-compaction-20261115/run_conversion128.py)。

15:05按用户要求切换为64个进程worker，每批仍为512条。旧128线程进程在验证
PID/启动标识/命令行后SIGINT退出，旧目录的`handoff-to64proc-request.json`及
`error.json`中的KeyboardInterrupt记录此次显式交接。新任务从已提交的13202条
接续；交接前已发布但未入索引的元数据通过现有恢复流程复用。

新owner：`7c5cc48cee814b19bc42ad1d97937d16`；运行目录：
`/home/sari/robots-maintenance/episode-compaction64proc-7c5cc48cee81`。
使用持久的`ProcessPoolExecutor(max_workers=64)`和spawn上下文；同一池执行准备、
写出及退休阶段，SQLite提交和审计仍由协调进程串行完成。转换模块新增可传入执行器
及可序列化入口，并显式回传worker内的元数据创建标记，使inode统计在跨进程时正确。
远端AST核对确认原准备、发布、退休、标签/媒体校验及保留规则函数未改变。
12项转换测试通过，包含两个spawn进程集成测试，覆盖提交顺序、恢复、池复用、
跨进程inode统计和校验失败保留源文件；Ruff检查通过。

15:06已核验64个独立worker进程，每个1个线程（另有协调进程及Python资源跟踪进程）。
首批512条完成，累计13714条。首批包含交接前已发布元数据，不用该批估计稳定提速。
后续进度以新目录的`progress.json`、`error.json`、`result.json`为准。
[进程及进度快照](nav-train/docs/evidence/episode-compaction-20261115/conversion64proc-snapshot.json)、
[验证记录](nav-train/docs/evidence/episode-compaction-20261115/conversion64proc-validation.json)、
[64进程接续脚本](nav-train/docs/evidence/episode-compaction-20261115/run_conversion64proc.py)。

15:08:46快照：64进程首个正常批次512条耗时138.70秒，即3.69条/秒；已完成两批
共1024条，累计14226/92167条，可用inode为1305599。首批交接恢复不计入速度。
切换前128线程最近四批观测为5.90条/秒；当前正常批次样本仅一批、episode不同，
这不是严格同数据对照。初步观测未见提速，任务按用户指定64进程继续运行，无错误标记。
15:07一次3秒采样中64个worker均处于磁盘等待，主要等待文件系统日志提交及写入；
没有据此修改并发数或落盘、校验规则。


## 128进程持续队列（2026-11-15 15:27北京时间）

按用户要求改为128个进程，并取消512条整批等待。旧64进程任务在完成当前批次后
受控退出，已提交18322条，旧进程组全部退出。新任务使用同一格式、同一逐条校验、
发布和删除函数；仅将SQLite提交代码抽为复用函数，新增持续队列调度。

新owner：`62774debc6e14413902d56b125e06e35`；运行目录：
`/home/sari/robots-maintenance/episode-compaction128proc-62774debc6e1`。
128个worker共享持续任务队列，最多256条episode在途。准备、发布、退休按完成事件
推进，完成一条即补充一条，不等待整批或最慢任务。协调进程是唯一SQLite写入者，
只合并当前已完成的发布；每条仍须先验证和同步元数据，再提交及同步索引与审计，
最后才派发删除原文件。停止时停止领取新条目，排空已经进入队列的条目。

本地40项相关测试和远端18项转换测试通过，Ruff通过。阻塞第一条准备任务时，
第二、第三条仍可完成转换并补充队列；故障注入覆盖发布失败、索引提交失败、
原冻结哈希不匹配及提交后中断恢复，未提交的源文件保留。远端AST核对18个原函数
及类未变，逐帧媒体仍不重编码。

首次队列运行完成512条至18834条。启动检查发现NumPy自动创建每进程64条内部线程，
因此排空队列后封存该次日志、计划、索引提交/删除审计与进程记录于
`attempt-before-thread-limit/`，显式设置数值库线程数为1后重新启动同一128进程队列。
该次主动交接没有丢弃审计，也不是自动错误重试。当前运行从索引中的18834条继续。
后续状态看当前目录的`progress.json`、`error.json`和`result.json`，只有最终通过索引
完整性与冻结配置核验的`result.json`表示全量转换完成。采集尚未重启。

[128进程队列脚本](nav-train/docs/evidence/episode-compaction-20261115/run_conversion128proc.py)、
[验证记录](nav-train/docs/evidence/episode-compaction-20261115/conversion128proc-validation.json)。

15:28:11启动核验：实际128个spawn worker、每个1个OS线程，已从18834条继续
完成至18836条，无错误标记。队列内已有发布和退休在并行进行，该冷启动快照不用于
估计稳定吞吐。[运行快照](nav-train/docs/evidence/episode-compaction-20261115/conversion128proc-snapshot.json)。


## 单队列、完整episode任务、128个进程（2026-11-15 15:31北京时间）

用户进一步明确：一个任务队列，每个worker取完整episode并独立做完。因此将上节
阶段调度退休；旧任务排空后累计19603条，旧进程组已退出，全部日志与审计保留。
新任务的队列项是完整episode，worker依次校验、发布元数据、提交索引、退休旧产物，
全部在同一进程中执行，再领取下一条。主进程只投递任务和统计完成结果。
所有来源根的待转换episode进入同一队列，不按来源根设置转换屏障。

新owner：`26a4b3c6c87c4fdca920267c461aaf58`；运行目录：
`/home/sari/robots-maintenance/episode-singlequeue128-26a4b3c6c87c`。
实际128个进程，共享任务队列最多256条在途；每进程数值库线程限制为1。
SQLite通过`BEGIN IMMEDIATE`与自身写锁串行化短事务，主进程不再代交索引。
每个worker单独追加并同步`worker-audit/worker-PID.jsonl.gz`，每条仍严格先保存
元数据、同步索引与提交审计后再删除源文件。源文件白名单、身份检查及媒体保留规则未改。

本地41项相关测试及A100的19项转换测试通过，Ruff通过。新增8个spawn进程同时转换
两个来源根的16条episode，验证相同case名称跨根不混淆、索引完整、单条三个阶段
均由同一进程完成，以及各worker审计顺序；发布、提交失败与提交后中断恢复继续通过。

后续检查新目录的`progress.json`、`error.json`、`result.json`及worker审计；最终完成
仍须通过92167条索引总数、完整性和冻结配置核验。采集仍未启动。
[单队列脚本](nav-train/docs/evidence/episode-compaction-20261115/run_conversion_queue128.py)、
[测试记录](nav-train/docs/evidence/episode-compaction-20261115/singlequeue128-validation.json)。

15:31:54核验实际128个worker，每个1个OS线程，128份独立worker审计日志；
新任务已完成52条，累计19655/92167条，无错误标记。
[启动快照](nav-train/docs/evidence/episode-compaction-20261115/singlequeue128-snapshot.json)。


## 单队列SQLite读写锁修复（2026-11-15 15:35北京时间）

用户要求检查速度时发现上一单队列任务已停止，不能将旧progress里的running当作
当前状态。异常栈在prepare_episode调用open_episode读取索引时返回database is locked：
读取连接沿用了SQLite默认5秒等待，128个worker并发提交时出现读阻塞。旧任务启动
26.21秒的最后进度为19727条，不足以判断稳定提速。主进程因异常停止领取后，已运行
worker仍按正常顺序完成；最终逐根核对SQLite共19976条，完整性检查通过，没有已入
索引而未退休的条目。旧异常、元数据及各worker审计保留。

保留一个完整episode队列和128个worker，修复为启动投递前统一将各索引置为WAL，
使读取与写入并行；读取等待也显式设为60秒。worker继续用BEGIN IMMEDIATE和FULL
同步提交短事务，提交及审计完成后才删除源文件。全部任务结束后先checkpoint并恢复
DELETE journal，再做最终完整性核验；运行期间SQLite的-wal与-shm是索引状态的一部分，
不能脱离数据库单独清理或复制。

新owner：`872cd5d629d545bd9f4c38a923f33766`；运行目录：
`/home/sari/robots-maintenance/episode-singlequeue128-wal-872cd5d629d5`。
保留旧任务和冻结配置，从实际索引19976条接续，没有重放模型或运动。
本地42项相关测试通过；A100使用实际128个spawn worker，转换两个根共256条测试
episode，20项转换测试通过。新增回归在另一连接持有写事务期间读取已提交episode，
以及全部连接关闭后checkpoint并移除WAL，Ruff通过。
[修复后队列脚本](nav-train/docs/evidence/episode-compaction-20261115/run_conversion_queue128_wal.py)、
[修复验证](nav-train/docs/evidence/episode-compaction-20261115/singlequeue128-wal-validation.json)。


## 单队列写事务显式互斥（2026-11-15 15:39北京时间）

上一WAL版本解决了原先读连接5秒超时，但两分钟实测期间，worker在BEGIN IMMEDIATE
等待写事务超过60秒后仍退出。因此不能将之前的启动测试或初期速度表述为稳定运行。
退出后完整性核对确认共20455条已入索引、无已入索引而未退休的条目；旧错误、
日志、worker审计及速度采样保留。两次异常均未重放模型、规划或实际运动。

保持一个完整episode队列和128个worker。通过spawn初始化参数共享一个
multiprocessing.Lock，在worker内部仅围住索引连接、事务提交和目录同步；
其余校验、发布、审计和删除继续并发。这将写入者的排队放在共享互斥锁上，避免
128个连接同时争抢SQLite写事务而饿死超时；并未恢复主进程阶段调度。
WAL仍用于允许索引读取与写入并行，全量完成时仍checkpoint并恢复DELETE journal。

新owner：`8f68e3b63060450d882a40637d8de80c`；运行目录：
`/home/sari/robots-maintenance/episode-singlequeue128-serial-8f68e3b63060`。
该版本从20455条继续，主进程仍只投递完整episode和统计结果。42项本地相关测试通过，
共享锁的本地转换测试20项通过；A100使用实际128个worker及共享互斥锁处理256条
测试episode，20项转换测试通过，Ruff通过。
[显式互斥队列脚本](nav-train/docs/evidence/episode-compaction-20261115/run_conversion_queue128_serial_commit.py)、
[验证记录](nav-train/docs/evidence/episode-compaction-20261115/singlequeue128-serial-validation.json)。


## 独立目录worker，SQLite索引移出转换路径（2026-11-15 15:52北京时间）

用户确认移除每条任务的共享索引操作。旧共享锁队列排空后累计22598条，全部旧进程
退出，审计及目录保留。新版128个进程只领取一个完整episode目录，独立完成原始
冻结哈希、媒体文件集合、实际状态/动作及PointNav校验，写入并读回meta.json，
同步发布审计后退休核准旧产物，再领取下一条。worker不读也不写SQLite，不使用
索引互斥锁；生产worker初始化时直接禁止sqlite3.connect，防止隐含数据库依赖。

新owner：`3c2cb91ae51a407dbe3ca1d5b3b4ddee`；运行目录：
`/home/sari/robots-maintenance/episode-directory128-3c2cb91ae51a`。
数据逐帧媒体、目标、PointNav值和保留规则不变。每个run提前将现有公共模型模板
保存为不可变request-templates.json，核对模板摘要和原请求还原；原SQLite仍保留。
目录meta.json与run级模板足以读取数据，不依赖尚未生成的索引。

投递前将基线索引快照、全部待处理目录及规范meta路径、原冻结哈希写入根分区的
catalog-baseline.json和tasks.jsonl，校验哈希并同步后才启动转换。即使客户端目录
已退休且结果尚未汇总，重启仍能按同一清单从meta.json恢复，不遗漏已完成记录。
worker返回小型索引行供主进程记入独立结果账本，但不等待索引写入。
全部队列任务完成后，主进程才按基线加结果一次性生成每个run的新SQLite索引，
检查唯一性、计数和完整性后原子替换旧索引；旧索引在此之前保持静态。结束时总数
仍须达到92167条且保护配置哈希一致，才写result.json。

运行中已完成条数以当前progress.json和目录清单为准，不能用旧SQLite的行数判断
转换进度。转换完成前新目录可以用Episode(meta_path)读取；旧case查询索引会在
最后统一更新。停止时排空已进入队列的目录并保留清单，索引可延后重建。

本地47项相关测试及1项完整控制器恢复测试通过；A100实际128进程下25项转换测试
及1项控制器恢复测试通过，Ruff通过。独立目录压力测试的worker内禁止SQLite连接，
两个run的256条episode仍可全部完成，原数据库字节不变，最后重建的索引可完整读取。
中断测试覆盖元数据发布后中断、已清理目录的重复恢复、模型请求精确还原，以及目录
全部清理但索引尚未生成时从清单恢复；失败索引重建不替换原数据库。

[独立目录队列脚本](nav-train/docs/evidence/episode-compaction-20261115/run_conversion_directory128.py)、
[验证记录](nav-train/docs/evidence/episode-compaction-20261115/directory128-validation.json)。

15:54:26核验：实际128个worker，每个1个OS线程，worker打开的SQLite文件数为0；
五个旧索引的inode、大小及mtime均与投递前快照一致。已从22598条继续完成841条，
累计23439/92167条，无错误标记。处理阶段80.01秒平均10.51条/秒；最近61.17秒
完成695条，即11.36条/秒。旧64进程正常批次平均约4.69条/秒，当前短时观测约
2.4倍，但episode不同、窗口较短，不作为全量速度承诺。
[独立worker与速度快照](nav-train/docs/evidence/episode-compaction-20261115/directory128-snapshot.json)。

### 独立目录转换已完成，开始训练数据清单核验

2026-11-15 19:01:59（北京时间）最终`result.json`已写入`completed / passed`：
92167条全部转换，五个run的SQLite索引最后统一重建并校验通过；运行时长约3小时9分。
`progress.json`保留完成索引前的最后一条`running`快照，完成状态以最终`result.json`为准。

用户随后明确选择“先完成数据清单与质量检查，样本接口稍后讨论”。新清单位于A100：
`/data/nav_storage/nav-train/dataset-preparation/free-subtask-inventory-20261115-c590dabe257c`。
五份catalog、来源模板及转换凭证已冻结，清单按request_id对齐全部92167条原成功集合。
全部61场景、2184847帧、2092680个控制转移，录制145.325小时；逐条元数据、目标、位姿、
动作、终止回执及媒体文件集合检查全部通过。分层抽取324条episode（覆盖所有场景和已有
目标方向、每场景最长录制），13742对RGB-D帧及1296张起点图的哈希和实际解码检查全部通过。
这只是媒体抽检，不代表全量媒体字节重新审计；语义状态仍为pending_review。

实际媒体能力已明确：起点四视图，执行时逐帧前视RGB-D。没有发现按记录位姿与媒体指纹
识别的精确重复记录；37300条额外重复指令文本不作为重复episode删除。全部split_assignment
仍为unassigned，样本接口、训练窗口和新的train/dev划分尚未确定，未启动训练。

本地[清单与质检报告](nav-train/docs/evidence/free-subtask-inventory-20261115-c590dabe257c/inventory/report.md)、
[独立交叉验证](nav-train/docs/evidence/free-subtask-inventory-20261115-c590dabe257c/independent-validation.json)。
独立验证逐条复现前次分布快照的身份、场景、帧数、原指令、固定目标及路径长度；五份源
catalog在整个检查期间SHA256未变化。新的检查器5项测试及静态检查通过，源码及输入清单
随证据冻结，原始记录未修改。


## 全部成功数据的分布与完整场景地图（2026-11-15）

用户要求在转换保持运行期间统计起始点、指令、实际轨迹与目标，并在每个完整场景
平面图可视化。统计使用转换开始前已冻结的成功记录清单，合计92167条；提取时
41781条为meta格式、50386条为旧格式，逐条核对冻结身份、起终点与实际帧序列，
旧文件退休造成的读取竞态会回读已原子发布的meta，不把失败或未决运动计入样本。
没有读取RGB-D媒体，没有调用模型、Simulator或重放运动，转换保持后台运行。

统计控制根：`/home/sari/robots-maintenance/free-distribution-20261115-484a46fcd393`。
本地成果：`docs/evidence/free-distribution-20261115-484a46fcd393/viewer/`；离线ZIP在其父目录。
全部61个场景使用固定NavMesh的完整三角形、岛屿及边界，不按已采集区域裁剪；
自动推断132个高度带供分图和交互筛选，高度带不是人工建筑楼层标签。
底图明确标注为完整可通行区域图，覆盖率以0.5m格中心近似，轨迹以0.2m间隔插值。

结果摘要：每场景334–3356条；实际轨迹中位数2.38m、P95为6.48m；目标水平距离
中位数2.36m、P95为6.15m，front目标视图占60.8%；指令中位18词、54867种完全
不同文本，按文本计超出首次出现的重复份额40.5%，并不表示重复episode。
完整可通行粗栅格的起点、目标及实际轨迹覆盖率分别为58.1%、49.2%、89.2%。
所有数据的语义审核状态仍为pending_review，不以关键词统计替代语义审核。

提取逻辑7项、地图与安全嵌入2项测试通过；61个交互页全部通过高度筛选、精确搜索、
轨迹选择、密度模式和空结果恢复检查；浏览器截图人工核验、全部链接和CSV行数、
离线包完整性检查通过。源清单哈希、NavMesh哈希、代码哈希及验证结果随成果保存。

[交互地图与总览](nav-train/docs/evidence/free-distribution-20261115-484a46fcd393/viewer/index.html)、
[61场景总览图](nav-train/docs/evidence/free-distribution-20261115-484a46fcd393/viewer/figures/scene_atlas.png)、
[统计报告](nav-train/docs/evidence/free-distribution-20261115-484a46fcd393/viewer/report.md)、
[离线包](nav-train/docs/evidence/free-distribution-20261115-484a46fcd393/distribution-viewer.zip)。

局域网查看：按用户要求使用systemd用户服务`nav-distribution-484a46fcd393.service`
托管上述viewer目录，绑定`192.168.66.221:8767`，访问
`http://192.168.66.221:8767/`。本机首页、场景页及图片HTTP检查通过，另从A100
验证首页HTTP 200。进程独立于聊天运行，服务记录在成果父目录`lan-server.json`。
