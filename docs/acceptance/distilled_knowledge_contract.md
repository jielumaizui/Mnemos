# Distilled Knowledge Contract

状态：第 1/3 步验收产物。

本文档定义 raw 进入 Mnemos 知识仓库后的结构化输出契约。可执行定义位于 `core/agent_kit/acceptance_contracts.py` 的 `DISTILLED_KNOWLEDGE_FIELD_CONTRACTS` 和 `DOWNSTREAM_LINK_FIELD_CONTRACTS`；运行 `python3 scripts/verify_acceptance_contracts.py` 可校验覆盖。

## 蒸馏字段

| 字段 | 来源 | 含义 | 必填性 | 缺失降级策略 |
|---|---|---|---|---|
| `title` | `KnowledgeExtractor` fragment title | 可读知识页标题 | required | 拒绝 fragment 并要求修正 |
| `core_content` | fragment core content | 足够上下文的正文 | required | 拒绝 fragment；修正失败写 `distill_failed` |
| `frontmatter` | fragment frontmatter + wiki writer | wiki 页 YAML metadata | required | 写入前拒绝 |
| `摘要` | `frontmatter.摘要` | 搜索和人工扫描摘要 | required | 拒绝并要求修正 |
| `领域` | `frontmatter.领域` | 路由和聚类领域 | required | 拒绝并要求修正 |
| `tags` | fragment tags/keywords/frontmatter | 搜索和组织标签 | required | 仅当 entities/领域 存在时可为空 |
| `source_sessions` | raw session ids + distillation context | 支撑页面的 raw 会话 | required | 阻止写入，保证可追溯 |
| `source_agent` | raw `source_agent` | 主要来源 Agent | required | 多 Agent 时显式写 `mixed` |
| `raw_event_refs` | Capture handoff / Amphora meta | 支撑当前结果的 immutable `revision_id`、logical alias、content hash 与非空 span | required for captured conversation distillation | 缺失/非法 span 在 LLM 前失败；不得退回 session surrogate |
| `distill_input_spec` | `DistillInputSpec v4` | 在 Prompt 前冻结 `source_agent/source_session_id/source_event_ids/raw_completeness/visible_input_sha256/gate_decision_id/input_mode`、artifact/source-authority catalog 与 cognition extraction context 的不可变输入身份 | required for live conversation extract | 缺失、与可见输入 hash 不同或由模型猜测时拒绝提取、checkpoint 和正式写入 |
| `cognition_extraction_context` | `mnemos.cognition_extraction_context.v1` | 系统拥有的 agent/role/origin/authority、exact Raw revision/span、ACL/purpose/retention、完整性/损失合同和当前 chunk catalog oracle；模型只回显 hash 并选择合法 ref | required before every extract prompt | 任一 context/catalog/hash/Raw oracle 漂移，或模型伪造/跨 chunk ref 时拒绝 |
| `source_authority_catalog` | `SourceAuthorityCatalog` | 以 role-local Raw span / artifact summary 生成系统权限引用；结构化引用/代码按原始 offset 拆为 `quoted_content`，detached 无 role proof 输入默认低权；模型只选确定的 ref，否则系统按 quote 唯一解析，再解析七类 authority | required for claim/intent evidence and cognitive actions | quote/role/span 不匹配或多匹配、external/quoted metadata 被覆盖升级、模型猜 ref/自填权限、低权证据触发 active cognition 时拒绝或转 pending hypothesis |
| `root_output_admission` | `ExtractionOutcome.canonical_output` + `canonical_output_hash` + admission | 原始根返回、judgment、structured output 与解析 fragments 的一致性证明 | required before checkpoint/formal write | 仅 fragments、旧 list-return 或 hash/judgment/structured-output 不一致时失败，不能视为 skip |
| `evidence_refs` | `distill_output_v4` claims.evidence/source_event_ids | raw evidence id、短引文和可选 artifact URI | required for non-skip output | 非 `skip` 输出缺失时阻止写入 |
| `artifact_refs` | `distill_output_v4` claims.evidence[].artifact_uri + raw metadata.artifact_refs | 从 raw capture 带入 claim evidence 的标准化 artifact URI | conditional | 只有支撑证据不涉及多模态/工具 artifact 时可为空 |
| `user_behavior_intent` | `distill_output_v4.user_behavior_intent` + `ContentSource`/`UserIntent`/`IntentRouter` meta | 用户为什么引入/需要这条知识、意图证据、后续验证/修正事件、状态和置信度 | required for non-skip output | 非 `skip` 缺失时拒绝写入；未知时必须显式 `unknown/unverified`；外部文件只证明材料被提供，缺少精确高权证据时不得自动提升意图或置信度 |
| `cognition_episode` | `distill_output_v4.cognition_episode` + claims + user_behavior_intent → `mnemos.cognition_episode.v2` | situation、goal、desired_state、facts、assumptions、hypotheses、causal_links、alternatives、tradeoffs、decision、rationale、actions、outcomes、root_cause、correction、supersedes、uncertainty、invalidation_conditions、scope 的完整 typed 认知链，以及不可变 claims catalog/hash 和行为意图 | required for every non-skip output | 19 字段任一缺失/空、known 无 exact evidence/claim、unknown/NA 携带断言、核心 grounding 全 unknown、claim 未被 episode 映射或 catalog hash 漂移时拒绝；历史 v1 仅只读，不能伪造完整 |
| `skip_reason` / `no_value_evidence` | `distill_output_v4` skip branch | 无长期价值的原因和至少一条来自 `source_event_ids` 的最小证据 | required for skip output | `fragments=[]` 或 `claims=[]` 单独出现不构成 skip；缺任一字段拒绝准入 |
| `entities` | fragment keywords/concepts + KG extraction | KG、搜索、reranker 可用实体 | required | 从关键词/概念派生或低置信度留空 |
| `relations` | fragment relations + KG relation builder | 与既有知识或实体的结构化关系 | required | 可为空；后续隐藏关系建议补齐 |
| `confidence` | claims.confidence + quality gate | claim 置信度 | required | 默认低置信并路由 review |
| `cognitive_actions` | `distill_output_v4.claims[].cognitive_actions` + `DistillActionRouter` | 高价值 claim 需要触发的下游认知动作，如 observation/reflection/policy/methodology/pitfall/relation/reinforcement | required for high-value claim types | 缺失时拒绝高价值 claim；普通 technical_fact/entity/open_question 可标记 `ordinary_knowledge` |
| `cognitive_contribution_types` | `CognitiveValueGate` + fragment content/frontmatter | 页面贡献给认知系统的类型，如 decision/method/anti_pattern/preference/relationship_update/evidence/future_trigger | required for formal wiki page | 低认知贡献拒绝；高价值但证据不足进入 review |
| `cognitive_consumers` | `CognitiveValueGate` | 预期消费方，如 preflight/guard、persona/KG、wiki search | required for formal wiki page | 缺失时进入 review 或拒绝正式入库 |
| `quality_gate_action_ledger_ref` | `DistillationEngine` + `ActionLedger` | 写入前最终质量门/认知价值门决策的全局账本 id | required when runtime config has `database_dir` | ledger 不可用时只允许降级记录 warning，不改变准入结论 |
| `wiki_route_status` | `core/vaults/page_routing.py` + Charon resolver | Wiki 写入路由结果：`direct` 表示直写正式目录，`inbox` 表示留待复核 | required for created wiki page | 不确定分类、resolver 失败或正式区 basename 冲突时写 Inbox 并记录 `wiki_route_reason` |
| `expression_format` | `ContentExpressionFormatter` | Obsidian 展示建议，如 checklist/table/flow/config/plain | required for created wiki page | 正文格式化关闭时仍写建议值，正文保持原样 |
| `embedding_status` | embedding index build/update | embedding 生命周期状态 | required | 置为 `pending` 并入队 |
| `distill_status` | distillation queue/sync_log/wiki writer | 蒸馏生命周期状态 | required | 写 `failed` artifact，不静默丢弃 |
| `input_revision` | Amphora task identity | 当前 source/session 输入修订，与 generation 一起绑定输出 | required | 缺失时不得提交终态 receipt |
| `write_receipt` | `DistillationWriteReceipt` + cognition episode commit + Wiki writer/trusted push | canonical episode、durable pages、intentional skip、proposal、partial、retry 或 failure 的结构化结果 | required | 非 skip 必须先提交 canonical episode revision/event/outbox；失败时阻断全部 action/Wiki sink 并保持 retryable |
| `terminal_reason` | terminal receipt classifier | 说明终态来自 durable page 或 explicit intentional skip | required for terminal success | proposal/partial/retry/intercept/write failure 均不得终结任务 |

## 下游关联字段

| 字段 | 来源 | 含义 | 必填性 | 缺失降级策略 |
|---|---|---|---|---|
| `kg_entity_refs` | entities/frontmatter/KG event handler | KG 实体 id 或名称 | conditional | 页面仍可检索，KG 更新标记 pending |
| `kg_relation_refs` | relations / `relation_to_existing` | KG 关系 id 或 typed payload | conditional | 空列表，后续关系建议补齐 |
| `embedding_ref` | embedding index manager | 向量或索引引用 | conditional | `embedding_status=pending/failed` |
| `reranker_features` | frontmatter、summary、persona、freshness | context search/reranker 特征 | conditional | 回退到 keyword/semantic score |
| `observation_refs` | L3 observation store/events | 观察 id 到 page/raw evidence 的连接 | optional | 不触发 observation bridge，页面仍有效 |
| `persona_alignment` | persona scoring/frontmatter | 个性化检索和行为提示信号 | optional | persona score 默认 0 |

## 通过标准

- 正式 conversation extract input 必须保留除显式 private thinking 外的全部可见代码、命令、附件占位与格式字节；长消息用 token split 进入多个 chunk。canonical builder 必须使用 `lossless=True`，在极小总预算/单消息预算下仍保持 `truncated=false`、`silent_omission_count=0`，并只记录不含正文的 exclusion 类型/span/计数。cleaner 不得生成 `lines/commands omitted`，WikiBuilder plaintext fallback 不得固定截取字符前缀。检查点哈希与 `chunk_info` 必须包含当前 `lossless-visible-v1` 输入契约；缺少该版本的存量结果只能 miss 后重跑，禁止原地伪造版本继续复用。
- 分块恢复必须把精确渲染 prompt、输出 schema、extract/parse/quality 代码、backend/provider/model route、merge 合同与全部输出相关有效配置收敛为不可变 `DistillExecutionSpec`。checkpoint 只有 chunk input、execution spec、完整 `DistillInputSpec` 与 output admission contract 同时相等并重跑 canonical union validator 才能命中，并输出 `execution_spec_hash/prompt_hash/schema_hash/model_id/cache_hit/miss_reason/spec_diff_fields` 以及 `input_spec_hash/output_contract_version/canonical_output_hash/output_judgment`。写入端也必须先以同一 immutable spec 重验 root；旧 schema、损坏 spec/payload、缺少显式 `checkpoint_identity()`、缺 root output/admission、input-spec/contract 漂移或 root/hash/fragment binding 不一致必须 fail closed；新规格 failed 不能覆盖旧 completed。intent hint 预判必须 rule-only，cache identity 计算不得产生额外 LLM 请求。
- `prompts/distill/_output_schemas/extract.json` 是 `distill_output_v4` 的唯一根 union，`validate_extraction_output()` 用同一份 Draft 2020-12 schema 和 typed validator 在首次模型输出进入 correction 前、每次 correction 后、checkpoint 保存/读取和正式写页前执行。合法 skip 必须同时满足 `judgment=skip`、`fragments=[]`、`structured_output.distill_intent=skip`、`claims=[]`、非空 `skip_reason` 与至少一条引用 `source_event_ids` 的 `no_value_evidence`；knowledge/skill 必须有至少一个 admitted fragment、non-skip intent、非空 claims 与 `user_behavior_intent`。schema 的条件分支还拥有 external-file/unknown 行为意图限制、`artifact_uri` 的 type/summary 依赖、relation 的 target/delta/reason/action 约束，以及高价值 claim 的 `cognitive_actions` 要求；不能依赖 prompt 文案或下游宽松处理代替这些格式校验。非法空 non-skip 只能 bounded correction 或失败，不得被泛化为 skip。
- 每个 accepted root 都必须以 `ExtractionOutcome` 的 canonical root/hash、judgment、structured output 和 `DistillInputSpec` 形成 admission proof；正式写入前再次验证这些字段完全一致。`DistillActionRouter` 的 `create_page` 还必须收到 Engine 在受控的 merge/link/quality 末段签发的 `FragmentRouteCapability(root_hash,input_spec_hash,object_refs)`；它只接受该 tuple 的有序、无重复对象身份子序列，因此同对象格式化合法，调用方替换 `result.fragments`、缺 capability 或根/input 绑定漂移一律拒绝。发布/strict profile 必须保持 `distill.structured_output_contract.enforce=true` 与 `distill.action_router.enabled=true`，由 `python3 scripts/audit_distill_output_contract.py --strict --json` 复核；关闭任一项只能产生 non-certifying 诊断。
- 非 skip admitted root 在任何 action/Wiki sink 前必须由 `commit_cognition_episode()` 写入唯一 `CognitiveStateStore`：typed revision、`CognitiveDataEvent` envelope 与 `wiki/knowledge_graph/cognitive_graph` outbox 在同一 transaction 内提交。ledger 只保存 envelope/receipt，Wiki/KG/CognitiveGraph 只消费 committed revision id，不能持有第二份可改 canonical state；路由必须反查 revision 的存在性和 object type。四类 golden corpus 的 57/57 字段必须有效，exact-span/context mismatch 为 0，forged ref、cross-chunk ref 和 all-unknown 非 skip 均必须被拒绝。
- `judgment=skill` 必须先提交包含完整 admitted root、全部最终 fragments、chunk aggregate、Raw source spans 和 private ACL 的不可变 cognition asset，再派生 versioned proposal，最后走普通 Wiki/action-router 与 Wiki/search 投影。资产、proposal、页面各自写 receipt；proposal `optional_failed` 不回滚资产或页面，asset 未提交则不得写 proposal/Wiki、不得 processed。`skill_suggestion` 仅为已提交 proposal 的显示字段。资产 payload 对个人隐私、API key/凭据、银行卡和密码执行 `pii_credentials_only_v1` 脱敏，不加密、不截断其他可见内容；`skill_asset_without_cognition` 必须为 0。
- 对新 Capture 数据，输出必须能回溯到 immutable raw `revision_id + span`；`source_sessions` 只作导航字段，不能单独充当最终 evidence id。历史页无法证明精确 span 时必须进入 provenance gap/rebuild 状态。
- 蒸馏成功必须有与 task/input revision 匹配的 durable page receipt，或带明确 reason 的 intentional-skip receipt。空页面列表、写入失败、intercept、partial、retry，以及尚未裁决/尚未落目标的 trusted proposal 都是非终态，不得把 Amphora 标记 `done` 或把 L1 标记 `distilled`。
- trusted shadow 只生成 proposal/metrics 时不得阻断已经成功的 legacy page write；trusted enforce 的 proposal 必须在 decision 与 target receipt 齐全后才可完成。相同 active proposal 应复用 idempotent receipt，rejected/failed 后允许新 proposal。
- recap confirmed/consumed 必须引用 committed page receipt、已完成的 trusted proposal receipt，以及全部 required canonical target 的 committed/intentional-skip receipt；plan 或 target label 落表不算消费。finalized 页面缺失、consumer partial 或 correction receipt 未齐时必须保持 retryable，负反馈要能撤销、抑制或补偿既有 effect。
- 普通新建知识页必须把 `source_event_ids`、`evidence_refs`、`raw_completeness`、`gate_decision_id` 和 `distill_intent` 写入 frontmatter 或来源追踪正文，不能只停留在 LLM JSON 结果里。
- 普通新建知识页必须把 `user_behavior_intent` 的摘要、来源、意图假设、意图证据、验证事件、状态和置信度写入 frontmatter/来源追踪正文；页面首屏必须能说明“用户为什么需要/引入这条知识”。`SourceReader` 回读 Wiki 时必须把这些字段恢复成 `content_source` 与 `user_intent`，供 Observation/persona/reflection/cognitive decision 消费。
- 普通新建知识页必须经过 noise、value prejudgment、hard schema、`QualityGate` 和 `CognitiveValueGate` 五层准入；正式入库页面必须写出 `认知价值门禁状态`、`认知贡献类型`、`认知消费者` 和可反查的 `质量门禁账本ID`。
- 写入前最终 accept/review/reject 决策必须记录为 `ActionLedger(action_type=quality_gate)`，verification 至少包含 session、fragment index、最终 disposition、通用质量门状态和认知价值门状态。
- `distill_output_v4` 的高价值 claim 类型（preference/procedure/decision/constraint/pattern/anti_pattern/relationship/meta）必须带非空 `cognitive_actions`；每个最终 fragment 必须以非空、去重的 `claim_ids` 精确覆盖 admitted claims，禁止用标题或数组位置猜测 claim↔fragment。`DistillActionRouter` 先提交不可变父动作/意图，再生成 `mnemos.distill_cognitive_action.v2` artifact 和带 lease/retry/dead 状态的子命令；只有 Observation/Reflection/PolicyPatch/Relation 目标服务写出 reciprocal effect receipt、稳定 effect id 与 before/after hash 后，命令才能进入 `applied`。action DB 自签消费、关闭 router 直写 Wiki、shadow/proposal 派发正式子命令和 replay 重复效果均被拒绝。存量先停止 daemon，运行 `python3 scripts/reconcile_cognitive_action_effects.py --json` 预览，确认备份后才使用 `--apply --process --backup-dir <dir> --json`；发布门禁运行 `python3 scripts/audit_cognitive_action_effects.py --strict --json`，要求 `applied_without_effect=0`、`effect_without_action=0`、target receipt/state/hash 缺口和 lineage gap 全为 0。`mnemos health --json` 必须通过 `checks.distill_cognitive_actions` 暴露动作、效果和缺口计数。
- 普通 `technical_fact`、`entity`、`open_question` 允许不带 `cognitive_actions`，但正式页 frontmatter 必须标记 `认知动作状态: ordinary_knowledge`。
- 新建 Wiki 页必须先经过 Charon 写入路由：可确定分类时直接写正式目录；无法分类、resolver 错误或正式区同 basename 冲突时写 `00-Inbox`，并在 frontmatter 标记 `Wiki路由状态/原因/目标`。正文展示建议必须写 `表达格式`，但是否重排正文由 `distill.auto_expression_formatting` 控制。
- 格式完整但只像普通参考资料、没有决策/方法/反模式/偏好/关系更新/证据/未来触发场景的片段不得直接成为正式知识；高价值但来源证据不足的片段只能 pending verification。
- claim evidence 可带 `artifact_uri`、`artifact_type`、`artifact_summary`、`artifact_sha256`、`artifact_mime_type`；`artifact_uri` 必须使用 `mnemos-artifact://<agent>/<session>/turn/<turn_number>/<artifact_type>[/<index>]`，并且不能替代 `source_event_id` 和 `quote`。
- Obsidian 正文只展示 artifact 摘要和链接，不嵌入截图、完整终端输出或测试报告正文。
- 普通知识页、冲突页、强化记录都必须遵守 `distill_output_v4` 的 evidence 和 action 约束。
- `distill_output_v4` 的 `recommended_action` 必须进入 `DistillActionRouter`，并写入 `distill_action_log` / `knowledge_action_log`；低置信或高冲突 merge/update 不得直接改正文。
- embedding、KG、reranker、observation、persona 缺失时必须有显式状态或可解释降级，不能表现为“成功但没有数据”。
