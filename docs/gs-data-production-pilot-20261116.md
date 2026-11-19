# GS 数据生产首批五条：2026-11-16

已完成 **5 条、109 帧 RGB-D**，覆盖智元 2 条、智园1 2 条、智园2 1 条。
GPU 5 上现有 `Qwen3.6-27B-MM` 生成全部指令和目标框，GS 仿真运行在 GPU 2，
规划使用独立 CPU 池，轨迹由专家控制器执行。未使用预设指令或预设终点替代模型输出。

五条均通过录制和逐帧 PointNav 独立核验：**0 碰撞、专家 STOP、停止点在原目标框内**。
最终目标误差最大 0.07490 m。来源保留 `development`，数据为 `audit_only / pending_review`。

[五条目标框与终帧预览](nav-train/docs/evidence/gs-data-production-pilot-20261116/five-samples.jpg)
（左侧为模型所选视角及原框，右侧为实际终帧）。

| 场景 | 模型指令简述（原始英文保存在记录中） | 步数 | 路径长度 | 目标误差 | 轨迹 |
| --- | --- | ---: | ---: | ---: | --- |
| 智元 | 右转，到白色圆柱罐前空地 | 19 | 2.36 m | 0.41 cm | `outputs/gs-production-pilot-20261116-final/audit-v2/zhiyuan-00-rollout.gif` |
| 智元 | 转身，到白色机器与黄色护栏前空地 | 19 | 1.59 m | 1.64 cm | `outputs/gs-production-pilot-20261116-final/audit-v2/v5-zhiyuan-01-rollout.gif` |
| 智园1 | 转身，到绿色出口标识下的门口前方 | 23 | 2.97 m | 7.49 cm | `outputs/gs-production-pilot-20261116-final/audit-v2/v5-zhiyuan1-00-rollout.gif` |
| 智园1 | 右转，到蓝色楼梯前方地面 | 16 | 1.94 m | 4.06 cm | `outputs/gs-production-pilot-20261116-final/audit-v2/v5-zhiyuan1-01-rollout.gif` |
| 智园2 | 右转，到蓝色工业设备与管道前地面 | 27 | 3.74 m | 0.00 cm | `outputs/gs-production-pilot-20261116-final/audit-v2/v5-zhiyuan2-00-rollout.gif` |

## 本轮接通的部分

1. nav-train 显式接纳注册的 GS development 场景，校验对应数据集、场景和后端，
   PointNav 标签保留真实来源，不伪装成 Habitat train。
2. 原模型服务未设置工具调用解析器，因此增加显式 `response_mode=json_schema`，
   复用现有字段约束及校验器，保存实际线缆请求和原始响应。默认工具协议保持兼容。
3. 修正采集规划提交端遗留的 `.navmesh` 文件名推导，统一读取场景 manifest 的导航资产。
4. 执行前的恢复检查使用原四视图观测中的同一前视传感器。270° 朝向时，旧实现用
   主 RGB 传感器比较，出现 136/388800 个通道值相差 1 的误判；三次只读复现均显示
   同一前视传感器差异为 0。修正后仍要求逐像素一致，没有放宽阈值。

完整回归：sim-engine **214 passed**，nav-train **290 passed**。
sim-engine Ruff、nav-train 本次核心客户端/提案模块/客户端测试 Ruff，以及两库
`git diff --check` 通过。nav-train 两个旧格式模块的既有全规则 lint 问题不属于本轮修复。

## 独立核验

- 核对模型原始 JSON 与实际使用的 instruction、view、bbox、needs_review、reason 一致。
- 确认模型请求为四幅当前 RGB，使用 `Qwen3.6-27B-MM` 与 JSON Schema。
- `CUDA_VISIBLE_DEVICES=5`、模型清单和 GPU 进程映射已保存。
- 冻结世界目标在移动前写入，且与规划目标、PointNav 目标一致。
- 全部 RGB-D 哈希、深度、标定、4 Hz 状态和命令记录通过 `validate_recording`。
  104 个实际动作对应 208 条 started/returned 日志及 109 个状态帧。
- 逐帧重新计算当前机体坐标下的 PointNav 目标，与保存标签一致。
- 五条均达到冻结目标容差，停止点投影在模型原框内。

GS 局部近景存在模糊、重影和空洞，门口那条终帧尤其明显。几何/录制通过不代表语义、
视觉质量或扫描几何精度已审核通过；深度报告仅验证单位与投影约定。本轮不导出训练集。

## 尝试和产物

五条成功来自 v4 的 1 条和 v5 的 4 条。v4 另有一条在恢复检查阶段被拦截，v5 有一条
规划预算耗尽，均未执行移动；用新候选补足数量。更早联调的 HTTP 依赖、模型协议和
规划路径问题也保留独立目录，没有重放已完成或不确定的运动。

- 独立审计报告：`nav-train/docs/evidence/gs-data-production-pilot-20261116/report.json`
- 五条完成清单：`nav-train/docs/evidence/gs-data-production-pilot-20261116/completion.json`
- GPU 5 模型身份：`nav-train/docs/evidence/gs-data-production-pilot-20261116/model-service.json`
- 恢复检查复现：`nav-train/docs/evidence/gs-data-production-pilot-20261116/restore-diagnosis.json`
- 完整五条数据包（约 67 MiB）：`outputs/gs-production-pilot-20261116-final/five-records.tar.gz`
- 本机解包目录：`outputs/gs-production-pilot-20261116-final/records/`。
  包括模型请求/响应、指令/目标框、四视图、原始 RGB-D、状态/动作、规划凭据和 PointNav 标签。
  导出包已展开符号链接，JSON 内仍保留原 A100 绝对路径作为来源证据。
- A100 最终部署及完成清单：
  `/data/nav_storage/sim-engine/deployments/gs-production-pilot-20261116-v5/completion.json`。
  `prepared.json` 记录源码哈希、剩余配额和继承的那条成功记录；v1–v4 的原始证据保留。

采集服务在完成后已退出，GPU 5 模型服务继续运行。复用配置说明见
[GS 数据生产链路](gs-data-production.md)；冻结运行目录不能直接重跑，应准备新 owner 和输出目录。
