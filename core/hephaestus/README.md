# core/hephaestus — 蒸馏子系统

Hephaestus（希腊神话中的火神与工匠之神）是 Mnemos 的**知识冶炼厂**：把原始 AI 对话、外部文档等原始材料，经过多层过滤、价值判断、LLM 提取、硬校验、跨 Agent 关联，最终写入结构化的 Obsidian Wiki。

> **设计原则**：蒸馏执行权在 Mnemos，直接调用 LLM API，宿主 Agent 不执行蒸馏。

---

## 目录

- [设计目标](#设计目标)
- [七层蒸馏流水线](#七层蒸馏流水线)
- [模块地图](#模块地图)
- [两条关键路径](#两条关键路径)
- [对外接口](#对外接口)
- [测试入口](#测试入口)

---

## 设计目标

1. **自动**：Agent 对话结束后无需人工干预即可触发蒸馏。
2. **可靠**：通过硬校验（hard validation）确保输出质量，不通过则自我修正或失败隔离。
3. **可追溯**：每个 Wiki 页面都能追溯到原始 L1 对话或文件。
4. **防污染**：通过 `RecirculationGuard` 防止 Wiki 内容再被蒸馏回知识库。
5. **可演进**：支持版本绑定、时间范围检测、跨 Agent 关联。
6. **终态可证明**：只有 durable page 或 explicit intentional skip receipt 才结束任务；proposal、partial、retry、intercept 和 write failure 都保持非终态。

---

## 七层蒸馏流水线

| 层级 | 文件 | 职责 |
|---|---|---|
| L1 噪音过滤 | `distillation_engine.py` | 剔除过短、无意义、高度重复的原始内容。 |
| L2 价值预判 | `distillation_value_judge.py` | 快速规则/轻量模型判断内容是否值得蒸馏。 |
| L3 LLM 判断 | `distillation_llm.py` | 调用 LLM 评估知识价值与类型。 |
| L4 知识提取 | `distillation_extractor.py` | 调用 LLM 抽取标题、摘要、核心内容、标签等。 |
| L5 自检/准入 | `distillation_self_check.py`、`distillation_quality.py`、`cognitive_value_gate.py` | 硬校验输出 schema、长度、必填字段，并在普通质量门后判断认知贡献。 |
| L6 跨 Agent 关联 | `distillation_cross_linker.py` | 关联其他 Agent 的相似主题页面。 |
| L7 反馈循环 | `distillation_feedback.py` | 根据入库后质量和用户反馈更新评分模型。 |

---

## 模块地图

| 文件名 | 核心类/函数 | 一句话职责 |
|---|---|---|
| `distillation_engine.py` | `DistillationEngine` | 七层流水线核心引擎，编排整个蒸馏过程。 |
| `prompt_builder.py` | `PromptBuilder`, `TokenBudget` | 蒸馏 Prompt 构造、模板继承回退、Token 预算管理；内容格式化统一使用 token 预算。 |
| `distillation_extractor.py` | `KnowledgeExtractor` | L4 知识提取：LLM + assertion_extractor 硬校验。 |
| `response_budget.py` | `ResponseTokenLimits`, `resolve_response_token_limits()` | 对话蒸馏 LLM 输出预算四档：default 6000、medium 8000、long 12000、length retry 16000。 |
| `fragment_merger.py` | `FragmentMerger` | 跨 chunk 知识片段聚类合成完整 Wiki 页面。 |
| `evolution_tracker.py` | `TemporalEvolutionTracker`, `RecirculationGuard` | 版本绑定、时间范围检测、防循环蒸馏。 |
| `document_pipeline.py` | `DocumentDistillationPipeline` | 外部文件（PDF/Word/PPT/Excel/Book/HTML）深度蒸馏。 |
| `document_processor.py` | `DocumentProcessor` | 文件内容解析与预处理入口；验证使用当前本地规则入口，旧 AgentDelegate 委托验证模式已退役。 |
| `wiki_builder.py` | `WikiBuilder` | L1 → Wiki 共享工具、批量回追、索引/MOC 更新；`--watch` 支持 `--once`、`--max-cycles`、`--run-seconds`、`--interval`，可被 CI/agent 一轮验证。 |
| `wiki_rebuild.py` | — | Wiki 重建/回追脚本入口；dry-run 与执行报告会展示候选页面的 L1 记录数。 |
| `quality_gate.py` | `QualityGate` | 通用片段质量门，输出 accept/review/reject。 |
| `cognitive_value_gate.py` | `CognitiveValueGate` | 认知贡献门，识别 decision/method/anti-pattern/preference/relation/evidence/future-trigger 等贡献类型。 |
| `distillation_models.py` | `KnowledgeFragment`, `DistillationResult` | 知识片段与蒸馏结果统一数据模型。 |
| `distill_input_spec.py` | `DistillInputSpec`, `ExtractionRequest`, `PreparedExtractionPrompt` | 在 Prompt 渲染前冻结 source agent/session/event、完整度、可见输入 hash、gate id、输入模式、系统 catalog 与 `CognitionExtractionContext`；模型只能回显合同字段并选择 opaque evidence ref，不得猜测身份。 |
| `distillation_contract.py` | `validate_extraction_output()` | 以 `extract.json` 的 Draft 2020-12 根 discriminated union 同时校验 Prompt、修正、检查点与正式写入。 |
| `cognition_episode_validation.py` | `validate_cognition_episode_draft()` | 校验 19 个 typed episode 字段、claim 映射和 exact evidence 分母；具体 evidence identity 仍委托外层不可变 catalog 校验。 |
| `../cognitive/cognition_extraction_context.py` | `CognitionExtractionContext` | 系统侧封存 exact Raw span、agent/role、完整性、ACL、用途、保留策略和 catalog hash，模型只有引用选择权。 |
| `../cognitive/cognition_episode_persistence.py` | `commit_cognition_episode()` | 在任何 Wiki/action sink 前，把 admitted `mnemos.cognition_episode.v2` revision（含完整 claims catalog 与 user behavior intent）、event envelope 和三个 projection outbox 原子提交到唯一 `CognitiveStateStore`；历史 v1 只读兼容。 |
| `behavior_intent.py` | `infer_behavior_intent_signal()` | 将 `ContentSource`、`UserIntent` 与 `IntentRouter` 的系统侧预判注入提取 prompt，作为 `distill_output_v4.user_behavior_intent` 的 meta 输入。 |
| `distillation_text.py` | — | 文本清洗、分块、合并工具；`build_session_text` 使用 `max_tokens` / `per_message_token_limit`。 |
| `distillation_json.py` | `JsonExtractionResult` | JSON 输出解析与校验；返回 direct/markdown/balanced/fixed/failed 路径元数据，fallback 成功不写 warning。 |
| `distillation_metrics.py` | — | 蒸馏 JSON 解析质量指标；写入 redacted `distill_json_parse_events` 并供 health 展示趋势。 |
| `distillation_quality.py` | — | 质量评分辅助函数。 |
| `distillation_prejudge.py` | — | 预判断逻辑。 |
| `distillation_pause.py` | — | 流水线暂停/恢复控制；`mnemos status` 会展示暂停原因、恢复时间、API 链与最后错误。 |
| `distillation_failure.py` | — | 失败处理与重试策略；失败文件包含结构化错误元数据，同类格式失败复盘按错误指纹聚类。 |
| `distillation_errors.py` | — | 蒸馏相关异常定义。 |
| `distillation_write_receipt.py` | `persist_with_receipt()` | 持久化后将页面、intentional skip、trusted proposal、partial、retry 和 failure 分类为 typed terminal/nonterminal receipt。 |
| `distillation_page_identity.py` | revision-aware page identity helpers | 以 source/session/input revision 生成稳定页面 identity，重试幂等且新 revision 不覆盖旧 generation。 |
| `tokenizer.py` | `Tokenizer` | 基于 Token 的文本拆分。 |
| `content_expression.py` | — | 内容表达式/规则引擎。 |
| `link_probe_worker.py` | — | 链接探测 worker。 |

`DistillationResult` 是蒸馏报告与序列化输出契约。`data_profile`、
`anomalies`、`needs_reconfirm`、`reconfirm_question` 和
`prejudgment_confidence` 即使没有普通代码直接读取，也必须保留给调用方、
审计报告和后续 UI/CLI 展示层。

---

## 两条关键路径

### 路径 A：对话蒸馏

```
对话结束
    ↓
Amphora (core/kia/amphora.py) 入队
    ↓
DistillationEngine 读取 L1 原始对话
    ↓
Tokenizer 分 chunk
    ↓
七层流水线：过滤 → 预判 → LLM判断 → 提取 → 自检 → 关联 → 反馈
    ↓
硬校验 + QualityGate + CognitiveValueGate 通过 → Charon 路由写入正式 Wiki 目录；无法确定分类或正式区同名冲突才留 00-Inbox（写入 source_event_ids / evidence_refs / gate_decision_id / cognitive contribution / quality_gate_action_ledger_ref / Wiki 路由状态）
硬校验失败 → distill_failed/ + 自我修正重试
    ↓
CrossAgentLinker 建立跨 Agent 链接
    ↓
KGEventHandler / EntityManager / RelationManager 更新图谱
```

该路径的“完成”由 `DistillationWriteReceipt` 判定，而不是由函数正常返回或空列表判定。durable page 与 explicit intentional skip 才是 terminal success；proposal pending、partial、retry、intercept、写页失败保持 Amphora 可处理状态，且不得提前把 L1 标为 `distilled`。trusted enforce 只有 proposal decision 和 target receipt 均持久化才完成。

Capture outbox 的 `meta.raw_event_refs` 为每个 turn 保存 immutable `revision_id`、logical alias、content hash 和字符 span。Amphora 接收后写 `amphora_task` provenance edge；`DistillationEngine` 把同一 refs 写入 fragment/Wiki frontmatter，正式页面落盘后再写 `wiki_page` edge。缺失或空 span 在调用 LLM 前直接失败，不能先写页面再补证据；引用 edge 同时参与 raw retention。

ROOT-005 起正式 extract 输入遵循 lossless contract：`clean_message_content()` 只排除显式 private thinking，不压缩长代码块、连续 shell 命令、编号/空行或首尾可见格式；WikiBuilder plaintext fallback 传递完整正文。`_extract_standard()`/`_extract_chunked()` 调用 `build_session_text(..., lossless=True)`，总预算与单消息预算只记录 overflow，不执行 head-tail/消息截断；`message_truncations` 对 private exclusion 只保存类型、span、计数和 fully-excluded 状态。预算不足必须通过 `Tokenizer.split_to_tokens()` 继续分 chunk。摘要/评分/skill suggestion 的显式预算结果不是 canonical extraction input。

ROOT-014 起 `distill_execution_spec.py` 是分块恢复 identity 的唯一 owner。`DistillExecutionSpec` 冻结并 canonical serialize 精确 prompt hash、schema hash、extractor contract hash、backend/model identity、merge contract 与 37 个输出相关配置；`build_chunk_fingerprint()` 再组合 lossless chunk input 与 spec hash。`KnowledgeExtractor` 只渲染一次 prompt 并用同一字符串执行，行为意图预判强制 rule-only，cache hit 不得先触发额外 LLM。检查点表以 `(session_id, chunk_index, chunk_hash)` 保存多代结果，typed lookup 输出 `cache_hit/miss_reason/spec_diff_fields`；旧 schema、损坏 spec/payload 均不复用，新 spec failed 行不能覆盖旧 completed 行。迁移入口 `scripts/reconcile_distill_execution_checkpoints.py` 默认只读，`--apply` 必须先生成并验证 SQLite 备份。

COG-011 起，conversation extract 的根返回是 `distill_output_v4` 的严格 discriminated union，而不是“空 fragments 即 skip”：Engine 在渲染 Prompt 前构造不可变 `DistillInputSpec`，其中绑定 `source_agent`、`source_session_id`、`source_event_ids`、`raw_completeness`、`visible_input_sha256`、`gate_decision_id` 和 `input_mode`；`ExtractionRequest` 与预渲染 Prompt 都必须匹配这份 spec。合法 skip 必须同时是 `judgment=skip`、`fragments=[]`、`structured_output.distill_intent=skip`、`claims=[]`，并带非空 `skip_reason` 和至少一条引用 `source_event_ids` 的 `no_value_evidence`；知识/技能分支则必须有至少一个可解析的 fragment、非 skip intent、非空 claims 与 `user_behavior_intent`。`extract.json` 自身还拥有 unknown 行为意图、source-authority/artifact ref、relation target/delta/reason/action 及高价值 claim cognitive actions 的条件格式规则，不能只靠 Prompt 提示或 router 补救。运行时在首次输出进入修正前、每次修正后、保存检查点前和读取检查点后都执行同一份 schema 加 typed validator；非法空知识输出会走有限次修正，绝不被偷换为 skip，合法 skip 才直接准入。

COG-028 起，模型调用的 canonical evidence port 是 `DistillBackendResponse`，不是 parsed dict。Extractor 会把 source/claim/action/artifact/fragment 的 schema 与 semantic errors 原样送入 bounded correction，并把每次 response evidence 附到 `ExtractionOutcome`；Engine 防御性复验失败时保存最后可用 raw response，或保存明确的 `transport_empty`，同时记录 provider/model/request ID/finish reason/parse path/attempt history、prompt hash、input-spec hash 与 response hash。外部 `distill_output` 文件收集器、parser-unavailable 放行、基础 frontmatter raw fallback 和相关配置预算已删除；daemon 的 active queue owner 固定为 `daemon/distill_service.py -> HephaestusWorker.process_all -> DistillationEngine` 一条。`scripts/audit_distill_output_contract.py --strict --json` 同时审计 contract drift 和 `active_owner_count=1`。

COG-029 起，`DistillInputSpec v2` 还拥有不可变 `ArtifactCatalog`。Capture outbox 与完整 Session handoff 都先把 artifact 绑定 authoritative Raw revision；系统再按 type + 完整 SHA-256 生成稳定 `artifact_ref_id` 和 content-addressed URI，并按当前 chunk 的 revision span 收窄 allowlist。文件型 ref 现场读回，pathless tool result 对 canonical inline payload 重算；缺 payload、hash/type/ACL/source 不符或 malformed ref 会在模型前整体拒绝。Prompt 只暴露 ref、类型、窄脱敏摘要和允许的 source event，不暴露 inline payload、本机路径、URI、hash 或 ACL；模型 evidence 只能选择 `artifact_ref_id`。Extractor 在首次响应和每次 correction 后解析 ref，canonical root 才携带系统 URI/type/summary/hash/MIME/ACL。catalog/URI resolver 代码纳入 execution-spec hash；未知、伪造、跨 chunk、越权、type/hash 不一致或模型自填 identity 都拒绝；同字节换路径或轮次不改变 ref/checkpoint identity。

每次准入得到 typed `ExtractionOutcome`，其中 `canonical_output`、`canonical_output_hash`、judgment 和 admission 共同证明根返回未被解析层替换。分块检查点必须持久化 `CheckpointAdmission(input_spec_hash, output_contract_version, canonical_output_hash, judgment)`，并让 `chunk_info` 同步记录这些字段；save 与 lookup 都必须携带完整 `DistillInputSpec` 重跑 union validator，缺失 root/admission 的旧行、input spec 或输出契约变化、root/fragment/hash 损坏都只能 miss 后重提取。正式 Wiki 写入会再次验证 canonical root、hash、judgment、structured output 与 `DistillInputSpec` 的一致性；`create_page` 还必须携带 Engine 在受控 merge/link/quality 末段签发的 `FragmentRouteCapability`，它将 root/input hash 与可写 fragment 对象引用 tuple 绑定，拒绝 direct caller 在准入后替换片段。发布配置还必须保持 `distill.structured_output_contract.enforce=true` 和 `distill.action_router.enabled=true`，由 `python3 scripts/audit_distill_output_contract.py --strict --json` 审计；开发期关闭其中任一开关不能取得 release 资格。

COG-010 起，所有非 skip 根必须输出完整的 19 个 typed 字段；COG-015 起 canonical store 使用 `mnemos.cognition_episode.v2`，并把已准入的完整 `claims`、`claim_catalog_hash` 和 `user_behavior_intent` 一起冻结，避免 Wiki 消费后 canonical store 丢失可检索语义。`known` 只接受当前输入内 exact Raw span 和 admitted claim，`unknown/not_applicable` 只能写原因，不能夹带 value/evidence/claim；`situation/facts/scope` 至少各有一条 evidence-bound known，全部 claim 必须被 episode 字段映射。模型只能选择当前 `CognitionExtractionContext` 的 opaque ref，不能生成 agent、span、ACL、retention 或 artifact identity。正式持久化先原子提交 canonical episode revision、event envelope 与 `wiki/knowledge_graph/cognitive_graph` outbox，再允许 action 或 Wiki 写入；路由端会反查 committed revision，伪造/缺失/异库 revision 均 fail closed。历史 v1 revision 只读兼容，新写入只能使用 v2。`tests/fixtures/cognition_episode_golden/manifest.json` 固定 decision、failure correction、preference boundary、no-conclusion 四类验收分母，`scripts/audit_distill_output_contract.py --strict --json` 要求 100% 字段有效、Raw/context mismatch 为 0，并拒绝 forged、cross-chunk 和 all-unknown 绕过。

`distill_output_v4` 中的 `source_event_ids`、claims evidence、`gate_decision_id`、`raw_completeness` 和 `distill_intent` 会进入蒸馏页面 frontmatter 与“来源追踪”正文，保证普通新建知识页能从 Mnemos Vault 反查 raw evidence。问题 29 起，非 `skip` 输出还必须包含 `user_behavior_intent`：prompt 会注入 `ContentSource`、`UserIntent` 与 `IntentRouter` 预判，LLM 必须写出用户为什么引入/需要这条知识、意图证据、后续验证/修正事件、状态和置信度。COG-044 起，外部文件、结构化引用、助手推断和工具观察都不能单独证明用户意图；缺少精确高权证据时预判保持 `unknown/unverified` 且置信度不高于 `0.3`，低权知识仍可检索但只能进入 Attention 或 pending hypothesis。问题 30 起，`resolve_response_token_limits()` 的四档输出预算统一为 `6000/8000/12000/16000`，`distillation_engine.RESPONSE_TOKENS` 兼容回退也提升到 6000；短会话不再沿用旧 4000 输出上限，`finish_reason=length` 重试升到 16000。Wiki frontmatter 和来源追踪会展示行为意图和来源权限；只有 `explicit_user`、`system_policy` 或 `project_contract` 的精确证据能够授权 active cognitive derivative。

2026-07-04 起，普通质量门通过后还会运行 `CognitiveValueGate`：低认知贡献但格式良好的片段会被拒绝或进入 pending verification，高价值但证据不足的片段进入 review；正式入库页面必须写出 `cognitive_value_*`、`cognitive_contribution_types`、`cognitive_consumers` 和 `quality_gate_action_ledger_ref`，并把最终 accept/review/reject 记录到全局 `ActionLedger(action_type=quality_gate)`。问题 22 起，高价值 claim 必须写 `cognitive_actions`，`DistillActionRouter` 会把 observation/reflection/policy/methodology/pitfall/relation/reinforcement 候选写入 `cognitive_action_log` 并生成 `mnemos.distill_cognitive_action.v1` artifact；`mnemos health --json` 的 `checks.distill_cognitive_actions` 可只读汇总动作计数、状态计数和 artifact 数量；普通技术事实没有动作时页面会标记 `cognitive_action_status=ordinary_knowledge`。问题 23 起，`DistillationEngine.write_pages()` 和 `DocumentDistillationPipeline.write_to_wiki()` 会先用 Charon `resolve_page_folder()` 解析写入目录，可确定分类时直接写 `01-People/02-Projects/03-Tech/04-Concepts/06-Retrospectives/99-Reports` 等正式区，不确定或正式区 basename 冲突才写 `00-Inbox`，并在 frontmatter 写 `Wiki路由状态/原因/目标`；表达格式建议默认写 `表达格式`，正文格式化仍由 `distill.auto_expression_formatting` 控制。多模态或工具证据由模型选择 catalog ref，系统解析出 `mnemos-artifact://content/sha256/...` URI、类型和摘要；它仍不能替代 `source_event_id` 与短 quote。Wiki 只渲染系统解析后的摘要链接，不嵌入原始截图、终端全文或测试报告正文。

### 路径 B：文件蒸馏

```
Agent 调用 document_process(file_path) MCP tool
    ↓
DocumentImportService / FileIngestor 完整提取并写 1 个 canonical raw revision
    ↓
capture outbox 持久交接给 Amphora（公开入口返回 accepted/pending）
    ↓
Hephaestus 文档流水线 → 硬校验/认知价值门 → Wiki
```

raw projection 是 Obsidian raw vault 的唯一 writer。默认入口不调用 StorageBackend direct save，也不直接投递 Amphora；重复文件复用同一 raw revision/capture event/handoff。`DocumentProcessor` 只负责解析及显式内部直出蒸馏，旧 `--save`、`save_to_backend()` 和 L1 分片旁路已删除。

---

## 对外接口

- `DistillationEngine.process(session_id, ...)` — 蒸馏单个会话的实例入口。
- `distill_session(session_id, ...)` — 蒸馏单个会话的模块级便捷函数。
- `distill_and_write(session_id, ...)` — 推荐高阶便捷入口；完成蒸馏、写页、`knowledge_distilled` 事件/同步兜底，并触发 Embedding 增量索引。
- `DocumentDistillationPipeline.process(file_path, ...)` — 蒸馏外部文件。
- `WikiBuilder.run_build_cycle(...)` — 批量回追 L1 → Wiki，流水线为唯一支持模式。
- `PromptBuilder.render(task, context, ...)` — 构造蒸馏 Prompt。
- `QualityGate.evaluate(fragment, ...)` — 质量门决策。
- `CognitiveValueGate.evaluate(content, frontmatter=..., lifecycle_signals=...)` — 认知贡献准入决策。

`HttpApiHostAgentCaller(force_provider=...)` 中，`None` / `auto` / `api` 使用默认 API chain；传入具体 provider 名（如 `dmxapi`、`siliconflow`、`openai`）会收窄候选配置，未匹配时返回明确错误。

---

## 测试入口

```bash
# Hephaestus 单元测试
pytest tests/unit/hephaestus/
pytest tests/unit/test_distillation_engine.py
pytest tests/unit/test_cognitive_value_gate.py
pytest tests/unit/test_prompt_builder.py
pytest tests/unit/test_fragment_merger.py
pytest tests/unit/test_evolution_tracker.py
pytest tests/unit/test_document_pipeline.py
pytest tests/unit/test_wiki_builder.py

# Hephaestus 集成测试
pytest tests/integration/test_distillation_engine_pipeline.py
pytest tests/integration/test_distill_worker_loop.py
pytest tests/integration/test_document_processor_pipeline.py
pytest tests/integration/test_distill_terminal_states.py
pytest tests/integration/test_recap_trusted_completion.py
```
