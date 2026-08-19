# Mnemos

**Local-First AI Memory, Knowledge, and Decision-Support System**

> Mnemos v2.0.0：本地 AI Agent 知识采集、蒸馏、搜索与行动辅助系统。
>
> 核心链路已可用：raw_events.db 原始采集、Raw Vault 展示投影、Wiki 蒸馏、KG 补建、认知压缩计划、信任评分闸门、PreFlight/Guard、评分闭环、主动推送、新鲜度检测。
> 个人稳定版持续打磨中：认知决策飞轮长期效果、自动回顾触达质量、蒸馏输出可读性。
>
> 🌍 [English Version](README-en.md)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://img.shields.io/github/actions/workflow/status/jielumaizui/mnemos/CI.yml?branch=main)](https://github.com/jielumaizui/mnemos/actions)

---

**你是否也被这些问题困扰？**

- 和 AI 聊完一个复杂项目，两周后再问，它已经完全忘了之前的上下文
- 每次遇到同样的问题，都要重新搜索、重新踩一遍同样的坑
- 花了很多时间记笔记、整理文档，但真正需要的时候永远找不到
- 学了很多东西，过了一段时间就忘得一干二净
- 知道自己有很多知识盲区，但不知道盲区在哪里

**所有这些问题，本质上都是同一个问题：人类的认知能力是有限的。**

Mnemos 是一套面向本地运行的 AI Agent 记忆、知识和决策辅助系统。它通过 Agent Kit、MCP、CLI、daemon 和本地 source parser 采集可授权的对话/文件信号，经 raw 保留、蒸馏、质量门、Wiki/KG、搜索、画像和提醒链路，把可复用知识注入回 AI 工作流。

2026-07-11 起，daemon 的 PID file 使用 `mnemos.daemon_instance.v2`，heartbeat 使用 `mnemos.daemon_heartbeat.v3`；`daemon status/stop` 和 strict health 共同核对 OS start token/boot session/executable、runtime code fingerprint、配置文件字节哈希、canonical 有效配置指纹、database identity 与当前精确 service manifest。PID 复用、证据不完整或 env/performance tier 导致有效配置漂移时 fail closed 且不发信号；`start` 只有在当前 instance heartbeat 写出后才报告成功。

同日起，Capture → Amphora → Hephaestus → recap 使用持久化、revision-aware 的 typed receipt 闭环：Capture 只有拿到匹配的 Amphora 入队回执才进入 `done`；蒸馏只有形成正式页面或明确的 intentional skip 回执才进入终态；trusted proposal、partial、retry 和 write failure 均保持非终态。跨版本遗留缺口先用 `python3 scripts/reconcile_pipeline_receipts.py` 只读审计，确认备份和影响范围后再显式加 `--apply` 修复。

2026-07-11 起，recap 自身的下游消费也使用 durable fan-out outbox：`wiki_search/context_aware_search`、`preflight/guard/policy_patch`、`follow_up`、`persona`、`scheduler`、`scoring` 先映射到 canonical consumer，再分别写 command 与 receipt；全部 required receipt 提交或明确 intentional skip 后 session 才是 `consumed`。`recap_feedback` 会通过 correction outbox 撤销提醒/调度、抑制检索与策略补丁、向 persona/scoring 写补偿信号；partial、崩溃和重启只重试缺失 effect，冲突反馈必须携带最新 `supersedes_event_id`。生产对账使用 `python3 scripts/reconcile_recap_consumption.py --json`，确认四库备份后才显式 `--apply --json`。

同日起，Agent Kit 升级为 `agent-kit-v2`：安装、MCP/Policy、source fidelity 和认知能力声明只构成静态 `conformance_ok`，不能再推出运行态 `full_power`。运行满血还必须具备 `user_authorized/shadow_enabled` 内容授权，并由同一认证宿主先调用 canonical `health_check`，再在 5 分钟内提交固定 `mnemos.agent_runtime_probe.v1` synthetic-safe 样本；回执只保存 agent、时间、health check-set hash 和完整性元数据，不保存样本文本。`runtime_receipt_at` 超过 24 小时、授权撤销、样本畸形、握手缺失/过期或 check-set 不一致都会严格失败。MCP 与 CLI health 现在共用 `core.ops.health_check.build_health_report_quiet()`，共同输出 30 个 `health_check_ids` 与 `health_check_ids_hash`；Agent 运行能力也进入 strict health。

Wiki 下游投影也使用独立的 append-only 生命周期账本：每次 create/update/move/delete 都生成稳定 `page_id`、因果 `page_revision` 和 tombstone，EventBus 只在 KG、Cognitive Graph、关系向量、Wiki 搜索索引、metrics 与 MOC 六个消费者分别返回 typed `ack/noop` 后才闭合。`retry/defer/dead`、daemon 重启和乱序 revision 都保留可见状态，不再把业务软失败当传输成功。`scripts/rebuild_wiki_projection_state.py` 提供默认只读预览、显式备份重建、全量/增量/隔离 comparator 与 receipt 对账；详细契约见 [Wiki 投影生命周期](docs/WIKI_PROJECTION_LIFECYCLE.md)。

2026-07-20 起，committed CognitionEpisode 的认知语义不再附着于 `knowledge_distilled` 的 Wiki/entity/relation payload，也不再同步双写图。唯一 dispatch owner 发布 ID-only、versioned `cognition_episode_committed`，固定由 Wiki、EvidenceGraph/knowledge graph 和 CognitiveGraph 三个消费者以 durable lease、typed outcome、稳定 effect/receipt 和 before/after hash 投影；重启只补缺失 effect。EvidenceGraph 固化 `RawRevisionSpan→Observation→Claim/Belief→Decision→Prediction→Action→Outcome` 方向，并为 ACL 与被省略来源留下 typed receipt。只读验收入口为 `scripts/audit_cognitive_event_dispatch.py --strict --json` 与 `scripts/audit_evidence_graph_direction.py --strict --json`；历史库只可在停止所有 writer、审阅 inventory hash 并指定备份目录后运行 `scripts/reconcile_cognition_episode_projections.py --apply`。

同一 logical turn 的正文变化现在写入 append-only `raw_turn_revisions`，`raw_turns.current_revision_id` 只承担 current pointer；旧 revision 可按 `raw-revision:<revision_id>` 稳定回查。Capture handoff、Amphora task、Hephaestus fragment 与 Wiki page 使用 `revision_id + span` provenance，引用边会阻止 raw retention 物理删除。`session_search` 先对 canonical metadata 鉴权，再从 `raw_events.db` 取 immutable revision 正文；RawIndex/Markdown 只提供已授权候选，投影缺失或截断不再决定检索真相。历史对账使用 `python3 scripts/reconcile_raw_revision_provenance.py`，默认 dry-run；`--apply` 会先备份数据库，能证明的页面写 edge，不能证明的页面只写 `pending_rebuild` gap，禁止伪造来源。

2026-07-14 起，Observation 也把这一边界落实到运行路径：正式入口从 `raw_events.db` 的只读 typed API 读取 eligible current revision，而不是解析 Raw Vault Markdown；每个 Observation 绑定 exact `revision_id` 与完整可见 span，不能用 logical event ID、目录名或路径猜测替代。无法产出 Observation 的 eligible revision 只可写带限定原因的 `intentional_no_observation` terminal，提取/持久化失败保持可重试且不推进 cursor；Markdown 仅保留为显式的 v2 hash-parity compatibility audit，不能充当 fallback。`mnemos.learning_signal.v2` 会重新计算这些 edges/terminals，并把“所有可见 Raw 都被跳过”作为预算失败，避免 coverage 假绿。

2026-07-16 起，Observation 校准只按 canonical Raw root 计算独立证据：同一 Raw 及其派生 Wiki 只形成一簇，同时引用多个 Raw 的汇总页只是不计票 overlay，不会重复加权或把真实独立根合并。每次结果以 `CalibrationRecord` 原子提交到唯一 `CognitiveStateStore`，绑定窄脱敏的完整计算输入、peer Observation、lineage、validator/combiner 实现与 spec hash、prior/posterior、支持/反证簇、时间窗和投影 outbox；脱敏前基础测量只留下 SHA-256 identity，因此不同敏感输入不会收敛成同一占位符记录，也不会保存敏感字面量。validator 缺陷、缺失精确 Raw/span、重复 validator identity 和不可读取的实现源码都直接失败关闭。`observations.db` 只在 current committed record 存在且基础测量为 `verified` 后绑定 posterior；superseded receipt 不能回绑旧 posterior，清理/retention 也不能越过 canonical record 生命周期制造孤儿。全量/增量 Wiki 都重放同一 committed revision，并展示 Observation ID、Calibration revision/hash、source span 和 omission receipt。存量 schema 先运行 `scripts/reconcile_observation_calibration_state.py --json` 只读预览，停止 daemon 并指定备份目录后才能显式 `--apply`；无法从旧 posterior 证明原始 prior 的行会保守标为 `historical_unverified`，必须重新提取后才能校准，绝不把旧值伪装成基础测量。`scripts/audit_cognitive_calibration_lineage.py --strict --json` 是零预算验收入口。

完整蒸馏输入不再把长代码压成头尾片段，也不会只保留前三条 shell 命令；除显式 `[thinking]...[/thinking]` 私密块外，`clean_message_content()` 保留所有可见内容与格式。WikiBuilder 纯文本 fallback 不再截为 500 字符。标准/分块 extractor 以 `lossless=True` 组装输入，极小总预算或单消息预算只产生 `budget_overflow_tokens`，不触发 head-tail/消息截断；私密块排除以不含正文的类型、span、计数留痕。超出预算的内容由 `Tokenizer.split_to_tokens()` 拆成多个 chunk，首/中/尾内容都进入真实 extractor input。分块检查点把 `lossless-visible-v1` 写入哈希和 `chunk_info`；旧的无版本检查点不会被复用，重试时会按当前无损契约重新提取。

分块恢复现在要求完整的 `mnemos.distill_execution_spec.v2`：真实渲染 prompt、输出 schema、extract/parse/quality 代码摘要、显式 provider/model/backend route、merge 合同、全部输出相关有效配置，以及不可变 `DistillInputSpec` hash 共同决定 `execution_spec_hash`。模型根输出必须先通过 `distill_output_v4` 的 schema-owned 条件约束和 typed runtime validator：skip 只有在空 fragments/claims、非空原因和来源 evidence 均齐全时才合法；knowledge/skill 必须有 fragments、claims、非 skip intent、行为意图和完整 19 字段 `cognition_episode`，artifact、关系、认知 action 的附加字段也由 schema 条件校验。该验证在 correction 前后、checkpoint 读写和正式写入前复用；`CheckpointAdmission` 持久化 input-spec hash、输出契约版本、canonical root hash 与 judgment。正式 sink 前还要先向唯一 `CognitiveStateStore` 原子提交 episode revision/event/outbox。命中/失效原因与字段差异写入 chunk provenance；旧 schema、缺失 root/admission、损坏 spec/payload 或任一有效字段变化都必须重提取，新规格失败不会覆盖旧成功结果。`python3 scripts/audit_distill_output_contract.py --strict --json` 校验发布契约；`python3 scripts/reconcile_distill_execution_checkpoints.py --json` 默认只读盘点，停止 daemon 后显式 `--apply --backup-dir <dir> --json` 才会先备份再迁移，旧行保留为不可复用的历史代际。

COG-028 起，Extractor 在首次响应及每次有界修正后都执行同一 JSON Schema + semantic contract，Engine 只做防御性复验。蒸馏 backend 的正式端口返回 `DistillBackendResponse`，同时保留 raw text、parsed payload、usage、provider/model、request ID、finish reason、parse path、完整 attempt history 与 response hash；最终失败 artifact 还绑定 prompt/input-spec/response hash，空传输会显式标记 `transport_empty`。daemon 只有 `HephaestusWorker.process_all()` 这一名 active owner；旧 `distill_output` 目录 collector、弱 validator、raw Markdown fallback 及 `distill.max_collect_per_cycle` 已删除。`audit_distill_output_contract.py` 会同时阻断第二 active owner、parsed-only backend 回流和旧 collector 方法；失败落盘只对个人隐私、API key/令牌、银行卡、密码/私钥做窄脱敏，不增加整库或 artifact 加密。

COG-029 起，artifact identity 也不再由模型生成。Capture 与完整 Session handoff 都把附件、工具结果、reasoning/test artifact 绑定到 authoritative Raw revision；`DistillInputSpec v2` 在模型调用前按完整 SHA-256 构建 path-free、chunk-local `ArtifactCatalog`。文件型 ref 现场读文件重算 hash；pathless tool result 由系统对不进 Prompt 的 canonical inline payload 重算，caller 自报 SHA/marker 无效。复用 Raw revision 时还会核对当前内容 hash，handoff 只采用 revision header 的 authoritative hash；malformed ref 不会被静默丢弃。Prompt 只暴露 opaque `artifact_ref_id`、窄脱敏摘要和允许的 source event；模型只能选择 ref，不能输出 URI、type、hash、MIME 或 ACL。任一当前 input ref 缺文件、hash 不可验证、越权或 source admission 失败时，Extractor 会在模型调用前整体阻断；合法 ref 才会在 correction/admission 边界解析为 content-addressed URI 与系统字段。catalog/URI resolver 代码摘要属于 checkpoint execution spec；相同字节即使换路径/轮次仍得到同一 ref/checkpoint identity。

COG-012 后，分块路径不再把每个局部 structured output 覆盖进同一个可变 result。`ChunkedExtractionCoordinator` 为每块建立只含实际 Raw revision/span 的 immutable `DistillInputSpec` 和 `ChunkExtractionResult`；`ChunkEpisodeMerger` 再从所有 admitted canonical roots 生成一个确定性 session root。claim/relation 使用稳定派生 ID，重复证据和验证事件保留每次出现的 chunk/local ordinal，竞争意图显式保留为 competing hypotheses，confidence 使用保守最小值；最后一个合法 local skip 不会擦除前块知识。`FragmentMerger` 的 LLM 与规则路径都必须按顺序保留完整正文、背景、代码、空行、关系和精确来源。冷运行、checkpoint 全命中与进程重启必须得到相同 canonical hash 和 Wiki 页面；写页前会从局部 canonical roots 重算聚合，update/merge/shadow/dispute/reinforcement 也必须在物理写入前证明 claim 对应的 exact Raw spans，禁止回退到整 session 的宽泛来源。

**当前版本为 v2.0.0，核心链路已可用，但它不是完整自治的认知系统，也不是默认强制执行的可信推送决策系统。** 可信推送是可配置写入闭环：`trusted_push.mode=off` 保持旧写入，`shadow` 只生成 shadow proposal，`enforce` 才要求 ProposalQueue / Journal / Writer 审批链路。当前也没有本地 Web 控制中心；配置与运维入口以 `mnemos config`、`~/.mnemos/configs/main.json`、`mnemos doctor`、`mnemos health --json`、`mnemos proposal ...` 和 MCP 工具为准。

灵感源自 [Karpathy 的 LLM Wiki 模式](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)——让 LLM 增量构建并维护持久化的知识库。Mnemos 在此基础上增加了决策辅助层：**知识不是存完就完了，它应该在决策中活着。**

## 它和别的"Second Brain"有什么不同？

| 维度 | 常见 Second Brain 工具 | Mnemos |
|------|----------------------|--------|
| 系统定位 | 知识存储与检索 | 本地优先 AI 记忆、知识与决策辅助系统 |
| 自动化程度 | 半自动（需手动整理/打标签） | v2.0.0：自动采集→蒸馏→入库；评分/推送/新鲜度持续优化中 |
| 知识流向 | 你 → 系统 → 你自己查 | 系统 → AI Agent → 实时辅助决策 |
| 质量保障 | 去重（如果有） | 七层蒸馏流水线 + 通用质量门 + 认知价值门 + 自适应评分 + 三道自检 |
| 适应能力 | 规则固定 | 冷启动规则 → 贝叶斯自适应 → 行为反馈闭环 |
| 用户建模 | 无 | 用户认知画像 v2（三层雷达 + 画像断言 + 消费效果日志），驱动决策策略 |
| 知识生命周期 | 手动管理或不管 | 评分驱动自动进化，过时知识主动预警，强制复盘闭环 |
| 模块耦合 | 一体化 | 热插拔设计，按需启用 |

## 核心竞争优势

### 存储是底线，不是卖点

知识存储和记忆检索是 Mnemos 最基础的功能。用户可以指定文件经蒸馏后进入知识库，AI 对话记录也会被自动采集并蒸馏为结构化知识——无需手动整理。当前 v2.0.0 版本核心链路已可用，高级决策辅助能力仍在持续优化中。

### 一、自适应动态调整引擎

系统内置自适应评分框架，数据积累后逐步从规则驱动过渡到数据驱动：

- **三阶段冷启动**：COLD（纯规则）→ WARM（规则+贝叶斯混合）→ HOT（数据驱动），任何自适应模块在数据不足时都有规则兜底，不会因为"没数据"就罢工
- **贝叶斯评分**：每条知识、每个实体、每段关系都有置信度评分，新证据到来时实时更新后验概率
- **反馈闭环**：隐式信号（搜索/复制/停留时长）+ 显式反馈 → 加权融合 → 驱动评分模型重训练
- **漂移检测**：当特征分布偏移超过 3-sigma 时自动触发模型校准

评分引擎覆盖 6 个子系统：raw_events.db 采集质量、同步优先级、蒸馏决策、知识图谱置信度、画像稳定性、运维健康度。每个子系统独立评分、独立进化、独立降级。自适应配置不再只影响 `app.push_max_items`：`core/kia/adaptive_policy_matrix.py` 与 `docs/acceptance/adaptive_policy_matrix.json` 固化 `mnemos.adaptive_policy_coverage.v1`，覆盖 distill、quality_gate、scoring、delivery、search、raw、document_process、intent、cognitive_decision 9 个域和 11 条默认规则；每条规则必须声明输入信号、可调参数、运行时读取入口、回滚指标和验收指标。`daemon/adaptive_service.py` 会从 search/no_result、raw completeness、distill action、delivery feedback、document rejection、stale page 与 scorer feedback 等账本采集指标，`EffectivePolicy` 消费 active shadow 时保留 24h rollback 窗口；没有 active shadow 时运行代码继续尊重调用方配置/默认值。

2026-07-04 起，系统级契约统一在 `core/system_contracts.py` 中登记：`mnemos.cognitive_asset.v1`、`mnemos.cognitive_readiness.v2`、`mnemos.learning_signal.v2`、`mnemos.quality_decision.v1`、`mnemos.capability_registry.v1`、`mnemos.privacy_retention.v1`、`mnemos.lifecycle_status.v1`、`mnemos.action_ledger.v1`、`mnemos.domain_glossary.v1`、`mnemos.scorecard.v1`、`mnemos.module_toggle.v1`、`mnemos.toggle_output.v1`、`mnemos.wiki_quality.v1`、`mnemos.golden_benchmark.v1`、`mnemos.install_lifecycle.v1`，并由运行态 `mnemos.runtime_producer_consumer.v2` ledger 对 producer/consumer 闭环做对账。`mnemos_cli.py health --json` 会输出 `checks.system_contracts`、`checks.module_toggles`、`checks.runtime_producer_consumer`、`checks.golden_benchmark`、`checks.distill_json_quality`、`checks.distill_cognitive_actions`、`checks.wiki_route`、`checks.adaptive_policy`、`checks.cognitive_learning`、strict `checks.cognitive_readiness`、`checks.install_lifecycle`、`checks.sqlite_disk_budget`、`checks.security` 与 `checks.auto_healing`，并在顶层给出 `status`、`ok`、`usable`、`strict_ok`、`strict_failures`；storage/wiki/agent/disk/api/schema/heartbeat/wiki_route/runtime_producer_consumer/install_lifecycle/amphora/queues/cognitive_readiness/sqlite_disk_budget 是 strict health checks，当前 service error、Wiki 路由预算超线、runtime producer/consumer orphan outputs、no-source consumers、item mismatches 或 dead letters 超预算、installed_partial 或未完成 required install step、Amphora failed task、distill failed 超预算、distill processing 超过 stale 预算、high/critical recap pending 超预算、dialog reminder pending/active 超预算、认知就绪度预算失败或 SQLite 磁盘预算超线都会让 strict health 降级。`checks.auto_healing` 使用 `mnemos.auto_heal_orchestrator.v1` 把每个非 ok health check 标注为 `auto_fixed`、`auto_fix_failed`、`needs_user`、`ignored_with_reason` 或 `blocked`，并给出 risk、repair action、rollback plan、verification command 和用户介入预算；`mnemos doctor repair --dry-run --json` 输出同一计划，带 agent 名称的 `doctor repair <agent>` 仍走 Agent 主动接入修复。`checks.adaptive_policy` 是非 strict 可观测面，展示覆盖规则数量、覆盖域、active shadow、overdue shadow 和 coverage_errors；`checks.cognitive_learning` 是非 strict 学习闭环面，展示 raw/search/feedback/reflection 到 observations、policy_patches、cognitive_consolidation run 的转化缺口和修复动作；`checks.cognitive_readiness` 复用 `audit_cognitive_readiness.py --json --budget` 的预算语义，输出 score、budget_ok、failure_count 和 repair_actions，预算失败必须进入 `strict_failures`，不能被 health 顶层 ok 掩盖；`mnemos status` 同步打印 active shadow 的 experiment/config/old→new/metric_before/age_hours。`checks.sqlite_disk_budget` 使用 `mnemos.sqlite_disk_budget.v1` 监控 `.db-wal`、Mnemos temp、snapshot 和 `raw_events.db` 的体积/增长率；`.db-wal` 和过期 Mnemos temp 标记为可安全修复，snapshot 与 raw_events 删除必须由用户确认。`checks.distill_json_quality` 是非 strict 蒸馏质量面，按 direct/fallback/fixed/failed 路径展示 JSON 解析成功率、修复次数和 24h 趋势；`checks.distill_cognitive_actions` 是非 strict 认知动作面，从 `distill_actions.db.cognitive_action_log` 汇总下游动作计数、状态计数和 artifact 数量；`checks.wiki_route` 是 strict Obsidian 路由面，按预算检查 `inbox_ready_to_classify`、`needs_review_pages`、正式区 source-prefixed 页和标题/basename 冲突组。`checks.runtime_producer_consumer` 是 strict 数据流闭环面，从 `producer_consumer_ledger.db` 汇总 `adaptive_data_flows.json` 的 runtime topic、produced/consumed/dead_letter 计数、item_id 级对账、last produced/consumed 时间、pending 和 lag 预算，并把 orphan outputs、no-source consumers、item mismatches、dead letters 映射到 `data_pipeline` scorecard。`checks.install_lifecycle` 是 strict 安装旅程面，会列出 `incomplete_required_steps`、repair actions 和状态错误，不能把 `installed_partial` 当作完全健康；health 读取真实 `mnemos setup` 写入 `ActionLedger(action_type=install_setup)` 的 verified `installed_ready` 证据来闭环 runtime step，但仍会重新检查当前配置、Vault 和必填模型端点是否 blocked。`checks.security` 是非 strict 运维安全面：敏感目录/配置权限违规会给出 `chmod` 修复动作，`mnemos.secret_inventory.v1` 会递归扫描 `api_key/token/secret/password/credential/bearer/key_source` 等 secret-like 字段且忽略 `token_budget`/`max_tokens` 等非密钥字段，keyring 不可用会显示 backend/error 和 env fallback 说明。`mnemos_cli.py doctor modules --json` 可只读查看默认关闭原因、自动开启条件、产出契约、消费方、效果指标和回滚策略；`mnemos_cli.py doctor config --strict --json` 输出 `mnemos.config_audit.v1`，一次性验收 LLM/embedding/reranker/multimodal、secret inventory、storage disk budget config、路径、legacy/stale 配置、privacy、retention、daemon 和权限，机器产物写入 `~/.mnemos/config_audit.json` 且只记录 `env:`/`keyring:`/`keyref:` 来源、字段路径和长度统计，不写明文 key/token/secret。对应 strict 审计脚本包括 `scripts/audit_cognitive_asset_schema.py`、`scripts/audit_cognitive_readiness.py --json --budget`、`scripts/audit_quality_decision_contract.py`、`scripts/audit_capability_registry.py`、`scripts/audit_privacy_retention_policy.py`、`scripts/audit_lifecycle_status_contract.py`、`scripts/audit_action_ledger.py`、`scripts/audit_domain_glossary.py`、`scripts/audit_mnemos_scorecard.py`、`scripts/audit_wiki_quality_contract.py`、`scripts/audit_adaptive_policy_matrix.py --strict`、`scripts/audit_module_toggle_registry.py`、`scripts/audit_cold_start_toggle_matrix.py`、`scripts/audit_toggle_auto_disable_policy.py`、`scripts/audit_toggle_output_consumers.py`、`scripts/audit_runtime_producer_consumer_closure.py --strict`、`scripts/audit_golden_benchmark_contract.py` 和 `scripts/audit_install_upgrade_contract.py`。

问题 34 起，`producer_consumer_ledger.db` 同时承载 `mnemos.cognitive_data_event.v1` 和 `mnemos.data_interface_registry.v1`：`core/ops/cognitive_data_contract.py` 为 CaptureService、CaptureQueue、SyncEngine、FileIngestor、DocumentProcessor、Amphora、EventBus、ReflectionStore、AdaptiveScorer、DistillActionRouter 和 persona signal store 注册统一数据接口；`core/ops/producer_consumer_ledger.py` 记录 cognitive data event、consumer outcome 和 duplicate/derived/reinforcement 对账关系。新增数据入口或消费者后必须运行 `python3 scripts/audit_data_interface_registry.py --strict` 和 `python3 scripts/audit_runtime_producer_consumer_closure.py --strict`。

ROOT-20260710-021 后，`scripts/check_maintainability_budget.py --closure` 将 development ratchet 与 release zero-closure 分离。当前扫描为 16 个超大文件、478 个 broad catch（其中 120 个未分类、关键路径 0）；exact AST fingerprint、owner、expiry、telemetry 和 remove condition 防止同文件同数量替换、解析失败、过期接受或基线未随改善收紧。当前另有 131 个未记录 zombie candidate，均会阻断 closure，不能被 baseline 吸收。development profile 与 strict release 结论分离：full-score 使用 `--closure --strict --json`，只在 residual=0 时认证；vulture current/baseline 已固定为 0/0。

### 二、用户画像决策中枢

画像不是标签墙，而是决策中枢。系统从 AI 对话行为中推断用户的认知模式和价值取向，并将画像注入 AI 的工作流中：

- **三层雷达**：能量模式（专注/启动/续航/切换）、认知模式（抽象/系统/质疑/创造）、价值优先级（正确/效率/深度/完美/创新/自主）
- **用户认知画像 v2**：`core/persona/cognitive_profile.py` 将纠错、忽略、打断、返工和明确偏好沉淀为 `profile_signals`，聚合成带证据、置信度、隐私等级、修订/反驳策略的 `profile_assertions`，输出决策偏好、判断标准、交互契约、风险边界、当前目标和认知决策飞轮输入
- **画像驱动对话策略**：根据画像动态生成 AI 提示词片段，让 AI 的行为风格适配用户——完美主义者看到更严谨的建议，效率优先者看到更简洁的方案
- **消费效果闭环**：preflight、ContextAwareSearch、蒸馏 prompt、CognitiveValueGate、Auto-Healing 和 Cognitive Decision Flywheel 消费画像后写入 `profile_usage_log`，记录用到哪些断言、是否改变行为、结果和用户反馈
- **双画像交叉验证**：行为画像 vs 知识画像，检测"言行不一"——嘴上说注重效率，行为上却在反复优化细节
- **情境隔离**：工作/个人/学习三种情境下的画像独立演进，避免跨界污染
- **14 维演化时间线**：长期追踪画像变化趋势，自动检测倦怠信号、认知转变和价值翻转

### 三、强制复盘与逻辑自检

知识入库不是终点，持续验证才是。系统按预算、权重和用户确认策略追踪知识生命周期，在关键时刻提醒或请求介入：

- **组合权重强制打开**：系统实时评估每条复盘待办的紧迫性——综合重要性（severity）、等待时长、同类问题频率、当前工作上下文关联度、承诺违约五个维度打分，达到阈值（≥4分）时自动打开 Obsidian 展示决策页面或复盘页面，确保你不会错过关键信号；未达阈值则仅对话内轻提醒，不打断工作流
- **用户预约直接弹开**：用户说"1天后提醒我复盘"，到点直接打开 Obsidian 对应的复盘页面，不走权重算法——你自己约的，系统不废话
- **启动补偿**：关机或合盖期间过期的预约，下次启动时自动补发——过期用户预约立即打开 Obsidian，过期系统提醒走权重判断
- **周报追达升级**：周报生成后 3 天内轻提醒，3 天后仍未读则强制插入对话，7 天后自动归档。配合 Wiki 看板徽章，100% 兜底
- **七层蒸馏流水线**：噪音过滤 → 价值预判 → LLM 判断 → 知识提取 → 自检验证 → 跨 Agent 关联 → 反馈循环。写入 Wiki 前还要经过 `QualityGate` 和 `CognitiveValueGate`，后者要求页面说明它对决策、方法、反模式、偏好、关系、证据或未来触发场景的认知贡献；高价值 claim 还必须声明 observation/reflection/policy/methodology 等 `cognitive_actions`，非 skip 输出必须声明 `user_behavior_intent`，记录用户为什么引入这条知识、意图证据、验证/修正状态和置信度；蒸馏输出预算四档为 `6000/8000/12000/16000`，由 `scripts/audit_distill_response_budget.py` 防回归；最终门禁决策会写入 `ActionLedger(action_type=quality_gate)`
- **可证伪性标记**（实验性）：过时知识检测已融入 ShadowPage/争议流程，不再保留独立兼容骨架
- **争议仲裁**：争议页面生成、冲突记录、KG 败方关系标记、带 marker 的上下文同步回双方原始页面均可用；`mnemos dispute rollback-context` 可回滚同步块。更细粒度的正文自动合并作为下一阶段升级项
- **增量蒸馏 + 延迟蒸馏**：长对话每 5 轮增量生成草稿，低置信度内容进入延迟队列等信号充分后再处理
- **再循环守卫**：防止 Wiki 注入的内容被再次蒸馏回知识库，杜绝知识自引用污染

### 四、热插拔功能模块

Mnemos 的 14+ 子系统是可独立启停的功能模块，不是紧耦合的巨石。daemon/CLI/MCP 能自动运行可配置任务，并在模块级故障时隔离或降级：

- **模块化架构**：每个子系统（知识图谱、影子页面、DNA 指纹、熵引擎、时间胶囊……）独立运行，关掉任何一个不影响核心链路
- **KIA 调度器**：16 步调度任务拓扑排序并行执行，单模块连续 3 次失败自动禁用，不拖垮全局
- **事件驱动**：模块间通过 EventBus 松耦合通信，蒸馏完成→图谱更新→画像刷新→推送评估，全链路异步
- **按需启用**：核心链路（同步→评分→蒸馏）开箱即用，高级功能（向量索引、预测推送、争议仲裁）按需开启
- **资源治理**（v2.0.0，仍缺多机型基准）：`ResourceBudget` 动态监测 CPU/内存/温度/电源，后台任务自动降速而非关闭。高温时暂停 P2/P3，电池供电时降低非关键任务频率。`mnemos status` 可查看 1 小时资源趋势。后续需要补充 M4/M1/Intel 热压测报告

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│  应用层 — 决策输出                                               │
│  IntentRouter │ ApplicationHub │ ContextAwareSearch              │
│  PredictivePush │ BlindspotDiscovery │ DisputeResolver           │
│  FreshnessAlert │ WeeklyReport │ ForcedRetrospective              │
├─────────────────────────────────────────────────────────────────┤
│  知识层 — 理解与建模                                             │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ 知识图谱              │  │ 用户画像                          │  │
│  │ EntityManager         │  │ 三层雷达 + 交叉验证               │  │
│  │ RelationManager       │  │ 对话策略 + 情境隔离               │  │
│  │ EvolutionTracker      │  │ 14维演化时间线                    │  │
│  │ KGEventHandler        │  │ 事件驱动更新                      │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  评分与蒸馏层 — 质量保障                                         │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐  │
│  │ 自适应评分引擎        │  │ 七层蒸馏流水线                    │  │
│  │ COLD/WARM/HOT 三阶段  │  │ 噪音→预判→LLM→提取→自检→关联→反馈│  │
│  │ 6 子系统评分器         │  │ PromptBuilder + TokenBudget       │  │
│  │ 反馈闭环 + 漂移检测    │  │ 增量蒸馏 + 延迟蒸馏               │  │
│  └──────────────────────┘  └──────────────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  同步层 — 数据摄入                                               │
│  SyncEngine │ 12 Agent Source │ DocumentImportService │ FileIngestor │
│  TriggerSystem (Watchdog/Polling/Hybrid) │ AgentLifecycleManager │
└─────────────────────────────────────────────────────────────────┘
```

## 系统怎么跑起来（6 步链路）

Mnemos 的核心价值链路可以概括为 **采集 → 同步 → 投影 → 蒸馏 → 入库 → 辅助决策**：

```
1. 采集（Capture）
   各 Agent 对话结束 / 用户导入文件
        ↓
2. 同步（Sync）
   SyncEngine 将原始内容标准化为 Turn，写入 append-only raw revision；logical turn 只保存 current pointer
        ↓
3. 展示投影（Raw Projection）
   daemon 将 current revisions 投影为 raw/<agent>/<date>/<chunk>.md；投影/RawIndex 只作可重建候选，正文取证回到 raw_events.db
        ↓
4. 蒸馏（Distill）
   Hephaestus 七层流水线把原始材料提炼成结构化 Wiki 页面
        ↓
5. 入库 + 建图谱（Store & Graph）
   默认 off 模式下 Wiki 页面按既有路径进入 Obsidian vault；启用 trusted_push shadow/enforce 后，Hephaestus 写入先进入 ProposalQueue，经 PushDecisionGate、append-only WriteJournal 和 KnowledgeVaultWriter，再写 NativeStore/MarkdownAdapter
        ↓
6. 辅助决策（KIA）
   Preflight 预加载、Aegis 守护、PredictivePush 主动推送、强制复盘闭环
```

v2.0.0 阶段采集到入库链路已稳定可用，辅助决策的精准度随数据积累持续提升。

## 蒸馏执行模型

Mnemos **通过 `DistillBackend` 接口调用 LLM API** 完成蒸馏任务，确保品质可控、流程闭环。当前生产默认实现仍是 `LLMBackend` 包装现有 OpenAI-compatible HTTP caller；本地 CLI `AgentBackend` 只允许在 shadow-only 评估面运行，不进入生产写入链路。

**设计原则：蒸馏执行权在 Mnemos，不在 Agent。**

为什么不用 Agent 执行蒸馏：
1. **品质不可控** — Agent 可能绕过 Mnemos 管道自行处理文件，导致硬校验、知识图谱构建、Wiki 入库全部失效
2. **约定不可靠** — Agent 的自主行为无法强制约束，"君子协定"必然被违反
3. **流程闭环** — 只有 Mnemos 自己执行，才能保证原始素材 → 蒸馏 → 硬校验 → 入库 → 知识图谱的完整闭环

Mnemos 通过 **LLMApiChain** 实现有序 failover（按 `llm.chain` 顺序尝试，兼容 primary / same-provider / cross-provider 字段，并保留额外后备节点），在 `core/llm_config.py` 中统一管理。

2026-07-08 起新增 P0 可信推送写入底座：`trusted_push.mode=off|shadow|enforce`。`off` 保持既有行为；`shadow` 继续旧写入但生成 shadow proposal，不写正式 Journal；`enforce` 会拦截 Hephaestus 对话蒸馏和文档蒸馏写页，把候选写入 `ProposalQueue`，用户通过 `mnemos proposal approve/reject/edit/recover/audit` 决策后，`KnowledgeVaultWriter` 才能写入 NativeStore 和 Markdown。2026-07-12 起，正式 Markdown write/delete/move 统一由 typed receipt commit helper 执行；receipt 绑定 target、content hash 和 expected-existing hash，move 额外绑定 source 与 source hash，不能把 A 提案复用于 B 页面。`python3 -m core.trust.static_scan` v4 使用 AST 发现 `write_text/write_bytes/open/rename/replace/unlink/os/shutil/atomic helper`，删除目录/整文件 marker 放行；非正式或恢复 sink 必须登记精确 `sink_id + owner + target_class + expiry`，unknown、stale registry、`known_bypass` 或伪造 `guarded_trusted_push` 均失败。当前分母为 169 个 sink：143 个精确 registry、17 个 receipt-dominated formal callsite、7 个 central writer sink 和 2 个 primitive sink，unknown/stale/known bypass 均为 0。Journal 使用 append-only event model，不更新同一行 phase；`prepare/commit/rollback/abort` 都是独立 event，并由 hash chain 校验。`mnemos proposal push/decide` 提供白盒结构化决策卡和 inline approve/reject/snooze/edit，不写入原生 agent 聊天历史；该命令是手动请求，默认不受 quiet-hour 抑制，自动投递方如需安静窗口必须显式调用 `DialogDecisionPush.push(respect_quiet_hours=True)`，没有可投递卡片时返回 `surface=none` 而不是伪装成 agent surface。`mnemos agent shadow status|enable|disable` 管理单 agent 灰度，默认关闭且一次只允许一个 CLI 型 agent；`mnemos golden eval --confirm-send-content` 才会把脱敏 synthetic fixtures 发送给 shadow agent，并比较 baseline 与 shadow 的 schema 成功率、fallback 率和字段完整度。AgentBackend subprocess 发送内容前必须先通过 `PromptSanitizer`，阻断内部路径、SQLite/config 路径、未授权目录和 secret-like token；shadow 输出不写 Wiki、不写正式 Journal、不影响 LLMBackend fallback。

## 5 分钟快速上手

> 跟着这个例子走一遍，你就知道 Mnemos 在做什么。

### 场景：你让 Claude 解决了一个 bug

**第 1 步：正常对话**

你问 Claude："asyncio.gather 为什么内存爆炸？" 经过一番排查，找到了根因。对话结束。

**第 2 步：自动触发蒸馏**

Session End Hook 可自动触发蒸馏。对话内容进入配置的蒸馏队列和 LLM 后端后，会经过七层流水线处理——噪音过滤掉闲聊，价值预判识别出"这是有价值的排障经验"，LLM 提取为结构化知识，自检验证断言和代码片段，再由通用质量门和认知价值门确认它不是普通参考文本，最终生成一条知识卡片或进入待审/失败闭环。

**第 3 步：评分与受控入库**

自适应评分引擎对这条知识自动打分：质量评分 0.85，蒸馏评分 0.92。评分与认知贡献门通过后，默认 off 模式会按既有自动路径进入知识库；若启用 `trusted_push.mode=enforce`，知识卡片会先进入 ProposalQueue，待用户审批后再由 Journal/Writer 写入。写入成功后 frontmatter 会写出认知贡献类型、预期消费者、质量门禁账本 ID、认知动作和行为意图摘要；知识图谱同步创建实体和关系，下游动作候选写入 `cognitive_action_log`，Observation/persona/reflection 可继续消费“用户为什么引入这条知识”的信号。

**第 4 步：画像学习**

系统从这次对话中自动采集信号：你在排查时表现出高专注深度和质疑倾向，也可能明确纠正“先测试再提交”。这些内容会进入 `profile_signals`，聚合成可反驳的画像断言。下次类似场景，preflight/search/distill/quality gate 会消费这些断言，并在 `profile_usage_log` 里记录它是否真的改变了行动。

**第 5 步：主动辅助决策**

一周后你开始写高并发爬虫。IntentRouter 自动识别任务意图，ContextAwareSearch 检索到之前的排障经验，画像决策中枢判断你应该会关心内存问题——主动在对话开头提醒你注意 asyncio.gather 的坑。

**日常使用只需正常对话，但高风险写入、可信推送 enforce、数据删除、快照清理和部分修复动作仍需要显式授权或人工确认。** v2.0.0 阶段核心链路（采集→蒸馏→入库）已可用，部分高级能力（自适应评分优化、主动推送精准度）仍在持续优化中。

### 验证系统在工作

```bash
# 1. 检查蒸馏队列
python3 core/kia/amphora.py --list

# 2. 检查 daemon 状态
python3 mnemos_cli.py daemon status

# 3. 查看 Inbox 是否有新内容
ls ~/Documents/mnemos/00-Inbox/

# 4. 查看画像
cat ~/Documents/mnemos/L5-Feedback/user-persona.md

# 5. 查看评分器状态
mnemos scorer status
```

## 🚀 30 秒快速开始

> 跟着走一遍，你就知道 Mnemos 在做什么。

### 前置条件

- Python >= 3.10
- 一个 AI Agent（Claude Code、Hermes、OpenClaw、OpenCode、Codex 等）
- **必装** [Obsidian](https://obsidian.md) 知识库
- **必配** 三类模型端点：LLM、Embedding、Reranker。每类都需要模型 ID、API 地址和 API Key。
- **可选** 多模态模型端点：用于图片、截图和视觉证据解析；不填写不影响 Mnemos 正常使用。

> **注意**：Mnemos 不绑定模型厂商。只要对应端点兼容所需 API，填写模型 ID、API 地址和 API Key 即可。安装阶段会对 LLM / Embedding / Reranker 分别做 smoke test；不可用会要求重新填写，交互模式默认最多 smoke 3 次，可用 `--max-smoke-attempts` 调整，并可选择保存当前配置后退出、打印 env 示例或停止到 dry-run 检查；`--yes` 或非 TTY 模式会直接失败。多模态模型只在配置后启用；配置后图片 inbox 会调用 OpenAI-compatible vision endpoint 解析入库并加入蒸馏队列，未配置时 `health` / `verify_installation` 会显示 `skipped`，图片 inbox 会生成可恢复任务。Reranker 的 `base_url` 可以填写服务根地址，也可以直接填写以 `/rerank` 结尾的完整 endpoint。
>
> 如果安装阶段未检测到 Obsidian，setup 会停止并说明原因：raw Vault 保存各 Agent 的原始对话，Mnemos Vault 保存蒸馏后的认知库，两者都需要能被 Obsidian 打开并人工核验；请先安装 Obsidian 后重新运行 setup。

### 产品级安装（推荐）

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
python3 mnemos_cli.py setup --dry-run --json
python3 mnemos_cli.py setup
```

如果已通过 `pip install -e .` 或发布包安装了 CLI，也可以直接运行：

```bash
mnemos setup --dry-run --json
mnemos setup
```

`mnemos setup` 是推荐主入口：它把配置、Vault 初始化、Agent policy、scheduler、部署验证和机器可读 repair action 串到同一个 `InstallLifecycleState`。`setup.sh`、`setup.bat` 和 `scripts/auto_setup.py` 仍保留为兼容/高级入口，但新文档和新自动化应优先调用 `mnemos setup`。当系统 Python 触发 venv re-exec 时，`mnemos setup --json` 会重新进入 `mnemos_cli.py setup ... --venv-reexec`，保持 lifecycle JSON/状态闭环；Homebrew/PEP 668 环境会优先回到 repo `.venv`，pip 升级超时只降级为 warning，editable install 的 build isolation 下载失败会用现有 venv `--no-build-isolation` 重试。`scripts/auto_setup.py --yes --preserve-config` 会通过 `scripts/setup_model_endpoints.py` 复用运行时模型解析规则，允许 embedding/reranker 复用全局 `SILICONFLOW_API_KEY`，并允许可选多模态通过 `MNEMOS_MULTIMODAL_*` 一次性录入；写配置后保持 `~/.mnemos/configs/main.json` 为 `0600`，最后只运行 `scripts/e2e_probe.py --dry-run --no-api`。`mnemos setup --dry-run --json` 的 lifecycle metadata 会暴露 `required_model_endpoints_failed` 和失败明细，供自动化判断必填模型缺口。

Windows PowerShell 兼容入口：

```powershell
.\setup.bat
```

安装过程中会明确提示正在配置的是 `LLM（对话/蒸馏模型）`、`Embedding（向量/语义召回模型）`、`Reranker（搜索重排模型）`，以及可跳过的 `多模态模型（图片/截图/视觉证据解析）`，避免把模型填错位置。

`mnemos setup` 会自动完成：
1. 检查 Python >= 3.10
2. 安装依赖
3. 检测 Obsidian 应用并确认 Mnemos/raw 两个默认 Vault 路径；未安装 Obsidian 会解释原因并停止部署
4. 生成 `~/.mnemos/configs/main.json`
5. 初始化标准 wiki 目录结构
6. 安装 AI Agent 主动接入（adapter hooks + MCP-only config/policy）
7. 启动后台守护进程
8. 配置/提示系统定时任务：macOS 写入 launchd；Linux 输出带运行环境变量的 cron 命令；Windows 调用 `mnemos scheduler install-windows` 注册 Task Scheduler，失败时打印手动命令
9. 运行部署验证：三类必填模型端点必须可用；可选多模态显示 configured/skipped/unreachable；已安装目标必须先静态合规，再以授权后的近期 MCP health + synthetic-safe completeness receipt 证明运行满血，未安装目标会跳过

全自动模式（无交互，macOS / Linux）：
```bash
export MNEMOS_LLM_MODEL=your_llm_model_id
export MNEMOS_LLM_BASE_URL=https://your-llm-api.example/v1
export MNEMOS_LLM_API_KEY=your_llm_key
export MNEMOS_EMBEDDING_MODEL=your_embedding_model_id
export MNEMOS_EMBEDDING_BASE_URL=https://your-embedding-api.example/v1
export MNEMOS_EMBEDDING_API_KEY=your_embedding_key
export MNEMOS_RERANKER_MODEL=your_reranker_model_id
export MNEMOS_RERANKER_BASE_URL=https://your-reranker-api.example/v1
export MNEMOS_RERANKER_API_KEY=your_reranker_key
# 可选：图片/截图/视觉证据解析
export MNEMOS_MULTIMODAL_MODEL=your_vision_model_id
export MNEMOS_MULTIMODAL_BASE_URL=https://your-vision-api.example/v1
export MNEMOS_MULTIMODAL_API_KEY=your_vision_key
python3 mnemos_cli.py setup --yes
```

Windows PowerShell 无交互模式：
```powershell
$env:MNEMOS_LLM_MODEL="your_llm_model_id"
$env:MNEMOS_LLM_BASE_URL="https://your-llm-api.example/v1"
$env:MNEMOS_LLM_API_KEY="your_llm_key"
$env:MNEMOS_EMBEDDING_MODEL="your_embedding_model_id"
$env:MNEMOS_EMBEDDING_BASE_URL="https://your-embedding-api.example/v1"
$env:MNEMOS_EMBEDDING_API_KEY="your_embedding_key"
$env:MNEMOS_RERANKER_MODEL="your_reranker_model_id"
$env:MNEMOS_RERANKER_BASE_URL="https://your-reranker-api.example/v1"
$env:MNEMOS_RERANKER_API_KEY="your_reranker_key"
# 可选：图片/截图/视觉证据解析
$env:MNEMOS_MULTIMODAL_MODEL="your_vision_model_id"
$env:MNEMOS_MULTIMODAL_BASE_URL="https://your-vision-api.example/v1"
$env:MNEMOS_MULTIMODAL_API_KEY="your_vision_key"
python3 mnemos_cli.py setup --yes
```

`--yes` 不会交互询问模型信息；三类必填模型端点任一缺失或 smoke test 失败都会直接退出。交互模式默认最多 3 次 smoke，可用 `--max-smoke-attempts N` 调整；非 TTY 环境不会等待输入。未设置 `MNEMOS_MULTIMODAL_*` 时只跳过可选多模态，不影响部署。

升级、修复与卸载统一走同一状态机：

```bash
python3 mnemos_cli.py upgrade plan --json
python3 mnemos_cli.py upgrade apply --json
python3 mnemos_cli.py doctor repair-all --json
python3 mnemos_cli.py uninstall --preserve-data --json
```

`upgrade apply` 会先创建全局快照，再执行迁移；包装旧迁移脚本默认 blocked，必须显式 `--execute-wrapped` 才会运行。`uninstall` 默认 `--preserve-data`；`--purge-data` 只生成数据所有权删除计划，真正删除必须先 `freeze`、提供 `snapshot_ref` 并二次确认。

### 手动安装

如果你偏好手动配置：

```bash
# 1. 克隆并安装
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
pip install -e .

# 2. 复制并编辑配置
mkdir -p ~/.mnemos/configs
cp config/config.example.json ~/.mnemos/configs/main.json
# 编辑 ~/.mnemos/configs/main.json，设置 wiki 路径，以及 llm/embedding/reranker 的 base_url、model；
# API key source 优先用 keyring:REF，无法使用 keyring 时才用 env:VAR 并显式接受 fallback 风险
# multimodal 为可选；配置后图片入口自动解析入库并入蒸馏队列，未配置或 API 失败时生成可恢复任务
export MNEMOS_LLM_MODEL=your_llm_model_id
export MNEMOS_LLM_BASE_URL=https://your-llm-api.example/v1
export MNEMOS_LLM_API_KEY=your_llm_key
export MNEMOS_EMBEDDING_MODEL=your_embedding_model_id
export MNEMOS_EMBEDDING_BASE_URL=https://your-embedding-api.example/v1
export MNEMOS_EMBEDDING_API_KEY=your_embedding_key
export MNEMOS_RERANKER_MODEL=your_reranker_model_id
export MNEMOS_RERANKER_BASE_URL=https://your-reranker-api.example/v1
export MNEMOS_RERANKER_API_KEY=your_reranker_key

# 3. 验证三类必填模型端点、可选多模态状态与系统状态
python3 mnemos_cli.py setup --dry-run --json
python3 mnemos_cli.py upgrade plan --json
python3 mnemos_cli.py secrets doctor --json
python3 verify_installation.py --api-smoke
mnemos doctor
mnemos doctor repair-all --json
mnemos doctor --cognitive-readiness --json
mnemos distill evidence-backfill --json
python3 scripts/plan_cognitive_consolidation.py --json
mnemos verify plan --json
```

`mnemos doctor` 会自动检测系统状态，检查依赖是否就绪；`mnemos_cli.py doctor config --strict --json` 是配置/隐私/secret 的一次性验收入口，报告写入 `~/.mnemos/config_audit.json`，默认脱敏真实 `base_url`、本机绝对路径和 `env:`/`keyring:`/`keyref:` 明细；只有本机私有排错时才用 `--unsafe-debug` 输出原值；并用 `legacy.config_stale_keys` 指向 `mnemos migrate apply config.stale_keys.v1 --json` 的迁移账本。`mnemos_cli.py secrets doctor --json` 输出 `mnemos.keyring_doctor.v1`，用于核对当前 Python keyring backend、secret 引用来源计数、`secret_inventory_plaintext_count=0`、`safe_but_not_best` 和 `security.accept_env_secret_fallback`；无法启用 keyring 时，必须在无明文 secret 的前提下运行 `python3 mnemos_cli.py secrets doctor --accept-env-fallback` 或等价设置 `security.accept_env_secret_fallback=true`，才能把 env 降级标为显式接受。`mnemos health --json` 的 heartbeat 会区分 daemon service 的 `active_service_errors` 和 `historical_service_errors`，`raw_projection` 在后续成功/跳过运行后会清除旧锁错误并写 `raw_projection_recovered` ActionLedger。health 顶层 `status` 明确区分 `ok/warning/degraded/failed`，`ok=false` 表示不是完全健康，`usable=true` 表示可用但有非阻断 warning；`checks.multimodal` 是非 strict 可选面，配置后显示 `endpoint_status=configured`，未配置显示 `skipped` 和恢复动作；Amphora failed task、队列预算超线、当前 heartbeat service error、API/schema/storage/wiki/disk/wiki_route/runtime_producer_consumer/install_lifecycle/cognitive_readiness/sqlite_disk_budget strict check 失败都会进入 `strict_failures` 并让 strict health 降级。判断本机是否 100% 运行时，不能只看一次 health JSON：还要确认 daemon PID 存活且 heartbeat 新鲜、`mnemos_cli.py distill status` 中 pending/processing/failed 为 0、daemon 日志没有持续 locked/closed database 或临时测试文件蒸馏、CPU 没有持续异常占用，并确认 `scripts/audit_cognitive_readiness.py --json --budget` 与 health strict 语义一致，再用 `mnemos_cli.py doctor`、`scripts/verify_installation.py --json` 与 `scripts/e2e_probe.py --dry-run --no-api` 做安装和基础链路交叉验证；默认 `verify_installation --json` 是 basic 验证，`results.integration_tests="skipped"` 且 `full_verification_ok=false` 时不能当作完整验收，发布前必须跑 `scripts/verify_installation.py --full --json` 并看到 `full_verification_ok=true`。2026-07-06 起，doctor 的 Wiki 待复核提示与 health `wiki_route_budgets` 共用预算，预算内待复核页只作为信息展示；`WikiMetrics` 使用显式 close/context manager 释放连接，`scripts/verify_installation.py --json` 调用 doctor 的子进程等待预算为 60 秒。

`checks.runtime_producer_consumer` 使用 `mnemos.runtime_producer_consumer.v2` 和 `producer_consumer_ledger.db`，把 `docs/acceptance/adaptive_data_flows.json` 的 flow id 注册为 runtime topic，报告 produced/consumed/dead_letter、item_id 对账、last_produced_at/last_consumed_at、pending、lag 和预算；orphan outputs、no-source consumers、item mismatches 或 dead letters 超预算会进入 strict health，并映射到 `data_pipeline` scorecard 的 `producer_consumer.orphan_outputs`、`producer_consumer.no_source_consumers`、`producer_consumer.item_mismatches` 和 `producer_consumer.dead_letters`。`checks.adaptive_policy` 使用 `mnemos.adaptive_policy_coverage.v1` 报告覆盖域、规则数量、coverage_errors、active_shadow 和 overdue shadow；`checks.cognitive_learning` 使用 `mnemos.learning_signal.v2` 报告 raw/search/feedback/reflection 是否转化为 observation、policy patch 和 cognitive consolidation run；strict `checks.cognitive_readiness` 使用同一 readiness budget，失败会进入 `strict_failures`，避免 health 假绿；`mnemos status` 会列出 active shadow 的 experiment/config、old→new、metric_before 和 age_hours，便于人工 commit/rollback。

`checks.sqlite_disk_budget` 使用 `mnemos.sqlite_disk_budget.v1` 报告 `.db-wal`、Mnemos temp、snapshot、历史 `raw-vault-projection-*` 和 `raw_events.db` 的当前体积与增长率。用户通过 `python3 mnemos_cli.py health --json` 看到异常；`checks.auto_healing` 会给出 `auto_heal_state`、`repair_actions` 和用户介入预算。`.db-wal` 超预算可运行 `python3 scripts/repair_sqlite_disk_budget.py --apply --wal` checkpoint；超过 `storage.disk_budget.temp_stale_minutes` 的 Mnemos temp 可运行 `python3 scripts/repair_sqlite_disk_budget.py --apply --temp` 删除。snapshot、Raw projection 历史备份和 `raw_events.db` 只告警，不自动删除；Raw projection 备份必须先运行 `python3 scripts/audit_raw_projection_backups.py --json` 审计 metadata/manifest/recovery value，再经明确授权处理。SQLite 不再做整库加密，也不会生成加密副本 或临时解密副本。

`checks.distill_json_quality` 从 `distill_metrics.db` 汇总 `direct_json`、`markdown_json`、`balanced_json`、`fixed_json` 和 `failed` 路径，展示 fallback 成功率、自动修复次数、最终失败率和 24h 趋势；fallback 成功只记 debug/metrics，不再写误导性 warning，同类格式失败复盘按错误指纹合并。`checks.distill_cognitive_actions` 从 canonical action store 汇总命令、真实 target effect、互惠 receipt、`applied_without_effect`、`effect_without_action` 和 lineage gap；`applied` 必须由 Observation/Reflection/PolicyPatch/Relation 目标服务的稳定 effect id 与 before/after hash 证明，action DB 自签不算消费。存量先用 `python3 scripts/reconcile_cognitive_action_effects.py --json` 预览，停止 daemon、确认备份后再显式 apply/process；`python3 scripts/audit_cognitive_action_effects.py --strict --json` 是独立发布审计。`checks.wiki_route` 使用 `mnemos.wiki_route_health.v1` 严格检查可分类 Inbox 页、needs_review 页、正式区 source-prefixed 页和标题/basename 冲突预算；daemon `wiki_route` 服务会周期性运行 Charon 的路由闭环，但默认以 `write_relations=False` 只移动/标记页面，不写 KG cooccurrence 关系，也不触发 embedding-heavy 图谱构建。需要完整图谱关系时由手工 Charon connect 或显式重型调度执行。若旧 `distill_failed/` artifact 已确认对应任务完成且失败片段只是质量门拒绝，应移动到 `distill_failed_resolved/` 并保留原因，而不是留在 active 目录继续触发 health/doctor 队列警告。

`checks.security` 会报告 `pickle/weak_hash/permission/secret_inventory/legacy_key/keyring` 状态：`secret_inventory` 使用 `mnemos.secret_inventory.v1`，递归覆盖 `api_key/token/secret/password/credential/bearer/key_source` 字段并过滤 `token_budget`、`max_tokens`、`tokenizer` 等非密钥名称，只输出路径、引用来源和长度统计；`~/.mnemos/logs`、database logs、configs 和数据库目录在配置初始化时收敛为 `700/600`，权限违规会输出 `repair_actions`；keyring 缺失不会阻断 strict health，但会暴露 `keyring_error`、`keyring_status`、`keyring_risk_level=safe_but_not_best`、env fallback 接受状态和 keyring/keyref/env 迁移建议。`scripts/security_audit.py` 直接运行时会优先选择 repo `.venv`，并用同一解释器执行 bandit、pip-audit 和 health security；需要验证当前解释器依赖时使用 `--strict-env` 或 `--no-venv-autodetect`，缺少 dev tools 会输出 `uv pip install -r requirements-dev.txt` 修复提示。机器报告固定为 `mnemos.security_audit.v2`：Bandit、pip-audit 和 health security 都先转成 typed finding，再由 findings 唯一推导 counts/status/`ok`/退出码，严格保持 `ok == (blocking_count == 0)`；`legacy_key_rows`、plaintext secret、pickle/weak hash 或 degraded/failed/error/unknown health 状态都属于 blocking。`scripts/audit_release_privacy_security.py --strict --json` 会调用同一 validator 复核 schema、findings、counts、status、`ok` 与返回码，再汇总 strict config doctor、docs/repo sensitive 和诊断脱敏；任何 blocking finding 都会阻断 release，warning 会作为非阻断证据保留。动态 SQL identifier 只能通过 `core.db_utils.validate_sql_identifier()` 与固定 allowlist 后拼接，无法参数化的位置必须保留精确 `# nosec B608` 和非法 identifier 单测。

队列预算由 `checks.queues` 输出：distill `failed_budget=0`、`distill_processing_stale_budget=0`，high/critical recap pending 预算为 0，dialog reminder pending/active 默认预算为 500；超时 stuck 的 processing 蒸馏任务用 `python3 mnemos_cli.py distill reset-timeouts --minutes 30 --json` 返回 pending，再由 daemon 或 `distill drain` 继续处理；失败蒸馏用 `mnemos distill retry-failed --all` 或 `mnemos distill archive-failed --all --reason ...` 显式处理，high recap 用 `mnemos recap dismiss --all --severity high --reason ...` 或 `resolve` 批处理，旧提醒用 `mnemos reminder expire-stale --days 30` 过期，`--limit 0` 是显式 no-op。`--cognitive-readiness` 会输出只读认知就绪度基线，帮助判断 raw、Wiki metrics、KG、recap/reminder、投递和 outcome 账本是否支撑认知闭环，并内嵌 `mnemos.learning_signal.v2` 的 raw/feedback/search/reflection 转化 KPI。`scripts/audit_cognitive_readiness.py --json --budget` 会把来源、证据、消费者、行为反馈四段映射到预算、状态机和 `cognitive_assets` scorecard；来源预算只约束 `page_metrics.page_role=knowledge` 且路径非派生/系统/占位的真实知识页，`WikiMetrics.scan_all_pages()` 会把派生 KG/observation/reflection/feedback、系统报告、MOC、vault index、占位/骨架页写入 `page_role`，readiness 输出 `source_required_total`、`source_exempt_total`、豁免原因和样本，不能把 stale metric rows 或系统产物当作 source debt。`mnemos health --json` 的 strict `checks.cognitive_readiness` 复用同一预算，失败时顶层不再为 ok；`--record-gaps` 才会把当前缺口写入 ActionLedger 的 `cognitive_readiness_gap`。

ROOT-013 起，`mnemos.cognitive_readiness.v2` / `mnemos.learning_signal.v2` 要求逐项、可复算的 lineage coverage 和 freshness：缺/坏/旧 required schema blocked，空证据或 0/0 lineage 不能得 100；delivery 总行数不再等于 feedback，只有 visible delivery 的显式 feedback 或 reciprocal outcome link 才算 effect；raw→observation、driver→patch/no_patch、candidate→applied consolidation 都有 denominator/covered/uncovered/ratio，dry-run 不算 applied。默认 freshness window 为 30 天，health、doctor、scorecard 与 audit CLI 使用同一事实源。

`scripts/wiki_lint.py --summary --json` 输出 `mnemos.wiki_quality.v1`，把 Wiki missing_meta/orphan/broken_link/stub 映射到统一生命周期、预算线、人工清单和 `obsidian_experience` scorecard；重建后用 `--budget` 阻断超线，`--fix` 的元数据自动修复会写 ActionLedger。`mnemos distill evidence-backfill` 默认 dry-run，从 provenance 表和页面已有 frontmatter provenance 规划 Wiki source refs 回填；确认后加 `--apply` 才会写 `page_metrics`、页面 frontmatter 和 `99-Reports/认知数据就绪度/` 报告。frontmatter 只把已有 `来源事件ID`、`来源会话`、`source_session*`、`evidence_refs`、带蒸馏上下文的 `来源/source_agent` 转成可审计 refs，不为无 provenance 页面编造来源。`scripts/plan_cognitive_consolidation.py` 默认 dry-run，只报告候选与投影一致性；`--apply` 仅冻结候选 revision/hash，绝不写 Wiki、coverage 或删除 Raw。随后以 `--submit-run <run_id>` 创建 trusted-push proposal，人工批准后用 `--reconcile-run <run_id> --trusted-proposal-id <id>` 核验页面字节、逐候选 exact source ref 与六类 projection receipt，才写 `consolidation_coverage_receipts`。Raw 删除始终属于独立的 DataOwnership 工作流，不能由本命令执行。

`KnowledgeTrustScorer` 会为 create_page extraction、apply 阶段的 consolidation method page extraction、merge/update、`predictive_push` 和 `push_feedback(ignore/dismiss/inaccurate/outdated)` 写 `~/.mnemos/trust_decisions.db`；`inaccurate` 记录 contradicted 证据，`outdated` 记录独立过时证据。阈值在 `~/.mnemos/configs/main.json` 的 `trust.*` 中调整，不需要改代码。`RetrospectiveConsumptionRouter.route_after_finalize()` 会把用户确认的 recap lesson 编译给 `PolicyPatchStore`，写入 `~/.mnemos/policy_patches.db` 的 TTL/Scope/Severity 策略补丁；`ReflectionPolicyPatchConsumer` 会把高置信 Reflection/shift 转为候选 patch，但不会把生成式 `key_points` 当 trigger；不满足条件时写 `policy_patch_feedback:no_patch`。无明确稳定 trigger 的 lesson 会被拒绝。active patch 只通过 `preflight_inject` 和 `guard_check` 追加到清单；匹配只看当前 task/subtype/context 和显式 project scope，不使用 patch content 自证；候选按 task-fit/命中 trigger 排序、去重并受 `max_active` 干扰预算约束，同时返回 why-matched 字段。它不会写宿主 system prompt；`policy_patch.*` 控制启用、TTL、最低置信度和最大装载条数。

`KnowledgeDeliveryRouter` 会为主动投递写 `~/.mnemos/delivery_events.db`，`predictive_push`、`preflight_inject`、`guard_check`、`check_pending_recaps` 和 dialog reminder 都会写 delivery decision；`preflight_inject` 的 silent preload 只入账，不消耗也不受可见预算/同 topic 可见冷却阻断。`push_feedback` 必须回传 `predictive_push` 返回的 `delivery_event_id`，并由服务端 principal + project/session 精确校验；topic/latest fallback 已删除。每条反馈先追加为 `mnemos.feedback_event.v1`，再由 `feedback_receipts` 扇出到 penalty、outcome、adaptive scorer、delivery 和负反馈 trust 消费者；全部 required receipt committed 才返回 `terminal_status=complete`，否则返回 partial/pending、失败消费者和可重试状态。各投影以同一 `feedback_event_id` 幂等，失败或重启后只补未完成投影。dialog reminder 响应以及 search click/open/ignore/no_result 也会写 outcome，daemon search ignore detection 必须同步关闭原 `search_sessions` 的 `ignored_at/outcome_status/outcome_at`，不能只写独立 scoring signal。`ContextAwareSearch(wiki_base=...)` 的测试/自定义 Wiki 会把搜索会话写入该 Wiki 的 `.kg/mnemos.db`，默认配置 Wiki 才写全局运行库，避免临时搜索污染真实 health。`scripts/replay_delivery_decisions.py --json` 可用临时 DB 回放投递策略。

### 开发贡献

```bash
# 安装 pre-commit 钩子（推荐）
pip install pre-commit
pre-commit install

# 一键跑本地门禁（包含 config examples / hardcoded path / docs freshness / desktop facts / docs sensitive info / repo sensitive literal / release privacy security audits）
python3 scripts/run_local_gates.py
python3 scripts/audit_hardcoded_paths.py --strict
python3 scripts/audit_docs_freshness.py --strict
python3 scripts/audit_docs_sensitive_info.py --strict
python3 scripts/audit_repo_sensitive_literals.py --strict
python3 scripts/audit_release_privacy_security.py --strict
python3 scripts/audit_adaptive_policy_matrix.py --strict

# 满分总验收入口：默认产物写 /tmp，严格模式遇到阻塞项返回非 0；发布/满分 real-api 运行禁止 skip
python3 scripts/run_full_score_gates.py --strict --real-api
python3 scripts/verify_full_score_certificate.py /tmp/mnemos-full-score-release/full_score_gates.json
python3 scripts/run_full_score_gates.py --strict --only health.strict --output-dir /tmp/mnemos-full-score-gates-health-smoke
python3 scripts/audit_test_suite_denominator.py --strict --json
python3 scripts/audit_gate_hermeticity.py --suite diagnostics --strict --json --output-dir /tmp/mnemos-diagnostics-hermetic

# 开发债务 closure：允许精确、未过期 residual，但明确非发布证书
python3 scripts/check_maintainability_budget.py --closure
python3 scripts/check_zombie_code_policy.py --closure
python3 scripts/ci_ratchet.py --closure --strict

# 发布 zero-closure：maintainability / zombie / vulture residual 必须为 0
python3 scripts/check_maintainability_budget.py --closure --strict --json
python3 scripts/check_zombie_code_policy.py --closure --strict --json

# Orphan module audit 默认只读输出 stdout；CI 比较不写文件
python3 scripts/audit_orphan_modules.py
python3 scripts/audit_orphan_modules.py --check

# 只有显式写入 repo 报告时才允许 --output + --apply，并记录 ActionLedger
python3 scripts/audit_orphan_modules.py --output docs/orphan-modules-report.md --apply

# 安全审计 direct 入口会优先使用 repo .venv；--strict 会阻断 Bandit medium 回归
python3 scripts/security_audit.py
python3 scripts/security_audit.py --strict --json
python3 scripts/security_audit.py --strict-env
python3 scripts/audit_release_privacy_security.py --strict --json

# 公开配置样例必须从 DEFAULT_CONFIG/env 分组生成并保持 100% 覆盖
python3 scripts/generate_config_examples.py
python3 scripts/verify_config_examples.py --strict

# 按反馈速度跑测试；根入口与 scripts 入口共用同一 layered runner
python3 run_tests.py quick
python3 run_tests.py integration
python3 run_tests.py system  # 仅 system test；Linux/macOS/Windows CI 共用 hermetic runner
python3 run_tests.py heavy
python3 run_tests.py full
python3 scripts/run_golden_benchmark.py --strict --mock-llm
python3 scripts/e2e_probe.py --dry-run
python3 scripts/e2e_probe.py --real-api
python3 scripts/e2e_wow_probe.py --dry-run
python3 scripts/e2e_wow_probe.py --mock-llm
python3 scripts/e2e_wow_probe.py --real-api
```

测试与门禁默认运行在唯一的 `mnemos.hermetic_run_environment.v1` 沙箱中：HOME、Mnemos/database/wiki、XDG、temp、pycache 和 artifacts 都属于同一个 `sandbox_root`，manifest 固定输出 `environment_hash`、`outside_write_count` 与 `formal_state_diff`。quick/integration/system/heavy、diagnostics 和非 real-api full-score 不继承本机 API 凭据；只有显式 `--real-api` 才传递受控凭据。`system` 是 Linux/macOS/Windows CI 共用的 OS-neutral system-test 入口，workflow 不再自行拼接 shell 专属临时目录或环境赋值。`--output-dir` 本身就是 sandbox root，必须不存在或为空，脚本不会清空/复用旧目录。`mnemos health`、`mnemos status`、`mnemos distill status` 与 `scripts/verify_installation.py` 默认只读，缺库时不得建目录/表；确需权限写探针时显式使用 `scripts/verify_installation.py --write-probes`。Golden benchmark 必须传 `--output-dir` 或在门禁沙箱中使用 `MNEMOS_RUN_ARTIFACTS_DIR`，不再覆盖共享 `~/.mnemos/benchmarks/golden/latest`。

发布认证与开发诊断是两个结果面。`--strict --real-api` 拒绝 `--only` 和所有 skip 选择器，只有当前 canonical 47-gate manifest 的 expected/selected/executed 完全相等、omitted 为空、全部 required gate 通过、工作树干净且绑定完整 Git commit 时，`mnemos.full_score_gates.v2` 才输出 `certifying=true/release_eligible=true`。47 个 gate 包含 maintainability/zombie/vulture 三个 strict zero-closure、required Desktop profile 的 `docs.asset_manifest.strict`、认知动作真实 effect closure、`contracts.cognitive_calibration_lineage` 零缺口，以及要求每个直接 provider sink 使用预留/结算账本的 `model_call_ledger.static`。每个 gate receipt 绑定 stdout/stderr SHA-256；`scripts/verify_full_score_certificate.py` 会对比当前代码的权威 manifest、完整 commit、干净工作树、receipt 和 artifact hash。非发布 `--only` 可以返回成功供排错，但始终是 non-certifying。测试分母由 `scripts/audit_test_suite_denominator.py` 保证当前 pytest 文件恰好归属 quick/integration/heavy；认知行为 10 个场景由 `scripts/run_cognitive_behavior_scenarios.py` 实际执行，不再只检查 schema/path。

模型调用账本由 `core.telemetry.model_call_ledger` 实现，`core.telemetry.prompt_call_log` 仅保留静态兼容导出，不产生第二条持久化路径。它只保留本地脱敏/opaque 的核算元数据，不持久化个人隐私、API key、银行卡信息、密码、raw prompt/response 或 caller error。`scripts/audit_model_call_ledger.py --json` 证明的是直接 provider 边界的静态契约；真实旧库迁移、健康复核和恢复演练是独立的运行态证据，详见 [Model Call Ledger](docs/MODEL_CALL_LEDGER.md)。

文档与 Prompt 资产使用 `mnemos.document_asset_manifest.v1` 统一发现和证明。当前分母为 70/70 tracked Markdown、23/23 Prompt/schema、25/25 Desktop system-map assets，exclude=0、unverified=0。freshness 与 sensitive 共用 tracked Markdown 自动发现；Prompt 必须绑定精确 hash、真实 consumer symbol 与 schema/inline output contract；Desktop `00–10` 同时绑定 current-state 和 repo 锚点，`86–98` 头部绑定当前 commit。运行 `python3 scripts/audit_document_asset_manifest.py --strict --desktop-mode required --json` 可独立复验。

`knowledge_graph.db.relation_evidence` 由 `core/kia/relation_evidence_schema.py` 独占 DDL、`mnemos.relation_evidence_schema.v1` version 和 semantic DDL hash。`KnowledgeGraph`、`RelationManager` 启动时先验证现有 columns/defaults/FK/index/registry，旧 KnowledgeGraph schema、旧 RelationManager defaults schema、索引缺失或未知结构不会再被 `IF NOT EXISTS` 静默接受。运维先运行 `python3 scripts/reconcile_relation_evidence_schema.py --json`；确认没有缺失/空白 `evidence_type` 后，停止 daemon，再使用 `--apply --backup-dir <dir> --json` 生成 integrity=ok SQLite 备份并事务迁移。`python3 scripts/audit_schema_registry.py --strict --json` 已进入 local/pre-commit/CI/full-score，要求生产 DDL owner 恰好一处且实际 hash 与 registry 一致。

详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

`scripts/e2e_probe.py` 是运行态端到端探针，不再把 `sync_log.status=new`、`skipped_backend` 或空 `backend_uids` 当作真实 L1/backend 落地证明。canonical raw 模式必须反查到 `raw_events.db.raw_turns` 的本次 `event_id`，外部 backend 模式必须从 `sync_log.backend_uids` 反查到实际记录；Wiki 检查必须命中本次 `session_id`，蒸馏跳过时明确标记 skip。`--dry-run --no-api` 默认把 wiki/database/raw vault 路径脱敏为 `<HOME>` / `<REPO>` / `<PATH>`，只有 `--unsafe-debug` 或 `--show-paths` 本机排错才输出原始路径。cleanup 输出会分开报告 `raw`、`sync_log`、`wiki`、`backend` 清理数量。

`scripts/e2e_wow_probe.py` 是用户价值端到端探针，覆盖首次配置三项必填模型与可选多模态、可信用户文档 100MB gate、默认 distill、行为/意图字段、Obsidian 路由、ContextAwareSearch/preflight 召回、runtime consumer ledger 和 auto-heal dry-run。开发/CI 默认用 `--mock-llm` 在临时 vault 中跑完整链路；发布验收可用 `--real-api`，`run_full_score_gates.py` 的 E2E gate 已指向该 wow probe。

### 命令行工具

```bash
mnemos setup --dry-run --json      # 产品级安装计划与结构化 repair action
mnemos setup                       # 推荐安装/配置/验证入口
mnemos upgrade plan --json         # 升级计划：migration plan + backup preflight
mnemos uninstall --preserve-data --json  # 默认保留数据的卸载状态
mnemos init                       # 交互式配置向导
mnemos doctor                     # 系统诊断
mnemos doctor repair-all --json   # 安装旅程全量修复计划
mnemos doctor --cognitive-readiness --json  # 认知系统就绪度只读审计
python3 scripts/audit_cognitive_readiness.py --json --budget  # 认知就绪度预算门
python3 scripts/audit_cognitive_readiness.py --budget --record-gaps  # 显式写入缺口账本
mnemos distill evidence-backfill --json     # 证据回链 dry-run；--apply 才写入
mnemos distill actions --json               # 只读查看蒸馏 action/knowledge action 日志
python3 scripts/plan_cognitive_consolidation.py --json  # 认知压缩/遗忘 dry-run
python3 scripts/plan_cognitive_consolidation.py --json --record-run  # 记录 dry-run run 账本
mnemos status                     # 查看系统状态
mnemos config                     # 查看/编辑配置

# Agent 管理
mnemos agent list                 # 列出本地可用的 AI Agent
mnemos agent install              # 安装 adapter hooks + MCP-only 主动接入
mnemos agent repair               # 修复主动接入并重跑满血验收
mnemos agent detect               # 检测已安装的 Agent
mnemos agent doctor               # 诊断 Agent 状态
mnemos doctor repair              # 一键修复 Agent 主动接入

# 后台服务
mnemos daemon start               # 启动后台守护进程
mnemos daemon stop                # 停止后台守护进程
mnemos daemon status              # 查看守护进程状态
mnemos scheduler install-windows  # 注册 Windows 开机启动
mnemos scheduler uninstall-windows # 卸载 Windows 开机启动

# 数据库维护
mnemos db maintenance             # 运行数据库存留清理与维护

# 事件系统
mnemos events stats               # 查看事件队列统计
mnemos events cleanup             # 清理过期事件

# 评分系统
mnemos scorer status              # 查看评分器状态和模式
mnemos scorer retrain             # 手动触发重训练
mnemos scorer rollback            # 回滚到上一版本模型

# 同步系统
mnemos sync status                # 查看同步状态
mnemos sync retry-failed          # 重试失败的同步任务

# 搜索与报告
mnemos search <query>             # 上下文感知搜索
mnemos report generate            # 生成每周画像报告

# 其他
mnemos calibrate                  # 启动画像校准流程
mnemos mcp serve                  # 启动 MCP 服务器
```

**完整命令速查**（当前 `mnemos --help` / `python3 mnemos_cli.py --help` 列出 58 个顶层命令，部分含 experimental 子命令）：

```bash
mnemos agent {list|install|detect|doctor}
mnemos backup {create|list}
mnemos blindspot {list|status|ignore|resolve|cleanup}
mnemos build-relation-index
mnemos calibrate
mnemos capsule {list|due|overdue|complete|snooze|report}
mnemos cognitive-graph {stats|reconcile|ingest}
mnemos config
mnemos data {inventory|export|freeze|delete}
mnemos daemon {start|stop|status|run}
mnemos db {maintenance}
mnemos dispute {scan|list|resolve|rollback-context|stats|weights|show}
mnemos distill {status|drain|audit|backfill-metadata|evidence-backfill|actions}
mnemos doctor
mnemos doctor --cognitive-readiness
mnemos entropy {scan|auto-fix}
python3 scripts/audit_golden_benchmark_contract.py --strict
python3 scripts/run_golden_benchmark.py --strict --mock-llm
mnemos events {stats|cleanup|archive-orphans|replay}
mnemos feedback {stats}
mnemos freshness {list|refresh|refresh-all}
mnemos health
mnemos ingest
mnemos import <path> --mode {parse|capture|distill|watch}
mnemos kg {doctor|rebuild-entities|consistency|normalize-endpoints}
mnemos link-probe {run|status}
mnemos migrate {status|plan|apply|rollback}
mnemos metrics {scan}
mnemos observe {run|search|stats}
mnemos perf
mnemos persona {behavior-metrics}
mnemos reflect {on|manual|pending|feedback}
mnemos reminder {list|push|resolve}
mnemos report generate
mnemos restore {plan|apply|verify}
mnemos scorer {status|retrain|rollback}
mnemos search <query>
mnemos shadow {sync|status}
mnemos signals {list}
mnemos status
mnemos stress {run|status}
mnemos sync {status|retry-failed|backfill|audit}
mnemos vaults {status|sync}
mnemos version {list|diff|restore|create|scan-all}
mnemos wiki {read|rebuild}
```

> 注：未在 README 详细说明的子命令多为高级/调试/实验性功能，使用 `mnemos <command> --help` 查看具体参数。

兼容入口：`context_search.py`、`blindspot_discovery.py`、`predictive_push.py`、`build_embedding_index.py`、`index_manager.py` 仅作为旧命令迁移 wrapper 保留；新文档和新脚本应优先使用 `mnemos ...` 或 `scripts/build_embedding_index.py`。

## 与 AI Agent 集成

### 方式一：MCP 协议（推荐，通用）

任何支持 MCP 的 AI Agent 都可以接入。接入后，Agent 可以使用以下工具：

`mnemos agent install` 会为每个宿主签发独立的高熵 launch capability；宿主配置只保存 `0600` 权限的 keyring reference，明文 capability 不写入配置、备份、日志或数据库。MCP stdio 进程启动时从 `AgentAuthorizationStore` 解析不可变的服务端 `PrincipalEnvelope`，并在每次 tool call 前重新验证撤销/过期状态。52/52 个工具必须命中统一 policy registry；caller 不能再通过 `agent`、`source_agent`、`allow_cross_agent` 或 `authorized_agents` 自报身份/扩权，`session_id/project` 只能收窄服务端 grant。

Wiki/raw/search 候选必须携带完整 ACL envelope；缺字段、冲突、未知来源均 fail closed。`wiki_read` 先规范化路径并只读 frontmatter 授权，通过后才读取正文；搜索热度、训练、画像、搜索会话、点击、提醒冷却和推送历史等副作用只接收已授权结果。跨 Agent/project 能力由用户显式运行 `mnemos agent grant-mcp <agent> ...` 配置，更新或撤销 grant 会立即作废旧 launch capability，随后需重跑 `mnemos agent install <agent>`。

**知识库操作**

| 工具 | 用途 |
|------|------|
| `wiki_search` | 搜索知识库（多来源：文件导入、人工输入、raw vault、蒸馏、复盘、Git） |
| `wiki_read` | 读取指定页面（经语义索引、评分、标签处理） |
| `wiki_write` | Agent 写入 Wiki 页面（蒸馏结果、生成的新知识） |
| `wiki_build` | 触发 Wiki 构建（扫描 → 蒸馏 → 生成页面 → 索引更新） |
| `memory_write_project` | 写入项目级记忆 |
| `memory_write_framework` | 写入框架级记忆 |
| `memory_write_global` | 写入全局级记忆 |
| `memory_search` | 按 project/framework/global 范围搜索记忆 |
| `knowledge_source_list` | 查看知识库来源分布统计 |

**知识摄入与蒸馏**

| 工具 | 用途 |
|------|------|
| `knowledge_ingest` | 用户主动口述/投喂知识 — 当用户说"记住这个"时调用；文件导入后写入 Wiki |
| `knowledge_distill` | 通过 LLM API 触发知识蒸馏 — 聊天记录转为结构化 Wiki 知识 |
| `document_process` | 处理用户指定路径文档（PDF/PPT/Word/Excel/HTML/EBOOK）；默认 trusted_user_document → canonical raw → capture outbox → Amphora → 质量门 → Wiki，立即返回 `accepted`/`pending`；`mode=capture` 只写 raw，`mode=parse` 才只预览；结果含 stable document asset、raw revision 与来源字段 |

**会话捕获（推荐）**

| 工具 | 用途 |
|------|------|
| `capture_turn` | 逐轮上报对话（低延迟 < 200ms，实时入队） |
| `capture_session` | 批量上报整个 session 的多轮对话 |
| `end_session` | 标记 session 结束，触发后续处理 |
| `capture_status` | 查询 session/turn 在队列中的状态 |
| `session_search` | 搜索历史会话（自动合并分片，恢复完整对话） |

**KIA 闭环**

| 工具 | 用途 |
|------|------|
| `preflight_inject` | 任务前装载历史经验（KIA 闭环第一步） |
| `guard_check` | 执行中风险守护（KIA 闭环第二步） |
| `retrospective_list` | 列出可用的 retrospective 经验（返回 path/title/task_type/subtype/version） |
| `check_pending_recaps` | 检查待复盘事项，推动任务收尾和复盘闭环 |
| `recap_start` / `recap_submit` / `recap_finalize` | 结构化三问复盘，确认后写入 `06-Retrospectives/复盘/`；required target receipts 完整后才返回 consumed |
| `recap_skip` / `recap_feedback` / `recap_status` / `recap_claim_owner` | 记录跳过原因、执行 durable correction、暴露 plan/receipt/feedback 状态和多 Agent owner 锁 |

`preflight_inject` 和 `guard_check` 是宿主 Agent 的高频入口；画像信号库默认 SQLite 连接/忙等待预算为 2 秒。当 daemon 正在持有画像相关 SQLite 连接导致 `PreFlightInjector` 暂时无法初始化时，MCP 工具应返回成功降级响应而不是工具执行错误：`preflight_inject` 会带 `degraded_reason`，`guard_check` 会回退到默认高风险守护清单继续判断。
同一降级边界也适用于 reflection 和 persona metrics：画像库暂不可用时，reflection 入口以 `persona_store=None` 继续运行，`persona_behavior_metrics` 返回基础行为指标并把 `profile_usage` 置为空指标。
`guard_check` 触发的 `guard_alert` 是可丢弃遥测事件；当前进程没有 EventBus 消费者时，`publish_event` 会直接跳过全局 EventBus 初始化，避免因为 daemon 持有 `events.db` 锁而把 MCP 响应拖成工具错误。daemon 启动时会先注册 KIA 模块订阅，再启动 EventBus dispatch；无消费者但需持久化的事件进入 dead-letter，可丢弃 telemetry 归档，不应长期停在 pending/processing。

**决策、搜索与推送**

| 工具 | 用途 |
|------|------|
| `context_aware_search` | 上下文感知搜索（画像加权 + 知识图谱召回） |
| `intent_route` | 意图路由（自动分类：回忆/知识/任务/闲聊） |
| `intent_correct` | 记录用户确认后的真实意图，纠正路由结果 |
| `blindspot_check` | 盲区检测（检查知识库覆盖缺口） |
| `freshness_check` | 知识新鲜度检查（版本绑定 + 过时预警） |
| `predictive_push` | 预测性知识推送（基于当前上下文主动推荐） |
| `push_feedback` | 按 `delivery_event_id` 反馈 accept/ignore/dismiss/inaccurate/outdated，并返回 consumer receipts |

**画像与信号**

| 工具 | 用途 |
|------|------|
| `persona_summary` | 获取用户画像摘要（能量/认知/价值三层雷达 + `user_cognitive_profile_v2`） |
| `persona_behavior_prompt` | 获取画像驱动的 AI 行为提示词，并返回/记录画像 v2 消费 |
| `persona_behavior_metrics` | 获取画像行为提示最近 N 天的使用指标和 `profile_usage` 消费效果 |
| `persona_update` | 触发画像更新（采集最新信号并重新计算） |
| `signal_collect` | 触发信号采集 |

**Observation & Reflection**

| 工具 | 用途 |
|------|------|
| `observation_run` | 运行 Observation Engine，提取客观观察 |
| `observation_search` | 搜索 Observation Index |
| `reflect_on_input` | 基于输入自动触发 Reflection |
| `reflect_manually` | 手动触发 Reflection |
| `reflection_feedback` | 对 Reflection 提交反馈 |
| `reflection_pending` | 获取等待反馈的 Reflection 列表 |

**系统**

| 工具 | 用途 |
|------|------|
| `health_check` | canonical 系统健康快照（与 CLI 同一 30-check 集合和 hash） |
| `agent_runtime_probe` | 授权宿主提交固定 synthetic-safe 样本，生成不含正文的近期运行能力回执 |
| `self_diagnose` | Mnemos 自诊断（数据源、L1 storage、Wiki 目录状态） |
| `detect_sources` | 检测所有 Agent 数据源和外部系统的连接状态 |
| `configure_wiki` | 配置 Wiki/Obsidian 路径 |

配置示例：

```json
{
  "mnemos": {
    "command": "mnemos",
    "args": ["mcp", "serve"]
  }
}
```

### 方式二：Claude Code Hooks

运行 `mnemos init` 时会默认尝试为检测到的 Agent 安装 hooks；Claude Code 会写入 `~/.claude/settings.json`。安装失败时运行 `mnemos doctor repair claude` 修复静态接入；`repair` 不会替代用户授权或伪造运行回执。

### 方式三：Hermes / Kiro / OpenClaw / OpenCode / Codex Agent

Codex、Hermes、Kiro、OpenCode、OpenClaw 走 MCP-only 主动接入 + passive source 被动采集，不恢复旧 adapter。`mnemos setup` 和 `mnemos agent install` 默认会写入它们的 MCP 配置与主动策略；若本机配置被手动改坏，再运行以下命令修复：

```bash
mnemos doctor repair
mnemos doctor repair-all --json
mnemos agent kit
mnemos doctor
```

Agent Kit 的唯一来源定义是 `core/agent_kit/agent_source_support_manifest.json`。它当前把 `codex/claude/hermes/opencode/openclaw/crush/kiro/kimi` 声明为 8 个 host Agent；用户不需要同时拥有全部 host，未安装目标显示 `not_installed`，其 conformance/runtime 为 N/A。已安装目标具备 active workflow、Mnemos MCP、Active Policy、full-fidelity passive source 和认知证据声明时只显示 `conformance_ok=true`；只有内容访问已授权、同一 host 的 canonical health 握手新鲜、固定 completeness 样本验证成功且回执未过期时，才显示 `full_power=true`。旧报告或无回执状态一律是 `runtime_unverified`，不会变绿。

同一 manifest 将 Aider、Gemini、Cursor、Windsurf 声明为 `ingestion-only`：它们的 native→Raw、ACL、fidelity、retention、continuous/backfill 合同仍受审计，并在 `mnemos agent kit --json` 的 `ingestion_sources` 中显式呈现；它们绝不计入 8 个 host 的 full-power 分母，也不能被安装为 host active policy。每个已声明来源的 Raw receipt、daemon/backfill `NativeSourceSnapshot` 和 host runtime receipt 都绑定 `support_manifest_hash`，manifest 变化后旧 runtime receipt 不可复用。连续采集由默认启用的 `daemon.raw_sync` 统一持有；watchdog/polling/hybrid trigger 只加速变更，不是唯一闸门。每轮扫描会把不含路径或正文的逐源发现/采集/游标/gap/error 状态发布到 daemon heartbeat；用 `python3 scripts/audit_agent_source_coverage.py --strict --json` 独立验证 8 个 host 的实际 owner 和 Native→Raw 覆盖。调度采用近期 tail 与持久化全分母 reconciliation；每 source/session 的 canonical Raw cursor 只在成功 receipt 后推进，配置的单批 session/turn 上限只能限制延迟，不能把旧会话、长会话尾部或 CLI partial/dry-run 伪写为全局完成。

满血版不是“能连上就算完成”。所有已安装目标都必须声明并验证 `visible_text`、`tool_calls`、`tool_results` 和 `source_fidelity=full`；Codex / Claude / Hermes / OpenCode / Kiro / Kimi 还必须暴露可采集的 reasoning/thinking 信号，Codex / Crush / Kiro / Kimi 还必须能采集附件、媒体或文件上下文证据。模型私有思维链不会被强制解密或保存；Codex 这类只暴露 reasoning 摘要/加密引用的来源，会按 metadata/summary 证据处理。

运行验收的机器契约由 `mnemos agent kit --json` 的 `runtime_probe_contract` 给出：宿主先调用 `health_check` 取得当前 `health_check_ids_hash`，再原样调用 `agent_runtime_probe`。该步骤只允许固定安全哨兵文本/工具对，不读取真实会话正文；结果写入 `agent_authorization.db.agent_health_roundtrips/agent_runtime_receipts`。`mnemos agent repair` 只修静态接入缺口，静态已合规但运行未验收时会明确提示授权与探针步骤，不会重复安装。

多模态证据不是把本地绝对路径写进 Wiki。Agent Kit 的 `artifact_uri_context` 样本要求目标 Agent 能把工具结果、附件、截图、终端输出和测试报告归一成 `artifact_refs`；capture URI 只定位来源，蒸馏阶段由系统 catalog 按完整 SHA-256 生成 content identity。路径、canonical URI、MIME、SHA 和 ACL 都不交给模型生成，蒸馏页面只保留系统解析后的摘要链接。

验收样本和数据契约入口：

- `docs/acceptance/agent_acceptance_samples.md`
- `docs/acceptance/raw_event_contract.md`
- `docs/acceptance/distilled_knowledge_contract.md`
- `benchmarks/golden/manifest.json`
- `benchmarks/golden/baseline/mnemos_benchmark_scorecard.json`
- `python3 scripts/verify_acceptance_contracts.py`

独立接入文档见：

- `docs/integrations/codex.md`
- `docs/integrations/crush.md`
- `docs/integrations/hermes.md`
- `docs/integrations/kiro.md`
- `docs/integrations/kimi.md`
- `docs/integrations/opencode.md`
- `docs/integrations/openclaw.md`

## 与 Obsidian 的关系

Mnemos 与 [Obsidian](https://obsidian.md) 是互补关系，不是替代。

### Obsidian：知识库的可视化与人工编辑

- Mnemos 的知识库层是**纯 Markdown + YAML Frontmatter**，不绑定任何特定工具
- 部署阶段要求安装 Obsidian：
  1. **原生兼容**：Obsidian 的笔记格式就是 Markdown，无需导出/转换
  2. **双向链接**：`[[页面名]]` 语法自动构建知识图谱
  3. **图谱视图**：Obsidian 的 Graph View 就是知识图谱可视化
  4. **社区生态**：Dataview、Templater 等插件可与 Mnemos 的数据联动
  5. **本地优先**：和 Mnemos 的数据隐私策略一致，所有知识库内容存本地
- Obsidian 是 [Obsidian Corp 的产品](https://obsidian.md)，Mnemos 不是 Obsidian 的插件或官方衍生品
- 配合方式：Obsidian 负责**知识的组织、可视化、人工编辑**；Mnemos 负责**知识的自动采集、raw 投影、蒸馏、评分、画像驱动、闭环进化**。人管创作，AI 管运营。
- Raw Vault 是 `raw_events.db` 的可读展示层；默认不作为反向采集入口，避免投影文件再次进入 L1 形成重复循环。正式 Raw 是无截断的 `lossless-visible-v1`：每个 current revision 的可见字段有 byte hash，增量 journal 只原子替换关联 chunk；`python3 scripts/audit_raw_projection_fidelity.py --strict --json` 可从 Markdown 反向验证 canonical 字段。Observation 的 retained-Markdown compatibility reader 复用该 v2 parser，并逐字段 hash 对照 current Raw；校验失败会拒绝输入而不是降级解析。

### 数据所有权

- Mnemos 的知识库存储在你的本地磁盘（默认 `~/Documents/mnemos`；raw 原始记录默认在 `~/Documents/raw`，均可自定义）
- 所有数据以纯 Markdown 文件形式存在，你可以随时用任何文本编辑器打开
- 画像数据存储在本地 SQLite 数据库中，不会上传到任何服务器
- `mnemos data inventory --json` 可列出 raw、Wiki、metadata、evidence refs、persona、reflection、scoring、Action Ledger、model-call ledger、consumer access log 和 agent source metadata 的本地保存位置、估算记录数、消费者和导出/冻结/删除策略。
- `mnemos data export --dry-run --scope all --json` 生成 secret-redacted manifest；`freeze` 会记录冻结请求；`delete --dry-run` 先列出原始数据、派生产物、索引、KG、画像、scorecard、model-call ledger 和 access log 影响范围；真正 apply 必须已有 freeze、snapshot ref 和确认。
- Mnemos 不会收集、上传或分享你的任何数据

## 配置

运行时权威配置文件位于 `~/.mnemos/configs/main.json`（跨平台统一路径）。
旧版 `~/.mnemos/config.yaml` 仅作为迁移来源，系统会在首次读取时迁移到 JSON。
旧配置 key、旧环境变量、旧 Vault 布局和独立迁移脚本由 `mnemos migrate status|plan|apply|rollback --json` 统一管理；配置 stale key 迁移会先写本地备份，再清理退役的 daemon service 别名、旧 HTTP token 配置和已移除开关。公开配置示例必须使用 canonical `daemon.services.eventbus`；`scripts/audit_docs_stale_service_keys.py` 已接入本地门禁，防止 README/docs 再出现旧服务键的 live config 示例。`scripts/audit_docs_freshness.py --strict` 默认扫描 AGENTS、CLAUDE、CONTRIBUTING、README、README-en、SECURITY、docs 以及可发现的 `~/Desktop/mnemos系统图谱`，也支持 `--paths` 指定正式扫描面；该 gate 同时校验 fenced shell 命令里的 repo 相对路径存在，并要求 `mnemos config set <key>` 示例出现在 `config/config.example.json` 中；`scripts/audit_desktop_system_map_facts.py` 已接入本地门禁，用 `99-代码扫描-facts.json` 的 `current_state` 校验桌面系统图谱机器事实对应当前 repo commit，并要求成功的 `python3 run_tests.py quick` 结果精确绑定该 commit。`run_local_gates.py` 本身包含这个 audit，因而不得要求其未来的自我回执作为前置条件；实际 local-gates 成功结果可在执行后作为历史证据记录。`scripts/audit_docs_sensitive_info.py --strict` 扫描 README、README-en、SECURITY、CLAUDE、AGENTS 和 docs，阻断 raw key/JWT、本机路径、真实 API endpoint、明文 credential 赋值、个人邮箱/手机号/身份证和 PII 赋值进入公开 Markdown；`scripts/audit_repo_sensitive_literals.py --strict` 扫描 git tracked 与未忽略的 untracked 文本，阻断测试/源码/文档中的完整 provider-shaped fake key、本机 home path 和明文 credential literal，并要求 redaction 测试用运行时拼接或安全哨兵值。`scripts/audit_release_privacy_security.py --strict` 把这些文档/仓库扫描与 strict security、strict config doctor、health security、health/config/`distill status`/E2E dry-run 诊断脱敏收口为发布级总门禁。`scripts/verify_config_examples.py --strict` 已进入 local gates、pre-commit 和 CI，确保公开配置样例与 `DEFAULT_CONFIG` 保持 100% 覆盖。配置、secret 和磁盘预算的 strict 验收使用 `python3 mnemos_cli.py doctor config --strict --json`，机器报告写入 `~/.mnemos/config_audit.json`；默认报告必须脱敏真实 API URL、本机绝对路径和 key source 细节，`--unsafe-debug` 只用于本机私有排错；底层 `mnemos.secret_inventory.v1` 递归扫描已加载配置树中的 `api_key/token/secret/password/credential/bearer/key_source`，明文风险只暴露字段路径和长度，不输出值。SQLite 磁盘预算由 `mnemos.sqlite_disk_budget.v1` 输出，预算配置在 `storage.disk_budget`。`scripts/repair_sqlite_disk_budget.py --dry-run` 只预览安全修复；`--apply --wal --temp` 只执行 WAL checkpoint 和过期 Mnemos temp 删除。snapshot 和 `raw_events.db` 属于历史/原始证据，不进入自动删除范围，必须由用户确认后处理。SQLite 不再做整库加密；旧 SQLite 加密配置由 `mnemos migrate apply config.stale_keys.v1 --json` 清理。

配置事实源是 `core/config_registry.py` 的 `mnemos.config_registry.v1`。`Config` 默认拒绝 unknown、removed、alias、类型错误和损坏配置；先用 `mnemos migrate plan --json` 预览全部迁移，再用 `mnemos migrate apply config.stale_keys.v1 --json` 在 `0600` 备份后原子迁移旧 key。`python3 scripts/verify_config_examples.py --strict` 按 flattened leaf 检查 JSON/YAML，`python3 scripts/audit_config_registry_closure.py --strict` 进一步对账 registry/read/example/test/doc/env/tier/migration，并阻断 caller fallback 漂移。daemon identity 同时保存配置文件字节哈希和 `config_fingerprint`，因此环境变量或 performance tier 改变也会被检测。

配置优先级：**代码默认值 < JSON 配置文件 < 环境变量**（环境变量优先级最高）。

支持的环境变量：

| 环境变量 | 对应配置项 | 说明 |
|----------|-----------|------|
| `MNEMOS_WIKI_DIR` / `WIKI_DIR` | `wiki.vault_path` | Wiki 知识库目录 |
| `MNEMOS_DIR` | — | Mnemos 数据根目录（默认 `~/.mnemos`） |
| `MNEMOS_LLM_API_KEY` | `llm.api_key_source` | LLM（对话/蒸馏模型）API Key |
| `MNEMOS_LLM_BASE_URL` / `MNEMOS_LLM_MODEL` | `llm.base_url` / `llm.model` | LLM 模型 API 地址与模型 ID |
| `MNEMOS_EMBEDDING_API_KEY` | `embedding.api_key_source` | Embedding（向量/语义召回模型）API Key |
| `MNEMOS_EMBEDDING_BASE_URL` / `MNEMOS_EMBEDDING_MODEL` | `embedding.base_url` / `embedding.model` | Embedding 模型 API 地址与模型 ID |
| `MNEMOS_RERANKER_API_KEY` | `reranker.api_key_source` | Reranker（搜索重排模型）API Key |
| `MNEMOS_RERANKER_BASE_URL` / `MNEMOS_RERANKER_MODEL` | `reranker.base_url` / `reranker.model` | Reranker 模型 API 地址与模型 ID；`base_url` 可为服务根地址或完整 `/rerank` endpoint |
| `CLAUDE_SETTINGS_JSON` | — | Claude Code settings.json 路径 |

关键配置项：

```json
{
  "wiki": {
    "vault_path": "~/Documents/mnemos"
  },
  "persona": {
    "enabled": true,
    "strategy_injection_enabled": true,
    "strategy_token_limit": 300,
    "skill_report_only": true,
    "data_sources": {
      "session": { "enabled": true },
      "git": { "enabled": false },
      "wiki": { "enabled": false },
      "file_system": { "enabled": false }
    }
  },
  "daemon": {
    "services": {
      "capture_worker": true,
      "eventbus": true
    }
  },
  "distill": {
    "max_tasks_per_cycle": 5
  },
  "policy_patch": {
    "enabled": true,
    "db_path": null,
    "ttl_days": 30,
    "min_confidence": 0.7,
    "max_active": 5
  },
  "delivery": {
    "preference": "balanced",
    "profiles": {
      "balanced": {
        "daily_total": 12,
        "per_task_total": 3,
        "per_task_hint": 2,
        "per_task_warn": 1,
        "force_open_daily": 0,
        "same_topic_cooldown_hours": 24,
        "dismiss_cooldown_days": 14,
        "overflow_defer_hours": 1
      }
    }
  },
  "verification_queue": {
    "enabled": true,
    "db_path": null,
    "report_path": null,
    "blindspots_db_path": null,
    "max_candidates": 50,
    "max_disputes": 10,
    "max_blindspots": 10,
    "max_freshness_alerts": 10,
    "cron": "20 16 * * *",
    "respect_resource_budget": true
  },
  "llm": {
    "provider": "openai-compatible",
    "base_url": "https://your-llm-api.example/v1",
    "api_key": "",
    "api_key_source": "env:MNEMOS_LLM_API_KEY",
    "model": "your-llm-model-id"
  },
  "embedding": {
    "enabled": true,
    "provider": "openai-compatible",
    "base_url": "https://your-embedding-api.example/v1",
    "api_key": "",
    "api_key_source": "env:MNEMOS_EMBEDDING_API_KEY",
    "model": "your-embedding-model-id",
    "embedding_model": "your-embedding-model-id",
    "use_rerank": true,
    "similarity_threshold": 0.72
  },
  "reranker": {
    "enabled": true,
    "provider": "openai-compatible",
    "base_url": "https://your-reranker-api.example/v1",
    "api_key": "",
    "api_key_source": "env:MNEMOS_RERANKER_API_KEY",
    "model": "your-reranker-model-id"
  }
}
```

投递偏好默认是 `delivery.preference=balanced`，初次安装不询问用户。需要更安静或更主动时，编辑运行时配置 `~/.mnemos/configs/main.json`，把 `delivery.preference` 改为 `quiet`、`balanced` 或 `active`，或直接调整 `delivery.profiles.<profile>` 下的预算和冷却值；`overflow_defer_hours` 控制 reminder 超出预算后的推迟时长。业务代码只读取这些配置，不应硬编码投递次数或冷却天数。

策略补丁默认读取 `policy_patch.*` 配置。`enabled=false` 会关闭策略补丁装载；`db_path` 为空时默认使用 `~/.mnemos/policy_patches.db`；`ttl_days`、`min_confidence`、`max_active` 控制补丁存活时间、采纳门槛和每次 preflight/guard 最大装载条数。`PolicyPatchStore.propose()` 要求显式、短且稳定的 trigger，空 trigger 或解释句不会成为通配匹配；`active_for()` 只接受当前上下文命中并执行 scope/task-fit/去重/干扰预算，响应解释字段为 `match_source`、`matched_triggers`、`task_fit_score`、`dedupe_key`、`interruption_budget_ok`。`ReflectionPolicyPatchConsumer` 不把生成式 `key_points` 当 trigger。存量清理使用 `python3 scripts/reconcile_policy_patch_triggers.py --json` 预览，显式 `--apply --json` 会先备份。这些值只应在 `~/.mnemos/configs/main.json` 或默认配置里调整，不应写死在 `preflight_inject`、`guard_check` 或 Agent 策略文件里。

执行中防分析循环默认读取 `guard.analysis_loop.*` 配置。默认 `max_analysis_turns_without_action=2`、`max_repeated_reads_per_target=2`，因此第 2 轮纯分析或同一文件/工具第 2 次重复读取就会触发 `guard_check` 提醒；需要更宽松时可改成 3 或更高。`guard_check` 的告警响应会返回 `threshold_source`、`threshold_value` 和 `current_count`，便于宿主 Agent 和审计报告确认当前触发语义。

受控求证默认读取 `verification_queue.*` 配置。`db_path`、`report_path` 和 `blindspots_db_path` 可显式覆盖运行态落点；为空时默认使用 `~/.mnemos/verification_queue.db`、`~/.mnemos/verification_report.md` 和 `~/.mnemos/blindspots.db`。`max_candidates` 和三类来源上限控制每次报告规模，`cron` 控制 Chronos 后台步骤时间，`respect_resource_budget=true` 时后台执行会先检查 `ResourceBudget.can_run("verification_queue")`；CLI 手动 `mnemos verify run` 默认仍是 dry-run。

#### 同端点多 Key 轮转（可选）

若同一模型端点有多个 API key，可配置 `api_key_sources` 让 Mnemos 在内存中自动轮转、失败冷却：

```json
{
  "llm": {
    "provider": "openai-compatible",
    "base_url": "https://your-llm-api.example/v1",
    "model": "your-llm-model-id",
    "api_key_sources": [
      "env:MNEMOS_LLM_API_KEY_1",
      "env:MNEMOS_LLM_API_KEY_2"
    ],
    "key_strategy": "weighted"
  }
}
```

- `key_strategy` 可选 `weighted`（按成功率加权，默认）、`round_robin`、`random`。
- 支持 `llm.api_key_sources`、`llm.providers.<provider>.api_key_sources` 以及 `llm.chain[*].api_key_sources` 三种层级。
- 失败冷却：429 → 1 分钟起步，5xx → 5 分钟起步，auth → 60 分钟，连续失败 5 次后 key 标记为 expired。
- Key 状态仅存内存，进程重启后重置；密钥仍只通过 `env:` / `keyring:` 读取，不持久化明文。

### 构建语义搜索索引

Embedding / Reranker 是部署阶段必配模型。配置通过 smoke test 后，可构建语义索引来提升未知查询的召回质量：

```bash
# 1. 安装可选依赖
pip install -e ".[ml]"

# 2. 确认部署阶段已经配置三类必填模型端点；多模态可选
export MNEMOS_LLM_MODEL=your_llm_model_id
export MNEMOS_LLM_BASE_URL=https://your-llm-api.example/v1
export MNEMOS_LLM_API_KEY=your_llm_key
export MNEMOS_EMBEDDING_MODEL=your_embedding_model_id
export MNEMOS_EMBEDDING_BASE_URL=https://your-embedding-api.example/v1
export MNEMOS_EMBEDDING_API_KEY=your_embedding_key
export MNEMOS_RERANKER_MODEL=your_reranker_model_id
export MNEMOS_RERANKER_BASE_URL=https://your-reranker-api.example/v1
export MNEMOS_RERANKER_API_KEY=your_reranker_key
# 可选
export MNEMOS_MULTIMODAL_MODEL=your_vision_model_id
export MNEMOS_MULTIMODAL_BASE_URL=https://your-vision-api.example/v1
export MNEMOS_MULTIMODAL_API_KEY=your_vision_key

# 3. 重新运行 setup 会 smoke test 三类必填端点，并用只读 E2E 探针收尾
python3 scripts/auto_setup.py

# 3b. 配置/secret/legacy/retention/daemon 一次性 strict 验收
python3 mnemos_cli.py doctor config --strict --json

# 4. 构建索引
python3 scripts/build_embedding_index.py

# 5. 验证
python3 scripts/verify_installation.py --full
python3 scripts/verify_installation.py --api-smoke
```

## 数据源与隐私

用户画像的数据源完全由用户自选。开启越多画像越精准，但隐私暴露也越多：

| 数据源 | 用途 | 隐私级别 |
|--------|------|---------|
| AI 对话 | 推断专注深度、质疑倾向、完美偏好 | 仅本地存储 |
| Git 提交 | 推断续航模式、创新倾向 | 仅统计信息，不存代码 |
| Wiki 交互 | 推断关注领域、学习路径 | 仅页面路径和动作类型 |
| 微信聊天 | 推断情绪模式、社交偏好 | 仅本地处理，不上传 |

画像信号会带 `scope/context`，例如 working_dir、session_tags 推断出的 work/personal/study/default，避免工作、个人、学习场景串场。用户认知画像 v2 还会为每条断言保留 `privacy_level`、`expires_at/status`、`supporting_signals`、`contradicting_signals` 和 `revision_policy`；低置信或被用户纠正的断言必须可被后续证据反驳或撤销。画像策略注入受 `persona.strategy_token_limit` 限制，可通过 `persona.strategy_injection_enabled=false` 关闭。行为驱动飞轮默认先生成 `cognitive_decision_asset.v1`（判断标准、失败边界、验证 recipe），不会直接写入或修改技能目录；automation skill 只能作为已验证资产的派生产物。

除核心 AI 对话采集外，扩展数据源默认关闭，用户需主动开启。微信数据源仅在本地处理，不涉及任何第三方服务。

## 技术栈

- **语言**：Python 3.10+
- **存储**：Markdown 文件（知识库）+ SQLite（画像/评分/图谱/调度）
- **协议**：MCP (Model Context Protocol) 用于 AI Agent 集成
- **蒸馏执行**：Mnemos 直接调用 LLM API（主备链 failover）
- **评分算法**：ComplementNB + TfidfVectorizer + 贝叶斯后验更新
- **聚类算法**：HDBSCAN → DBSCAN → K-Means 回退链
- **调度**：拓扑排序 + ThreadPoolExecutor 并行执行
- **文档处理**：PDF / PPT / Excel / Word / HTML / EBOOK 解析
- **核心依赖**：requests、pyyaml、watchdog、numpy

## 项目状态

**Mnemos v2.0.0** — 核心链路可用，高级能力持续优化中。

### v2.0.0 已可用（经过代码复查，生产可用）

- [x] **同步框架**：SyncEngine 8 步流水线 + 12 个 Agent Source + Raw Vault 兜底去重 + Raw→Wiki 追溯
- [x] **七层蒸馏流水线**：噪音过滤 → 价值预判 → LLM 判断 → 知识提取 → 自检 → 跨 Agent 关联 → 反馈循环
- [x] **知识图谱**：EntityManager + RelationManager + 置信度治理（source_method 上限）+ 上下文感知查询
- [x] **评分闭环**：COLD/WARM/HOT 三阶段 + 6 子系统评分器 + 训练样本收集 + 漂移检测
- [x] **质量保障**：Freshness 四态检测、Blindspot 降级暴露、Predictive Push relevance gate、搜索弱相关过滤
- [x] **数据治理**：raw completeness 审计、来源分布统一、KG 关系置信度上限
- [x] **热插拔架构**：14+ 子系统独立启停，故障自动隔离
- [x] **MCP 服务器**：多工具覆盖知识库/摄入/会话/KIA/画像/决策/系统
- [x] **Agent Kit 接入**：8 个目标（Codex / Claude / Hermes / OpenCode / OpenClaw / Crush / Kiro / Kimi）；静态 conformance 与授权后的近期 runtime receipt 分层，缺授权/回执不再误报 `full_power`

### 持续完善中

- [ ] **评分器冷启动**：需 ≥20 训练样本才能进入 WARM 模式，自然积累中
- [x] **语义搜索（Embedding/Reranker）**：部署阶段必配模型端点，支持向量索引 + 语义召回 + 搜索重排
- [ ] **Web Dashboard / 本地控制中心**：可视化知识图谱、画像趋势、API/系统配置项；当前未实现，现阶段请使用 CLI、MCP 和配置文件。
- [ ] **Obsidian 插件**：双向同步与内联查询

### 长期能力

- [x] 蒸馏 API 链（LLMApiChain 有序 failover，Mnemos 直接调用）
- [x] Agent Kit 接入（8 个目标；Claude/Kimi/Crush 为 adapter，Codex/Hermes/OpenCode/OpenClaw/Kiro 为 MCP-only；部署验证分别阻断静态不合规与授权/近期运行回执缺失）
- [x] MCP 服务器（多工具覆盖知识库/摄入/会话/KIA/画像/决策/系统）
- [x] 文档处理（PDF/PPT/Excel/Word/HTML/EBOOK 解析入库）
- [x] 热插拔模块化架构（14+ 子系统独立启停）
- [x] 语义搜索（厂商无关 Embedding/Reranker 端点，部署阶段 smoke 验证）
- [x] 可选多模态配置（`MNEMOS_MULTIMODAL_*`，配置后图片入口自动解析入库；未配置或 API 失败时生成可恢复任务）
- [ ] Web Dashboard / 本地控制中心
- [ ] Obsidian 插件

## 致谢

- [Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) —— LLM Wiki 模式的提出者，Mnemos 的核心灵感来源
- [Obsidian](https://obsidian.md) —— Mnemos 部署阶段必装的本地知识库可视化与人工编辑工具

## 许可证

[MIT License](LICENSE)

---

**Mnemos**（/ˈnɛmɒs/）—— 希腊神话中的记忆女神，谟涅摩叙涅。不是只帮你记住，而是在可审计、可配置、可降级的边界内，让 AI 更容易在合适的时候想起相关知识并辅助行动。
