# Agent 指南

## 已合并能力入口（禁止重新拆模块）

- 隐藏关系 / 间接关联 / 跨域关系：扩展 `core.kia.knowledge_graph.KnowledgeGraph.suggest_hidden_relations()`，返回 `RelationSuggestion` 列表，由调用方确认后写入数据库。
- 知识盲区（unsolved / unrecorded）：扩展 `core.kia.hygieia.KnowledgeImmuneSystem.detect_knowledge_gaps()` 的 trail 驱动分支。
- 关系网络分析：扩展 `core.kia.knowledge_graph.KnowledgeGraph`。
- 不要新增独立的“暗知识洞察”或“量子纠缠”模块；当前 CLI/daemon 只保留现行模式名。

## 蒸馏策略（P1-5 完整蒸馏）

- `core/hephaestus/distillation_engine.py::DistillationEngine._chunk_messages()` 不再对单条超长消息做前缀截断，而是使用 `core/hephaestus/tokenizer.py::Tokenizer.split_to_tokens()` 拆成多个 `part` 消息，保证所有内容都进入某个 chunk 被 LLM 看到。
- 2026-07-10 ROOT-005 起，`core/hephaestus/distillation_text.py::clean_message_content()` 只移除显式 `[thinking]...[/thinking]` 私密块，不得压缩长代码块、丢弃第 4 条及后续 shell 命令、删除编号/空行或重排可见字节。标准/分块 extractor 构建 session text 时必须使用 `lossless=True`：总预算或单消息预算不足只记录 `budget_overflow_tokens`，不得触发 head-tail/单消息截断；隐私排除只记录类型、span 和计数，不保存私密正文。`core/hephaestus/wiki_builder.py` 的纯文本 fallback 必须保留完整正文。分块检查点哈希和 `chunk_info` 必须声明 `lossless-visible-v1` 输入契约；缺少该版本的旧检查点只能 miss 后重跑，不能复用。
- 2026-07-11 ROOT-014 起，分块检查点必须绑定不可变 `DistillExecutionSpec`：精确渲染的 prompt、输出 schema、extract/parse/quality 代码摘要、显式 backend/provider/model/route、merge 合同和全部输出相关有效配置共同进入 canonical hash。backend 与 merger 必须实现 `checkpoint_identity()`，禁止反射猜测或 caller fallback。命中必须同时匹配 chunk input 与 execution spec；`chunk_info` 必须输出 `execution_spec_hash/prompt_hash/schema_hash/model_id/cache_hit/miss_reason/spec_diff_fields`。旧 schema 或损坏 metadata 只能 miss；新规格执行失败必须保留旧成功代际。生产迁移先停止 daemon，运行 `python3 scripts/reconcile_distill_execution_checkpoints.py --json`，确认影响后用 `--apply --backup-dir <dir> --json` 先备份再迁移，禁止给旧行伪造当前 spec。
- 2026-07-08 起，Hephaestus 对话蒸馏与文档蒸馏的正式写页必须经过 `core/trust` 可信推送闭环的 feature guard。`trusted_push.mode=off` 保持旧写入；`shadow` 只生成 shadow proposal/metrics 且不写正式 Journal；`enforce` 必须进入 `ProposalQueue -> PushDecisionGate -> append-only WriteJournal -> KnowledgeVaultWriter`。2026-07-12 起 `python3 -m core.trust.static_scan` 使用 v4 AST 逐调用点契约：禁止目录/整文件 marker 放行；正式 write/delete/move 必须由绑定 target/content/expected hash（move 另绑定 source/hash）的 typed receipt 支配，非正式 sink 只能用精确 `sink_id + owner + target_class + expiry` registry，unknown/stale/伪造 guarded 分类一律 fail closed。对话内决策只走 `DialogDecisionPush`/`mnemos proposal push|decide` 的结构化卡片，不写入原生 agent 历史；CLI 型 AgentBackend 只能通过 `mnemos agent shadow` 显式开启单 agent shadow，并由 `mnemos golden eval --confirm-send-content` 评估，不得进入生产写入链路；任何 AgentBackend subprocess 调用必须先过 `core.agent_kit.prompt_sanitizer.PromptSanitizer`。
- 分块路径提取出的多个局部片段会经过 `FragmentMerger`：
  - 先按标题 + keywords 的 Jaccard 相似度聚类；
  - 对包含多个片段的聚类，调用独立配置的 LLM 合成器整合成一条完整知识；
  - LLM 失败时自动回退到规则合并（字段并集 + 内容去重拼接）。
- 最终写入 Wiki 的仍是“针对整个对话”的一个或多个知识页面，不是每个分片一个页面。
- `doctor_helpers.py` 的 `LEGACY_OPTIONAL_DEPENDENCIES` 中已移除不存在的 `"scripts.mark_truncated"`。

## 工程债务闭环（ROOT-20260710-021）

- `scripts/check_maintainability_budget.py --closure` 与 `scripts/check_zombie_code_policy.py --closure` 分离 development ratchet 和 release closure；未过期的精确接受项可通过开发门，但必须输出 `release_eligible=false`。full-score 固定运行 `--closure --strict --json`，只接受 residual=0。
- broad `except Exception` 以 AST fingerprint 逐调用点登记，不能用同文件同数量替换继承旧额度；解析失败、过期接受、改善后未收紧 baseline 都 fail closed。普通 `--update` 只能固化改善；新增/替换风险必须显式 `--accept-risk-changes`。
- zombie baseline v2 每项必须有 owner/callers/remove_when/expires_at/telemetry；新增项不能由普通 update 自动接受。vulture current 与 `ci_ratchet_baseline.json` 必须同时为 0，非零状态禁止写入 baseline。

## 文档摄入单一所有权（ROOT-20260710-006）

- `DocumentImportService` / `FileIngestor` 的 `capture` 与默认 `distill` 只写 canonical raw；raw projection 独占 Obsidian raw vault，capture outbox 独占 Amphora handoff。禁止恢复 `FileIngestor.backend.save()`、`DocumentProcessor.save_to_backend()`、`--save` 或入口内直接 `enqueue_with_receipt()`。
- 文档 raw metadata 必须含稳定的 `asset_kind=trusted_user_document`、基于文件 SHA-256 的 `asset_id`、`distill_requested` 和 raw revision receipt。worker 收到已有 `raw_event_id` 时必须复用，不得再次 upsert revision；重复文件必须收敛为同一 revision、同一 capture event 和同一 handoff。
- 公共入口在 raw+capture queue receipt 后返回 typed `accepted` / `pending` / `existing`，不得把异步 Wiki 路径为空当失败。存量对账使用 `scripts/reconcile_pipeline_receipts.py`；只能删除 canonical receipt 已提交且无 provenance edge 的重复 worker raw。

## Wiki 投影生命周期（ROOT-20260710-007）

- 正式 Wiki Markdown 的 create/update/move/delete 必须先由 `core/wiki_projection_lifecycle.py::WikiProjectionLedger` 写入 append-only mutation，再由 `core/wiki_projection_publisher.py` 发布 canonical `wiki_page_updated`；不得新增只发事件、不记 mutation 的写页路径。
- EventBus 业务结果必须使用 `HandlerOutcome` 的 `ack/noop/retry/defer/dead`。`False`、`status=error`、异常、前序 revision 未完成或人工 proposal 未决定都不能被记为成功；每个 required consumer 必须以稳定 `consumer_id` 写 projection receipt。
- required consumers 固定为 `knowledge_graph`、`cognitive_graph`、`relation_embeddings`、`wiki_search_index`、`wiki_metrics`、`moc_navigation`。它们必须共享传入 config 的 Wiki、database 和 embedding index 路径，不能在自定义/测试环境回落到全局 `~/.mnemos`。
- Wiki ANN metadata 的 durable label 缺失、重复、错序或 chunk 数不符时必须重建；冲突 label 的所有 owner 必须重新嵌入。memory fallback 必须把向量持久化并能在重启后恢复，不能以永久 retry 代替实现。
- 存量修复先受控停止 daemon，运行 `python3 scripts/rebuild_wiki_projection_state.py --json` 预览，确认备份目录后才使用 `--apply --backup-dir <dir> --json`。验收必须同时满足 full/incremental/isolated comparator 相等、六类 receipt gap 为 0、Wiki/KG ANN label 与向量语义审计通过。完整契约见 `docs/WIKI_PROJECTION_LIFECYCLE.md`。

## 提取格式硬校验与自我修正

- `prompts/distill/extract/base.md` 与 `prompts/distill/_output_schemas/extract.json` 已明确硬校验要求：`title` ≥10 字符；`core_content` ≥100 字符且必须包含标题或代码块；`frontmatter` 必须含非空 `摘要`（≥5 字符）和 `领域`（≥2 字符）。
- `core/hephaestus/distillation_extractor.py::KnowledgeExtractor.extract()` 在首次提取未通过硬校验时，会把错误列表回传给 LLM 进行一轮自我修正（配置项 `distill.extract_correction_retries`，默认 1；设为 0 可关闭）。
- 历史测试产生的 stale `distill_failed` JSON 与对应 `recap_tasks.db` pending 记录，已通过临时脚本清理/标记为 `resolved`，不再触发高优先级复盘提醒。

## 文档与 Prompt 资产闭环

- `docs/acceptance/document_asset_manifest.json` 是 tracked Markdown、Prompt/schema 和 Desktop `mnemos系统图谱` 的 canonical 分类契约；`scripts/audit_document_asset_manifest.py --strict` 必须保持 repo Markdown、Prompt/schema、Desktop 三类 `unverified=0`。
- freshness 与 sensitive 审计必须复用 Git tracked Markdown 自动发现，禁止重新维护不完整的硬编码文档路径集合。exclude 必须逐项写 owner/reason/未过期日期；当前 exclude=0。
- Prompt 模板/schema 改动必须同步精确 SHA-256、实际 consumer symbol 与 output contract；orphan/stale/hash/consumer/schema 漂移 fail closed。
- Desktop `00–10` 当前契约必须在同一 `Current claim evidence:` 行同时引用 `99-代码扫描-facts.json#current_state` 和至少一个存在的 repo 代码/正式契约锚点；`86–98` 生成索引必须在头部绑定当前 repo commit。full-score 的 `docs.asset_manifest.strict` 使用 required Desktop profile。

## 认知来源权限（COG-044）

- `core/evidence/source_authority.py` 是蒸馏来源权限的唯一 owner；权限固定为 `system_policy/explicit_user/project_contract/assistant_inference/tool_observation/external_content/quoted_content`。模型只能选择 exact `source_authority_id`，无法确定时必须省略并由系统按 quote 唯一解析，不得猜 ref 或自填/升级 authority/purpose/eligibility/hash/role/span。
- `DistillInputSpec v4` 必须把 role-local Raw span、artifact summary 和系统入口 metadata 编成不可变 `SourceAuthorityCatalog` 与 `CognitionExtractionContext`，catalog/context hash 进入 input spec、Prompt 与 checkpoint execution identity。用户/系统高权文本中的 Markdown blockquote、代码围栏、行内代码及中英日韩成对引号必须拆为 exact `quoted_content` 子 span；缺少 role-local message 的 detached 格式化输入只能是低权引用，不能猜成 explicit user。quote 必须存在于所选 exact span；助手/工具角色或 external/quoted metadata 不能被 caller metadata 提升为用户/系统权限。
- `external_content/quoted_content/assistant_inference/tool_observation` 可保留为普通可检索知识或低权待验证假设，但不能单独创建 observation/reflection/policy/persona/reinforcement 或自动化派生 proposal；更新、合并、强化型动作转入 authority-pending shadow。只有 `system_policy/explicit_user/project_contract` 的精确证据可授权认知更新。
- Canonical Raw 永远完整保存用户、助手、工具和外部材料；prompt-injection 检测仅作非阻断标签，不得在 Raw 前删除或截断。Observation 的用户认知投影只读取 user span；assistant bytes 仍留在 Raw，外部文档只进入 Attention 类信号。
- 修改该链路后必须运行 `python3 scripts/audit_cognitive_source_authority.py --strict --json`，要求七类 catalog 分母与多语言/编码引用 corpus 完整、`unauthorized_cognitive_update_count=0`、embedded quote unauthorized=0、`high_authority_trace_gap=0`、外部知识保留且 Raw blocking site 为 0。

## 测试、门禁与诊断状态边界

- `python3 scripts/run_tests.py quick|integration|heavy|full` 和 `scripts/run_full_score_gates.py` 必须使用 `core.ops.hermetic_run.HermeticRunEnvironment`；不得手写第二套 HOME/MNEMOS/XDG/temp 重定向。
- 唯一已实现 profile 是 `isolated`。每轮必须使用不存在或为空的唯一 `sandbox_root`，所有日志/报告也是其子路径；manifest 必须输出 `environment_hash`、`outside_write_count` 和 `formal_state_diff`。
- quick/integration/heavy/diagnostics/非 real-api full-score 默认不继承 API 凭据；只有用户显式选择 `--real-api` 时可传递受控凭据。测试不得依赖开发机 key、真实模型或网络，应注入确定性 provider/client。
- health、status、distill status、verify、golden 默认只读；缺目录/库/表时报告未初始化，不得 mkdir/DDL/写 metrics。写权限探针只能由 `scripts/verify_installation.py --write-probes` 显式开启并使用唯一 `O_EXCL` 文件。
- pytest 会直接阻断正式暂停库、配置、golden latest、KG/metrics/projection/ANN 写入；发现失败应修正实际路径/依赖注入，不得删 guard、扩大预算、清正式账本或伪造 consumer receipt。
- quick/integration/heavy 的文件分母由 `scripts/audit_test_suite_denominator.py --strict --json` 校验；pytest 默认的 `test_*.py` 与 `*_test.py` 必须 100% 且恰好归属一层，missing/extra/overlap 都失败。
- `--strict --real-api` 是唯一 certifying profile，拒绝 `--only` 和全部 skip 选择器。发布报告必须是 `mnemos.full_score_gates.v2`，其 expected/selected/executed 集合完全相等、omitted 为空、工作树干净且绑定完整 commit；发布者必须再运行 `scripts/verify_full_score_certificate.py <full_score_gates.json>`。partial diagnostic 即使全部通过也只能 `certifying=false/release_eligible=false`。
- 安全审计机器报告固定为 `mnemos.security_audit.v2`。所有 Bandit、pip-audit 与 health security 风险必须先归一化为带 `source/code/severity/message/repair_action` 的 typed finding，再由 finding 集合唯一推导 `blocking_count`、`warning_count`、`status`、`ok` 与进程退出码；强制保持 `ok == (blocking_count == 0)`，不得只追加 `errors` 或依赖子命令返回码。发布聚合器必须运行 `python3 scripts/security_audit.py --strict --json` 并调用同一 validator 复核 schema、counts、findings 与退出状态；任何 blocking finding 都阻断 release，warning 只能保留为显式非阻断证据。

## Relation evidence schema authority（ROOT-20260710-019）

- `knowledge_graph.db.relation_evidence` 的唯一 DDL/version/hash owner 是 `core/kia/relation_evidence_schema.py`；`KnowledgeGraph` 与 `RelationManager` 只能调用 `validate_existing_relation_evidence_schema()` / `initialize_relation_evidence_schema()`，不得恢复各自的 `CREATE TABLE IF NOT EXISTS relation_evidence`。
- 构造器在任何其他 DDL 前验证已存在表；未注册的旧 KnowledgeGraph schema、旧 RelationManager defaults schema、索引缺失、registry/hash 不符或未知结构必须 fail closed，并提示显式 reconciliation。不得在启动时静默 ALTER、填默认值或猜测 `evidence_type`。
- 存量迁移先停止 daemon，运行 `python3 scripts/reconcile_relation_evidence_schema.py --json` 预览；确认 `null_evidence_type_count=0` 后才用 `--apply --backup-dir <dir> --json`。apply 必须先以 SQLite backup API 生成 integrity=ok 备份，事务迁移/登记后验证 row count、columns/FK/index、`mnemos.relation_evidence_schema.v1` 与 canonical hash；失败自动 rollback，未知/空类型数据留给人工分类。
- 本地、pre-commit、CI 与 full-score 均运行 `python3 scripts/audit_schema_registry.py --strict --json`，要求生产 DDL owner 恰好一处且 live/已初始化 DB 的实际 signature 与 registry 一致。相关改动至少覆盖两种初始化顺序、两种旧 schema、NULL/unknown/index/registry corruption、迁移失败回滚、CLI 备份和完整 Quick。

## 复盘消费与纠错闭环

- `recap_finalize` 和 `recap_skip` 只在 `recap_consumption_plans -> recap_consumption_commands -> recap_consumption_receipts` 的全部 required target 到达 `committed` 或有证据的 `intentional_skip` 后进入 `consumed`；target label、页面存在或 plan 落表本身都不是消费证明。
- target 必须经 canonical registry 映射到 `knowledge_retrieval/policy_patch/follow_up/persona/scheduler/scoring`；未知 target 在 plan 接受前拒绝，alias 不得重复执行同一 effect。
- `recap_feedback` 使用 append-only correction outbox。负反馈必须撤销、抑制或补偿所有已提交 effect；partial、stale processing 和重启只重放缺失 receipt。冲突反馈必须通过 `supersedes_event_id` 精确引用最新事件。
- 修改该链路后至少运行 `tests/integration/test_recap_consumption_runtime_e2e.py`、`tests/integration/test_recap_feedback_correction.py` 和 `tests/unit/test_reconcile_recap_consumption.py`。生产 schema 对账先运行 `python3 scripts/reconcile_recap_consumption.py --json`，停 daemon、确认备份范围后才使用 `--apply --json`，最后再次 dry-run 并逐库执行 `PRAGMA integrity_check`。

## PolicyPatch 相关性契约

- `PolicyPatchStore.active_for()` 只能用当前 task/subtype/context 和显式 scope 证明命中；禁止把 patch content 当作 trigger 语料。
- 非 global patch 必须与调用方显式 project scope 精确匹配。候选先按 task-fit 和当前上下文命中的 trigger 排序，再去重并执行 `policy_patch.max_active` 干扰预算；KIA 响应必须保留 `match_source=current_context`、`matched_triggers`、`task_fit_score`、`dedupe_key` 和 `interruption_budget_ok`。
- Reflection 的 `key_points` 是解释内容，不得生成 trigger。trigger 必须是短、稳定、可在当前任务中独立证明的激活词；历史清理使用 `python3 scripts/reconcile_policy_patch_triggers.py` 预览，显式 `--apply` 时必须先备份数据库，不得编造替代 trigger。
