# Mnemos Agent 主动接入操作手册

> 本文档面向 AI Agent。如果你是用户，请阅读 README.md。

---

## 你的身份

你是 Mnemos 的宿主 Agent。Mnemos 是你的**长期记忆外脑**和决策辅助层——它保存用户授权进入系统的知识、偏好、历史经验，并在你执行任务时提供上下文支持。

**核心原则**：主动使用 Mnemos，而不是等用户问。Mnemos 的价值在于在合适边界内提前检索、守护和提醒；但你不能把它理解为完全自治的认知系统，也不能绕过 trusted_push、数据删除、配置变更等需要明确授权的门禁。

正式 Markdown write/delete/move 必须复用 `core.trust.formal_markdown` / `core.trust.markdown_update` 的 typed receipt commit path；不得在 proposal 后自行 `write_text`、`unlink` 或 `rename`。receipt 与 target/content/expected-existing hash 绑定，move 另绑定 source/source hash。新增任何 filesystem sink 后必须运行 `python3 -m core.trust.static_scan`；v4 不接受目录/整文件 marker allowlist，非正式 sink 只能用精确 registry 元数据，unknown/stale/known bypass/伪造 guarded 分类均阻断。

## Daemon 身份与运行态判断

- `mnemos daemon status` 只有在 PID record、live OS process、代码/配置/数据库/服务指纹一致时才返回可信运行态；单独看到 PID 或新鲜 timestamp 不等于 daemon healthy。
- `mnemos daemon stop` 遇到 PID reuse、损坏/不完整 identity 或暂不可验证进程时返回非零且不发信号。旧整数 PID 只允许 OS 事实可证明时做一次性迁移停止。
- `mnemos daemon start` 返回成功时，当前 `instance_id` heartbeat 已落盘；随后仍要以 `python3 mnemos_cli.py health --json` 的 `checks.heartbeat.identity_match=true` 和 strict checks 判断完整健康。

## 跨阶段成功判定

- Capture 入 raw store 不等于已完成：只有 matching Amphora enqueue receipt 持久化后，capture event 才能标记 `done`；批量/session end 必须返回可观察的 partial 或 failure，不能吞错。
- Amphora task identity 由 source、session 和 input revision 组成。同 session 内容变化必须产生新 generation；不得恢复 session-only unique 或进程内永久去重。
- 历史 Amphora task 缺少 exact Raw source span 时，只能使用 `scripts/reconcile_amphora_source_spans.py` 的只读 inventory 与 plan-hash-bound apply。Capture→Raw 前置补齐和 source-span generation 替换是两个独立步骤，都要求 daemon/Mnemos MCP writer 停止、SQLite 备份与 apply 后 dry-run；ambiguous/缺消息/哈希漂移必须留作 blocker，不能猜 span 或批量改 terminal。该迁移只恢复可处理输入，不证明模型、Wiki 或 runtime consumer 已完成。
- source-span migration 退休的是旧 task generation，不是认知事件的 `distill` 消费义务。旧 generation 只能追加 canonical `skipped` runtime receipt；若历史 reconciler 曾把 migration script 记成 consumer，或把旧 task 的 `intentional_skip` 误记为 cognitive terminal，必须通过 `scripts/reconcile_distill_runtime_receipts.py` v4 的 reviewed-plan + 双库备份流程追加 exact supersession 与 `revoked + reopen_required` correction，不能 UPDATE/DELETE 原 receipt。历史 task 的无 offset 时间戳必须由操作员以 `--legacy-naive-timezone <IANA-zone>` 明确其原写入时区，禁止随 reconciler 当前宿主时区漂移；该 zone 必须同时绑定 dry-run/apply plan 与 prepared/completed receipt，未提供时相关 lifecycle 证明 fail closed。旧 prejudgment 假 runtime terminal、缺 payload hash 的旧 typed terminal，以及无 task binding 的 cognitive prejudgment/amphora handoff head，只能在 exact active predecessor、同 generation、cognitive event 唯一映射和完整新 terminal proof 同时成立时 append supersession；reviewed plan 必须逐条列出 runtime/cognitive action、predecessor ids 和 reasons，未知 terminal 不能被兼容分支吞掉。apply 在变更前须 durable 写 `prepared` migration receipt，成功时同一 receipt 必须绑定 reviewed plan、双备份、integrity、conservation 和变更计数；可捕获失败必须双库恢复并写 `rolled_back`，禁止 `completed + ok=false`，真正硬中断才保留 prepared 供新 plan 恢复。`terminal_outbox_anchor_sha256` 与 immutable trigger 只由 `core/kia/amphora.py` 持有 DDL；reconciler 必须在同一备份/事务边界调用 owner helper，缺列、缺 trigger、同名伪 trigger 或已绑定 anchor 被改写都 fail closed，rollback 同时恢复 schema 与数据。cognitive snapshot 只有在 correction 同时精确 supersede/correct 当前 head 时才重新暴露 missing；replacement 达到真实 prejudgment/terminal 后再 append superseding receipt。
- 蒸馏只有 durable page 或 explicit intentional skip 属于 terminal success。shadow/enforce proposal pending、partial、retry、intercept 和 write failure 均不能把任务/L1 标成 `done/distilled`。
- recap 只有在页面 receipt、已决定且已落目标的 trusted proposal receipt，或明确 consumer receipt 存在时才可 confirmed/consumed；缺页必须重新打开为 retryable。
- 修复历史状态先运行 `python3 scripts/reconcile_pipeline_receipts.py`，审阅并备份后才运行 `--apply`。实现改动至少覆盖 `tests/integration/test_capture_distill_receipts.py`、`test_distill_terminal_states.py`、`test_pipeline_receipt_reconciliation.py` 与 `test_recap_trusted_completion.py`。
- recap consumption plan 的 label 不是 effect。每个 requested target 必须映射到已注册 canonical consumer 并有 durable command/receipt；`recap_status` 应暴露 plan、required/terminal receipt 计数和最新 feedback correction。负反馈必须通过 correction receipt 撤销、抑制或补偿既有 effect，冲突反馈精确引用最新 `supersedes_event_id`。

## Canonical cognitive state 写入边界

- CognitionEpisode、Belief、Decision、Prediction、ValueContext、Outcome 等领域状态只能经 canonical commit/facade 和唯一 `CognitiveStateStore` 处理；conversation extract 的 CognitionEpisode 必须先由 `commit_cognition_episode()` 提交，其他状态经 `DefaultMnemosServiceFacade.build_cognitive_state/record_decision/apply_outcome` 处理。不要把 typed cognition 塞回 `CognitiveDataEvent.metadata`，也不要让 Wiki、KG、CognitiveGraph 或 ActionLedger 成为第二 source of truth。
- `build_cognitive_state` 必须保持零写入。material decision 与 outcome 写入必须由 `CognitiveStateUnitOfWork` 在同一 SQLite transaction 中提交 typed revision、event envelope 和 local outbox；下游只消费 durable outbox，并以 target effect ID、before/after hash 和 evidence refs 写 reciprocal receipt。
- 单个 consumer terminal 不能把多消费者 event 整体标成 consumed。聚合状态只能从 event × intended-consumer 的当前 append-only receipt 重建；纠错/撤销必须显式 supersede/correct，不能原位 UPDATE。
- committed CognitionEpisode 的下游传播只走 `cognition_episode_committed`：payload 只能引用 canonical revision/event/episode ID 和 schema/hash，禁止塞入 Markdown、让 consumer 调 LLM 重猜认知，或恢复 `knowledge_distilled` 后的同步 KG/CG 双写。固定消费者只有 `wiki/knowledge_graph/cognitive_graph`；它们必须使用同一显式 config/database scope，并返回带 effect/manifest/before/after/ACL hash 的 typed `HandlerOutcome`。
- EventBus 对 `cognition_episode_committed` 使用跨进程 lease、续租、fencing 和 event+consumer 唯一 terminal；该事件不进入普通历史事件清理。修改 dispatch、EventBus lease、EvidenceGraph direction 或 projection schema 后，必须运行 `python3 scripts/audit_cognitive_event_dispatch.py --strict --json`、`python3 scripts/audit_evidence_graph_direction.py --strict --json`、相关 unit/integration、Quick 和 local gates。旧库先停止 daemon/MCP writer，执行 `python3 scripts/reconcile_cognition_episode_projections.py --json`；审阅 inventory hash 与 backup target 后才允许 `--apply --expected-inventory-hash <hash> --backup-dir <dir> --json`，随后 second dry-run 和逐库 `PRAGMA integrity_check`。
- COG-015 起，认知检索必须以 `CognitiveSearchHit` 返回 channel/object/revision、match-offset snippet、matched field、source revision/span 和 ACL decision。Wiki、canonical cognition、CognitiveGraph、EvidenceGraph 各自召回并在授权后 oversample，再融合和取 top-k；禁止先取全局 top-N 再 ACL 过滤。应用层暴露 typed cognition 前必须按 channel + object + current revision 再授权。canonical state 的候选阶段只能读取 `mnemos.cognitive_search_state_headers.v4` 小型 ACL header/binding；binding insert 必须校验 canonical revision ACL preimage、payload hash 和 identity，正文只能在授权后加载。修改该链路必须运行 hermetic `python3 scripts/audit_cognitive_search.py --strict --json`；生产 Wiki/三类 store 另运行 `python3 scripts/audit_cognitive_search.py --production --strict --json`，真实 channel population 为 0 时 gate 必须保持非零退出，不能由 benchmark 或历史 exclusion 冒充 live traffic。旧 Wiki ACL 只能在 daemon/writer 停止、明确 target 且提供新/空 backup dir 后通过 `scripts/reconcile_access_metadata.py --apply` 修复，未知来源必须 `restricted_unknown`，不能默认为 public；Wiki 文件、lifecycle mutation 和 durable pending event 必须作为同一批次提交，任一失败时 Markdown 与两份 SQLite 都恢复到精确 preimage。无法证明 ACL 的历史对象还必须由 `scripts/reconcile_cognitive_search_exclusions.py` 的 reviewed inventory/object hashes 写入 exact append-only exclusion；state header 使用 `scripts/reconcile_cognitive_search_state_headers.py` 完成 dry-run、backup apply、second dry-run 和 integrity check；没有这些 disposition 时 production strict audit 必须保持失败。所有 apply 路径必须持有 offline migration writer lock，daemon/MCP runtime writer 则持有 shared lock。
- `CognitiveStateStore`、`ProducerConsumerLedger` 和 `ActionLedger` 普通构造器不得建表或迁移。新环境只在显式 bootstrap 写入口初始化；旧库先 dry-run，停 daemon 并备份后运行 `scripts/reconcile_cognitive_state_store.py` 与 `scripts/reconcile_action_ledger.py --apply`，再 second dry-run 和 strict audit。
- ActionLedger 只记录运维/动作证据。状态推进追加新 action ID 并引用前一条；相同 ID 只允许字节语义相同的 replay。持久化前仅窄脱敏个人标识、API key/令牌、银行卡、密码/私钥，普通用户内容保持原样，不增加加密。

## Wiki 投影成功判定

- Wiki 文件存在不等于 KG/search/metrics 已完成。create/update/move/delete 必须有 `WikiProjectionLedger` mutation，并由六个 required consumers 分别写 `ack/noop` receipt；`retry/defer/dead` 和缺失 receipt 都是未闭合。
- 消费者业务失败必须返回 `HandlerOutcome.retry()` 或 `.dead()`；等待 trusted-push decision 返回 `.defer()`。不得通过返回 `False`、`status=error`、日志 warning 或吞异常让 EventBus 误记 done。
- rename/delete 使用同一 stable `page_id`、`parent_revision` 与 tombstone；前序 revision 未完成时后序 mutation 必须 defer。重复投递必须由 terminal receipt 幂等跳过。
- 自定义 Wiki/测试环境必须显式贯穿 `wiki_dir`、`database_dir` 和 `embedding_index_dir`。pytest 会内容哈希生产 KG/metrics/Wiki ledger、WAL 与 ANN/meta，并拒绝可写生产 SQLite 连接。
- 修复或迁移存量前先停止 daemon 并备份；`python3 scripts/rebuild_wiki_projection_state.py --json` 只预览，只有显式 `--apply` 才重建。验收覆盖 `tests/integration/test_wiki_projection_lifecycle_e2e.py`、`tests/unit/test_index_manager.py`、`tests/unit/test_mnemos_bus.py` 和完整 Quick/local gates。

---

## 工具速查表

CI system test 的 canonical 入口是 `python3 scripts/run_tests.py system`。Linux、macOS、Windows matrix 必须共用该 OS-neutral hermetic runner，由 Python `tempfile` 创建沙箱并以 argv 启动 pytest；禁止在 workflow 中重新内联 POSIX env prefix、`mktemp` 或其他 shell 专属替代路径。

| 场景 | 调用的工具 | 说明 |
|------|-----------|------|
| 用户说"记住这个" | `knowledge_ingest` | 将口述知识写入 Wiki |
| 用户说"把这个文件存进知识库" | `document_process` | 默认 `mode=distill`：trusted_user_document → canonical raw → capture outbox → Amphora → 质量门 → Wiki；入口返回 `accepted`/`pending`，不得因 `wiki_paths=[]` 判失败；结果须带 stable asset、raw revision、handoff/projection 与来源字段 |
| 用户说"解析这个 PDF" | `document_process` | `mode=parse` 仅预览；未显式要求预览时不要用 parse-only |
| 会话进行中，每轮对话结束 | `capture_turn` | 逐轮上报对话（低延迟入队） |
| 会话结束 | `end_session` | 标记 session 完成 |
| 批量上报历史对话 | `capture_session` | 一次性上报整个 session |
| 用户问"原话/证据/聊天记录" | `session_search` | 搜索 raw 历史会话 |
| 用户问"上次怎么解决" | `session_search` + `context_aware_search` | 关联 raw 证据和沉淀知识 |
| 开始新任务（coding/debugging/design） | `preflight_inject` | 装载历史经验教训；画像 SQLite 暂时占锁时按 2 秒画像库连接预算返回成功降级响应和 `degraded_reason` |
| 执行中检测到风险模式 | `guard_check` | 实时守护检查；预加载器不可用时回退默认守护清单，`guard_alert` 遥测事件遇到 EventBus/SQLite 锁时降级为 warning，不返回 MCP 工具错误 |
| 需要了解用户偏好 | `persona_summary` | 获取三层画像和 `user_cognitive_profile_v2`；画像库暂不可用时走空画像降级 |
| 需要调整 AI 行为风格 | `persona_behavior_prompt` | 获取行为提示词并记录画像消费；画像库暂不可用时不阻断主响应 |
| 需要更新画像 | `persona_update` | 触发画像重新计算 |
| 系统异常/检查状态 | `health_check` | 健康检查 |
| 验收宿主运行能力 | `health_check` → `agent_runtime_probe` | 认证宿主先取得 canonical check-set hash，再提交固定 synthetic-safe completeness 样本；只写元数据回执，不写样本文本 |
| 整理最近的知识 | `wiki_build` | 触发 Wiki 构建 |
| 搜索已有知识 | `wiki_search` | 知识库搜索 |
| 上下文感知精准搜索 | `context_aware_search` | 用户认知画像 v2 加权 + 知识图谱召回；返回 `query_trace`/`degraded` 证明 embedding/reranker 调用或降级 |
| 读取具体页面 | `wiki_read` | 读取 Wiki 页面 |
| 写入蒸馏结果 | `wiki_write` | 写入 Wiki 页面 |
| 意图路由分类 | `intent_route` | 返回 intent、data_source、route_tools、fallback_tools 和解释 |
| 盲区检测 | `blindspot_check` | 检查知识库覆盖缺口；同一会话内同一 topic 只提醒一次，用户确认后搜索并继续对话 |
| 知识新鲜度 | `freshness_check` | 版本绑定 + 过时预警；MCP 入口是纯读检查，刷新必须走另一个具备写权限的维护工作流 |
| 认知反射 | `reflect_on_input` | 基于用户输入触发 L4 Reflection；默认自动调用 LLM 生成洞察 |
| 手动认知反射 | `reflect_manually` | 手动触发通用 Reflection；默认自动调用 LLM 生成洞察 |
| 反射反馈 | `reflection_feedback` | 对 Reflection 洞察提交准确/不准/有启发/无关反馈 |
| 预测性推送 | `predictive_push` | 基于上下文主动推荐；ACL 授权在正文读取、冷却和 delivery/history 记录前完成，调用方不能跳过历史记录 |
| 查看知识来源 | `knowledge_source_list` | 查看来源分布统计 |
| 查看复盘经验 | `retrospective_list` | 列出 retrospective 经验（返回 path/title/task_type/subtype/version） |
| 结构化复盘 | `recap_start` / `recap_submit` / `recap_finalize` | 三问一确认，写入 `06-Retrospectives/复盘/` 并生成消费计划 |
| 跳过/纠偏复盘 | `recap_skip` / `recap_feedback` / `recap_status` / `recap_claim_owner` | 记录跳过原因、反馈、状态和多 Agent owner 锁 |
| 采集信号 | `signal_collect` | 触发信号采集 |

MCP handler 只能使用 stdio 启动时从 keyring reference 解析的服务端 `PrincipalEnvelope`。所有 51 个工具必须存在于 `MCP_TOOL_POLICIES`，每次调用重验 capability 撤销/过期状态；tool schema 不得重新暴露 `agent/source_agent/allow_cross_agent/authorized_agents`。`session_id/project` 只允许收窄服务端 grant，跨 Agent 来源为空时表示无授权。涉及搜索、direct read、preflight、guard、retrospective、freshness 或 predictive push 的候选，必须先验证完整 ACL envelope，再读取正文或记录热度、训练、画像、搜索会话、点击、冷却、delivery/history。修改该边界时至少运行 `tests/unit/test_mcp_*`、`tests/integration/test_mcp_authorization_boundary.py`、`tests/integration/test_auth_before_side_effects.py`、`tests/integration/test_wiki_read_authorization.py` 和真实 stdio 双主体探针。

功能承诺验收以 `docs/acceptance/function_matrix.json` 为准；修改 CLI/MCP 入口或文档承诺后，运行 `python3 scripts/audit_function_matrix.py`，确保声明的入口、代码路径、文档路径和验收命令仍一致。

系统级统一契约以 `core/system_contracts.py`、`core/module_toggles.py`、`core/migrations/registry.py`、`core/backup/snapshot_manager.py`、`core/privacy/data_ownership.py`、`core/privacy/secret_inventory.py`、`core/ops/sqlite_disk_budget.py`、`core/benchmarks/golden.py`、`core/setup/install_lifecycle.py` 和 `core/kia/adaptive_policy_matrix.py` 为准。凡修改认知资产、质量门、能力发现、隐私保留、状态/错误、自动动作、领域语言、满分评分口径、模块开关、冷启动模块产物、迁移、备份恢复、数据所有权、配置 secret inventory、SQLite 磁盘预算、认知质量基准、配置/secret 验收、安装/升级/卸载入口或自适应策略覆盖，必须同步更新对应 registry/manifest/contract，并运行：
`python3 scripts/audit_cognitive_asset_schema.py --strict`、`python3 scripts/audit_quality_decision_contract.py --strict`、`python3 scripts/audit_capability_registry.py --strict`、`python3 scripts/audit_privacy_retention_policy.py --strict`、`python3 scripts/audit_lifecycle_status_contract.py --strict`、`python3 scripts/audit_action_ledger.py --strict`、`python3 scripts/audit_domain_glossary.py --strict`、`python3 scripts/audit_mnemos_scorecard.py --strict`、`python3 scripts/audit_wiki_quality_contract.py --strict`、`python3 scripts/audit_adaptive_policy_matrix.py --strict`、`python3 scripts/audit_module_toggle_registry.py --strict`、`python3 scripts/audit_cold_start_toggle_matrix.py --strict`、`python3 scripts/audit_toggle_auto_disable_policy.py --strict`、`python3 scripts/audit_toggle_output_consumers.py --strict`、`python3 scripts/audit_data_interface_registry.py --strict`、`python3 scripts/audit_runtime_producer_consumer_closure.py --strict`、`python3 scripts/audit_migration_registry.py --strict`、`python3 scripts/audit_backup_recovery_contract.py --strict`、`python3 scripts/audit_data_ownership_contract.py --strict`、`python3 scripts/audit_golden_benchmark_contract.py --strict`、`python3 scripts/audit_install_upgrade_contract.py --strict`。配置/secret/磁盘预算改动还必须运行 `python3 mnemos_cli.py doctor config --strict --json`、`python3 mnemos_cli.py secrets doctor --json`、`python3 mnemos_cli.py health --json` 和 `python3 scripts/repair_sqlite_disk_budget.py --dry-run`，确认 `mnemos.config_audit.v1` 产物里的 `security.secret_inventory` 为 ok、`plaintext_count=0`、不含明文 key/token/secret、真实 API URL、本机路径或未脱敏 key source；`mnemos.keyring_doctor.v1` 必须证明 `secret_inventory_plaintext_count=0`，keyring 不可用时只能在 `security.accept_env_secret_fallback=true` 后把 env fallback 标为 accepted；`checks.sqlite_disk_budget` 必须输出 `mnemos.sqlite_disk_budget.v1`，覆盖 `.db-wal`、Mnemos temp、snapshot 与 `raw_events.db` 体积/增长率。WAL checkpoint 和过期 Mnemos temp 删除可用 `scripts/repair_sqlite_disk_budget.py --apply --wal --temp`，snapshot/raw_events 删除必须人工确认。`QualityGateDecision.as_unified_decision()` 已把局部质量门映射为统一 `QualityDecision`；`CognitiveValueGate` 必须在普通质量门后判断认知贡献，并把 `cognitive_value_*`、`cognitive_contribution_types`、`cognitive_consumers`、`quality_gate_action_ledger_ref` 写入正式 Wiki frontmatter，同时将最终门禁结果写入 `ActionLedger(action_type=quality_gate)`；自适应策略必须在 `mnemos.adaptive_policy_coverage.v1` 中声明输入信号、可调参数、读取入口、回滚指标和验收指标，并保持 `docs/acceptance/adaptive_policy_matrix.json` 与代码一致；`scripts/wiki_lint.py --summary --json --budget` 输出 `mnemos.wiki_quality.v1`，把 missing_meta/orphan/broken_link/stub 映射到统一生命周期和 `obsidian_experience` scorecard，`--fix` 写 Wiki frontmatter 时必须产生 `wiki_quality_fix` ActionLedger 记录；`ActionLedger` 是后续自动修复、配置变更、文档导入、模块开关、产物消费、迁移、快照恢复、数据所有权、golden benchmark 消费验证、install lifecycle apply 和 Wiki 质量修复动作的全局账本 facade。默认关闭、隐私关闭、成本关闭、legacy/stale 开关不能绕过 `mnemos doctor modules --json` 中的默认原因、自动开启策略、消费方、效果指标和回滚策略；数据删除 apply 不能绕过 freeze、snapshot ref、确认和删除证明；`setup.sh`/`setup.bat`/`scripts/auto_setup.py` 是兼容入口，新安装/升级/卸载自动化应优先走 `mnemos setup`、`mnemos upgrade`、`mnemos uninstall` 和 `mnemos doctor repair-all`。

Golden benchmark 以 `benchmarks/golden/manifest.json` 和 baseline scorecard 为准。修改 prompt、schema、router、scorer、quality gate、cognitive value gate、preflight/search/persona 消费链路或满分评分口径后，运行 `python3 scripts/run_golden_benchmark.py --strict --mock-llm`；该入口不联网、不调用真实 LLM，不读取当前用户个人数据，只用固定 synthetic 样本证明认知资产、质量门拒绝、用户画像增量、搜索排序、preflight 命中、ActionLedger 消费和 `mnemos_benchmark_scorecard.json` 趋势没有退化。

认知系统就绪度以 `python3 scripts/audit_cognitive_readiness.py --json --budget` 或 `python3 mnemos_cli.py doctor --cognitive-readiness --json` 为准；修改 raw/Wiki/KG/CognitiveGraph/recap/reminder/search click/open/ignore/no_result/delivery/outcome/observation/reflection/policy patch/consolidation 链路后，必须复查来源、证据、消费者、行为四段报告和 `mnemos.learning_signal.v2`，确认 raw/feedback/search/reflection 正在转化为 observation、policy patch/no_patch 证据和 consolidation run，且 `cognitive_assets` scorecard 没有退化。修改 Wiki source readiness 时，不要在 readiness 审计里临时读取正文来绕过预算；应让 `WikiMetrics.scan_all_pages()` 通过 `core/wiki_page_roles.py` 写入 `page_metrics.page_role`，并确认报告里的 `source_required_total`、`source_exempt_total`、`source_exempt_reasons`、`stale_metric_rows` 合理。修改 search ignore detection 必须同时写 scoring signal 和原 `search_sessions.ignored_at/outcome_status/outcome_at`；`ContextAwareSearch(wiki_base=...)` 测试或自定义 Wiki 不得污染全局搜索会话 DB。修改蒸馏 JSON 解析、LLM response fallback、格式失败复盘或 distill metrics 时，还必须运行 `python3 -m pytest tests/unit/test_distillation_json_metrics.py tests/unit/test_distillation_llm.py tests/unit/test_distillation_failure_cleanup.py -q`，确认 fallback 成功不写 warning、`checks.distill_json_quality` 有 direct/fallback/fixed/failed 统计、同类格式失败按错误指纹合并。需要把当前缺口入账时显式运行 `python3 scripts/audit_cognitive_readiness.py --budget --record-gaps`，该入口会写 `cognitive_readiness_gap` ActionLedger。

`mnemos.cognitive_readiness.v2` 禁止以 global non-zero count 清 gap：required table 缺失/不可读/旧 schema 为 blocked，required evidence 或 lineage 的 0/0 为 unobserved。只接受 visible delivery→explicit/reciprocal outcome、raw/current revision→observation、driver→patch/no_patch、candidate→applied consolidation 的 exact coverage；dry-run 不算 applied。任何改动至少覆盖 missing/corrupt/old schema、initialized-empty、stale/invalid time、1/N、unlinked outcome、dry-run 与完整 100/100 golden fixture。distill receipt 必须传 task id/input revision 以绑定 exact producer generation；同 session 多 generation 不得使用 latest fallback。

daemon heartbeat、security health、SQLite disk budget health、wiki route health、runtime producer/consumer health、install lifecycle health、multimodal optional health、auto-healing 与 strict health 以 `core/ops/health_check.py`、`core/setup/install_lifecycle.py`、`core/ops/sqlite_disk_budget.py`、`core/ops/producer_consumer_ledger.py`、`core/ops/runtime_flow_health.py`、`core/ops/runtime_flow_telemetry.py`、`core/ops/auto_healing.py`、`core/ops/keyring_doctor.py`、`scripts/health_check.py`、`core/privacy/secret_inventory.py`、`core/config.py`、`daemon/heartbeat.py`、`daemon/intervals.py`、`daemon/service_registry.py`、`daemon/wiki_route.py` 为准；修改 service error、raw projection、Wiki routing、runtime producer/consumer ledger、install lifecycle、heartbeat JSON、Amphora 队列、recap/reminder 队列、安全权限/keyring/env fallback/secret inventory、SQLite 磁盘预算、诊断脱敏、可选多模态状态或 health reducer 时，必须区分 `active_service_errors` 与 `historical_service_errors`，并保持顶层 `status=ok/warning/degraded/failed`、`usable`、`strict_ok`、`strict_failures` 语义一致；默认 doctor 文本、JSON 报告、`mnemos_cli.py distill status` 和 `scripts/e2e_probe.py --dry-run --no-api` 不得暴露真实 `base_url`、本机绝对路径或 `env:`/`keyring:`/`keyref:` 明细，原值只允许通过 `--unsafe-debug` / `--show-paths` 本机排错。`checks.runtime_producer_consumer` 是只读 strict check：`docs/acceptance/adaptive_data_flows.json` 的 flow 必须由显式 bootstrap 注册到 `producer_consumer_ledger.db`；`mnemos.runtime_producer_consumer.v2` 以不可变 producer event、generation、intended consumer 和 append-only receipt 核对 event × intended-consumer coverage、pending、freshness 与 dead letter。required flow 的 0/0 必须 degraded 为 `unobserved`，事件触发型 flow 无事件时才允许 N/A；缺库/旧 schema 必须 blocked，health 不得建表、迁移、注册、seed 或 replay。异步 flow 只能用 `receipt_grace_seconds` 描述真实终态 SLA：窗口内状态为 `in_flight`，超时进入 `overdue_pending` 和 missing consumer；默认 0，不能改大 pending budget 假绿。初始化、v1 迁移与 `0600` durable outbox 的有序重放统一执行 `python3 scripts/bootstrap_runtime_producer_consumer_ledger.py`；orphan outputs、no-source consumers、item mismatches、extra consumers、stale continuous flow 或 dead letters 超预算必须 degraded，并进入 `data_pipeline` scorecard。`checks.install_lifecycle` 是 strict check：`installed_partial` 或 required step 未完成必须 degraded，输出 `incomplete_required_steps`、repair actions 和 `install_lifecycle_state` 错误；health 可消费真实 `mnemos setup` 写入 `ActionLedger(action_type=install_setup)` 的 verified `installed_ready` 证据，但不能用旧状态掩盖当前配置、Vault 或必填模型端点 blocked；默认 `scripts/verify_installation.py --json` 只能作为 basic 验证，完整安装验收必须跑 `--full --json` 并确认 `full_verification_ok=true`。`checks.multimodal` 是非 strict 可选面，未配置只能显示 `skipped` 和恢复动作，不能进入 `strict_failures`；配置后显示 `endpoint_status=configured`，真实联网 smoke 由 `scripts/verify_installation.py --api-smoke` 报告 configured/available/unreachable。`checks.auto_healing` 必须为每个非 ok health check 提供 `auto_heal_state` 和 `auto_heal.user_intervention_budget`，状态只能是 `auto_fixed`、`auto_fix_failed`、`needs_user`、`ignored_with_reason` 或 `blocked`；真正 apply 的低风险 handler 必须写 `ActionLedger(action_type=auto_heal)`、`rollback_ref` 和 verification 证据。`checks.security` 是非 strict warning 面：敏感目录/配置权限违规必须给出 repair action，`secret_inventory.plaintext_count>0` 必须只展示字段路径/长度并触发 `scripts/security_audit.py --strict` 失败，`keyring_available=false` 必须带 `keyring_error`、`keyring_status`、`keyring_risk_level`、`safe_but_not_best`、env fallback 接受状态和修复建议，不应静默丢在脚本报告里。`checks.sqlite_disk_budget` 是 strict check：`.db-wal`、Mnemos temp、snapshot 或 `raw_events.db` 超预算必须 degraded；WAL checkpoint 和过期 Mnemos temp 是安全修复，snapshot/raw_events 删除必须人工确认。`checks.wiki_route` 是 strict check：`inbox_ready_to_classify`、`needs_review_pages`、正式区 source-prefixed 页或标题/basename 冲突超过预算时必须 degraded，daemon `wiki_route` 服务最近一次 classified/moved/review 必须可通过 heartbeat 反查。`raw_projection` 从 `database is locked` 等错误恢复后应清除旧错误状态并写 `raw_projection_recovered` ActionLedger；Amphora `failed>0`、runtime producer/consumer 闭环预算超线、Wiki 路由预算超线、install lifecycle partial、SQLite 磁盘预算超线、distill failed 超预算、high/critical recap pending 超预算或 dialog reminder pending/active 超预算必须让 strict health 降级。回归测试至少运行 `python3 -m pytest tests/unit/test_auto_healing_orchestrator.py tests/integration/test_auto_healing_closed_loop.py tests/unit/test_daemon_heartbeat.py tests/unit/test_daemon_service_state.py tests/unit/test_health_check_heartbeat.py tests/unit/test_sqlite_disk_budget.py tests/unit/test_mnemos_daemon.py::TestRawProjectionService tests/unit/test_mnemos_daemon.py::TestWikiRouteService tests/unit/test_action_ledger_contract.py -q`、`python3 -m pytest tests/unit/test_health_check.py tests/unit/test_health_check_report.py tests/unit/test_config.py tests/unit/vaults/test_content_audit.py -q`、`python3 -m pytest tests/unit/test_runtime_producer_consumer_ledger.py tests/unit/test_adaptive_data_flows.py tests/unit/test_mnemos_scorecard_contract.py -q`、`python3 -m pytest tests/unit/test_runtime_flow_telemetry.py tests/integration/test_runtime_ledger_real_pipeline_e2e.py -q`、`python3 -m pytest tests/unit/test_capture_worker.py tests/unit/test_capture_service.py tests/integration/test_capture_distill_receipts.py -q`、`python3 -m pytest tests/unit/test_daemon_intervals.py tests/unit/test_daemon_service_registry.py tests/unit/test_amphora.py tests/unit/test_cli_recap.py tests/unit/test_cli_reminder.py tests/unit/test_dialog_reminder.py -q`、`python3 scripts/audit_runtime_producer_consumer_closure.py --strict`、`python3 mnemos_cli.py secrets doctor --json` 和 `python3 mnemos_cli.py health --json`。测试必须显式注入临时 `database_dir`/receipt config，完整 Quick 前后生产 ledger 哈希必须一致。

Raw projection 是 canonical Raw 的可验证展示，不是截断 preview：`raw_projection.max_turn_chars` 必须为 `0`，每个 revision 的 user/assistant/reasoning/structured 字段都通过 event/field byte hash 可逆审计。`scripts/project_raw_vault.py --apply` 只能原子写受影响 chunk 和其 `RawIndex` 项，`unrelated_files_moved` 必须为 `0`；`scripts/audit_raw_projection_fidelity.py --strict --json` 读取 canonical DB 的只读连接做字段对账。历史 `raw-vault-projection-*` 只能由 `scripts/audit_raw_projection_backups.py --json` 清单；`storage.disk_budget.raw_projection_backup_total_max_mb` 超预算只产生人工保留告警，删除必须有单独、明确的授权。

健康修复完成前后都要做真实运行态复验：daemon PID 和 heartbeat、`mnemos_cli.py distill status` 的 pending/processing/failed、daemon CPU、日志错误、`scripts/verify_installation.py --json` 的 basic 验证、`scripts/verify_installation.py --full --json` 的完整验证和 `scripts/e2e_probe.py --dry-run --no-api` 都是验收面。pytest 不得写用户真实 Amphora 队列；全局测试 fixture 会把 `core.kia.amphora._DB_PATH` 指向临时 DB，新增测试如果需要队列也必须保持隔离。

2026-07-06 运行态复审新增一条硬规则：Mnemos 本地 SQLite 不做整库加密；读写 SQLite 的诊断类对象不能在 doctor/verify/health 路径留下长连接。`core/wiki_metrics.py::WikiMetrics` 已提供 `close()`、context manager 和 transient connection 释放边界；直接调用 `_get_conn()` 的 CLI/doctor helper 必须在 `finally` 中关闭。诊断输出只需对用户个人隐私、API key、银行卡和密码等敏感字段做脱敏，不引入额外加密层。`mnemos doctor` 的 Wiki 质量提示必须读取 health 的 `wiki_route_budgets`，不能把预算内 `needs_review_pages` 误判为 warning；`scripts/verify_installation.py` 的 doctor 子进程等待预算为 `DOCTOR_TIMEOUT_SECONDS=60`。修改这些路径后，除目标单测外必须至少复跑 `mnemos_cli.py doctor`、`scripts/verify_installation.py --json`、`scripts/e2e_probe.py --dry-run --no-api` 和 `scripts/run_local_gates.py`。

证据回链修复以 `python3 scripts/backfill_wiki_evidence.py --json` 或 `python3 mnemos_cli.py distill evidence-backfill --json` 为准；默认 dry-run，只有显式 `--apply` 才写 `page_metrics`、Wiki frontmatter 和 `99-Reports/认知数据就绪度/`。默认只把 `anti_pattern_quote`、`distill_extraction` 作为强 relation evidence 计入 source refs；页面已有 `来源事件ID`、`来源会话/source_session*`、`evidence_refs` 或带蒸馏上下文的 `来源/source_agent` 可以作为 frontmatter provenance 回填，不能给无 provenance 页面生成虚假 refs。调整证据类型必须同步说明 `evidence_backfill.relation_evidence_types` 的信任边界。

蒸馏 action 路由以 `core/hephaestus/distill_action_router.py` 为唯一入口；不要在 `DistillationEngine.write_pages()` 或 Worker 中绕过它手写 merge/update/dispute/reinforce。正式 action 只能消费与不可变 `DistillInputSpec` 一致、已通过 `distill_output_v4` canonical root admission 的结果；`create_page` 还必须有 Engine 在受控 merge/link/quality 末段签发的 `FragmentRouteCapability(root_hash,input_spec_hash,object_refs)`，且路由参数只能是它的有序、无重复对象身份子集。单独 fragments、准入后替换 `result.fragments`、模型猜测的 source agent、缺失 capability 或 root/input hash 漂移都不能写页。问题 22 起，高价值 claim 的 `cognitive_actions` 必须落到 `cognitive_action_log` 和 `mnemos.distill_cognitive_action.v1` artifact，普通技术事实没有动作时页面必须标记 `ordinary_knowledge`。修改 `distill_output_v4` action 行为后，必须运行 `python3 -m pytest tests/unit/test_distill_cognitive_actions.py tests/unit/test_distill_action_router.py tests/unit/test_distillation_contract.py tests/unit/test_distillation_engine.py tests/unit/test_health_check_heartbeat.py::test_distill_cognitive_actions_health_reports_counts -q`、`python3 scripts/audit_distill_output_contract.py --strict --json`，并用 `mnemos distill actions --json` 与 `mnemos health --json` 的 `checks.distill_cognitive_actions` 确认 action log 和 cognitive action counts 可只读反查。

蒸馏模型响应必须经 `core.hephaestus.distill_response.DistillBackendResponse` 穿过 backend/extractor 边界，禁止恢复只返回 parsed dict 的正式端口。结构错误必须在 Extractor 内进入有界 correction，Engine 只做同合同复验；最终失败 artifact 必须有原始响应或明确 transport-empty 证据，并带 prompt/spec/response hash 和完整调用元数据。daemon 只允许一个 `process_all()` active owner，禁止恢复 `collect_completed`、外部 output-dir 弱 validator、parser 不可用放行或 raw Markdown fallback。修改这些路径后至少运行 `tests/unit/test_distillation_llm.py`、`tests/unit/test_distillation_engine.py`、`tests/unit/test_hephaestus_worker.py`、`tests/static/test_distill_entrypoint_ownership.py` 和 `scripts/audit_distill_output_contract.py --strict --json`。

蒸馏/文档蒸馏写页路由以 `core/vaults/page_routing.py` + `core.kia.charon.resolve_page_folder()` 为准；不要把新页面默认写死到 `00-Inbox`。可确定分类的 fragment 应直接写正式目录，无法分类或正式区 basename 冲突才留 Inbox，并写 `Wiki路由状态/原因/目标` frontmatter。daemon `wiki_route` 只能跑 route-only connect：调用 Charon 时传 `write_relations=False`，避免周期服务写 KG cooccurrence 关系或触发 embedding-heavy 图谱构建；完整图谱关系应由手工 connect 或显式重型调度承担。修改 `DistillationEngine.write_pages()`、`DocumentDistillationPipeline.write_to_wiki()`、Charon 分类、Vault 审计或表达格式化默认值后，至少运行 `python3 -m pytest tests/unit/test_distillation_engine.py tests/unit/test_document_pipeline.py tests/unit/vaults/test_content_audit.py tests/unit/test_health_check_heartbeat.py::test_wiki_route_health_degrades_when_budgets_are_exceeded -q`，并用 `python3 scripts/reorganize_wiki.py --dry-run` 或 `python3 mnemos_cli.py vaults audit-content --json` 复核真实 Vault。

KG 端点归一化与路径迁移以 `core/kia/kg_endpoint_normalizer.py`、`core/kia/relation_endpoint_quality.py` 和 `mnemos kg normalize-endpoints` 为准。默认只读 dry-run；`--apply` 只能执行唯一 basename 命中的旧路径迁移和多引用概念实体补齐，并在 `~/.mnemos/backups/kg-endpoints/` 创建定向备份。`--prune-invalid` 是显式删除开关，只能删除 marker、多行片段、附件、Shadow/Relations 投影和短中文半句等 prunable endpoint 对应的 relation，并同步清理 evidence/FTS/embedding；旧 Inbox、`L2.4-KG/Entities`、hash/session 标题、单引用概念和弱概念必须保留为 unresolved，不能为了清零 `endpoint_gaps` 删除或强行建实体。所有生产 relation 写入必须复用 `relation_writer.upsert_relation_row()`，不得直接 `INSERT INTO relations`；自动关系发现必须跳过 `07-Shadow`、`L2.4-KG/Relations`、`99-Reports`、`99-Archive` 和 entropy suggestion 等派生产物；查询 relation 来源字段只能使用 `source_method`，不能引用旧的 `method`。修改 KG consistency、endpoint gap、端点质量闸门、FTS 重写、entity 补齐、RelationManager 写入、KG 扫描范围或 CLI 路由后，必须运行 `python3 -m pytest tests/unit/test_relation_manager.py tests/unit/test_kg_endpoint_normalizer.py tests/unit/test_kg_consistency.py tests/unit/test_knowledge_graph.py tests/unit/test_kg_integration.py tests/unit/test_kg_event_handler.py tests/unit/test_kg_exporter.py tests/integration/test_kg_search_loop.py -q`、`python3 scripts/audit_kg_relation_contract.py`、`python3 mnemos_cli.py kg normalize-endpoints --json`、`python3 mnemos_cli.py kg consistency --json` 和 `python3 scripts/run_local_gates.py`。

`relation_evidence` 表结构改动必须经过 `core/kia/relation_evidence_schema.py`；该模块是 columns/defaults/FK/index、`mnemos.relation_evidence_schema.v1` 与 canonical DDL hash 的唯一 owner。构造器只允许为全新 DB 创建表；已存在表必须先只读验证，未注册旧 schema、RelationManager defaults schema、索引/hash/registry 漂移或未知结构都要在其他 DDL 前阻断。先运行 `python3 scripts/reconcile_relation_evidence_schema.py --json`；显式 apply 前停止 daemon，并提供 `--backup-dir`，不得给 NULL/blank `evidence_type` 猜值。验收必须运行 `python3 scripts/audit_schema_registry.py --strict --json`、`tests/unit/test_relation_evidence_schema_migration.py`、两种构造器顺序和完整 Quick。

认知压缩以 `core/cognitive/consolidator.py` 和 `python3 scripts/plan_cognitive_consolidation.py --json` 为准；禁止在该链路删除 Raw、让 Wiki/KG 候选自动物理删除，或以任一 evidence ref 覆盖整批候选。`--apply` 只冻结每个候选的 revision/content hash；`--submit-run` 必须走 trusted-push proposal，人工批准后 `--reconcile-run` 才能在页面 hash、每个 `raw-revision:<revision>:<hash>`、可信提交和六类 projection receipt 均通过时写 receipt-backed coverage。daemon 只重试已绑定 proposal 的核验，不能自动批准。Raw 删除另走 DataOwnership 工作流。修改该链路后必须运行 `python3 -m pytest tests/unit/test_cognitive_consolidator.py tests/unit/test_daemon_consolidation_service.py tests/unit/test_cognitive_readiness_audit.py -q`，并确认普通 dry-run 不创建 `~/.mnemos/cognitive_consolidation.db`。

信任评分与投递闸门以 `core/cognitive/trust_scorer.py` 为准；不要在 merge/update、predictive push、guard 或后续 DeliveryRouter 中各算各的置信度。`push_feedback` 的负反馈包含 ignore/dismiss/inaccurate/outdated，后两者分别映射 contradicted/outdated 证据；同一 `feedback_event_id` 的信任证据必须幂等。修改 `trust.*` 配置、negative evidence、merge gate、delivery gate 或 `push_feedback` 行为后，必须运行 `python3 -m pytest tests/unit/test_knowledge_trust_scorer.py tests/unit/test_feedback_signal_router.py tests/unit/test_distill_action_router.py tests/integration/test_feedback_event_identity_e2e.py tests/integration/test_feedback_outbox_recovery.py -q`。默认配置在 `~/.mnemos/configs/main.json` 的 `trust.*`，阈值和 penalty 不应硬编码在调用方。

策略补丁以 `core/cognitive/policy_patch.py`、`core/kia/policy_patch_adapter.py`、`core/app/retrospective_consumption_router.py` 与 `core/reflection/consumers.py::ReflectionPolicyPatchConsumer` 为准；不要把 L4/L5/recap 经验直接写进宿主 system prompt 或 Agent 本地策略文件。用户确认的 recap 会在 `RetrospectiveConsumptionRouter.route_after_finalize()` 中尝试生成 policy patch，并把 proposed/skipped/error 写入 `recap_consumption_outcomes`；没有明确 trigger 的 recap 只记录 skipped/missing_trigger，不创建全局泛化 patch。Reflection 默认消费者会把高置信 insight/shift 转成 `PolicyPatchStore.propose()` 候选，但生成式 `key_points` 只作为解释元数据，不能成为 trigger；不满足条件时写 `policy_patch_feedback` 的 `no_patch` 证据。`PolicyPatchStore` 只生成 TTL、scope、source、severity、稳定 trigger 和 evidence refs 明确的策略补丁；匹配只能使用当前 task/subtype/context，patch content 永不参与自证，非 global patch 必须与显式 project scope 精确匹配。候选按 task-fit 与当前命中 trigger 排序，再去重并执行 `max_active` 干扰预算；KIA 必须返回 `match_source=current_context`、`matched_triggers`、`task_fit_score`、`dedupe_key`、`interruption_budget_ok`，运行时只由 `preflight_inject` 与 `guard_check` 消费并通过 DeliveryRouter 入账。存量 trigger 对账先运行 `python3 scripts/reconcile_policy_patch_triggers.py --json`，确认后才用 `--apply`；apply 必须先备份且不得编造替代 trigger。`PreFlightInjector` 初始化期如果遇到画像库 SQLite 锁超时，KIA facade 必须降级而不是让 MCP 工具失败：`SignalStore` 默认画像库连接/忙等待预算为 2 秒，`preflight_inject` 返回成功但标记 `degraded_reason`，`guard_check` 使用默认守护清单继续风险判断；`guard_check` 触发的 `guard_alert` 属于可丢弃遥测，当前进程无 EventBus 消费者时不得初始化全局 EventBus 去触碰 `events.db`，事件发布遇到 SQLite 锁时只能 warning 降级；reflection/persona metrics 也必须在画像库暂不可用时降级，不得把锁超时包装成 MCP tool error。默认配置在 `~/.mnemos/configs/main.json` 的 `policy_patch.*`：`enabled`、`db_path`、`ttl_days`、`min_confidence`、`max_active` 都可调，不应在 KIA 入口硬编码。修改策略补丁、preflight/guard 注入、recap/Reflection policy patch 生产或反馈抑制后，必须运行 `python3 -m pytest tests/unit/test_policy_patch_store.py tests/unit/test_kia_policy_patches.py tests/unit/test_retrospective_workflow.py tests/unit/test_kia_delivery_events.py tests/unit/reflection/test_reflection_policy_patch_consumer.py -q`。

知识投递策略以 `core/cognitive/delivery_router.py` 为准；不要让 predictive push、preflight、guard、recap 或 reminder 各自实现次数、冷却和打断强度。预测推送反馈必须携带 exact `delivery_event_id`，由 `core/cognitive/feedback_event.py` 校验服务端 principal/project/session 并创建 append-only event + required consumer receipts；禁止恢复 topic/latest fallback，也禁止在部分 consumer 失败时返回顶层 success。penalty、outcome、adaptive scorer、delivery/trust 投影必须以 `feedback_event_id` 幂等，stale processing lease 只可在幂等投影成立时重领。默认 profile 是 `delivery.preference=balanced`。修改该链路后，必须运行 `python3 -m pytest tests/unit/test_feedback_event_ledger.py tests/unit/test_knowledge_delivery_router.py tests/unit/test_feedback_signal_router.py tests/unit/test_application_hub.py tests/unit/test_outcome_recorder.py tests/unit/test_agora.py tests/integration/test_feedback_event_identity_e2e.py tests/integration/test_feedback_outbox_recovery.py -q`，并执行 `python3 scripts/replay_delivery_decisions.py --json` 确认回放输出。

执行中防分析循环以 `core/kia/aegis.py` 和 `guard.analysis_loop.*` 为准；默认第 2 轮纯分析无行动、或同一文件/工具第 2 次重复读取就触发，配置为 3 时第三次才触发。`guard_check` 告警必须返回 `threshold_source`、`threshold_value`、`current_count`；修改该链路后运行 `python3 -m pytest tests/unit/test_analysis_paralysis_guard.py tests/unit/test_aegis.py -q`、`python3 scripts/verify_config_examples.py --strict` 和 `python3 scripts/audit_cognitive_behavior_scenarios.py`。

受控求证队列以 `core/cognitive/verification_queue.py` 为准；不要让 dispute、blindspot、freshness alert 直接触发后台代码修改或 Wiki 正文刷新。`mnemos verify plan` 和 `mnemos verify run` 默认 dry-run；`run --apply` 只写 `verification_queue.db` 与 data-dir report。每个 verification task 必须带 `evidence_refs` 或 `verification_commands`，后台 Chronos 步骤必须受 `ResourceBudget.can_run("verification_queue")` 约束。修改 verification queue、`verification_queue.*` 配置、`mnemos verify` CLI 或 Chronos 接线后，必须运行 `python3 -m pytest tests/unit/test_verification_queue.py tests/unit/test_resource_budget.py tests/unit/test_chronos.py -q`，并用 `python3 mnemos_cli.py verify plan --json --limit 1` 验证真实 dry-run 不写库。

多模态 evidence 以 `core/evidence/artifact_uri.py`、`core/evidence/artifact_catalog.py`、`core.llm_config.resolve_multimodal_api_config()`、`scripts/setup_model_endpoints.py` 和 Agent Kit `artifact_uri_context` 样本为准。`capture_turn` 负责把完整采集 artifact、reasoning artifact、工具结果和附件归一到 `metadata.artifact_refs`；capture 阶段可以使用 `mnemos-artifact://<agent>/<session>/turn/<turn_number>/<artifact_type>[/<index>]` 定位来源，但进入蒸馏前必须由系统按完整 SHA-256 解析为 content-addressed identity，不能用 `file://` 或本机绝对路径替代。`multimodal` 配置是可选能力，`MNEMOS_MULTIMODAL_API_KEY`、`MNEMOS_MULTIMODAL_BASE_URL`、`MNEMOS_MULTIMODAL_MODEL` 存在时启用；KnowledgeInbox 图片入口配置存在时应调用 OpenAI-compatible vision endpoint 解析 Markdown、写入 storage 并入蒸馏队列，未配置或 API 失败时必须生成 `mnemos.multimodal_image_task.v1` 可恢复任务，并保留人工 Markdown 回灌路径。`distill_output_v4` 的模型 schema 只允许 claim evidence 选择 `artifact_ref_id`；`artifact_uri/artifact_type/artifact_summary/artifact_sha256/artifact_mime_type/artifact_acl` 只能由系统 resolver 写入 canonical root，`source_event_id` 和短 `quote` 仍是必填。Obsidian 只展示 artifact 摘要和链接，不嵌入截图、完整终端输出或测试报告正文。修改 artifact URI/catalog、raw metadata artifact refs、蒸馏 evidence schema/prompt、多模态配置或 Wiki 来源追踪后，必须运行 `python3 -m pytest tests/unit/evidence/test_artifact_uri.py tests/unit/evidence/test_artifact_catalog.py tests/unit/test_distill_artifact_input_spec.py tests/unit/test_distillation_contract.py tests/unit/test_frontmatter_contract.py tests/unit/test_acceptance_contracts.py tests/unit/test_capture_service.py::TestCaptureServiceDedup tests/unit/test_multimodal_config_optional.py tests/integration/test_multimodal_degrades_without_config.py -q`，并执行 `python3 scripts/verify_acceptance_contracts.py` 与 `python3 scripts/audit_distill_output_contract.py --strict --json`。

自适应数据流验收以 `docs/acceptance/adaptive_data_flows.json` 为准；该矩阵顶层 `runtime_audit` 声明 `mnemos.runtime_producer_consumer.v2`、`mnemos.cognitive_data_event.v1`、`mnemos.data_interface_registry.v1`、`producer_consumer_ledger.db`、strict gate、health check 和 scorecard 指标。24 条 flow 中 19 条必须接真实 producer/consumer 边界，5 条只在明确前置条件不成立时允许 N/A；只有持续型 required flow 受无观测和 freshness strict 预算，事件触发型 flow 不得被伪装成持续流。修改 scoring、heat、freshness、entropy、observation、persona、feedback、predictive push、reminder、KG confidence、adaptive config、resource budget、capture/sync/persona/scorer/reflection/distill 数据入口或消费者后，运行 `python3 scripts/audit_adaptive_data_flows.py`、`python3 scripts/audit_data_interface_registry.py --strict` 与 `python3 scripts/audit_runtime_producer_consumer_closure.py --strict`，确保关键数据不是只写不读，且 required unobserved、orphan outputs、no-source consumers、item mismatches、extra consumers、dead letters、pending、freshness、duplicate/derived/reinforcement 对账、consumed-without-event 和 unexplained divergence 均可观测、可解释、可降级。

自适应策略覆盖验收以 `core/kia/adaptive_policy_matrix.py` 和 `docs/acceptance/adaptive_policy_matrix.json` 为准；修改 `AdaptiveConfig.DEFAULT_RULES`、`EffectivePolicy`、daemon metric collection、质量门、认知价值门、蒸馏阈值、raw retention、文档默认大小、意图 fallback、投递预算、trust 阈值或搜索新鲜度参数时，必须运行 `python3 scripts/audit_adaptive_policy_matrix.py --strict`。消费者应通过 active shadow 覆盖当前参数；没有 shadow 时必须保留调用方 config/default，不得让全局默认吞掉测试或嵌入式配置。

认知行为验收以 `docs/acceptance/cognitive_behavior_scenarios.json` 为准；修改 preflight/guard/persona/recall/predictive push/recap/dispute/blindspot 等会改变宿主 Agent 行为的入口后，运行 `python3 scripts/audit_cognitive_behavior_scenarios.py`，确保场景、工具、证据字段、用户解释、反馈/纠偏路径和测试覆盖仍完整。

可靠性/安全/性能/迁移验收以 `docs/acceptance/ops_resilience_matrix.json` 为准；修改 daemon、队列/重试、backfill、DB schema、API timeout/rate limit、密钥/权限、RawIndex/perf、资源预算、迁移脚本或兼容 wrapper 后，运行 `python3 scripts/audit_ops_resilience_matrix.py`，确保对应控制项、可观测入口、恢复/降级策略和测试覆盖仍完整。

配置事实源固定为 `core/config_registry.py::CONFIG_REGISTRY`（schema `mnemos.config_registry.v1`）；`core/config.py::DEFAULT_CONFIG`、env ownership、performance tier、公开 JSON/YAML 示例、alias 和 removed tombstone 都必须从该 registry 闭合。新增、删除或重命名配置键/env var 后，必须运行 `python3 scripts/generate_config_examples.py` 更新 `config/config.example.json`、`config/config.example.yaml` 和 `config/.env.example`，再运行 `python3 scripts/verify_config_examples.py --strict` 与 `python3 scripts/audit_config_registry_closure.py --strict`。生产 caller 不得读取 alias/removed key，也不得用不同的 literal fallback 构造第二套默认值；旧值只允许经 `config.stale_keys.v1` 备份、原子迁移和 ledger 验证。

全局回归与本地工程门禁以 `python3 scripts/run_local_gates.py`、`python3 run_tests.py quick|integration|heavy|full`（等价于 `python3 scripts/run_tests.py <layer>`）和必要时的 `.venv/bin/python -m pytest -q` 为准；`run_local_gates.py` 会优先使用仓库 `.venv/bin/python`，覆盖 flake8、mypy budget、compileall、bare except、maintainability/zombie accepted-debt closure、`scripts/verify_config_examples.py --strict`、tech debt annotations、`scripts/audit_hardcoded_paths.py --strict`、`scripts/audit_docs_freshness.py --strict`、`scripts/audit_desktop_system_map_facts.py`、`scripts/audit_docs_sensitive_info.py --strict`、`scripts/audit_docs_stale_service_keys.py`、`scripts/audit_repo_sensitive_literals.py --strict`、`scripts/audit_release_privacy_security.py --strict`、`scripts/audit_adaptive_policy_matrix.py --strict`、`scripts/audit_distill_response_budget.py`、No Zombie Code Policy、arch dependency graph、CI ratchet、vulture、`scripts/security_audit.py`（bandit + pip-audit + health security）。`scripts/audit_docs_freshness.py --strict` 默认覆盖 AGENTS、CLAUDE、CONTRIBUTING、README、README-en、SECURITY、docs 和可发现的 `~/Desktop/mnemos系统图谱`；需要复验指定正式文档集合时使用 `--paths` 显式列出。`scripts/audit_desktop_system_map_facts.py` 在 Desktop facts 文件存在时要求 `99-代码扫描-facts.json.current_state` 对齐当前 repo commit，并记录成功的 `python3 scripts/run_local_gates.py` 与 `python3 run_tests.py quick` 结果；历史 scan 字段只能当旧证据，不可当当前健康状态。生产代码新增路径默认值时必须走 `get_config().wiki_dir`、`Config.vault_dir()`、`core/setup/vault_layout.py` 或显式 CLI 参数，不能重新引入本机绝对路径、旧 Obsidian wiki 默认或未配置的 Mnemos/raw vault 字面量；Markdown 文档不能新增本机绝对路径、裸 `python` 调脚本、裸 `python` 调模块、裸 `python` 执行内联代码、缺失 repo 相对路径、真实 API endpoint、raw key/JWT、明文 credential 赋值、个人邮箱/手机号/身份证或 PII 赋值，`mnemos config set <key>` 示例必须存在于 `config/config.example.json`，公开配置示例里的 daemon service key 必须使用 canonical `eventbus`，退役服务别名只允许留在迁移实现和测试中，不能进入公开入口文档。源码、测试和文档不能提交完整 provider-shaped fake key、本机 home path 或明文 credential literal；需要 redaction/secret 样例时使用运行时拼接或 `DUMMY_CREDENTIAL_*` 哨兵。RawIndex/ObsidianBackend 改动必须覆盖 `tests/unit/test_raw_search.py`、`tests/unit/test_obsidian_backend_index.py` 和 `tests/unit/test_mnemos_cli.py::TestCmdRawIndex`，legacy backend 的索引库必须跟随当前 vault/chatlog，不得复用全局默认库；蒸馏输出预算四档必须通过 `scripts/audit_distill_response_budget.py` 保持 `6000/8000/12000/16000`。`scripts/check_maintainability_budget.py` 使用 `scripts/maintainability_budget.json` 阻断新增超大生产文件、既有超大文件继续增长、broad `except Exception` 数量反弹、未分类 broad catch 总量反弹，以及 health/queue/sync/install/distill 关键路径重新出现未分类 broad catch；修改超过 1500 行的文件或高密度异常文件时，应优先拆到新模块或收窄异常类型，并在降低预算后用 `--update` 刷新基线。安装链路改动必须额外覆盖 `python3 -m pytest tests/unit/test_auto_setup.py tests/unit/test_auto_setup_required_model_endpoints.py tests/unit/test_install_state_machine.py tests/unit/test_mnemos_cli.py -q`、`python3 mnemos_cli.py setup --dry-run --json` 和 `python3 scripts/auto_setup.py --yes --preserve-config`；必填模型端点失败要在 `InstallLifecycleState.metadata.required_model_endpoints_failed` 可读，交互 smoke 不能无限重试，PEP 668/镜像失败路径必须能回到 repo `.venv`。`scripts/security_audit.py` 自身也会优先选择 repo `.venv`，并把 bandit、pip-audit、health security 固定到同一解释器；满分/CI 加严场景必须运行 `python3 scripts/security_audit.py --strict` 和 `python3 scripts/audit_release_privacy_security.py --strict`，确保 Bandit high/medium、strict config、health security/privacy、公开文档敏感信息、仓库敏感字面量，以及 health/config/`distill status`/E2E dry-run 诊断脱敏都为 release-clean；需要验证当前解释器依赖时加 `--strict-env`/`--no-venv-autodetect`，缺少 dev tools 时应输出安装命令而不是原始 `No module named ...`。配置读取分类调整后必须确认 `scripts/audit_config_reads.py` 无 `unclassified` 项，并通过 `scripts/ci_ratchet.py --closure --strict`；新增或删除 `legacy/向后兼容/兼容旧/旧入口/旧链路/旧格式/已废弃/保留兼容` 函数/类级候选时，必须同步更新 `scripts/zombie_code_baseline.json` 的 owner、callers、remove_when、未过期 `expires_at` 和 telemetry，并通过 `python3 scripts/check_zombie_code_policy.py --closure`。

ROOT-20260710-021 起，local/pre-commit/CI 的 maintainability/zombie `--closure` 是 development profile：精确且未过期的 accepted residual 可通过，但输出必须是 `release_eligible=false`，最终汇总不得称为发布证书。full-score 固定追加 maintainability、zombie、vulture 三个 `--closure --strict --json` gate，只在 residual=0 时认证。broad catch 以 AST fingerprint 精确登记，解析失败、同数量替换、expiry、改善后未收紧 baseline 均失败；普通 `--update` 不能吸收新增/替换风险。Zombie v2 还要求 telemetry；vulture current/baseline 必须为 0/0，非零永远不能 rebaseline。

ROOT-20260710-022 起，文档门禁的分母由 `docs/acceptance/document_asset_manifest.json` 唯一定义。`scripts/audit_document_asset_manifest.py --strict` 自动发现全部 tracked Markdown，并逐项验证 Prompt/schema 的 SHA-256、实际 consumer symbol、loader binding 与 output contract；Desktop `00–10` 必须在同一 evidence 行同时绑定 current-state 和 repo 锚点，`86–98` 必须在头部绑定当前 commit。freshness 与 sensitive 复用同一 tracked Markdown 发现，当前 70/70 repo Markdown、23/23 Prompt/schema、25/25 Desktop asset，exclude=0、unverified=0；full-score 的 `docs.asset_manifest.strict` 使用 `--desktop-mode required`。新增文档或 Prompt 不能通过修改硬编码路径集合、跳过分类或只引用历史 `99` 快照绕过。

安全门禁的机器真相固定为 `mnemos.security_audit.v2`。`python3 scripts/security_audit.py --strict --json` 必须把 Bandit、pip-audit 与 health security 风险归一化为 typed findings，再由 findings 唯一推导 `blocking_count`、`warning_count`、`status`、`ok` 与退出码，强制保持 `ok == (blocking_count == 0)`。`python3 scripts/audit_release_privacy_security.py --strict --json` 必须调用相同 validator 复核 schema、counts、findings、status、`ok` 和返回码，禁止把 errors 非空但 `ok=true`、子命令返回 0 或 warning 丢失当作 release-clean。

修改 raw identity、projection、session search、Capture handoff、Hephaestus provenance 或 retention 时，必须维持 logical alias 与 immutable revision 分层：`upsert_turn()`/`find_event_id()` 对外返回 revision ID，logical ID 只用于 current pointer 和聚合 metrics。搜索必须先用 `list_current_headers()`/`get_revision_header()` 鉴权，再调用 `get_turn(revision_id)`；不能从投影正文先取候选再授权。至少运行 `tests/integration/test_raw_revision_provenance_e2e.py`、`test_raw_search_canonical_fetch.py`、`test_reconcile_raw_revision_provenance.py`、`tests/unit/test_raw_event_store.py`、`test_raw_search.py`、`test_project_raw_vault.py` 和 `test_cognitive_consolidator.py`。存量迁移先运行 `python3 scripts/reconcile_raw_revision_provenance.py --json`，只有复核备份/gap 后才加 `--apply`；无法证明的旧页只能登记 gap，禁止伪造 revision/span。

修改 conversation cleaner、WikiBuilder reconstruction、tokenizer/chunking、extractor prompt input 或 complete-distillation coverage 时，必须维持 lossless extract contract：除显式 private thinking 外，可见代码、命令、编号、空行、附件占位和首尾格式不得被 cleaner/fallback 删除；canonical builder 必须使用 `lossless=True`，使总预算/单消息预算只记录 overflow，不触发 head-tail/消息截断。private exclusion 元数据不得包含被排除正文。改变任何输入语义时必须更新 `DISTILLATION_INPUT_CONTRACT_VERSION`，使旧检查点 cache miss，并在 `chunk_info` 留下可审计版本；不得只改 cleaner 而继续复用旧输出。至少运行 `python3 -m pytest tests/integration/test_lossless_distill_e2e.py tests/unit/test_distillation_engine.py tests/unit/test_distillation_chunked.py tests/unit/test_wiki_builder_reconstruct.py -q`，并用首/中/尾 sentinel、附件占位与极小预算验证真实 extractor input。

修改 extractor template/schema/parser/quality/backend/model routing、fragment merge 或输出相关 distill 配置时，还必须更新或验证 `DistillExecutionSpec` 分母。生产 `DistillBackend` 与 `FragmentMerger` 必须显式实现 credential-free `checkpoint_identity()`；不得恢复反射猜测或 caller fallback。任一有效字段变化都必须产生 `execution_spec_changed`，无关存储路径变化应保持 hit。提示词只能预渲染一次并把同一字符串传给 LLM；规格计算不得触发 intent-router LLM fallback。检查点损坏或旧 schema 必须 fail closed，新规格执行失败必须保留旧 completed generation。COG-011 后，execution spec 还绑定 `DistillInputSpec` hash 与 `distill_output_v4` admission contract；completed checkpoint 必须持有 canonical root、`CheckpointAdmission(input_spec_hash, output_contract_version, canonical_output_hash, judgment)` 与一致的 parsed fragments，save 和 lookup 都要以完整 immutable `DistillInputSpec` 重跑 union validator。缺 root/admission 的旧行、input-spec/contract 漂移、root/hash/fragment 破损或 checkpoint root 合同非法一律 miss 后重跑，不能迁移时补造证明。生产迁移先停止 daemon，默认 dry-run 盘点，确认备份目录后才 `--apply`；至少运行 `python3 -m pytest tests/unit/test_distill_execution_spec.py tests/unit/test_chunk_checkpoint.py tests/integration/test_checkpoint_execution_spec.py tests/integration/test_reconcile_distill_execution_checkpoints.py -q` 和 `python3 scripts/audit_distill_output_contract.py --strict --json`。

修改 conversation extract 输出契约时，必须把 `DistillInputSpec` 作为唯一的 source agent/session/event/completeness/visible-input identity，并在模型调用前封存 `CognitionExtractionContext`；不能从模型输出或 `Session` 猜回 agent、span、authority、ACL、retention 或 artifact identity。根返回必须满足 `extract.json` 的同一 Draft 2020-12 union：合法 skip 同时具备 `judgment=skip`、`fragments=[]`、`distill_intent=skip`、`claims=[]`、非空 `skip_reason` 和引用已绑定事件的 `no_value_evidence`；knowledge/skill 必须具备至少一个 admitted fragment、非 skip intent、非空 claims、`user_behavior_intent` 和 19 字段完整的 `cognition_episode`。known 只允许当前 chunk exact evidence 和 admitted claim，unknown/not_applicable 不得带 value/evidence/claim，且 situation/facts/scope 不得全 unknown。首次 LLM 输出在 correction 前必须先校验；任何非法输出（包括空 non-skip fragments）只能进入有限 correction 或失败，不能宽容地降为 skip；保存/读取 checkpoint 与正式写页前必须再次校验 canonical root 及其 hash/judgment/structured-output binding。正式 action/Wiki 前必须先原子提交 canonical episode revision/event/outbox，router 反查 committed revision 后才执行。发布配置不得关闭 `distill.structured_output_contract.enforce` 或 `distill.action_router.enabled`，否则只可做非 certifying 诊断。

修改 artifact 采集或蒸馏证据时，artifact identity 必须由 `core.evidence.artifact_catalog.ArtifactCatalog` 独占。Capture outbox 与完整 Session handoff 都要把 ref 绑定 authoritative Raw revision；复用 revision 必须校验当前内容 hash，handoff 的 `raw_content_hash` 只能回读 immutable header。文件型 ref 必须读文件重算；pathless tool result 必须携带仅系统内部使用的 canonical inline payload并由 Catalog 重算，marker/caller SHA 不构成证明，inline payload 不得进入 Prompt。`DistillInputSpec v4` 只把当前 input/chunk 允许的 opaque `artifact_ref_id`、type、窄脱敏摘要与 source event 暴露给模型。模型不得输出 URI、type、summary、SHA、MIME 或 ACL；Extractor 必须在首次响应及每次 correction 后从 immutable catalog 解析这些字段。输入 catalog 只要有一个 path/hash/ACL/source admission 失败就必须在 Prompt/模型调用前整体阻断，handoff 也必须保留 malformed ref 供 gate 拒绝，不能静默丢弃坏 ref 后继续；artifact catalog/URI resolver 的代码摘要必须进入 `DistillExecutionSpec`，避免旧 checkpoint 跨身份语义复用。未知、伪造、跨 chunk、越权、type/hash 漂移或模型自填 identity 均 fail closed；不得恢复靠 Prompt 正则要求模型拼 URI，也不得在失败后猜测修正。测试至少覆盖相同内容跨路径/轮次、chunk allowlist、缺失文件配伪造 SHA、pathless payload 重算、伪造 ref、错误类型、完整 SHA 校验、旧增量轮次 Raw identity、revision/hash mismatch 和 checkpoint hit；审计必须报告非零 catalog 分母且 `artifact_ref_mismatch=0`。

蒸馏 claim、行为意图证据和认知动作还必须消费 `core.evidence.source_authority.SourceAuthorityCatalog`。系统从 exact Raw span 的 role 与入口 metadata 生成七类权限；高权消息里的 Markdown 引用、代码围栏/行内代码及中英日韩成对引号按原始 offset 拆成 `quoted_content`，缺 role-local proof 的 detached 文本也只能低权保存。模型只选能确定的 `source_authority_id`，否则省略并由系统按 quote 唯一解析；`assistant_inference/tool_observation/external_content/quoted_content` 不得单独成为用户信念、人格、策略或 reinforcement，external/quoted metadata 也不能被 caller label 覆盖升级。外部知识正文仍要完整保存并可检索，不能用 prompt-injection 关键词黑名单删除 Raw；低权更新进入 pending shadow，用户认知 Observation 只消费 user span。相关修改必须运行 `python3 scripts/audit_cognitive_source_authority.py --strict --json`，并保持未授权认知更新、embedded quote unauthorized、high-authority trace gap 与 Raw blocking site 全部为 0。

修改 Observation 校准时，必须以 canonical Raw revision + content hash 为独立性分母：同根的 Raw 与派生 Wiki 只计一簇，同时引用多根的派生页不得合并 Raw 根或独立计票。posterior 只能来自可独立重算的 weighted evidence shrinkage，计算身份必须绑定稳定 Observation ID、脱敏前 measurement digest、canonical 排序的 peer Observation、来源分类、lineage、validator/combiner 实现与 spec；validator 缺陷、重复 identity、源码不可读或精确 Raw/span 缺失都必须失败关闭。正式顺序是先向唯一 `CognitiveStateStore` 提交 `CalibrationRecord` revision/event/outbox，再由 `CalibrationRecordStore.apply_to_observation()` 把 current posterior 绑定到 `base_measurement_status=verified` 的 Observation，最后从 committed record 重放 Wiki；不得直接调用私有 binder，superseded receipt 不得回绑，历史值无法证明 prior 时必须保留 `historical_unverified` 并等待重新提取。禁止直接覆盖 confidence、把 JSON 只塞进 Markdown 或在 export 时重跑另一套 validator；清理 calibrated Observation 前也必须有 coordinated record retirement。窄脱敏只处理个人识别信息、API key/令牌、银行卡、密码和私钥，不加密或删除普通内容。至少运行 `python3 -m pytest tests/unit/cognitive/test_calibration_record.py tests/unit/cognitive/test_calibration_lineage_audit.py -q` 和 `python3 scripts/audit_cognitive_calibration_lineage.py --strict --json`。

修改 `judgment=skill` 路径时，不得恢复 suggestion-only early return 或用 `skill_suggestion` 充当正式资产。正式顺序固定为完整 `cognition_asset_commit`、可选 versioned `CognitiveDecisionAssetProposal`、普通 Wiki/action-router 写入和 Wiki/search 投影；只有资产与页面的 typed receipts 都提交后才能 processed。proposal 失败必须独立记录且不回滚资产/页面，asset 失败必须阻断后续写入。持久化仅用 `pii_credentials_only_v1` 脱敏个人隐私、API key/凭据、银行卡和密码，不做加密，也不得截断普通代码、数字、source span、ACL 或尾部证据。至少覆盖完整资产、尾部 sentinel、proposal failure、asset failure、Wiki/event 和 `skill_asset_without_cognition=0`。

架构依赖边界以 `scripts/arch_dependency_graph.py --check` 和 `docs/core-integrations-dependencies.md` 为准。新增 runtime-only cycle waiver 必须写明 `owner`、`target_interface`、`resolution` 和具体 `arch-debt-*` issue；不得用 T7/TODOS 占位。`core.cli.helpers` 等 core helper 不能依赖 integrations backend；需要 Obsidian vault 注册检查时复用 `core.vaults.obsidian_registry`。

认知动作变更必须保持 claim↔fragment 的显式 `claim_ids` 全覆盖，并走父动作 → intent → leased command → 真实 target effect → reciprocal receipt 的顺序。`applied` 不能由 action DB 自签；Observation/Reflection/PolicyPatch/Relation 目标必须返回稳定 effect id 与 before/after hash。存量先运行 `python3 scripts/reconcile_cognitive_action_effects.py --json` 只读预览，停止 daemon 并确认备份目录后才使用 `--apply --process --backup-dir <dir> --json`；完成后运行 `python3 scripts/audit_cognitive_action_effects.py --strict --json`，所有 lineage/target/hash gap 必须为 0。

满分/发布复验入口为 `python3 scripts/run_full_score_gates.py --strict --real-api`。`--strict --real-api` 拒绝 `--only` 与全部 skip 参数；只有当前 canonical 62-gate manifest 的 expected/selected/executed 完全一致、omitted 为空、required receipts 全通过、工作树干净并绑定完整 Git commit 时，`mnemos.full_score_gates.v2` 才能 `certifying=true/release_eligible=true`。三个 quality zero-closure gate 要求 maintainability/zombie/vulture residual=0，`docs.asset_manifest.strict` 还要求完整 Desktop profile，独立 Phase 5 清单要求 `contracts.persona_runtime_effectiveness`、`contracts.blindspot_asset_boundaries` 与 `contracts.phase5_failure_contracts` 都进入证书；最后一项同时验证 frozen baseline failure evidence、十类跨实例/进程/E2E/counterfactual/crash/omission/0-denominator/dynamic-entrypoint 反例，以及四个 PH5-031 验收计数归零。`contracts.cognitive_action_effects` 要求认知动作真实效果闭环，`contracts.cognitive_calibration_lineage` 要求派生证据去重、可重算 record 与投影零缺口，`contracts.cognitive_event_dispatch`/`contracts.evidence_graph_direction` 要求 episode dispatch 与 evidence direction 闭环，`contracts.cognitive_search` 要求冻结 benchmark 的召回、溯源和 ACL 指标通过，`model_call_ledger.static` 要求所有直接 provider 边界具有完整请求的预留、发送标记、结算/释放或已发送成本保留路径。静态/隔离代码合同不证明某台机器的旧账本已经迁移、健康清零或完成恢复演练，但 production red 必须保持显式阻断，不能被 seeded audit 掩盖。每个 receipt 保存 stdout/stderr SHA-256，发布前必须在同一干净 commit 上运行独立 verifier。开发自检可用 `--only`，但始终 non-certifying。`scripts/audit_test_suite_denominator.py` 负责 pytest 文件唯一归层。

处理 COG-018 时，生产实现应继续通过 `core.telemetry.prompt_call_log` 的静态兼容导出使用 `core.telemetry.model_call_ledger`，不得建立第二账本或双写路径。仅不持久化/脱敏个人隐私、API key、银行卡信息、密码、raw prompt/response 与 caller error。`scripts/reconcile_model_call_ledger.py` 只提供诊断；direct `--apply` 无 registry-issued capability，必须零写入 blocked。正式 apply/rollback 使用注册 CLI，sealed-v3 manifest、普通 SQLite backup/hash/lock 都只是本地恢复正确性证据，且 target/SQLite sidecar 的 orphan、缺失、漂移或篡改必须 fail closed。2026-07-14 已完成本机 apply → health/plan → v3 restore → reapply → final health/plan 演练及 `6156 passed, 15 subtests` 的 isolated Quick；此事实不能替代 full-score 发布证书。

测试/门禁状态边界以 `core/ops/hermetic_run.py` 为唯一实现。`scripts/run_tests.py`、pytest collection、full-score 和 `scripts/audit_gate_hermeticity.py` 必须共用一个不可复用的 `isolated` sandbox root；HOME、Mnemos/database/wiki、XDG、temp、pycache、artifact、stdout/stderr/report 都在根内。默认环境不含 API key，只有显式 full-score `--real-api` 才可继承；测试必须注入 deterministic provider，不能借开发机凭据假绿。`health/status/distill status/verify/golden` 默认只读，写权限探针必须显式 `--write-probes`。提交前至少核对 manifest 的 `environment_hash` 非空、`outside_write_count=0`、`formal_state_diff=[]`，并运行 `python3 scripts/audit_gate_hermeticity.py --suite diagnostics --strict --json --output-dir <empty-temp-dir>`。

审计脚本默认应是只读入口。`scripts/audit_orphan_modules.py` 默认只向 stdout 输出，`--check` 只比较 `docs/orphan-modules-report.md` 且不写文件；需要生成 repo 内报告时必须显式传 `--output docs/orphan-modules-report.md --apply`，并写 ActionLedger。`scripts/audit_vulture_whitelist.py` 无参数运行同样只读，只报告会添加的 `# noqa` 和 whitelist body 删除；只有显式 `--apply` 才能写 source/whitelist，且无 body 变化时不得只为 header/sort 重写 `vulture_whitelist.py`。新增或修改会写 repo 的审计脚本时，必须提供只读默认模式、`--check` 模式、显式写入参数和测试，避免只读评分工作区被报告文件污染。

---

## 会话生命周期（必须执行）

### 1. 会话开始（Session Start）

**你必须做的**：
1. 检测当前任务类型（coding / debugging / design / review / ...）
2. 调用 `preflight_inject(task_type=xxx)` 装载历史经验
3. 调用 `persona_behavior_prompt()` 获取行为提示词
4. 如果有用户提问涉及已有知识，调用 `wiki_search(query=xxx)` 补充上下文

**示例**：
```json
{
  "tool": "preflight_inject",
  "arguments": {
    "task_type": "coding",
    "subtype": "python",
    "context_text": "用户正在写一个 FastAPI 项目"
  }
}
```

### 2. 会话进行中

**持续监控**：
- 用户每发送一条消息，评估是否需要调用 `intent_route`、`wiki_search` 或 `context_aware_search` 补充知识
- 如果检测到风险模式（如用户要删除数据、修改配置），调用 `guard_check`
- 如果用户提到"原话/证据/聊天记录"，调用 `session_search` 查找 raw 历史会话
- 如果用户提到"上次怎么解决"，同时调用 `session_search` 和 `context_aware_search`
- 如果 `context_aware_search` 返回空结果，先查看 `query_trace`、`degraded`、`degraded_reasons`，区分真实无匹配与 embedding/reranker 降级
- 如果用户提到知识库可能未覆盖的主题，调用 `blindspot_check(query=..., session_id=...)`
  - 当返回 `blindspot_found=true` 时，将 `prompt_for_user` 展示给用户
  - 用户回复"查一下/记录"后，使用 `suggested_query` 搜索资料并继续对话
  - 对话经 `knowledge_distill` 写入 Wiki 后，系统会自动销警

### 3. 会话结束（Session End）——**最重要**

**如果本轮对话有价值（学到了东西、解决了问题、做了决策）**：

1. **逐轮上报对话**（推荐，实时）：
```json
{
  "tool": "capture_turn",
  "arguments": {
    "source_agent": "claude",
    "session_id": "唯一的 session 标识",
    "turn_number": 0,
    "user_content": "用户的消息内容",
    "assistant_content": "AI 的回复内容"
  }
}
```

> 每轮对话结束后立即调用 `capture_turn`，低延迟（< 200ms），只做校验和入队。
> 如果本轮带有工具结果、附件、reasoning artifact、截图、终端输出或测试报告，`capture_turn` 会在 raw metadata 中写入标准 `artifact_refs`；handoff 会把它们绑定 authoritative Raw revision，后续蒸馏只允许模型选择系统 catalog ref。路径、canonical URI、hash、MIME 与 ACL 不由模型生成，Wiki 只展示系统解析后的摘要链接。

2. **会话结束标记**：
```json
{
  "tool": "end_session",
  "arguments": {
    "source_agent": "claude",
    "session_id": "唯一的 session 标识"
  }
}
```

> `end_session` 通知 Mnemos 会话已完成，触发队列排空和完整性校验。

3. **批量上报历史对话**（如需一次性上报完整记录）：
```json
{
  "tool": "capture_session",
  "arguments": {
    "source_agent": "claude",
    "session_id": "唯一的 session 标识",
    "turns": [
      {"turn_number": 0, "user_content": "...", "assistant_content": "..."},
      {"turn_number": 1, "user_content": "...", "assistant_content": "..."}
    ]
  }
}
```

4. **触发知识蒸馏**（将对话转为 Wiki 知识）：
```json
{
  "tool": "knowledge_distill",
  "arguments": {
    "session_id": "同上",
    "messages": [同上],
    "write_to_wiki": true
  }
}
```

> **为什么不只保存不蒸馏？** `capture_turn` 是 L1 原始记录（快速、全量），`knowledge_distill` 是 L2 结构化知识（慢、精华）。两者互补。

---

## 知识摄入（用户主动投喂）

### 场景 1：用户口述知识

用户说：
- "记住这个：Python 的 asyncio.gather 可以设置 return_exceptions=True"
- "帮我记下：以后遇到这种错误先检查 DNS"
- "这很重要：项目里所有 API 都要加限流"

**你的操作**：
```json
{
  "tool": "knowledge_ingest",
  "arguments": {
    "content": "用户说的完整内容",
    "tags": ["coding", "python", "asyncio"],
    "source": "human"
  }
}
```

### 场景 2：用户指定文件导入

用户说：
- "把这个文件加入知识库：~/notes/architecture.md"
- "解析这个代码文件，提取设计模式"
- "把这份文档存进去，以后好查"

**你的操作**：
```json
{
  "tool": "document_process",
  "arguments": {
    "file_path": "~/notes/architecture.md",
    "title": "系统架构笔记",
    "mode": "distill"
  }
}
```

### 场景 3：用户指定文档解析

用户说：
- "解析这个 PDF，把内容存进知识库"
- "这份 PPT 讲了什么？帮我整理成笔记"
- "把这个 Excel 的数据提取出来"

**你的操作**：
```json
{
  "tool": "document_process",
  "arguments": {
    "file_path": "~/documents/spec.pdf",
    "mode": "distill"
  }
}
```

---

## 知识查询（用户回忆/查找）

### 场景 1：查知识库

用户说：
- "我之前写过关于 Redis 的笔记吗？"
- "查一下我们关于架构设计的讨论"
- "我记得有个反模式，叫什么来着"

**你的操作**：
```json
{
  "tool": "wiki_search",
  "arguments": {
    "query": "Redis 架构设计",
    "limit": 5
  }
}
```

### 场景 2：查历史会话

用户说：
- "我们之前聊过什么？"
- "上次那个 session 里我怎么说的"
- "找回之前的对话"

**你的操作**：
```json
{
  "tool": "session_search",
  "arguments": {
    "query": "Redis",
    "limit": 10
  }
}
```

如果知道具体 session_id：
```json
{
  "tool": "session_search",
  "arguments": {
    "session_id": "abc123"
  }
}
```

---

## KIA 闭环（Knowledge-in-Action）

KIA 是 Mnemos 的核心价值——知识在行动中被使用。

### 第一步：PreFlight（任务前装载）

**何时调用**：每次开始新任务时

```json
{
  "tool": "preflight_inject",
  "arguments": {
    "task_type": "coding",
    "subtype": "refactoring",
    "context_text": "当前任务上下文"
  }
}
```

返回的 `checklist` 是一个风险清单。你应在任务开始时提醒用户注意这些风险。

### 第二步：Guard（执行中守护）

**何时调用**：用户发送的每条消息都可能触发

```json
{
  "tool": "guard_check",
  "arguments": {
    "user_message": "用户刚发的消息",
    "ai_response": "你刚回复的内容",
    "task_type": "coding"
  }
}
```

如果返回 `alert: true`，立即向用户发出警告。

### 第三步：Retrospective（自动复盘与强制复盘）

**何时触发**：会话结束、任务成功完成、用户主动说“复盘一下”、或 `check_pending_recaps` 返回高价值待复盘项时。

普通会话沉淀由 `capture_turn`（每轮）+ `end_session`（结束）+ `knowledge_distill`（有价值时）自动完成。

强制复盘必须走结构化链路：

1. `check_pending_recaps` 找到待复盘项。
2. `recap_start` 领取 owner 并返回三问契约。
3. 当前 Agent 只问三问：目标和实际、关键原因或教训、下次具体怎么改。
4. `recap_submit` 生成结构化草稿；缺字段时继续补问。
5. 用户确认后调用 `recap_finalize` 写入正式复盘页并生成 consumption plan；只有全部 required receipt 完整时才把 `state=consumed` 当终态。
6. 用户跳过时调用 `recap_skip`，必须记录 `no_time`、`low_value`、`false_positive`、`already_handled` 或 `no_response`，并检查 scheduler/scoring/persona 等声明 target 的真实 receipt。
7. 用户纠正 recap 时调用 `recap_feedback`；若已有不同反馈，必须把 `recap_status.latest_feedback.feedback_event_id` 作为 `supersedes_event_id`。partial correction 保持可重试，不向用户报告完成。

---

## 认知反射（L4 Reflection）

**何时调用**：
- 用户提到重要决策、新项目、角色转变、反复卡壳等信号
- 用户说"分析我的模式""我最近有什么变化"时

### 自动触发

```json
{
  "tool": "reflect_on_input",
  "arguments": {
    "text": "我要启动一个新项目",
    "auto_llm": true
  }
}
```

**返回字段**：
- `triggered`: 是否触发反射
- `insight_summary`: 一句话洞察摘要
- `key_points`: 关键发现列表
- `confidence`: 洞察置信度
- `llm_called`: 是否实际调用了 LLM
- `llm_error`: LLM 调用失败原因（如有）
- `prompt_used`: 生成洞察使用的 prompt（可用于调试或手动调用）

内部证据解释由 `EvidenceGraph.explain_why()` 提供；该接口会返回 `direct_evidence_chain`，并把 Insight 直接指向的 Observation/Knowledge 证据合并进解释列表，供宿主回答“为什么这么认为”时追溯来源。

反馈历史由 `ReflectionEngine.get_feedback_history(limit, feedback_type)` 暴露；宿主需要回看用户对 Insight 的准确/不准确/有启发/无关反馈时走该门面，不直接绕过 Reflection 层访问底层 collector。CLI、daemon 与 MCP facade 默认通过 `reflection.register_default_consumers=true` 注册 Layer 5 消费者，将 Reflection 输出同步送入 persona/KIA/PolicyPatch/Hephaestus；只读 pending 查询可关闭消费者，避免查询动作产生写入。

### 手动触发

```json
{
  "tool": "reflect_manually",
  "arguments": {
    "query": "分析我最近一个月的决策模式",
    "auto_llm": true
  }
}
```

### 只生成 Prompt（由你自行调用 LLM）

将 `auto_llm` 设为 `false`，Mnemos 只准备证据和 prompt，不调用 LLM：

```json
{
  "tool": "reflect_on_input",
  "arguments": {
    "text": "我要启动一个新项目",
    "auto_llm": false
  }
}
```

### 提交反馈

拿到洞察后，如果用户评价"准""不准""有启发""无关"，调用：

```json
{
  "tool": "reflection_feedback",
  "arguments": {
    "reflection_id": "record_id",
    "feedback_type": "insightful",
    "comment": "用户认为洞察准确"
  }
}
```

---

## 画像系统

画像对外同时保留旧三层雷达和 `user_cognitive_profile_v2`。v2 画像的 schema、DTO、repository 和 payload 构建集中在 `core/persona/cognitive_profile.py`，由 `profile_signals`（明确偏好、纠错、忽略、打断、返工等原始信号）、`profile_assertions` / `profile_assertion_revisions`（当前投影与不可变断言修订）、`profile_read_authorizations`（绑定 server-resolved principal、project/session、consumer、purpose、exact assertion revision 和 ACL hash 的短期 opaque token）、`profile_usage_outbox`（durable intent）和 `profile_usage_log`（exact target effect receipt）组成。当前只有 preflight、ContextAwareSearch 和 persona behavior prompt 具备真实授权路由；三者必须携带签发时同一 principal/narrowing 消费 token 后才能记录 usage。Distill、CognitiveValueGate、Auto-Healing 和 Cognitive Decision Flywheel 在取得 server-resolved principal 或 sealed read decision 前保持 disabled，不能计入 effective consumer 或制造 usage 证据。

`user_cognitive_blindspots.db` 与 `interaction_preferences.db` 的初始化/迁移必须作为同一 generation 执行：先运行 `python3 scripts/reconcile_user_model_asset_stores.py --json` 复核两库共同的 `plan_hash`，停止 daemon/MCP writer 后再使用 `--apply --backup-dir <dir> --expected-plan-hash <sha256:...> --json`。apply 必须在共享 offline lock 下使用 attached-database 原子 transaction，并输出唯一 backup generation、fresh-missing pre-state manifest、两库 integrity/FK、second apply zero-change 和 restore drill；prepared generation 若在 commit window 崩溃，下一次 apply 必须先恢复旧完整代际。禁止分别调用两个 store initializer 充当生产迁移。

历史 Wiki `知识形态` 补写必须先 dry-run 生成并人工复核 plan hash，再执行 `python3 scripts/reconcile_wiki_knowledge_forms.py --wiki-dir <wiki> --review-manifest <review.json> --apply --backup-dir <empty-dir> --expected-plan-hash <sha256:...> --json`。apply 在任何 backup/write 前取得 offline lock，先验证全部 preimage 并写 Wiki 外 staged generation，再 materialize Markdown 和提交 exact projection batch；任一失败必须同时恢复 projection databases 与全部 Wiki preimage。若进程崩溃留下 prepared/source_materialized/projection_committed manifest，先以相同参数加 `--recover --backup-dir <dir>` 恢复旧代际，再重新 dry-run，不能直接覆盖备份或继续 apply。

六类知识形态的 alias 与显示值只能由 `core/knowledge_form.py` 定义。producer prompt/schema、Wiki renderer、migration planner 和 coverage audit 必须通过 canonical normalizer/display API 对齐；不能在迁移或消费者里复制 `FORM_ALIASES`。归一化合同包含 Unicode NFKC、trim、casefold，以及“洞察”映射到 canonical“洞察关联”。修改任一路径后运行 `python3 scripts/audit_blindspot_asset_boundaries.py --strict --json`，结构证据必须显示 `knowledge_form_vocabulary_owner_count=1` 与 `producer_migration_consumer_normalization_drift=0`；生产 Wiki coverage 仍需单独观察，结构绿灯不能替代历史页面 reconciliation。

### 获取画像

**何时调用**：
- 会话开始时（了解用户当前状态）
- 用户说"分析我的偏好"时

```json
{
  "tool": "persona_summary",
  "arguments": {}
}
```

### 获取行为提示词

**何时调用**：每次会话开始时

```json
{
  "tool": "persona_behavior_prompt",
  "arguments": {}
}
```

返回的提示词应**追加到你的 system prompt 中**，指导你以用户偏好的方式交互。

### 更新画像

**何时调用**：
- 用户说"更新我的画像"时
- 定期（如每天一次）

```json
{
  "tool": "persona_update",
  "arguments": {}
}
```

### 查看画像行为提示效果指标

**何时调用**：
- 用户问"我的画像有没有用""行为提示效果如何"时
- 定期评估画像驱动策略的覆盖率和命中策略分布

```json
{
  "tool": "persona_behavior_metrics",
  "arguments": {
    "days": 30
  }
}
```

**返回字段**：
- `total_calls`: 总调用次数
- `by_agent`: 各 Agent 调用分布
- `by_source`: 调用来源分布（preflight / mcp_persona_behavior_prompt / cli）
- `by_strategy`: 命中策略分布（如 `focus_depth_high`、`abstraction_high`）
- `ab_test`: A/B 实验分组分布
- `daily_calls`: 每日调用趋势
- `profile_usage`: `mnemos.profile_usage.v1`，统计 v2 画像消费者、`action_changed_count` 和用户反馈分布

**CLI**：

```bash
mnemos persona behavior-metrics [--days N]
mnemos persona daily-summary [YYYY-MM-DD] [--json]
mnemos persona projects [--days N] [--json]
mnemos persona recent-signals [--source all|notes|wechat] [--days N] [--json]
```

- `daily-summary` 输出指定日期的画像信号日聚合摘要；日期省略时默认今天。
- `projects` 输出最近有 session/git 画像信号的项目路径，可继续传给 `project-signals` 深查。
- `recent-signals` 输出最近 notes/wechat 原始画像信号，适合排查画像输入是否已入库。
- 修改画像结构或画像消费后，必须运行 `python3 -m pytest tests/unit/test_user_cognitive_profile_v2.py tests/integration/test_profile_signal_assertion_usage_loop.py -q`、`python3 scripts/audit_persona_profile_contract.py --strict`、`python3 scripts/audit_persona_runtime_effectiveness.py --strict --json` 和 `python3 scripts/audit_runtime_producer_consumer_closure.py --strict`。当前 effective consumer 只有 preflight、context search、persona behavior prompt；DistillTask 未携带 server-resolved principal 或 sealed read decision，`distillation_prompt`、quality gate、auto-healing、cognitive flywheel 必须保持 disabled 且不能计入分母。前者只验证 isolated seeded structural contract；后者必须只读真实 store，不能用临时 seed 代替 live evidence，并按 read authorization 签发时刻验证 historical temporal validity，不能把之后正常 supersede 的旧 immutable usage 误报为 drift。future/unrelated/stale-at-read revision 必须阻断；三类 consumer 必须匹配固定 target/kind/revision comparator，context search 还必须从 baseline/enabled ranking 独立重算哈希与 rank delta。普通 `SignalStore` 打开不得建目录、DDL、ALTER 或 missing-table 自动修复；显式新建只允许 bootstrap/隔离 fixture 使用 `initialize_schema=True`。历史 schema 先运行 `python3 scripts/reconcile_profile_assertion_revisions.py --json`，人工复核输出的 `plan_hash`，停止 daemon/MCP writer 后再运行 `python3 scripts/reconcile_profile_assertion_revisions.py --apply --backup-dir <dir> --expected-plan-hash <sha256:...> --json`；apply 必须持有 offline lock，并给出唯一 backup generation、source/backup integrity、FK、逐语句 rollback、second apply zero-change 与 restore drill 证据。

---

## 系统运维

### 健康检查

**何时调用**：
- 系统异常时
- 用户说"检查系统状态"时
- 定期（如每天一次）

```json
{
  "tool": "health_check",
  "arguments": {}
}
```

### 构建 Wiki

**何时调用**：
- 用户说"整理最近的对话"时
- 定期（如每天一次）

```json
{
  "tool": "wiki_build",
  "arguments": {
    "dry_run": false
  }
}
```

---

## 蒸馏执行方式

Mnemos 通过配置的 OpenAI-compatible API 直接执行蒸馏。宿主 Agent 负责上报/触发，不替代 Mnemos 思考。

**这意味着什么**：
1. 当 Mnemos 需要蒸馏时，session 进入 `distill_queue`（amphora SQLite 队列）
2. `mnemos daemon start` 启动的 HephaestusWorker 消费队列
3. **Mnemos 直接调用 LLM API** 执行蒸馏
4. 将结构化 Wiki 页面按 Charon 路由写入正式目录；无法分类或同名冲突才写入 `00-Inbox`；成功蒸馏后的 L1 `status=distilled` 标记使用当前 Worker 的 `inbox_dir.parent` 创建 StorageBackend，必须与写入目标同 vault
5. 宿主 Agent 只需上报 session，无需自行蒸馏

**你的责任**：
- 确认 `~/.mnemos/configs/main.json` 中已配置 LLM、Embedding、Reranker 三类必填模型的 `model`、`base_url` 与 `api_key_source`；多模态模型是可选项，配置 `multimodal.*` 或 `MNEMOS_MULTIMODAL_*` 后图片入口会自动解析入库并进入蒸馏队列，未配置或 API 失败时生成可恢复任务；真实 key 应通过 `env:...`、`keyring:...` 或 `keyref:...` 解析，不写入明文 `api_key`，并用 `python3 mnemos_cli.py doctor config --strict --json` 复验配置、隐私、retention、daemon、legacy/stale key 和权限
- 运行 `mnemos daemon start` 保持后台 Worker 活跃
- 如需更快消费队列，可运行 `mnemos distill run`

---

## 蒸馏层配置说明

`~/.mnemos/configs/main.json` 的 `distill` 段控制七层蒸馏行为。常用键：

| 键 | 默认值 | 说明 |
|---|---|---|
| `token_budget_total` | 16000 | 单轮蒸馏总 token 预算 |
| `token_budget_system_pct` / `context_pct` / `content_pct` / `output_reserve` | 0.10 / 0.25 / 0.55 / 2000 | PromptBuilder 预算分配 |
| `response_tokens` | 6000 | LLM 调用的静态兼容 `max_tokens`；动态预算开启时也是默认输出档 |
| `response_tokens_default` / `medium` / `long` / `retry_max` | 6000 / 8000 / 12000 / 16000 | 蒸馏结构化输出四档上限；`finish_reason=length` 重试使用 `retry_max` |
| `effective_max_tokens` | 24000 | `build_session_text` 与 `_chunk_messages` 的默认 chunk 上限 |
| `per_message_token_limit` | 6000 | 超长单条消息拆分时使用的上限 |
| `chunk_std_factor` / `chunk_total_factor` / `chunk_size_factor` | 3 / 25 / 1.5 | 决定标准/分块/超长的 token 阈值（乘以 `token_budget_total`） |
| `incremental_batch_turns` | 5 | 分块蒸馏时每 chunk 最多包含的原始 turn 数 |
| `llm_cost_budget_per_session` | 10 | 单次会话 LLM 调用成本上限；超过后返回 `budget_exceeded` |
| `cold_knowledge_archive_days` | 90 | 超过该天数未更新的页面会被 daemon 归档到 `99-Archive/Cold/` |
| `fragment_boundary_chars` | 8000 | 片段内容超长时自动补充适用/不适用边界的字符阈值 |
| `min_value_context_chars` | 30 | 短片段需要 background 等上下文的最小字符数 |
| `max_tasks_per_cycle` | 5 | HephaestusWorker 每轮最大处理任务数 |
| `poll_interval_seconds` | 60 | HephaestusWorker 轮询间隔 |

**成本预算**：默认使用内置模型单价（`core/llm_config.py`），可通过 `llm.provider_prices` 覆盖。未配置价格的 provider/model 按 0 计算，预算控制不会触发。

**行为/意图与 skip 契约**：`PromptBuilder` 会把 `ContentSource`、`UserIntent` 和 `IntentRouter` 的系统侧预判，以及不可变 `DistillInputSpec` 的 source/event/completeness/hash/source-authority 合同注入蒸馏 prompt。非 `skip` 的 `distill_output_v4` 必须输出 `user_behavior_intent`，包括用户为什么引入/需要这条知识、意图证据、后续验证/修正事件、`intent_status` 和 `intent_confidence`。外部文件或附件只能证明材料被提供，不能自动证明用户认可其内容或要把它作为决策材料；没有 `explicit_user`、`system_policy` 或 `project_contract` 的精确证据时，预判保持 `unknown/unverified` 且置信度不高于 `0.3`。若没有长期可复用知识，只能走完整 skip 分支：`fragments=[]` 与 `claims=[]` 不足以单独成立，还必须有 `judgment=skip`、`distill_intent=skip`、`skip_reason` 和绑定 `source_event_ids` 的 `no_value_evidence`。Wiki 页面会在 frontmatter 和“来源追踪”展示行为意图及来源权限；低权内容仍可检索，但只进入 Attention 或 pending hypothesis，不能触发 active cognition。

---

## 知识新鲜度 CLI

把 freshness_check 从"只报警"推进到"可手动/自动刷新"：

```bash
mnemos freshness list [--status stale|fresh|all]   # 列出页面新鲜度状态
mnemos freshness refresh <page_path>               # 手动刷新指定页面
mnemos freshness refresh-all [--limit N]           # 批量刷新过期页面
```

- 刷新前自动备份原页面到 `07-Shadow/08-Refresh/`
- `timeless` 页面强制跳过
- 自动刷新由 daemon `freshness_refresh` 服务每日执行

## 熵减 CLI

手动触发知识熵减扫描并查看合并/关联候选：

```bash
mnemos entropy scan [--limit N] [--write-report]   # 扫描并打印摘要，--write-report 写入 99-Reports
mnemos entropy auto-fix [--apply-links]            # 自动执行建议（默认不删除，--apply-links 建立 KG 关系）
```

- daemon `entropy_scan` 服务每日运行一次，将高相似候选入队提醒

## 提醒 CLI

管理对话提醒队列，手动触发推送或关闭提醒：

```bash
mnemos reminder status [--json]                                # 查看提醒状态计数
mnemos reminder list [--status pending|pushed|resolved|deferred|ignored|dismissed|expired|all]
mnemos reminder push [--max N]                                 # 手动触发兜底推送
mnemos reminder resolve <reminder_id> [--choice ...]           # 按提醒 ID 关闭
mnemos reminder resolve --issue <issue_id> [--choice ...]      # 按 issue_id 关闭未解决提醒
mnemos reminder dismiss <reminder_id>|--issue <issue_id>       # 忽略提醒并记录原因
mnemos reminder expire-stale --days 30 [--json]                # 过期旧 pending/deferred 提醒
```

- daemon `reminder_scan` 服务扫描到高优先级过期页面时会自动入队
- 兜底推送默认通过 `DeliveryBudgetPolicy` 读取 `delivery.preference` 与 `delivery.profiles.<profile>`；`app.push_max_items` 仅作为迁移期 per-task 兼容兜底。
- 复盘任务队列用 `mnemos recap list --status pending --severity high --json` 查看，用 `mnemos recap resolve <task_id> --reason ...` 或 `mnemos recap dismiss --all --severity high --reason ...` 显式闭环；每次状态更新会写 `recap_task_events` 方便审计。

## 争议仲裁 CLI

当知识图谱检测到冲突关系时，Mnemos 可生成争议仲裁页面，供用户裁决。CLI 提供以下命令：

```bash
mnemos dispute scan [--max-disputes N]          # 手动触发争议扫描
mnemos dispute list [--unresolved-only]         # 列出争议页面
mnemos dispute show <page_path>                 # 查看指定争议页的评分明细
mnemos dispute resolve <page_path> --resolution <adopt_new|keep_old|keep_both|need_more_info> [--context TEXT]
mnemos dispute stats                            # 统计争议数量
mnemos dispute weights                          # 查看当前仲裁权重
mnemos dispute weights --set dim=value          # 调整权重（写入 state）
mnemos dispute weights --reset                  # 清除 state 权重，回退到 config/默认值
mnemos dispute weights --learn                  # 手动触发一次自适应权重学习
```

**权重加载优先级**：`learner > state 文件 > config > 默认值`。state 文件路径为 `~/.mnemos/state/dispute_weights.json`。

---

## 错误处理

### 蒸馏失败

如果 `knowledge_distill` 失败：
1. 检查 `capture_turn` 是否成功（确保 raw capture 已入队或写入 `raw_events.db`；raw vault markdown 由 daemon 的 `raw_projection` 服务投影生成）
2. 重试一次
3. 如果仍然失败，记录错误日志，稍后由 daemon 重试

运行 `scripts/e2e_probe.py` 时，不要把 `sync_log.status=new`、`skipped_backend` 或空 `backend_uids` 当作 L1/backend 已落地。canonical raw 模式必须看到本次 session 的 `raw_events.db.raw_turns.event_id` 和 `sync_log` row；外部 backend 模式必须能从 `backend_uids` 反查到实际记录。Wiki 验收必须命中本次 `session_id`，`--no-api` 跳过蒸馏时 Wiki 应为 skip。`--dry-run --no-api` 默认输出脱敏路径，只有 `--unsafe-debug` / `--show-paths` 可显示本机原始路径。

运行 `scripts/e2e_wow_probe.py` 时，`--mock-llm` 必须在隔离临时目录证明用户价值链路：三项必填配置 mock 就绪、可选多模态跳过、可信文档 100MB gate、默认 distill、行为/意图字段、Obsidian 路由、ContextAwareSearch/preflight 召回、runtime consumer ledger 和 auto-heal dry-run。`user_intervention_count` 应为 0；`--real-api` 仅用于发布/本机验收，不要求普通 CI 使用真实 API。

### 会话搜索无结果

如果 `session_search` 返回空结果：
1. 尝试用不同的关键词
2. 检查用户是否记错了时间范围
3. 如果确实没有，告知用户"没有找到相关记录"

---

## 最佳实践

1. **主动调用，不要等用户问**
   - 会话开始 → 自动 preflight_inject
   - 检测到风险 → 自动 guard_check
   - 每轮对话结束 → 自动 capture_turn
   - 会话结束 → 自动 end_session + knowledge_distill（有价值时）

2. **知识优先于猜测**
   - 用户提到技术名词 → 先 context_aware_search / wiki_search，再回答
   - 用户提到"原话/证据/聊天记录" → 先 session_search，再回答
   - 用户提到"上次怎么解决" → session_search + context_aware_search 联合查

3. **画像驱动行为**
   - 重正确性 → 详细解释、暴露假设
   - 重效率 → 直接给答案、省略背景
   - 高质疑 → 主动暴露局限和边界条件

4. **完整闭环**
   - 保存 → 蒸馏 → 构建 → 更新画像
   - 不要只做一半

---

## 快速参考卡片

```
会话开始：preflight_inject → persona_behavior_prompt → wiki_search(可选)
会话中：  guard_check(风险时) → context_aware_search/wiki_search(知识) → session_search(raw)
每轮结束：capture_turn
会话结束：end_session → knowledge_distill → wiki_build(定期)
用户投喂：knowledge_ingest(口述) / document_process(mode=distill 文件导入) / mnemos import(本地 CLI)
用户查询：context_aware_search/wiki_search(知识) / session_search(raw) / 两者联合(混合回忆)
系统运维：health_check/doctor/status(检查) / persona_summary/persona_update(画像) / check_pending_recaps(复盘)
画像：     mnemos persona behavior-metrics / daily-summary
争议仲裁：mnemos dispute scan / show / weights
新鲜度：   mnemos freshness list / refresh / refresh-all
熵减：     mnemos entropy scan / auto-fix
提醒：     mnemos reminder list / push / resolve
```

---

*最后更新：2026-07-01*
