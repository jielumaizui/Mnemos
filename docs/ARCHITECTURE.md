# Mnemos v2.0.0 架构设计文档

> 基于实际代码的完整架构说明
>
> **注意**：本文档已按当前 v2.0.0 架构刷新顶层模块图与职责说明。部分底层子系统（Ingest Engine / Guards / Pipeline）的描述仍保留在「核心数据流」等章节，但已移除旧版架构图。
>
> `core/agent_kit/agent_source_support_manifest.json` 是 AgentSource 的唯一 tracked 定义：当前 8 个 `host_agent`（`codex/claude/hermes/opencode/openclaw/crush/kiro/kimi`）使用静态 `conformance_ok` 与两段授权 runtime 证据分层；`installed`、`path_detected`、`discovery_covered`、`content_parsed`、`raw_committed`、`runtime_verified` 各自独立，passive source/声明存在不再等同于 `full_power`。Aider、Gemini、Cursor、Windsurf 是保留的 `ingestion-only`，只走受 ACL/fidelity/retention 约束的 native→Raw 路径，不能进入 host 分母。

## 架构变更注记（2026-06-13）

- **Agent 适配器层重构**：已删除的 `integrations/{caduceus,daedalus,musae,typhon}.py` 等 adapter 及其文档目录。Codex / Hermes / OpenCode / OpenClaw / Kiro 统一通过 `integrations/sources/` 下的 Source 模块被动采集会话，通过 **MCP 协议** 做 `preflight_inject`。
- **Wrapper 脚本下线**：`~/.codex/mnemos_wrapper.py`、`~/.hermes/mnemos_wrapper.py`、`~/.opencode/mnemos_wrapper.py`、`~/.openclaw/mnemos_wrapper.py` 及相关 launcher（`mnemos-codex` 等）已删除；它们从未被实际调用，也不具备对话捕获能力。
- **Source 模块恢复**：`integrations/sources/codex_source.py`、`hermes_source.py`、`openclaw_source.py` 从 git 历史恢复并注册到 `SourceRegistry`。
- **MCP 配置清理**：Hermes / OpenClaw 中指向不存在路径的遗留 MCP server 已移除，仅保留正确的 `mnemos` MCP server。
- **MCP 可信 Principal 与前置 ACL（2026-07-10 / ROOT-20260710-001）**：`core/agent_kit/authorization.py` 复用 `AgentAuthorizationStore` 保存 hash-only、可撤销/过期的 host launch capability 与显式 grant；8 个 Agent 配置只保存 keyring reference，并通过 `integrations/mcp_config_security.py` 以 `0600`、prepare → durable config → activate 的顺序原子轮换。`integrations/agora.py` 的 52/52 tools 与 `core.access_policy.MCP_TOOL_POLICIES` 必须精确闭合，每次调用先重验 server principal，再拒绝 caller identity/ACL override、未知参数和越权 project。搜索/RawIndex/direct read 统一携带严格 ACL envelope，direct read 只读 frontmatter 授权后才读取正文，授权前不写热度、训练、画像、搜索会话、点击、提醒冷却或推送历史。存量 Wiki/raw 由 `scripts/reconcile_access_metadata.py` 可重复对账，无法证明来源的项保持 restricted。
- **Agent Runtime Capability Receipts（2026-07-11 / ROOT-20260710-011）**：`core/agent_kit/report.py` 把旧静态 `full_power` 拆为 `conformance_ok`、synthetic-safe host runtime probe 和独立 source→Raw capture receipt；未安装目标 N/A，已安装目标必须再满足 `content_access_authorized`、近期 `AgentRuntimeReceiptStore` 回执、完整 daemon denominator、`agent_sync_cursors.db` 与 Raw revision 逐 session 对账和无 runtime gap。`health_check` 由 MCP/CLI 共用 `core.ops.health_check` 的 30-check canonical snapshot，并输出顺序敏感 `health_check_ids_hash`。认证 host 的 health 调用写短时 `agent_health_roundtrips`；`agent_runtime_probe` 只接受固定 synthetic-safe 文本、配对 tool call/result 与 completeness，随后写不含正文的 `agent_runtime_receipts`。`scripts/attest_agent_source_capture.py --agent <agent> --apply` 只读取 coverage/cursor/Raw 元数据并写 content-free source receipt，绑定 health hash、support manifest hash、native snapshot hash、Raw revision-set hash、完整性计数和时间。授权撤销、握手/回执过期、样本畸形、check-set/manifest 漂移、未完成分母或 Raw 不一致均 fail closed；`agent` 已进入 strict health。
- **Lossless Incremental Raw Projection（2026-07-12 / COG-009、COG-026）**：`scripts/project_raw_vault.py` 的 canonical Raw 固定为 `lossless-visible-v1`，不接受 `max_turn_chars>0`；每个 current revision 的 user/assistant/reasoning/structured 可见字段都有 byte hash，Markdown 通过 event/field marker 可逆审计。投影 journal 同时记录 stable logical event、immutable revision-set hash 与 content hash；同一逻辑事件的新 revision 原子替换原 chunk，只有 changed/deleted publisher-owned chunk 进入 `RawIndex`，无整 vault move/full rewrite。`scripts/audit_raw_projection_fidelity.py --strict --json` 用 read-only Raw DB 反查所有字段，`scripts/audit_raw_projection_backups.py --json` 只清单历史 `raw-vault-projection-*` 的 metadata/manifest/recovery value；`storage.disk_budget.raw_projection_backup_total_max_mb` 超线是 strict health 的人工保留告警，绝不自动删除历史证据。
- **Runtime Producer/Consumer Ledger v2（2026-07-11 / ROOT-20260710-012）**：`core/ops/producer_consumer_ledger.py` 输出 `mnemos.runtime_producer_consumer.v2`，以不可变 producer event、generation、intended consumer 和 append-only receipt 对账真实运行边界；required flow 的 0/0 是 `unobserved` 并 strict fail，事件触发型 flow 允许在无事件时保持 N/A，持续型 flow 另受 freshness 预算约束。异步边界必须声明 `receipt_grace_seconds`：窗口内未终态项保留为可见 `in_flight`，超时后计入 `overdue_pending`、missing consumer 和 strict failure；默认 grace=0，KG relation projection 明确为 60 秒，不能用提高 pending budget 掩盖丢消费。health 路径只读且禁止建表、迁移、注册或补数据；schema 初始化、v1 迁移与 `0600` durable outbox 的顺序重放必须通过显式 bootstrap 完成。24 条 adaptive flow 中 19 条接入真实 producer/consumer 边界、5 条声明可解释 N/A；Capture→Queue→Worker→Amphora→Distill 还以统一 cognitive event/receipt 证明 persona、raw-quality prejudgment 和蒸馏消费，不允许把 provider 超时伪装为终态成功。`CaptureService` 将同一 config 显式传给 queue、worker、SyncEngine 与 telemetry，测试的 tmp config 不得退回生产账本。
- **Daemon 可信实例身份（2026-07-10 / ROOT-20260710-002；2026-07-11 ROOT-20260710-018 配置指纹收口）**：`daemon/instance_identity.py` 定义 PID/start/boot/executable/command、runtime-code、配置文件字节哈希、有效配置指纹、database 与 service-manifest 指纹，`daemon/instance_control.py` 把 start/status/stop 收口到同一验证 seam。PID file 使用 `mnemos.daemon_instance.v2`，heartbeat 使用 `mnemos.daemon_heartbeat.v3`，两者携带相同 instance 且为 `0600`；health 交叉核对 PID record、heartbeat、live OS process 和当前上下文。有效配置指纹由 canonical `ConfigRegistry`、持久化配置、环境覆盖和 performance tier 共同决定，配置文件字节未变但 env/tier 改变也会触发漂移。`pid_exists()` 只做无副作用 liveness，不再拥有 daemon 身份语义；每次 SIGTERM/SIGKILL 前重新验证，PID reuse 或暂不可验证时不向目标发信号。父进程在当前 heartbeat 落盘后才报告启动成功。
- **Revision-aware Pipeline Receipts（2026-07-10 / ROOT-20260710-003）**：`core/pipeline_receipts.py` 定义 Capture→Amphora、蒸馏写入和 session end 的 typed receipts；`core/sync_framework/capture_handoff.py` 以 SQLite outbox 把 capture `done` 绑定到匹配 source/session/input revision 的 Amphora durable ack。Amphora 任务 identity 包含 source、session 和 input revision，允许同一 session 的新 revision 进入新 generation；Hephaestus 只有拿到 durable page 或 explicit intentional-skip receipt 才能进入终态，proposal/partial/retry/write failure 不得标记 L1 distilled。`core/app/retrospective_completion.py` 让 recap 等待 page/proposal/consumer receipt，`scripts/reconcile_pipeline_receipts.py` 负责只读发现和显式修复历史不一致。
- **Durable Recap Consumption（2026-07-11 / ROOT-20260710-010）**：`RecapConsumptionLedger` 保存 immutable plan revision、canonical target command 和 per-attempt receipt；`RetrospectiveConsumptionRouter` 只负责编排首次 drain 与恢复，`recap_consumption` daemon service 重试 pending/retryable/stale commands。requested labels 只能映射到 `knowledge_retrieval/policy_patch/follow_up/persona/scheduler/scoring`，未知 target 在 plan 接受前拒绝；全部 required receipt 提交或 explicit intentional skip 后才聚合为 `consumed`。`RecapFeedbackOutbox` 为每个已提交 effect 创建 correction command：策略与检索被 supersede、提醒与错误 skip 调度被撤销、persona/scoring 写幂等补偿；冲突反馈形成显式 supersession chain。`scripts/reconcile_recap_consumption.py` 负责四库备份、保守 schema 初始化和二次 dry-run，不从旧 target label 猜测历史成功。
- **Single-owner Document Ingest（2026-07-11 / ROOT-20260710-006）**：`DocumentImportService` 与 `FileIngestor` 的默认 `distill`/`capture` 只产生 canonical raw revision 和 capture queue receipt；raw projection 独占 Obsidian raw vault，CaptureWorker outbox 独占 Amphora handoff。document asset 使用 `trusted_user_document` + file SHA-256 stable id 关联 raw revision；重复文件复用同一 revision/event/handoff。`SyncEngine` 检测已有 `raw_event_id` 后只复用，不创建 worker revision；旧 `FileIngestor.backend.save/direct Amphora`、`DocumentProcessor.save_to_backend/--save` 与同步 MCP 蒸馏路径已删除。`scripts/reconcile_pipeline_receipts.py` 只清理 canonical 已提交且无 provenance edge 的历史重复 worker raw。
- **Wiki Projection Lifecycle（2026-07-11 / ROOT-20260710-007）**：`WikiProjectionLedger` 以 `wiki_projection.db` 保存 stable page identity、append-only create/update/move/delete mutation、因果 revision/tombstone 与六类 per-consumer receipt；`publish_wiki_page_updated()` 先提交 mutation 再发布 canonical EventBus 事件。EventBus 通过 `HandlerOutcome(ack/noop/retry/defer/dead)` 区分业务结果，以稳定 `consumer_id`、前序 revision watermark、持久退避、人工 decision defer/resume 和 DLQ 保证重启/乱序可恢复。KG、Cognitive Graph、relation ANN、Wiki ANN、metrics、MOC 都消费同一 mutation；`scripts/rebuild_wiki_projection_state.py` 用干净全量重建、真实增量 replay、隔离 comparator 和向量语义审计证明结果等价。完整边界见 `docs/WIKI_PROJECTION_LIFECYCLE.md`。
- **Canonical Cognition Episode（2026-07-15 / COG-010，2026-07-20 / COG-015）**：`DistillInputSpec v4` 在模型调用前封存 `CognitionExtractionContext`，以 exact Raw span 和系统 catalog 绑定 agent/role/authority、完整性、ACL、用途与保留策略。`distill_output_v4` 的非 skip 分支必须给出全部 19 个 typed 字段；canonical `mnemos.cognition_episode.v2` 还冻结完整 claims catalog/hash 与 user behavior intent，使 Wiki projection 不再是可检索语义的唯一载体。known 项必须映射当前 chunk 的 exact evidence 与 admitted claim，unknown/not_applicable 不得夹带断言，全部 claim 必须映射到 episode 字段。正式写入顺序固定为 `CognitiveStateStore` 原子提交 episode revision + `CognitiveDataEvent` + 三类 projection outbox，再进入 action/Wiki；ledger、Wiki、KG、CognitiveGraph 均只消费 committed revision id。历史 v1 只读兼容，新 revision 只能写 v2。`audit_distill_output_contract.py` 以四类 golden corpus 和 forged/cross-chunk/all-unknown probes 证明边界。
- **Cognition Episode Durable Dispatch（2026-07-20 / COG-030）**：`core.cognitive.cognition_episode_dispatch.CognitionEpisodeDispatch` 是 committed episode 下游投影的唯一 owner，只发布 `mnemos.cognition_episode_committed.v1` 的 ID-only EventBus envelope，并固定消费集合为 `wiki/knowledge_graph/cognitive_graph`。每个 target 先写稳定 effect、manifest、before/after hash、ACL hash 和必要的 omission receipt，再由 `CognitiveStateStore` 写 reciprocal terminal；EventBus 以 event+consumer 唯一 terminal、跨进程 lease/续租/fencing、retry/dead 保证崩溃后只补缺失 effect。EvidenceGraph 的 canonical edge 方向是 evidence/source 指向 derived cognition，节点覆盖 `RawRevisionSpan/Observation/Claim/Belief/Decision/Prediction/Action/Outcome`；`explain_why()` 沿该方向反查完整 Raw 血缘，不再依赖旧 incoming-edge 偶然语义。`scripts/audit_cognitive_event_dispatch.py` 与 `scripts/audit_evidence_graph_direction.py` 使用独立 SQL/遍历和代码合同复核，`scripts/reconcile_cognition_episode_projections.py` 是唯一 production schema/direction reconciliation 入口。
- **ACL-first Cognitive Retrieval（2026-07-20 / COG-015）**：`core.cognitive.search.CognitiveSearch` 从 canonical cognition、CognitiveGraph 与 EvidenceGraph 三条独立通道产生 typed `CognitiveSearchHit`；Wiki 仍由 `ContextAwareSearch` 独立召回。各通道在最终 top-k 之前完成 ACL/purpose 校验与 oversample/refill，再用确定性融合排序；application service 在暴露前按 channel/object/current revision 二次授权。结果固定携带 matched field/terms、match-offset snippet、source revision/span、scope 与 ACL decision。canonical state 的候选授权只读取 `mnemos.cognitive_search_state_headers.v4` 小型投影：immutable binding 在 revision 事务内绑定 canonical ACL preimage、payload hash 与 identity，header 必须再绑定同一 preimage；ACL 决策通过后才读取 revision body。`scripts/audit_cognitive_search.py --strict --json` 运行冻结的 36 条正向（28 holdout）+ 7 条负向 hermetic benchmark，要求 critical Recall@5=100%、Recall@10≥95%、MRR≥0.90、unauthorized=0、field/current gap=0 和 production-answer leakage=0；`--production` 另审计生产 Wiki ACL、三类 store 的精确 exclusion denominator 与真实 channel population。未知历史 Wiki 只进入显式 `restricted_unknown` quarantine，不默认公开。ACL/entropy 批量迁移必须把 Markdown preimage backup、Wiki lifecycle mutation、durable pending event 和两份 SQLite rollback 绑定为一个提交单元；state header、search index 与 exclusion reconciliation 必须持有同一 offline migration writer lock。当前生产 ACL/store inventory 已闭合，但三类 typed channel population 均为 0，故只可标记 `CLOSED_ROOT_VERIFIED / LIVE_POPULATION_BLOCKED`。
- **Canonical Feedback Attribution（2026-07-18 / COG-038）**：`core/cognitive/feedback_attribution.py` 是 reaction、attribution revision、correction 与 target command 的唯一 deep owner。显式 reaction 与客观 `OutcomeMeasurement` 分型；click/open/ignore/silence 只记录观察事实，不能直接成为 ground truth。private attribution identity 绑定 subject、exact scope 与 principal/agent，recap/dialog 的 source scope 不能由 caller 重绑定。`core/cognitive/feedback_causal_refs.py` 在写入前把每个 available decision/prediction/action ref 解析到真实 canonical revision/action spec，并验证 prediction access 与 prediction→decision/action 链；独立 audit 从 ledger 重新解析同一证明，不存在的 available ref fail closed。project scope 的空 session ID 不会抹掉独立 exposure 身份。每个 eligible target 只接收 registry 固定的 proposal/neutralization command；proposal 必须通过真实 `PushDecisionGate`，由 `core/cognitive/feedback_proposal_gate.py` 封存 exact DecisionTrace/material permit 后，domain owner 才能写 pending-review proposal及 reciprocal terminal。成功 receipt 只能由 specialized state API 在 domain adapter 独立复核后写入，并以 domain-owned before/after hash、DecisionTrace/action refs、reciprocal effect receipt 和 canonical consumption receipt 闭环；exact replay 先独立复核既有 domain/material terminal，命中后不得因 wall-clock 漂移再次签发 DecisionTrace。旧 pending command 在 correction UOW 内原子终结，跨 revision 的已提交 effect 必须先 revoke/compensate/suppress，replacement 才能激活。predictive、proposal、reminder、Context Search、recap、scorer 与 reflection 正式入口都使用 server/OS-bound principal 和 exact scope；历史 feedback 对象只按 database/table/PK/schema/content hash 进入不可激活 quarantine，active reader 只排除退役的 legacy object type，普通 `source_event_id` lineage 不会让正常认知对象消失。生产 cognitive-state 已是 canonical v3，三域 3,625 个历史对象完成 restore-tested object-level migration，最终 uncovered=0、active promotion=0。`scripts/audit_feedback_attribution.py --strict --json` 独立重算 principal binding、命令/receipt、causal refs、material proof、terminal target、formal seam、legacy reader 与对象 coverage。
- **Canonical Observation Calibration（2026-07-16 / COG-049）**：`calibration_lineage.py` 以 immutable Raw revision + exact SHA-256 建立独立根，同根 Raw/Wiki 去重，多根派生汇总只作 non-voting overlay。`CalibrationEngine` 使用可重放的 weighted evidence shrinkage，计算身份同时绑定稳定 Observation ID、脱敏前 measurement digest、canonical peer/source 顺序、来源分类、lineage、validator/combiner 实现和 spec；validator 缺陷、重复 identity、源码不可读或精确来源缺失都失败关闭。`CalibrationRecordStore` 通过 `CognitiveStateUnitOfWork` 原子提交 typed revision、event 和 `observation_index/wiki_projection` outbox，只允许 current committed receipt 调用唯一 binder，并在冷读时复核 payload/identity hash。`ObservationStore` 保留 immutable base confidence 与 `verified/historical_unverified` 状态，只在 verified base 和 current revision 可回读后绑定 posterior；无法证明 prior 的旧行不伪造 record，删除/retention 也不能制造 orphan record。规格变更会 supersede 旧 record 并让旧投影 stale。Wiki 全量/增量重放同一 committed record，显示 Observation/Calibration/source-span identity 与有 hash 的 omission receipt；SQLite、CalibrationRecord、EvidenceGraph 和 Wiki 只应用 `pii_credentials_only_v1` 窄脱敏，不加密。
- **Derived Cognitive Projection Lifecycle（2026-07-22 / COG-050）**：L2.4 KG、L3 Observation、L4 Reflection 与 L5 Persona 不再以永久 `report` 豁免或直接文件写入表示成功。`DerivedProjectionLifecycle` 把 canonical revision、目标内容 hash、generation manifest、create/update/delete mutation、原子文件发布和 EventBus trace 绑定到同一投影代际；A→B→A 重放以单调 activation time 选出最新 binding，自定义 ledger 必须显式绑定同一 `EventBus`，目录位置本身不再授予删除所有权。Observation/Reflection/Entity 回放 facade 对任何 canonical mutation fail closed；Vault sync 只读 canonical store，并在任一层失败或 canonical hash 漂移时拒绝 Git 快照。Persona calibration 先提交 canonical calibration，再通过同一 lifecycle 重放页面，不能直接改派生 Markdown。`scripts/audit_cognitive_projection_lifecycle.py --strict --json` 是代码/隔离合同门，生产状态必须另加 `--production` 检查真实 binding、stale 与六类 receipt。2026-07-22 代码合同已闭合，但按用户指令未执行生产重建；live residual 仍为 binding 480、stale 480、required-consumer receipt 9,204，因此状态是 `CODE_CLOSED / RUNTIME_REBUILD_PENDING`，不是 release certificate。
- **Preflight 路径统一**：新增 `integrations/preflight_builder.py` 作为 agent-agnostic 预加载段落构建器；`apollon.py` 提供 `get_context_for_agent()` 通用入口并保留 `get_context_for_claude()` 兼容；`active.py` 在 5s 超时内调用完整路径并自动回落轻量版；`ObsidianBackend`/`WikiReader`/`PredictivePushEngine` 增加 60s 类级缓存；新增配置项 `preflight.mode=light|full`。
- **StorageBackend 工厂化（P1-#22）**：新增 `core/sync_framework/storage_backend.py::create_storage_backend()` 作为全库唯一后端工厂；所有模块统一通过工厂创建后端，移除散落的 `ObsidianBackend()` 硬编码；`update_tags()` 提升为抽象方法，强制新 backend 必须实现，避免蒸馏后重复处理；`scripts/distill_all.py` / `scripts/batch_distill.py` 修复调用不存在 `StorageBackend.create()` 的 bug。
- **未接入模块归档（2026-06-25）**：已删除 `integrations/agora_tools/` 下除 `schema.py` 外的 11 个未使用子模块；移除 `integrations/apollon.py` 中已废弃的 `run_kia_cycles()`；`core/orchestrator.py` 已删除；`core/credential_pool.py` 已移除，其多 key 管理思想由 `core/llm_key_pool.py` 轻量化替代。
- **系统级契约底座（2026-07-04）**：新增 `core/system_contracts.py`、`core/module_toggles.py` 与 `core/ops/producer_consumer_ledger.py`，统一认知资产 Schema、QualityDecision、CapabilityRegistry、PrivacyPolicy、LifecycleStatus/FailureClass、ActionLedger、领域语言、MnemosScorecard、模块开关、冷启动产物消费契约和运行态 producer/consumer 闭环。`core/hephaestus/quality_gate.py` 可把局部质量门映射为统一 `QualityDecision`，`core/ops/health_check.py` 输出 `checks.system_contracts`、`checks.module_toggles` 与 strict `checks.runtime_producer_consumer`，`mnemos doctor modules --json` 只读展示“为什么没开、何时可开、打开后产出什么、谁消费、如何回滚”，`scripts/audit_runtime_producer_consumer_closure.py --strict` 对账 module output 与 adaptive flow runtime ledger。
- **Cognitive Data Event Registry（2026-07-05）**：`core/ops/cognitive_data_contract.py` 新增 `mnemos.cognitive_data_event.v1` 与 `mnemos.data_interface_registry.v1`，统一 source_id、asset_id、dedupe_key、consumer_id、privacy_class、retention 和 evidence_refs；`core/ops/producer_consumer_ledger.py` 在原 produced/consumed/dead_letter flow ledger 之外记录 cognitive data event、consumer outcome 和 duplicate/derived/reinforcement reconciliation。`scripts/audit_data_interface_registry.py --strict` 校验 CaptureService、CaptureQueue、SyncEngine、FileIngestor、DocumentProcessor、Amphora、EventBus、ReflectionStore、AdaptiveScorer、DistillActionRouter 和 persona signal store 均映射到统一数据接口。
- **Cognitive Value Gate（2026-07-04）**：`core/hephaestus/cognitive_value_gate.py` 接在普通 `QualityGate` 之后，按来源证据、认知贡献类型、未来触发场景、消费者影响和 raw 生命周期信号判断 Wiki 准入。格式良好但没有认知贡献的片段会被拒绝；高价值但证据不足的片段进入 pending verification；正式页面写出 `cognitive_value_*`、`cognitive_contribution_types`、`cognitive_consumers` 和 `quality_gate_action_ledger_ref` frontmatter；写页前最终 accept/review/reject 决策会进入 `ActionLedger(action_type=quality_gate)`。
- **Distill Cognitive Actions（2026-07-04，COG-011；2026-07-15 v4）**：`distill_output_v4` 的高价值 claim 必须声明 `cognitive_actions`，覆盖 observation、reflection seed、policy patch、methodology、pitfall pattern、relation update 和 reinforcement。`DistillActionRouter` 只消费与不可变 `DistillInputSpec` 匹配的已准入输出，将动作写入 `cognitive_action_log` 并生成 `mnemos.distill_cognitive_action.v1` JSON artifact；`core/ops/health_check.py` 暴露非 strict `checks.distill_cognitive_actions` 计数；普通技术事实无动作时 Wiki frontmatter 标记 `cognitive_action_status=ordinary_knowledge`。
- **Distill Behavior Intent（2026-07-05，COG-011；2026-07-15 v4）**：`distill_output_v4` 的非 `skip` 输出必须带 `user_behavior_intent`，记录 `content_source`、`user_intent_signal`、`intent_hypothesis`、意图证据、后续验证/修正事件、`intent_status` 和 `intent_confidence`。`PromptBuilder` 会通过 `core/hephaestus/behavior_intent.py` 注入 `ContentSource`、`UserIntent` 与 `IntentRouter` 预判；Wiki frontmatter/来源追踪展示用户引入原因，`SourceReader` 回读后把字段恢复成 `content_source` 与 `user_intent`，供 Observation/persona/reflection/cognitive decision 消费。
- **User Cognitive Profile v2（2026-07-05，COG-019–021 修订）**：`core/persona/psyche.py` 继续负责通用行为信号，`core/persona/cognitive_profile.py` 承载 `profile_signals`、当前 `profile_assertions` 投影、不可变 `profile_assertion_revisions`、`profile_read_authorizations`、`profile_usage_outbox` 与 `profile_usage_log` 的 schema、DTO、repository 和画像 payload 构建。画像输出不再只停留在 energy/cognitive/value，而是提供决策偏好、判断标准、交互契约、风险边界、负反馈、当前目标和认知决策飞轮输入。纠错/撤销以 revision/content hash/supersedes 表达；授权读取会签发短期、单命令消费的 opaque token，精确绑定 server-resolved principal、narrowing project/session、consumer、purpose、assertion→revision mapping 与 assertion ACL hash。usage sink 必须在 durable intent 前消费该 token，并把 sealed scope snapshot、exact target delta 和 reciprocal receipt 一起落库；未知、重复、缺失或 ACL 漂移的 assertion 全部 fail closed，崩溃重放只允许已被同一 command 消费的 token。`SignalStore` 的默认构造只打开并验证已存在的 canonical schema，不创建父目录、建表、ALTER 或在 missing-table 异常后重试初始化；缺库、旧 schema、registry/hash/index/FK/head 漂移均提示显式 reconciliation。只有明确的 bootstrap/隔离 fixture 才能使用 `initialize_schema=True`。生产迁移必须先 dry-run 取得锁定 source state 的 exact plan hash，再持有共享 offline writer lock，以唯一 `O_EXCL` 代际完成 SQLite backup、source/backup integrity 与 FK 检查、逐语句事务故障回滚、second apply zero-change 和 restore drill。只有已授权且拥有真实 runtime route 的 preflight、context search 和 persona behavior prompt 三类 consumer 可记录实际 effect。production audit 按 token 签发时刻验证 revision 的历史有效性，不要求旧 usage 继续指向审计时 current head；future/unrelated revision、读时已 supersede 的 revision、signal 在读时尚未观测或已过期均阻断。effect audit 先复核不可伪造的 before/after comparator receipt，再按 consumer 固定 target/kind/mapping；context search 还会从持久化的 baseline/enabled ranking 独立重算哈希、真实 rank delta、eligible candidate 与 assertion 集合。DistillTask 当前没有 server-resolved principal 或 sealed read decision，因此 `distillation_prompt` 与 quality gate、auto-healing、cognitive flywheel 一样保持显式 disabled，不计入 effective consumer 分母；不得从 session/source metadata 推导用户主体。`audit_persona_profile_contract.py --strict` 是 isolated structural contract，`audit_persona_runtime_effectiveness.py --strict --json` 是 production read-only effectiveness audit，二者不得互相替代。
- **独立用户模型资产库（2026-07-23，COG-016）**：`UserCognitiveBlindspot` 与 `InteractionPreference` 继续使用两个独立 append-only SQLite store，但必须由 `scripts/reconcile_user_model_asset_stores.py` 作为一个 migration generation 协调。dry-run 对两库状态、逻辑 hash、integrity/FK 和 exact action 生成同一个 plan hash；apply 在共享 offline writer lock 内以 SQLite attached-database DELETE-journal transaction 同时安装两套 canonical schema，禁止先成功一库再遗留另一库。每代使用不可覆盖的唯一 backup directory，并为 fresh-missing store 写明确 pre-state manifest；prepared/committed 状态允许进程在 transaction commit 与 manifest commit 之间崩溃后，于下一次 apply 先恢复旧完整代际。现有库必须备份并通过 restore drill；成功后要求两库均 canonical、second apply changed rows 为零。
- **Wiki 知识形态迁移代际（2026-07-23，COG-016）**：`scripts/reconcile_wiki_knowledge_forms.py --apply` 强制 exact reviewed plan hash，并在任何 backup、staging 或 Wiki source write 之前取得 shared offline writer lock、确认 daemon/MCP writers 已停止。全部 Markdown after-image 先写到 Wiki 外的 staged generation 并验证 hash，再在锁内 materialize；随后同代提交 `WikiProjectionLedger`/events。顶层 manifest 使用 `prepared -> source_materialized -> projection_committed -> committed` 状态；任一写页、projection 或提交后故障会先调用 canonical projection database recovery，再恢复全部 Markdown preimage。进程崩溃留下的 prepared generation 必须用 `--recover --backup-dir <dir>` 在重新规划前恢复，禁止让 source files 与 projection ledger 停留在不同代际。
- **Canonical 知识形态词表（2026-07-23，COG-016）**：`core/knowledge_form.py` 是六类知识形态 alias、canonical display 与 entity-type 语义的唯一 owner；归一化先执行 Unicode NFKC、trim 和 casefold，覆盖“洞察”/“洞察关联”、中英文 alias、大小写和全角英文。producer prompt/schema、Wiki renderer、历史 reconciliation plan 与 coverage audit 都必须复用该 owner，`audit_blindspot_asset_boundaries.py` 独立核对 prompt/schema 的六类显示值、运行时 Unicode corpus、renderer/plan/audit 接线，并要求 `knowledge_form_vocabulary_owner_count=1`、`producer_migration_consumer_normalization_drift=0`。
- **Phase 5 冻结失败合同（2026-07-23）**：`scripts/audit_phase5_failure_contracts.py` 固定十类跨链路反例：跨 100 个新 service 实例/多进程 cooldown、Persona version 冲突、Apollon direct writer residual、真实 challenge presentation、真实 producer→assertion→consumer→target effect、enabled/disabled counterfactual、三类 migration crash/restore、required full-score gate omission mutation、production 0/0 阻断与 alias/config/dynamic entrypoint 扫描。`docs/acceptance/phase5_baseline_failure_evidence.json` 绑定 frozen audit SHA、基线 commit 和失败 snapshot；同一 audit 在基线检出 6 个失败合同，在候选要求 `baseline_failure_evidence_present=1`、`wrong_legacy_behavior_expected_by_runtime_test=0`、`old_production_caller_residual=0`、`static_green_production_red=0`。该 audit 自身是 independently declared required full-score gate，不能由 seeded structural audit 或当前分支从未失败过的测试代替。
- **Distill Response Budget（2026-07-05）**：蒸馏结构化输出预算四档统一为 `6000/8000/12000/16000`，分别对应 default、medium、long/chunked 和 `finish_reason=length` retry。`core/hephaestus/response_budget.py`、`core/hephaestus/distillation_engine.py` 的兼容回退常量、`core/config.py`、配置样例和 `scripts/audit_distill_response_budget.py` 共同防止旧 `4000/6000/8000/12000` 回归。
- **Wiki Route Closure（2026-07-04 / 2026-07-06 运行态收敛）**：蒸馏和文档蒸馏写页前会调用 Charon `resolve_page_folder()`，可确定分类时直接写正式 Wiki 目录，不确定或正式区 basename 冲突才留 `00-Inbox`，并写 `Wiki路由状态/原因/目标` frontmatter；daemon `wiki_route` 服务由 `daemon/wiki_route.py` 周期性运行 Charon route-only connect，传入 `write_relations=False`，只移动/标记页面，不写 KG cooccurrence 关系，也不触发 embedding-heavy 图谱构建；`checks.wiki_route` 作为 strict health 面检查 Inbox/needs_review/source-prefixed/标题冲突预算。完整关系构建保留给手工 Charon connect 或显式重型调度。
- **迁移/备份/数据所有权底座（2026-07-04）**：新增 `core/migrations/registry.py`、`core/backup/snapshot_manager.py` 与 `core/privacy/data_ownership.py`。旧配置 alias、旧 Vault 布局、`migrate_db.py`、`migrate_vault_layout.py` 和加密迁移脚本统一进入 `MigrationRegistry`/`MigrationLedger`；全局快照生成 `SnapshotManifest`，覆盖配置、SQLite、mnemos vault、raw vault、Action Ledger、迁移账本和模块状态；数据所有权提供 inventory/export/freeze/delete/deletion proof 契约。CLI 新增 `mnemos migrate`、`mnemos backup`、`mnemos restore`、`mnemos data`，health 输出 `checks.migrations`、`checks.backup` 和 `checks.data_ownership`。
- **配置/secret strict doctor（2026-07-04）**：`core/privacy/secret_inventory.py`、`core/ops/config_audit.py` 与 `mnemos_cli.py doctor config --strict --json` 固化为 `mnemos.secret_inventory.v1` + `mnemos.config_audit.v1`。该入口复用运行时 LLM、embedding、reranker 和可选 multimodal 解析器，统一报告模型端点、secret inventory、路径、顶层 `memos` 等 legacy/stale 配置、privacy、retention、daemon 和权限；secret inventory 递归扫描 `api_key/token/secret/password/credential/bearer/key_source` 并过滤 `token_budget`、`max_tokens` 等非密钥字段；机器报告写入 `~/.mnemos/config_audit.json`，默认只暴露脱敏引用、字段路径和长度统计，不落明文 key/token/secret、真实 API URL、本机绝对路径或未脱敏 key source；`--unsafe-debug` 才允许本机私有排错输出原值。stale 配置修复通过 `MigrationRegistry` 的 `config.stale_keys.v1` 备份并写入 `~/.mnemos/migrations.db`。
- **Keyring / env fallback doctor（2026-07-05 / F25）**：`core/ops/keyring_doctor.py` 固化 `mnemos.keyring_doctor.v1`，作为 keyring backend 探测、secret 引用来源计数、`safe_but_not_best` 风险分级和 `security.accept_env_secret_fallback` 判定的单一来源。`scripts/health_check.py`、`core/ops/config_audit.py` 和 `mnemos secrets doctor [--json|--accept-env-fallback]` 复用同一报告；keyring 不可用时仍是非 strict warning，但必须显示 `keyring_error`、`keyring_status`、`keyring_risk_level`、是否显式接受 env fallback，以及 keyring/keyref/env 迁移动作。
- **Optional Multimodal Endpoint（2026-07-04）**：`core/config.py` 新增 `multimodal` 可选配置块，`core.llm_config.resolve_multimodal_api_config()` 统一解析 `MNEMOS_MULTIMODAL_API_KEY`、`MNEMOS_MULTIMODAL_BASE_URL`、`MNEMOS_MULTIMODAL_MODEL` 或显式 `multimodal.enabled=true` 配置。`scripts/auto_setup.py` 在三类必填模型后提示“多模态模型，可跳过，不影响 Mnemos 正常使用”，launchd/cron 导出同组 env；`scripts/verify_installation.py --json` 和 `mnemos health --json` 分别显示 configured/skipped/unreachable 或非 strict `checks.multimodal`。KnowledgeInbox 图片入口配置存在时调用 OpenAI-compatible vision endpoint 解析 Markdown、写入 storage 并入蒸馏队列；未配置或 API 失败时写 `mnemos.multimodal_image_task.v1` 可恢复任务。
- **SQLite Disk Budget Health（2026-07-07）**：`core/ops/sqlite_disk_budget.py` 固化 `mnemos.sqlite_disk_budget.v1`，由 `core/ops/health_check.py` 作为 strict check 输出。它监控数据库目录下 `.db-wal`、系统 temp 中的 Mnemos 临时文件、`~/.mnemos/backups/snapshots` 总量/增长率和 `raw_events.db` 总量/增长率；采样状态写入 `sqlite_disk_budget_state.json`，只保存体积和时间，不保存内容。`.db-wal` 超预算可通过 `scripts/repair_sqlite_disk_budget.py --apply --wal` checkpoint，超过 `storage.disk_budget.temp_stale_minutes` 的 Mnemos temp 可通过 `--apply --temp` 删除；snapshot 和 raw_events 属于历史/原始证据，health 只告警并给出人工处理动作，不自动删除。SQLite 不再做整库加密，`Config` 不安装 SQLite 加密 hook，`sqlite_artifact_exists/size` 只识别同名 `.db`。pytest 默认把 Amphora `_DB_PATH` 隔离到临时 DB，禁止测试临时文件污染用户真实蒸馏队列。
- **Golden Benchmark 基准（2026-07-04）**：新增 `core/benchmarks/golden.py`、`benchmarks/golden/manifest.json`、`scripts/run_golden_benchmark.py` 与 `scripts/audit_golden_benchmark_contract.py`。固定 synthetic raw conversation、用户文档、踩坑、决策、低价值和冲突输入，通过 deterministic mock LLM/embedding/reranker/multimodal provider 复验认知资产、质量门、画像增量、搜索/preflight 消费、ActionLedger 和 `mnemos_benchmark_scorecard.json`；health 输出 `checks.golden_benchmark`。
- **Hermetic Validation Boundary（2026-07-11 / ROOT-20260710-015）**：`core/ops/hermetic_run.py` 定义唯一已实现的 `isolated` profile 和 `mnemos.hermetic_run_environment.v1` manifest；`scripts/run_tests.py`、pytest collection boundary、`scripts/run_full_score_gates.py` 与 `scripts/audit_gate_hermeticity.py` 共享同一环境所有权，把 HOME、Mnemos/database/wiki、XDG、temp、pycache、artifacts 收进一个不可复用的 `sandbox_root`。默认凭据集合为空，只有 full-score `--real-api` 才显式继承 API key。`core/runtime_environment.py` 是 Config 的环境读取单一 seam，`core/ops/config_scope.py` 用 `ContextVar` 提供线程隔离配置快照。health/status/distill status/verify/golden 的默认路径只读；缺库不初始化，显式写探针使用唯一文件并清理，golden 输出目录必须由调用方或 run artifacts 拥有。manifest 与 gate audit 同时给出 `environment_hash`、`outside_write_count`、`formal_state_diff` 和 repo diff，非空输出根、路径逃逸或正式状态变化直接失败。
- **认知动作 Effect Closure（2026-07-15 / COG-014）**：`DistillActionRouter` 只接受以 `claim_ids` 精确映射的 admitted fragment，先提交不可变父动作与 intent，再由 `DistillCognitiveActionWorker` 租约执行 command。Observation/Reflection/PolicyPatch/Relation 目标服务各自在自己的数据库提交稳定 effect 与 reciprocal receipt，`DistillActionStore.complete_effect()` 独立只读复核目标 receipt 后才允许 `applied`；attempt 追加写、retry/dead、artifact schema/hash/ACL/source 校验和 replay identity 均 fail closed。shadow/proposal 不派发正式命令，关闭 router 不再形成正式 Wiki 绕路。`scripts/audit_cognitive_action_effects.py` 独立复算 target current state 与 before/after hash；历史 v1 数据只可通过带 SQLite backup 的显式 reconciliation 转为标记的 legacy projection，不能伪造精确 mapping。
- **Full-score Certification Manifest（2026-07-12 / ROOT-20260710-016，ROOT-019/021/022、COG-018/014/049/030/015、PH5-001/031 分母扩展）**：`scripts/run_full_score_gates.py` 将诊断选择与发布认证拆开。`mnemos.full_score_gate_manifest.v1` 从代码构造当前 canonical 62-gate denominator，其中包含 relation-evidence schema、maintainability/zombie/vulture 三个 strict zero-closure、required-Desktop `docs.asset_manifest.strict`、由 `docs/acceptance/phase5_required_full_score_gates.json` 独立声明并由 verifier 复核的 `contracts.persona_runtime_effectiveness`、`contracts.blindspot_asset_boundaries` 与 `contracts.phase5_failure_contracts`、`contracts.cognitive_action_effects`、`contracts.cognitive_calibration_lineage`、`contracts.cognitive_event_dispatch`、`contracts.evidence_graph_direction`、`contracts.cognitive_search` 与 `model_call_ledger.static`；`mnemos.full_score_gates.v2` 记录 expected/selected/executed/omitted、完整 Git commit、clean status hash、per-gate receipt 和 stdout/stderr SHA-256。`model_call_ledger.static` 只证明仓库中直接 provider 边界具备预留/结算的静态契约，不能替代某台机器的旧库迁移、健康检查或恢复演练证据。只有 strict+real-api 且三集合完全相等、omitted 为空、required 全通过和工作树干净时才 release eligible。`scripts/verify_full_score_certificate.py` 默认对比当前代码权威 manifest、独立 Phase 5 required manifest 和当前干净 commit，旧 v1 报告为 `legacy_scope_unverifiable`。`scripts/audit_test_suite_denominator.py` 证明 pytest 文件唯一归属 quick/integration/heavy；`scripts/run_cognitive_behavior_scenarios.py` 运行矩阵的真实行为测试。
- **Model Call Ledger（2026-07-14 / COG-018）**：`core/runtime_paths.py::RuntimePaths` 将 `model_call_ledger.db` 作为唯一账本路径；`ModelCallLedger` 对完整 canonical provider request 的 UTF-8 字节**上界**原子预留、发送前标记，并只从 provider meter、token、latency 与价格快照结算。SDK retry 与 HTTP redirect 在边界关闭；3xx 属于已发送但未结算的失败，不能在一条预留下二次 POST。未知/partial/非有限价格和未明确配置的任一零价都在发送前拒绝；已发但无法证明 usage 时保守保留成本，actual 超过 reservation 则进入阻断性的 `incurred_overrun`。账本不持久化原始 prompt、response、调用方错误文本或可逆 preview；run、外部 request/usage 与 legacy metadata 只以本地 opaque reference 留存，operation/cache 是审核过的枚举。receipt guard 只约束受支持的本地记账接口，不作更广泛的设备或代码隔离承诺。run root 与 exact `model_call_entry_subjects` 分离，批量调用可绑定多主体；freeze 是 durable dispatch barrier，删除/保留期会写 daily 与 still-live-run tombstone，不能借删除重置预算，且在途调用阻断删除。apply 关闭自身 SQLite 句柄并要求 WAL→`DELETE`/`secure_delete=ON` 后才释放记录；该证据仅覆盖本地 SQLite 的 active DB/WAL/free-page 清理，不代表文件系统快照、备份、副本或 provider 侧记录已被取证级抹除。运行时完整校验 SQLite columns/defaults/PK/unique/FK/index/attribution/no-preview；旧库只允许显式的 plan、SQLite backup、exact `execution_plan_hash` apply 和 sealed v3 recovery 过程，不能把 run root 伪造为 entry map，coverage-unknown legacy 需显式 discard。`scripts/reconcile_model_call_ledger.py` 默认只读，注册迁移要求 daemon 停止、reviewed hash、正常本地 backup 与 post-apply clean/noop；rollback 先预览 sealed v3 manifest，只有显式 apply 才写回 backup。实现和静态审计已进入仓库；静态审计本身仍不能替代迁移或恢复演练证据。完整操作契约见 `docs/MODEL_CALL_LEDGER.md`。
- **COG-018 implementation boundary（2026-07-14）**：实现物理上位于 `core.telemetry.model_call_ledger`；`core.telemetry.prompt_call_log` 只是静态兼容导出，禁止形成双写或第二存储。受控 reconciler 位于 `core.migrations.model_call_ledger_reconcile`，独立脚本只能诊断，缺少 registry-issued capability 的 direct `--apply` 零写入 blocked；正式变更只走注册的 wrapped migration。sealed-v3 manifest、普通 SQLite backup、hash 和运行时 lock 都用于本地恢复正确性：manifest 绑定本次实际范围内的 target 及其 SQLite sidecar，orphan、缺失、漂移或篡改任一项均 fail closed。该边界只处理个人隐私、API key、银行卡信息、密码、raw prompt/response 和 caller error 的不持久化/脱敏。2026-07-14 已有本机 apply → health/plan → v3 restore → reapply → final health/plan 的运行证据；它不等同于全仓发布证书。
- **Relation Evidence Schema Authority（2026-07-12 / ROOT-20260710-019）**：`core/kia/relation_evidence_schema.py` 独占 `relation_evidence` canonical DDL、semantic signature hash、`mnemos_schema_registry` component row 和 fresh-create/existing-validate seam。`KnowledgeGraph` / `RelationManager` 在任何自身 DDL 前调用 validator；未注册 KG schema、旧 RM defaults schema、NULL/blank evidence type、缺 index、损坏 registry 或未知结构 fail closed。`scripts/reconcile_relation_evidence_schema.py` 是唯一显式迁移入口，先 SQLite backup，再 transaction/recount/integrity；`scripts/audit_schema_registry.py` 对账实际 columns/defaults/FK/index/hash 与单一生产 DDL owner，并进入 local/pre-commit/CI/full-score。
- **Install Lifecycle（2026-07-04 / 2026-07-09 验证口径收紧）**：新增 `core/setup/install_lifecycle.py` 与 `core/cli/commands/setup.py`，把 `mnemos setup`、`mnemos upgrade`、`mnemos uninstall` 和 `mnemos doctor repair-all` 收口到同一个 `InstallLifecycleState`。升级计划必须引用 `MigrationRegistry` 和 backup preflight；升级 apply 先创建 `SnapshotManifest` 再写 ActionLedger；卸载默认保留用户数据，`--purge-data` 必须通过 data ownership 删除计划。`setup.sh`、`setup.bat` 和 `scripts/auto_setup.py` 保留为兼容/高级入口；`mnemos setup` 的 venv re-exec 必须回到 `mnemos_cli.py setup ... --venv-reexec`，保持机器可读 lifecycle 输出；`scripts/auto_setup.py --yes --preserve-config` 使用同一模型配置解析、保持配置文件 `0600`，可选多模态缺失不阻塞，并以 `scripts/e2e_probe.py --dry-run --no-api` 收尾。2026-07-05 起必填模型端点 smoke 受 `--max-smoke-attempts` 约束，非 TTY fail fast，失败细节写入 `InstallLifecycleState.metadata.required_model_endpoints_failed`；2026-07-09 起 `checks.install_lifecycle` 是 strict health check，`installed_partial` 或 required step 未完成会输出 `incomplete_required_steps` 并降级，`scripts/verify_installation.py --json` 默认只标记 basic 验证，只有 `--full --json` 且 `full_verification_ok=true` 才代表完整安装验证。health 不再只消费静态 setup plan：当 `mnemos setup` 已在 `ActionLedger(action_type=install_setup)` 中记录 verified `installed_ready` 证据时，runtime probe steps 可由该证据闭环；当前配置、Vault 或必填模型端点重新变为 blocked 时仍会降级。PEP 668/镜像失败路径会回到 repo `.venv`，并在 build isolation 下载失败时用 `--no-build-isolation` 复用现有 venv。
- **Guard Analysis Loop（2026-07-05）**：`core/kia/aegis.py` 的连续纯分析与同一文件/工具重复读取守护默认读取 `guard.analysis_loop.*`，默认阈值均为 2，达到第 2 轮/第 2 次即触发；需要兼容旧“第三次触发”语义时显式设为 3。`guard_check` 告警响应和 `guard_alert` 事件 metadata 均暴露 `threshold_source`、`threshold_value`、`current_count`，并按实际触发的阈值来源区分 config/default。
- **Canonical Config Registry（2026-07-11 / ROOT-20260710-018）**：`core/config_registry.py` 的 `mnemos.config_registry.v1` 是配置 path、类型、默认值、env ownership、performance tier、示例/测试/文档覆盖、alias 与 removed tombstone 的唯一事实源。`Config` 默认 strict：未知、已移除、alias、类型错误、非对象 JSON、损坏 JSON 和非法 tier 均 fail closed；caller default 不得改变已注册值。alias 只供 `config.stale_keys.v1` 原子迁移使用，canonical 值冲突时胜出并进入 ledger，迁移只处理原始持久化文档，不物化 env/default，先写 `0600` backup 再原子替换。`effective_source` 和 `config_fingerprint` 暴露真实有效来源；`scripts/audit_config_registry_closure.py --strict` 对账定义、读取点、JSON/YAML flattened leaf、测试、文档、env、tier、alias/tombstone 与 live config；COG-018 收敛退役 prompt-call 配置后，当前为 464 个 registry entry、309 个 read sites，removed/unknown reader 和 divergent fallback 均为 0。
- **Document Asset Manifest（2026-07-12 / ROOT-20260710-022）**：`docs/acceptance/document_asset_manifest.json` 是 repo Markdown、Prompt/schema 与 Desktop system-map 的单一分类契约，`scripts/audit_document_asset_manifest.py` 用 Git tracked 分母验证当前 70/70 Markdown、23/23 Prompt/schema、25/25 Desktop assets，exclude=0、unverified=0。Prompt 条目绑定精确 SHA-256、实际 AST consumer symbol 与 json-schema/inline/Markdown/runtime output contract；document loader 还验证实际 `_load_document_prompt()` binding，orphan schema 失败。Desktop `00–10` 的证据行同时引用 current-state 与存在的 repo 锚点，`86–98` 的头部 commit 必须当前；full-score required profile 在 hermetic HOME 下通过 repo ancestor 只读发现真实 Desktop map。
- **Docs Freshness Audit（2026-07-05 / F20；2026-07-12 ROOT-022 全分母）**：`scripts/audit_docs_freshness.py --strict` 已接入 local/pre-commit/CI，并复用 document manifest 的 Git tracked Markdown 自动发现，再加可发现的 Desktop map；阻断裸 `python`、本机路径、缺失 repo 相对路径和未登记 config key。`scripts/audit_desktop_system_map_facts.py` 继续校验 `current_state` 的 commit、Quick 与 local receipt，历史 scan 字段只作为旧证据。
- **Docs Sensitive Info Audit（2026-07-05 / F21；2026-07-12 ROOT-022 全分母）**：`scripts/audit_docs_sensitive_info.py --strict` 已接入 local/pre-commit/CI，并复用同一 tracked Markdown 自动发现和 Desktop map，当前 94 份 Markdown 全量扫描；阻断 raw provider key/JWT、本机绝对路径、真实 API endpoint、明文 credential 赋值、个人邮箱/手机号/身份证和 PII 赋值。安全占位值仍必须使用安全引用或示例域名。
- **Repo Sensitive Literal Audit（2026-07-05 / F24）**：`scripts/audit_repo_sensitive_literals.py --strict` 已接入 `scripts/run_local_gates.py`、pre-commit 和 CI，扫描 git tracked 与未忽略的 untracked 文本，阻断源码、测试和文档中的完整 provider-shaped fake key、本机 home path 和明文 credential literal；需要测试 redaction 时必须用运行时拼接或 `DUMMY_CREDENTIAL_*` 哨兵，根目录不再保留陈旧 `PLAN.md`。
- **Release Privacy/Security Gate（2026-07-05 / F26，ISS-009 扫描面收紧）**：`scripts/audit_release_privacy_security.py --strict` 已接入 `scripts/run_local_gates.py` 和 `scripts/run_full_score_gates.py` 的 `security.release_privacy` gate，聚合 `scripts/security_audit.py --strict`、`mnemos doctor config --strict --json`、`mnemos health --json` 的 security/privacy 切片、docs sensitive audit、repo sensitive literal audit，以及 health/config、`mnemos_cli.py distill status`、`scripts/e2e_probe.py --dry-run --no-api` 的诊断脱敏扫描，统一输出 `mnemos.release_privacy_security.v1`、`blocking_findings`、`warning_findings` 和 `repair_actions`。
- **Typed Security Report（2026-07-12 / ROOT-20260710-017）**：`scripts/security_audit.py` 独占 Bandit、pip-audit 与 health security 到 `SecurityFinding` 的归一化和 `mnemos.security_audit.v2` 校验；报告的 counts/status/`ok`/退出码只能由 findings 推导，核心不变量为 `ok == (blocking_count == 0)`。`scripts/audit_release_privacy_security.py` 不复制判断逻辑，而是运行 strict JSON 并调用同一 validator；矛盾 schema、counts、findings、status、`ok` 或返回码本身就是 release blocker。
- **Docs Stale Service Key Audit（2026-07-05）**：公开配置示例中的 daemon service key 以 `daemon.services.eventbus` 为 canonical；退役服务别名只允许留在迁移实现和测试中。`scripts/audit_docs_stale_service_keys.py` 已接入 `scripts/run_local_gates.py`，扫描 README、README-en 和 docs 的 live config 示例，防止旧服务键被复制回新手配置。
- **Runtime Dependency Cycle Gate（2026-07-05）**：`scripts/arch_dependency_graph.py --check` 继续保证无未豁免 module-level cycle，并要求 runtime-only waiver 明确 owner、target interface、resolution 和具体 arch-debt issue。`core.cli.helpers` 的 Obsidian vault 注册检查已下沉到 `core.vaults.obsidian_registry`，因此 core helper 不再反向依赖 integrations backend；backend adapter 也复用同一个 core vault registry port。
- **Wow Path E2E（2026-07-05）**：新增 `scripts/e2e_wow_probe.py` 与 `tests/e2e/test_wow_path.py`，把首次配置、可选多模态、可信用户文档 100MB gate、默认 distill、行为/意图字段、Obsidian 路由、ContextAwareSearch/preflight 召回、runtime consumer ledger 和 auto-heal dry-run 串成一条用户价值验收链路。`scripts/run_full_score_gates.py` 的 E2E gate 改为 `e2e_wow_probe.py --mock-llm` / `--real-api`，避免 full-score 只通过底层连通性探针。
- **Runtime Path Authority（2026-07-04）**：运行时代码默认 Wiki/raw 路径必须从 `get_config().wiki_dir`、`Config.vault_dir("raw"|"mnemos")` 或显式 CLI 参数读取；首次设置默认值集中在 `core/setup/vault_layout.py`。`scripts/audit_hardcoded_paths.py --strict` 已接入 `scripts/run_local_gates.py`，阻断生产代码中的本机绝对路径、旧 Obsidian wiki 默认和绕过配置的 Mnemos/raw vault 字面量。
- **Trusted Push Write Authority（2026-07-09；2026-07-12 ROOT-20260710-020 静态证明收口）**：正式 Markdown mutation 默认必须走 `TrustedVaultMutationService`、`core.trust.formal_markdown.submit_or_write_markdown` 或 `core.trust.markdown_update`。底层 fallback write/delete/move 只存在于 central commit helper；typed receipt 绑定 target/content/expected-existing hash，move 再绑定 source/source hash，提交后目标或源发生变化即拒绝。Charon 分类移动和 Eris duplicate 删除也复用同一边界，`enforce` 只生成 proposal。`core.trust.static_scan` v4 用 AST 发现 write/open/rename/replace/unlink/os/shutil/atomic helper，按控制流证明 receipt guard dominance；删除目录/整文件 marker allowlist。非正式 report/artifact/system_state/backup/shadow 或显式 recovery 只能进入精确 sink registry，并携带稳定 sink ID、owner、target class、expiry；registry 不能自称 guarded/trusted writer。当前 169 sinks=143 registry+17 receipt-dominated+7 central writer+2 primitive，unknown/stale/known bypass=0。
- **Immutable Raw Revision / Provenance（2026-07-10 / ROOT-004）**：`raw_turns` 是 logical alias/current view，正文证据追加到 `raw_turn_revisions(revision_id, supersedes_revision_id, snapshot_blob)`；`raw_provenance_edges` 用 revision/span 连接 Amphora task 和 Wiki page，`raw_provenance_gaps` 只登记无法证明的历史页。`StorageApplicationService.session_search()` 先读取 metadata-only header 做 ACL，再取 canonical revision 正文；RawIndex 必须以 authorized identity 约束候选，缺失投影不能阻断 canonical search。retention 以 logical metrics 聚合访问，但任何 revision edge 都提高 reference count 并阻止物理删除。
- **Lossless Distillation Input（2026-07-10 / ROOT-005）**：正式 extract 输入只允许结构化排除显式 private thinking；`clean_message_content()` 不再压缩代码、命令或可见格式，WikiBuilder plaintext fallback 不再固定取 500 字符。长消息由 `_chunk_messages() -> Tokenizer.split_to_tokens()` 全量拆成 part；标准/分块 extractor 均以 `lossless=True` 组装 session text，格式化后超预算只记录 overflow，不执行 head-tail 或单消息截断。private exclusion 元数据只含类型、span、计数，不含正文；摘要/评分型预算路径不得复用为 canonical extraction input。检查点身份由 `chunk_checkpoint.build_chunk_fingerprint()` 统一生成，包含 `lossless-visible-v1`，同一版本写入 `chunk_info`；旧的无版本检查点不能命中当前执行，必须重跑缺失 chunk。
- **Distill Execution Spec Checkpoints（2026-07-11 / ROOT-014，COG-011 v2）**：`core/hephaestus/distill_execution_spec.py` 将真实渲染 prompt、输出 schema、extract/parse/quality 代码摘要、显式 backend/provider/model route、merge 合同和全部输出相关有效配置冻结为 `mnemos.distill_execution_spec.v2`；v2 还绑定 `DistillInputSpec` hash 与 `distill_output_v4` admission contract。生产 backend/merger 通过 `checkpoint_identity()` 声明身份，不允许反射或隐式 caller fallback。chunk fingerprint 组合 lossless input 与 spec hash；SQLite 主键 `(session_id, chunk_index, chunk_hash)` 保留多代 completed/failed，lookup 用 `miss_reason/spec_diff_fields` 解释失效。schema v1 行迁移后保留但 spec 为空，永不复用；新规格失败不能覆盖旧成功代际。Prompt 预渲染使用 rule-only intent route，保证 cache identity 计算不产生额外 LLM 调用。
- **Typed Distill Output Admission（2026-07-15 / COG-011）**：`core/hephaestus/distill_input_spec.py` 在任何 Prompt 渲染前冻结 agent/session/event、raw completeness、可见输入 hash、gate id 和 mode；`ExtractionRequest`、`PreparedExtractionPrompt`、`DistillExecutionSpec` 与最终 root output 都必须绑定同一 spec。`prompts/distill/_output_schemas/extract.json` 是唯一的 Draft 2020-12 根 union：合法 skip 必须是 `judgment=skip`、空 fragments/claims、`distill_intent=skip`、非空 `skip_reason` 和引用已绑定 event 的 `no_value_evidence`；knowledge/skill 则必须有至少一个 fragment、non-skip intent、claims 与 `user_behavior_intent`。同一 schema 还拥有 behavior、artifact ref、relation 和 cognitive-action 的条件依赖，runtime format validation 不能由 prompt 或 router 推测替代。同一 runtime validator 在首次输出修正前、修正后、检查点保存/读取和正式写入前执行；保存/读取 checkpoint 都必须提供完整 `DistillInputSpec`，非法空 non-skip 不能被转换成 skip。checkpoint 另持久化 `CheckpointAdmission(input_spec_hash, output_contract_version, canonical_output_hash, judgment)`，缺失根输出/admission 的旧行、spec/contract 漂移或 root/hash/fragment 破损一律 fail-closed miss。正式写入再次核验 canonical root、hash、judgment 与 structured output；`create_page` 还必须通过 Engine 在受控末段签发的 `FragmentRouteCapability`，它绑定 root hash、input-spec hash 和可写 fragment 对象 tuple，拒绝后续片段替换；release audit 要求 `distill.structured_output_contract.enforce=true` 与 `distill.action_router.enabled=true`，关闭任一项只能是非发布诊断。
- **System-owned Artifact Identity（2026-07-15 / COG-029）**：`core/evidence/artifact_catalog.py` 是 artifact catalog/ref resolution owner。Capture 与 SyncEngine complete-session handoff 先把附件、工具结果、reasoning/test artifact 绑定 authoritative Raw revision；`DistillInputSpec v4` 再按 type + 完整 SHA-256 生成稳定 opaque ref、content-addressed URI 和 chunk-local source allowlist。文件型 artifact 必须现场读文件重算 hash；pathless tool result 必须携带不进 Prompt 的 canonical inline payload，由 Catalog 重算，marker 或 caller 自报 SHA 不能证明身份。复用 Raw revision 时当前内容 hash 必须匹配 header，handoff 只使用 header 的 authoritative hash；malformed ref 也会作为无效 catalog 项保留到 pre-model gate，不能静默丢弃。Prompt 不暴露 path/inline payload/URI/hash/ACL，模型 schema 只允许选择 `artifact_ref_id`；Extractor 在 correction/admission 前解析完整系统字段。任一当前 input ref 的文件、hash、ACL 或 source admission 失败会在模型调用前整体阻断，不会静默降级；catalog/URI resolver 代码摘要也进入 `DistillExecutionSpec`，防止旧 checkpoint 跨身份语义命中。未知、伪造、跨 chunk、越权、type/hash 漂移和模型自填 identity fail closed；相同内容跨路径/轮次保持同一 ref 与 checkpoint identity。摘要仅按个人隐私、API key/令牌、银行卡、密码/私钥做窄脱敏，不加密。
- **System-owned Cognitive Source Authority（2026-07-15 / COG-044）**：`core/evidence/source_authority.py` 把每个 role-local Raw span 与 artifact summary 固化为七类 `SourceAuthority`，并随 `DistillInputSpec v4` 进入 Prompt、execution spec 和 checkpoint hash。高权 user/system 文本中的 Markdown 引用、代码围栏/行内代码及中英日韩成对引号会按原始 offset 拆成低权 `quoted_content` 子 span；detached 格式化输入没有 role-local proof 时也只能低权保存。模型只能选能确定的 opaque `source_authority_id`，否则省略并由系统按 quote 唯一解析；系统验证 event、artifact、quote、role 与 span 后解析权限字段，助手/工具角色以及 external/quoted metadata 不能靠 caller metadata 或模型输出升级。外部/引用/助手/工具内容仍可创建普通可检索页面；高价值 update/merge/reinforcement 转为 authority-pending hypothesis，认知动作记录 `authority_blocked`，skill 全量资产可保存但不生成自动化派生 proposal。Raw 入口不再因 prompt-injection 关键词阻断：内容完整写入并带非阻断标签；Observation 只把 user span 作为用户认知，assistant bytes 保留在 Raw，外部页与外部 Raw 只供 Attention 类消费者。`scripts/audit_cognitive_source_authority.py --strict --json` 以七类分母、多语言/编码引用 corpus、角色混淆/metadata/模型伪造 mutation、Raw 无损和外部知识保留共同验收。
- **Skill Cognition Asset Commit（2026-07-15 / COG-013）**：`judgment=skill` 不再是 suggestion-only 终止分支。`CognitionAssetStore` 必须先把完整 admitted root、全部最终 fragments、chunk aggregate、Raw source spans 与 private ACL 作为不可变 `cognition_asset_commit` 写入 `distill_actions.db`，随后才允许派生 versioned `CognitiveDecisionAssetProposal`；最后仍通过普通 action router 写 Wiki 并发布 Wiki/search 投影事件。资产、proposal 与页面各有独立 receipt：proposal 失败仅记录 `optional_failed`，不回滚已提交资产/页面；资产未提交则禁止 proposal、Wiki 与 processed。`skill_suggestion` 只保留为已提交 proposal 的显示兼容字段，不是存储真相。持久化边界仅按 `pii_credentials_only_v1` 脱敏个人隐私、API key/凭据、银行卡和密码，不做整库或字段加密；资产 identity 绑定该策略版本，ACL、source/session/provenance 与普通代码内容保持可用。
- **Adaptive Policy Coverage（2026-07-04）**：`core/kia/adaptive_policy_matrix.py` 固化 `mnemos.adaptive_policy_coverage.v1`，把 AdaptiveConfig 从单一 `app.push_max_items` 动态入口扩展到 distill、quality_gate、scoring、delivery、search、raw、document_process、intent 和 cognitive_decision。`daemon/adaptive_service.py` 采集 search/no_result、raw completeness、distill action、delivery feedback、document rejection、stale page 和 scorer feedback 指标；运行时消费者通过 `EffectivePolicy` active shadow 覆盖蒸馏阈值、质量门阈值、raw retention、文档大小、intent fallback、delivery budget 和 trust delivery gate。`mnemos status` 与 `checks.adaptive_policy` 暴露 coverage、active_shadow、metric_before、age_hours 和 overdue rollback。
- **Cognitive Decision Flywheel（2026-07-04）**：`core/kia/ixion.py` 负责编排，`core/kia/cognitive_decision_assets.py` 承担 `cognitive_decision_asset.v1` DTO、行为生成器、Wiki 候选扫描和资产持久化 mixin；旧 `SkillWikiFlywheel` / `Skill 飞轮` 只作为兼容 alias。Wiki 方法论、行为重复模式和 Skill 失败/新场景会先沉淀为带证据、适用条件、失败模式和验证 recipe 的认知决策资产；automation skill 只能在资产显式 `automation_derivative_allowed=true` 后派生。
- **Auto-Healing Orchestrator（2026-07-04）**：`core/ops/auto_healing.py` 将 health、daemon、queue、KIA/issue pipeline 暴露的修复面收束为 `mnemos.auto_heal_orchestrator.v1` 决策卡。`checks.auto_healing` 为每个非 ok health check 标注 `auto_fixed`、`auto_fix_failed`、`needs_user`、`ignored_with_reason` 或 `blocked`，并暴露 risk、repair action、rollback plan、verification command 与 `auto_heal.user_intervention_budget`；显式 handler apply 成功时写 `ActionLedger(action_type=auto_heal)`，`mnemos doctor repair --dry-run --json` 输出同一计划。
- **Wiki Quality Contract（2026-07-04）**：`scripts/wiki_lint.py --summary --json` 固化为 `mnemos.wiki_quality.v1`，输出 summary、预算线、manual review、统一生命周期映射和 `obsidian_experience` scorecard 指标；`--budget` 用重建后质量预算阻断超线，`--fix` 只修已有 frontmatter 的缺失元数据并记录 `wiki_quality_fix` ActionLedger。`scripts/audit_wiki_quality_contract.py --strict` 校验 missing_meta/orphan/broken_link/stub 的状态机、预算 owner/strategy 和 scorecard 映射。
- **Cognitive Readiness Contract（2026-07-04）**：`core/ops/cognitive_readiness.py` 输出 `mnemos.cognitive_readiness.v2`，并内嵌 `mnemos.learning_signal.v2`，把 raw/Wiki metrics/KG/evidence graph/delivery/outcome/search/reminder/observation/reflection/policy patch/consolidation 运行态整理为来源、证据、消费者、行为四段；`scripts/audit_cognitive_readiness.py --json --budget` 按统一预算阻断未闭环数据，`--record-gaps` 显式写 `cognitive_readiness_gap` ActionLedger，`mnemos health --json` 的 strict `checks.cognitive_readiness` 复用同一预算并在失败时进入 `strict_failures`；非 strict `checks.cognitive_learning` 暴露 raw/feedback/search/reflection 到 observation、policy patch/no_patch 证据和 consolidation run 的转化缺口，并把结果纳入 `cognitive_assets` scorecard。`WikiMetrics.scan_all_pages()` 通过 `core/wiki_page_roles.py` 将 Wiki 页面标注为 `knowledge`、派生产物、系统报告、index、占位/骨架页或测试产物，readiness 的来源预算只要求真实 knowledge 页具备非空 `source_refs`，同时报告豁免原因与 stale metrics。`ContextAwareSearch` 兼容记录 search click/open/ignore/no_result，测试/自定义 Wiki 搜索会话落本地 `.kg/mnemos.db`，`AdaptiveScorerV2.ensure_tables()` 会为旧 `search_sessions` 表补 outcome 字段，daemon ignore detection 必须关闭原 search session outcome。
- **Cognitive Readiness Evidence Contract（2026-07-11 / ROOT-013）**：v2 的 score 只能由逐项 effect evidence 清 gap。required table/DB 不可读、缺失或旧 schema 为 blocked；required evidence/lineage 的 0/0 为 unobserved，不能得 100。`core/ops/cognitive_readiness_lineage.py` 统一计算 visible delivery→explicit feedback/reciprocal outcome、raw event/current revision→observation、reflection/recap driver→patch/no_patch、consolidation candidate→applied coverage，并输出 denominator、covered、uncovered、coverage ratio、lineage sample、freshness 和 cold-start。默认时效窗口 30 天；坏时间、过期、unlinked outcome 和 dry-run consolidation 都不能清 gap。health、doctor、scorecard 和 audit CLI 消费同一 v2 报告。distill runtime receipt 还必须以 task id + input revision 绑定 exact producer generation，禁止同 session 的旧 generation 消费最新 producer event。
- **PolicyPatch Relevance Contract（2026-07-11 / ROOT-008）**：`core/cognitive/policy_patch.py` 负责 trigger 生产约束、当前上下文匹配、scope 过滤、task-fit 排序、去重与干扰预算；`core/kia/policy_patch_adapter.py` 只把已解释的 match 投影为 KIA checklist/response。patch content 永不参与 trigger 命中，ASCII trigger 使用 token boundary，非 global patch 只有在 `AccessNarrowing.project` 精确匹配时可见。`ReflectionPolicyPatchConsumer` 只使用稳定 dimension/trigger，不把生成式 `key_points` 送入 trigger；历史修复由 `scripts/reconcile_policy_patch_triggers.py` 以 dry-run、备份、apply、幂等复核完成。
- **Distill JSON Quality（2026-07-04）**：`core/hephaestus/distillation_json.py` 返回 `direct_json`、`markdown_json`、`balanced_json`、`fixed_json`、`failed` 解析路径元数据；`core/hephaestus/distillation_metrics.py` 将 redacted 解析事件写入 `distill_metrics.db`，`core/ops/health_check.py` 输出非 strict `checks.distill_json_quality` 趋势。fallback 成功只进 debug/metrics，最终失败才 warning；格式失败复盘按错误指纹聚类。
- **Quality Debt Closure（2026-07-12 / ROOT-20260710-021）**：`scripts/check_maintainability_budget.py` v2 同时输出 `ratchet_status` 与 `mnemos.maintainability_closure.v1`。当前扫描为 16 个超大文件、478 个 broad catch（120 个未分类、required-path 0）；exact AST fingerprint、owner、expiry、telemetry、remove condition 使同数量替换、parse failure、过期接受、改善后未收紧 baseline 或普通 update 吸收增长均失败。`scripts/check_zombie_code_policy.py` 当前检出 131 个未记录 candidate，均保持 closure 失败而非进入 baseline；local/pre-commit/CI 与 strict release 证据分开。full-score 的 maintainability/zombie/vulture 三个 strict zero-closure gate 任一 residual 非零即 `release_eligible=false`；vulture current/baseline 为 0/0 且非零永不允许 rebaseline。

---

## 目录

1. [系统架构概览](#系统架构概览)
2. [核心数据流](#核心数据流)
3. [五大子系统详解](#五大子系统详解)
4. [数据模型](#数据模型)
5. [状态机设计](#状态机设计)
6. [扩展点设计](#扩展点设计)

---

## 系统架构概览

### 整体架构图（v2.0.0 三层模型）

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 3: Agent 适配器层 (Olympus / Source 模块)                 │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │
│  │ Claude  │ │  Kimi   │ │ OpenCode│ │  Codex  │ │ Cursor  │   │
│  │(Apollon)│ │(Adapter)│ │(Source) │ │(Source) │ │(Source) │   │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘   │
│       │           │           │           │           │         │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐                │
│  │ Windsurf│ │  Aider  │ │ Gemini  │ │ OpenClaw│                │
│  │(Source) │ │(Source) │ │(Source) │ │(Source) │                │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘                │
│       │           │           │           │                      │
│       └───────────┴───────────┴───────────┴───────────┘          │
│                           │                                      │
│                           ▼                                      │
├──────────────────────────────────────────────────────────────────┤
│  Layer 2: 统一事件总线 (Mnemos Event Bus)                         │
│  SQLite 持久化事件队列（跨进程/跨 Agent）                         │
│  session.start | session.end | distill.request | signal.batch    │
├──────────────────────────────────────────────────────────────────┤
│  Layer 1: Mnemos 核心服务（Agent-Agnostic）                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐      │
│  │Hephaestus│ │   KIA    │ │ Persona  │ │   Daemon       │      │
│  │(蒸馏Worker)│ │(知识注入) │ │(画像系统) │ │ (后台服务)      │      │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └───────┬────────┘      │
│       │            │            │               │               │
│       └────────────┴────────────┴───────────────┘               │
│                           │                                      │
│                           ▼                                      │
│  ┌────────────────────────────────────────────────────────┐     │
│  │         Wiki 知识库 (Obsidian Vault)                    │     │
│  │  00-Inbox/  01-People/  02-Projects/  03-Tech/         │     │
│  │  04-Concepts/ 05-MOCs/  06-Retrospectives/ 07-Shadow/  │     │
│  └────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
```

### 各层职责

**Layer 3 — Agent 适配器层 (Olympus / Source 模块)**
- `core/sync_framework/registry.py` — `SourceRegistry` 注册中心 + `AgentLifecycleManager` 生命周期管理
- `core/agent_kit/source_support_manifest.py` — 加载并冻结唯一 Source support manifest；registry、Agent Kit report、diagnostics、daemon snapshot 与 install evidence 仅从它派生。每个 active source 还在该 manifest 中声明由默认启用的 `daemon.raw_sync` 持有的连续 owner、trigger accelerator 与 SLA；逐源安全 coverage state 会在重启后恢复到 heartbeat。`NativeSourceSnapshot` 绑定 manifest hash、resolved roots、parser、cursor 和 native denominator，不能反向定义来源能力。
- `integrations/sources/` — 各 Agent 的 `AgentSource` 实现，负责被动读取本地会话文件
  - `claude_source.py`, `kimi_source.py`, `aider_source.py`, `gemini_cli_source.py`
  - `cursor_source.py`, `windsurf_source.py`, `opencode_source.py`
  - `codex_source.py`, `hermes_source.py`, `openclaw_source.py`（2026-06-13 恢复）
- `integrations/olympus.py` / `apollon.py` / `kimi_adapter.py` — Claude / Kimi / Crush 等 Agent 的 adapter 主动接入（Hooks / MCP）
- **接入方式**：Codex / Hermes / OpenCode / OpenClaw / Kiro 统一走 MCP-only 主动接入，通过 `mnemos` MCP server 调用 `preflight_inject` 等工具；Claude / Kimi / Crush 走 adapter hooks，并可同时配置 MCP。所有 MCP host 只持有 keyring reference，服务端 launch capability/grant 决定 principal 和 tool policy，caller 不得自报身份或跨 Agent 权限。Mnemos 本地 SQLite 不做整库加密，诊断输出只脱敏用户个人隐私、API key、银行卡和密码等敏感字段。画像相关 MCP 高频入口必须可降级：画像 SQLite 被 daemon 占锁导致 `PreFlightInjector` 或 `SignalStore` 暂不可用时，`SignalStore` 按 2 秒默认连接/忙等待预算退出并释放 transient connection，`preflight_inject` 返回带 `degraded_reason` 的成功响应，`guard_check` 回退默认守护清单，reflection 以 `persona_store=None` 继续运行，`persona_behavior_metrics` 返回基础行为指标并把 `profile_usage` 降级为空指标，避免 MCP 工具错误中断宿主会话。
- 所有 Source 实现统一的 `AgentSource` 接口：`name`, `model_tag`, `data_dir`, `discover_sessions()`, `parse_turns()`, `trigger_strategy()` 等

**Layer 2 — 统一事件总线**
- `core/mnemos_bus.py` — SQLite 持久化事件队列与 typed handler outcome 调度
- 运行库：`~/.mnemos/events.db`；Wiki mutation 权威账本为 `~/.mnemos/wiki_projection.db`
- 标准事件：`session.start`, `session.end`, `distill.request`, `signal.batch`
- `publish()/subscribe()` 是主路径；`poll()/ack()/move_to_processing()` 只保留旧手动消费兼容面
- Wiki lifecycle 使用 `ack/noop/retry/defer/dead`、stable consumer ID、因果 revision watermark、持久重试和 per-consumer receipt；业务 soft failure 不得进入 done
- 跨进程、跨 Agent，无需额外消息队列依赖
- `guard_alert` 等 `_NO_PERSIST_EVENT_TYPES` 是可丢弃遥测：全局 EventBus 尚未初始化且当前进程没有消费者时，`publish_event()` 不应为了丢弃事件触碰 `events.db`；Aegis 事件发布遇到 SQLite 锁也必须降级为 warning，不能反向破坏 `guard_check` 的 MCP 响应。

**Layer 1 — Mnemos 核心服务**
- `core/hephaestus_worker.py` — 蒸馏 Worker（轮询 amphora SQLite 队列 → 刷新 processing `updated_at` → 直接调用 LLM API → 验证格式 → 按 Charon 路由写入正式 Wiki 目录或留 Inbox 待审；L1 `status=distilled` 标记使用当前 worker 的 `inbox_dir.parent` 创建 StorageBackend，保证追溯标记与写入目标同 vault；卡住的 processing 任务会被 health stale 预算识别，并可用 `mnemos distill reset-timeouts` 返回 pending）
- `core/kia/` — Knowledge-in-Action 闭环（预加载、守护、复盘）
- `core/persona/` — 用户画像系统（通用信号采集 → `cognitive_profile.py` 用户认知画像 v2 signal/assertion/usage → 画像分析 → 盲区检测 → 校准 → 消费效果日志）
- `mnemos_daemon.py` — 后台守护进程（信号采集、蒸馏调度、画像更新、知识调度）
- `core/system_contracts.py` — 跨模块契约注册表和轻量 facade，不替代业务存储；用于 health、strict audit、质量门映射和 ActionLedger 接入。
- `core/document_import.py` / `core/application/document_import_service.py` — trusted_user_document 单一所有权导入后端；`mnemos import`、MCP `document_process`、daemon `FileIngestor` 和 KnowledgeInbox 共享路径安全、100MB 配置化大小限制、隐私预扫描、ActionLedger 和来源字段。`capture` 只写 canonical raw，默认 `distill` 在同一 raw receipt 上请求 capture outbox，`parse` 只预览，`watch` 只预检；公开结果包含 ingestion/handoff/projection 状态、stable document asset 和 raw revision。
- `core/module_toggles.py` — 模块开关与冷启动产物契约注册表；覆盖默认关闭、隐私关闭、成本关闭、watcher/daemon、legacy/stale 开关，声明自动开启策略、自动关闭策略、产出 schema、消费者、效果指标、互斥关系和回滚策略。
- `core/migrations/registry.py` — 版本迁移注册表和迁移账本；旧配置 key 清理会先备份再 apply，旧 DB/Vault/加密迁移脚本保留为 registry wrapper。
- `core/backup/snapshot_manager.py` — 全局快照与恢复 manifest；恢复必须先 plan、再 apply、再 verify，冲突返回 blocked。
- `core/privacy/data_ownership.py` — 用户数据所有权契约；统一 raw、Wiki、metadata、evidence refs、persona、reflection、scoring、Action Ledger、model-call ledger、consumer access log、agent source metadata 的导出、冻结、删除计划和删除证明。
- `core/benchmarks/golden.py` — 可重复认知质量基准；使用固定 synthetic manifest 和 mock provider 生成临时 Wiki、persona delta、ActionLedger 与 scorecard，不读取真实用户个人数据。
- `core/setup/install_lifecycle.py` — 产品级安装/升级/卸载状态机；统一 setup、upgrade、uninstall、repair-all 的机器可读状态、失败原因、修复动作、备份引用和 ActionLedger 证据；health strict 面会把 partial required steps 降级。
- `scripts/wiki_lint.py` / `scripts/audit_wiki_quality_contract.py` — Wiki/Vault 质量合同；把 lint 统计、预算、人工清单和自动元数据修复证据纳入 `mnemos.wiki_quality.v1`、统一生命周期和 scorecard。
- `core/ops/cognitive_readiness.py` / `scripts/audit_cognitive_readiness.py` — 认知数据就绪度合同；把来源、证据、消费者、行为反馈与 `mnemos.learning_signal.v2` 转化缺口纳入预算、ActionLedger、strict health `checks.cognitive_readiness`、非 strict health `checks.cognitive_learning` 和 `cognitive_assets` scorecard。
- `core/kia/adaptive_config.py` / `core/kia/adaptive_policy_matrix.py` / `core/kia/policy.py` / `daemon/adaptive_service.py` — 自适应策略闭环；`AdaptiveConfig` 维护 EWMA/cooldown/shadow/rollback，覆盖矩阵声明输入信号、可调参数、读取入口、回滚指标和验收指标，`EffectivePolicy` 只在 active shadow 存在时覆盖调用方配置。
- `core/hephaestus/distillation_json.py` / `core/hephaestus/distillation_metrics.py` — 蒸馏 LLM JSON 解析质量；直接解析、markdown、平衡括号、自动修复和最终失败都有结构化路径，metrics 只保存路径/错误摘要/修复次数，不复制原始 LLM 输出。
- `scripts/setup_model_endpoints.py` — 安装期模型端点 helper；`scripts/auto_setup.py` 复用它检测必填模型 env 和可选 multimodal，保持 `MNEMOS_MULTIMODAL_*` 录入、跳过和 launchd/cron 导出语义一致。
- `daemon/heartbeat.py` / `daemon/service_state.py` / `core/ops/health_check.py` / `core/ops/cognitive_data_contract.py` / `core/ops/producer_consumer_ledger.py` / `core/ops/runtime_flow_health.py` / `core/ops/runtime_flow_telemetry.py` / `core/ops/auto_healing.py` / `core/ops/keyring_doctor.py` / `scripts/health_check.py` / `core/privacy/secret_inventory.py` — daemon service、runtime producer/consumer closure、cognitive data event registry、SQLite disk budget、keyring/env fallback security warning、Wiki route、distill JSON quality、distill cognitive action counts、auto-healing 计划与 strict health 状态；区分当前错误和历史已恢复错误，`raw_projection` 恢复后清除旧错误并写 `raw_projection_recovered` ActionLedger；`wiki_route` daemon 服务只做 route-only Charon connect，不写 KG 关系；`checks.runtime_producer_consumer` 只读输出 `mnemos.runtime_producer_consumer.v2` 的 produced events、consumer receipts、dead letters、event × intended-consumer coverage、pending、freshness、last produced/consumed 和 `cognitive_data` 摘要，required 0/0 不再算 green，未 bootstrap/旧 schema 直接 blocked。后者覆盖事件数、消费数、duplicate/derived/reinforcement 对账、unregistered producer/consumer、consumed-without-event 和 unexplained divergence；初始化、迁移与 outbox replay 由显式 bootstrap 承担。
- `scripts/check_maintainability_budget.py` / `scripts/maintainability_budget.json` — 可维护性 ratchet + closure；exact catch fingerprints 和时限接受阻断同量替换/过期/宽松 baseline，strict profile 只接受 residual=0。
- `scripts/audit_docs_freshness.py` — 文档新鲜度审计；默认覆盖 AGENTS、CLAUDE、CONTRIBUTING、README、README-en、SECURITY、docs 和可发现的 `~/Desktop/mnemos系统图谱`，阻断本机绝对路径、裸 `python` 调脚本/调模块/执行内联代码、缺失 repo 相对路径和未登记配置 key 示例回归。
- `scripts/audit_document_asset_manifest.py` — 文档资产分母与 Prompt/Desktop 契约审计；自动发现 tracked Markdown，核对 Prompt/schema hash/consumer/output contract，并要求 Desktop 当前证据锚点与生成 commit 对齐；release profile 使用 `--desktop-mode required`。
- `scripts/audit_desktop_system_map_facts.py` — Desktop 系统图谱机器事实当前态审计；校验 `99-代码扫描-facts.json.current_state` 的 schema、repo commit、成功 local gates 与 quick 证据，防止旧事实快照被误读为当前运行结论。
- `scripts/audit_docs_sensitive_info.py` — 公开 Markdown 敏感信息审计；阻断 raw key/JWT、本机路径、真实 API endpoint、明文 credential 赋值、个人邮箱/手机号/身份证和 PII 赋值，允许安全占位值和示例域名。
- `scripts/audit_repo_sensitive_literals.py` — repo 文本敏感字面量审计；扫描 tracked 与未忽略的 untracked 文件，阻断完整 provider-shaped fake key、本机 home path 和明文 credential literal，覆盖测试与源码，不只覆盖公开 Markdown。
- `scripts/security_audit.py` — typed 安全审计所有者；输出并校验 `mnemos.security_audit.v2`，从 findings 唯一派生 counts/status/`ok`/退出码。
- `scripts/audit_release_privacy_security.py` — 发布级隐私安全总门禁；验证 strict security v2 后聚合 strict config doctor、health security/privacy、docs sensitive、repo sensitive，以及 health/config、`distill status`、E2E dry-run 诊断脱敏扫描，输出 `mnemos.release_privacy_security.v1`。
- `scripts/audit_docs_stale_service_keys.py` — 文档陈旧 daemon service key 审计；阻断 README、README-en 和 docs 的 live config 示例再次出现退役服务键。
- `scripts/e2e_wow_probe.py` — 用户价值端到端探针；在临时 vault 中验证可信文档到 Wiki、召回、preflight、consumer ledger 和 auto-heal dry-run 的哇塞链路，支持 `--dry-run`、`--mock-llm`、`--real-api`。
- `scripts/run_full_score_gates.py` — 满分总验收入口；汇总测试层、local gates、strict health、security strict、release privacy/security、wow-path E2E、配置、认知就绪度、Wiki budget、benchmark、安装/升级探针和 contract audits，默认把 JSON/Markdown/log artifacts 写到 `/tmp`。full-score 的 `health.strict` 要求 `status=ok`、`ok=true`、`usable=true`、`strict_ok=true` 且无 failed/degraded/warning/critical skipped checks；`--strict --real-api` 发布运行拒绝 skip 参数。
- `scripts/verify_full_score_certificate.py` — 独立发布证书 verifier；复算权威 manifest/certificate hash、Git clean/full commit、expected=selected=executed、required receipts 与 stdout/stderr artifact hash。
- `scripts/audit_test_suite_denominator.py` / `scripts/run_cognitive_behavior_scenarios.py` — 分别验证全部 pytest 文件唯一归层，以及执行认知行为矩阵承诺的真实测试文件。
- `scripts/audit_gate_hermeticity.py` — 测试/门禁/诊断状态边界审计；strict 分母固定为 quick、integration、heavy、full-score，`--suite diagnostics` 覆盖 health、verify、status、distill status 和 golden，报告所有产物必须位于同一 sandbox root。


## 核心数据流

### Ingest流程

```
L1原始记录
    │
    ▼
┌─────────────────────┐
│ 1. 内容去重检查     │ ← Guard L0
│    (content_hash)   │
└──────────┬──────────┘
           │ 重复 → 跳过
           ▼
┌─────────────────────┐
│ 2. Wiki污染检测     │ ← Guard L3
│    (wiki_ref密度)   │
└──────────┬──────────┘
           │ 污染 → 仅创建Source
           ▼
┌─────────────────────┐
│ 3. 上下文回忆检测   │ ← Guard L4
│    (context:recall) │
└──────────┬──────────┘
           │ 回忆内容 → 仅创建Source
           ▼
┌─────────────────────┐
│ 4. 质量评估         │ ← Guard LQ
│    (5维评分)        │
└──────────┬──────────┘
           │ <40分 → 隔离
           ▼
┌─────────────────────┐
│ 5. 四大类识别       │
│    (分类+提炼)      │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 6. AI三道自检       │
│    (唯一/严谨/中立) │
└──────────┬──────────┘
           │ 失败 → 拒绝
           ▼
┌─────────────────────┐
│ 7. 多源验证         │
│    (四级验证)       │
└──────────┬──────────┘
           │ 冲突 → 隔离
           ▼
┌─────────────────────┐
│ 8. 写入Wiki         │
│    (Source+Entities)│
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 9. 热力初始化       │
│    (L1, 0分)        │
└─────────────────────┘
```

### AI搜索流程

```
AI查询
    │
    ▼
┌─────────────────────┐
│ 1. 语义搜索         │
│    (关键词+向量)    │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 2. 热力过滤         │
│    (排除L0沉睡)     │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 3. 排序             │
│    (热力+相关度)    │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 4. 读取深度控制     │
│    (按等级裁剪)     │
├─────────────────────┤
│ L0: 仅元数据        │
│ L1-L3: 摘要100字    │
│ L4-L6: 段落500字    │
│ L7-L9: 全文         │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 5. 热力加成         │
│    (+3/+8/+...)     │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│ 6. 返回结果         │
└─────────────────────┘
```

---

## 五大子系统详解

### 1. 四大类信息识别引擎

**核心类**: `FourCategoryEngine`

```python
class FourCategoryEngine:
    def classify_and_refine(self, content: str, tags: List[str]) -> RefinedContent:
        # 1. 三层分类
        classification = self._three_layer_classify(content, tags)
        # 2. 差异化提炼
        cleaned = self._apply_strategy(content, classification)
        # 3. 实体提取
        entities = self._extract_entities(cleaned, classification.type)
        return RefinedContent(
            content_type=classification.type,
            cleaned_content=cleaned,
            entities=entities
        )
```

**三层分类器**:

```
输入: content + tags
    │
    ├──→ Layer 1: 元数据识别 (权重0.3)
    │           基于标签快速判断
    │           source=claude → code (0.9)
    │
    ├──→ Layer 2: 结构特征 (权重0.5)
    │           正则匹配代码/业务/知识/感悟模式
    │           代码块存在 → code (0.3)
    │           金额出现 → business (0.3)
    │
    └──→ Layer 3: 语义识别 (权重0.2)
                关键词密度统计
                关键词超过阈值 → 对应类型
```

### 2. 质量评估体系

**核心类**: `ContentQualityAssessor`

**五维评分算法**:

```python
def assess(self, content: str) -> QualityScore:
    # 各维度评分 (0-1)
    density = self._assess_density(content)      # 信息密度
    structure = self._assess_structure(content)  # 结构化
    uniqueness = self._assess_uniqueness(content) # 独特性
    practicality = self._assess_practicality(content) # 实用性
    citation = self._assess_citations(content)   # 引用质量

    # 加权计算 (0-100)
    total = (
        density * 0.20 +
        structure * 0.20 +
        uniqueness * 0.20 +
        practicality * 0.25 +
        citation * 0.15
    ) * 100

    # 惩罚项
    penalties = self._calculate_penalties(content)
    final_score = max(0, total - sum(p.deduction for p in penalties))

    return QualityScore(total=final_score, ...)
```

**密度评估指标**:
- 有效词比例 = 去除停用词后 / 总词数
- 词汇多样性 = 唯一词数 / 总词数
- 实体密度 = 实体标记数 / 100词

### 3. 多源验证机制

**核心类**: `CrossValidator`, `TieredContentFilter`

**四级验证流程**:

```
内容文本
    │
    ▼
┌─────────────────────┐
│ StatementClassifier │
│  - 分句             │
│  - 句式匹配         │
│  - 类型标记         │
└──────────┬──────────┘
           │
           ▼  [fact, description, definition, conclusion, evaluation, prediction]
┌─────────────────────┐
│ CrossValidator      │
│  - 提取硬事实       │
│  - 两两比较         │
│  - 冲突检测         │
└──────────┬──────────┘
           │
           ▼  验证状态: pending/cross_checking/verified/core/conflicted
┌─────────────────────┐
│ TieredContentFilter │
│  - 按级别过滤       │
│  - 拦截低级别表述   │
│  - 存入隔离库       │
└──────────┬──────────┘
           │
           ▼
    [allowed_statements] [blocked_statements→quarantine]
```

**硬事实冲突检测**:

```python
def _check_hard_fact_conflict(self, facts1, facts2):
    conflicts = []
    hard_facts = [f for f in facts if f.priority == FactPriority.HARD]

    for f1 in hard_facts1:
        for f2 in hard_facts2:
            if f1.type == f2.type and f1.value != f2.value:
                # 同类硬事实值不同 = 冲突
                conflicts.extend([f1, f2])

    return conflicts  # 有冲突 → 状态降级为 conflicted
```

### 4. Expand 2.0 引擎

**核心类**: `ExpandEngineV2`, `ExpandExecutor`

**动态阈值计算**:

```python
class DynamicThresholdCalculator:
    def calculate(self, entity_type: str, source_count: int) -> Threshold:
        base = self.thresholds[entity_type]

        # 素材越多，阈值越高
        source_bonus = min(source_count * 5, 20)

        # 热力越高，要求越低
        heat_discount = self._heat_discount(heat_score)

        return Threshold(
            source_count=base.min_sources,
            heat_score=base.min_heat - heat_discount + source_bonus
        )
```

**三级Expand**:

| 级别 | 触发条件 | 扩展策略 | 输出 |
|------|----------|----------|------|
| L1 | 同义词检测 | 名称变体、缩写 | 实体别名 |
| L2 | 关系挖掘 | 共现实体、引用关系 | 关系图谱 |
| L3 | 深度合成 | 多源知识合并 | 合成页面 |

### 5. 热力追踪系统

**核心类**: `WikiHeatTracker`

**10级状态机**:

```
                    ┌─────────────────────────────────────┐
                    │                                     │
    ┌───────────┐   │   ┌───┐   ┌───┐   ┌───┐          │
    │   Input   │───┴──→│L0 │──→│L1 │──→│L2 │──→ ...   │
    └───────────┘       └───┘   └───┘   └───┘          │
     (负分/衰减)        (-100~0) (0-20) (20-50)        │
                          ▲                            │
                          │ 唤醒 +30分                 │
                          └────────────────────────────┘
```

**每日上限控制**:

```python
def _add_heat(self, page_id: str, points: float):
    today_added = self._get_today_added(page_id)

    if today_added >= DAILY_CAP:  # 50分上限
        return {"blocked": True, "reason": "daily_cap"}

    actual_points = min(points, DAILY_CAP - today_added)
    new_score = min(500, current_score + actual_points)  # L9封顶

    # 检查升级
    if new_level > old_level:
        self._on_level_up(page_id, old_level, new_level)
```

**升级回调**:
- L3+: 标记Expand资格
- L5+: 标记合成候选
- L7+: 加入AI优先读取
- L9: 最高优先级

---

## 数据模型

### 核心实体关系

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Source    │────→│   Entity    │←────│   Concept   │
│   (L1)      │     │   (核心)    │     │   (类型)    │
└─────────────┘     └──────┬──────┘     └─────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌─────────┐  ┌─────────┐  ┌─────────┐
        │  Heat   │  │ Expand  │  │Synthesis│
        │  (L0-L9)│  │ (L3+)   │  │ (L5+)   │
        └─────────┘  └─────────┘  └─────────┘
```

### 数据库表结构

**wiki_heat** (热力主表)
```sql
CREATE TABLE wiki_heat (
    page_id TEXT PRIMARY KEY,
    current_level TEXT DEFAULT 'L1',
    heat_score REAL DEFAULT 0,
    ai_search_hits INTEGER DEFAULT 0,
    ai_citation_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    consecutive_search_days INTEGER DEFAULT 0,
    today_heat_added REAL DEFAULT 0,
    last_heat_date TEXT,
    status TEXT DEFAULT 'active'  -- active/sleeping
);
```

**quarantined_statements** (隔离库)
```sql
CREATE TABLE quarantined_statements (
    id INTEGER PRIMARY KEY,
    entity_name TEXT NOT NULL,
    statement_text TEXT,
    statement_type TEXT,
    required_level INTEGER,
    current_sources INTEGER,
    reviewed BOOLEAN DEFAULT 0,
    decision TEXT  -- approved/rejected/auto_released
);
```

**quality_scores** (质量评分)
```sql
CREATE TABLE quality_scores (
    id INTEGER PRIMARY KEY,
    page_id TEXT NOT NULL,
    total_score REAL,
    density_score REAL,
    structure_score REAL,
    uniqueness_score REAL,
    practicality_score REAL,
    citation_score REAL,
    quality_level TEXT
);
```

---

## 状态机设计

### Ingest任务状态机

```
                    ┌─────────┐
        ┌──────────→│ PENDING │←────────────────┐
        │           └────┬────┘                 │
        │                │ submit               │
        │                ▼                      │
        │           ┌─────────┐    fail(max)   │
        │     ┌────→│PROCESSING│───────────────┼──→ FAILED
        │     │     └────┬────┘               │
        │   retry      success                │
        │     │        /                      │
        │     │       /                       │
        │     │      ▼                        │
        │  ┌────────┐/                    ┌───┴───┐
        └─←│RETRYING│────────────────────→│COMPLETED│
           └────────┘                     └───────┘
```

### 实体验证状态机

```
         ┌─────────────┐
    ┌───→│   pending   │←── 初始状态
    │    └──────┬──────┘
    │           │ 多源交叉验证
    │           ▼
    │    ┌─────────────┐
    └───←│cross_checking│←── 2-3源
    │    └──────┬──────┘
    │           │ 一致性>0.7
    │           ▼
    │    ┌─────────────┐
    └───←│   verified  │←── 4-5源
    │    └──────┬──────┘
    │           │ 一致性>0.8
    │           ▼
    │    ┌─────────────┐
    └───←│    core     │←── 6+源
    │    └──────┬──────┘
    │           │
    │    ┌──────┴──────┐
    └────┤  conflicted │←── 硬事实冲突
         └─────────────┘
```

---

## 扩展点设计

### 插件接口

```python
# 1. 自定义分类器
class CustomClassifier(ContentClassifier):
    def _layer2_structure(self, content: str):
        # 添加自定义模式
        patterns = [
            (r'custom_pattern', 0.3, 'custom_feature')
        ]
        return super()._layer2_structure(content, patterns)

# 2. 自定义质量维度
class CustomQualityAssessor(ContentQualityAssessor):
    def _assess_custom(self, content: str) -> float:
        # 自定义评估逻辑
        score = custom_logic(content)
        return score

# 3. 自定义热力规则
class CustomHeatTracker(WikiHeatTracker):
    def _calculate_level(self, score: float) -> str:
        # 自定义等级划分
        if score > custom_threshold:
            return "L10"  # 添加新等级
        return super()._calculate_level(score)
```

### 配置扩展

```yaml
# config/custom_rules.yaml
custom_classifiers:
  - name: medical
    patterns:
      - r'\b(diagnosis|treatment|symptom)\b'
    type: knowledge

custom_quality_rules:
  - name: citation_count
    weight: 0.1
    min_citations: 3

custom_heat_rules:
  - event: external_share
    points: 15
    daily_cap: 30
```

---

## 性能指标

| 指标 | 目标值 | 实际值 | 状态 |
|------|--------|--------|------|
| Ingest吞吐量 | 100条/小时 | 实测 | ✅ |
| 搜索响应时间 | <100ms | 实测 | ✅ |
| 热力计算延迟 | <10ms | 实测 | ✅ |
| 质量评估时间 | <50ms | 实测 | ✅ |
| 数据库大小 | <1GB | 实测 | ✅ |

---

## 部署架构

```
┌─────────────────────────────────────┐
│           User Machine              │
│  ┌─────────────────────────────┐   │
│  │   Mnemos System         │   │
│  │  ┌─────────┐  ┌─────────┐  │   │
│  │  │Ingest   │  │Scheduler│  │   │
│  │  │Engine   │  │(launchd)│  │   │
│  │  └────┬────┘  └─────────┘  │   │
│  │       │                     │   │
│  │  ┌────┴─────────────────┐  │   │
│  │  │   SQLite Databases   │  │   │
│  │  │  (local storage)     │  │   │
│  │  └─────────────────────┘  │   │
│  └─────────────────────────────┘   │
│            │                        │
│            ▼                        │
│  ┌─────────────────────────────┐   │
│  │   Wiki Directory            │   │
│  │   (Markdown files)          │   │
│  └─────────────────────────────┘   │
│            │                        │
└────────────┼────────────────────────┘
             │
             ▼
┌─────────────────────────────────────┐
│        External Services            │
│  ┌─────────┐  ┌─────────────────┐  │
│  │ LLM API │  │ Embedding/Rerank│  │
│  │Provider │  │  Providers      │  │
│  └─────────┘  └─────────────────┘  │
└─────────────────────────────────────┘
```

---

**文档版本**: v2.0.0 | **代码版本**: v2.0.0 | **更新日期**: 2026-07-01
