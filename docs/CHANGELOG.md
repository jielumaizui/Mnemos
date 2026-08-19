# CHANGELOG

> 单一事实源 — 所有历史修改、审计、集成报告统一归档于此文件。
> 更早的零散报告原文已合并至此后删除（2026-05-02）。
> 当前系统版本口径统一为 **Mnemos v2.0.0**；本文按日期与能力记录变更，不再使用旧实验版本号作为阅读入口。

---

## [Unreleased] — Vault 展示/分类/结构化治理（2026-07-03）

- 修复 Phase 4 COG-050 派生认知投影的永久 report 豁免、直接文件写入、失败吞没和“重建反写 canonical”问题：新增 typed `DerivedProjectionLifecycle`，把 L2.4–L5 的 generation manifest、canonical revision/content hash、create/update/delete、原子发布和 EventBus trace 绑定到现有 `WikiProjectionLedger`；A→B→A replay、第二文件失败、publisher 失败、stale delete 与重启续跑均有回归。Observation、Reflection、KG、Persona 全量投影只删除 lifecycle 已证明所有权的页面，未绑定 Markdown 保留给显式 reconciliation；read-only replay store 拒绝 cursor/save/entity mutation，Vault sync 覆盖 CognitiveGraph canonical hash 且任一层失败时不提交 Vault Git。Persona calibration 改为 canonical commit 后重放，画像 alignment 跳过全部派生认知根。新增独立 strict audit 并接入 local/pre-commit/CI/full-score。按当前用户边界没有执行生产 rebuild/replay，也没有创建或清理备份；主库已只读验证 SQLite integrity、foreign key、schema 与最新 migration/generation transaction 状态，生产 projection residual 仍为 binding 480、stale 480、required-consumer receipt 9,204，明确保留 `RUNTIME_REBUILD_PENDING`。最终关联分母为 `559 passed`；一次隔离 full 为 `7556 passed, 4 failed, 15 subtests passed`，4 项均属于 EventBus 生成图未同步或拆分后测试 owner/参数断言滞后，随后精确 `4/4` 与三模块 `103 passed`。按高成本测试策略未重复 full，因此该证据不冒充 clean full 或 release certificate。现有备份只有在未来主库同时通过物理完整性与投影语义收敛后才可进入清理候选。
- 修复 Phase 4 COG-015“认知只在 Wiki 可读、混合检索先截断后鉴权、无法证明命中来源”的问题：canonical CognitionEpisode 升级为 `mnemos.cognition_episode.v2`，在 19 字段之外冻结完整 claims catalog/hash 与 user behavior intent，历史 v1 只读兼容；Wiki 投影展示全部字段和来源。新增 typed `CognitiveSearchHit`，由 canonical cognition、CognitiveGraph、EvidenceGraph 与 Wiki 独立召回，在 ACL/purpose 后 oversample/refill 并确定性融合，应用层按 channel/object/current revision 二次授权；snippet 围绕真实 match offset，返回 matched field、source revision/span 与 ACL decision。语义 ANN 和 CognitiveGraph ACL 路径移除 top-N-before-ACL 截断。新增 36 条正向（28 holdout）+ 7 条负向冻结 hermetic benchmark 与 production-answer leakage scan，并纳入 local/pre-commit/CI/full-score。生产 Wiki ACL 在停止 writer 后以备份完成显式修复，无法证明来源的页面保持 `restricted_unknown` quarantine；旧超大 entropy frontmatter 也以备份收敛为 count/range/hash + SQLite locator。ACL/entropy 文件迁移现与 Wiki lifecycle mutation、durable pending event 和两库回滚原子绑定。canonical state 检索另使用 `mnemos.cognitive_search_state_headers.v4`：ACL header 与独立 immutable binding 均在 revision 提交事务内写入，binding insert 必须校验 canonical revision 的 ACL preimage、payload hash 与 identity；三条授权入口只读取小型投影，ACL 通过后才 hydration 正文。生产 v4 对账为 revisions=3103、headers/bindings=1534、typed exclusions=1569，coverage/hash/schema/current gap 均为 0，备份完整性为 ok。授权、独立投影审计与索引代际持久化已拆为单一职责 mixin/support，四个生产文件均回到 1,500 行预算内；maintainability、zombie、CI ratchet、mypy、flake8、security 与可信写入扫描均为零债务。clean-commit Quick 首轮进一步暴露旧 demo-fixture reconciler 只认 v1；现已同时精确验证 v1/v2，并要求 v2 claim、catalog hash、user behavior intent 与 Raw/authority 证据完全匹配，非 demo 元数据继续 fail closed。第二轮又暴露 Wiki 二次授权已拒绝缺失 ACL metadata 的结果、却丢弃精确拒绝计数；现会把 Wiki 与 cognitive channel 的授权原因分别、累加回传，不改变 fail-closed 判定。生产 strict 审计仍如实只失败于 `runtime_channel_population`：Wiki=728，canonical cognition/CognitiveGraph/EvidenceGraph 均为 0；因此状态是代码/合同闭环、真实 population 未发生，不构成发布证书。
- 修复 Phase 4 COG-030 认知语义只停留在 Wiki/KG 拓扑的问题：蒸馏在 canonical `CognitionEpisode` revision 提交后只由一个 durable dispatch owner 发布 ID-only、versioned `cognition_episode_committed`，固定 fan-out 到 `wiki/knowledge_graph/cognitive_graph`，删除 `knowledge_distilled` 路径上的同步认知双写。三个消费者返回 typed `HandlerOutcome`，目标库先提交稳定 effect、manifest、before/after hash、ACL hash 和 omission receipt，state store 再以 command+consumer 唯一 receipt 闭合；失败、进程崩溃和重启只重放缺失 effect。EventBus 新增跨进程 claim、可续租 lease、fencing epoch 与 cognition terminal 唯一约束，且该 canonical 事件永久保留，不受普通历史事件清理影响。EvidenceGraph 固化 `RawRevisionSpan→Observation→Claim/Belief→Decision→Prediction→Action→Outcome` 的 canonical 方向，新增 Episode/Belief/Decision/Prediction/Action/Outcome、ACL 与来源 omission；独立审计不导入 producer oracle，直接重算事件、命令、图节点/边、effect/receipt 和 evidence direction。生产五库在停止 daemon/MCP writer、校验 inventory `sha256:0cc2ed29b586f01f7e92bb923dee9c1fb2744a94d896925e5eed2afb68f95696` 后完成 SQLite backup-protected reconciliation，备份位于 `~/.mnemos/backups/cog030-20260720-0cc2ed29`；逐库 snapshot hash 与 integrity 均一致，二次 dry-run inventory `sha256:9e0de187911b6abf988293821b0520122095c177dd57de01a1ed3fb5fca46ad7` 为零动作，现存单个退休 synthetic fixture 仅写 3 条 typed intentional-omission receipt，不伪造图投影。最终独立 dispatch/direction audit 全 gap 为 0，EvidenceGraph 为 138,671 nodes / 149,668 edges；isolated Quick 为 `6974 passed, 2 skipped, 15 subtests`，`outside_write_count=0`、`formal_state_diff=[]`。local gates 除本次变更前已存在的 `raw_quality_to_distill_gate` 1,763 条生产 backlog 外全部通过；该 backlog 继续独立阻断 release，不以 COG-030 关闭冒充全仓发布通过。
- 继续收口 Phase 3 后的整仓阻断：将 13 个 mixin 的 282 个 mypy 错误归零；修复 cognitive-state 重建时 retirement proof 丢失、health evidence 泄露嵌入路径、PDF malformed 文档边界、迁移运行态隔离、provider exception alias provenance，并拆除 KIA projection、reminder rendering 与 source-span migration facade 的 import cycle；canonical dependency/EventBus 文档已重生成。历史 Amphora 队列另新增 fail-closed source-span reconciliation：Capture→Raw 前置修复 451/451 后，对 1,749 个可证明任务完成 SQLite 备份、计划绑定与 exact span 迁移，复验 `missing_span_tasks=0`、`candidate_tasks=0`、blocked=0。typed terminal 对账先恢复 1,696 条缺失 receipt，并用 canonical Wiki create→delete lifecycle 证明唯一缺页对象；随后 strict runtime audit 发现旧 migration retirement 错把 reconciliation script 记成 1,737 个 consumer，且通用 reconciler 将 1,631 个旧 generation 的 4,377 个 cognitive event 误记为 `distill intentional_skip`。v2 修复不删改历史行：追加 1,737 个 canonical skipped/supersession receipt 与 4,377 个 `revoked + reopen_required` cognitive correction。后续 v3 又把缺少显式 `supersession_reason` 的早期 successor 视为未纠正，以新 idempotency generation 追加 exact successor 并同时 supersede 旧 recorder/v2 receipt；SQLite backup/integrity 均为 ok。当前 1,763 个 runtime pending/overdue 与 4,447 个 cognitive missing terminal（其中 4,377 等待 replacement 真实处理）继续作为发布阻断，不由 migration skip 冒充完成。
- 完成 Phase 3 最后一项 COG-048 trustworthy training governance：`TrainingGovernanceStore` 成为训练 admission、split、correction、run、model activation 与 reciprocal receipt 的唯一 owner；正式 producer、Chronos scheduler、daemon worker、model reader、optimizer/Bayesian effect 全部改走 governed seam，旧 `ground_truth_signals`、queue、feedback、model、Bayesian state 与 rule optimizer 接口永久 fail closed，不能通过 alias、caller permit 或兼容开关复活。生产训练历史按精确对象级 provenance 盘点 25,139 行，inventory `sha256:edc48c2dcb39f3f406a83652d5e6e2a67f446fd75f76aef5bdf28f2d95b63fec`、object manifest `sha256:d1f03b507addd5e2820bc6d3b277d966be840ca70825ce37d9159bc2ac4ea1b3`；完成 sealed backup/apply/真实 restore/reapply/zero-insert replay，最终 covered=25,139、uncovered/invalid/unexpected=0、active promotion=0，25 项 strict 指标全为 0。COG-035/036/037/038/043/044 复验通过；双轴 review 无 finding；当前改动文件回归 `649 passed`，完整 isolated Quick `6822 passed`、Integration `349 passed`、Heavy `19 passed`。全局 maintainability、mypy、security、runtime backlog、zombie/vulture 与 release certificate 仍是独立阻断项，不以 Phase 3 关闭冒充发布通过。
- 复验 COG-036 时发现 2026-07-18 旧代码窗口留下 12 条 delivery event 缺少 exact non-material/material provenance proof；当前 writer 已具备正确 proof，因此未放宽代码合同，而是按方案 A 追加对象级历史 quarantine。三域最终 denominator 146,967（ActionLedger 1,966、delivery 2,325、formal mutation 142,676），inventory `sha256:238be50c6fda42b62a528fcac975207d271174f70881303daea7c4ac0b9dc747`、manifest `sha256:5b86a8307bb937d50a85254396c447de0e013fb01c488a31524b4d72e838cfc1`；apply 新增 15 个 exact historical identity，随后真实 restore、reapply 和前后快照/报告 hash 相同的 replay。最终 uncovered=0、七项 DecisionTrace 指标=0、33/33 material sink guarded，target integrity=`ok`、`migration_required=false`。
- 实现并完成 COG-038 canonical feedback attribution：所有正式 reaction 入口统一写 append-only `UserReactionEvent` 与独立 attribution revision，客观 `OutcomeMeasurement` 继续由 COG-037 单独负责；weak interaction 不再直接写 scorer ground truth、trust penalty、persona、policy、reflection 或 optimizer。private attribution identity 同时绑定 subject/scope/principal/agent，recap/dialog source scope 不能被 caller 重绑定；eligible proposal 必须先通过真实 `PushDecisionGate`，再以 exact DecisionTrace/material action permit 和 reciprocal terminal receipt 闭环，成功 effect receipt 只能经 specialized state API 与 domain adapter 独立复核。available decision/prediction/action ref 在 writer 与独立 audit 中都必须解析到真实 canonical revision/action spec，并复核 prediction principal/project/session 与 prediction→decision/action 绑定；不存在的 ref fail closed。project scope 可在空 session ID 时用独立 exposure identity 满足 weak materiality 的“session 或 exposure”阈值。exact replay 先验证既有 domain/material terminal，禁止 wall-clock 变化触发第二次 DecisionTrace。correction 会沿完整 attribution chain 找到 active effects，在同一 UOW 原子关闭旧 pending commands，并要求 domain owner 的 revoke/compensate/suppress receipt 后才允许 replacement。recap correction 真实撤销 retrieval、policy、follow-up、persona、scheduler 与 legacy scoring effect；legacy scorer/reflection readers 只排除退役 feedback object type，保留正常对象的 `source_event_id` lineage。生产 `producer_consumer_ledger.db` 已显式升级到 canonical v3；三域历史 inventory `sha256:0b9854759e4ea51696063152c32caea635f18388e212b0f7a55dd53a70569b15` 与 object manifest `sha256:6c307444608e13a6d39330b3b2eb86b983599b1ae1a2c4f7589604a5d490806b` 的 3,625 个对象已完成 backup/apply/restore/reapply/replay，最终 covered=3,625、uncovered=0、active promotion=0，幂等 replay 为 inserted=0/existing=3,625；strict audit 全指标为 0，六库 integrity 均为 `ok`。最终双轴 review hard/spec finding 均为 0；clean-commit isolated Quick `6822 passed`、Integration `350 passed`、Heavy `19 passed`，三层均为 `outside_write_count=0`、`formal_state_diff=[]`。该关闭不代表 COG-048、其余 Phase 3、全局 maintainability/zombie 或 release certificate 已完成。
- 实现 COG-037 canonical PredictionLedger：每个 predictive `deliver/silent/suppress` 在 route effect 前冻结不可变 PredictionRecord，material delivery 与 DecisionTrace/action command 同事务，suppression 先 seal 再写 reciprocal projection receipt；成熟服务只产生 `measured/unknown/censored/confounded` 终态，OutcomeMeasurement 必须由固定 TaskResultOracle 从 exact canonical Raw tool observation 签发并绑定 prediction/decision/action/delivery、source authority 与写 ACL，reaction/timeout 不再伪装 objective outcome。calibration 每次从 Raw、authority catalog、oracle issuance 与 committed receipt 重新验证，不信任存量 `eligible`；终态 projection receipt 精确绑定 canonical revision ID/hash/state、target、确定性 before/after hash 与 reciprocal refs，独立审计可识别篡改。新增 categorical confusion-matrix/coverage report、terminal correction、`prediction_ledger.py` + `prediction_ledger_support.py` + `prediction_outcome_support.py` 三文件实现 identity、五对象历史 provenance quarantine migration、restore/reapply/replay 和独立 strict lineage audit，并接入 local/pre-commit/CI/full-score；生产 inventory `sha256:f11e1e0a48082ab56cf7b3be4754028a9f420aaf559b9090d7699523d763696a` 已完成授权 apply、manifest restore、最终 reapply 和零变更 replay，active PredictionRecord 仍为 0。最终 COG-037 review 双轴 clean，isolated Quick `6734 passed, 15 subtests`、integration `350 passed`、Heavy `18 passed`；全局 maintainability/release closure 仍由其他 Phase 3 债务阻断，不以本项冒充发布通过。
- 收敛 COG-037 全量 integration 暴露的相邻契约缺口：SourceAuthority 的 Raw decoder 改为惰性导入以保持蒸馏入口 import purity；Hephaestus trusted-push 在正式 Wiki 动作前封存精确 project-contract DecisionTrace，并按 source revision 安全复用 exact retry；`needs_manual_review` proposal 可幂等复用；旧 Capture handoff 只在 visible message identity 精确相等时重绑原任务；relation embedding 首个初始化共享 `knowledge_graph.db` 时先安装 canonical material-effect schema。
- 修复 COG-036 决策记录与实际行动断裂：在唯一 `CognitiveStateStore` 上新增 system-owned `ValueContext`、pre-action `CognitiveStateSnapshot`、`DecisionTrace` 与 atomic seal，identity、candidate/value/conflict/hard-constraint、snapshot/ref hash 和 commit order 均由 canonical coordinator 生成并可重算。所有 material sink 改为严格消费绑定 decision/action/target/executor/precondition 的 single-use permit，目标库写 reciprocal terminal effect，失败、重放、漂移和多 terminal 都不能伪装成功；delivery 的 verified non-material suppression 另有 typed proof，不进入 action denominator。ActionLedger、delivery event 与 formal cognitive mutation 三域历史对象只按 object-level provenance 写 `historical_incomplete` quarantine，不从文本、metadata 或 actor 猜 decision。新增四库 material-effect schema reconciliation、三域 dry-run/备份/apply/restore/replay 工具、七项零缺口 strict audit，并接入 local、pre-commit、CI 与 full-score required gate。
- 修复 COG-035 缺少权威 BeliefRevision：在唯一 `CognitiveStateStore` 上新增 append-only belief revision 状态机，系统派生 belief/claim identity 与 stance，保留支持/反对 evidence、scope、valid time、confidence method、uncertainty、纠正/supersedes lineage；冲突形成 `disputed`，未知、过期、否定与废弃保持不同语义。`CognitiveGraph` 只消费 committed outbox 并以 exact effect receipt 证明投影，重试不会产生第二 current head。历史 Wiki page、relation 与 Reflection 只按对象级 source identity、字段分母和内容 hash 写 `unverified_candidate` quarantine，不从正文猜 belief。新增 dry-run/备份/apply/replay 迁移工具和 `audit_belief_revision_lineage.py --strict --json`，并接入 local、pre-commit、CI 与 full-score required gate。
- 修复 COG-049 Observation 校准的派生证据双计票和内存态漂移：以 canonical Raw revision + exact SHA-256 建立独立 lineage cluster，同根 Raw/Wiki 去重，多根派生汇总改为 non-voting overlay；直接 delta 累加改为可重算 weighted evidence shrinkage。新增 typed `CalibrationRecord`，绑定稳定 Observation ID、脱敏前 measurement digest、canonical peer/source 顺序、lineage、validator/combiner 实现与 spec、prior/posterior、支持/反证簇、时间窗和 omission receipt；validator 缺陷、重复 identity、实现源码不可读和精确来源缺失都失败关闭，并通过唯一 `CognitiveStateStore` 原子提交 revision/event/outbox 后才允许 `observations.db` 的 verified base 绑定 current posterior。superseded receipt、直接 binder、无 record 投影以及会制造 orphan record 的清理均被拒绝。全量/增量 Wiki 重放 committed record，显示 Observation/Calibration/source-span identity；旧 schema 只能在 daemon 停止、SQLite 备份验证后显式 reconcile，无法证明 prior 的旧值标成 `historical_unverified`，不伪造 base 或 record。新增独立 strict audit 并纳入 local/pre-commit/CI/full-score，canonical 发布分母增为 47 gates；隐私仅脱敏个人标识、API key/令牌、银行卡、密码/私钥，不加密。
- 修复 COG-047 通用账本不能证明不可变认知状态：新增唯一 `CognitiveStateStore`、typed revision schema 与 `CognitiveStateUnitOfWork`，在同一 `producer_consumer_ledger.db`/`BEGIN IMMEDIATE` 中原子提交 semantic revisions、`CognitiveDataEvent` envelope 和 local outbox；consumer terminal 改为 event × consumer append-only head，ActionLedger 改为拒绝 UPDATE/DELETE/同 ID 覆盖。`MnemosServiceFacade` 新增零写入 `build_cognitive_state`、原子 `record_decision` 与 `apply_outcome`；旧语义 metadata 只迁为非 active `historical_candidate`，缺字段数据隔离而不编造。构造器不再静默建表，迁移必须 dry-run、停 daemon、备份、apply、second dry-run。持久化只窄脱敏个人标识、API key/令牌、银行卡、密码/私钥，不增加加密。
- 修复 COG-044 来源权限混淆：`DistillInputSpec v3` 绑定 system-owned `SourceAuthorityCatalog`，以 role-local Raw span、artifact summary 和结构化引用 offset 区分 `system_policy/explicit_user/project_contract/assistant_inference/tool_observation/external_content/quoted_content`。模型只能引用唯一匹配的 opaque ref，不能自填或升级权限；外部/引用/助手/工具内容保持 lossless 与可检索，但无法单独触发 belief/persona/policy/reinforcement/automation 派生。外部文件行为意图不再强制 `curate_or_decision_material >= 0.7`，缺少精确高权证据时保持 `unknown/unverified <= 0.3`；认知动作写门、skill proposal、Raw/Wiki 认知投影和独立审计同步 fail closed。
- 修复 COG-029 artifact identity 交给概率模型：新增 system-owned `ArtifactCatalog` 与 `DistillInputSpec v2`，Capture/outbox 和 SyncEngine complete-session handoff 统一把 artifact 绑定 authoritative Raw revision；按 type + 完整 SHA-256 生成 path-free opaque ref、content URI 和 chunk-local allowlist。文件现场读回、pathless tool result canonical inline payload 重算，caller marker/自报 SHA 不再可信；复用 Raw revision 强制核对 source/session/turn/hash，handoff 回读 header hash，malformed refs 保留到 pre-model gate 拒绝。模型 schema 只允许选择 `artifact_ref_id`，Extractor 在 correction/admission 前解析 URI/type/summary/hash/MIME/ACL；伪造、跨 chunk、越权、类型/hash 漂移或模型自填 identity fail closed。catalog/URI 代码进入 execution-spec hash，相同内容换路径/轮次保持 checkpoint hit；摘要只做个人隐私、API key/令牌、银行卡、密码/私钥窄脱敏，不加密。
- 修复 COG-028 structured contract 错过 correction loop：backend/extractor 正式端口改为 `DistillBackendResponse`，完整保留 raw/parsed/usage/provider/model/request/finish/parse/attempt/hash；source、claim type、action、artifact type 和 fragment 错误在首次输出及每次修正后复用 canonical schema + semantic validator。最终失败 artifact 绑定 prompt/input-spec/response hash，空传输显式记录；隐私仅做个人信息、API key/令牌、银行卡、密码/私钥窄脱敏，不加密。删除 external `distill_output` collector、弱 validator、parser-unavailable/raw fallback 与配置预算，配置经备份迁移清理；新增静态门保证 active daemon owner 恰好一个且 typed port 不回退。
- 修复 COG-014 认知动作“自签 applied 但无真实效果”：fragment/claim 改为显式 `claim_ids` 全覆盖，父动作/意图/命令/attempt/effect/consumption 由 `mnemos.distill_action_store.v2` 统一持久化；worker 使用 lease/retry/dead，仅在 Observation/Reflection/PolicyPatch/Relation 目标服务提交 reciprocal receipt、稳定 effect id 与 before/after hash 后标记 applied。关闭 action router 不再直写正式 Wiki，shadow/proposal 不派发正式子命令，replay 保持单效果。新增只读审计和备份迁移；本机 142 个父动作、201 个历史命令迁移为 201 个真实效果和 201 个互惠回执，独立审计全部 lineage/target/hash gap 为 0、integrity=ok。full-score 分母增为 46 gates。
- 修复 COG-013 skill 蒸馏提前返回：`judgment=skill` 现在先把完整 admitted root、全部 fragments、chunk aggregate、Raw source spans 与 private ACL 写入 `CognitionAssetStore`，再生成 versioned `CognitiveDecisionAssetProposal`，并继续走普通 Wiki/action-router 与 Wiki/search 投影。资产、proposal、页面使用独立 typed receipt；proposal 失败不回滚资产/页面，asset 未提交则禁止 processed。`skill_suggestion` 降为显示兼容字段，移除 `skill_suggestion_max_chars` 运行语义并忽略旧配置。持久化仅按 `pii_credentials_only_v1` 脱敏个人隐私、API key/凭据、银行卡和密码，不做加密；回归覆盖尾部证据、ACL/source span、proposal/asset 失败和 `skill_asset_without_cognition=0`。
- COG-018 模型调用账本实现契约（静态实现；本机演练见下一条）：`ModelCallLedger` 以 `RuntimePaths.model_call_ledger_db` 统一预留、发送标记、provider-meter usage 结算、退款与保守 incurred-cost 保留；完整 provider-visible request 以 UTF-8 字节**上界**预留，SDK retry/HTTP redirect 在边界关闭，3xx 不可在一条预留下二次 POST。账本不保存原始 prompt、response、调用方错误文本或 preview，只保留本地 opaque reference、审核过的操作元数据和 exact entry-level subject relation；freeze、tombstone 与 in-flight deletion block 防止删除重置预算。旧 prompt-call owner 的注册迁移先输出 `execution_plan_hash`，非 clean apply 必须传完全相等的 `--expected-plan-hash`，缺失/漂移零写入 blocked；clean 状态零写入 noop。apply 使用正常本地 SQLite backup、完整性/漂移检查和 sealed `mnemos.model_call_ledger_recovery.v3` manifest，rollback 先预览、只有显式 apply 才恢复。legacy entry attribution 未知或不可恢复 tombstone history 只能经审阅后显式 discard，不能由 run root 推断。`scripts/audit_model_call_ledger.py` 对 direct provider sink 做静态路径对账；它和代码测试不替代真实库的 plan/apply/noop/health/restore 演练证据。`secure_delete`/WAL→DELETE 只覆盖 active SQLite 文件的本地清理，不是备份、快照、副本或 provider 记录的取证级抹除承诺。
- COG-018 结构与本机演练收口（2026-07-14）：实现已下沉到 `core.telemetry.model_call_ledger`，`core.telemetry.prompt_call_log` 只做静态兼容导出；reconciler 下沉到 `core.migrations.model_call_ledger_reconcile`，独立脚本只读且 direct `--apply` 无 capability、零写入 blocked。sealed-v3 manifest 连同普通 SQLite backup/hash/lock 都是本地恢复正确性机制，按实际 in-scope target 绑定 SQLite sidecar，orphan/缺失/漂移/篡改 fail closed。账本仅对个人隐私、API key、银行卡信息、密码、raw prompt/response 和 caller error 做不持久化/脱敏。isolated Quick 已通过 `6156 passed, 15 subtests`；本机 daemon stopped 后完成 registered apply、ledger health/plan verified、v3 restore、registered reapply、最终 plan/health verified。该运行证据不构成全仓 release certificate。
- 修复 ROOT-20260710-023 Windows CI 默认 PowerShell 无法解释 POSIX env prefix/`mktemp`：删除 workflow 内联沙箱命令，新增 `scripts/run_tests.py system` 精确层，让 Linux/macOS/Windows matrix 统一复用 `mnemos.hermetic_run_environment.v1`、Python `tempfile` 与无 shell argv 执行。新增契约测试阻断 `mktemp`/`MNEMOS_DIR=$(...)` 回流、`shell=True` 和 system 测试面漂移。代码提交 `d136e60a`；focused 14、相关宽回归 45、真实 system `6 passed`，manifest `outside_write_count=0`、`formal_state_diff=[]`；最终 Quick `5776 passed, 15 subtests passed`。本地门禁除 Desktop successful-local receipt 与运行态 3 条真实待消费/逾期记录外全部通过。
- 修复 ROOT-20260710-022 文档门禁分母不完整：新增 `mnemos.document_asset_manifest.v1` 与 `scripts/audit_document_asset_manifest.py`，自动发现 65/65 tracked Markdown，逐项验证 23/23 Prompt/schema 的精确 hash、AST consumer、loader binding 与 output contract，并分类验证 25/25 Desktop system-map assets。freshness/sensitive 改为共用 tracked Markdown 发现，首次全量扫描直接发现并修复 PR 模板裸 Python、失效测试路径和 onboarding 旧名；Desktop `00–10` 增加 current-state + repo 双锚点，`86–98` 头部绑定 current commit。新 gate 进入 local/pre-commit/CI/full-score；canonical 分母增为 44，pytest 文件分母增为 480/480。代码提交 `3dc4833e`；focused 50、相关宽回归 161、最终 Quick `5772 passed, 15 subtests passed`，`outside_write_count=0`、`formal_state_diff=[]`；local 仅因 Desktop successful-local receipt 和 8 条真实 overdue receipt 非零。
- 修复 ROOT-20260710-021 “ratchet pass 冒充满分 closure”：maintainability v2 对 510/510 broad catch 保存 exact AST fingerprint，并为 15 个超大文件和全部 catch 验证 owner/expiry/telemetry/remove condition；same-count replacement、parse failure、过期接受、改善后未收紧 baseline、普通 update 吸收增长均 fail closed。Zombie baseline 升级 v2，99 个 exact candidate 增加 telemetry 且新增风险必须显式接受。CI vulture 历史 294/292 baseline 收紧为 current/baseline 0/0，非零永不允许 rebaseline；CI/config/architecture 增长也不能由普通 update 吞掉。local/pre-commit/CI 明确是 accepted-debt development profile；full-score 新增 3 个 strict zero-closure gate，当前 525 maintainability residual 与 99 zombie residual 会正确阻断发布认证。代码提交 `79b588b5`；最终 Quick `5762 passed, 15 subtests passed`，`outside_write_count=0`、`formal_state_diff=[]`；local 仅因 Desktop facts 待同步和 7 条真实 overdue receipt 非零。
- 修复 ROOT-20260710-020 trusted static scan 的整文件路径/marker 假证明：扫描器升级为 `mnemos.trusted_push_static_scan.v4`，用 AST 覆盖 write_text/write_bytes/open/rename/replace/unlink/os/shutil/atomic helper，并按结构化控制流验证 receipt dominance；删除 `DIRECT_WRITE_RULES`。143 个非正式/恢复调用点进入 exact sink registry（sink ID/owner/target/expiry/reason），registry 不能伪造 guarded/trusted writer，unknown/stale/known bypass 均 fail closed。正式 write/delete/move 收敛到 central typed receipt commit helper，receipt 绑定 target/content/expected-existing hash，move 另绑定 source/source hash；Charon enforce 不再 proposal 后继续 rename，Eris duplicate 删除也受门禁。当前 169 sinks=143 registry+17 receipt-dominated+7 central writer+2 primitive，unknown/stale=0；代码提交 `52821199`，最终 Quick `5749 passed, 15 subtests passed`，local gates 除待同步 Desktop facts 与 6 条真实 overdue distill receipt 外全部通过。
- 修复 ROOT-20260710-019 `relation_evidence` 两份 DDL 由初始化顺序决定约束：新增 `core/kia/relation_evidence_schema.py` 作为 columns/defaults/FK/index、`mnemos.relation_evidence_schema.v1` 与 semantic hash 的唯一 owner；`KnowledgeGraph`/`RelationManager` 在任何其他 DDL 前 fail-closed 验证，旧 KG/RM schema、NULL/blank type、缺 index、损坏 registry 与 unknown schema 均不能静默通过。新增显式 preview/apply reconciliation、SQLite verified backup、事务 rebuild/rollback、row-count/integrity 对账与 strict owner/hash audit，并接入 local/pre-commit/CI/full-score；该 Root 关闭时 full-score 为 40 gates，ROOT-021/022 后当前为 44，pytest denominator 为 480/480。真实库迁移前后均 7,831 evidence、NULL=0、integrity=ok，备份已验证；代码提交 `06db89a3`，最终 Quick `5738 passed, 15 subtests passed`。
- 修复 ROOT-20260710-017 安全审计“记录 blocking error 但仍 `ok=true`”：引入 `SecurityFinding` 与 `mnemos.security_audit.v2`，Bandit、pip-audit、旧 credential row、plaintext secret、pickle/weak hash 和 health 状态全部先类型化，再由 findings 唯一派生 blocking/warning counts、status、`ok` 与退出码，严格保持 `ok == (blocking_count == 0)`。发布隐私安全聚合器改用 `--strict --json` 并调用同一 validator，拒绝 schema/counts/findings/status/`ok`/返回码矛盾，同时保留 warning 证据。代码提交 `4d1501e2`；focused 25、相关安全/隐私 33、最终 Quick `5720 passed, 15 subtests passed`，live security v2 与 release privacy/security 均为 blocking=0、warning=0。
- 修复 ROOT-20260710-016 full-score 可缩分母/空集合假绿：报告升级为 `mnemos.full_score_gates.v2`，代码内置 canonical 39-gate denominator；strict+real-api 拒绝 `--only` 和全部 skip，unknown/empty selector 退出 2。报告固定输出 expected/selected/executed/omitted、manifest/certificate hash、完整 Git commit/clean status、per-gate receipt 与 stdout/stderr SHA-256；仅全集完全相等、required 全通过、工作树干净才 `certifying=true/release_eligible=true`。新增独立 verifier 对比当前代码权威 manifest/commit/artifact，旧 v1 为 `legacy_scope_unverifiable`；新增 test-suite denominator 478/478 唯一归层和 10 个认知行为场景/30 文件/285 tests 实测。代码提交 `bb9bb71b`；最终 Quick `5710 passed, 15 subtests passed`、integration 279、heavy 18。
- 修复 ROOT-20260710-015 测试、满分门禁和诊断入口共享正式状态：新增 `mnemos.hermetic_run_environment.v1` 与 `mnemos.gate_hermeticity_audit.v1`，quick/integration/heavy/full-score 每次创建唯一 sandbox root，统一接管 HOME、Mnemos/database/wiki、XDG、temp、pycache 和 artifact 路径，默认不继承 API 凭据；只有显式 `full-score --real-api` 才传递受控凭据。pytest 在收集前安装边界并直接阻断正式 SQLite/投影/配置/benchmark 写入，runner 结束输出 `environment_hash`、`outside_write_count`、`formal_state_diff`。health/status/distill status/verify/golden 默认只读：缺库不建目录/表，写探针必须显式 `verify_installation.py --write-probes` 且使用唯一 `O_EXCL` 文件；golden 不再删除共享 `latest`。深审进一步把配置快照改为线程隔离 `ContextVar`、保留只读 Agent 会话探测、修复全新安装状态误报 partial，并清除四处测试对本机模型凭据的隐式依赖。代码提交 `f3796536`；最终 Quick `5676 passed, 15 subtests passed`、integration `279 passed`、heavy `18 passed`，diagnostics 5/5 且 `outside_write_count=0`、`formal_state_diff=[]`、凭据键为空；local gates 除正式运行账本已有 2 条待消费回执外全部通过。
- 修复 ROOT-20260710-014 分块检查点把旧结果冒充当前执行：新增不可变 `mnemos.distill_execution_spec.v1`，将精确 prompt、输出 schema、extract/parse/quality 代码摘要、显式 backend/provider/model route、merge 合同和 37 个输出相关有效配置纳入 hash；backend/merger 改用强制 `checkpoint_identity()`，删除隐式 caller/反射身份。检查点表改为多代主键并输出 hit/miss/spec diff，旧/损坏 metadata fail closed，新 spec 失败不覆盖旧 completed。Prompt 预渲染强制 rule-only intent route，避免 cache hit 先发额外 LLM 请求。真实库在 daemon 停止后先备份再从 111 条/9 session 的 v1 schema 迁移，行数与 integrity 保持不变，旧行作为不可复用历史保留。参数矩阵/迁移/恢复/故障测试 129 passed，完整 Quick `5649 passed, 15 subtests passed`，三类生产 DB 前后哈希一致。
- 修复 ROOT-20260710-018 配置治理假闭环：新增 `mnemos.config_registry.v1`，统一 467 个配置 entry 的类型、默认值、env、performance tier、示例/测试/文档覆盖、alias 与 removed tombstone；`Config` 默认拒绝 unknown/removed/alias/错误类型/损坏配置，caller fallback 不再形成第二套默认值。`config.stale_keys.v1` 只迁移原始持久化文档，先写 `0600` backup，再原子映射/删除并记录 canonical conflict；生产存量完成 79 个 stale 值清理、1 个 alias 迁移、7 个 canonical conflict 记录和 2 个安全数值转换。`scripts/audit_config_registry_closure.py --strict` 已进入 pre-commit/CI/local/full-score，当前 467 defined、313 read sites、JSON/YAML 各 467、unknown/removed reader 和 divergent fallback 均为 0。daemon identity 升级为 `mnemos.daemon_instance.v2` / heartbeat v3，同时保存配置文件字节哈希与 canonical 有效配置指纹。代码提交 `d701959b`；深审提交 `5756a76a` 又将 `config_fingerprint` 纳入 heartbeat↔PID 对账并暴露到 strict health。最终 Quick `5630 passed, 15 subtests passed in 637.11s`，local gates 全部通过。
- 修复 ROOT-20260710-013 cognitive readiness 假健康：`mnemos.cognitive_readiness.v2` / `mnemos.learning_signal.v2` 不再用跨库全局非零总数清 gap；required DB/表/旧 schema/读错误进入 blocked，已初始化但 0/0 的 required evidence/lineage 进入 unobserved，30 天默认时效窗口由 `cognitive_readiness.freshness_window_seconds` 配置。delivery feedback 只计算 visible delivery 的非空 feedback 或 reciprocal `delivery_event_id + outcome_id`；raw→observation、reflection/recap driver→patch/no_patch、consolidation candidate→applied coverage 都输出 denominator/covered/uncovered/ratio/lineage refs/freshness/cold-start。dry-run consolidation 不再算 applied。完整 golden fixture 为 100/100，缺库、空库、旧/坏 schema、stale、1/N、unlinked、dry-run 均 fail closed。深审同时修复同 session 多 generation 的 distill receipt 误绑：producer 以 task/input revision 建 generation/idempotency，consumer 精确解析 immutable producer，缺 cognitive producer 时不写 orphan consumption。代码提交 `f1908c89`；最终 Quick `5609 passed, 15 subtests passed`，producer ledger 与 KG 主库/WAL 前后哈希一致。
- 修复 ROOT-20260710-011 Agent Kit/health 假运行能力：Agent Kit 升级为 v2，旧安装/声明/source/tool 检查降格为 `conformance_ok`；runtime `full_power` 必须同时具备内容授权、同一 host 近期 canonical health 握手、固定 synthetic-safe completeness 样本与未过期回执。新增 `agent_health_roundtrips`/`agent_runtime_receipts` 元数据表和 `agent_runtime_probe` MCP tool，52/52 tool-policy/schema/handler 闭合；MCP health 删除浅层 Facade 实现并复用 CLI 30-check snapshot/hash，`agent` 纳入 strict health。旧/缺失/畸形/stale/未授权/check-set mismatch 均 fail closed，`agent repair` 不再因缺运行回执重复安装。代码 `ac89f4ff`；Quick `5577 passed, 15 subtests passed`，local gates 全通过。
- ROOT-20260710-010 将 recap consumption 从 target label 改为 durable fan-out outbox：新增 plan/command/per-attempt receipt、canonical target registry 和 60 秒 daemon retry；required receipts 未齐时 finalize/skip 保持 partial/retryable，重复 finalize、参数漂移重试与 page/skip plan commit gap 可幂等恢复。`recap_feedback` 以显式 supersession chain 为每个 committed effect 建 correction receipt，真实撤销提醒/错误 scheduler 状态、抑制检索与 policy patch，并向 persona/scoring 写原子幂等补偿。`scripts/reconcile_recap_consumption.py` 默认 dry-run，apply 前备份 recap/user-signal/policy/reminder 四库且不猜测历史 target；生产二次 dry-run为零。命名回归、Quick 5,565+15 与除提交后 Desktop HEAD 生成项外的 local gates 已通过。
- ROOT-20260710-009 反馈闭环改为 exact event outbox：MCP `push_feedback` 强制 `delivery_event_id/topic/action`，服务端校验 principal + project/session + subject + delivered status，删除 topic/latest fallback；`mnemos.feedback_event.v1` 与 required `feedback_receipts` 只有全 committed 才返回 complete，partial/failed/stale processing 可幂等恢复。penalty、outcome 五投影、adaptive scorer、delivery signal、trust negative evidence 都使用稳定 `feedback_event_id`，新增 inaccurate/outdated 动作和命名 E2E/restart 测试。

### Added
- 修复 ROOT-20260710-008 PolicyPatch 自匹配和无关注入：`PolicyPatchStore.active_for()` 删除 patch content 自证，只接受当前 task/subtype/context 命中的有界 trigger；ASCII 使用 token boundary，非 global patch 要求显式 project scope。候选先按 task-fit 与命中 trigger 排序，再做通用/专用重复抑制和 `max_active` 干扰预算，KIA 返回 `match_source`、`matched_triggers`、`task_fit_score`、`dedupe_key` 与 `interruption_budget_ok`。Reflection producer 不再把生成式 `key_points` 当 trigger；`scripts/reconcile_policy_patch_triggers.py` 默认 dry-run，`--apply` 先备份再清理存量解释句，不编造替代词。真实库 101 条 active patch 完成两轮清理（移除 604 个无效 trigger term），最终 dry-run changed=0、integrity=ok；无关负例 0 条，相关正例去重为 1 条。相关回归 102 passed，两轮 Quick 分别 5,540+15、5,541+15，local gates 全绿。
- 修复 ROOT-20260710-007 Wiki 投影生命周期假闭环：新增 `wiki_projection.db` 的 stable page identity、append-only create/update/move/delete mutation、因果 revision/tombstone 与六类 projection receipt；EventBus 使用 typed `ack/noop/retry/defer/dead`、稳定 consumer ID、前序 revision defer、持久指数退避、人工 decision resume 和 DLQ，不再把 `False`/`status=error`/异常 soft-ack。KG、Cognitive Graph、relation ANN、Wiki ANN、metrics 与 MOC 全部接入同一生命周期，自定义 config 路径不再回落生产目录。Wiki ANN 对缺失/重复/错序 durable label 自动重建，冲突 owner 全量重嵌；memory fallback 可持久化并在重启后恢复。`scripts/rebuild_wiki_projection_state.py` 提供备份、干净全量重建、真实增量 replay、隔离 comparator、ANN 语义审计和 receipt 对账。真实 Vault 验收为 3,242 pages、20,366 mutations、122,196 receipts、gap=0；relation ANN 8,160 labels、Wiki ANN 11,954 chunks 均无缺失/孤儿/重复/低于阈值，Quick 5,528+15 与除待同步 Desktop commit facts 外的 local gates 通过。
- 修复 ROOT-20260710-005 “完整蒸馏”静默丢内容：`clean_message_content()` 删除长代码头尾压缩、第 4+ shell 命令省略、编号/空行删除和首尾 strip，只保留显式 `[thinking]...[/thinking]` 私密块排除；WikiBuilder 纯文本 fallback 删除 `[:500]`。标准/分块 extractor 改用 `build_session_text(..., lossless=True)`，极小总预算/单消息预算不再触发 head-tail 或消息截断；private exclusion 只记录类型/span/计数。`chunk_checkpoint.build_chunk_fingerprint()` 将 `lossless-visible-v1` 纳入哈希并写入 `chunk_info`，旧无版本检查点自动 miss；真实库盘点的 9 个会话、111 条 completed 记录全部属于旧候选，保留原记录并在再次处理时按当前契约重提取。lossless E2E 证明代码/命令/附件占位/格式字节与首中尾 sentinel 经 token chunking 进入 extractor input；E2E 5 passed、目标集 104 passed、宽回归 250 passed、Quick 5469+15 与 local gates 全绿。
- 修复 ROOT-20260710-004 canonical raw 可变 evidence 与 projection 权威检索：`raw_turn_revisions` 以 content hash 生成 immutable revision，`raw_turns.current_revision_id` 只保存 logical current pointer，旧 revision 可稳定回查；Capture→Amphora→Hephaestus→Wiki 传递 revision/content hash/span 并写 durable provenance edge，edge 同步 reference retention。`session_search` 改为 metadata-only ACL 后 canonical fetch，RawIndex 仅在 authorized identity 内提供候选，投影截断/删除仍可命中 canonical 正文。新增 `scripts/reconcile_raw_revision_provenance.py` 默认 dry-run/显式 apply+backup，把可证明旧页写 edge，无法证明的只登记 `pending_rebuild` gap。真实库 10,376 turns 对应 10,376 revisions、missing current=0、697 个历史页面登记 gap、integrity=ok；Quick 5467+15 与全部 local gates 通过。
- 修复 ROOT-20260710-003 采集、蒸馏与复盘跨阶段提前成功：新增 revision-aware typed receipts、Capture→Amphora transactional outbox、Amphora generation identity、durable page/intentional-skip 终态分类和 recap receipt completion seam。proposal/partial/retry/write failure 不再标记任务或 L1 完成；`scripts/reconcile_pipeline_receipts.py` 默认 dry-run，显式 `--apply` 可把历史无输出 Amphora `done`、Capture 无 handoff 和 recap 伪完成恢复为可处理状态。真实库对账将 238 个无输出 legacy `done` 重排、为 171 个历史 Capture session 补齐 handoff，`reconciliation_gap` 从 1005 收敛为 0。
- 修复 ROOT-20260710-002 daemon PID 复用与 heartbeat 假健康：新增 `daemon/instance_identity.py` / `instance_control.py`，PID file、heartbeat、status、stop 共享 OS start/boot/executable、runtime-code、config/database 和 34-service manifest 指纹；PID reuse、旧/损坏 identity、SIGTERM 后暂不可验证均 fail closed 且不补发信号。旧整数 PID 只有 OS 事实可证明时一次性迁移；新写入强制完整 JSON identity，PID/heartbeat 均为 `0600`。health 输出 `identity_match`、commit/current_commit、build compatibility 与各 fingerprint，daemon `start` 在当前 heartbeat 落盘后才报告成功。
- 修复 ROOT-20260710-001 MCP caller 自报身份/权限的 P0 边界：`AgentAuthorizationStore` 新增 hash-only host launch capability 与显式 principal grant，8 个 Agent 配置通过 keyring reference 和 `0600` 原子轮换；51/51 tools 进入统一 `MCP_TOOL_POLICIES`，caller identity/ACL override、未知参数和越权 project 在 handler 前拒绝。Wiki/raw/search 结果保留严格 ACL envelope，`wiki_read` 只读 frontmatter 授权后才读正文，搜索/preflight/guard/retrospective/freshness/predictive push 的拒绝候选不写副作用；MCP `freshness_check` 删除 caller-selectable 自动刷新，`predictive_push` 删除 caller-selectable history 跳过。`scripts/reconcile_access_metadata.py` 完成存量 provenance 对账与 RawIndex 重建。新增 capability 撤销/过期/轮换、配置崩溃恢复、双主体 stdio、ACL backfill、direct read 和 auth-before-side-effects 回归矩阵。
- 修复 cognitive readiness 真实数据闭环：`WikiMetrics.scan_all_pages()` 现在写入 `page_metrics.page_role`，`core/wiki_page_roles.py` 统一区分真实 knowledge 页、派生 KG/observation/reflection/feedback、系统报告、MOC/index、占位/骨架和测试产物；`core/ops/cognitive_readiness.py` 只对 source-required knowledge 页要求非空 `source_refs`，并报告 `source_required_total`、`source_exempt_total`、豁免原因、样本和 stale metric rows，避免把系统产物或旧测试行当作 source debt。`evidence_backfill` 会消费已有 frontmatter provenance；daemon search ignore detection 同步关闭原 `search_sessions` outcome；`ContextAwareSearch(wiki_base=...)` 不再把测试/自定义 Wiki 搜索写入全局 DB；`reminder expire-stale --limit 0` 为 no-op。
- 修复 trusted push static scan 的绕过式放行：`core.trust.static_scan` 升级到 v3 契约，`known_bypass` 不再属于 pass 分类；legacy Hephaestus worker、forced retrospective、distill metadata/evidence backfill、KIA frontmatter/link/dispute/stress/DNA mutation、wiki metrics 与 persona calibration 等正式 Markdown mutation 改为通过 `TrustedVaultMutationService` / `core.trust.formal_markdown` 提交 proposal，报告、artifact、system_state 剩余写入必须逐点 inline 分类。新增 `tests/unit/test_formal_markdown.py` 并收紧 `tests/static/test_hephaestus_no_bypass_write.py`，当前 `python3 -m core.trust.static_scan` 输出 `known_bypass=0`。
- 修正安装验证假健康：`scripts/verify_installation.py --json` 默认不再把未运行的核心集成测试记为 `integration_tests=true`，而是输出 `verification_level=basic`、`full_verification_ok=false`、`results.integration_tests="skipped"` 和 `skipped_checks=["integration_tests"]`；`--full --json` 才会运行真实集成测试并在通过时给出 `full_verification_ok=true`。`checks.install_lifecycle` 进入 strict health，`installed_partial` 或 required step 未完成会列出 `incomplete_required_steps`、repair actions 并让 health degraded。
- 修正安装生命周期 health 对静态计划的误读：`build_install_lifecycle_health()` 现在合并当前 setup prerequisite 探测和 `ActionLedger(action_type=install_setup)` 中由真实 `mnemos setup` 写入的 verified `installed_ready` 证据；已有 ready 证据可关闭 capability/agent/verify/health 等 runtime step 的 planned 债，但当前配置、Vault 或必填模型端点变为 blocked 时仍会 degraded。`run_setup()` 与 health ready 合并态都会把非必填 scheduler step 标为 `skipped`，避免 ready 状态携带无关 repair action。
- 加严满分总验收的 health 判定：`scripts/run_full_score_gates.py` 的 `health.strict` 现在要求 health JSON 同时满足 `status=ok`、`ok=true`、`usable=true`、`strict_ok=true`，并拒绝 failed/degraded/warning/critical skipped checks；`--strict --real-api` 满分/发布运行会拒绝 `--skip*` 参数，避免带 warning 或跳过项的运行被计为 100/100。
- 修正根目录测试入口漂移：`run_tests.py` 不再维护旧的 `all/unit/integration/e2e` 分支，而是直接复用 `scripts.run_tests` 的 `quick/integration/heavy/full` layered runner；`python3 run_tests.py quick` 与 `python3 scripts/run_tests.py quick` 语义一致。
- 修正 vulture whitelist 审计的只读契约：`scripts/audit_vulture_whitelist.py` 默认无参数运行不再写 source 或 `vulture_whitelist.py`，写入必须显式 `--apply`；无 whitelist body 变化时不会仅因 header/sort 重写文件，stdout 会分别报告 source `# noqa`、whitelist body 删除和 header/file 写入状态。`tests/unit/test_vulture_check.py` 增加默认只读、apply 无 body 变化、apply 写入闭环回归。
- 修正 Hephaestus worker 成功蒸馏后的 L1 标记后端绑定：`HephaestusWorker.backend` 现在用当前 `inbox_dir.parent` 创建 StorageBackend，避免自定义/测试 worker 在标记 `status=distilled` 时扫描默认全局 vault；`tests/unit/test_hephaestus_worker.py` 增加 backend vault 绑定回归。
- 新增 KG endpoint 语义归一化/路径迁移入口：`core/kia/kg_endpoint_normalizer.py` 与 `mnemos kg normalize-endpoints [--apply] [--json]` 输出 `mnemos.kg_endpoint_normalization.v1`。默认 dry-run；apply 前创建定向 `knowledge_graph.db` 备份，只迁移唯一 Wiki basename 命中的旧路径端点并同步 `relations_fts`，只为多引用、非路径、未命中现有 entity 的概念端点补 `kg_endpoint_auto` / `semantic_normalization` entity。唯一匹配冲突、自指关系、旧 Inbox 残留和无法判定概念保留为 unresolved，不为了清零 `endpoint_gaps` 强行迁移或建实体。2026-07-08 真实库第一轮执行后，`endpoint_gaps.count` 从 499 降至 342，迁移 relation 321 条，补概念 entity 69 个；随后新增 `core/kia/relation_endpoint_quality.py`，`KnowledgeGraph.add_relation()` / `relation_writer.upsert_relation_row()` 拒绝 marker、多行片段、附件和内部投影等非法 endpoint，Charon 中文技术词提取不再对整段中文做 2-6 字切片；`mnemos kg normalize-endpoints --prune-invalid --apply --json` 只删除明确非法 endpoint 对应关系行并同步清理 `relation_evidence`、`relations_fts` 和 `relation_context_embeddings`。真实库第二轮清理备份为 `~/.mnemos/backups/kg-endpoints/knowledge_graph-20260708155009.db`，删除 invalid relations 1153 条，endpoint gaps 从 342 降至 291；`kg consistency` 仍为 ok，三类 hard_orphans 为 0，`relations_missing_fts=0`。
- 追补 KG endpoint 根因闭环：`RelationManager.add_from_distill()` 与 `apply_implicit_relations()` 不再直接 `INSERT OR REPLACE INTO relations`，统一复用 `relation_writer.upsert_relation_row()`，非法 endpoint 会逐条跳过并保留整批处理；`RelationManager` 独立初始化时同步创建 `relations_fts`，避免绕过 FTS 合约。`KnowledgeGraph._candidate_existing_pages()` 与隐式关系索引复用 `relation_endpoint_quality.is_derived_kg_scan_path()`，跳过 `07-Shadow`、`L2.4-KG/Relations`、`99-Reports`、`99-Archive` 和 entropy suggestion 等派生产物，防止 KG 对系统内部投影二次建图。新增 `scripts/audit_kg_relation_contract.py` 并接入 `scripts/run_local_gates.py`，禁止生产代码绕过 `relation_writer` 直接写 `relations`，也禁止查询旧的 `relations.method` 字段。
- 新增 Desktop System Map Facts Audit：`scripts/audit_desktop_system_map_facts.py` 校验 `~/Desktop/mnemos系统图谱/99-代码扫描-facts.json` 的顶层 `current_state`，要求 schema、repo commit、成功 local gates 和 quick 结果均对应当前工作树；该 gate 已接入 `scripts/run_local_gates.py`、pre-commit 和 CI，防止历史 scan 字段或旧 quick 结论被误读为当前状态。
- 修复完整健康检查复审暴露的运行态不干净问题：daemon `wiki_route` 改为 route-only Charon connect，传入 `write_relations=False`，避免周期服务写 KG cooccurrence 关系或触发 embedding-heavy 图谱构建；默认 daemon 重型 Chronos 步骤保持关闭，仅由显式配置/手工入口执行。`ContextAwareSearch`/KIA facade、Psyche、EventBus、sync/raw store 和 adaptive/scoring 信号路径统一使用短连接降级边界，避免 SQLite 长连接被 daemon 持有时让 `preflight_inject`、`guard_check` 或 health 卡死/报错。`WikiMetrics` 使用显式 `close()` / context manager 释放连接，`mnemos doctor` 的 Wiki overview 会显式关闭连接；doctor Wiki quality gate 与 `health.wiki_route_budgets.needs_review_pages` 使用同一预算，403/78 类预算内待复核页不再被误报为 warning；`scripts/verify_installation.py` 的 doctor 子进程超时提升到 60 秒，避免并发 health/doctor 时把 20 秒级合法 doctor 误判失败。pytest 全局隔离 Amphora 队列，防止 FileIngestor 测试把临时文件写入用户真实 `distill_queue.db`；EventBus 在 KIA 模块订阅完成后再启动 dispatch，无消费者的 no-persist telemetry 直接归档，真实无消费者事件进入 dead-letter。完整运行态验收要求 health strict ok、daemon heartbeat 新鲜、distill pending/processing/failed 为 0、CPU/日志无持续异常，并用 doctor/install/e2e 探针交叉验证。
- 修复 SiliconFlow embedding/rerank 限流器无限等待风险：`SiliconFlowRateLimiter.acquire()` 对单次预估 token 超过 TPM 的请求立即抛出 `ValueError`，`record()` 改为自持锁线程安全入口，`wait_and_record()` 增加 `max_wait_seconds` / `cancel_event` 退出预算；`SiliconFlowEmbeddingClient` 会把总 token 超过 TPM 但单条未超限的 embedding batch 自动分片，避免把旧的无限等待替换为大 batch 直接失败。
- 修复 watch/独立 CLI 永久循环缺少机器可测退出条件：新增 `core/cli/periodic.py`，为 watch 模式提供 `--once`、`--max-cycles`、`--run-seconds`、`--interval` 和受控 `run_periodic_loop()`；`core/hephaestus/wiki_builder.py`、`core/kia/charon.py` 与 `scripts/auto_commit_wiki.py` 均接入该循环。`charon --dry-run` 不再写 KG 关系或触发真实 embedding API，避免一轮 dry-run 卡在网络调用。
- 修复 setup 必填模型端点交互重试无上限：`scripts/auto_setup.py` 与 `mnemos setup/init` 新增 `--max-smoke-attempts`（默认 3），非 TTY 直接 fail fast，交互失败后可重试、打印 env 示例、保存当前配置并退出或停止到 dry-run 检查；`InstallLifecycleState.metadata` 现在暴露 `required_model_endpoints_failed` 和失败明细。安装依赖阶段也补强了 Homebrew/PEP 668 路径：repo `.venv` 的 pip 升级超时只降级为 warning，editable install 的 build isolation 依赖下载失败会自动用现有 venv `--no-build-isolation` 重试。
- 新增 Auto-Healing Orchestrator：`core/ops/auto_healing.py` 输出 `mnemos.auto_heal_orchestrator.v1`，把 health/daemon/queue/KIA 修复面汇总为带风险、状态、repair action、rollback plan、verification command 和用户介入预算的决策卡；`mnemos health --json` 的每个非 ok check 会带 `auto_heal_state`，`mnemos doctor repair --dry-run --json` 输出统一自愈计划，显式 apply handler 成功时写 `ActionLedger(action_type=auto_heal)`。
- 明确 DialogDecisionPush quiet-hour 契约：`mnemos proposal push` 和直接 `push()` 属于手动白盒请求，默认绕过 quiet-hour；自动投递路径如需安静窗口必须显式传 `respect_quiet_hours=True`；无可投递卡片时返回 `surface=none`，不会把 no-card 状态包装成 agent surface。新增单测覆盖 quiet-hour 手动出卡、显式 quiet-hour 抑制和 agent adapter no-card。
- 修正 health 顶层状态过于乐观：`core/ops/health_check.py` 现在输出 `status=ok/warning/degraded/failed`、`usable`、`strict_ok`、`strict_failures`，并将 storage/wiki/disk/api/schema/heartbeat/wiki_route/runtime_producer_consumer/install_lifecycle/amphora/queues/cognitive_readiness/sqlite_disk_budget 纳入 strict checks；Amphora `failed>0`、distill failed 超预算、high/critical recap pending 超预算、dialog reminder pending/active 超预算、install lifecycle partial 或 required step 未完成返回 degraded 和 repair action。CLI health 对 warning 仍可返回可用退出码，但 strict degraded 会返回非 0。
- 新增队列闭环运维入口：`mnemos distill retry-failed|archive-failed` 显式重试或归档 failed Amphora 任务；`mnemos distill reset-timeouts --minutes N` 会把超过 processing freshness 预算的 Amphora 任务返回 pending，health strict `checks.amphora`/`checks.queues` 会在 stale processing 超预算时 degraded 并给出修复命令；`mnemos recap list|resolve|dismiss` 可按 severity/source 批量闭环 pending 复盘任务并写 `recap_task_events`，`mnemos reminder status|dismiss|expire-stale` 补齐 dialog reminder 的 dismissed/expired 生命周期，避免 pending 无界增长。
- 新增 daemon heartbeat 服务错误恢复语义：`daemon/heartbeat.py` 和 `core/ops/health_check.py` 区分 `active_service_errors` 与 `historical_service_errors`；`raw_projection` 成功/跳过运行后清除旧 `database is locked` 错误，并通过 ActionLedger 写入 `raw_projection_recovered`。
- 新增认知系统就绪度合同：`core/ops/cognitive_readiness.py` 汇总 raw、Wiki metrics、KG/CognitiveGraph/evidence graph、recap/reminder、search click/open/ignore/no_result、delivery/outcome 账本状态，`scripts/audit_cognitive_readiness.py --json` 与 `mnemos doctor --cognitive-readiness --json` 输出 `mnemos.cognitive_readiness.v1`。`--budget` 按来源、证据、消费者、行为四段预算返回非 0，`--record-gaps` 显式写 `cognitive_readiness_gap` ActionLedger，并把结果纳入 `cognitive_assets` scorecard。
- 扩展学习进化闭环：`core/ops/cognitive_readiness.py` 内嵌 `mnemos.learning_signal.v1`，把 raw/search/feedback/reflection 到 observations、policy_patches/policy_patch_feedback、cognitive_consolidation runs 的转化缺口纳入 readiness 预算和非 strict `checks.cognitive_learning`；Observation 增量 0 产出返回 `status/reason/processed_items`；`ReflectionPolicyPatchConsumer` 默认把高置信 Reflection/shift 写入 `PolicyPatchStore`，不生成时写 `no_patch` 证据；`scripts/plan_cognitive_consolidation.py --record-run` 可在 dry-run 下初始化并记录 consolidation run。
- 新增 P0.5 EvidenceBackfill：`core/ops/evidence_backfill.py`、`scripts/backfill_wiki_evidence.py` 与 `mnemos distill evidence-backfill [--apply]` 从 `document_wiki_link`、distill output/meta、强 `relation_evidence` 回填 page-level source refs；默认 dry-run，`--apply` 写 `page_metrics.source_count/source_refs/evidence_level`、frontmatter 和 `99-Reports/认知数据就绪度/` 报告。默认强证据类型为 `anti_pattern_quote`、`distill_extraction`，避免弱 KG 关系虚高证据等级。
- 新增 P1 DistillActionRouter：`core/hephaestus/distill_action_router.py` 将 `distill_output_v2` 的 `create_page`、`update_page`、`merge_into_page`、`route_to_dispute`、`record_reinforcement`、`skip` 统一路由到可审计 action log。`merge/update` 写正文前创建备份和 `MergeDecisionCard`，低置信/高冲突样本进入 `07-Shadow/distill-actions`，冲突样本进入 `08-Disputes`，reinforce 只更新目标页 frontmatter/metrics，不再生成重复页面。新增只读入口 `mnemos distill actions [--json]` 反查 `source_event_ids`、`target_page`、`backup_path`、错误和 knowledge action。
- 新增 CognitiveValueGate：`core/hephaestus/cognitive_value_gate.py` 接在普通 `QualityGate` 之后，按来源证据、认知贡献类型、未来触发场景、消费者影响和 lifecycle 信号判断 Wiki 准入。低认知贡献但格式良好的片段会拒绝或 pending verification；正式页面写入 `认知价值门禁状态`、`认知贡献类型`、`认知消费者` 和 `质量门禁账本ID`，最终门禁结果写入 `ActionLedger(action_type=quality_gate)`。默认配置新增 `quality_gate.cognitive_value.*`，配置样例 strict 覆盖仍为 100%。
- 新增 distill cognitive actions：`distill_output_v2.claims[].cognitive_actions` 覆盖 observation/reflection/policy/methodology/pitfall/relation/reinforcement 候选。高价值 claim 缺动作会被契约拒绝；`DistillActionRouter` 写 `cognitive_action_log` 和 `mnemos.distill_cognitive_action.v1` artifact，`mnemos distill actions --json` 和 `mnemos health --json` 的 `checks.distill_cognitive_actions` 可只读反查计数和明细；普通技术事实无动作时页面标记 `ordinary_knowledge`。
- 新增 distill behavior intent：`distill_output_v2.user_behavior_intent` 成为非 `skip` 输出必填子契约，记录 `content_source`、`user_intent_signal`、`intent_hypothesis`、意图证据、后续验证/修正事件、`intent_status` 和 `intent_confidence`。`PromptBuilder` 会注入 `ContentSource`、`UserIntent` 与 `IntentRouter` 预判；COG-044 后外部文件不再自动升级意图或置信度。Wiki frontmatter/来源追踪展示用户引入原因，`SourceReader` 回读后按来源权限限制 Observation/persona/reflection/cognitive decision 消费。
- 新增用户认知画像 v2：`core/persona/cognitive_profile.py` 新增 `profile_signals`、`profile_assertions`、`profile_usage_log` 的 schema、DTO、repository 与 payload 构建，`core/persona/psyche.py` 保留信号采集和兼容代理；明确偏好、纠错、忽略、打断和返工会沉淀为带证据、置信度、隐私等级、过期/状态和修订/反驳策略的画像断言。`persona_summary` / `persona_behavior_prompt` 返回 `user_cognitive_profile_v2`，preflight、ContextAwareSearch、蒸馏 prompt、CognitiveValueGate、Auto-Healing 和 Cognitive Decision Flywheel 均记录画像消费效果；新增 `tests/unit/test_user_cognitive_profile_v2.py`、`tests/integration/test_profile_signal_assertion_usage_loop.py` 和 `scripts/audit_persona_profile_contract.py --strict`，`run_full_score_gates.py` 增加 `contracts.persona_profile`。
- 新增 Cognitive Data Event Registry：`core/ops/cognitive_data_contract.py` 定义 `mnemos.cognitive_data_event.v1`、`mnemos.data_interface_registry.v1`、统一事件字段和 CaptureService/CaptureQueue/SyncEngine/FileIngestor/DocumentProcessor/Amphora/EventBus/ReflectionStore/AdaptiveScorer/DistillActionRouter/persona signal store 接口注册；`core/ops/producer_consumer_ledger.py` 新增 `cognitive_data_events`、`cognitive_data_consumptions`、`cognitive_data_reconciliations`，记录 source_id、asset_id、dedupe_key、consumer outcome，并自动对账 duplicate/derived/reinforcement。新增 `scripts/audit_data_interface_registry.py --strict`、`tests/unit/test_cognitive_data_event_contract.py`、`tests/integration/test_cognitive_data_ledger_capture_to_distill.py`、`tests/integration/test_duplicate_capture_consume_reconciliation.py`，`run_full_score_gates.py` 增加 `contracts.data_interface_registry`。
- 新增 distill response budget 审计：对话蒸馏输出预算四档从 `4000/6000/8000/12000` 提升为 `6000/8000/12000/16000`，同步 `DEFAULT_CONFIG`、配置样例、`response_budget.py`、`distillation_engine.RESPONSE_TOKENS`、LLM fallback、测试和文档；`scripts/audit_distill_response_budget.py` 已接入 `scripts/run_local_gates.py`，防止旧档位回归。
- 新增 Docs Freshness Audit：`scripts/audit_docs_freshness.py --strict` 默认扫描 AGENTS、CLAUDE、CONTRIBUTING、README、README-en、SECURITY、docs 和可发现的 `~/Desktop/mnemos系统图谱`，阻断本机绝对路径、裸 `python` 调脚本、调模块或执行内联代码回归；F20 后还会校验 fenced shell 命令中的 repo 相对路径存在，并要求 `mnemos config set <key>` 示例存在于 `config/config.example.json`；ISS-008 起支持 `--paths` 显式指定正式扫描面，local gate 不再漏掉正式文档和桌面图谱。`scripts/verify_config_examples.py --strict` 已进入 local gates、pre-commit 和 CI，`config/config.example.{json,yaml}` 也补齐三类必填模型的 `api_key_source` 示例。
- 新增 Docs Sensitive Info Audit：`scripts/audit_docs_sensitive_info.py --strict` 扫描 README、README-en、SECURITY、CLAUDE、AGENTS 和 docs，阻断 raw provider key/JWT、本机绝对路径、真实 API endpoint、明文 credential 赋值、个人邮箱/手机号/身份证和 PII 赋值进入公开 Markdown；F21 手工复核确认当前 CHANGELOG/发布历史未包含高置信 raw key/JWT，宽泛 `token`/`api_key` 命中均为安全说明、环境变量占位值或 token budget 术语。该 gate 已接入 local gates、pre-commit 和 CI。
- 新增 Repo Sensitive Literal Audit：`scripts/audit_repo_sensitive_literals.py --strict` 扫描 git tracked 与未忽略的 untracked 文本，阻断源码、测试和文档中的完整 provider-shaped fake key、本机 home path 和明文 credential literal；F24 同步删除陈旧根目录 `PLAN.md`，并把 redaction 测试的敏感样例改为运行时拼接或 `DUMMY_CREDENTIAL_*` 哨兵。该 gate 已接入 local gates、pre-commit 和 CI。
- 新增 Release Privacy/Security Audit：`scripts/audit_release_privacy_security.py --strict` 输出 `mnemos.release_privacy_security.v1`，聚合 `scripts/security_audit.py --strict`、`mnemos doctor config --strict --json`、`mnemos health --json` 的 security/privacy 切片、docs sensitive audit、repo sensitive literal audit 和 health/config 诊断脱敏扫描，统一给出 `blocking_findings`、`warning_findings` 与 `repair_actions`；ISS-009 起门禁还会运行 `mnemos_cli.py distill status` 和 `scripts/e2e_probe.py --dry-run --no-api`，阻断可分享诊断中的本机路径泄漏。该 gate 已接入 `scripts/run_local_gates.py` 和 `run_full_score_gates.py` 的 `security.release_privacy`。
- 新增 Keyring/env fallback Doctor：`core/ops/keyring_doctor.py` 输出 `mnemos.keyring_doctor.v1`，`mnemos secrets doctor [--json|--accept-env-fallback]` 可只读核对 active Python keyring backend、secret 引用来源计数和 `secret_inventory_plaintext_count`，或显式写入 `security.accept_env_secret_fallback=true`。`mnemos health --json` 与 `doctor config --strict --json` 的 security keyring 项现在显示 `keyring_status`、`keyring_risk_level`、`safe_but_not_best`、env fallback 接受状态和迁移修复动作；keyring 不可用仍是非 strict warning，但 env 降级必须在无明文 secret 后显式接受。
- 新增 Runtime Dependency Cycle Gate：`scripts/arch_dependency_graph.py --check` 现在要求 runtime-only cycle waiver 写明 owner、target interface、resolution 和具体 arch-debt issue，不再接受泛化 T7/TODOS 占位；`docs/core-integrations-dependencies.md` 已重新生成。`core.cli.helpers` 的 Obsidian vault 注册检查下沉到 `core.vaults.obsidian_registry`，CLI helper 不再反向依赖 integrations backend；新增 `tests/test_arch_dependency_graph.py::test_core_cli_helpers_has_no_integration_dependency` 防回归。
- 修复 Runtime Producer/Consumer 假闭环（2026-07-11 / ROOT-20260710-012）：`core/ops/producer_consumer_ledger.py` 升级为 `mnemos.runtime_producer_consumer.v2`，以不可变 producer event、generation、intended consumer 和 append-only receipt 核对 event × intended-consumer coverage；required flow 的 0/0 现在是 `unobserved` strict failure，事件触发型 flow 才能在无事件时为 N/A，持续型 flow 另受 freshness 预算约束。health 改为纯只读，缺库/旧 schema blocked；显式 bootstrap 负责初始化、v1 迁移和 `0600` durable outbox 有序 replay。24 条 adaptive flow 中 19 条已接真实运行边界、5 条保留有依据的 N/A，Capture→Queue→Worker→Amphora→Distill 的 cognitive receipt 同步闭合 persona/raw-quality/distill 消费；provider timeout 保留 pending/retry，不再伪造成功。
- 补强 ROOT-20260710-012 异步与测试隔离闭环：新增 `receipt_grace_seconds`、`in_flight_count` 与 `overdue_pending_count`，KG projection 在 60 秒真实终态窗口内保持可观察 in-flight，超时仍 strict fail；其他 flow 默认 grace=0。`CaptureService` 将同一 config 显式传给 queue/worker/SyncEngine/telemetry，蒸馏/capture 测试显式注入临时 receipt config/backend；清理两次 Quick 可证明的测试 ledger 污染后，最终 Quick `5597 passed, 15 subtests` 且生产 ledger SHA-256 前后不变。
- 新增 Runtime Producer/Consumer Ledger（历史 v1）：`core/ops/producer_consumer_ledger.py` 输出 `mnemos.runtime_producer_consumer.v1`，把 `docs/acceptance/adaptive_data_flows.json` 的 flow 注册为 runtime topic，记录 produced/consumed/dead_letter、item_id 对账、last_produced_at/last_consumed_at、pending 和 lag 预算；`scripts/audit_runtime_producer_consumer_closure.py --strict` 同时检查 module toggle 产物与 adaptive runtime ledger，`mnemos health --json` 新增 strict `checks.runtime_producer_consumer`，orphan outputs、no-source consumers、item mismatches 或 dead letters 超预算会降级，并进入 `data_pipeline` scorecard。该口径已由 ROOT-20260710-012 的 v2 事件/回执模型取代。
- 新增 Wow Path E2E Probe：`scripts/e2e_wow_probe.py` 输出 `mnemos.e2e_wow_probe.v1`，把首次配置三项必填模型、可选多模态跳过/配置、可信用户文档 100MB gate、默认 distill、行为/意图字段、Obsidian 路由、ContextAwareSearch/preflight 召回、runtime consumer ledger 和 auto-heal dry-run 串成一条用户价值验收链路；`tests/e2e/test_wow_path.py` 覆盖 dry-run、mock LLM 完整链路和 CLI JSON，`scripts/run_full_score_gates.py` 的 E2E gate 改为 `e2e_wow_probe.py --mock-llm` / `--real-api`，避免满分验收只证明底层连通性。
- 新增 Docs Stale Service Key Audit：README/README-en 的 daemon services 示例统一改用 canonical `eventbus`；`scripts/audit_docs_stale_service_keys.py` 扫描 README、README-en 和 docs 中 live config 示例，阻断退役服务键回归。
- 新增 Wiki Route Closure：蒸馏和文档蒸馏写页前通过 Charon `resolve_page_folder()` 解析目标目录，可确定分类时直接写正式 Wiki 目录，不确定、resolver 失败或正式区同 basename 冲突才留 `00-Inbox`，并写 `Wiki路由状态/原因/目标` frontmatter。daemon 新增 `wiki_route` 服务，主体在 `daemon/wiki_route.py`，周期性运行 Charon connect 并在 heartbeat 暴露 classified/moved/review；`mnemos health --json` 新增 strict `checks.wiki_route`，按预算检查可分类 Inbox、needs_review、正式区 source-prefixed 页和标题/basename 冲突组。`distill.auto_expression_formatting` 默认开启；即使正文格式化关闭，Wiki frontmatter 仍写 `表达格式` 建议。
- Trusted Document Import 已在 ROOT-20260710-006 收敛为单一所有权：`mnemos import`、MCP `document_process`、daemon `FileIngestor` 与 KnowledgeInbox 默认只写 canonical raw + capture receipt，raw projection 独占 Obsidian，capture outbox 独占 Amphora；默认 `mode=distill` 异步返回 accepted/pending，`mode=capture` 只写 raw，`mode=parse` 才只预览。文档资产使用 file SHA-256 stable id；worker 复用已有 raw receipt，重复文件保持 1 revision/event/handoff。删除 FileIngestor direct backend/Amphora、DocumentProcessor `save_to_backend`/`--save`、facade 同步蒸馏旧路径及其假测试；reconciliation 会删除仅 canonical committed 且无 provenance edge 的历史重复 worker raw。旧布尔参数到 `mode` 的兼容映射仍保留，删除条件是 MCP 客户端不再发送 `write_to_wiki/save_to_l1`。
- 新增 Cognitive Decision Flywheel：`core/kia/ixion.py` 现在以 `CognitiveDecisionFlywheel` / `cognitive_decision_asset.v1` 为主产物，`core/kia/cognitive_decision_assets.py` 承担资产 DTO、行为生成器、Wiki 候选扫描和资产持久化 mixin；旧 `SkillWikiFlywheel` 名称保留为兼容 alias。Wiki/行为/Skill 反向信号先生成认知决策资产，记录 `asset_type`、证据、适用条件、失败模式、验证 recipe 和 `automation_derivative_allowed`；只有明确允许派生时才创建 automation skill。`integrations/apollon.py` 的主动汇总改为“认知决策飞轮”，旧 `_run_skill_wiki_flywheel()` 仅转发兼容。`skill_suggestion` prompt/schema 保留任务名但语义迁移为认知决策资产建议，已经沉淀方法论的对话不再被排除。
- 新增 Distill JSON Quality：`core/hephaestus/distillation_json.py` 返回 `direct_json`、`markdown_json`、`balanced_json`、`fixed_json`、`failed` 解析路径元数据；`core/hephaestus/distillation_metrics.py` 写入 redacted `distill_json_parse_events`，`mnemos health --json` 输出非 strict `checks.distill_json_quality`，展示直接成功率、fallback 成功率、自动修复次数、最终失败率和 24h 趋势。
- 新增 Maintainability Budget：`scripts/check_maintainability_budget.py` 与 `scripts/maintainability_budget.json` 对生产超大文件和 broad `except Exception` 做 ratchet，接入 `run_local_gates.py`、pre-commit 和 CI；新增超大文件、既有超大文件增长、broad catch 数量反弹、未分类 broad catch 总量反弹或关键 health/queue/sync/install/distill 路径出现未分类 broad catch 都会失败。F19 后 `scripts/auto_setup.py` preserve-config JSON 读取边界已收窄，生产 broad exception 从 557 降到 532，未分类 broad catch 从 146 降到 131，关键路径未分类数为 0。
- 新增 Hardcoded Path Audit：`scripts/audit_hardcoded_paths.py --strict` 接入 `scripts/run_local_gates.py`，阻断生产代码中的本机绝对路径、旧 Obsidian wiki 默认和绕过 Config 的 Mnemos/raw vault 字面量。`QuestionAnswerSearch`、CognitiveConsolidator、EvidenceBackfill、cognitive readiness、raw recompact、wiki auto-commit、magic-number refactor 和 vault layout 迁移入口已改为读取 `get_config().wiki_dir`、`Config.vault_dir()`、setup 布局 helper 或显式 CLI 参数。
- 修复 `mnemos setup` 包装入口与 `scripts/auto_setup.py` 的 venv re-exec 闭环：顶层 setup 参数会完整转发，系统 Python 触发 venv 重启时重新进入 `mnemos_cli.py setup ... --venv-reexec`，保留 `InstallLifecycleState` JSON 输出；临时 HOME setup 后 health strict 与 E2E dry-run 可通过。
- 新增 Adaptive Policy Coverage：`core/kia/adaptive_policy_matrix.py` 与 `docs/acceptance/adaptive_policy_matrix.json` 输出 `mnemos.adaptive_policy_coverage.v1`，把 AdaptiveConfig 默认规则扩展到 distill、quality_gate、scoring、delivery、search、raw、document_process、intent 和 cognitive_decision 9 个域共 11 条规则。`scripts/audit_adaptive_policy_matrix.py --strict` 校验覆盖矩阵和文档同步，并接入 `scripts/run_local_gates.py`；`mnemos status` 和 `checks.adaptive_policy` 展示覆盖数量、coverage_errors、active_shadow、metric_before、age_hours 和 overdue rollback。
- 新增审计报告写入策略：`scripts/audit_orphan_modules.py` 默认输出 stdout，`--check` 只比较 `docs/orphan-modules-report.md` 不写文件；repo 内报告刷新必须显式 `--output docs/orphan-modules-report.md --apply` 并写 ActionLedger，避免只读评分污染工作区。
- 新增 Full Score Gate：`scripts/run_full_score_gates.py --strict --real-api` 汇总 quick/integration/heavy、local gates、strict health、security strict、release privacy/security、wow-path E2E、config examples、cognitive readiness、Wiki budget、golden benchmark、install/upgrade probes 和 contract audits；默认 artifact 写 `/tmp/mnemos-full-score-gates/...`，任一必需 gate 失败返回非 0 并输出 repair hint。
- 新增 Config Strict Doctor：`mnemos_cli.py doctor config --strict --json` 输出 `mnemos.config_audit.v1`，统一验收 LLM/embedding/reranker/multimodal、secret 来源、路径、legacy/stale 配置、privacy、retention、daemon 和权限；机器报告写入 `~/.mnemos/config_audit.json`，只记录 `env:`/`keyring:`/`keyref:` 引用、状态和长度统计，不写明文 key。
- 新增可选多模态模型配置：`DEFAULT_CONFIG.multimodal`、`MNEMOS_MULTIMODAL_API_KEY`、`MNEMOS_MULTIMODAL_BASE_URL`、`MNEMOS_MULTIMODAL_MODEL` 和 `resolve_multimodal_api_config()` 统一图片/截图/视觉证据入口；`scripts/setup_model_endpoints.py` 承载安装期可选 endpoint 检测，`scripts/auto_setup.py` 提示“多模态模型，可跳过，不影响 Mnemos 正常使用”，launchd/cron 导出同组 env，`scripts/verify_installation.py --json` 与 `mnemos health --json` 显示 configured/skipped/unreachable 或非 strict `checks.multimodal`；KnowledgeInbox 图片在配置存在时调用 OpenAI-compatible vision endpoint 解析为 Markdown、写入 storage 并入蒸馏队列，未配置或 API 失败时写 `mnemos.multimodal_image_task.v1` 可恢复任务。
- 新增 Secret Inventory：`core/privacy/secret_inventory.py` 输出 `mnemos.secret_inventory.v1`，被 `mnemos health --json`、`scripts/security_audit.py --strict` 与 `doctor config --strict` 共同复用；递归覆盖 `api_key/token/secret/password/credential/bearer/key_source`，过滤 `token_budget`、`max_tokens`、`tokenizer` 等非密钥字段，plaintext 风险只记录路径和长度，不输出值。
- 落位 SQLite 磁盘预算健康面：`core/ops/sqlite_disk_budget.py` 输出 `mnemos.sqlite_disk_budget.v1`，监控 `.db-wal`、Mnemos temp、snapshot 与 `raw_events.db` 的体积/增长率；health 将 `checks.sqlite_disk_budget` 作为 strict check，`scripts/repair_sqlite_disk_budget.py` 提供 WAL checkpoint 与过期 temp 清理，snapshot/raw_events 删除必须人工确认。
- 落位 F23/ISS-009 诊断报告默认脱敏：`core/privacy/redaction.py` 统一处理 API URL、本机路径和 key source，`mnemos health --json`、`doctor config --strict --json`、doctor 文本、`scripts/verify_installation.py --json`、`mnemos_cli.py distill status` 与 `scripts/e2e_probe.py --dry-run --no-api` 默认不暴露真实 endpoint、用户路径或 `env:`/`keyring:` 明细；原值只通过 `--unsafe-debug` / `--show-paths` 供本机私有排错使用。
- 新增 P2 CognitiveConsolidator：`core/cognitive/consolidator.py` 与 `scripts/plan_cognitive_consolidation.py` 输出 `mnemos.cognitive_consolidation.v1`，统一规划 raw retention/purge、Raw Vault 投影一致性、Wiki/KG 压缩候选、LLM abstraction callback 产物和 `consolidation_runs` / `consolidation_coverage`。默认 dry-run 不创建 `cognitive_consolidation.db`，Wiki/KG 候选只报告不物理删除，method page trust gate 只预览不写 trust ledger；只有 apply 且方法论页包含 `evidence_refs`、配置范围内 `key_details`、不适用条件，并通过 extraction trust decision 时才允许小批量 raw purge。
- 新增 P2.5 KnowledgeTrustScorer：`core/cognitive/trust_scorer.py` 写入 `mnemos.trust_decisions.v1`，用 `trust_score/task_fit_score/interruption_cost/outcome_score` 统一 extraction、merge/update、predictive_push、guard 阻断条件和负证据读取。`DistillActionRouter` 的 create_page 写 audit-only extraction trust decision，`CognitiveConsolidator` 的 method page apply 覆盖/清理前写 extraction trust decision，update/merge 写 Wiki 前必须有 apply trust decision；MCP `predictive_push` 只返回 delivery decision 为 `deliver` 的候选；ROOT-009 起 push feedback 的 ignore/dismiss/inaccurate/outdated 证据按稳定 feedback event 幂等写入。
- 新增 P2.7 PolicyPatchStore：`core/cognitive/policy_patch.py` 写入 `mnemos.policy_patches.v1`，把高价值 L4/L5/recap 经验转成有 TTL、scope、source、severity、trigger 和 evidence refs 的策略补丁；`RetrospectiveConsumptionRouter.route_after_finalize()` 会把用户确认的 recap lesson 写成 policy patch 候选并记录 consumption outcome；Store 层拒绝无 trigger lesson，空 trigger 不会通配；`preflight_inject` 与 `guard_check` 会把 active patch 追加为 KIA checklist item 并走 DeliveryRouter 入账，不写宿主 system prompt。
- 新增 P3/P4 KnowledgeDeliveryRouter 统一入口：`core/cognitive/delivery_router.py` 写入 `mnemos.delivery_events.v1`，用 `DeliveryBudgetPolicy` 统一 profile 预算、同 topic 冷却、dismiss/ignore 冷却、强度降级和 delivery/outcome 账本。`predictive_push` 通过 DeliveryRouter 后才返回，payload 带 `delivery_event_id`；ROOT-009 起用户反馈只接受 exact delivery event，并通过 append-only FeedbackEvent/outbox/required receipts 扇出；`preflight_inject` 写 silent preload 事件但不消耗用户可见预算；`guard_check`、`check_pending_recaps`、dialog reminder 都写 delivery decision；`scripts/replay_delivery_decisions.py --json` 默认用临时 DB dry-run 回放策略。
- 新增 P4 VerificationQueue：`core/cognitive/verification_queue.py` 输出 `mnemos.verification_report.v1`，把 unresolved dispute、active blindspot 和 stale freshness alert 转成带 `evidence_refs` / `verification_commands` 的受控求证任务。新增 `mnemos verify plan|run`，`run` 默认 dry-run，`--apply` 只写 `verification_queue.db` 和 data-dir report；Chronos 注册 `verification_queue` 后台步骤并受 `ResourceBudget` 约束。
- 新增 P5 多模态 evidence 标准化：`core/evidence/artifact_uri.py` 定义 `mnemos-artifact://<agent>/<session>/turn/<turn>/<artifact_type>[/<index>]` 稳定 URI；`CaptureService.capture_turn()` 将完整采集 artifact、reasoning artifact、工具结果和附件写入 `metadata.artifact_refs`；`distill_output_v2` claim evidence 支持 `artifact_uri/artifact_type/artifact_summary/artifact_sha256/artifact_mime_type`，但仍要求 `source_event_id` 和短 quote；Wiki 来源追踪只渲染摘要链接。Agent Kit acceptance 新增 `artifact_uri_context` 样本，raw/distilled contract 新增 artifact refs 字段。
- P6 深审修复：`AdaptiveScorerV2.enqueue_training_sample()` 写入训练样本前会确保 `scorer_training_queue` / `ground_truth_signals` 表存在，避免 fresh DB 下 `guard_check` 触发训练样本时只打印缺表 warning。
- F13/F14 深审修复：`guard.analysis_loop.*` 成为 Aegis 防分析循环阈值来源，默认连续纯分析和同一文件/工具重复读取均第 2 次触发；配置为 3 时才恢复第三次触发。`guard_check` 响应和 `guard_alert` metadata 返回 `threshold_source`、`threshold_value`、`current_count`，并按实际触发阈值区分 config/default 来源；`InProcessGuard.start_session()` 里重复 `_tool_read_counts` 初始化和 `type: ignore[no-redef]` 已删除。
- 新增 `mnemos vaults audit-content [--json]`，只读审计 mnemos Obsidian vault 的展示问题、分类问题和结构化输出问题，覆盖 root 页面、长文件名、source/session 前缀文件名、已结构化但仍滞留 Inbox 的页面、needs_review 页面、标题归一化碰撞、frontmatter 缺失和正式区必填字段缺失。
- 新增 Wiki 质量合同：`scripts/wiki_lint.py --summary --json` 输出 `mnemos.wiki_quality.v1`，把 missing_meta、orphan、broken_link、stub 映射到统一生命周期、预算 owner/strategy、manual review 和 `obsidian_experience` scorecard；`--budget` 可按预算阻断，`--fix` 的元数据自动修复会写 `wiki_quality_fix` ActionLedger。新增 `scripts/audit_wiki_quality_contract.py --strict` 与单测覆盖 schema、预算和 ledger 证据。
- 新增 `core/vaults/naming.py` 作为 Obsidian 文件名统一策略：蒸馏页、文档导入页默认使用展示标题命名，仅在磁盘碰撞时追加短哈希；Shadow 与 KG relation 投影使用短投影 ID，正文/frontmatter 保留可读上下文。
- 新增系统级契约与模块开关治理：`core/system_contracts.py` 覆盖认知资产、统一质量决策、能力发现、隐私保留、生命周期状态、ActionLedger、领域语言和 scorecard；`core/module_toggles.py` 覆盖默认关闭、冷启动、隐私、成本、watcher/daemon、legacy/stale 开关，并为每个模块声明自动开启策略、自动关闭策略、产物 schema、消费者、效果指标、互斥关系和回滚策略。`mnemos health --json` 输出 `checks.system_contracts`、`checks.module_toggles` 与 strict `checks.runtime_producer_consumer`，`mnemos doctor modules --json` 是只读核对入口；新增 `audit_module_toggle_registry.py`、`audit_cold_start_toggle_matrix.py`、`audit_toggle_auto_disable_policy.py`、`audit_toggle_output_consumers.py`、`audit_runtime_producer_consumer_closure.py` 五个 strict 审计脚本，其中 runtime closure 审计已扩展到 adaptive flow ledger。
- 新增迁移/备份/数据所有权底座：`core/migrations/registry.py` 将旧配置 alias、已移除配置项、`migrate_db.py`、`migrate_vault_layout.py` 和加密迁移包装进 `MigrationRegistry`，并用 `MigrationLedger` 记录 plan hash、备份引用、验证和回滚状态；`core/backup/snapshot_manager.py` 生成覆盖配置、SQLite、mnemos vault、raw vault、Action Ledger、迁移账本和模块状态的 `SnapshotManifest`；`core/privacy/data_ownership.py` 提供 raw/Wiki/metadata/evidence/persona/reflection/scoring/action/prompt/access/agent metadata 的 inventory、export、freeze、delete dry-run 和 deletion proof 契约。CLI 新增 `mnemos migrate`、`mnemos backup`、`mnemos restore`、`mnemos data`，health 新增 `checks.migrations`、`checks.backup`、`checks.data_ownership`；新增 `audit_migration_registry.py`、`audit_backup_recovery_contract.py`、`audit_data_ownership_contract.py`。
- 新增产品级安装生命周期：`core/setup/install_lifecycle.py` 定义 `mnemos.install_lifecycle.v1`，把 `mnemos setup`、`mnemos upgrade plan/apply`、`mnemos uninstall` 和 `mnemos doctor repair-all` 串成同一状态机，覆盖 setup/upgrade/uninstall/repair-all 的机器可读状态、repair action、ActionLedger、迁移计划、升级前快照和数据保留/删除契约。`scripts/auto_setup.py` 保留为兼容执行面；health 新增 strict `checks.install_lifecycle`；新增 `scripts/audit_install_upgrade_contract.py`、`scripts/e2e_install_probe.py`、`scripts/e2e_upgrade_probe.py`、`tests/unit/test_install_state_machine.py` 与 `tests/integration/test_setup_upgrade_roundtrip.py`。
- `scripts/ci_ratchet_baseline.json` 将本批有意新增的运行态 manifest/backup 读取预算从 `291` 前移到 `294`，并把 `MigrationRegistry` 对 legacy env alias 的升级计划探测归类为 `known_internal`；未归类直接配置读取仍为 `0`。

### Fixed
- SQLite 锁超时路径改为降级而不是卡死：`mnemos health --json`、`mnemos status` 和 MCP/诊断面遇到 SQLite lock timeout 时返回降级信息；`mnemos_daemon.py start` 的启动确认等待时间覆盖真实初始化耗时，并在状态文件超时时用仍在运行的 daemon PID 兜底。
- MCP 画像相关入口在画像库被 daemon 长连接占锁时不再把 `preflight_inject` / `guard_check`、reflection 和 persona metrics 变成工具执行错误；`core.persona.psyche.SignalStore` 的默认 SQLite 连接/忙等待预算收紧到 2 秒，`core.application.kia.KiaApplicationService` 会捕获 `PreFlightInjector` 初始化期 SQLite 锁超时，`preflight_inject` 返回带 `degraded_reason` 的成功响应，`guard_check` 回退到默认守护清单继续给出风险判断；`guard_check` 触发的 `guard_alert` 遥测事件在当前进程没有 EventBus 消费者时不再初始化全局 EventBus，也不会因为 daemon 持有 `events.db` 锁把 MCP 响应变成工具错误；`core.application.reflection` 会在画像库不可用时以 `persona_store=None` 启动 ReflectionEngine，`persona_behavior_metrics` 会把 `profile_usage` 降级为空指标。新增 SignalStore、EventBus/Aegis、应用服务、MCP facade 和 stdio 协议回归测试覆盖这些路径。
- `distillation_json.extract_json()` 不再在直接 `json.loads()` 失败后立即输出 `json.JSONDecodeError suppressed` warning；fallback 成功只记录 debug/metrics，只有所有路径失败才 warning。LLM 调用 usage 会携带 `json_parse` 元数据，失败蒸馏文件包含 `failure_class`、`error_fingerprint`、`parse_metadata` 和 `raw_output` 路径，同类格式失败复盘按错误指纹合并，避免重复高优先级提醒堆积。
- `core/scoring/adaptive_scorer_v2.py` 中两个只围绕 SQLite/序列化写入的 broad `except Exception` 已收窄为 `sqlite3.Error` 或 `sqlite3.Error/TypeError/ValueError`，降低核心评分路径的 broad catch 数量。
- `scripts/audit_orphan_modules.py` 不再默认改写 `docs/orphan-modules-report.md`；默认运行和 `--check` 都保持 repo 只读，只有显式 `--output ... --apply` 才写 repo 报告。
- `scripts/security_audit.py --strict` 现在会在 Bandit medium 回归时失败；本轮把 delivery/readiness/health/data ownership/evidence backfill/E2E cleanup 等动态 SQL 拼接收敛到 `validate_sql_identifier()`、固定 allowlist 和精确 `# nosec B608`，并补非法 identifier 单测，当前安全审计 high=0、medium=0。
- `config/config.example.json`、`config/config.example.yaml` 与 `config/.env.example` 重新由 `DEFAULT_CONFIG` / env 分组生成，补齐 `evidence_backfill` 和 `MNEMOS_RETENTION_DAYS_DISTILLATION_CHUNKS`；`scripts/verify_config_examples.py --strict` 现在要求三类公开样例达到 100% 覆盖，普通模式仍保留 95% 日常阈值。
- `scripts/security_audit.py` 现在直接运行时优先选择 repo `.venv`，并用同一解释器执行 bandit、pip-audit 与 health security；`--strict-env`/`--no-venv-autodetect` 用于检查当前解释器依赖，缺少 `bandit` 或 `pip_audit` 时输出明确安装命令，避免系统 Python 的 `No module named ...` 被误判为安全审计失败。
- `mnemos health --json` 新增非 strict `checks.security`：复用 `scripts.health_check.check_security()` 输出权限、keyring、secret inventory、旧 credential、pickle/weak hash 状态；`~/.mnemos/logs` 与 database logs 在配置初始化和 auto setup 中收敛为 `700`，权限违规会给出 `chmod` repair action，plaintext secret-like 配置会进入 `secret_inventory.plaintext_count` 并让 `scripts/security_audit.py --strict` 失败。F25 后 keyring 不可用会显示 `keyring_error`、`keyring_status`、`keyring_risk_level=safe_but_not_best`、`env_fallback_accepted` 和 `mnemos secrets doctor` 修复入口，不再只给泛化 `env:` fallback 说明。
- `scripts/e2e_probe.py` 收紧真实落地验收：canonical raw 模式必须反查 `raw_events.db.raw_turns.event_id` 与 `sync_log` row，外部 backend 模式必须从非空 `backend_uids` 反查实际记录；`skipped_backend`、空 `backend_uids` 或仅有 `sync_log.status=new` 不再单独判 pass。Wiki 检查绑定本次 `session_id`，蒸馏 skip 时 Wiki 明确 skip；`--dry-run --no-api` 默认脱敏路径，`--real-api` 蒸馏最多做一次真实重试但仍以实际写出 Wiki 页面为 pass；cleanup 分别清理并报告 `raw/sync_log/wiki/backend`。
- `scripts/audit_cognitive_readiness.py --json` 新增 `source_refs_nonempty` 和 `source_count_positive_empty_refs` 指标，避免只看 `source_count` 而漏掉来源列表为空的两张皮问题。
- `AdaptiveScorerV2.ensure_tables()` 会为旧 `search_sessions` 表补齐 `opened_path/opened_at/ignored_at/outcome_status/outcome_at` 字段；`ContextAwareSearch` 在搜索无结果、点击/打开结果、忽略结果时写入可审计行为闭环，避免长期只有 session 但没有反馈结果。
- `scripts/ci_ratchet_baseline.json` 将 P0.5 EvidenceBackfill 引入的已分类 `runtime_data_io` 配置读取预算从 `285` 前移到 `286`，避免总 gate 把本切片的可解释配置读取误报为新增架构债。
- `scripts/ci_ratchet_baseline.json` 将 P1 DistillActionRouter/只读 action log CLI 引入的已分类 `runtime_data_io` 配置读取预算从 `286` 前移到 `288`。
- `scripts/ci_ratchet_baseline.json` 将 P2 CognitiveConsolidator 引入的已分类 `runtime_data_io` 配置读取预算从 `288` 前移到 `291`；`cognitive_consolidation.candidate_limit`、`raw_purge_limit`、`min_key_details`、`max_key_details` 等阈值均为配置项。
- `KnowledgeTrustScorer` 的 decision id / negative evidence id 使用 nonce，避免同秒同 subject/action 反复决策时覆盖 `trust_decisions.db` 审计行；`trust.*` 阈值和 penalty 写入默认配置，运行时通过 `~/.mnemos/configs/main.json` 调整。
- 新增 `policy_patch.*` 默认配置，包含 `enabled`、`db_path`、`ttl_days`、`min_confidence`、`max_active`；策略补丁 TTL、最低置信度和最大注入数量不在 KIA 入口硬编码。
- 新增 `delivery.*` 默认配置，`delivery.preference=balanced`，`quiet/balanced/active` 三档预算均从 `~/.mnemos/configs/main.json` 读取；`overflow_defer_hours` 控制 reminder 超预算后的推迟时长。`app.push_max_items` 仅作为迁移期 per-task 兜底，不再作为业务逻辑硬编码次数来源。
- `AdaptiveConfig` 运行时消费者从单一 push 数量扩展到蒸馏分片通过率、质量门阈值/复核区间、raw retention、文档大小上限、intent LLM fallback、投递预算和 trust delivery gate；后台指标新增 search no_result、raw partial、quality gate reject/review、delivery dismiss、document rejection 等来源。新增 `get_shadowed_value()` 保证只有 active shadow 才覆盖调用方配置，避免全局默认吞掉测试或嵌入式配置。
- 新增 `verification_queue.*` 默认配置，包含 DB/report/blindspot 路径、`max_candidates`、三类来源上限、`cron` 和 `respect_resource_budget`；后台求证队列不再把扫描次数、运行路径或调度时间硬编码在 Chronos/CLI 中。
- `ApplicationHub` 不再为 `predictive_push` 维护本地 `max_per_batch=3` / 10 分钟硬编码限流；单批上限读取 `DeliveryBudgetPolicy.per_task_total`，topic 冷却由 DeliveryRouter/PushPenaltyTracker 承接。
- `core/frontmatter.py` 增加 `reinforcement_count`、`reinforced_at`、`reinforcement_source_event_ids` 字段契约，确保 reinforce 写回不会丢失或吞掉强化元数据。
- 修复本地门禁回归：`scripts/security_audit.py` 可解析 Bandit 进度文本前缀后的 JSON 输出；同时收窄 `HephaestusWorker` token 估算 fallback 异常、补齐 vault/retrospective/charon/adaptive config 的 flake8/mypy/security 标注，使 `scripts/run_local_gates.py` 恢复全绿。
- `scripts/auto_setup.py --yes --preserve-config` 的模型端点 smoke 复用运行时解析器，embedding/reranker 可正确复用全局 `SILICONFLOW_API_KEY`；安装末尾改为 `scripts/e2e_probe.py --dry-run --no-api`，避免真实 API E2E 超时阻断配置安装验收。
- `scripts/auto_setup.py` 写 `~/.mnemos/configs/main.json` 和 distill 策略时强制 `0600`，避免配置重写后被 `doctor config --strict` 或 health security 报权限违规。
- 执行 `mnemos migrate apply config.stale_keys.v1 --json` 清理当前 `~/.mnemos/configs/main.json` 中的退役配置 key、旧 HTTP token 配置和已移除开关；迁移前备份写入 `~/.mnemos/migrations/config_backups/`，迁移账本写入 `~/.mnemos/migrations.db`。
- `DistillationEngine` 与 `DocumentDistillationPipeline` 不再默认生成 `{session}_{title}.md`，避免 source/session 前缀污染 Obsidian 页面名空间。
- `Charon._move_page_to_category()` 在从 Inbox 移动页面时按页面标题生成目标文件名，并用标题归一化 stem 检测正式知识区碰撞，防止 `codex-20_标题.md` 绕过已有 `标题.md`。
- `ShadowPageManager` 新生成影子页改为 `shadow-{hash}.shadow.md`，写入新投影时会清理同一页面对应的旧长文件名 shadow。
- `KGExporter` 新生成关系投影改为 `rel-{hash}.md`，MOC 链接仍展示完整 `source → relation → target`，下一次受控 KG 重建会清理旧长关系文件。
- `ForcedRetrospective.create_system_recap()` 的系统复盘任务 ID 从秒级改为微秒级，避免批量创建不同 topic 的 pending recap 时同秒 task_id 碰撞。

## [Unreleased] — Step 10 全局门禁与回归清理（2026-07-01）

### Added
- 强制复盘从提醒/待办升级为结构化链路：新增三问状态机、正式 Wiki 入库、消费计划、跳过事件存储，以及 `recap_start` / `recap_submit` / `recap_finalize` / `recap_skip` / `recap_feedback` / `recap_status` / `recap_claim_owner` MCP 工具；补 `tests/unit/test_retrospective_workflow.py` 覆盖入库、跳过语义、owner 锁和契约拒绝。

### Fixed
- 重新补齐 No Zombie Code Policy 的真实执行面：新增 `scripts/check_zombie_code_policy.py`、`scripts/zombie_code_baseline.json`、`tests/unit/test_check_zombie_code_policy.py`，并接入 CI、pre-commit 和 `scripts/run_local_gates.py`；2026-07-09 起 baseline 每项必须有 owner、callers、remove_when 和未过期 expires_at，99 个候选均已补责任计划。
- `scripts/run_local_gates.py` 优先使用仓库 `.venv/bin/python`，避免全局 Python 缺少 dev tools 时误报。
- `scripts/security_audit.py` 兼容当前 `pip-audit --format=json` 的 `dependencies` 输出结构；本地门禁改为运行统一安全审计，任何已知依赖漏洞都会阻断。
- 清零 mypy、bare except、bandit、CI ratchet gate 回归；`audit_config_reads` 重新分类后无 `unclassified` direct reads。
- 修正 RawIndex 表计数动态 SQL、Codex rollout 类型注解、Agent capability 类型声明、诊断 Optional 路径窄化。
- legacy `ObsidianBackend` 的 RawIndex 数据库改为当前 vault/chatlog 内 `.raw_index.db`，避免多个临时 vault 或兼容 backend 共享全局 `raw_index.db` 造成 SQLite 写锁竞争和相对路径覆盖。
- 对齐 `vaults sync` 测试的默认 dry-run / `--apply` 语义，并补齐 CaptureService 测试 fake config 的 vault 路径。
- `RelationManager.discover_implicit_relations_batch()` 为单次隐式关系批处理构建可复用 Wiki 索引，避免每个实体反复全库扫描 Markdown；索引仅本次调用有效，新增 Markdown 会在下一次批处理被读取。
- `KGEventHandler.on_distilled()` 使用批量隐式关系发现，并通过 `knowledge_graph.implicit_relation_discovery_enabled` 与 `knowledge_graph.implicit_relation_max_entities_per_event` 控制事件热路径成本。
- `IntentRouter` 默认关闭 LLM fallback（`intent_router.llm_fallback_enabled=false`），新增 `intent_router.llm_fallback_timeout_seconds` 短超时保护；中英文知识问句优先走本地规则。
- `IntentRouter` 补齐英文动作词，并在 knowledge/task 同时命中且存在强动作词时优先 task，避免 `compare/explain + fix/update` 被误路由为知识查询。
- `RelationManager` 在构建隐式关系索引时预计算 `entity -> pages`，查找阶段不再对每个实体遍历全部页面文本。
- 新增 `scripts/run_tests.py` 测试分层入口：`quick`、`integration`、`heavy`、`full`，缩短日常反馈环并保留完整回归入口。
- 删除已废弃的 `scripts/ingest_engine_service.py` Clean/Expand 占位入口；当前素材摄入与蒸馏入口统一走 L1 raw storage、Capture/Sync、amphora 与 HephaestusWorker 链路。
- 删除已废弃的 `scripts/batch_clean_submit.py` Clean 提交占位入口；不再保留只返回 `(0, 0)` 的假提交脚本。
- `McpOnlyAgentStatusProvider` 纳入 Crush MCP-only 诊断项，`mnemos agent kit/doctor crush` 可直接读取 `~/.config/crush/crush.json` 与 `~/.config/crush/CRUSH.md` 的 active 接入状态。
- `SessionInfo` 新增 additive `metadata`，`SyncEngine._raw_source_metadata()` 会合并 session-level metadata；Crush source discovery 现在保留 `parent_session_id`、title、message_count 与创建/更新时间，避免会话树证据只被 SELECT 后丢弃。
- 补齐 `DistillationResult` 输出契约测试，锁定 `data_profile`、`anomalies`、`needs_reconfirm`、`reconfirm_question` 和 `prejudgment_confidence` 的 dataclass 序列化形状。
- 补齐 `distill_and_write()` 高阶入口测试，锁定蒸馏、写页、`knowledge_distilled` 事件/同步兜底和 Embedding 增量触发都集中在该便捷入口。
- `AgentAdapter.delegate_distillation()` 保留为外部 adapter 兼容废弃窗口，但现在显式发出 `DeprecationWarning`，并在 zombie baseline 中写明后续主版本/第三方 adapter 迁移完成后的 removal target。
- `HephaestusWorker.get_delegated_count()` 继续作为旧 stats shim 保留并返回 `0`，但测试已锁定 `DeprecationWarning`，zombie baseline 写明 `delegated` 统计字段移除后的删除条件。
- `EventBus.poll()` / `ack()` / `move_to_processing()` 继续作为旧手动消费协议兼容面保留；新增直接 deprecated API 测试，并在 zombie baseline 中写明后续主版本/外部消费者迁移完成后的 removal target。
- `WikiBuilder.run_build_cycle(use_pipeline=...)` 继续作为回追入口参数兼容面保留；`use_pipeline=False` 和 CLI `--no-pipeline` 均会强制走流水线，docstring/help/baseline 已写明后续主版本删除窗口。
- `build_session_text(max_chars=..., per_message_limit=...)` 继续把旧字符预算转换为 token 预算；新增直接转换测试，docstring/baseline 写明迁移到 `max_tokens` / `per_message_token_limit` 后删除。
- `ContentFormatter.format_session(max_chars=...)` 继续作为旧字符截断兼容参数保留；已有单测锁定旧行为，docstring/baseline 写明迁移到 `max_tokens` 后删除。
- `DocumentProcessor.validate_extraction()` 文档已改为“旧 AgentDelegate 验证模式已退役，本方法仍是当前本地验证入口”，并移除对应 stale zombie baseline。
- `core.prometheus_fire` 明确为 `QueueDistillTask` 队列任务 DTO 模块；旧 AgentDelegate 委托模式说明改为历史备注，相关测试审计文档不再把它标为当前 Agent 委托器。
- Vulture 白名单补充 K-09 复审规则：CLI/daemon 动态入口、pytest fixture/mock helper、sqlite `row_factory`、DTO/dataclass 字段不能仅凭静态未读删除，优先使用精确 `# noqa` 或审计规则。
- Vulture whitelist 技术债进入后续分批治理：W0-1 已移除 9 条陈旧白名单项（`DecisionDependencyExtractor`、`estimate_cost`、`StatFileWatcher.prime`、两处 `__exit__` 协议参数的旧 `exc_*` 记录），预算从 `292/310` 降至 `283/310`；本批不改运行行为。
- Vulture whitelist W0-2 将 `Config.is_source_enabled()` 确认为 persona 数据源开关的公共配置 API：新增契约测试，定义处加精确 `# noqa`，并从全局白名单移除该条，预算降至 `282/310`。
- Vulture whitelist W0-3 将 `tests/unit/test_mnemos_cli.py` 的 `FakeConfig.l1_storage_token` / `l1_storage_api_url` 迁为测试源码旁精确豁免，保留旧 L1 配置替身形状，并从全局白名单移除两条测试属性，预算降至 `280/310`。
- Vulture whitelist W0-4 将 4 个测试动态/替身项迁为源码旁精确豁免：`MagicMock.__getitem__` row 协议、两个 pytest `autouse` fixture、`MockSignalStore.add_wiki()`；预算降至 `276/310`。
- Vulture whitelist W0-5 清理 5 条测试债：保留并标注 sklearn `Pipeline` module mock，删除两个未引用 `DocumentJudgeResult` fixture，移除两个未使用 helper 参数（`period_end_days_ago`、`title2`），预算降至 `271/310`。
- Vulture whitelist W0-6 清空剩余测试目录白名单：将 4 个测试变量改为有效断言或私有协议参数（`analysis_type`、`expect_json`、`file_obj`、`mtype`），当前全局白名单不再包含 `tests/` 条目，预算降至 `267/310`。
- Vulture whitelist W0-7 将 `EventBus.poll()` / `ack()` / `move_to_processing()` 旧手动消费兼容 API 迁为源码旁精确豁免，保留后续主版本/外部消费者迁移前的废弃窗口，预算降至 `264/310`。
- Vulture whitelist W0-8 将 watchdog 动态回调 `WikiAutoCommitHandler.on_any_event()` 与 `_DebounceHandler.on_created/on_modified()` 迁为源码旁精确豁免，预算降至 `261/310`。
- Vulture whitelist W0-9 将 `CodexSource` / `HermesSource` / `OpenClawSource` 内置 source 插件类迁为源码旁精确豁免，并新增 `SourceRegistry.register_builtin_agents()` 反射注册契约测试，预算降至 `258/310`。
- Vulture whitelist W0-10 将 `Event.from_file()` 旧文件事件兼容 API 与 `InProcessGuard.from_task_type()` 守护工厂入口迁为源码旁精确豁免；补旧格式事件文件映射、缓存命中和注入器加载契约测试，预算降至 `256/310`。
- Vulture whitelist W0-11 删除 3 个已下沉到 `daemon.raw_sync` / `daemon.maintenance` / `service_persona_analyzer()` 的 daemon 私有 wrapper（`_select_l1_sources`、`_build_push_context`、`_trigger_persona_analysis`），并将退役同步服务别名迁为源码旁精确豁免，预算降至 `252/310`。
- Vulture whitelist W1-1 将 `HephaestusWorker.get_delegated_count()` 废弃 stats shim 与 `watch_queue()` 运维轮询入口迁为源码旁精确豁免，并新增 `watch_queue()` callback/stop smoke，预算降至 `250/310`。
- Vulture whitelist W1-2 将 `TriggerDispatcher.unregister()` 热插拔生命周期入口迁为源码旁精确豁免，并新增注销时停止 trigger、清理路径表和缺失 source 幂等测试，预算降至 `249/310`。
- Vulture whitelist W1-3 将 `Config._default_wiki_path()` 旧 wiki 默认路径 alias 迁为源码旁精确豁免，并新增 alias 指向主认知 vault 默认路径的契约测试，预算降至 `248/310`。
- Vulture whitelist W1-4 将 `SiliconFlowEmbeddingClient._resolve_base_url()` 旧 embedding base_url 解析入口迁为源码旁精确豁免，并新增统一 embedding 配置解析契约测试，预算降至 `247/310`。
- Vulture whitelist W1-5 将 `EventProcessor` 旧 daemon 轮询兼容层迁为源码旁精确豁免，并新增 `register + process_all` 契约测试锁定 pending 事件处理与归档行为，预算降至 `246/310`。
- Vulture whitelist W1-6 将 `scripts/migrate_db.py --rollback VERSION` 的目标版本参数改为真实输出项，保留人工恢复 CLI 语义并从全局白名单移除 `target_version`，预算降至 `245/310`。
- Vulture whitelist W2-1 将 `CaptureService.get_pending_counts()` 吸收到 MCP/facade `capture_status` 状态面：返回 capture_queue pending 总量和按 source 分布，并从全局白名单移除，预算降至 `244/310`。
- Vulture whitelist W2-2 将 `ForcedRetrospective._create_from_session_end()` 确认为 wiki_builder 蒸馏跳过复盘触发器，并补低质量跳过、管道跳过和忽略未知原因的契约测试，预算降至 `243/310`。
- Vulture whitelist W2-3 删除 `CrossAgentLinker._get_dna()` 未接线 DNAEngine 懒加载 shim；跨 Agent 关联保留向量、关键词和零依赖文本相似度三层路径，并补“不初始化 DNAEngine”契约测试，预算降至 `242/310`。
- Vulture whitelist W2-4/W2-5 将 `EvidenceGraph.add_mirror_observations()` 与 `add_insight_derivation()` 吸收到 `add_reflection_record()` 真实写图路径，并补两个语义接口的直接契约测试，预算降至 `240/310`。
- Vulture whitelist W2-6 将 `AdaptiveConfig.add_rule()` 吸收到 `adaptive_config.rules` 配置加载路径，保留为自定义调参规则公共 API，并补直接 API、配置加载和非法范围契约测试，预算降至 `239/310`。
- Vulture whitelist W2-7 将 `TaskClassifier.classify_and_confirm()` 吸收到公开 `classify_task(..., llm_confirm_callback=...)` 入口，保留“分类 + 可选确认”语义并补确认回调契约测试，预算降至 `238/310`。
- Vulture whitelist W2-8 将 `IssueRegistry.count_by_severity()` 吸收到 `KnowledgeScheduler._run_issue_pipeline()` 状态输出，补 registry 统计与 scheduler 可观测契约测试，预算降至 `237/310`。
- Vulture whitelist W2-9 将 `RelationEngine.decrement()` 保留为旧共现权重负反馈/回滚契约，不重启已停用的 RelationEngine 主链路；补持久化递减、clamp 和非法 amount 契约测试，预算降至 `236/310`。
- Vulture whitelist W2-10 将 `TimeCapsule.dismiss_reminder()` 接入 `mnemos capsule dismiss <id>` 用户动作，补 TimeCapsule 状态更新与 CLI 路由/执行契约测试，预算降至 `235/310`。
- Vulture whitelist W2-11/W2-13 将 `ContextSignal.emotional_state` 吸收到预测推送动态阈值和决策理由：困惑/紧急情绪会降低推送阈值并写入 `PushDecision.reason` / 推送历史，全局白名单移除 3 条，预算降至 `232/310`。
- Vulture whitelist W2-14 将 `KnowledgeGraph.export_dataview_query()` 接入 `mnemos kg export-dataview <page>`，保留 Obsidian Dataview 查询块导出能力并补 KG/CLI 契约测试，预算降至 `231/310`。
- Vulture whitelist W2-15 保留并修正 `DNAEngine.find_cluster()`：BFS 现在按请求 depth 扩展多层 DNA 知识簇，补直接契约测试并从全局白名单迁出，预算降至 `230/310`。
- Vulture whitelist W2-16 将 `PageTrail.first_accessed` 保留为 Ariadne/page_stats 轨迹输出契约，补直接读取测试并从全局白名单迁出属性与 dataclass 字段两条记录，预算降至 `228/310`。
- Vulture whitelist W2-17/W2-18 将 `EffectivePolicy.force_commit()` / `force_rollback()` 吸收到 `mnemos policy list|commit|rollback` 人工裁决入口，保留自适应配置安全窗口的手动提交/回滚能力并补底层/CLI 契约测试，预算降至 `226/310`。
- Vulture whitelist W2-19 将 `ProfileGenerator.generate_and_report()` 确认为 Chronos `knowledge_profile` 默认调度步骤的动态入口，补调度契约测试并迁为源码旁精确豁免，预算降至 `225/310`。
- Vulture whitelist W2-20 将 `KnowledgeImmuneSystem.generate_report_markdown()` 吸收到 `mnemos immune scan --write-report` 用户可见入口，补 Markdown 渲染与 CLI 路由/handler 契约测试并迁为源码旁精确豁免，预算降至 `224/310`。
- Vulture whitelist W2-21 将 `ObservationIndex.get_by_dimension()` 吸收到 `mnemos observe search --dimension ...` 与应用服务 `observation_search()` 的维度查询路径，补 Index/CLI/facade 契约测试并从全局白名单移除，预算降至 `223/310`。
- Vulture whitelist W2-22 将 `DialogReminderQueue.get_by_issue()` 吸收到 `mnemos reminder resolve --issue <issue_id>`，保留按问题关闭未解决提醒的低摩擦入口，补队列/CLI/main 路由契约测试并从全局白名单移除，预算降至 `222/310`。
- Vulture whitelist W2-23 将 `SignalStore.get_daily_summary()` 吸收到 `mnemos persona daily-summary [date]`，索引为空时从现有信号表实时生成日聚合摘要，补 SignalStore/CLI/main 路由契约测试并从全局白名单移除，预算降至 `221/310`。
- Vulture whitelist W2-24 将 `AdaptiveConfig.get_effective()` 吸收到 `DialogReminderQueue._get_max_per_session()` 推送上限读取链路；未注入 policy 时默认通过 AdaptiveConfig 读取 `app.push_max_items`，补提醒队列契约测试并从全局白名单移除，预算降至 `220/310`。
- Vulture whitelist W2-25 将 `EvidenceGraph.get_evidence_chain_for_insight()` 吸收到 `EvidenceGraph.explain_why()` 证据解释输出；`explain_why()` 现在返回 `direct_evidence_chain`，并把 Insight 直接指向的 Observation/Knowledge 节点合并进证据列表，补 EvidenceGraph 契约测试并从全局白名单移除，预算降至 `219/310`。
- Vulture whitelist W2-26 将 `TaskClassifier.get_expected_goal_prompts()` 吸收到 `_extract_expected_goals()` 默认提示生成链路；分类结果无显式目标时通过该公共方法填充 `_prompts`，补内置/自定义 taxonomy/未知类型兜底契约测试并从全局白名单移除，预算降至 `218/310`。
- Vulture whitelist W2-27 将 `FeedbackCollector.get_feedback_history()` 吸收到 `ReflectionEngine.get_feedback_history()` 门面；支持按反馈类型过滤历史反馈，补 collector/engine 契约测试并从全局白名单移除，预算降至 `217/310`。
- Vulture whitelist W2-28 将 `WikiReader.get_knowledge()` 确认为 `build_wiki_section(mode="deep")` 的深度 Wiki 预加载入口，补 deep preflight 调用参数契约测试并移除陈旧全局白名单，预算降至 `216/310`。
- Vulture whitelist W2-198 删除 `_BehaviorCalibrator._adjustments` 未使用私有缓存和空 `__init__`；校准仍由调用局部 `adjustments` 计算，相关 Pythia 测试通过，预算降至 `34/310`。
- Vulture whitelist W2-199 删除 `InProcessGuard._current_knowledge` 两处未使用实例状态；知识复用仍由类级 `_KNOWLEDGE_CACHE` 提供，相关 Aegis 测试通过，预算降至 `32/310`。
- Vulture whitelist W2-200 将 `_DomainPreferenceAnalyzer` 接入 `PreferenceProfile.domain_preferences`；全量和增量画像分析都会输出 domain 偏好分，相关 Pythia 测试通过，预算降至 `31/310`。
- Vulture whitelist W2-201 删除 `InProcessGuard._last_action_turn` 三处仅写不读动作轮次状态；分析轮计数和工具读取去重逻辑保持不变，相关 Aegis 测试通过，预算降至 `28/310`。
- Vulture whitelist W2-202 将 `EventBus._max_latency_ms` 接入 `stats()` 可观测输出，保留 EventBus 延迟阈值配置契约，相关 EventBus 测试通过，预算降至 `27/310`。
- Vulture whitelist W2-203 将 `ConflictResolver._meta_provider` 接入 `arbitrate()` 缺省元数据获取路径，保留可注入元数据解析器契约，相关冲突仲裁测试通过，预算降至 `26/310`。
- Vulture whitelist W2-204 删除 Aegis 上未读取的 `blindspot_manager` / `challenge_balancer` 急切实例状态，Hamartia 能力仍由 persona 模块自身提供，相关 Aegis 测试通过，预算降至 `24/310`。
- Vulture whitelist W2-205 将 Chronos `KnowledgeScheduler.get_last_results()` 接入 `mnemos scheduler status` 的 `live_status` / `live_error` 输出，保留最近一次 tick 内存结果可观测契约，相关 CLI/Chronos 测试通过，预算降至 `23/310`。
- Vulture whitelist W2-206 将 `AdaptiveConfig.get_metrics_summary()` 接入 `mnemos status` 的自适应配置指标输出，展示 `ewma`、趋势、最近值和样本数，相关 status/AdaptiveConfig 测试通过，预算降至 `22/310`。
- Vulture whitelist W2-207 将 `SignalStore.get_project_isolated_signals()` 接入 `mnemos persona project-signals <project_dir>`，用于查看 session/git/file_system 的项目隔离画像信号；同批补齐项目路径 LIKE 通配符转义，相关 persona/Psyche 测试通过，预算降至 `21/310`。
- Vulture whitelist W2-208 将 `PredictivePushEngine.get_push_stats()` 接入 `mnemos push stats [--days N] [--json]`，输出推送总数、反馈分布和接受率，相关 push/Teiresias 测试通过，预算降至 `20/310`。
- Vulture whitelist W2-209/W2-210 将 `SignalStore.get_recent_note_signals()` / `get_recent_wechat_signals()` 接入 `mnemos persona recent-signals [--source all|notes|wechat] [--json]`，输出最近 notes/wechat 原始画像信号，相关 persona/Psyche 测试通过，预算降至 `18/310`。
- Vulture whitelist W2-211~W2-213 删除 Charon 已退休 `RelationEngine.get_related_sessions()` / `get_weight()` / `get_total_mentions()` 内存态 introspection helpers；`ConnectModule` 不再启动旧共现主链路，关系分析继续由 KnowledgeGraph / EntityManager 承担，相关 Charon 测试通过，预算降至 `15/310`。
- Vulture whitelist W2-214 将 `TimeParser.get_reminder_days_before()` 固化为 full KIA 延期调度提醒提前量契约；中期任务提前 3 天、长期任务提前 7 天，`_build_full_kia()` 调度延期任务时输出提醒提前量，相关 Kairos/Preflight 测试通过，预算降至 `14/310`。
- Vulture whitelist W2-215 修正 `DecisionGraph.get_root_decisions()` 根决策语义：依赖边 `from -> dependency` 中有出边或 `dependencies` 非空的节点不再被误判为根；distillation self-check 的 `decision_graph.roots` 由该 API 计算，相关决策依赖/self-check 测试通过，预算降至 `13/310`。
- Vulture whitelist W2-216 将 `SignalStore.get_signal_projects()` 接入 `mnemos persona projects [--days N] [--json]`，用于列出可继续用 `persona project-signals` 深查的 session/git 项目候选；同批修复空项目路径进入候选列表的问题，相关 persona/Psyche 测试通过，预算降至 `12/310`。
- Vulture whitelist W2-217 将已由 full KIA preflight 消费的 `TaskClassifier.get_task_type_label()` 迁出全局白名单；标签格式 `类型/子类型` 由直接契约测试覆盖，相关 Dike/Preflight 测试通过，预算降至 `11/310`。
- Vulture whitelist W2-218 将 `KnowledgeTrail.get_user_journey()` 接入知识使用周报“最近知识路径”段落；session 过滤与周报展示由 Ariadne 测试覆盖，预算降至 `10/310`。
- Vulture whitelist W2-219 将 `ProfileGenerator.incremental_update()` 接入 `wiki_page_updated` 写页事件同步路径；新增 `sync_profile_update()` 事件 payload helper，画像增量更新和事件发布同步由 Metis 测试覆盖，预算降至 `9/310`。
- Vulture whitelist W2-220 移除 `SignalStore.insert_wechat_signal()` 陈旧白名单项；该 API 已由微信信号入库、情绪字段和最近信号读取契约测试直接覆盖，预算降至 `8/310`。
- Vulture whitelist W2-221 将 `BlindSpotReminder.is_actionable` 迁出全局白名单；`detected`/`reminded` 可操作状态策略由直接契约测试覆盖，预算降至 `7/310`。
- Vulture whitelist W2-222 将 `ConflictResolver.resolve_all()` 吸收到 `resolve_all_conflicts()` 兼容函数执行面；批量仲裁继续使用稳定 claim key 取元数据，预算降至 `6/310`。
- Vulture whitelist W2-223 将 `scripts/curator.py archive_pages()` 接入 curator merge 请求的合并前备份路径；`--daily-merge` 等非 dry-run 请求会先复制候选页到 `.archive/curator-*`，预算降至 `5/310`。
- Vulture whitelist W2-224 将 `EvidenceNodeType.USER_FEEDBACK` 固化为用户反馈证据节点 JSON/DB 契约；`test_feedback_on_relation_type_is_serialized_contract` 覆盖节点类型序列化与 DB round-trip，预算降至 `4/310`。
- Vulture whitelist W2-225 将 `validate_tag()` 接入 `build_tag_string()` 标签 frontmatter 写出路径；未知键必须使用 `x-` 前缀，否则抛出 `ValueError`，预算降至 `3/310`。
- Vulture whitelist W2-226 将 `WikiPageMeta.verification_history` 固化为冲突仲裁元数据 DTO/asdict 契约，预算降至 `2/310`。
- Vulture whitelist W2-227 将 `DocumentJudgeResult.why` 固化为文档价值判断 DTO/asdict 契约；LLM judge 解析结果和 dataclass 序列化均覆盖该字段，预算降至 `1/310`。
- Vulture whitelist W2-228 将 `DistillProgress.WRITING` 固化为 Amphora 队列 `writing` 进度阶段契约；`update_progress()` 写入与 `list_pending()` 读回均由测试覆盖，`vulture_whitelist.py` 清零，预算降至 `0/310`。
- Function matrix 审计支持带 CLI option 的用户入口声明；`cli:reminder resolve --issue` 现在按真实 argparse 命令路径 `reminder resolve` 校验，并补回归测试。

### Tests
- `python3 scripts/check_zombie_code_policy.py`：`OK: 97 zombie-code candidate(s) documented.`
- `python3 -m pytest tests/unit/test_check_zombie_code_policy.py -q`：`4 passed`。
- `python3 -m pytest tests/unit/test_llm_config.py tests/unit/test_daemon_watchers.py tests/unit/test_entity_manager.py -q`：`101 passed`。
- `python3 -m pytest tests/unit/test_config.py -q`：`24 passed`。
- `python3 scripts/audit_vulture_whitelist.py --dry-run`：`282 remaining`。
- `uv tool run --from "vulture>=2.11,<3" vulture . vulture_whitelist.py --min-confidence 80`：通过，无输出。
- `python3 -m pytest tests/unit/test_mnemos_cli.py -q`：`141 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 280/310`。
- `python3 -m pytest tests/unit/test_pythia_p3.py tests/unit/test_cross_agent_integration.py tests/unit/test_pythia.py -q`：`145 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 276/310`。
- `python3 -m pytest tests/unit/test_adaptive_scorer_v2.py tests/unit/test_document_pipeline.py tests/unit/reflection/test_mirror_engine.py tests/unit/test_fragment_merger.py -q`：`153 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 271/310`。
- `python3 -m pytest tests/unit/test_fragment_merger.py tests/unit/test_document_pipeline.py tests/unit/test_file_ingestor.py tests/integration/test_scorer_v2_training_loop.py -q`：`73 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 267/310`。
- `python3 -m pytest tests/unit/test_mnemos_bus.py -q`：`69 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 264/310`。
- `python3 -m pytest tests/unit/test_daemon_triggers.py tests/unit/test_daemon_triggers_and_ingest.py tests/unit/test_triggers.py -q`：`50 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 261/310`。
- `python3 -m pytest tests/unit/test_registry.py tests/unit/test_codex_source.py tests/unit/test_hermes_source.py tests/unit/test_openclaw_source.py -q`：`87 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 258/310`。
- `python3 -m pytest tests/unit/test_mnemos_bus.py tests/unit/test_aegis.py tests/unit/test_aegis_context.py -q`：`108 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 256/310`。
- `python3 -m pytest tests/unit/test_mnemos_daemon.py tests/unit/test_daemon_service_registry.py tests/unit/test_daemon_raw_sync.py tests/unit/test_daemon_maintenance.py -q`：`44 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 252/310`。
- `python3 -m pytest tests/unit/test_hephaestus_worker.py -q`：`31 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 250/310`。
- `python3 -m pytest tests/unit/test_triggers.py tests/unit/test_daemon_triggers.py tests/unit/test_daemon_triggers_and_ingest.py -q`：`52 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 249/310`。
- `python3 -m pytest tests/unit/test_config.py tests/test_audit_config_reads.py tests/unit/kia/test_adaptive_config_effective.py -q`：`41 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 248/310`。
- `python3 -m pytest tests/unit/test_embedding_client.py tests/unit/test_llm_config.py -q`：`64 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 247/310`。
- `python3 -m pytest tests/unit/test_mnemos_bus.py -q`：`71 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 246/310`。
- `python3 -m pytest tests/unit/test_migrate_db.py -q`：`1 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 245/310`。
- `python3 -m pytest tests/unit/test_capture_service.py tests/integration/test_mcp_capture_loop.py -q`：`48 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 244/310`。
- `python3 -m pytest tests/unit/test_forced_retrospective.py -q`：`17 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 243/310`。
- `python3 -m pytest tests/unit/test_cross_agent_linker.py tests/unit/test_kia_module_registry.py -q`：`16 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 242/310`。
- `python3 -m pytest tests/unit/evidence/test_evidence_graph.py -q`：`14 passed`。
- `python3 -m pytest tests/unit/reflection/test_reflection_capability.py -q`：`6 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 240/310`。
- `python3 -m pytest tests/unit/kia/test_adaptive_config_effective.py tests/unit/test_kia_module_registry.py -q`：`11 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 239/310`。
- `python3 -m pytest tests/unit/test_dike.py tests/unit/test_kia_module_registry.py -q`：`11 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 238/310`。
- `python3 -m pytest tests/unit/test_issue_pipeline.py tests/unit/test_kia_module_registry.py tests/test_functional.py::TestKnowledgeSchedulerFunctional::test_issue_pipeline_step_scans_and_fixes -q`：`30 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 237/310`。
- `python3 -m pytest tests/unit/test_charon.py tests/unit/test_kia_module_registry.py -q`：`18 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 236/310`。
- `python3 -m pytest tests/unit/test_aion.py tests/unit/test_mnemos_cli.py::TestCmdCapsule tests/unit/test_kia_module_registry.py -q`：`16 passed`。
- `python3 -m pytest tests/unit/test_mnemos_cli.py -q -k 'capsule_dismiss'`：`1 passed, 142 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 235/310`。
- `python3 -m pytest tests/unit/test_teiresias.py tests/unit/test_teiresias_embedding.py tests/unit/test_kia_module_registry.py -q`：`14 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 232/310`。
- `python3 -m pytest tests/unit/test_knowledge_graph.py tests/unit/test_kia_module_registry.py -q`：`48 passed`。
- `python3 -m pytest tests/unit/test_mnemos_cli.py -q -k 'kg_export_dataview or TestCmdKg'`：`2 passed, 143 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 231/310`。
- `python3 -m pytest tests/unit/test_genos.py tests/unit/test_kia_module_registry.py -q`：`9 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 230/310`。
- `python3 -m pytest tests/unit/test_ariadne.py tests/unit/test_kia_module_registry.py -q`：`8 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 228/310`。
- `python3 -m pytest tests/unit/kia/test_policy.py tests/unit/test_mnemos_cli.py -q -k 'force_commit or force_rollback or policy_commit or TestCmdPolicy'`：`6 passed, 149 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 226/310`。
- `python3 -m pytest tests/unit/test_metis.py tests/unit/test_chronos.py tests/unit/test_kia_module_registry.py -q -k 'metis or knowledge_profile or module_registry'`：`10 passed, 29 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 225/310`。
- `python3 -m pytest tests/unit/test_hygieia.py tests/unit/test_mnemos_cli.py tests/unit/test_kia_module_registry.py -q -k 'hygieia or immune or module_registry'`：`14 passed, 149 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 224/310`。
- `python3 -m pytest tests/unit/cognitive/test_observation_store.py tests/unit/test_cli_commands_p3.py tests/unit/test_observation_application.py -q -k 'ObservationStore or CmdObserve or observation_search'`：`15 passed, 13 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 223/310`。
- `python3 -m pytest tests/unit/test_dialog_reminder.py tests/unit/test_cli_reminder.py tests/unit/test_mnemos_cli.py tests/unit/test_kia_module_registry.py -q -k 'DialogReminderQueue or reminder or module_registry'`：`40 passed, 151 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 222/310`。
- `python3 -m pytest tests/unit/test_psyche.py tests/unit/test_cli_persona.py tests/unit/test_mnemos_cli.py tests/unit/test_pythia.py -q -k 'daily_summary or persona_daily_summary or SignalStore or Pythia'`：`135 passed, 173 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 221/310`。
- `python3 -m pytest tests/unit/test_dialog_reminder.py tests/unit/kia/test_adaptive_config_effective.py tests/unit/test_kia_module_registry.py -q -k 'DialogReminderQueue or AdaptiveConfigEffective or module_registry'`：`29 passed, 13 deselected`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 220/310`。
- `python3 -m pytest tests/unit/evidence/test_evidence_graph.py tests/unit/reflection/test_reflection_capability.py -q`：`22 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 219/310`。
- `python3 -m pytest tests/unit/test_dike.py tests/unit/test_kia_module_registry.py -q`：`12 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 218/310`。
- `python3 -m pytest tests/unit/reflection/test_feedback_collector.py tests/unit/reflection/test_reflection_engine.py tests/unit/reflection/test_reflection_engine_extra.py -q`：`40 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 217/310`。
- `python3 -m pytest tests/unit/test_preflight_builder.py tests/unit/test_oracle.py tests/unit/test_olympus.py -q`：`93 passed`。
- `python3 scripts/run_vulture_check.py --budget-only`：`Vulture whitelist entries: 216/310`。
- `python3 scripts/check_zombie_code_policy.py`：`OK: 92 zombie-code candidate(s) documented.`。
- `python3 scripts/ci_ratchet.py --check`：通过，无新增架构债。
- `uv run --no-project --with "vulture>=2.11,<3" vulture . vulture_whitelist.py --min-confidence 80`：通过，无输出。
- `python3 scripts/run_local_gates.py` 全部通过。
- `.venv/bin/python scripts/run_tests.py quick`：`4488 passed, 15 subtests passed in 354.59s`。
- `.venv/bin/python scripts/run_tests.py integration`：`119 passed in 21.74s`。
- `.venv/bin/python scripts/run_tests.py heavy`：`14 passed in 11.37s`。
- `.venv/bin/python -m pytest -q`：`4621 passed, 15 subtests passed in 437.03s`。

---

## Crush Agent 主动 MCP 接入（2026-06-28）

> 在被动数据源接入的基础上，为 Charm Crush 添加 MCP 主动工具配置与使用策略。

### Added
- `integrations/crush_adapter.py`：新增 Crush Agent 适配器。
  - 注册到 `AgentRegistry`，优先级 7；
  - 通过 `~/.crush/crush.db` 或 `~/.config/crush/crush.json` 检测 Crush；
  - 安装 MCP server 到 `~/.config/crush/crush.json`（Crush 的 `mcp` 顶级键，type=stdio）；
  - 安装主动策略到 `~/.config/crush/CRUSH.md`；
  - 提供 `collect_signals()` 从 Crush 数据库采集最近会话信号。
- `integrations/active.py`：
  - 新增 `crush_mcp_server_spec()` / `crush_config_path()` / `upsert_crush_mcp_server()` / `crush_mcp_configured()`；
  - `_agent_policy_path()` 支持 `crush` → `~/.config/crush/CRUSH.md`。
- `integrations/olympus.py`：`_ensure_adapters_loaded()` 加载 `integrations.crush_adapter`。
- `tests/unit/test_active_integration.py`：新增 Crush MCP 配置与策略安装测试。
- `tests/unit/test_olympus.py`：更新 `_ensure_adapters_loaded` 期望导入列表，包含 `integrations.crush_adapter`。

### Fixed
- `tests/e2e/test_golden_path.py`：使用 `monkeypatch` 隔离 `_patch_config`，避免 `DemoConfig` 泄漏到后续测试导致大量 `AttributeError`。
- `integrations/apollon.py`：`_run_skill_wiki_flywheel` 捕获 `RuntimeError`，确保画像加载失败时仍能以无画像模式继续。
- `docs/core-integrations-dependencies.md`：重新生成依赖图，纳入 `integrations.crush_adapter`。

### Tests
- 新增 Crush 主动接入测试：`4 passed`。
- 全量测试：`4448 passed, 15 subtests passed`。
- 本地门禁（flake8 / mypy / compileall / vulture / bandit / CI ratchet）全部通过。

---

## L3 Observation 事件驱动 L4 Reflection（2026-06-19）

> 修复审计第四项高风险：Observation → Reflection 不是事件驱动的。`ObservationEngine` 发布 `observation.updated` 事件后，唯一消费者是 `CognitiveGraphUpdater`，没有任何处理器把 L3 观察结果路由到 L4 Reflection。

### Added
- `mnemos_daemon.py`：新增 `_on_observation_updated(event)` 事件处理器
  - 订阅 `observation.updated`，读取 `observation_ids`
  - 通过 `ObservationStore.get_by_id()` 查询观察详情
  - 筛选高置信度观察（`confidence >= reflection.observation_trigger_confidence`，默认 0.7）或类型为 `deviation`/`trend`/`contrast` 的突变观察
  - 将显著观察构建为查询摘要，调用 `_get_reflection_engine().reflect_on_user_input()`
  - 自动触发器未命中时 fallback 到 `reflect_manually(trigger=ReflectionTrigger.OBSERVATION_UPDATED)`
  - 成功后发布 `reflection.completed` 事件
- `core/cognitive/observation_store.py`：新增 `ObservationStore.get_by_id(obs_id)`，支持按 ID 查询单个观察
- `core/reflection/models.py`：`ReflectionTrigger` 枚举新增 `OBSERVATION_UPDATED`
- `core/config.py`：`reflection` 块新增配置项：
  - `reflection.observation_trigger_enabled`（默认 `True`）
  - `reflection.observation_trigger_confidence`（默认 `0.7`）
- `core/mnemos_bus.py`：将 `observation.updated` 加入 `_PERSISTENT_EVENT_TYPES`，确保事件在 daemon 启动时可被重放消费

### Changed
- `mnemos_daemon.py`：daemon 启动 EventBus 初始化时注册 `observation.updated → _on_observation_updated`，补齐 L3 → L4 事件驱动链路

### Tests
- 新增 `tests/unit/test_observation_reflection_bridge.py`：
  - `test_observation_store_get_by_id_round_trip`
  - `test_on_observation_updated_triggers_reflection`
  - `test_on_observation_updated_skips_low_confidence`
  - `test_on_observation_updated_disabled_by_config`
  - `test_on_observation_updated_fallback_to_manual`
- 全量测试：`3710 passed, 15 subtests passed`
- daemon 重启：PID 66797 → 72994

---

## Crush Agent 数据源接入（2026-06-28）

> 让 mnemos 支持新的本地 Agent：Crush。

### Added
- `integrations/sources/crush_source.py`：新增 Crush 被动数据源插件。
  - 只读连接 `~/.crush/crush.db`；
  - 解析 `sessions` / `messages` / `read_files` 表；
  - 支持 text、tool_call、tool_result 片段；
  - 自动识别秒/毫秒时间戳；
  - 提供 hybrid 触发策略（watchdog + 30s polling）。
- `core/sync_framework/registry.py`：注册 `CrushSource`，并加入 `PathDiscover.AGENT_CONFIG`（标准路径 `~/.crush`，环境变量 `CRUSH_HOME`）。
- `tests/unit/test_crush_source.py`：覆盖会话发现、Turn 解析、tool_call/tool_result、read_files、session state。

### Tests
- 新增 Crush 单元测试：`6 passed`。
- 全量测试与本地门禁通过。

---

## 查漏补缺与命名/契约一致性修复（2026-06-25）

> 深度检查最近 5 个 commit 的修复遗漏，并全量扫描名字/契约与行为不符的问题。

### Added
- `core/config.py`：EventBus 默认链深守卫为 `10`；`embedding.enabled` 默认开启。
- `mnemos_daemon.py`：接入 `raw_vault_watch` / `agent_path_watch` 轮询服务；检测到变更后真正摄入文件或标记 trigger dirty。
- `core/mnemos_bus.py`：事件链深度守护，防止 handler 级联发布导致无限循环。
- `core/kia/stress_test.py`：重入锁，防止 `knowledge_needs_reinforcement` 事件自环。

### Changed
- `mnemos_daemon.py`：
  - `service_eventbus()` 改名为 `service_eventbus_health()`，保留 `service_eventbus` 别名兼容；
  - `_run_persona_extensions()` 改名为 `_run_persona_blindspot()`，保留 `_run_persona_extensions` 别名兼容；
  - `service_persona_analyzer` / `service_signal_collector` / `_run_persona_blindspot` 增加 `persona.enabled` 开关检查；
  - `daemon.services.link_probe` 默认改为 `False`，与 `features.enable_link_probe = False` 一致，避免空转。
- `core/cli/commands/cognitive_graph.py`：`ingest` 子命令改为通过 `EventBus.publish` 发布事件，并同步派发一次保证 CLI 即时效果。
- `predictive_push.py`（根目录）：改为直接调用 `IntelligenceApplicationService.predictive_push()`，不再错误转发到对话提醒队列。
- 更新 `config/config.example.{json,yaml}` / `.env.example`、README/CLAUDE.md/KIA 文档中关于 embedding、PredictivePush 的说明。

### Fixed
- `watchers.*.enabled` 配置开关未生效 → 改为在 service 函数内读取。
- `service_raw_vault_watch()` 只发事件不摄入文件 → 改为调用 `FileIngestor.ingest_file()`。
- `core/telemetry/heartbeat.py` 孤儿模块清理，相关测试与 checklist 同步更新。
- `redact_secrets` / `_deep_merge` 增加循环引用检测；文件名碰撞循环增加上限。
- 修复 `psutil.NoSuchProcess` / `ZombieProcess` 导致的 flaky 测试。

### Tests
- 新增/更新 `tests/unit/test_daemon_watchers.py`、`tests/unit/test_compat_wrappers.py`、`tests/unit/test_cli_commands_p3.py`。
- 全量测试：`4243 passed`。
- 本地门禁（flake8 / mypy / compileall / vulture / bandit / CI ratchet 等）全部通过。

---

## L5 经验回流 preflight/guard + 配置键补全（2026-06-19）

> 修复审计第三项高风险：Layer 5 反射经验写入 `reflections.db` 后，preflight/guard 未消费，形成外循环断裂。
> 同次审计进一步发现：多个已在代码中消费的配置键未在 `DEFAULT_CONFIG` 中声明，本次一并补齐。

### Added
- `core/reflection/reflection_store.py`：新增 `get_experiences(type, dimension, limit)`，按时间倒序读取 `layer5_experiences`
- `core/kia/prophasis.py`：
  - `_load_layer5_experiences()`：从 `database_dir/reflections.db` 读取 Layer 5 经验，映射为 `ChecklistItem`
  - `_format_layer5_experience_item()` / `_format_layer5_experience_detail()`：把认知变迁/洞察/无关注入等经验格式化为 checklist 文本与详情
  - `_merge_layer5_experiences()`：去重合并到 preflight checklist
  - `_load_full()` 在有专用复盘文件和无专用复盘文件的 fallback 路径均回流 Layer 5 经验
- `core/kia/aegis.py`：`InProcessGuard.from_task_type()` 通过 `PreFlightInjector` 自动获得 Layer 5 经验，守护检查可命中

### Fixed
- `core/config.py` 补齐代码中已消费但 `DEFAULT_CONFIG` 缺失的配置键：
  - `distill.fragment_merge_threshold`、`distill.enable_llm_fragment_merge`、`distill.extract_correction_retries`、`distill.min_session_fragment_pass_ratio`
  - EventBus dead-letter 上限
  - `llm.rate_limits`
  - `integrations.openclaw.state_dir`
  - `document_process.max_file_size_mb`
  - `preflight.mode`
  - `intent_router.llm_fallback_enabled`、`intent_router.llm_fallback_threshold`、`intent_router.llm_fallback_timeout_seconds`
  - `search.weights`
  - `charon.tech_keywords`、`charon.concept_keywords`（`None` 表示使用模块内置默认词典）
- `core/kia/charon.py`：当配置关键词为 `None` 时使用内置默认词典，避免默认空列表意外清空词典

### Tests
- `tests/unit/reflection/test_reflection_store.py`：新增 `test_get_experiences_round_trip`
- `tests/unit/test_prophasis.py`：新增 `test_inject_loads_layer5_experiences`，验证 reflection DB 经验回流到 checklist
- 全量测试：`3705 passed, 15 subtests passed`
- daemon 重启：PID 57809 → 66797

---

## 蒸馏层配置键真正接入代码（2026-06-21）

> 修复“装饰性”配置键：删除 `core/config.py` 中重复的 `distill` 块，把 token budget、分块阈值、片段质量、成本预算、冷归档等常量改为配置驱动。

### Added
- **蒸馏配置体系化**
  - `core/config.py`：`distill` 块合并为一个完整块，新增 `response_tokens`、`effective_max_tokens`、`per_message_token_limit`、`chunk_std_factor`、`chunk_total_factor`、`chunk_size_factor`、`fragment_boundary_chars`、`min_value_context_chars`、`skill_suggestion_max_chars`、`value_prejudgment_rule_assessment_length`、`content_formatter_max_tokens`
  - `core/config.py`：`llm.provider_prices` 默认配置项，用于覆盖模型单价
  - `core/llm_config.py`：新增 `DEFAULT_PROVIDER_PRICES`、`get_provider_price()`、`estimate_cost()`
- **会话级 LLM 成本预算**
  - `core/hephaestus/distillation_engine.py`：`HttpApiHostAgentCaller` 捕获 `response.usage`，累加 `_session_cost_acc`，预算耗尽后拒绝新调用
  - `DistillationEngine.process()` 在 L3 判断、L4 提取、skill 建议前检查 `budget_exceeded`，超支时返回 `judgment="budget_exceeded"`
  - `prompt_call_log` 表新增 `prompt_tokens`、`completion_tokens`、`cost` 列
- **incremental_batch_turns**
  - `DistillationEngine._chunk_messages()` 新增 `max_turns_per_chunk` 参数
  - 分块蒸馏路径传入 `distill.incremental_batch_turns`，限制每 chunk 的原始 turn 数
- **cold_knowledge_archive_days**
  - `core/app/freshness_refresh_worker.py`：新增 `archive_cold_pages()`，把超期页面移动到 `99-Archive/Cold/`，更新 `status: archived` 与 `archived_at`
  - `mnemos_daemon.py::service_freshness_refresh()` 在每日刷新后执行冷归档
  - `core/config.py`：`freshness_refresh.archive_limit` 默认 10

### Changed
- `core/hephaestus/distillation_engine.py`：
  - `_distill_prompt_budget()` 基于 `token_budget_total`、`chunk_std_factor`、`token_budget_output_reserve` 计算
  - `build_session_text()`、`HttpApiHostAgentCaller._try_api_config()`、`_chunk_messages()`、`process()` 阈值均改为配置读取
  - 片段边界/价值上下文常量统一读取 `distill.fragment_boundary_chars`、`distill.min_value_context_chars`
- `core/hephaestus/prompt_builder.py`：`TokenBudget.from_config()` 与 `ContentFormatter.format_session()` 使用配置
- `core/hephaestus/fragment_merger.py`：`max_tokens` 读取 `distill.response_tokens`
- `core/hephaestus_worker.py`：`watch_queue()` 默认轮询间隔读取 `distill.poll_interval_seconds`

### Fixed
- 删除 `core/config.py` 中重复的 `"distill"` 块，避免 `incremental_batch_turns` / `llm_cost_budget_per_session` / `cold_knowledge_archive_days` / `min_task_interval_seconds` / `poll_interval_seconds` 被覆盖丢失
- `build_session_text()` 不再默认应用单条消息 6000 token 截断，修复 `max_chars` 大但内容被截断的回归
- `HephaestusWorker.process_all()` 真正应用 `distill.min_task_interval_seconds`

### Removed
- 从 `distill` 配置块移除从未被消费的 `provider`、`allow_host_agent_delegate`、`tick_interval_seconds`
- 同步更新 `core/cli/commands/init.py`、`scripts/auto_setup.py`、README、config example、相关测试

### Fixed
- **MCP 反射 L5 消费者缺失**
  - `integrations/agora.py`：新增 `_get_reflection_engine()`，统一以 `register_default_consumers=True` 构造 `ReflectionEngine`，并传入 `persona_store` 与 `kia_store`
  - `_tool_reflect_on_input`、`_tool_reflect_manually`、`_tool_reflection_feedback` 均改为使用带消费者引擎
  - 修复 MCP 反射入口 insight/feedback 无法反哺画像、KIA、评分器的外循环断裂问题

### Tests
- 新增 `tests/unit/test_distillation_engine.py`：`_chunk_messages` turn 限制、成本预算耗尽、`HttpApiHostAgentCaller` 成本累加
- 新增 `tests/unit/test_freshness_refresh_worker.py`：冷知识归档、跳过新鲜页面、幂等性
- 全量测试：`3703 passed, 15 subtests passed`（修复 usage 对象 pydantic/dict 兼容后重跑）
- daemon 重启：PID 87932 → 22893

---

## 意图路由 LLM 回退 + 新鲜度自动刷新 + entropy/reminders 产出（2026-06-19）

> 修复 P2-2/3/4：意图路由低置信时 LLM 兜底；知识新鲜度支持手动/自动刷新；熵减与提醒队列产生可见产出。

### Added
- **P2-2 意图路由 LLM 回退**
  - `core/app/intent_router.py`：新增 `_llm_classify()`，使用主备 LLM API 对歧义/无规则输入兜底分类
  - `RoutingDecision` 新增 `llm_fallback` 标记
  - 触发条件：`rule_matches` 为空，或 `needs_correction=True` 且 `confidence <= threshold`
  - 配置开关：`intent_router.llm_fallback_enabled`（v2.0.0 当前默认 false）、`intent_router.llm_fallback_threshold`（默认 0.65）、`intent_router.llm_fallback_timeout_seconds`（默认 2.0）
  - MCP `intent_route` 返回增加 `llm_fallback` 与 `suggested_action`
- **P2-3 新鲜度自动刷新**
  - 新增 `core/app/freshness_refresh_worker.py`：`FreshnessRefreshWorker`
    - `refresh_page()`：备份到 `07-Shadow/08-Refresh/`，可选 LLM 重新蒸馏，更新 `updated_at` / `修改日期`
    - `refresh_all_stale()`：批量扫描过期页面
    - `list_pages()`：列出 stale/fresh 页面
  - 新增 CLI：`mnemos freshness list|refresh|refresh-all`
  - 修复 `core/kia/chronos.py::_trigger_page_modified()`：正确调用 `KnowledgeFreshnessChecker.check()`，并在 stale 时自动刷新
  - 新增 daemon 服务 `freshness_refresh`：每日自动刷新高优先级过期页面
  - MCP `freshness_check` 新增 `auto_refresh=False` 参数，stale 时可直接刷新
- **P2-4 entropy/reminders 产出**
  - 新增 CLI：`mnemos entropy scan|auto-fix`
  - 新增 CLI：`mnemos reminder list|push|resolve`
  - `core/kia/dialog_reminder.py`：新增 `list_reminders()` 方法
  - `mnemos_daemon.py`：`service_reminder_scan()` 对高优先级过期页面调用 `DialogReminderQueue.enqueue()`
  - 新增 daemon 服务 `entropy_scan`：每日运行熵减扫描并将候选入队提醒
  - MCP `predictive_push` 默认写入 `push_history`，新增 `no_record=False` 开关

### Changed
- `core/config.py`：新增 `freshness_refresh`、`entropy` 配置段；`daemon.services` 增加 `freshness_refresh` / `entropy_scan`
- `mnemos_daemon.py`：`INTERVALS` 与 `_resolve_service_call` 注册 `freshness_refresh`、`entropy_scan`
- `integrations/agora.py`：更新 `_tool_intent_route`、`_tool_freshness_check`、`_tool_predictive_push` 工具说明与返回字段
- `docs/AGENT_GUIDE.md`：新增 freshnes/entropy/reminder CLI 章节与工具说明

### Tests
- 新增 `tests/unit/test_freshness_refresh_worker.py`：timeless 跳过、stale 刷新更新日期、批量扫描
- 新增 `tests/unit/test_cli_freshness.py`、`test_cli_entropy.py`、`test_cli_reminder.py`
- 更新 `tests/unit/test_chronos.py`：`_trigger_page_modified` 自动刷新断言
- 更新 `tests/unit/test_mnemos_daemon.py`：`reminder_scan` 入队、`freshness_refresh`、`entropy_scan` 断言
- 全量测试：`3695 passed, 15 subtests passed`

---

## 争议仲裁可视化与可调权重（2026-06-19）

> 修复 P2-1：争议页面增加评分明细，权重支持 state 文件覆盖，CLI 新增 `dispute weights` / `dispute show`。

### Added
- **争议页面评分明细**
  - `core/app/dispute_resolver.py::_render_score_breakdown()` 新增评分明细表格
  - 维度/权重/新断言得分/现有断言得分/加权差/综合分/建议动作一目了然
  - 评分结果写入争议页 frontmatter 的 `features_a` / `features_b`
- **可调权重与 state 文件覆盖**
  - `core/app/dispute_scorer.py` 支持权重加载优先级：`learner > state 文件 > config > 默认值`
  - state 文件路径：`~/.mnemos/state/dispute_weights.json`
  - 新增 `DisputeScorer.save_weights()` / `reset_weights()` / `load_weights_from_state()`
  - `DisputeScorer.__init__` 新增 `state_dir` 参数，便于测试与自定义路径
- **CLI 增强**
  - `mnemos dispute weights`：查看当前权重、来源与阈值
  - `mnemos dispute weights --set dim=value`：写入 state 覆盖权重
  - `mnemos dispute weights --reset`：清除 state 回退到 config/默认值
  - `mnemos dispute weights --learn`：手动触发自适应权重学习
  - `mnemos dispute show <page_path>`：解析争议页 frontmatter 输出评分详情

### Changed
- `core/cli/commands/dispute.py`：新增 `weights` / `show` 子命令处理函数
- `mnemos_cli.py`：注册 `dispute weights` / `dispute show` 参数解析
- `docs/AGENT_GUIDE.md`：新增争议仲裁 CLI 章节与快速参考

### Fixed
- **噪声内容误判为高价值**
  - `core/kia/ingest_helpers.py::is_noise_message()` 新增短语占比检测：去除标点后噪声短语覆盖 ≥80% 且总长度 ≤40 时判为噪声
  - 修复 `_NOISE_PHRASES` 中 `' Roger '` 带空格的问题，统一为 `'roger'`
  - `core/scoring/scorers/distill_scorer_v2.py::should_distill()` 先判噪声，低价值内容不再触发蒸馏
  - `core/hephaestus/distillation_engine.py::ValuePrejudgment.judge()` 增加快速通道：所有非空消息均为噪声时直接返回 `CERTAINLY_NO`
- **E2E 反射 CLI mock 缺字段**
  - `tests/e2e/test_cli_smoke.py::FakeInsight` 补齐 `llm_called` / `summary` / `key_points` / `llm_error`

### Tests
- 新增 `tests/unit/test_dispute_resolver.py::test_create_dispute_page_includes_score_breakdown`
- 新增 `tests/unit/test_dispute_scorer.py::TestStateWeights`：state 覆盖、部分合并、保存、重置
- 新增 `tests/unit/test_cli_dispute.py`：`weights` 查看/设置/重置、`show` 评分详情
- 全量测试：`**3673 passed**, 15 subtests passed`

---

## 画像 → 行为闭环验证（2026-06-19）

> 修复 P1-3：为画像行为提示建立使用追踪与效果指标，完成"画像 → 行为 → 反馈"闭环验证。

### Added
- **画像行为提示使用追踪**
  - 新增 `core/persona/behavior_tracker.py`：
    - `BehaviorPromptTracker.track()` 记录每次 `get_behavior_prompt` 调用
    - 自动从 prompt 文本中解析命中的策略标签（如 `focus_depth_high`、`abstraction_high`）
    - 记录 Agent、来源（preflight / mcp_persona_behavior_prompt / cli）、A/B 分组、prompt 长度
  - `core/persona/psyche.py` 新增 `behavior_prompt_signals` 表
- **画像行为提示指标**
  - 新增 MCP 工具 `persona_behavior_metrics(days=30)`：返回总调用次数、Agent/来源/策略分布、A/B 分组、每日趋势
  - 新增 CLI：`mnemos persona behavior-metrics [--days N]`

### Changed
- **`core/persona/delphi.py`**：
  - `get_behavior_prompt()` 返回前调用 `BehaviorPromptTracker.track(source="preflight", ...)`
  - 新增 `_get_ab_test_group_label()`，将 A/B 分组写入追踪记录
- **`integrations/agora.py`**：
  - `_tool_persona_behavior_prompt()` 返回前调用 tracker（source="mcp_persona_behavior_prompt"）
  - 注册新 MCP 工具 `persona_behavior_metrics` 并更新 schema
- **`mnemos_cli.py` + `core/cli/commands/persona.py`**：新增 `persona behavior-metrics` 子命令

### Tests
- 新增 `tests/unit/persona/test_behavior_tracker.py`：策略解析、track 写入、metrics 聚合、失败隔离
- 更新 `tests/unit/test_delphi.py`：`get_behavior_prompt` 埋点测试、A/B 分组标签测试
- 更新 `tests/unit/test_agora.py`：`persona_behavior_metrics` MCP 工具测试
- 相关模块合并跑：`tests/unit/persona` + `test_delphi.py` + `test_agora.py` + `test_preflight_builder.py` = **123 passed**

---

## Reflection 内置 LLM 洞察（2026-06-19）

> 修复 P1-2：`reflect_on_input` / `reflect_manually` 默认自动调用 LLM 生成洞察，并暴露 `auto_llm` 开关与 LLM 调用状态。

### Changed
- **Reflection LLM 洞察内置化**
  - MCP 工具 `reflect_on_input` / `reflect_manually` 新增 `auto_llm` 参数（默认 `true`）
  - 默认由 Mnemos 自动调用 LLM，返回 `insight_summary` / `key_points` / `confidence` / `llm_called` / `llm_error`
  - `auto_llm=false` 时仅返回 `prompt_used`，由宿主 Agent 自行调用 LLM
- **LLM 控制链路打通**
  - `ReflectionEngine` 保存 `use_llm` 并传给 `ReflectionCapability`
  - `ReflectionCapability` 新增 `use_llm` 参数，控制内部 `InsightGenerator` 是否调用 LLM
- **CLI 同步升级**
  - `mnemos reflect on <text> [--auto-llm|--no-auto-llm]`
  - `mnemos reflect manual [query] [--auto-llm|--no-auto-llm]`
  - 输出增加 LLM 调用状态、洞察摘要、关键发现、失败原因

### Fixed
- `InsightGenerator._build_prompt` 中 `temporal.humanize_duration` 调用错误（`TemporalContext` 无此方法），改为通过 `TimeAwareness().humanize_duration()` 调用

### Tests
- `tests/unit/reflection/test_insight_generator.py`：新增 `llm_called` / `llm_error` 断言与 `use_llm=False` 不调用 LLM 测试
- `tests/unit/reflection/test_reflection_capability.py`：新增 `use_llm=False` 测试
- `tests/unit/reflection/test_reflection_engine.py`：新增 `reflect_manually` 传递 `use_llm` 测试
- `tests/unit/test_agora.py`：新增 4 个 reflect MCP 工具测试
- 相关模块合并跑：`tests/unit/reflection` + `tests/unit/test_agora.py` + `tests/unit/test_cli_reflect_command.py` = **218 passed**

---

## Blindspot 实时化与闭环修复（2026-06-19）

> 修复 P1-1：盲点检测从"24h 冷却 + 每日 1 条"改为"会话级去重 + 用户确认后搜索 → 蒸馏入 Wiki → 自动销警"的完整闭环。

### Changed
- **Blindspot 触发策略实时化**
  - `BlindspotDiscovery.check_blind_spot()` 改为会话级去重：同一 topic 在同一 session 内只提醒一次
  - 无 `session_id` 时使用 5 分钟兜底冷却，避免高频重复提醒
  - 保留"已忽略 7 天冷却"与"已解决/已缓解不再提醒"
  - 单 session 最多提醒 3 个不同 topic
- **Blindspot 闭环修复**
  - 检测到盲区后返回 `suggested_query` 与自然语言 `prompt_for_user`，宿主 Agent 在当前对话内确认
  - 用户确认"记录/查一下"后，AI 搜索资料并继续对话，知识随对话蒸馏进入 Wiki
  - `KGEventHandler.on_distilled()` 在 wiki 页面生成后自动调用 `BlindspotDiscovery.resolve_by_wiki_page()` 销警
- **MCP 工具输出升级**
  - `integrations/agora.py::_tool_blindspot_check()` 返回结构化结果，新增 `prompt_for_user` / `expected_user_actions` / `suggested_query`
- **应用层调度解耦**
  - `core/app/application_hub.py` 中 `blind_spot` 的硬限制移除，完全交给 `BlindspotDiscovery` 做策略判断
- **新增 CLI**
  - `mnemos blindspot list [--status <status>]`：列出所有盲区
  - `mnemos blindspot status`：查看盲区统计
  - `mnemos blindspot ignore <topic>`：手动忽略盲区
  - `mnemos blindspot resolve <topic> [--page <page_path>]`：手动关闭盲区
  - `mnemos blindspot cleanup [--days N]`：清理 N 天前已解决的盲区记录

### Added
- 新增 `core/app/blindspot_response_builder.py`：统一构建盲区自然语言提示、结构化工具结果、用户意图识别
- 新增 `core/cli/commands/blindspot.py`：盲区管理 CLI

### Tests
- 新增/更新 `tests/unit/test_blindspot_discovery.py`：14 passed
- 新增 `tests/unit/test_kg_event_handler.py::test_on_distilled_resolves_blindspots_by_wiki_page`
- 相关模块合并跑：`test_blindspot_discovery.py` + `test_application_hub.py` + `test_mnemos_bus.py` + `test_kg_event_handler.py` = **99 passed**

---

## 统一 BayesianScorer，替换 BetaBayesianFusion（2026-06-13）

> 修复 P2-#23：消除 `BayesianScorer` 与 `BetaBayesianFusion` 的重复实现，让增强后的 `BayesianScorer` 成为 V2 唯一贝叶斯引擎。

### Changed
- **统一贝叶斯评分器**
  - 新增 `core/scoring/bayesian_scorer.py::BayesianScorer`，合并：
    - `core/scoring/beta_bayesian.py::BetaBayesianFusion` 的 stateless fuse、显式 `P(E|¬H)`、`ml_confidence`、自适应规则权重、非对称 tail down-weight
    - `core/kia/bayesian_scorer.py::BayesianScorer` 的 SQLite 持久化、反馈日志、`DimensionScore` 高层 API
  - `AdaptiveScorerV2` 改用 `BayesianScorer` 作为 `_bayesian` 实例，并传入 `db_path=self.db_path` 使贝叶斯状态写入同一个 `mnemos.db`
  - 废弃 V1 `AdaptiveScorer` 的 `BetaBayesianFusion` 导入改为 `core.scoring.bayesian_scorer.BayesianScorer`
- **持久化策略**
  - `BayesianScorer` 的 `bayesian_scorer_state` / `bayesian_feedback` 表成为先验状态真相源
  - `AdaptiveScorerV2.save_model` 仍保留 `bayesian_priors` 快照用于便携/迁移，但正常加载不再用 `meta_json` 覆盖 DB 状态
  - 自动迁移旧表结构（补齐 `total_samples`、`neg_likelihood`、`last_updated` 列）

### Removed
- 删除 `core/scoring/beta_bayesian.py`
- 删除 `core/kia/bayesian_scorer.py`
- 删除 `tests/unit/scoring/test_beta_bayesian.py`，测试合并到 `tests/unit/test_bayesian_scorer.py`

### Tests
- `python3 -m pytest tests/unit tests/integration -q`：**3466 passed, 2 skipped**

---

## StorageBackend 工厂统一（2026-06-13）

> 修复 P1-#22：统一 StorageBackend 创建入口，消除散落工厂与损坏脚本。

### Changed
- **StorageBackend 工厂化**
  - 新增 `core/sync_framework/storage_backend.py::create_storage_backend()` 作为全库唯一工厂入口
  - 统一读取 `config.storage_backend`，当前仅支持 `obsidian`，未知后端抛出清晰 `ValueError`
  - 所有直接 `ObsidianBackend()` 调用收敛到工厂：
    `SyncEngine`、`FileIngestor`、`DocumentProcessor`、`KnowledgeInboxProcessor`、
    `wiki_builder`、`wiki_rebuild`、`agora`、`apollon`、`preflight_builder`、
    `scripts/distill_all.py`、`scripts/batch_distill.py`、`scripts/e2e_probe.py`
- **StorageBackend 接口收紧**
  - 将 `update_tags()` 提升为 `@abstractmethod`，强制任何新 backend 必须实现，避免蒸馏后重复处理
- **代码清理**
  - 移除 `FileIngestor.__init__` 中无用的 `self.config = get_config()`
  - 清理 `integrations/agora.py` 中 `_get_storage_backend()` 的废弃 `config` 参数

### Fixed
- `scripts/distill_all.py` 与 `scripts/batch_distill.py` 调用不存在的 `StorageBackend.create()` 导致 `AttributeError`

### Tests
- `python3 -m pytest tests/unit tests/integration -q`：**3454 passed, 2 skipped**

---

## Preflight 统一与 KIA 闭环补全（2026-06-13）

> 统一 Claude / 通用 Agent 的 preflight 路径，补全 KIA checklist 命中反馈边，清理 prophasis 死代码。

### Changed
- **Preflight 路径统一**
  - 新增 `integrations/preflight_builder.py`，提供 agent-agnostic 的 KIA / Wiki / L1 / PredictivePush / Observation / Persona 段落构建器
  - `integrations/apollon.py` 新增 `get_context_for_agent(agent, ...)`，保留 `get_context_for_claude()` 兼容入口
  - `integrations/active.py` 的 `_run_preflight_with_timeout()` 优先调用完整路径，5s 超时后自动回落轻量路径，并增加非主线程保护
  - 新增配置项 `preflight.mode=light|full`
  - 为 `ObsidianBackend.list_by_tags/search`、`WikiReader._build_index`、`PredictivePushEngine._get_page_index` 增加 60s TTL 类级缓存
- **KIA 闭环补全**
  - `PreFlightInjector.mark_checklist_used()` 接入生产路径：Guard 触发/静默记录后回写源 checklist 的 `hit_count`/`last_hit`
  - 新增 `tests/unit/test_preflight_builder.py` 覆盖命中回写路径
- **MCP 工具增强**
  - `retrospective_list` 返回结果新增 `task_type`、`subtype`、`version` 结构化字段（原 `path`/`title` 保留）
- **代码清理**
  - 删除 `core/kia/prophasis.py` 中的死代码 `load_knowledge()` 和未使用的 `list_available_types()`
  - 清理 `integrations/apollon.py` 中已失效的 A/B 画像统计 `get_ab_test_stats()`

### Fixed
- `build_wiki_section` deep 模式使用错误热力分组 key（已改为 `hot/warm/cold/unknown`）
- `build_wiki_section` light 模式 snippet 为空
- `build_kia_section` 中 f-string 含反斜杠导致 Python 3.10/3.11 SyntaxError
- `_mark_guard_checklist_usage` 使用 session checklist 索引而非源文件索引导致的错标

### Tests
- `python3 -m pytest tests/unit tests/integration -q`：**3451 passed, 2 skipped**

---

## 蒸馏 Prompt 体系统一（2026-06-14）

> 将两套 prompt 体系合并为 `PromptBuilder` 单一入口。

### Changed
- **统一蒸馏 Prompt 体系**
  - `DistillationEngine` 主链路（`LLMValueJudge`、`KnowledgeExtractor`、skill suggestion）全部通过 `core/hephaestus/prompt_builder.py` 渲染
  - `core/hephaestus/distillation_prompts.py` 仅保留 `PROMPT_VERSION`
  - 模板目录统一为 `prompts/distill/{task_type}/{session_type}.md`
  - 原 `unified.md` / `filter.md` 内容分别迁入 `extract/base.md` / `value_judge/base.md`
  - 类型专用 prompt 从 `prompts/distill/type/*.md` 迁至 `prompts/distill/extract/{coding,analysis,marketing,strategy,writing,review}.md`
  - 新增 `skill_suggestion` 任务类型与模板
  - 文档 prompt 通过同一 `TemplateRegistry` 加载
- **PromptBuilder 增强**
  - 上下文自动注入 `prompt_version`
  - 支持 `preformatted` 透传已格式化会话文本
  - 模板选择增加 `{task_type}` 顶层文件回退

---

## Agent 接入架构重构（2026-06-13）

> 统一 MCP preflight + Source 模块被动捕获，清理无实际作用的 wrapper。

### Changed
- **恢复 Source 模块**
  - 从 git 历史恢复 `integrations/sources/codex_source.py`、`hermes_source.py`、`openclaw_source.py`
  - 在 `core/sync_framework/registry.py` 中重新注册 codex / hermes / openclaw
  - 代码风格同步到当前规范：顶层 import、`%` 风格日志、移除内联 import
- **下线 wrapper 脚本**
  - 删除 `~/.codex/mnemos_wrapper.py`、`mnemos-codex`、`mnemos-codex.bat`
  - 删除 `~/.hermes/mnemos_wrapper.py`、`mnemos_config.toml`、`mnemos_install.md`
  - 删除 `~/.opencode/mnemos_wrapper.py`，并清空 `settings.json` 中的 `hooks`
  - 删除 `~/.openclaw/mnemos_wrapper.py`
  - 这些 wrapper 从未被实际调用，且 `session_end` 未传 messages，无捕获能力
- **统一 MCP preflight**
  - Codex / Hermes / OpenCode / OpenClaw 均通过各自配置中的 `mnemos` MCP server 调用 `preflight_inject`
  - 清理 Hermes `config.yaml` 和 OpenClaw `openclaw.json` 中指向不存在路径的 `memos` MCP server
- **CLI 加固**
  - `mnemos agent install/doctor codex|hermes|opencode|openclaw` 现在可通过 `active.py` 的 MCP/policy 辅助函数直接工作，无需 AgentAdapter
- **文档同步**
  - 更新 `docs/ARCHITECTURE.md` Layer 3 描述与架构变更注记
  - 更新 `README.md` Agent Source 数量

### Tests
- `python3 -m pytest tests/unit -q`：**3254 passed, 2 skipped**

---

## P0/P1/P2 审计全量修复（2026-06-05）

> 基于 `Mnemos-代码层与重构蓝图差距审计-2026-06-05.md` 的 17 项差距全部修复。核心链路从 "可运行" 提升到 "完整闭环"。

### Fixed — P0 核心链路
- **P0-1 daemon 默认 L1 扫描**
  - `auto_setup.py` 部署后自动调度 `mnemos sync backfill --source all --since 0`
  - `mnemos_cli.py doctor` 显示 skipped 统计（skipped_large / skipped_stale / skipped_over_limit）
  - `mnemos sync audit` 报告各 Agent session 缺洞和覆盖率
- **P0-2 active bridge 绕过旧外部笔记层**
  - `integrations/active_bridge.py` `_enqueue_session()` 改走 `CaptureService.capture_session()`
  - 标记 `capture_source=active_hook` 和 `completeness.visible_text=host_provided`
  - 保留 amphora 入队兼容现有蒸馏链路
- **P0-3 OpenCode 被动 AgentSource**
  - 新增 `integrations/sources/opencode_source.py`
  - 探测 `~/.opencode`、`~/.config/opencode` 等常见路径
  - 注册到 `AgentRegistry.register_builtin_agents()`
- **P0-4 旧外部笔记层分片 tag 解析不一致**
  - `SEGMENT_PATTERN` 改为 `segment[=:](\d+)/(\d+)`，同时兼容 `=` 和 `:`
  - 写入统一使用 `segment=`
- **P0-5 旧 save_session_full 截断风险**
  - 添加 `allow_legacy_truncation=False` 默认参数
  - 不传 `True` 时抛 `RuntimeError`，强制引导到 `CaptureService.capture_session()`

### Fixed — P1 增强闭环
- **P1-1 超长会话 >500k 字符蒸馏**
  - L4 知识提取层改用 map-reduce 分块蒸馏（`max_chars_per_chunk=30000`）
  - 每块记录 `covered_turn_range`、`chunk_index`、`fragment_count`
  - 精确去重 + 语义去重，不再 head-tail
- **P1-2 OutcomeCollector 数据库路径**
  - 改读 `get_config().data_dir / "sync_log.db"`
  - 状态兼容 `distill_status IN ('done', 'distilled')`
- **P1-3 AdaptiveScorerV2 增量学习**
  - 使用 `FeatureHasher + ComplementNB.partial_fit` 实现真正增量
  - replay fit：读取 pending + 最近30天 completed 样本，防止旧样本遗忘
- **P1-4 权重适配器统一**
  - `deferred_distill.py` 内部 `HardcodedWeightAdapter/BayesianWeightAdapter` 删除
  - 统一使用 `core.weight_adapter.AutoSwitchWeightAdapter`
- **P1-5 关系向量 SQLite fallback**
  - `RelationEmbeddingManager._search_sqlite_fallback()`：hnsw 不可用时遍历 SQLite cosine
  - `RelationEmbeddingManager._rebuild_from_sqlite()`：hnsw 缺失时自动重建
- **P1-6 页面 embedding chunk**
  - `EmbeddingIndexManager` 改用 `MAX_CHUNK=1200` 分块 embedding
  - `_extract_chunks()` 按 heading/段落切片，深层内容可召回
- **P1-7 KIA 事件占位步骤**
  - `core/kia/chronos.py` 注册 EventBus 消费者（page.created / page.modified / session.start / message.exchanged）
  - 代码中无 `event_only` 残留
- **P1-8 部署后 E2E 探针**
  - 新增 `scripts/e2e_probe.py`：capture → raw/sync/backend → distill → wiki → search 全链路验证
  - 探针数据自动清理
- **P1-9 ResourceBudget 动态节流**
  - 新增 `core/resource_budget.py`：CPU/内存/thermal state/power source 监测
  - 优先级分层：capture_worker (P0) > L1 sync (P1) > distill/embedding (P2) > persona/KIA (P3)

### Added — 正式版差距修复闭环（2026-06-05）
- **`mnemos status` 用户可见状态面板**
  - `_print_today_summary()` 显示：今日采集 turns（按 agent）、24h 蒸馏 sessions、Wiki 新增/修改 pages、Agent 接入状态（双通道/仅被动/仅主动）
  - 资源状态异常时显示警告（CPU/内存/温度/电源）
- **ResourceBudget 轻量性能基准**
  - 增加 `_history` 环形缓冲区（120 条 ≈ 1h @ 30s 采样），记录 (timestamp, cpu%, mem%)
  - `history_stats(hours=1.0)` 返回样本数、平均值、峰值
  - `mnemos status` 1 小时趋势行：CPU 均值/峰值、内存均值，负载状态标签（后台空闲/负载正常/负载偏高）
- **README 诚实化**
  - 资源治理标注为 beta/缺多机型基准
  - 可证伪性标记明确为实验性/兼容骨架
  - 争议仲裁标注为核心功能可用、细节完善中
- **熵减引擎增量扫描（原 TODO）**
  - `core/kia/eris.py` 新增 `_incremental_scan()`：新知识入库时仅将新页面与已有页面两两比对（O(n)），避免全库 O(n²)
  - `_on_knowledge_ingested()` 从空实现改为调用增量扫描，触发 `entropy.suggestions` 事件
  - 全库扫描 `scan()` 能力保留不变
- **P2-1 Obsidian 页面模板**
  - 首屏添加知识卡片：来源 | 置信度 | 覆盖度 | 状态（截断醒目标记）
  - `update_moc_pages()` 自动生成 4 个 MOC：最近新增 / 热门知识 / 待复盘 / 低置信度待确认
- **P2-2 预测推送**
  - `_llm_confirm()` 可选启用：边界分数 0.55-0.75 时调用 LLM 二次确认
  - `record_user_action("ignore")` 写入负样本训练（`expected_score=0.1`）
- **P2-3 auto_setup 文案**
  - 步骤数修正，蒸馏策略统一为 API 模式
  - embedding 自动复用 `SILICONFLOW_API_KEY`

---

## 五 Agent 全适配重构版（2026-05-19）

> 从 "Claude Code First" 到 "Agent-Agnostic" 的架构重构。所有 5 个 Agent 适配器成为一等公民，统一事件总线实现跨 Agent 通信。

### Added

- **五 Agent 适配器全部可用**
  - `integrations/apollon.py` — Claude Code（Hooks + settings.json）
  - `integrations/caduceus.py` — Hermes（Poll + Inbox 轮询）
  - `integrations/typhon.py` — OpenClaw（SQLite + Hooks）
  - `integrations/musae.py` — OpenCode（JSON Config + Hooks）
  - `integrations/daedalus.py` — Codex（File-based + Windows .bat wrapper）
- **统一事件总线** `core/mnemos_bus.py`
  - 文件系统事件队列（`~/.mnemos/events/{inbox,processing,archive}/`）
  - 标准事件格式：session.start / session.end / distill.request / signal.batch
  - 跨进程、跨 Agent 通信，无需额外依赖
- **统一蒸馏 Prompt** `core/hephaestus/distillation_prompts.py`
  - 单一 truth source，所有 Agent 使用完全相同的蒸馏 prompt
  - 支持数据蒸馏模式（Data Distillation Mode）
- **蒸馏格式验证层**
  - `HephaestusWorker._validate_distill_output()` 严格校验 JSON 格式
  - judgment 字段必须属于 {knowledge, skill, skip}
  - knowledge 判定要求 fragments 数组且每个 fragment 有 title + form
  - 无效输出自动移入 `distill_failed/`，避免污染 Inbox
- **Skip 智能过滤**
  - 判定为 skip 的蒸馏结果直接丢弃，不进入 Wiki Inbox
- **画像冷启动**
  - `PersonaStore._create_default_persona()` 为新用户生成默认模板
  - 所有维度初始值 0.5，confidence 0.0，避免 None 导致的空指针
- **Windows 支持**
  - `mnemos scheduler install-windows` — 注册 Task Scheduler 开机启动
  - `mnemos scheduler uninstall-windows` — 注销任务
- **画像校准 CLI**
  - `mnemos calibrate` — 交互式校准流程（1-5 分评分 + 置信度调整）
- **蒸馏重试机制**
  - `MAX_RETRIES = 3`，超期任务（24h）自动恢复为待处理
- **Daemon 预检**
  - `_run_preflight_checks()` 启动前检查目录、API、Agent 可用性、数据库
- **Agent 诊断命令**
  - `mnemos agent doctor` — 诊断所有 Agent 状态
  - `mnemos agent list` — 列出可用 Agent
  - `mnemos agent detect` — 检测宿主 Agent

### Fixed

- **Claude Code 适配器检测失败** — `is_available()` 增加 `~/.claude/settings.json` 和 `shutil.which("claude")` 检测路径
- **Caduceus datetime 导入缺失** — `from datetime import datetime` 补全
- **所有适配器 placeholder 格式错误** — judgment 值从 `keep/skip` 修正为 `knowledge/skill/skip`
- **Apollon timezone 导入缺失** — `delegate_distillation()` 使用的 `timezone.utc` 未导入
- **文件编码问题** — 13 处 `write_text()` 补全 `encoding="utf-8"`
- ** distill_queue 双通道问题（历史口径）** — 当时所有蒸馏路径统一为队列入口；2026-07-10 起生产入口已进一步收口为 `enqueue_with_receipt()` 与 revision-aware typed receipt，旧无回执 `enqueue()` 已删除。

### Changed

- **架构升级**：从单层处理模型升级为三层模型（Agent 适配器层 → 事件总线 → 核心服务层）
- **Agent 检测优先级**：Claude Code > Hermes > OpenClaw > OpenCode > Codex
- **README 重写**：新架构图、五 Agent 适配器说明、更新后的 CLI 命令列表

---

## [Unreleased] — 持续集成

### P0-P3 代码复查与修复（2026-06-01 ~ 2026-06-02）
> 全链路代码复查，修复 12 处生产隐患，新增 12 个测试文件、25 个测试用例，702 passed。

#### Fixed — P0 核心链路
- **L1 兜底去重** (`sync_engine.py`)
  - `_trace_sync_log` 改为 opt-in，`_save_content()` 显式传 `_trace_sync_log=False`
  - 统一 `content_hash=<16位>` 标签，旧外部笔记端二次查重兜底
  - 修复 turn_number/memouids JSON 格式
- **旧外部笔记→Wiki 追溯** (`wiki_builder.py`)
  - `_link_session_memos_to_wiki()` 同时更新 `sync_log.wiki_page_paths/distill_status/distilled_at`
  - frontmatter 添加 `source_session` / `蒸馏时间`
  - `_mark_processed()` status 对齐实际 method（distilled/skipped_low_quality 等）
- **KG 事件主路径** (`distillation_engine.py`)
  - 提取 `_emit_knowledge_distilled()` 公共函数
  - `wiki_builder` 流水线成功后接入事件发射

#### Fixed — P1 质量与感知
- **Freshness 假绿** (`freshness_alert.py`)
  - `FreshnessResult` 区分 `fresh/stale/not_found/error` 四态
  - 修复 `entity.meta` → `last_updated` 字段错配
  - MCP 不再把异常包装为 fresh
- **Predictive Push 错推** (`predictive_push.py`)
  - 主题抽取优先 code token / env var / 英文词
  - 增加 relevance gate（ContextAwareSearch score ≥ 0.55）
  - 结果 title 必须包含 query token
- **Blindspot 降级** (`blindspot_discovery.py`)
  - 修复 `BlindSpotProfile` dataclass `.get()` 错配
  - `_detect_blindspots()` 返回 `degraded/degraded_reasons`
  - MCP `_tool_blindspot_check` 暴露降级状态
- **搜索 false positive** (`context_search.py`)
  - 过滤 `relevance < 0.15` 的弱相关结果
- **来源分布一致** (`mnemos_cli.py doctor`)
  - 复用 `fm_get()` 兼容中英文 source
  - claude/kimi/codex/openclaw/hermes 归为 distilled
  - 排除系统页

#### Fixed — P2 评分与数据质量
- **评分闭环** (`adaptive_scorer_v2.py`)
  - `enqueue_training_sample()` 同时写入弱 ground_truth
  - `_get_training_samples()` 按 dimension 匹配
  - `audit_check.py` P3 判定修正（检查 scorer_models/training_queue）
- **KG 置信度治理** (`knowledge_graph.py`)
  - `apply_discovered()` 应用 source_method 上限：same_directory ≤ 0.45, hash_prefix ≤ 0.55, keyword_overlap ≤ 0.65, link_parse ≤ 0.8
- **历史截断数据**（历史实现；当前代码已退役 `mark_truncated.py`）
  - 当时扫描旧外部笔记中的历史完整性标记（10+ 种正则），写入已退役的完整性标记表
  - doctor 报告 `raw_incomplete_count`
  - 新 `save_long_content()` 已移除截断，自动分片

#### Fixed — P3 宣传与工程
- **README 降调**：标题曾改为当时的预发布口径，移除"零手动""全自动闭环"等过度宣传，标注核心链路可用 / 高级能力完善中
- **测试隔离**：`tests/conftest.py` autouse fixture 全局 mock `publish_event`，根治测试污染 `~/.mnemos/events.db`

#### Added — 测试
- `test_l1_memos_duplicate_fallback.py` — 4 tests（去重兜底）
- `test_memos_wiki_traceability.py` — 2 tests（追溯链路）
- `test_distill_to_kg_event_path.py` — 3 tests（KG 事件）
- `test_freshness_alert.py` — 4 tests（新鲜度）
- `test_blindspot_discovery.py` — 3 tests（盲点降级）
- 历史截断标记测试 — 12 tests（当前代码树已移除）

---

### Knowledge-in-Action 闭环系统（2026-05-07）
> 从"知识沉淀"到"知识驱动行动"的完整闭环。不仅存储知识，更在实际工作中主动应用、复盘、迭代。

#### Added — 7个核心模块
- `core/task_classifier.py` — 通用任务分类器
  - 支持 coding/marketing/analysis/strategy/writing 五大类型及子类型
  - 关键词匹配 + 历史模式学习，置信度分层确认（>0.9静默/0.7-0.9提示/<0.7询问）
  - 自动提取预期目标（参与人数、转化率、预算等）
- `core/time_parser.py` — 时间解析器
  - 中文/英文相对时间解析（今天/明天/下周/下个月/明年Q1）
  - 周期性检测（weekly/biweekly/monthly/quarterly），加权滑动窗口
  - 返回 TimeWindow（immediate/short/medium/long/periodic）
- `core/pre_flight_injector.py` — 预加载注入器
  - 从 wiki/retrospectives/ 装载历史经验
  - 知识衰减排序（freshness_score，每版本衰减0.1）
  - 场景适配过滤（applies_when/not_applies_when）
  - 命中追踪（hit_count/last_hit）
- `core/in_process_guard.py` — 执行中守护
  - 三级策略：轻微偏差静默记录、中等偏差自然融入、严重偏差打断确认
  - 基于 checklist trigger_keywords 和 risk_patterns 匹配
- `core/auto_retrospective.py` — 自动复盘引擎
  - 触发检测：复盘关键词 + 自然结束检测
  - 预期 vs 实际对比，提取差异
  - checklist 使用情况问责记录
  - 提取新增教训
- `core/iteration_tracker.py` — 迭代版本追踪器
  - 基于复盘结果自动生成连续迭代版本
  - 知识衰减合并，更新 active 软链接
  - 归档旧版本到 .archive/
- `core/knowledge_scheduler.py` — 知识调度器
  - 使用 live_sync.db 存储远期/周期性任务
  - 启动补偿扫描，避免漏掉
  - 中期提前3天提醒，长期提前7天提醒

#### Added — 集成到 claude_integration.py
- `--session-start` 时自动调用 TaskClassifier + PreFlightInjector
- `--session-end --session-messages='...'` 时自动触发 Auto-Retrospective
- `--kia-check` 检查调度器中的到期提醒

#### Added — 复盘数据目录
```
wiki/retrospectives/
├── coding/
├── marketing/
├── analysis/
├── strategy/
└── writing/
```

#### Changed
- `claude_integration.py`: 导入 KIA 全部7个模块
- `claude_integration.py`: `get_context_for_claude()` 增加 KIA 知识装载逻辑

---

### Karpathy 蒸馏范式迁移（2026-05-03）
> 旧 Wiki 体系（Clean/Expand/L0-L9）全面废弃，改用 Karpathy LLM Wiki 范式。
> 旧外部笔记层无损保留全部上下文，LLM 主动蒸馏成结构化 Wiki。

#### Added
- 新建 `core/topic_splitter.py` — 轻量 LLM 话题切分器
  - 输入：一个 session 的消息列表
  - 输出：主题块列表（topic/start_msg/end_msg/type）
  - type：concept（有结论）/ thread（没结论）/ skip（跳过）
  - prompt 轻量，只切分不蒸馏，内容截断到 3000 字符控制费用
- 新建 `core/distiller.py` — 蒸馏器主模块
  - 概念文蒸馏：有结论的对话 → wiki/concepts/xxx.md
  - 话题串蒸馏：没结论的讨论 → wiki/threads/xxx.md
  - 质量自评（0-1），低于 0.3 丢弃
  - 内容指纹去重（MD5），已蒸馏过自动跳过
  - 自动更新 wiki/index.md 索引
  - 反向索引：记录 source_memos 到 wiki 的映射
- 新建 `core/wiki_quality.py` — 质量追踪系统（替代 L0-L9 热力）
  - 指标：完整性、新鲜度、矛盾数、引用深度、原子化程度
  - 状态：verified / draft / stale / conflicted
  - 存储：~/.claude/wiki_quality.db
  - 指纹表：distill_log（content_hash → wiki_path）
  - 反向链接：wiki_backlinks（谁引用了谁）
- 新建 `scripts/distill_worker.py` — 定时/手动蒸馏 Worker
  - 定时模式：`--run`，每天晚上 8:30 扫描增量
  - 手动模式：`--manual --uids uid1,uid2`
  - checkpoint 机制：上次成功时间 → 只处理新数据
  - RunAtLoad：开机自动补跑（错过的时间）
  - 从 ~/.zshrc 加载 MEMOS_TOKEN（launchd 不继承 shell env）
- 新建 `~/Library/LaunchAgents/com.memos.wiki.distill.plist`
  - 每天晚上 20:30 触发
  - 日志：/tmp/memos_distill.log

#### Changed
- `memos_sdk.py`: `mark_l1_processed()` 标记为废弃（空操作，打印警告）
  - 蒸馏体系用指纹表追踪状态，不在旧外部笔记层打 processed 标签
- `memos_sdk.py`: `batch_save()` 去掉 `type=clean-ingest` / `type=expand-ingest` 标签
- `claude_live_sync.py`: 去掉 `processed=false` 标签写入
  - 保留 source/thread/time/scope 描述性标签
  - `AUTO_INGEST_CLEAN = False`（改由 distill_worker 定时处理）
- `batch_clean_submit.py`: 整文件改为废弃提示（后续于 2026-07-01 删除占位入口）
- `ingest_engine_service.py`: 整文件改为废弃提示（后续于 2026-07-01 删除占位入口）

#### Removed
- 清掉旧 `wiki/` 目录全部内容（用户确认无价值）
- 删除旧 Wiki heat 数据库文件
- 删除 `~/.claude/expand_v2.db`
- 废弃标签：`processed=true/false`, `ingest=wiki/skip`, `cleaned-to:{uid}`
- 废弃旧 L0-L9 热力追踪体系（代码保留但不再维护）

#### 待办（首尾工作）
- [ ] 停用旧 wiki 相关 launchd 任务（cold_demotion, draft_clean, expand_scan, heat_decay, health_check, synthesis_pipeline, weekly_report, wiki_tags_sync）
- [ ] 跑首次全量基线扫描（把历史旧外部笔记过一遍蒸馏）
- [ ] 观察一周 wiki 产出质量，调 prompt 和阈值

### Added
- `scripts/health_check.py`: 多库检查扩展
  - 新增 `ingest_engine.db` / `expand_v2.db` 健康检查
  - 新增 L0-L9 全分布统计 (`level_distribution`)
  - 新增衰减候选检测：15天未访问 (`stale_pages`) 与 60天沉睡 (`deep_sleeping`)
  - `check_database()` 返回结构改为按库分层的多字典

### Fixed
- `scripts/health_check.py`: 已全面迁移至 Wiki heat 表，旧 `heat_scoring.db` 彻底退役

### Infrastructure (Wave B)
- 新建 `core/event_queue.py` — SQLite 轻量事件队列
  - 表结构：event_queue (id, event_type, entity_name, payload_json, dedupe_key, status, retry_count...)
  - 去重：同 dedupe_key 只保留一条，应用层自动跳过重复入队
  - 重试：失败 3 次后转 dead_letter，支持延迟重试（5min 退避）
  - 接口：enqueue / dequeue / mark_done / mark_retry_or_dead / get_stats / peek_dead_letters
- 新建 `core/rate_limiter.py` — Token Bucket LLM 限流器
  - 默认配置：并发 ≤3，每分钟 ≤30 调用，调用间隔 ≥1s
  - 支持阻塞 acquire() 与非阻塞 try_acquire()
  - 全局单例 `get_global_limiter()`，所有 LLM 调用点统一接入
- 新建 `scripts/event_worker.py` — Tier 2 队列消费 Worker
  - 轮询间隔 30s，批次 5 条
  - 已集成 rate_limiter，限流时自动回退重试
  - 处理器注册表当前为空，Wave C/D 逐步填充

### Architecture (Wave C)
- `ingest_engine.py`: `entity_source_count` 表加 `category` 字段 + 增量迁移
  - 新 schema: entity_name, source_count, created_at, last_updated, category
  - 旧数据回填: `UPDATE ... SET category = 'unknown' WHERE category IS NULL`
  - `_increment_entity_source_count()` 新增 `category` 参数，调用方传入 `refined.content_type`
- `core/cross_validator.py`: `_semantic_similarity()` 接入真 LLM
  - 优先调用 `LLMHelper.call_llm()`（已集成 Wave B rate_limiter）
  - prompt: 要求返回 0-1 数字评分
  - LLM 失败时 fallback 到关键词重叠，并打 WARNING 日志
- `core/llm_helper.py`: `LLMHelper` 新增通用 `call_llm()` 方法
  - 懒加载 Anthropic client，兼容 `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`
  - 集成 `get_global_limiter()`，所有 LLM 调用自动过限流
- `document_processor.py`: 验证流程补全
  - `_call_claude_vision()` 改为走 `LLMHelper.call_llm()`，统一限流
  - `validate_extraction()` prompt 新增 `reject` 选项
  - `process_document_with_validation()` 新增 reject 路径
  - 新增 `save_to_rejected()` 方法：验证拒绝的文档保存到 `~/.claude/rejected_documents/`

### Event-Driven (Wave D)
- `core/expand_engine.py`: 新增 `evaluate_entity()` 单实体评估接口，复用批量逻辑
- `scripts/expand_scan.py`: 明确标注为 Tier 3 兜底扫描，主路径已迁移到事件驱动
- `ingest_engine.py`: `_increment_entity_source_count()` 成功后自动 enqueue `expand_eval` 事件
  - dedupe_key = `expand_eval:{entity_name}`，避免重复评估
- `core/wiki_heat_tracker.py`: `_add_heat()` 成功后自动 enqueue `sync_wiki_tags` 事件
  - dedupe_key = `sync:{page_id}`，同一页面只保留一个 pending 同步
- `scripts/sync_wiki_tags.py`: 提取 `sync_single_page()` 单页同步函数，供事件 handler 调用
- `scripts/event_worker.py`: 注册两个 handler
  - `expand_eval` -> `ExpandEngine.evaluate_entity()` + `ExpandExecutor.execute()`
  - `sync_wiki_tags` -> `sync_single_page()`（从 DB 读取最新 level/score）
- `synthesis_pipeline` 保持定时，后续 Phase 4 单独迁移（触发条件复杂，需观察）

### Cleanup (Wave E)
- 根目录 4 个 .py 搬入 `core/`
  - `entity_resolver.py` → `core/entity_resolver.py`
  - `ai_self_check.py` → `core/ai_self_check.py`
  - `conflict_merger.py` → `core/conflict_merger.py`
  - `cross_ai_tracker.py` → `core/cross_ai_tracker.py`
  - 更新 import：`ingest_engine.py` ×3, `ai_context_reader.py` ×1
- `HEAT_SYSTEM_COMPLETE_GUIDE.md` → `research/archive/HEAT_SYSTEM_COMPLETE_GUIDE.md`
  - 整份描述旧 `heat_scoring` L2-C/B/A 系统，已过时

### AI Integration Fixes (改造2)
- `ai_memory_sync.py`: 标签统一（P0）
  - 4 处 `status=ready-for-ingest` → `processed=false`
  - 补 `source=hermes` / `source=openclaw` + `ingest=wiki`
  - Hermes sessions / OpenClaw files+chunks 同样补全
- 删除 `ingested` 死代码（P1）
  - `batch_clean_submit.py:61` / `ingest_engine_service.py:93,276`: 删掉 `or "ingested" in tags`（历史文件位置；两者后续于 2026-07-01 删除）
  - `core/namespaces.py:301`: special_tags 移除 `"ingested"`
- 新建 `scripts/migrate_status_tag.py`: 一次性迁移脚本
  - 遍历旧外部笔记所有 `status=ready-for-ingest` → `processed=false`
  - 防御：已有 `processed=true` 则跳过
- Token 泄露处理
  - `sync_all.sh`: 移除硬编码 token，改为从环境变量读取，缺失时报错退出
  - `AI_INTEGRATION.md`: 7 处明文 token 全部替换为 `$MEMOS_TOKEN`
  - 新增 "Token 安全配置" 章节，说明配置在 `~/.zshrc` + `chmod 600`
- 新建 `~/.claude/CLAUDE.md`: 跨会话记忆查询协议
  - 触发关键词：时间指代 / 会话续接 / 回忆复盘 / 历史引用
  - 执行命令：`claude_integration.py --session-start`
  - 禁止：知识查询类问题查旧外部笔记、每轮都查、结果直接展示

### 已知遗留 (待处理)

- `ingest_engine.py` 当前 ~70KB（1700+ 行），仍属 God Class，可继续拆分（解析/落库/调用 LLM 三段独立化）
- 5 个 .py 文件 >30KB（document_processor, image_processor, ingest_engine, memos_sdk, wiki_reader）值得评估拆分
- `core/expand_executor._detect_conflicts` 为占位（空 pass），需等建立 `entity_sources` 关联表后才能真正接通 `cross_validator`
- Q4 watchdog 实时监听 Hermes：P0 修完后观察 3-5 天，按数据决定是否需要
- `synthesis_pipeline` 事件驱动化（Phase 4）待后续单独实施
- 文档审计报告需重新跑（P1/P2 后"4 个死代码模块"已全部被引用）

---

## 2026-05-02 — L0 浅下沉 + 文档归并

### Changed
- `core/wiki_heat_tracker.py` `MIN_SCORE`: -100 → -30
  - 设计动机：单次搜索命中 L0 页面 = wake +30 + 事件分 → 直接脱沉睡，"一次搜索就能回归正分"
  - 旧 -100 floor 下最坏要 9 次衰减触底，现在最多 6 次，沉睡更"浅"更可逆
  - 全仓 L0 描述同步更新（README、ARCHITECTURE、旧 Mnemos-Auto 文档、research 系列）

### Removed
- `~/Desktop/ai/` 下 14 份镜像 .md（与 `~/memos-client/` 完全相同或更旧）
- 旧 README 简版（已被 `README.md` 完整版取代）
- `docs/EXPAND_2_0_ARCHITECTURE.md`（旧版，根目录有更新版）

---

## 2026-05-02 — P0-P3 重构周期收尾

### P0-1 — 清理 requirements.txt 错误依赖
- 移除项目实际未使用的依赖项

### P0-2 — `scripts/expand_scan.py` 迁移 V1→V2
- 旧 V1 引擎调用全部切到 V2 接口

### P0-3 — 热力等级单一事实源
- `core/wiki_heat_tracker.py` 引入 `LEVEL_RANGES` 元组表
- `_calculate_level` 用阈值表替换硬编码区间判断
- `PROMOTION_THRESHOLDS` 由 `LEVEL_RANGES` 自动推导
- 修改 L0 范围只需改一处常量

### P1-1 — 删除 3 个死代码函数
- `quality_assessor.assess_content_quality`
- `four_category_engine.classify_and_refine`
- `expand_engine_v2.evaluate_expand_candidates`

### P1-2 — 等级字符串比较改数值比较
- 新增静态方法 `WikiHeatTracker._level_int("L7") -> 7`
- 修复字典序反模式：旧代码 `"L10" < "L9"` 字面量比较出错
- 6 处比较点全量替换

### P1-3 — 删除 V1 模块并重命名 V2
- 删除：`core/expand_engine.py` (V1)
- 重命名：`core/expand_engine_v2.py` → `core/expand_engine.py`
- 全仓 6 处 `from core.expand_engine_v2 import` 已统一为 `from core.expand_engine import`
- 注：导出类名仍是 `ExpandEngineV2`（待去除 `V2` 后缀，见 [Unreleased]）

### P2-1 — `ingest_engine.py` God Class 渐进拆分
- 抽出 7 个去重/抽取纯函数 → `core/ingest_helpers.py`
  - `compute_fingerprint`, `is_duplicate_content`, `extract_concept_definition`
  - `extract_entities_fallback`, `extract_concepts_fallback`
  - `extract_entity_description`, `detect_wiki_reference_pollution`
- ingest_engine 内 `_extract_tech_entities` / `_parse_list` 删除（无调用点）
- 其余 7 个方法降级为 thin wrapper（保签名零侵入）

### P2-2 — 补三件套核心 unit test
- 新增 `tests/unit/` 67 个测试用例：
  - `test_wiki_heat_tracker.py` — `LEVEL_RANGES` SOT、`_calculate_level`、`_level_int`、L0/L9 边界、L10 前向兼容
  - `test_statement_classifier.py` — 句子切分、分类模式、级别准入
  - `test_cross_validator.py` — 事实抽取、语义相似度、硬事实冲突、置信度封顶
  - `test_ingest_helpers.py` — 七个抽取函数全覆盖
- 67 tests 0.040s 全绿

### P3-1 — 项目文档与桌面 ai 文档同步
- 全仓 L0/L9 数值描述同步
- 之后又触发 [2026-05-02 文档归并] 将桌面镜像清除

---

## 2026-04-30 — 死代码审计（已部分过期）

> 原报告：`memos_dead_code_audit_report.md`（合并后已删除）
> 状态：**报告时效已失，原文标记为"0 引用"的 4 个 .py 文件在 P1-P2 集成后均已被引用**

### 历史结论（仅供参考，不再适用）
| 模块 | 审计时状态 | 当前 (2026-05-02) |
|------|----------|-------------------|
| `entity_resolver.py` | 0 引用 | ✅ `ingest_engine.py:191` 实例化、`:960/971` 调用 |
| `ai_self_check.py` | 0 引用 | ✅ `ingest_engine.py:192` 实例化 |
| `conflict_merger.py` | 0 引用 | ✅ `ingest_engine.py:193` 实例化、`:1056/1088` 调用 |
| `cross_ai_tracker.py` | 0 引用 | ✅ `ai_context_reader.py:63` 实例化、`:225/270` 调用 |

### 历史指标（审计当时）
- 总 Python 文件数：43
- 总函数/方法数：~350+
- 标记的死代码模块：4 个
- 标记的死代码方法：2 个

---

## 2026-04-29 — 四大类信息识别集成（早期工程）

> 原报告：`INTEGRATION_REPORT.md`（合并后已删除）

### Added
- `config/entity_config.yaml` — 四大类分类完整配置
- `core/four_category_engine.py` — Layer1-4 四层识别引擎
  - Layer1: 元数据标签识别
  - Layer2: 结构特征匹配
  - Layer3: LLM 语义识别
  - Layer4: 动态权重融合
- `core/llm_helper.py`（既有）

### Changed (`ingest_engine.py`)
- 引入 `FourCategoryEngine` 实例化
- `_extract_categorized_content` 新方法：调用引擎做智能分类提炼
- `_process_clean` / `_process_expand` / `_create_source_page` 三处改造：分类信息接入
- 后向兼容保留

---

## 2026-04-29 — 热力架构迁移审查（heat_scoring → wiki_heat_tracker）

> 原报告：早期架构审查报告（合并后已删除）

### 关键问题（已修复）
- 🔴 新旧热力系统并存：旧 `heat_scoring.py` (L2-C/B/A, 5 级) vs 新 `wiki_heat_tracker.py` (L0-L9, 10 级)
- 🔴 双数据库：旧 heat scoring 数据库 vs Wiki heat 数据库
- 🔴 7 个文件仍引用旧系统（已在 P1-3 全量收敛到新系统）：
  ```
  ingest_engine.py:35, verify_automation.py:39, heat_integration.py:13,
  heat_monitor.py:16, l1_refinement.py:32, claude_live_sync.py:22,
  cross_ai_tracker.py:18
  ```

### 关键风险（已修复）
- 🔴 `wiki_heat_tracker.on_ai_search_hit` 潜在无限递归（边界条件已加 guard）

---

## 2026-04-30 — 早期热力架构审查

> 原报告：`ARCHITECTURE_REVIEW_REPORT.md`（合并后已删除）

### 已识别矛盾点（部分修复）
1. 🔴 `DocumentProcessor` 字段缺失：`extraction.validation_status / needs_review / review_reason` 在 `knowledge_inbox.py:313-360` 被引用，但 dataclass 未定义
2. 🔴 `DocumentProcessor.save_to_memos_with_review()` 被 `knowledge_inbox.py:322` 调用但方法不存在
3. 🔴 `ImageProcessor.save_to_memos` 签名不一致
4. 🟡 `ingest_engine.py:789, 874` 动态导入路径脆弱
5. 🟡 `DocumentProcessor` 缺验证机制（图片处理器有完整 Claude Vision 验证流程，文档处理器无）
6. 🟡 配置不一致：`ai_context_reader.py` 用 `Path.home() / "memos-client"`，其他用 `os.path.expanduser("~/memos-client")`

> 注：上述 1-3 项部分仍存在；建议在下一轮 doc-processor 重构时一并解决。

---

## 早期架构阶段摘要

### 全自动架构阶段（2026-04 中期）
- 热力追踪迁移至 Wiki 层（`wiki_heat_tracker.py`）
- AI 仅搜索 Wiki，不搜索旧外部笔记原始草稿池
- 10 级 L0-L9 体系，L9 封顶 500
- 衰减 + 冷降级双机制

### 人工审核阶段（2026-04 早期）
- 热力追踪在旧外部笔记层
- 6 级 L1-L5 + L6 体系
- 现已被后续架构完全取代

---

*本文件为 SOT — 不再维护多份 ARCHITECTURE_REVIEW / INTEGRATION_REPORT / dead_code_audit 散文件。*
