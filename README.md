# nav-data

导航专家数据生产：采集编排、任务提案与落地、Episode 压缩、冻结发布（release）。

本仓库只产出**与模型无关的物理事实**：轨迹、位姿、媒体、指令、来源与审核状态。它不选帧、
不做图像预处理、不编码训练标签、不训练模型。这些属于 [nav-train](../nav-train)；
仿真、规划与采集服务属于 [sim-engine](../sim-engine)。

## 职责边界

| 仓库 | 负责 |
|---|---|
| sim-engine | 世界怎么运行：仿真、规划、采集服务（`serve-collection` / `serve-planner`） |
| **nav-data** | 采到了什么：提交采集任务、模型标注、落地与录制检查、压缩、清点，产出冻结 release |
| nav-train | 模型怎么学：历史选帧、图像预处理、prompt、标签编码、split、SFT 导出、训练与评测 |

两仓之间只有一份接口：冻结 release 目录。nav-train 的 `nav_train.episodes.inventory_rows`
按 manifest 哈希读取它，除此之外不 import 本仓库任何代码。格式见
[release-format](docs/release-format.md)。

## 组成

```
src/nav_data/
  records.py            创建即不可覆盖的 JSON 记录与内容哈希
  io.py                 发布数据使用的规范化序列化
  collection/
    client.py           sim-engine 采集服务的 HTTP 客户端（提交、模型标注、回执落地）
    pool.py             多端点有界模型池与 worker 握手
    admission.py        按服务端场景窗口做客户端准入
    distribution.py     从转换根导出规范化行
  tasks/
    region.py           画框任务提案、深度反投影
    instruction.py      执行指令标注
    short_instructions.py  短指令任务提案
    pointnav.py         PointNav 目标、逐帧目标、候选结果落盘
    recording.py        录制完整性检查（转换前置条件，不是训练准入）
  planner_chunks/
    annotation.py       规划片段标注
    pool.py             多端点规划片段标注队列
  release/
    compact.py          episode 压缩与 schema 校验
    evidence.py         模型请求证据的紧凑保存与还原
    inventory.py        冻结发布：清点、质量审计、manifest
  cli.py                所有生产阶段的单一命令行入口
ops/compaction_runner.py 128 目录并发压缩运行器
annotations/planner_chunks/ 冻结的任务配置、协议与夹具
```

## 使用

```bash
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -e '.[dev]'

.venv/bin/nav-data --help
.venv/bin/nav-data planner-chunks --cases X --config Y --protocol Z --output DIR
.venv/bin/nav-data planner-chunk-pool --cases X --config Y --protocol Z --output DIR
.venv/bin/nav-data extract --conversion-root R --output DIR
.venv/bin/nav-data inventory --conversion-root R --owner OWNER --output DIR
.venv/bin/nav-data inspect-episode --inspect META.json [--full-media]
```

```bash
.venv/bin/ruff check src tests ops
.venv/bin/python -m pytest -q
```

## 硬规则

- 完成即不可覆盖：`records.write` 只创建不覆盖，重试不能替换已有回执引用的证据。
- 不自动重放：模型超时、worker 退出或服务重启时，未完成任务记为 `failed_or_uncertain`，
  证据保留。
- 结构检查不等于语义正确：指令正确性审核状态保持 `pending_review`，发布数据
  `training_ready=false`。
- 不放宽几何校验：画框提案的可见地面筛选只会在原框内收窄，不会扩张。
- 历史证据 `docs/evidence/` 不迁移、不改写；本仓库文档用链接引用。

## 文档

- [release 格式](docs/release-format.md)：与 nav-train 的唯一接口
- [采集服务](docs/collection-service.md)
- [数据主线](docs/navigation-data-mainlines.md)
- [可见区域 PointNav](docs/visible-region-pointnav-20261114.md)
- [GS 数据生产](docs/gs-data-production.md)、[首次试点](docs/gs-data-production-pilot-20261116.md)
- [自由指令 v5 生产](docs/free-instruction-v5-production-20261115.md)
