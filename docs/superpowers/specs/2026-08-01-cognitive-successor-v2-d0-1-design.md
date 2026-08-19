# 认知后继 V2：设计裁决、待裁决条款集、双分母与 D0.1 启动设计

## 文档状态

**状态：DRAFT / NON-GOVERNING / DESIGN-ONLY / USER-REVIEW-REQUIRED。**

本文是对既有
`docs/superpowers/specs/2026-08-01-cognitive-successor-capability-atomicity-design.md`
的增量设计，不覆盖、不重解释，也不把该文件升级为 governing contract。

本文作者工作快照为：

- latest authoring base commit：`7b72d0d6026693b8f81c12943a658c98f9358756`；
- latest authoring base tree：`4569bd4c53419532f5c86f8411b6de625a2abcf7`；
- D0 v1 绑定的 legacy behavior snapshot 仍为
  `1e36a31a26b0b5baf768815f185d57174e9c59dd`，本文不移动该基线；
- 既有 successor design SHA-256：
  `6e2fbcbbccf02b9c7b5fbc7bba484b5ea8f7cc8022ac734f381f26af2ec82484`；
- review date：2026-08-01。

本文不批准生产代码实现、D0 v1 原地改写、测试门禁弱化、daemon 启停、真实 API、
生产配置读取、生产迁移、生产 replay、生产数据写入或 cutover。本文也不冻结最终
产品名；“V2”只是本轮设计版本标签。

机器可读的待裁决条款集位于
`docs/acceptance/cognitive_successor_d0_1/constitution.candidate.json`。它没有 approval
receipt，其中全部 clause 都是 `NOT_ADJUDICATED`，不能被解释为已经成立或激活的宪法。
候选裁决方法单独位于
`docs/acceptance/cognitive_successor_d0_1/design_adjudication_method.candidate.json`；方法
自身不能自证。Decision Map #1 的增量 written spec 位于
`docs/superpowers/specs/2026-08-01-cognitive-successor-design-adjudication-design.md`；它把
本文的初始 criteria list 深化为约束优先、claim-kind-aware 的裁决方法，并修正
`REVISE` 可激活原字节等 bootstrap 缺口。该方法当前仍等待 exact-byte challenge、分离的
finalizer、typed dependency bundle、post-final structural verification、用户 exact-byte
authorization 和 detached post-authorization acceptance，不是 active constitution。

## 一、设计阶段的裁决规则

### 1.1 没有旧宪法或旧版本的自动优先权

认知后继仍处于设计阶段。Mnemos 现行合同、旧 successor 骨架、保留的 V2、用户
此前表达的方向、Claude/Kimi 审查和本设计都属于 **design evidence**，不能因为
“更早确定”“写进合同”“来自某个 reviewer”或“刚刚提出”就自动获胜。

每项候选必须先按 claim kind、typed standing 与适用硬约束分类，再按同一冻结范围
重新裁决：

1. 是否更完整地实现当前产品目标；
2. 是否符合可辩护的认知理论和经验事实，并诚实声明理论边界；
3. 是否与当前代码、数据、入口、状态和 effect 现实相容；
4. 是否能形成清晰 owner、稳定 Interface、较高 Depth 和较好的 Locality；
5. 是否降低数据、安全、隐私、恢复、迁移和操作风险；
6. 是否可被独立 oracle、反例和 falsification prototype 推翻；
7. 是否在完整生命周期成本下可工程落地，而不是只在纸面上漂亮。

对最终实现候选，以上标准按非补偿式优先级运作：完整功能分母、安全、数据和恢复先
通过；随后冻结 exact material metric denominator、scenario/cohort coverage 和 typed
baseline registry，由 normalized candidate-baseline effect interval 对 SLO、non-inferiority、
equivalence 与 meaningful-improvement margins 推导逐指标状态。slice 只证明 applicable
metrics 的 SLO/non-inferiority；最终 cutover 才要求性能和体验两个非空完整分母各有至少
一项预声明 meaningful improvement 且无 material regression。metric normalization、target-
range distance、interval/margin ordering 与 state precedence 必须由 verifier 单值重算；
confidence interval 跨越 non-inferiority 边界时为 `UNKNOWN`。只有 exact all-material profile
中的 cognitive、safety、privacy、agency、data、effect、recovery、performance 与 experience
outcomes 全部等效时，才以 typed-fact
`K_API/K_AUTHORITY/K_PATH/K_CHANGE/K_LIFECYCLE/K_ABSTRACTION` 的 exact-denominator
独立 whole-system 测量选择更简洁优雅的代码；最终实现必须相对 approved outcome-equivalent
implementation baseline 至少改进一项 predeclared material complexity fact、无 material
complexity regression，并通过 negative oracles 与绑定 profile/oracle/workload/evidence 的
local quality guardrails。代码行数本身不是简洁 oracle，god Interface、
假 Seam、删除 required test/runbook 或把复杂度移入配置、schema、数据、测试和 runbook
也不是简洁。

来源身份、文档年代、reviewer 数量和措辞强度都不是“正确”的代理。如果证据不能
裁决，状态保持 `UNRESOLVED`，通过研究、原型或用户价值裁决继续推进；不得以旧合同
默认值或多数投票填空。

### 1.2 旧合同的作用域

现行 Mnemos governing contract 继续约束本仓库和旧生产系统的实际操作：本轮仍不
启动 daemon、不读取或修改生产数据、不迁移、不 replay、不调用真实 API，也不降低
门禁。但该合同对 successor 的产品语义、Module 边界、物理拓扑和认知模型只是一份
重要证据，不拥有自动设计裁决权。

### 1.3 当前候选方向全部可重开

| 议题 | 当前候选 | 设计状态 |
| --- | --- | --- |
| 功能终点 | 切换前保留 100% 有效 Mnemos 功能；无效旧行为通过显式 adjudication 拒绝 | 强候选 / REOPENABLE |
| 旧系统角色 | 冻结、可用、数据源、离线 oracle、行为参考和 data-forward rollback engine | 强候选 / REOPENABLE |
| 建设路线 | 新架构、完整功能分母、选择性迁移，不机械复制旧文件 | 当前推荐 / REOPENABLE |
| 生产比较 | 单生产 writer；优先离线 replay/隔离 clone，不默认生产双写 | 安全候选 / REOPENABLE only with stronger proof |
| 产品目标 | 理解用户，并利用 AI 的广泛信息补足用户的信息边界；不能只模拟用户认知 | 当前产品目标候选 / REOPENABLE |
| 双分母 | 分开证明 legacy parity 与 V2 cognitive adequacy | 当前推荐 / REOPENABLE |
| V2 原稿 | 保留现有 bytes 作为参考版本，不覆盖 | 历史保存，不等于规范优先 |
| 产品名 | 应表达 cognition，不使用 memory-only 或 vNext | 候选约束；具体名称未决 |

“同意开始”授权创建和比较设计资产，不等于批准任何候选 clause、denominator 或
最终 JSON bytes。只有经过上述 adjudication 后仍成立的结论，才进入 adopted candidate
clause set；
detached finalizer 形成 non-governing final candidate、typed bundle 固定完整依赖、post-final
verifier 验证 pre-authorization exact bytes，用户批准同一 root/action/scope/time/recovery
boundary，且其后的 detached acceptance gate 重新验证授权 raw bytes、内部文本 hash、principal、
scope、recovery 与双时间窗后，才可能成为后续冻结输入。

### 1.4 正确性裁决与激活授权是两件事

“谁是对的”不能只靠身份解决，也不能只靠一句抽象判断解决。本设计先用 non-terminal
`MethodProducerAssessment` 保存 producer 的工作假设，再由 detached challenge、分离的
finalizer 产生 `FinalMethodAdjudicationRecord` 候选，随后由 typed dependency bundle 与
post-final structural verifier 把最终选择转成可检查的证据过程。完整方法和字段由增量 written spec 与本票专用
`design_adjudication.schema.json` 定义；最低语义包括：

| 字段 | 作用 |
| --- | --- |
| `competing_claims` | 明确互相冲突或可替代的主张，不制造稻草人版本 |
| `evaluation_criteria_version` | 绑定同一组产品、理论、工程、安全与演进标准 |
| `supporting_evidence_refs` / `refuting_evidence_refs` | 同时保存支持与反对证据，不按来源身份加权获胜 |
| `theory_scope_and_limits` | 说明论文证据适用的人群、任务、时间尺度与外推限制 |
| `legacy_compatibility_effect` | 说明对现有能力、数据和迁移的真实影响 |
| `engineering_feasibility_effect` | owner、Interface、状态、性能、成本和可运维性影响 |
| `safety_privacy_recovery_effect` | 安全、隐私、回滚、审计和失败恢复影响 |
| `falsification_plan_or_result` | 什么反例、原型或实验会推翻当前结论 |
| `disposition` | `ADOPT`、`REVISE`、`REJECT`、`RESEARCH_REQUIRED`、`PROTOTYPE_REQUIRED`、`USER_VALUE_CHOICE_REQUIRED` 或 `EQUIVALENT_SET` |
| `rationale` / `challenger_refs` | 可复核理由和独立 challenger |

只有独立 challenge、non-governing final `ADOPT` candidate、完整 bundle、post-final
`ELIGIBLE` structural verification、exact user authorization 和 `ACTIVATION_ELIGIBLE`
post-authorization receipt 全部绑定同一 bytes，方法才可在批准的 design scope 中使用；这仍
不能自动生成 adopted candidate clause。`REVISE`
终结原 candidate generation，必须生成新 bytes 并从头
裁决；`RESEARCH_REQUIRED` 和 `PROTOTYPE_REQUIRED` 保持未决，不能继承旧系统默认值。
最终用户 exact-byte
approval 是对进入下一阶段或未来激活的授权，不是让一个缺少证据的主张变成正确；授权前的
structural verifier 也不能预先证明未来授权有效。

`constitution.candidate.json` 因而是 **clause proposal ledger**，不是待逐项改绿后原地
变成宪法的列表。`REJECT` 仍保留历史记录但进入 `EXCLUDED_REJECTED`；只有
`ADOPTED_CANDIDATE + INCLUDED` 派生 adopted clause set；`REVISION_REQUIRED` 原字节
进入 `EXCLUDED_REVISION_REQUIRED`。条件约束由
`constitution.candidate.schema.json` 表达：当前 v5 producer-only generation 的 17 个条款
必须全部保持 `NOT_ADJUDICATED` 与空 evidence binding；伪造的 adopted/revision-required/
rejected 状态在这一代不可表示。未来 ledger schema generation 才可在绑定 payload hash、
competing claims、record、evidence root、detached challenger、final、bundle、post-final
verifier 和 freshness 后表达终态。有效性不能由 producer 自报布尔值。裁决方法与 schema
也以 exact SHA-256 绑定；旧 record 不能批准后来改变的 method bytes。

## 二、为什么 D0 v1 不能直接继续填绿

当前 D0 v1 是必要而诚实的 discovery artifact，但不是 freeze-capable wire format：

- `verification_scope.mode=DISCOVERY_ONLY`；
- `freeze_capable=false`；
- `freeze_evaluator_unimplemented=1`；
- `denominator_frozen=false`、`denominator_approved=false`；
- 39 个 capability 全部仍为 `ADJUDICATION_REQUIRED`；
- 1,172 个 surface 未映射，148 个 canonical owner 未知，39 个 effect target 未知；
- typed adjudication、owner/target registry、independent oracle receipt、config
  applicability 和完整 reverse state/effect inventory 尚不存在。

因此当前证据不支持通过给 v1 增加一个状态字符串、删记录、合并 ID 或手工回填
receipt 就把它称为 v2。D0.1 保持现有 v1 bytes 不动并将其作为 challenger/input；
长期是否保持原格式、怎样绑定新 family，留给 Decision Map #2/#13 裁决。任何
freeze-capable 方案都需要独立 verifier 重算完整性。

更关键的是，D0 v1 当前只有两条 prior successor constitution candidate：全量旧
功能和旧系统 oracle/rollback。它们需要与其他候选一样重新经过 design-stage
adjudication；即使最终保留并闭合，也只证明“完整后继 Mnemos”，不能证明“V2
认知系统”已经具备主动扩展、元认知纠偏和人机互补能力。

## 三、D0.1 的三种做法

| 方案 | 优点 | 主要风险 | 裁决 |
| --- | --- | --- | --- |
| A. 在 D0 v1 中直接加入新字段并逐项改绿 | 文件最少 | 原地重解释 discovery wire；旧 hash/receipt 语义失真；容易制造假冻结 | 本次 bootstrap 不选 / REOPENABLE |
| B. 保留 v1，新建设计裁决账、候选认知宪法、双分母和 freeze-capable v2 | 保留 challenger 与历史；旧功能对等和 V2 新能力分别可审计 | 前期需要两套 exact set 和交互 oracle | **WORKING APPROACH / NOT ADJUDICATED** |
| C. 先创建新仓库和代码骨架，再反推分母 | 很快看到代码 | owner、能力与验收被实现反向绑架；最容易漏功能 | 本次 bootstrap 不选 / REOPENABLE |

方案 B 当前在证据保留、可推翻性和防止假冻结方面最强，因此作为 working approach；
它不是因为与既有设计一致而正确，后续若 challenger 证明其双分母、artifact family
或 gate 结构有更优替代，可以通过 typed design adjudication 改写。本文只完成规划和
候选设计资产，不实现生成器。

## 四、完整后继系统的边界

当前边界假设认为 V2 不是只有认知算法的窄内核，并把完整后继暂分为三个同等必要的
平面。三平面、名称和边界都需要在能力分母和 owner inventory 后裁决：

1. **Cognitive Runtime**
   - User Context Model；
   - Epistemic World Model；
   - Evidence Admission 与 Personalized Presentation；
   - Typed Cognitive Workspace；
   - Epistemic Expansion 与 Epistemic Control；
   - decision、prediction、outcome、correction 和 learning control。
2. **Capability Continuity Envelope**
   - Capture、Raw、distill、Wiki、KG、search、recap、scheduler、time capsule、MCP、
     Agent integration、projection 和 reports；
   - legacy surface Adapter、数据迁移、reverse mapping 和 rollback envelope。
3. **Platform / Ops / Governance**
   - daemon、installation、configuration、schema owner、backup/restore、health、
     hermetic tests、security/audit、release gates 和 independent verifier。

在此候选中，Ops 能力不塞进 Cognitive Runtime，但仍进入 successor system
denominator；这表达关注点分离，不预先批准三平面的最终 Module topology。

## 五、当前 Mnemos 已经提供的高杠杆底座

本设计不把 Mnemos 贬低为简单记忆库。当前代码已经具有可选择性复用的深语义：

- `core/evidence/source_authority.py:39-55` 已区分七类来源，并严格限制用户模型型
  cognitive update 的高权来源；
- `core/cognitive/user_model_assets.py:1-7` 已禁止把知识缺口、用户盲区和交互偏好
  混为同一身份；
- `core/cognitive/state_contract_schema.py:38-55` 已有 episode、belief、snapshot、
  decision、prediction、calibration、reaction、outcome、correction 和 training 对象；
- `core/cognitive/decision_trace_store.py:56-73` 已能原子封存 value、state、decision
  和 action command；
- feedback/training 合同已把用户 reaction 与 objective outcome 分开；
- revision/head/CAS、outbox/receipt、target journal、projection 和 rollback 原语是
  successor Commit Fabric 与 Action Broker 的高价值复用候选。

当前复用假设是保留经证明有效的原语，另为“任务中如何形成判断”建立清晰 owner 和
稳定 Interface；是否复用、重新实现或替换仍逐项通过能力和架构裁决。

## 六、当前实现证据暴露的候选认知结构缺口

本节把代码事实与 successor 机制假设分开：文件和行为描述是当前实现证据；“应当
拆分”“需要新增 owner”以及具体 Seam 都只是待裁决架构主张。

### 6.1 同一个 `allows_cognitive_update` 不能同时承担两种 authority

`core/evidence/source_authority.py:49-55,296-298` 当前只允许 system policy、explicit
user 和 project contract 主动更新 cognitive state。该规则目前旨在保护 Persona、
Policy 和用户目标，也说明现有 authority 语义主要是 user-model admission。

当前待裁决方案是拆成正交的 target-typed admission：

- **User-model authority**：谁可以声明或修订用户目标、价值、偏好、约束和授权；
- **World-evidence admission**：哪些 external/tool/quoted/assistant-derived 材料可以
  支持、反驳或使一个 world claim 保持 unknown；
- **Action authorization**：谁可以批准有实质副作用的行动。

外部证据可以进入 Epistemic World Model，但不能因此升级成用户意图、Persona、
Policy 或行动许可。

### 6.2 DecisionTrace 是提交 Seam，不是认知回合 owner

`DecisionTraceStore.seal()` 接收调用方已组织好的 candidates、evidence、selection、
expected outcome 和 action。它能可靠封存结果，但不负责生成候选、补证、找反证、
比较来源依赖、决定继续搜索或停止。

当前候选引入 Typed Cognitive Workspace 保存有界 round state、预算、冲突、unknown、
no-progress fingerprint 和 typed stopping disposition，并让 DecisionTrace 只作为
提交 Seam。它需要通过 Decision Map #9 原型与其他 owner 方案比较。

### 6.3 个性化排序仍可能改变最终证据可见性

`core/app/context_search.py:383-409` 把 persona score 加入最终分数，
`core/app/context_search.py:488-500` 再按该分数截取 top-k。现有 rank-effect receipt
值得保留，但正确执行该合同仍可能让 material counterevidence 退出窗口。

当前候选让 User Context 先帮助绑定 task、goal、risk 和 cognitive-load contract；
随后要求 Evidence Admission 的 material evidence、counterevidence、alternative 和
unknown floor 不因仅 presentation variant 改变。个性化只影响表达、解释顺序、深度、
时机和打扰程度。该不变量需要 Decision Map #8 的反例原型证明。

### 6.4 已有求证、盲区和校准原语尚未形成主动扩展回路

`core/cognitive/verification_queue.py:123-128` 明确只计划和保存 verification proposal，
不执行求证。隐藏关系、知识缺口、压力测试、外部搜索和 independent lineage cluster
是有价值的 contributor，但没有一个 Module 对下列完整回路负责：

~~~text
task claims
→ evidence search
→ counterevidence and alternatives
→ source dependency collapse
→ unknown frontier
→ expected information gain / budget
→ seek, experiment, abstain, escalate or commit
~~~

### 6.5 现有预测合同不能代替通用认知预测

`core/cognitive/state_contract.py:688-711,761` 和
`core/cognitive/prediction_ledger.py:85-86` 将当前正式 prediction 限定为
`predictive_push / predictive_delivery_usefulness`。若 legacy parity 候选成立，该
能力需要保持兼容；另一个待裁决主张是版本化支持 claim forecast、decision outcome、
plan milestone、experiment result 和 tool/action effect。现有窄合同本身不能作为
通用预测已经存在的证据。

## 七、双分母模型

本节是当前 working model，不因延续旧 parity 方向或出现在本文中而自动成立。它必须
经过 §1.4 的共同标准和 challenger 裁决；若单分母、多轴 obligation graph 或其他
结构能以更低复杂度提供同等或更强证明，应替换本节方案。

### 7.1 Legacy Parity Denominator（LPD）

LPD 是 frozen legacy snapshot 上全部有效 atomic **capability intent** 的 exact set；功能身份与
当前实现是否健康分开。每个记录继承既有 capability 字段，并新增至少：

| 字段 | 含义 |
| --- | --- |
| `legacy_capability_id` | 稳定 ID；不以路径或命令名作为身份 |
| `capability_intent_evidence_refs` | 从入口、合同、代码、配置、schema、state/effect、测试、文档、历史与运维流程重建的 intended behavior |
| `legacy_realization_health_at_snapshot` | working、partial、broken、unreachable、unknown 或 superseded；不决定功能是否进入分母 |
| `legacy_defect_refs` | 当前实现缺陷、未接通链路和反例；用于修复或 successor negative oracle |
| `adjudication_disposition` | `PRESERVE_EQUIVALENT`、`PRESERVE_VIA_ADAPTER`、`REPLACE_STRONGER_CONTRACT` 或 `REJECT_INVALID_LEGACY_BEHAVIOR` |
| `constitution_clause_refs` | replacement/rejection 的精确宪法依据 |
| `successor_owner_ref` | 唯一 Module owner；Adapter 不能成为 owner |
| `interface_ref` | successor 稳定 Interface 与 facet |
| `legacy_oracle_refs` | 被证明正确的旧行为 oracle |
| `legacy_contract_evidence_refs` | 旧实现 broken/partial 时的合同证据；经裁决后才可成为 successor behavior oracle |
| `state_effect_oracle_refs` | identity、revision、receipt、target effect 与守恒 |
| `failure_performance_oracle_refs` | crash/restart、retry/uncertain、SLO/RTO/RPO |
| `migration_reverse_refs` | importer、reverse adapter、rollback envelope |
| `contract_status` | candidate、verified、approved、superseded；只描述分母合同本身 |

在此候选中，前三种 disposition 计入切换 parity set。第四种不是“删功能”捷径：
它需要 DesignAdjudicationRecord 比较保留、替换和拒绝，绑定 negative oracle，证明
背后的有效用户结果仍被保留，并经独立 challenge；用户 exact-byte authorization
只激活裁决结果，不替代这些正确性证据。

因此 Mnemos 当前存在大量 bug 并不使其背后的功能退出 LPD，也不要求 successor 原样复刻
错误输出。已证明正确的 legacy scenario 才能直接充当行为 oracle；broken/partial 链路必须由
capability intent、数据与 effect 守恒、不变量、仍有效的合同和独立 acceptance test 共同重建
expected behavior。已知 bug 本身进入 negative oracle；它所暴露的未完成 capability intent 仍
进入功能考古和分母候选。只有经过独立裁决证明“行为本身无效”，而非“实现没修好”，才可用
`REJECT_INVALID_LEGACY_BEHAVIOR`。

`implemented`、`equivalence_verified` 等实现进度不得写回已批准的 LPD record；否则
每迁移一项都会改变 denominator bytes 并使批准失效。实现和验证结果进入 detached
`RealizationEvidenceRegistry`，由 cutover registry 比较其 exact verified set 与批准
的 immutable denominator set。

### 7.2 Cognitive Adequacy Denominator（CAD）

在双分母候选中，CAD 是 V2 作为认知系统拟新增或系统化的 obligation exact set。
它不从旧 surface 数量推导，每项由场景、风险和可证伪结果定义。候选记录至少包含：

| 字段 | 含义 |
| --- | --- |
| `adequacy_id` | 与 legacy capability namespace 分离的稳定 ID |
| `human_limit_addressed` | 有限信息、注意、工作记忆、偏差、校准或行动约束 |
| `task_scope_contract` | goal、risk、time、privacy、budget 和 materiality |
| `canonical_owner_ref` | 唯一认知 Module owner |
| `workspace_state_contract` | round state、evidence、alternatives、unknown、budget、progress |
| `evidence_admission_contract` | provenance、independence、freshness、counterevidence 和 source dependency |
| `control_disposition_contract` | accept/revise/seek/experiment/abstain/escalate |
| `agency_and_load_contract` | approval、reversibility、challenge policy 和 cognitive load |
| `correction_contract` | error class、external verification、supersession、recurrence |
| `complementarity_oracle_refs` | human-only、system-only、joint 与 appropriate-reliance 对照 |
| `failure_oracle_refs` | echo chamber、false balance、correlated sources、fluent unknown、manipulation |
| `contract_status` | candidate、verified、approved、superseded；只描述 obligation 合同 |

CAD 的初始 family seed 仅用于发现，不能作为冻结分母或完成率：

1. user-context/world-model separation；
2. target-typed authority 与 world-evidence admission；
3. evidence admission / personalized presentation separation；
4. active epistemic expansion 与 source-dependency collapse；
5. typed cognitive workspace 与 bounded rounds；
6. epistemic control、停止、求证、实验、弃权和升级；
7. generalized prediction、objective outcome 与 error attribution；
8. externally verified correction 与 recurrence control；
9. episodic/semantic/procedural/prospective/working memory lifecycle；
10. user agency、challenge/cognitive-load governor 和 anti-manipulation；
11. complementarity gain、appropriate reliance 和 overreliance evaluation。

这些 family 后续可能拆分；任何 seed 数都不能被报告为 CAD 百分比。
CAD 的 implemented/validated evidence 同样只进入 detached evidence registry，不
修改已批准的 obligation bytes。

### 7.3 两个分母的关系

- 在双分母 working model 下，LPD 与 CAD 使用不同 ID namespace、不同批准 receipt 和不同 set root；
- 同一实现 Module 可以为两个分母提供证据，但不能合并或删除任一 obligation；
- LPD 证明“旧能力一个不少”，CAD 证明“不是只重写出一个 bug-free Mnemos”；
- joint interaction oracle 证明新认知控制没有破坏 legacy 功能、隐私、性能或用户
  主权；
- 任一分母未冻结、未批准或未 100% 验证，cutover 都为 false。

概念性硬门为：

~~~text
v2_cutover_eligible =
    base_cutover_eligibility_registry
    AND legacy_parity_denominator_frozen
    AND legacy_parity_exact_approval_valid
    AND legacy_equivalence_verified_set == approved_legacy_parity_set
    AND cognitive_adequacy_denominator_frozen
    AND cognitive_adequacy_exact_approval_valid
    AND cognitive_adequacy_verified_set == approved_cognitive_adequacy_set
    AND joint_interaction_oracle_gap == 0
    AND complementarity_required_cohort_gap == 0
    AND manipulation_or_overreliance_blocking_gap == 0
~~~

这只是设计 predicate，不能在 D0.1 中修改现行 machine registry。精确字段、receipt
schema 和 freshness 规则由 Decision Map 后续票裁决。

### 7.4 freeze-capable v2 artifact family 候选

D0.1 当前保持 v1 五本账 bytes 不动作为 challenger。freeze-capable v2 的候选 artifact
family 至少独立保存：

~~~text
requirements.v2.jsonl
surfaces.v2.jsonl
legacy_capabilities.v2.jsonl
cognitive_obligations.v2.jsonl
successor_realizations.v2.jsonl
oracle_specs.v2.jsonl
coverage_edges.v2.jsonl
canonical_owner_registry.v2.json
effect_target_registry.v2.json
manifest.v2.json
~~~

当前激活候选是：两个 denominator 可分别审阅，由一个 `DualDenominatorGeneration`
以 CAS 成对激活；替代方案仍由 Decision Map #2/#13 比较。候选 hash 链保持单向：
source/constitution/config/adjudication → denominator books → manifest → independent
verification report/receipt → exact user approval → activation。manifest 不反向引用
verifier 或 approval；design 不反向嵌入 manifest hash。

## 八、39 个 legacy capability seed 的承接假设

下表只是 cluster hypothesis，不是逐项 adjudication，也不清零当前 39 个
`ADJUDICATION_REQUIRED`：

| Legacy seed cluster | Successor owner / Adapter hypothesis | 初始 disposition hypothesis |
| --- | --- | --- |
| capture、sync、artifact、MCP capture | Capture + PayloadVault + ingress Adapter | preserve / adapter |
| distill、Wiki quality、consolidation | Knowledge Formation + Semantic Memory + Projection Lifecycle | preserve / stronger contract |
| Raw/Wiki/context search、QA | Query + Evidence Admission | preserve；truth-relevant admission 不受 presentation 改写 |
| KG、hidden relation、blindspot、verification | Epistemic World Model + Epistemic Expansion contributors | preserve and deepen |
| Observation、Reflection、Persona、trajectory | User Context Model + Metacognitive Assessment | preserve；重新定位 owner |
| Policy、Trust、Delivery、predictive context | Procedural Policy + Presentation + Action Broker | preserve；移除 truth coupling |
| Recap、reminder、time capsule、scheduler | Prospective Memory + Workflow + Action Broker | preserve / adapter |
| Decision、Prediction、Scoring、Feedback、Training | Workspace Seam + Epistemic Control + Learning Control | reuse and version |
| Agent kit 与 MCP KIA | bounded Contributor / ingress Adapter | preserve；agent count 不等于证据独立 |
| shadow、reports、Wiki/KG/ANN/metrics | Projection Lifecycle + read models | preserve and rebuildable where proven |
| install、daemon、health、migration、backup、gates、audit | Platform / Ops / Governance | full parity outside Cognitive Runtime |

当前 owner 假设要求任何真实 capability 即使跨多个 cluster，也只在 LPD record 中有
一个 canonical owner；Adapter 和 projection 作为 surface/effect。该唯一 owner 规则
本身仍由 Decision Map #6 与 state/effect ownership evidence 裁决。

## 九、D0.1 交付物与停止线

本轮 bootstrap 只交付：

1. 本设计；
2. `design_adjudication_method.candidate.json`；
3. `constitution.candidate.json` 与 `constitution.candidate.schema.json`；
4. `2026-08-01-cognitive-successor-v2-decision-map.md`；
5. `CONTEXT.md` 中已澄清的领域语言；
6. 文档资产分类与静态校验结果。

Decision Map #1 随后的增量资产另增加：本票 lifecycle owner
`design_adjudication.schema.json`、protocol-owned
`method_bootstrap_protocol.candidate.json`、`method_evidence_manifest.candidate.json`、
五候选 `method_candidate_denominator.candidate.json`、non-terminal producer
assessment `method_adjudication_record.candidate.json` 和独立 written spec。它们不回写
D0 v1，且在同包 prototype、challenger、detached final decision candidate、typed dependency
bundle、post-final verifier 和 exact-byte authorization 完成前仍是 non-governing candidate。
authorization 之后还必须有 detached acceptance-gate receipt；二者任一缺失都不能激活方法。

D0.1 bootstrap 完成不表示：

- constitution 已 exact-byte 批准；
- LPD/CAD 已冻结或完成；
- D0 v2 schema、generator 或 verifier 已实现；
- 39 个 seed 已完成承接裁决；
- D1 TransactionGraph 可以开始；
- 新仓库、产品名、物理 topology、迁移、生产或 cutover 已获批准。

用户已经批准继续深化 Decision Map #1，并明确了“功能完整、性能与体验较优，再追求
简洁优雅”的价值方向；这不是对当前方法或 JSON exact bytes 的 `ADOPT`。仍需审阅该票
最终 written spec 与 exact bytes。审阅只表示允许同包 prototype、独立 challenge、finalizer、
bundle、verifier、exact authorization 与 post-authorization acceptance 继续，不等于把当前
clause、denominator 或实现冻结，也不在本票内同时解决后续票。

## 十、自审清单

- [x] 保留既有 V2/successor 原稿，不覆盖其 bytes；
- [x] 区分 authoring HEAD 与 D0 legacy snapshot；
- [x] 没有把旧合同、旧 V2、reviewer 或此前用户方向当成不可挑战的设计权威；
- [x] 区分 design evidence、当前 working recommendation 与 exact-byte machine approval；
- [x] 裁决方法独立保存并按 exact bytes 绑定，不由条款集自证；
- [x] proposal ledger 与 adopted clause set 分离，拒绝候选不会丢历史或永久阻塞激活；
- [x] 把 LPD/CAD 和三平面明确标成 working hypotheses，不用 39 个 seed 冒充分母；
- [x] 未把外部证据授权升级为用户意图或行动许可；
- [x] 未把 Persona、用户 reaction、LLM confidence 或多 agent 数量当作世界真值；
- [x] 未批准实现、生产写、双 writer、迁移或 cutover。
