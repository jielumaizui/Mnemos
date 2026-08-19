# 认知后继 V2：D0.1 Decision Map

## Map 状态

**状态：NON-GOVERNING / FRONTIER=#1 / WRITTEN-SPEC-REVIEW-REQUIRED。**

本 Map 是认知后继规划的紧凑 canonical navigation artifact。完整设计与证据见：

- [D0.1 design](./2026-08-01-cognitive-successor-v2-d0-1-design.md)
- [Decision #1 written spec](./2026-08-01-cognitive-successor-design-adjudication-design.md)
- [保留的原 successor design](./2026-08-01-cognitive-successor-capability-atomicity-design.md)
- `docs/acceptance/cognitive_successor_d0_1/design_adjudication_method.candidate.json`
- `docs/acceptance/cognitive_successor_d0_1/constitution.candidate.json`
- `docs/acceptance/cognitive_successor_d0_1/constitution.candidate.schema.json`
- `docs/acceptance/cognitive_successor_d0_1/design_adjudication.schema.json`
- `docs/acceptance/cognitive_successor_d0_1/method_bootstrap_protocol.candidate.json`
- `docs/acceptance/cognitive_successor_d0_1/method_evidence_manifest.candidate.json`
- `docs/acceptance/cognitive_successor_d0_1/method_candidate_denominator.candidate.json`
- `docs/acceptance/cognitive_successor_d0_1/method_adjudication_record.candidate.json`
- `docs/acceptance/cognitive_successor_d0/manifest.json`

每次只解决一张 frontier ticket。票内不复制大型调查资产，只链接其结果。产品名、
实现代码、生产状态和 cutover 不由本 Map 自行批准。

## 当前操作边界与可重开假设

- Mnemos 现行 governing contract 继续约束本轮对旧仓库/生产系统的操作安全，但不
  自动裁决 successor 的产品语义或架构。
- 原 successor/V2 bytes 保持不动以保存证据；其内容全部可被更强证据推翻。
- 100% 有效 legacy parity、旧系统 rollback、单生产 writer、双分母和增量 artifact
  family 都是当前强候选，不是因为曾被写入合同就不可挑战。
- D0 v1 继续作为 discovery challenger；是否永久保持原格式、由什么替代、如何绑定，
  仍按 design-stage adjudication 证明，禁止简单改名制造 freeze capability。
- 具体产品名不阻塞调查，但在新仓库、package、DB namespace 和公开 Interface 前必须
  被裁决。
- 设计裁决不按旧合同、新提案、reviewer 或多数票排序，而按产品目标、理论证据、
  代码现实、工程可行性、风险、可证伪性和生命周期成本比较。

## #0：是否覆盖现有 V2 原稿？

Blocked by: none
Type: Discuss
Status: RESOLVED

### Question

D0.1 应修改原 successor design，还是创建增量资产？

### Answer

创建增量设计、独立裁决方法候选、待裁决条款集和本 Decision Map；不修改原稿。这里
“保留”只保存证据和
历史 challenger，不给予原稿规范优先权，也不承诺最终架构必须兼容其所有结论。

## #1：如何裁决候选设计并形成最终宪法？

Blocked by: none
Type: Discuss
Status: WRITTEN_SPEC_REVIEW_REQUIRED

### Question

Mnemos 现行合同、旧 successor、V2、用户方向、代码证据、认知理论与 reviewer 建议
发生冲突时，如何在没有先验版本权威的前提下裁决；哪些胜出的结论才有资格成为
constitution clause？

### Answer

当前 producer recommendation 是以 **约束优先的类型化证据裁决** 作为下一轮区分性原型
的 working base；它尚未 `ADOPT`。本轮把 status quo、权威层级、标量 MCDA、审议式
委员会和约束优先类型化证据冻结为五个对称 strongest-fair 候选。权威层级的合法辅助
角色是产品价值 owner、责任和操作授权；MCDA 只在已证明的可行集合内表达可补偿偏好和
敏感性；委员会用于发现场景、合并 finding 与保留异议；任何一项都不能靠身份、总分或
票数承担跨 claim-kind 的 correctness adjudication。

“谁对听谁”被落实为分型规则：事实服从可复核证据，价值服从合法 owner，安全/隐私/
主体性服从有来源和 scope 的不可补偿约束，架构服从冻结后的原型、基准和故障测试，
迁移服从能力/数据/effect 守恒与恢复，执行服从精确授权。`UNKNOWN`、`CONTESTED`、
`TIE_SENSITIVE` 和不可比不得被强制数值化或投票消除。

最终代码使用非补偿式质量门：先完成 approved Legacy Parity 与 Cognitive Adequacy 两个
exact denominator 以及安全/数据/恢复门；再冻结 exact material metric denominator、
scenario/cohort coverage 与 typed baseline registry，由 candidate-baseline effect interval 对
SLO、non-inferiority、equivalence 和 meaningful-improvement margins 推导逐指标状态。slice
只证明 applicable metrics 的 SLO/non-inferiority；最终 cutover 才要求性能与体验两个非空
完整分母各有至少一项预声明 meaningful improvement 且无 material regression。metric
normalization、target-range distance、interval/margin ordering 和 exclusive state precedence
由 verifier 单值重算，跨 non-inferiority 边界的 interval 为 `UNKNOWN`。material outcome
不可比时进入 prototype 或用户价值选择，简洁性不能破局。只有 exact all-material profile 中
的 cognitive/safety/privacy/agency/data/effect/recovery/performance/experience outcomes 等效时，
才按 typed-fact
`K_API/K_AUTHORITY/K_PATH/K_CHANGE/K_LIFECYCLE/K_ABSTRACTION` 的 exact-denominator
独立 whole-system 测量选择更简洁、优雅的实现；最终实现必须相对 approved outcome-
equivalent implementation baseline 至少改进一项 predeclared material complexity fact、无
material complexity regression，并通过 negative oracles 与 local quality guardrails；否则
输出 `SIMPLICITY_UNRESOLVED`。
LOC、文件数、删能力、删 required asset、god Interface、假 Seam 或把复杂度移给调用方和
运维都不能制造 simplicity green。

`design_adjudication.schema.json` 只作为 Decision #1 方法 bootstrap lifecycle 的 schema
owner，不能膨胀成所有 constitution clause 的 god schema。artifact DAG 按
schema/protocol/evidence/denominator/method → producer assessment → challenger → detached
final-decision candidate → typed dependency bundle → post-final structural verifier → exact
authorization → detached authorization-acceptance gate 单向绑定；pre-authorization verifier 不得
声称验证未来授权。JSON Schema 只管局部结构，hash preimage、
exact set partition、DAG、身份独立性与状态派生必须由独立 executable verifier 重算。
`REVISE` 终结当前 generation；只有 final `ADOPT` exact bytes 才可请求授权。

当前 `method_adjudication_record.candidate.json` 是
`PRODUCER_ASSESSMENT_NON_GOVERNING_REQUIRES_DETACHED_CHALLENGE`，其 epistemic outcome 为
`INDETERMINATE`、design outcome 为 `PROTOTYPE_REQUIRED`。待裁决条款增至 17 条，仍
全部为 `NOT_ADJUDICATED`：V2-CONST-015 只保存用户的价值排序，V2-CONST-016 保存
性能/体验证据合同候选，V2-CONST-017 保存 whole-system simplicity 架构机制候选；三者
不得互相越权。本票退出仍需要：用户审阅 written spec、mutable evidence snapshot、
同包盲测的五方法区分性 prototype、基于最终 bytes 的 detached challenge、独立 finalizer
生成 non-governing final candidate、typed dependency bundle、post-final verifier 正反
fixtures 与 blocking finding gap=0。只有 final candidate 为 `ADOPT` 且 verifier 为
`ELIGIBLE` 时才另行请求 exact authorization；授权还须通过 duplicate-aware raw loader、
内部文本 hash、principal/scope/action/recovery 与双时间窗的 detached acceptance gate，receipt
为 `ACTIVATION_ELIGIBLE` 后才能在 design-method scope 中使用。激活方法也不自动采纳任何 clause。

## #2：双分母的 wire schema 和共同硬门是什么？

Blocked by: #1
Type: Discuss
Status: BLOCKED

### Question

Legacy Parity Denominator 与 Cognitive Adequacy Denominator 如何保持独立 exact set，
又如何通过 realization、owner、oracle 和 joint interaction evidence 连接？

### Answer

未决。推荐新建 freeze-capable v2 family；D0.1 当前保持 v1 五本账 bytes 不动作为
challenger，其长期格式与绑定方式仍在本票内裁决。双分母候选让两个分母
拥有不同 ID namespace、set root、verification receipt 和 approval receipt。同一
Module 可实现两类 obligation，但不能合并 denominator IDs。退出证据是完整 schema、
canonicalization、freshness/staleness、hash-DAG 和独立 verifier specification。

## #3：产品正式名称是什么？

Blocked by: #1
Type: Discuss
Status: BLOCKED

### Question

哪个名称能表达 cognition、epistemic expansion 与人机互补，同时不暗示只做 memory、
模拟用户或完全自治？

### Answer

未决。名称不决定 Module 或数据库边界；但必须在创建新仓库、package、DB namespace、
CLI/MCP public namespace 和 migration artifact 前冻结。

## #4：Legacy Parity reverse inventory 如何达到完整？

Blocked by: none for read-only capability archaeology; denominator freeze still depends on #2
Type: Research
Status: READY_FOR_READ_ONLY_CAPABILITY_ARCHAEOLOGY

### Question

如何从全部 ingress、参数 facet、state/effect sink、dynamic trigger、schema、配置、
安全/恢复/性能合同和测试反向闭合 atomic capability set？

### Answer

未决。输入是 D0 v1 的 1,390 surface、39 capability seed、2,100 tests/oracles 和全部
UNKNOWN challenger，不是 39 项 function matrix。退出要求两套独立 census 的 exact
multiset diff=0、pending family=0、unmapped/unclassified/unknown=0，且 config
applicability、owner、target 和 oracle 都有 detached evidence。

现在即可开始只读 capability archaeology，不必等待整个裁决方法激活；但 inventory 的最终
冻结与批准仍等待 #2。每个发现项使用两个正交轴：`capability_validity = CANDIDATE | VALID |
INVALID | UNKNOWN`，以及 `realization_health = WORKING | PARTIAL | BROKEN | UNREACHABLE |
UNKNOWN | SUPERSEDED`。`BROKEN/PARTIAL` 绝不推出 `INVALID`；它可能正是尚未修好的必保功能。
只有 `VALID` capability 的已证明正确 scenario 能直接提供 legacy behavior oracle，其他
scenario 由产品意图、守恒、不变量、有效合同和独立 acceptance test 重建 expected behavior；
具体 defect 形成 negative oracle。分母冻结要求 capability validity unknown=0，但不能靠实现
健康状态删项。

每项 archaeology record 至少覆盖：所有入口与参数 facet、预期输入/输出、state/effect、失败/
恢复/并发/性能/安全语义、配置适用性、schema/data owner、当前代码链、测试与文档证据、历史
变更、realization health、defect refs、oracle 强度和候选 successor owner/Interface。冻结后再以
现有 Mnemos 的模块拼接为 baseline，同时提出 outcome-equivalent 的复用、修正、重组和重写
方案；完整性、性能与体验先过门，最后比较 whole-system simplicity。

## #5：Cognitive Adequacy 场景分母是什么？

Blocked by: #1, #2
Type: Research
Status: BLOCKED

### Question

哪些 bounded scenarios 能证明系统了解用户但主动补足信息边界，而不是只做一个
bug-free Mnemos？如何定义 materiality、risk、false balance 和 expansion-required？

### Answer

未决。最低候选 family 见 D0.1 design §7.2。当前退出证据候选包含 user-only、
system-only、joint、appropriate reliance、counterevidence、unknown、abstain、
correlated-source 和 anti-manipulation oracles；具体集合由本票裁决。seed family
数量不能当完成率。

## #6：每个 capability/obligation 的 owner、Interface 与 disposition 是什么？

Blocked by: #4, #5
Type: Research
Status: BLOCKED

### Question

每个 LPD capability 和 CAD obligation 由哪个 deep Module 拥有；哪些 legacy surface
通过 Adapter 保留；哪些旧行为需要更强合同或明确拒绝？

### Answer

未决。当前 LPD disposition 候选是 `PRESERVE_EQUIVALENT`、
`PRESERVE_VIA_ADAPTER`、`REPLACE_STRONGER_CONTRACT`，以及有 adopted design
adjudication 和 negative oracle 支持的 `REJECT_INVALID_LEGACY_BEHAVIOR`。
退出要求 owner/target unknown=0、duplicate owner=0、Interface contract 完整且每项有
独立 oracle 与 migration/reverse mapping。

## #7：User Context、World Model 与 Action 的 authority matrix 是什么？

Blocked by: #1, #5, #6
Type: Prototype
Status: BLOCKED

### Question

如何让 external/tool evidence 更新 World Model，却永远不能越权写用户目标、Persona、
Policy 或 action permission？现有 exact span/hash/catalog 如何复用？

### Answer

未决。推荐把单一 `allows_cognitive_update` 深化为 target-typed authority matrix：
`USER_CONTEXT`、`WORLD_EVIDENCE`、`POLICY`、`ACTION_PERMISSION`。原型必须证明低权来源
不能横向升级、同一 span 对不同 target 有独立 admission receipt、读写 owner 排他。

## #8：Evidence Admission 与 Personalized Presentation 的 Seam 在哪里？

Blocked by: #1, #5, #6
Type: Prototype
Status: BLOCKED

### Question

在用户目标确实影响 task scope 的同时，如何证明 Persona、表达偏好和 challenge mute
不会删除 material evidence、counterevidence、alternative 或 unknown？

### Answer

未决。推荐先冻结 task/risk/materiality，再生成 evidence roots；presentation 只消费
这些 roots。同 task/corpus 只改变 presentation variant 时，material evidence、
counterevidence、epistemic status 和 risk class 必须不变。另需 false-balance 反例。

## #9：Typed Cognitive Workspace 与 Epistemic Control 的最小深 Interface 是什么？

Blocked by: #5, #6, #7, #8
Type: Prototype
Status: BLOCKED

### Question

谁拥有 round state、budget、proposal、conflict、unknown、no-progress 和 terminal
disposition；现有 DecisionTrace、Prediction、Feedback 和 Action Broker 如何只作为
稳定 Seam 被复用？

### Answer

未决。原型必须覆盖 `ANSWER/ACCEPT`、`REVISE`、`ASK_USER`、`SEEK_INFORMATION`、
`PROPOSE_EXPERIMENT`、`ABSTAIN`、`ESCALATE`，并证明预算耗尽或无进展不会被包装成
高置信答案。当前 prototype hypothesis 让 DecisionTrace 只封存结果、不拥有探索过程；
它必须与扩展 DecisionTrace owner 等替代方案一起比较。

## #10：V2-only 状态如何进入 rollback/data-forward 合同？

Blocked by: #5, #6, #7, #9
Type: Research
Status: BLOCKED

### Question

旧 Mnemos 无法表达 WorldClaim、UnknownFrontier、WorkspaceRound 或新 correction class
时，如何保证 rollback window 内数据不丢失、旧引擎可运行且新事实不被降格？

### Answer

未决。当前 safety-constraint candidate 不允许通过窗口内关闭有效 V2 capability 获得
rollback=green；它仍需与缩短或取消 rollback window 等替代策略比较。机制候选包括
legacy opaque extension envelope、read-only sidecar 或限制 cutover eligibility，需用
exact round-trip conservation 和旧引擎行为 oracle 裁决。

## #11：Wiki semantic input 与 publication/projection 的唯一 owner 是谁？

Blocked by: #4, #6
Type: Discuss
Status: BLOCKED

### Question

用户编辑、trusted publish、move/delete、semantic knowledge revision、Markdown 文件和
六类 projection receipt 如何拆分 owner，且不恢复双写？

### Answer

未决。必须先完成所有 Wiki surface/state/effect 的 LPD inventory；在此之前不得宣布
Wiki 全部为 projection，也不得把 page lifecycle 同时保留在 Core 与旧 ledger。

## #12：D1 TransactionGraph 后选择哪个物理 topology？

Blocked by: #6, #7, #8, #9, #10, #11
Type: Prototype
Status: BLOCKED

### Question

单 MetadataCore 与 Capture/Cognitive/Workflow 三 ledger 的哪一个能在相同完整分母、
workload、SLO 和 fault schedule 上满足事务、隐私、性能、备份和 rollback 合同？

### Answer

未决。先完成 D1 read/write/same_tx/pending graph；物理拓扑不得由产品名、当前库文件
数量或纸面整洁度预选。任何候选使用较小 capability set 都使比较无效。

## #13：freeze-capable D0 v2 generator/verifier 如何实现且不自证？

Blocked by: #2, #4, #5, #6
Type: Prototype
Status: BLOCKED

### Question

如何实现两个 exact denominator、typed adjudication、owner/target registry、oracle
receipt、config applicability、reverse inventory 和独立 verification，而不复用 runner
的完整性判断？

### Answer

未决。D0.1 当前保持 v1 bytes 不动作为 input/challenger；是否作为永久 input 由 #2
裁决。v2 的 hash-DAG 候选是 source/constitution/schema/generator → denominator
artifacts → detached independent report → exact user approval。任何源、代码、配置、
registry、artifact bytes 或有效期变化使 receipt stale，是待原型验证的 freshness 主张。

## #14：何时允许从 D0 进入 D1？

Blocked by: #1, #2, #4, #5, #6, #13
Type: Discuss
Status: BLOCKED

### Question

哪些 exact artifacts 和独立证据足以宣布两个分母 frozen/approved，并开始
TransactionGraph？

### Answer

未决。当前 exit-gate 候选要求两个 denominator 的 required-zero predicate 全部为 0、
独立 census complete、exact set roots 经 detached verifier 复核、constitution/
denominator final bytes 分别获得有效用户 activation authorization；`UNKNOWN`、
`UNLINKED`、pending family 或自报 receipt 是否全部阻断及其例外规则由本票正式裁决。

## Fog of war

D1 之后的 topology、implementation plan、能力簇迁移、全量演练、命名落盘和 cutover
票暂不展开。它们依赖 #14，过早细化会让实现假设反向污染 D0。
