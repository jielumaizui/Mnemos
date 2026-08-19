# 认知后继：设计裁决生命周期与质量优先级

## 0. 文档状态与边界

**状态：WRITTEN-SPEC / REVIEW-READY / NON-GOVERNING / DESIGN-ONLY。**

本文是 Decision Map frontier #1 的书面设计，只定义“设计阶段如何判断哪一项主张更可靠”
以及最终实现的质量优先级。它不批准任何 constitution clause，不批准新系统实现，也不
授权旧 Mnemos 的 daemon、真实 API、生产数据、迁移、replay 或 cutover 操作。

旧设计不被覆盖：

- 已保留的 V2 参考设计
  `docs/superpowers/specs/2026-08-01-cognitive-successor-capability-atomicity-design.md`
  的冻结 SHA-256 为
  `6e2fbcbbccf02b9c7b5fbc7bba484b5ea8f7cc8022ac734f381f26af2ec82484`；
- 本文是在该参考设计之上的裁决层候选，不把旧设计改写成“已经错误”，也不把本文改写
  成“已经正确”；
- 当前阶段只形成 producer assessment。独立 challenge、detached finalizer 形成的非治理
  final-decision candidate、typed dependency bundle、post-final structural verifier、用户对
  exact bytes 的授权与其后的 detached authorization-acceptance gate 均尚未完成，因此本文和
  配套候选工件不能激活为治理规则。

用户在设计阶段给出的原始方向包括：

> 现阶段，不是谁必须遵守谁，应该是谁是对的，遵守谁

> 我刚才说的对错说针对现阶段整个系统处于设计阶段，不一定非得遵守之前确定的宪法或者合同，而应该是综合考虑，最终谁是对的就按照谁说的来，懂了吗？

> 同意，最终的代码要以功能完整，性能与体验较优的前提下，代码更简介，更优雅

> 有bug的功能不一定是不要的，可能在做mnemos的时候留下的，只是没修好，所以现在要做的是查清楚mnemos到底有哪些功能，这些功能是怎样的，至于后续的拼接，应该是参考现有的mnemos系统然后看看是否有更简洁更优雅的方案

据此，本文不预设旧宪法、旧合同、旧版本、作者、reviewer、agent 或时间顺序必然优先；
综合判断后，哪项主张在其自身类型下证据更充分，就采用哪项。最终代码以功能完整为
前提，性能与体验要较优，在这些条件成立后再追求简洁与优雅。本文保留原字节中的“简介”，
但按上下文解释为“简洁”；该解释仍须用户在最终 exact-byte authorization 时确认。
功能完整的对象是重建并批准后的 capability intent；legacy realization 有 bug 只改变其 oracle
强度和修复/重建策略，不自动删除该功能。现有 Mnemos 既是能力考古输入，也是后续拼接方案
的 baseline reference，但不是必须照搬的物理架构。

## 1. 目标、非目标与基本不变量

本文要解决四个问题：

1. 设计阶段的“谁对听谁的”如何变成可复查的 typed-standing 规则；
2. 候选方法如何冻结完整、最强公平且可反驳的 exact denominator；
3. producer、challenger、finalizer、dependency bundle、pre-authorization verifier、用户授权与
   post-authorization acceptance gate 如何形成无环生命周期；
4. 功能、性能、体验、简洁与优雅如何形成不可相互补偿的实现优先级。

本文不解决：

- 各认知能力的最终模块划分；
- Legacy Parity Denominator（LPD）和 Cognitive Adequacy Denominator（CAD）的具体条目；
- 生产基准值、SLO 数值、迁移计划与切换时间；
- 任何旧 Mnemos 缺陷是否已经修复；
- 任何候选是否已经被最终 `ADOPT`、独立验证或用户授权。

全生命周期保持以下不变量：

- 身份、资历、票数、时间先后和文档名称不能把经验主张变成真；
- 用户决定产品价值与是否授权 exact action，但用户偏好不能改写观测事实；
- 安全、隐私、主体性、功能完整与数据守恒的硬失败不能被性能、体验或简洁性补偿；
- `UNKNOWN` 与 `INCOMPARABLE` 必须保留，不能转成零分、平均分或默认通过；
- `REVISE` 终止当前 bytes，修订后必须以新 bytes 重新进入生命周期；
- producer assessment 不得自我升级为 final decision 或 activation authorization。
- 采用裁决方法不等于采用该方法引用的质量条款；V2-CONST-015、016、017 必须分别裁决、
  验证和授权。

## 2. “谁对听谁的”：Typed Standing

“谁对听谁的”不是选择一个永久最高权威，而是先给主张分型，再把它交给具备合法
standing 的证据与 owner。不同类型禁止互相越权：

| Claim kind | 合法 standing | 必需证明 | 不得替代它的内容 |
| --- | --- | --- | --- |
| `EMPIRICAL_CURRENT_STATE` | `EMPIRICAL_EVIDENCE` | 直接工件、来源链、可重复观测、独立 oracle | 用户偏好、reviewer 身份、模型票数 |
| `EMPIRICAL_CAUSAL` | `EMPIRICAL_EVIDENCE` | 机制、替代解释、反证、适用边界 | 单篇论文标题或未经限定的类比 |
| `PREDICTIVE` | `EMPIRICAL_EVIDENCE` | 预先冻结的指标、基线、时间窗、失败阈值与校准 | 事后挑选样本 |
| `PRODUCT_VALUE` | `PRODUCT_VALUE_OWNER` | 用户或明确产品 owner 对后果知情后的选择 | 实验自动推出“应该追求什么” |
| `SAFETY_PRIVACY_AGENCY` | `SAFETY_CONSTRAINT` | owner、source、scope、version、失效条件与最坏后果 | 性能、体验、进度或多数票 |
| `ARCHITECTURE_ENGINEERING` | `ARCHITECTURE_VALIDATION` | Interface、不变量、原型、基准、故障注入、可证伪预测 | “更新”“先进”或图更整齐 |
| `CAPABILITY_EQUIVALENCE` | `MIGRATION_CONSERVATION` | 同输入下的输出、状态、effect、失败语义与守恒 oracle | 文件数、命令数或测试数相近 |
| `MIGRATION_CUTOVER` | `MIGRATION_CONSERVATION` | writer authority、守恒、兼容、回滚、并发与 crash/restart 演练 | 功能清单完成本身 |
| `OPERATION_AUTHORIZATION` | `OPERATION_AUTHORIZATION` | 已认证主体对 exact bytes、动作、范围、期限和回滚条件的授权 | 设计胜出、测试通过或历史概括同意 |

一个复合主张必须拆成原子 claim kinds 分别裁决。例如“该架构更简洁且应该上线”至少拆成
架构机制、经验性能、产品价值与操作授权四项；任一项的 standing 不能代理其余三项。

## 3. 五候选 Exact Denominator

方法选择的候选分母必须在 producer 评估前冻结，并由独立的
`MethodCandidateDenominator` 保存 exact bytes。人类可读的五个候选如下；机器裁决只能
使用 denominator 中绑定的完整陈述、流程、优势、局限、支持证据、反驳证据和
falsification conditions，不能只使用本表摘要。

| Candidate ID | Strongest-fair 版本 | 合法优势 | 不能承担的角色 |
| --- | --- | --- | --- |
| `STATUS_QUO_INFORMED_HOLISTIC_JUDGMENT` | 有经验的设计者综合上下文直接判断，保留讨论自由度，不引入完整机器流程 | 低成本、适合低风险和早期探索、能快速暴露直觉假设 | 不能证明分母完整、独立性、可重放性或阻止无意补偿 |
| `AUTHORITY_HIERARCHY` | 按用户、产品 owner、领域专家、正式合同和先例的明确层级解决冲突 | 能确定价值 owner、责任边界与操作授权，决策速度快 | 不能因为来源身份而证明事实、因果、架构或性能正确 |
| `MULTI_CRITERIA_SCALAR_AGGREGATION` | 冻结指标、尺度与权重，以 MCDA、效用函数和敏感性分析汇总可补偿取舍 | 能表达可补偿偏好并呈现权重敏感性 | 不能用总分抵消硬失败、未知、不可比或证据缺口 |
| `DELIBERATIVE_COMMITTEE` | 多领域且依赖透明的成员审阅同一证据，寻求有理由的共识并保留异议 | 能汇集分散经验、发现场景与盲点并形成实施承诺 | 票数、地位或表面共识不能证明事实，也不能压掉少数安全 finding |
| `CONSTRAINT_FIRST_TYPED_EVIDENCE` | 按 claim kind 分配 standing；先过不可补偿约束，再对可行候选做证据、Pareto 与反证比较 | 同时保留事实、价值、安全、未知和授权边界，可组合 MCDA 与 falsification | 流程成本更高；若 protocol、denominator 或独立性未冻结，也会退化为形式化自证 |

分母规则：

- 五项一项不少；`status quo` 也是候选，不能因其未形式化而从分母消失；
- 每项必须使用 strongest-fair bytes，包含其最好适用场景，不得以稻草人描述淘汰；
- 每项必须同时绑定 support、refute、limits 与 falsification conditions；
- producer 可以提出第六项，但必须先生成新 denominator generation，旧评估立即 stale；
- denominator 未冻结或无法证明覆盖当前已知方法空间时，只能声明 bounded scope，不得声称
  “穷尽所有可能方法”。

## 4. Protocol-Owned Constraints：候选不能自己出题

`MethodBootstrapProtocol` 是本票方法选择硬约束的唯一 owner。每个约束必须在查看评估
结果前冻结为完整对象，而不只是一个 ID：

```text
constraint_id (the keyed catalog identity)
statement
owner_ref
scope
source_refs
invalidation_conditions
failure_semantics
version
```

方法候选只能引用适用的 `constraint_id`，不能新增、改写、降级或删除用来评价自己的
约束。`scope` 必须明确约束适用的 claim/risk 边界；producer assessment 也只能记录
protocol-owned 约束的 `PASS / FAIL / BLOCKING_UNKNOWN / NOT_APPLICABLE` 和证据，不能
重新定义考试。

最低 protocol 约束包括：

- exact question、scope、五候选 denominator、claim-kind standing map 与证据 freshness
  在评估前冻结；
- 五候选均有 strongest-fair exact bytes 和正反证据；
- hard `FAIL` 与 blocking `UNKNOWN` 不可被总分、票数、性能或简洁性补偿；
- producer、challenger、finalizer 与 verifier 的角色、身份和物质依赖可验证分离；
- 任何候选不得使用 producer self-oracle 或循环 hash/receipt 自证；
- `REVISE`、`REJECT`、`RESEARCH_REQUIRED`、`PROTOTYPE_REQUIRED` 与
  `USER_VALUE_CHOICE_REQUIRED` 均不得进入授权；
- activation 必须最后绑定 final decision、typed dependency bundle、post-final verifier 与
  用户 exact-byte authorization；
- schema 通过只证明结构有效，不证明经验事实、独立性或最终正确。

产品实现的“功能完整、性能体验较优、代码简洁优雅”属于待裁决的产品价值与工程质量
条款，不得被候选方法偷偷加入为“选择该方法本身”的自利硬题。

## 5. 冻结输入与裁决算法

### 5.1 冻结输入

producer 开始评估前必须冻结：

- protocol、evidence manifest、五候选 denominator 与被评估 method bytes 的 exact SHA-256；
- exact question、scope、materiality 和 risk profile；
- claim kinds、standing、hard constraints、tradeoff registry 与 freshness policy；
- evidence dependency clusters，防止同源论文、数据、模型或摘要重复计数；
- 如涉及实现质量：LPD/CAD、exact material metric denominator、scenario/cohort coverage、
  typed baseline registry、candidate root、运行环境、故障计划、SLO bounds、non-inferiority、
  equivalence 与 meaningful-improvement margins、effect confidence-interval derivation、
  complexity fact denominator、required Seam registry、change scenarios 与阈值 profile。

任一冻结输入变化都产生新 generation；不得回写旧记录或复用旧授权。

### 5.2 先过硬门，再比较可行集

1. 对每个候选逐项执行 protocol-owned constraints；
2. `FAIL` 或 blocking `UNKNOWN` 将候选移出可行集，但原始 tradeoff 向量仍保留；
3. 对可行候选做 ordinal/Pareto 比较，不先制造单一总分；
4. 对 non-dominated 候选执行反证、故障注入和敏感性分析；
5. 经验或机制证据不足时输出 `RESEARCH_REQUIRED` 或 `PROTOTYPE_REQUIRED`；
6. 只剩知情后的真实价值取舍时输出 `USER_VALUE_CHOICE_REQUIRED`；
7. 多个候选在当前证据下等效时输出 `EQUIVALENT_SET`；
8. producer 只输出 recommendation，不能输出具有治理效力的 final decision。

A 支配 B 当且仅当：A 在所有适用 material tradeoff 上不劣于 B，至少一项有证据证明
更优，且不存在会翻转结论的 `UNKNOWN` 或 `INCOMPARABLE`。MCDA 只能在已过硬门的可行集
内做敏感性分析；reviewer 数量不能消除一个尚未解决的 blocking finding。

## 6. 无环 Artifact DAG 与状态语义

Ticket #1 的依赖方向固定如下：

```text
TicketSchema ───────────────────────────────┐
                                           │
MethodEvidenceManifest ───────────────> MethodCandidateDenominator
          │                                         │
          └──────────────────────┬──────────────────┘
                                 v
                    MethodBootstrapProtocol

MethodEvidenceManifest ───────────────┐
MethodCandidateDenominator ───────────┼──> MethodCandidate
MethodBootstrapProtocol ──────────────┘

MethodEvidenceManifest ───────────────┐
MethodCandidateDenominator ───────────┤
MethodBootstrapProtocol ──────────────┼──> ProducerAssessment
MethodCandidate ──────────────────────┘             │
                                                     v
                                            DetachedChallenge
                                                     │
                                                     v
                                         FinalDecisionCandidate
                                                     │
                                                     v
                                      TypedDependencyBundle
                                                     │
                                                     v
                                      PostFinalStructuralVerifier
                                                     │
                                                     v
                                          ExactUserAuthorization
                                                     │
                                                     v
                             PostAuthorizationAcceptanceGateReceipt
```

`TicketSchema` 的 exact bytes 也是 protocol、evidence、denominator、method、assessment、
challenge、final、bundle、verifier、authorization 与 post-authorization acceptance-gate
receipt 的显式依赖；上图只省略其重复箭头。
historical `supersedes` 使用 commit、path、blob ID 与 raw SHA-256 定位旧 revision，只形成
lineage edge，不进入当前 artifact DAG。

规则：

- 前置 artifact 绝不引用后置 artifact 的 hash、状态或 receipt；
- 每个后置 artifact 绑定所有直接前置 artifact 的 exact hash；
- `ProducerAssessment` 只能记录 producer 的证据与 recommendation；
- `DetachedChallenge` 必须从冻结原始输入重建 hard-constraint 与 non-dominated set，不能
  复用 producer 的“valid”字段；
- `FinalDecisionCandidate` 由与 producer、challenger 分离的 finalizer 综合 producer、
  challenge 与仍然显式的未知，输出
  `ADOPT / REVISE / REJECT / RESEARCH_REQUIRED / PROTOTYPE_REQUIRED /
  USER_VALUE_CHOICE_REQUIRED / EQUIVALENT_SET`，但它在验证与授权前始终 non-governing；
- `TypedDependencyBundle` 必须精确列出 schema、evidence、denominator、protocol、method、
  assessment、challenge、final、verifier implementation、authorization acceptance-gate
  implementation 与 finalizer identity evidence 共 11 类 bytes，缺失、额外或角色错误均失败；
- `PostFinalStructuralVerifier` 绑定 final 与 bundle，复算结构、hash、集合、角色/类型/key
  相等、状态转换、身份分离和 structural mutation fixtures，不宣布经验或规范主张为真，
  也不验证尚未产生的授权日历窗口；
- 只有 `FinalDecisionCandidate=ADOPT`、challenger blocking finding 为零且 verifier
  `ELIGIBLE` 时，才可请求
  用户对同一 dependency root 的 exact-byte authorization；
- 用户授权必须绑定已认证 principal、exact root、exact selected method、允许动作、scope、
  valid-from、expires-at、失效条件、恢复边界与撤销语义。概括同意、旧消息或 producer
  recommendation 都不能替代它。

当前生命周期只到 `ProducerAssessment`。任何当前工件中的 recommendation 都是 producer
观点，不是 `ADOPT`、不是 verified、不是 authorized，也不能成为后续条款的 active input。

## 7. Schema Owner 与独立 Verifier 的边界

`docs/acceptance/cognitive_successor_d0_1/design_adjudication.schema.json` 只作为
**Ticket #1 方法裁决生命周期**的单一 JSON Schema owner。它不是全部 constitution clause、
全部认知模块、全部迁移合同或全系统治理的 god schema；只有第二个真实 use case 证明了
稳定 Seam，才考虑提炼可复用核心。

JSON Schema 能证明：

- artifact type、必需字段、枚举、格式、互斥分支和禁止额外字段；
- 局部条件关系，例如 `PASS` 对应零个已声明 blocking finding；
- hash 字符串、ID 集合和显式状态的结构形状。

JSON Schema 不能单独证明：

- 文件实际 SHA-256 与声明值相等；
- denominator 的 strongest-fair 语义完整；
- evidence 是否真实、充分、独立、新鲜或来自正确 scope；
- producer、challenger、finalizer、verifier 是否由独立主体、独立角色和足够独立的实现产生；
- finding count 是否与实际重算结果相等；
- 性能、体验、功能守恒或架构机制是否真的成立；
- 用户 principal 是否真实认证并有权授权。

因此 post-final detached structural verifier 必须绑定 final record 与 typed dependency bundle，以
独立实现重算 pre-authorization exact hashes、DAG、角色/类型/key 相等、集合相等、约束执行、
quality arithmetic、状态转换、身份/依赖独立性和 structural mutation fixtures。它至少要捕获
duplicate raw members、stale 或遗漏 schema/bundle hash、漏候选、漏或非对称 tradeoff axis、
稻草人候选、漏反证、hard `UNKNOWN`、inadmissible candidate 进入 feasible set、self-oracle、
共同上游伪独立、scalar compensation、未知值数值化、跨方法或非相邻 typed supersedes、
质量公式或分母漂移，以及 final/bundle/method identity 不一致。它发生在授权之前，因此不得
声称验证未来授权的 principal、文本、时间窗或 scope。

只有 structural verifier 为 `ELIGIBLE` 才可产生 exact user authorization；授权本身仍是
`PENDING_DETACHED_AUTHORIZATION_ACCEPTANCE_GATE`。随后由 bundle 预先绑定实现的 detached
acceptance gate 使用 duplicate-member-aware UTF-8 raw loader，重算授权全文及内部
`authorization_text.exact_text_sha256`、method/final/challenger/verifier/bundle dependency root、
principal authentication 与 authority scope、allowed action、prohibitions、recovery boundary 和
RFC 3339 calendar window。gate 还必须证明 receipt `valid_until <= authorization.expires_at`，并
要求每次使用同时复核 authorization 与 receipt 两个时间窗。只有该 receipt 为
`ACTIVATION_ELIGIBLE` 时，bound method bytes 才在批准的 design-method scope 内可用；这不激活
任何 constitution clause，也不授权实现、生产写入、迁移、replay、真实 API 或 cutover。

## 8. 实现质量：Quality-Preserving Simplicity

质量比较是非补偿式、词典序的三层规则。后层永远不能补偿前层失败。

### 8.1 Tier 1：Exact Completeness 与安全硬门

实现候选必须同时满足：

- 当前 approved development slice 的 LPD/CAD 分母 exact gap 为 0；
- 最终 cutover 时，全量 LPD 和 CAD 的 exact gap 均为 0，即切换前功能分母 **100%**；
- capability intent 与 current realization health 分开建模；一个入口当前 broken、partial、
  unreachable 或 unknown，不足以证明其对应功能不应进入 LPD；
- 输出、状态、effect、失败语义、配置 facets、并发、重启、恢复、迁移、诊断、审计与运维
  能力没有被遗漏；
- 安全、隐私、用户主体性、数据守恒、writer authority 与回滚硬门全部通过；
- 不通过删功能、合并能力 ID、永久延期、弱化 oracle、改变分母或降低门禁获得简洁。

Tier 1 任一 `FAIL` 或 blocking `UNKNOWN` 都终止该候选；它没有资格进入性能、体验或代码
美学比较。

### 8.2 Tier 2：性能与体验结果

每个 material metric 必须冻结以下合同：

```text
metric_id, domain, approved_metric_set_root, scenario_cohort_coverage_root,
baseline_registry_root, baseline_class, unit, normalized_direction,
scope_root, workload_root, baseline_root, candidate_root, runtime_environment_root,
cold_or_warm_state, fault_schedule_root,
SLO_lower_and_upper_bounds, non_inferiority_margin, equivalence_margin,
meaningful_improvement_delta,
baseline_estimate, candidate_estimate, candidate_uncertainty_interval,
normalized_candidate_minus_baseline_effect, effect_uncertainty_interval,
statistical_method_binding, sample_sizes,
multiple_comparison_policy,
effect_normalization_rule, target_range_distance_rule,
interval_order_rule, margin_order_rule, state_precedence, state_derivation_rule,
derived_state = SUPERIOR | EQUIVALENT | NONINFERIOR | FAIL | UNKNOWN
```

执行指标 key 必须与预先批准的 exact material metric denominator 完全相等；missing、extra、
duplicate 或未覆盖的 scenario/cohort 一律阻断。baseline registry 至少区分 exact legacy
certifying release、human-only、system-only、human-AI joint 和另行证明的 task-specific
baseline。比较必须使用相同 workload coverage、数据规模、硬件/运行时/配置、cold/warm
条件、故障计划、cohort 和统计方法；`SUPERIOR / EQUIVALENT / NONINFERIOR / FAIL / UNKNOWN`
只能由 verifier 按冻结的 effect interval 与 margins 重算，不能由候选作者填写结论。

归一化与状态必须是单值函数：higher-is-better 使用 `candidate - baseline`，lower-is-better
使用 `baseline - candidate`，target-range 使用“baseline 到批准范围的距离减去 candidate 到该
范围的距离”，正值始终表示 candidate 更好。candidate/effect interval 都必须满足
`lower <= upper`；margins 必须满足
`0 <= equivalence <= non-inferiority` 且 `equivalence < meaningful-improvement`。先判 absolute
SLO `FAIL/UNKNOWN`，再依次判 `SUPERIOR`、`EQUIVALENT`、`NONINFERIOR`；只有 effect interval
的 upper bound 严格小于 `-non-inferiority margin` 才是 effect `FAIL`，跨越该边界必须是
`UNKNOWN`，不得把同一 interval 同时标成两个状态。

Tier 2 有两个不同的完成 profile：

1. `SLICE_COMPLETE`：该 slice 的 Tier 1 分母 zero-gap，所有适用且已批准的 slice metrics
   满足 SLO 与 non-inferiority。`NOT_APPLICABLE` 需要独立 scope 证明；slice 不需要证明与它
   无关的 UX 提升，也不得外推全系统更优或 cutover-ready；
2. `FINAL_CUTOVER`：全量 Tier 1 zero-gap，performance 与 user-experience 的 exact
   denominators 都非空且全部执行；每个指标满足 SLO/non-inferiority，两个域各至少一项
   predeclared meaningful improvement，并且没有 material regression 或 blocking unknown。

性能与体验向量只做逐指标和 Pareto 比较，不把 latency、throughput、资源、recovery、任务
成功、有效等待、交互负担、可理解性、纠错成本、可控性、无障碍、novice/expert 差异、
纵向适应、appropriate reliance 与认知负担压成一个“体验分”。

批准的 all-material outcome profile 必须把 exact material set 精确分成非空 performance、
非空 user-experience 与完整 cross-domain guardrail set；后者覆盖 cognitive、safety、privacy、
agency、data、effect 与 recovery。executed keys 和 result keys 必须与三者并集完全相等。
当前这些 profile bindings 仍为 `null` 且 metrics maps 为空，因此本轮没有任何 Tier 2 verdict。

若一项候选在 material 性能或体验结果上明确更优，它在 Tier 2 胜出，简洁性不能翻转
结果。若存在会改变选择的 material `UNKNOWN/INCOMPARABLE`，输出
`PROTOTYPE_REQUIRED`；若差异只剩已知后果下不可公度的产品价值，输出
`USER_VALUE_CHOICE_REQUIRED`。只有落在同一 outcome-equivalence class 的候选才能进入
Tier 3；该等效类必须覆盖所有 material cognitive、safety、privacy、agency、data、effect、
recovery、performance 和 user-experience outcomes。两个候选都过 Tier 1 最低门不等于结果
等效。

不得通过跳过认知轮次、证据检查、持久化、审计、故障路径或错误处理制造性能与体验绿。

### 8.3 Tier 3：可独立测量的复杂度向量

同一 all-material outcome-equivalence class 内，至少比较两个实现；若只有一个实现，必须绑定
独立的 candidate-saturation evidence。每个维度都必须由 typed facts、exact denominator、
单位、阈值 policy、冻结场景和 `UNKNOWN` 语义构成，不制造单一“优雅分”：

- `K_api`：caller 必须学习的公开方法、参数、不变量、调用顺序和错误模式；
- `K_authority`：重复或重叠的 schema/validator/state-machine/transaction/effect owner gap，
  以及每个独立 cohesive concern 的 authority 是否可证明；不能靠一个 god owner 把数字压低；
- `K_path`：完整 success/failure/recovery/restart trace denominator 上跨 Module、进程、
  store、trust 与 effect boundary 的数量；
- `K_change`：冻结 change scenarios 与 cohesive-owner denominator 下触及的 owner、public
  contract、caller、test、config、schema 与 migration；
- `K_lifecycle`：完整 config/state/transition/incident/compatibility denominator 上的状态、
  重试、恢复、runbook、兼容义务与关键依赖；
- `K_abstraction`：required/optional Seam registry 上的 unjustified seam gap、pass-through
  module、unrelated-change coupling、god-interface、expired glue 与 hidden complexity export。

独立 complexity assessor 必须对 baseline 与 candidate 使用相同 code/config/schema/data/
generated-asset/dependency/test/runbook roots、相同静态规则和相同 change scenarios。任何
required test、runbook、Seam、owner、schema、data 或 recovery asset 缺失都记为 `FAIL` 或
`UNKNOWN`，不能因为删除了测量对象而得到更低复杂度。向量使用 Pareto/ordinal 比较；
不可比时输出 `SIMPLICITY_UNRESOLVED` 并继续测量或原型。只有全部 material complexity
facts 等效后，剩余命名、局部控制流与审美才能由知情的产品/工程 owner 选择，不能由 LOC、
文件数或 reviewer 票数假装成事实。局部实现仍必须通过 typed error、重复逻辑、Interface
conformance、可读性/可维护性以及静态、动态、故障与恢复 guardrails。

每个 complexity fact 还必须绑定 approved fact set、denominator membership proof、fact
definition、scenario、baseline/candidate roots、materiality、unit、baseline/candidate values、
normalized improvement、ordered interval、improvement delta、equivalence/regression margins 与
机械派生 state。executed fact keys 和 membership proofs 必须与 approved set 完全相等。每个
local guardrail 必须绑定 approved profile、oracle、完整 workload 和 evidence；只有完整执行且
blocking finding 为零才是 `PASS`，missing/stale/partial/unverified 一律 `UNKNOWN`。当前这些
bindings 仍为 `null` 且 result maps 为空，因此本轮没有任何 Tier 3 simplicity/elegance verdict。

最终 accepted implementation 还必须相对一个 approved、outcome-equivalent implementation
baseline，在至少一项 predeclared material complexity fact 上改进且没有 material complexity
regression，同时全部 Deep Module negative oracles 与 local quality guardrails 通过。否则
保持 `SIMPLICITY_UNRESOLVED`，继续设计或原型；不能把“目前只有一个实现”改写成“已经更优”。

### 8.4 Deep Module 不是 God Object

Deep Module 的目标是用小而稳定的 Interface 隐藏高价值复杂度，并让调用方行为更简单。
出现任一达到冻结阈值的负向 oracle，都必须拆分 owner 或重画 Seam。阈值至少包括：

- `UNRELATED_CHANGE_INTERFACE_MUTATION_COUNT == 0`；
- `INDEPENDENT_AUTHORITY_COLLAPSE_COUNT == 0`；
- `MAX_REPRESENTATIVE_LOCAL_CHANGE_OWNER_CROSSING_RATIO <= 0.5`；
- `UNSEPARABLE_COHESIVE_CONCERN_COUNT == 0`；
- “单一 owner”被误解为单一巨型文件、单一 facade 或所有 schema 集中在一处。

canonical owner 可以组合私有子模块和私有子 schema；Depth 来自隐藏稳定复杂度，而不是
聚拢所有职责。

### 8.5 Seam、Adapter 与兼容胶水

Seam 的成立依据是可证明的变化轴，或 trust/effect/failure boundary，而不是“至少存在
两个 adapter”。即使只有一个当前实现，只要边界隔离了外部 I/O、时间、随机性、权限、
信任、不可逆 effect 或独立 failure domain，也可以有真实 Seam。

每个 Seam 必须进入 exact required-Seam registry，记录 owner、稳定合同、替换或隔离义务、
同合同 fixtures 和故障语义；删除时必须证明有 verified equivalent boundary。多个 adapter
必须执行同一 conformance suite 与共享 contract/fault fixtures，并增加 adapter-specific
external-failure fixtures；两个薄 wrapper 本身不能证明 Seam。
测试与生产调用方应使用同一公开 Interface，测试专用胶水不得成为第二套行为 owner。

兼容胶水只允许作为显式临时资产，并必须携带：

```text
owner, exact_scope, expires_at, remove_when, callers, telemetry
```

过期、无调用、无 telemetry 或已满足 `remove_when` 的胶水必须阻断 release closure，不能
永久藏在“兼容层”名下。

## 9. 分阶段开发与最终切换

分阶段开发不等于降低最终分母：

- 每个 capability cluster 先冻结 approved slice LPD/CAD、状态/effect oracle、exact applicable
  metric denominator、baseline、工作负载和故障场景；只有 slice Tier 1 zero-gap 且全部
  applicable metrics 满足 SLO/non-inferiority 时，才对该 slice 声明完成；
- slice 完成不得推出全系统对等、生产 readiness 或 cutover eligibility；
- 未迁移能力继续由冻结的旧 Mnemos 提供，旧系统保持 reference、data source、oracle 与
  rollback 保障；
- 先完成 Mnemos 全量 capability archaeology：从所有入口、合同、代码、配置、schema、
  state/effect sink、测试、文档、历史与运维流程重建 capability intent 和 expected behavior，
  再把 working/partial/broken/unreachable/unknown 作为独立的 realization-health 事实记录；
- LPD 包含经过裁决的 **有效 capability intent**，包括当前实现有 bug 或未修好的功能；不能把
  broken realization 当作删除功能的理由。偶然 buggy effect、重复 owner、测试适配胶水和失效
  合同不自动变成功能本体。旧 Mnemos 同时作为能力发现、架构拼接和实现机制的参考；其已证明
  正确的场景可直接提供行为 oracle，broken/partial 链路则使用产品意图、数据与 effect 守恒、
  不变量、有效合同和独立 acceptance test 重建 oracle，并把具体缺陷转化为 successor 必须
  拒绝的 negative oracle；
- capability inventory 冻结后，逐项比较复用、修正、重组和重新实现；只有在相同完整功能、
  性能、体验、安全、数据和恢复结果下，才选择 Interface 更小、owner 更清晰、路径更短且
  whole-system complexity 更低的拼接方案；
- 不强制所有能力长期双跑。高风险、可比较且副作用可隔离的链路可做 shadow/differential
  运行；不可隔离的写链路使用 replay、snapshot comparator、contract tests 与受控演练；
- 最终切换必须重新冻结全量 LPD/CAD、非空 exact performance/experience denominators 与
  all-material outcome-equivalence profile，并证明 100% 功能分母、数据无缺口、关键状态与
  effect 对等、两个质量域各有 meaningful improvement 且无 material regression、并发和
  crash/restart 通过、相对 approved outcome-equivalent implementation baseline 的 Tier 3
  complexity Pareto improvement 与 local quality guardrails 通过、回滚可用，且不存在依赖
  旧系统的隐性写路径。

风险比例化只改变证据深度，不改变不变量：低风险且可逆的局部设计可使用较轻的 fixtures；
高风险、不可逆、涉及安全/隐私/主体性/数据迁移或公共 Interface 的设计必须增加独立
challenge、故障注入、规模演练和恢复证据。任何 profile 都不能跳过 Tier 1。

## 10. 当前完成度与下一步

当前只可声明：

- 本文给出了 review-ready、non-governing 的书面设计；
- producer 已形成 assessment 候选；
- 五候选 denominator、protocol-owned constraints、typed standing、无环 DAG 与质量优先级
  已在书面层定义。

当前不可声明：

- 方法已经 `ADOPT`；
- denominator、evidence、schema、hash 或独立性已经验证；
- challenge、final decision candidate、dependency bundle、post-final structural verification、
  exact authorization 或 post-authorization acceptance 已完成；
- 任一 constitution clause、新系统实现、生产迁移或 cutover 已获批准。

下一步严格按 DAG 进行：冻结所有 producer 输入及 exact hashes，完成 detached challenge，
由独立 finalizer 形成 non-governing final-decision candidate，再生成 exact typed dependency
bundle；post-final structural verifier 必须绑定并重算 final 与 bundle。仅当 challenger
blocking finding 为零、final 为 `ADOPT` 且 verifier 为 `ELIGIBLE` 时，最后单独请求用户对
同一 dependency root、method、action、scope、时间窗和 recovery boundary 的 exact-byte
authorization；授权仍须由 detached post-authorization gate 对 raw bytes、内部文本 hash、
principal、scope、action、prohibitions、recovery 和双时间窗验收。只有 gate receipt 为
`ACTIVATION_ELIGIBLE` 时 method bytes 才可在 design-method scope 内使用。任何修订都生成新
bytes 并从 producer assessment 重新开始。

## 11. Review Checklist

- [x] 旧 V2 参考设计及其 exact hash 被保留，本文未覆盖其历史结论；
- [x] “谁对听谁的”被拆成 claim kind 与 typed standing，不按身份或时间排序；
- [x] status quo、authority hierarchy、scalar MCDA、deliberative committee 与
  constraint-first typed evidence 五项均在分母；falsification 是所有候选的义务，不是第六候选；
- [x] 硬约束由 protocol owner 冻结，候选不能自己出题；
- [x] artifact DAG 从 producer 到 post-final structural verifier、exact user authorization 和
  post-authorization acceptance gate 单向无环；
- [x] JSON Schema 与独立 verifier 的证明边界明确；
- [x] schema 只负责 Ticket #1 lifecycle，不是全系统 god schema；
- [x] Tier 1、Tier 2、Tier 3 不可相互补偿；
- [x] slice profile 与 final-cutover profile 分离，指标 exact denominator、baseline class、
  direction normalization、target-range distance、interval/margin ordering、exclusive effect
  state 与 all-material cross-domain exact set 义务明确；
- [x] 复杂度使用 typed-fact 六维向量、exact denominator、阈值、同根测量和 unresolved exit，
  且每项绑定 baseline/candidate/materiality/delta/margins/membership/derived state，local
  guardrail 绑定 profile/oracle/workload/evidence；不使用 LOC 或单一优雅分；
- [x] Deep Module 有 god-object 负向 oracle，Seam 不按 adapter 数量判定；
- [x] slice 完成与最终 cutover 严格分离，切换前功能分母必须 100%；
- [x] 本文未声称 ADOPT、verified、authorized、production-ready 或 cutover-ready。
