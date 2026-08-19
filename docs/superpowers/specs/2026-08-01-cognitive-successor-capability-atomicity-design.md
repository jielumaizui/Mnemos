# 认知后继系统：能力分母、事务骨架与切换设计

## 文档状态

**状态：DRAFT / NON-GOVERNING / DESIGN-ONLY。**

本文绑定只读审计基线：

- repository：`<repo-root>`（当前 Mnemos checkout 的逻辑根，不绑定任一本机绝对路径）
- branch：main
- commit：1e36a31a26b0b5baf768815f185d57174e9c59dd
- review date：2026-08-01

本文不是 Phase 0–7 治理合同修订，不冻结产品名，不批准实现、测试、daemon
启停、真实 API 调用、生产迁移、生产 replay、生产数据修改或切换。本文中的
“认知后继系统”只是中性工作称呼；Mnemos vNext 也只保留为历史讨论标签，
不能成为最终产品名。

只有以下两类内容在本文中视为已接受原则：

<!-- accepted-principle:complete-function-denominator -->
1. 开发可以按能力簇分阶段进行，但最终切换前必须达到冻结后的 100% 有效
   功能分母，不允许删功能或永久延期。
<!-- accepted-principle:legacy-frozen-oracle-rollback -->
2. 旧 Mnemos 在切换前保持冻结、可用、只修最高优先级安全或数据损坏问题，
   并作为数据源、行为参考、离线 oracle 和回滚引擎。

其余架构结论按“已确定、默认假设、挑战者、阻断项”分别标识，不能把默认
假设写成已经证明的事实。

## 一、结论先行

当前最稳妥的骨架不是“复制旧仓库”、也不是“先选三个数据库再填功能”，而是：

> 先冻结完整能力分母和全量读写图，再以 typed deep modules 包住一个因果提交
> 内核；完整 Raw 等大字节进入不可变 PayloadVault，外部副作用保留 target-owned
> truth，已确认的 KG/ANN/Search/Metrics 等读模型成为可版本化重建的投影；
> Wiki 的输入/投影边界另行裁决；生产写 generation 在任一时刻至多一个。

当前架构裁决状态如下。

| 议题 | 当前裁决 | 状态 |
| --- | --- | --- |
| 最终功能范围 | 冻结后的有效能力分母 100% 迁移和验证 | 已确定 |
| 旧系统角色 | 冻结的生产版本、数据源、行为参考、回滚引擎 | 已确定 |
| 生产双写 | 禁止旧系统与后继系统同时拥有生产写权或真实 effect 权 | 已确定 |
| 比较方法 | 离线 record/replay、隔离 clone、无真实 effect shadow、故障注入 | 已确定 |
| 逻辑模块 | Capture、Knowledge Formation、Cognition、Workflow、Projection Lifecycle、Query、Maintenance | 默认骨架 |
| 事务机制 | typed Causal Commit Kernel，不公开通用 transact 或表级能力 | 默认骨架 |
| 大字节 | 独立 PayloadVault，以 immutable ref/hash 被 metadata 引用 | 默认骨架 |
| 外部副作用 | target owner 保存真实结果；Core 只保存 intent、observation 和 closure | 已确定 |
| 已确认投影 | KG/ANN/Search/Metrics 等采用 generation build → verify → activate | 已确定 |
| Wiki 角色 | 区分 semantic input、publication target 与 rebuildable consumer 后再定 owner | 阻断项 |
| metadata 物理候选 A | 单 MetadataCore | 与候选 B′ 同级，尚未冻结 |
| metadata 物理候选 B′ | CaptureLedger + CognitiveLedger + WorkflowLedger | 与候选 A 同级，尚未冻结 |
| 回滚 | engine rollback、data-forward，不在新写入后整库倒退 | 已确定 |
| 产品名 | 必须体现 cognition，不以 memory-only 或 vNext 命名 | 待单独裁决 |

当前证据只证明 Capture、Cognition、Feedback、Scheduler、Privacy 和 rollback
envelope 各自存在不可分原子岛；它尚未证明这些岛形成覆盖 Capture、Cognition、
Workflow 三域的 same-transaction 传递闭包。反方向上，单 SQLite writer 的吞吐、
p99、WAL、checkpoint、backup RTO 和隐私隔离也尚未实测。因此 A 与 B′ 必须在
D1 TransactionGraph 后保持同级，并使用同一 trace、SLO 和 fault schedule
裁决，不能在纸面上预选物理拓扑。

## 二、系统不变量

后继系统必须同时满足下列不变量。

1. 一个业务事实恰好一个 canonical owner；投影、测试夹具、治理证据和兼容层
   不能成为第二 owner。
2. Canonical Raw 完整保存用户、助手、工具、reasoning、附件和来源证据；高权
   认知更新仍只允许 exact role-local authority span 授权。
3. revision、head、event、receipt、outbox 和其 expected-head precondition
   只要语义上需要同生共死，就必须位于同一真实事务，而不是依赖后续扫描补洞。
4. 跨 SQLite、PayloadVault、文件和外部 provider 不伪造“魔法分布式事务”。
5. source mutation 与 origin outbox 同事务；target mutation 与 target-local
   observation/journal 同事务；source terminal 由 target 证据推导，不能自签。
6. 对外状态区分 accepted、pending、committed、existing、deferred、dead、
   effect_uncertain；不能把排队、路径存在或 worker 返回值写成 committed。
7. 外部 provider 无稳定幂等键或状态查询时，发送后崩溃必须停在
   effect_uncertain，禁止盲重试。
8. 读路径零 DDL、零目录创建、零 metrics write、零生产 fallback。
9. 可产生生产写的 active writer generation 任一时刻至多一个；正常服务时恰好
   一个，cutover fencing 可有受控的 zero-writer 窗口。retired generation 在
   OS capability 和 destination contract 两端都必须被拒绝。
10. 生产新写入出现后，不得用旧快照覆盖新事实；恢复和回滚只能前滚数据或追加
    compensation。
11. 运行、治理和证据三平面分离；独立 verifier 不得复用 writer reducer、
    comparator 或 self-oracle。
12. 性能优化不能通过降低功能分母、ACL、Raw fidelity、effect quality、失败
    终态或门禁阈值达成。

这些不变量与现行合同的关键锚点一致：

- Desktop Phase 0–7 合同 945：runner 消费 tracked denominator，verifier 独立；
- 同合同 980：一个认知状态一个 canonical owner；
- 同合同 992：生产写恢复后禁止整库倒退；
- 同合同 1060–1066：outbox、target receipt、saga 和 forward correction；
- 同合同 1425：多 SQLite/Vault snapshot 只能在 writers quiesced 时形成共同
  epoch manifest。

## 三、为什么现有 39 项 function matrix 不是完整分母

docs/acceptance/function_matrix.json 当前有 39 个粗粒度 feature，分属 18 个
group label，其中 persona.profile 仍标记为 partial。它是有价值的功能种子，
但 scripts/audit_function_matrix.py 只做以下检查：

- 声明字段非空；
- 已声明 CLI 路径存在；
- 已声明 MCP tool 存在；
- MCP 注册表与 schema 集合一致；
- 已声明代码和文档路径存在；
- status 属于允许集合，feature ID 不重复。

它没有从 CLI、MCP、daemon、source、scheduler、scripts、schema owner 和
effect sink 反向枚举，因此“漏写一个入口”不会让审计失败。它也没有展开会
改变行为的参数模式，没有记录并发、重启、恢复、权限、性能和 failure oracle。
所以 39 不能直接成为切换分母，也不能用 39/39 表示功能对等。

手工文档已经出现可观察漂移：README.md:554 仍声称 58 个顶层 CLI，而当前
argparse 树为 59。这个差异本身就证明分母必须由 exact-commit collector 生成，
再由独立实现复核，不能靠人工数字长期维护。

## 四、当前机械入口基线

以下是 commit 1e36a31a 上的只读静态基线。各集合互有映射和重叠，不能相加
得到“功能总数”；它们是反向完整性必须覆盖的 surface denominator。

| Surface | 当前机械分母 | 关键证据 |
| --- | ---: | --- |
| 安装 console script | 1：mnemos | pyproject.toml:56–57 |
| 主 CLI | 59 个顶层 parser、226 个 command node、179 个 leaf parser | mnemos_cli.py |
| 主 CLI 参数面 | 408 个源码定义 action：347 optional + 61 positional；按 leaf 展开继承参数后为 426 个有效 facet：365 optional + 61 positional；178 boolean；12 个 choice action / 62 个 choice value | 不能混淆“定义点分母”和“按叶可观察参数面” |
| 主 CLI selected flag counters | 39 leaf 有 required positional；72 有 --json；16 有 --dry-run；11 有 --apply；3 有 --confirm；4 有 --yes | 不是完整参数分母；choices/default/mutex 仍待 D0 展开 |
| daemon CLI | 6 choices：start、stop、status、run、install-windows、uninstall-windows | daemon/entrypoint_support.py:666；mnemos_daemon.py 仅委托 |
| MCP tool | 注册、schema、policy 三集合精确一致，共 57；category registry 只有 56，遗漏 session_save | integrations/agora.py:174–305；这是显式 blocker，不能用默认分类补齐 |
| MCP protocol | initialize、notifications/initialized、tools/list、tools/call 四类 JSON-RPC method/notification | integrations/agora.py:1377–1427,1543–1570 |
| 应用 facade | 67 个 public Protocol method | core/application/contracts.py:12–559 |
| Source | 12：8 host_agent + 4 ingestion_only | core/agent_kit/agent_source_support_manifest.json |
| Source 接入 | 5 mcp_only + 3 adapter + 4 none；6 watchdog + 3 hybrid + 3 polling | 同一 source manifest |
| daemon interval | 38，均映射 handler；另有 1 个 legacy alias l1_sync | daemon/intervals.py:20–66；daemon/service_registry.py |
| Chronos | 26 step：20 cron + 4 event + 1 condition + 1 passive | core/kia/chronos_scheduler_support.py；chronos_builtin_steps.py |
| EventBus policy event | 32：16 persistent + 16 no-persist | core/mnemos_bus.py:515 |
| 静态订阅 | 33 条 concrete registration edge，覆盖 21 个 concrete topic；另有 1 条星号 wildcard | core/pluggable.py:332 及 scripts/ 订阅点；wildcard 表示集合仍开放 |
| Health check | 31，builder 要求 exact ordered equality | core/ops/health_contract.py；core/ops/health_check.py:1308 |
| 默认 KIA module | 5：genos、eris、hygieia、ixion、stress_test | core/kia/module_registry.py:11 |
| 可直接执行脚本 | 204：79 audit、48 reconcile、49 other、7 generate/refresh、6 run/gate、5 check、5 verify、3 migrate、2 rebuild | scripts/**/*.py AST census |
| 脚本 selected flag counters | 185/204 使用 argparse；59 有 --apply；45 有 --backup-dir；13 有 --dry-run；68 有 --strict；140 有 --json；3 有 --real-api；1 有 --confirm | 不是完整参数分母 |
| 无 direct-main 的脚本 module | 36；不能据此推定都是 helper | scripts/**/*.py |
| scripts 非 Python/无 guard 入口 challenger | wiki_git_auto_commit.sh；wrapper_weekly_report.py 具有 shebang、import-time I/O 和 runpy 执行 | 不能按 __main__ guard 静默排除 |
| scripts 之外的 guarded Python main | core/integrations 25，root compatibility wrapper 8 | 必须逐项裁决 duplicate、adapter 或独立能力 |
| root setup challenger | setup.sh、setup.bat | 安装/卸载与 OS effect 合同仍待展开 |
| DDL 文件命中清单 | 129 个 entry，其中 109 unregistered，release_eligible=false | docs/acceptance/schema_owner_manifest.json；这是文件命中，不是 target schema 数 |
| HTTP route | 常见 FastAPI、Flask、aiohttp route 静态命中为 0 | 只表示当前扫描未发现，不是未来 API 禁令 |

57 个 MCP tool 中，category registry 当前只覆盖 56：5 core、22 extended、
9 auxiliary、4 lifecycle、16 advanced，`session_save` 未分类；访问策略分为
21 memory_read、11 memory_write、10
feedback_write、6 admin_runtime、5 capture_write、4 public_metadata。任何新
系统映射都必须同时保留 tool 行为、参数 schema、principal、scope、失败状态和
访问策略，不能只保留同名方法。

204 个 guarded 脚本入口尤其不能按文件名机械迁移；它们也不是 scripts 的完整
executable denominator。49 个 other、19 个无 argparse 入口、无 guard 的 wrapper、
shell/setup 入口和 scripts 之外的 guarded main 首先属于 unclassified；即使脚本
具有 --apply，也不能据此推定它的 dry-run、backup、幂等、恢复和生产授权合同。
反过来，没有 --apply 或没有 __main__ guard 也不证明它只读或不可达。

表中的 flag 数只用于证明参数面存在，不能当作完整参数分母。D0 还必须展开
--strict、--unsafe-debug、--write-report、--no-commit、--allow-dirty、
--execute-wrapped、--all、daemon 的 controlled-raw-sync-only，以及 positional
choices、default、互斥组和组合约束。

此外，docs/acceptance/cognitive_requirement_test_manifest.json 当前记录
requirement_count=126、root_coverage_count=50、unregistered_count=95，其中
unregistered 是 requirement → test registration 缺口。requirement、surface、
capability 和 test 是不同实体，不应要求基数或集合相等；必须分别冻结四个分母，
再验证 coverage relation 无 orphan，不能选其中任何一份单独代替完整能力分母。

## 五、完整功能分母的定义

### 5.1 原子能力不是文件或命令数量

一个 atomic capability 是一项具有相同外部可观察合同的行为。只要下列任一项
不同，就应拆成不同能力或明确的 contract facet：

- principal、scope、source role 或 authority；
- required input 或行为改变参数；
- 返回状态或用户可观察输出；
- canonical state delta；
- target effect；
- 幂等、并发或 expected-head 语义；
- crash/restart、retry、defer、dead、uncertain 规则；
- privacy、retention、audit 或 security 约束；
- 性能、容量、RTO 或 RPO。

因此同一个 CLI leaf 的 --apply、--strict、--real-api、--production、
--write-probes 等模式可能是不同能力 facet；同一个底层行为也可能由 CLI、MCP、
daemon 和 scheduler 四个 surface 暴露，但只对应一个 canonical behavior。

### 5.2 分母记录

每个能力记录至少包含：

| 字段 | 要求 |
| --- | --- |
| capability_id | 稳定业务 ID，不使用文件路径作为身份 |
| legacy_snapshot | 精确 commit、config/schema/source manifest hash |
| surface_refs | CLI/MCP/daemon/source/script/API/internal trigger 与参数模式 |
| input_contract | 类型、principal、scope、authority、前置状态 |
| output_contract | 返回值、状态、artifact、presentation |
| state_delta | canonical owner、read set、write set、head/revision 变化 |
| effect_contract | target owner、幂等键、observation、补偿和不确定状态 |
| failure_contract | failpoint、retry/defer/dead、crash/restart、恢复 oracle |
| data_contract | identity、generation、revision、payload hash、retention |
| security_contract | ACL、source authority、secret、audit、privacy |
| performance_contract | workload、hardware profile、latency/throughput/RTO/RPO |
| oracle_refs | legacy black box、现行合同、独立模型、数据守恒、fault fixtures |
| migration_contract | importer、reverse adapter、conservation、rollback mapping |
| status | discovered、contracted、implemented、equivalence_verified 或 adjudication_required |

禁止使用 wont_support、permanently_deferred、not_in_vnext 等状态消掉有效功能。
只有“精确重复入口”或“可证明从未形成可观察行为的 unreachable artifact”可以
在用户审阅 exact evidence 后标记为 not_a_capability；这不能删除其背后的行为。

### 5.3 反向完整算法

冻结分母必须由两个相互独立的方向闭合。

第一条是 surface → behavior：

1. 枚举 1 个 console script、全部主 CLI leaf/完整 argparse facet，以及 6 个
   独立 daemon CLI mode。
2. 枚举 MCP register/schema/policy、四类 MCP protocol method/notification 和
   host integration。
3. 枚举 12 source 的格式、解析、实时/回填、setup、ACL 和 Raw fidelity。
4. 枚举 38 daemon service、26 Chronos step、32 EventBus policy event、21 个
   已知 subscription topic（33 条 registration edge）、wildcard、动态 event/task 注册，以及非 interval worker、
   thread、subprocess 和生成式 OS hook/adapter。
5. 枚举 31 health check、5 个默认及动态 KIA module。
6. 对全部 240 个 scripts module 做 reachability：204 个 direct-main 逐项展开，
   36 个无 direct-main module 必须映射到 wrapper/caller、归类为 package marker，
   或证明间接不可达；shebang、import-time effect 和 runpy wrapper 也按入口候选处理。
7. 枚举 67 application facade method 以及 facade 之外的公开 Python/IPC adapter。
8. 将每个 surface 映射到一个或多个 atomic capability；重复入口共享行为 ID，
   一个入口内的多种语义拆成不同 facet。

第二条是 state/effect → behavior：

1. 反向枚举全部 SQLite table/index/trigger owner、文件/Vault/CAS 路径、配置、
   keyring、模型 artifact 和备份。
2. 枚举所有正式 writer site、DDL owner、cursor/head、outbox/inbox、receipt、
   tombstone 和 projection activation。
3. 枚举 Wiki、KG、ANN、FTS、metrics、MOC、notification、LLM/provider、
   trainer 和 host presentation 等 target effect。
4. 枚举 Phase 0–7 仍有效合同、现行 docs、故障测试、迁移/恢复/发布门禁。
5. 每个 canonical state 和 effect 必须回连至少一个 capability 和唯一 owner。

最终由独立 verifier 重新枚举 surface 和 sink，比较 runner 生成的 manifest。
runner 与 verifier 不能 import 同一 enumerator 或共享“是否完整”的判断函数。

### 5.4 分母冻结门

只有同时满足以下条件，能力分母才可标记 frozen：

~~~text
freeze_evaluator_unimplemented  = 0
surface_unmapped                = 0
behavior_without_surface        = 0  # 纯内部行为须有 trigger_ref
requirement_without_capability_or_adjudication = 0
capability_without_requirement_or_adjudication = 0
capability_without_independent_test_or_oracle  = 0
test_without_capability_or_adjudication        = 0
test_file_without_disposition   = 0
declared_missing_test_file      = 0
canonical_owner_unknown         = 0
effect_target_unknown           = 0
parameter_mode_unclassified     = 0
script_entry_unclassified       = 0
script_parameter_contract_unknown = 0
dynamic_trigger_unclassified    = 0
contract_conflict_unresolved    = 0
effective_capability_excluded   = 0
independent_inventory_diff      = 0
independent_inventory_pending_family = 0
missing_required_source_binding = 0
config_applicability_attestation_gap = 0
constitution_requirement_missing = 0
constitution_approval_missing  = 0
duplicate_record_id             = 0
duplicate_discovery_key         = 0
invalid_record                  = 0
generator_error                 = 0
unresolved_adjudication         = 0
~~~

这 28 项必须由 freeze-capable schema 的独立 verifier 重算，不得由
manifest 自报缩减。v1 因 `freeze_evaluator_unimplemented=1` 且固定
`freeze_capable=false`，永远不能通过本门。

冻结 artifact 必须绑定 legacy commit、配置、schema owner manifest、source
manifest、Phase 合同 hash 和生成器版本。独立 verifier 先对 exact bytes/hash
签发 verification receipt，再由用户批准同一 SHA-256；只有 approval hash 与
artifact bytes 精确匹配时 denominator_approved=true。任一输入或字节变化都会使
验证与批准同时 stale，必须重新生成、独立复核和批准，collector 不能自动决定
“100%”的最终范围。

### 5.5 100% 切换公式

100% 不是粗功能数量相等。CutoverEligibilityRegistry 是唯一 canonical machine
predicate；D4、D5 和后续文档只能填充或引用其字段，不能另加一套口头硬门：

~~~text
cutover_eligible =
    denominator_frozen
    AND denominator_exact_approval_valid
    AND included_capability_set == approved_d0_capability_denominator_set
    AND included_capability_set_root == approved_d0_capability_denominator_set_root
    AND equivalence_verified_set == approved_d0_capability_denominator_set
    AND equivalence_verified_set_root == approved_d0_capability_denominator_set_root
    AND permanent_defer_count == 0
    AND data_conservation_gap == 0
    AND migration_gap == 0
    AND cutover_required_cohort_gap == 0
    AND required_effect_uncertain_at_cutoff == 0
    AND projection_required_gap == 0
    AND hidden_legacy_writer_count == 0
    AND target_adapter_qualification_gap == 0
    AND writer_destination_fence_qualification_gap == 0
    AND backup_epoch_restore_drill == PASS
    AND production_workload_concurrency_restart == PASS
    AND rollback_mapping_gap == 0
    AND rollback_readiness == ROLLBACK_READY
    AND independent_gate_omission == 0
    AND gate_regression_or_weakening == 0
    AND cutover_plan_exact_approval_valid
~~~

这里的四个 set/set-root 等式都是 exact set equality，不是 count equality、subset
equality 或“两个较小集合彼此相等”。`approved_d0_capability_denominator_set` 是用户
批准的 exact D0 capability manifest 中全部有效 capability ID；其 set root 按冻结
manifest 的 canonical set-hash 规则计算。任何 ID 缺失、额外、重命名、合并、拆分
或顺序/规范化规则漂移都会使 receipt stale，并令 `cutover_eligible=false`。

每次 true 结果都生成不可变 CutoverEligibilityReceipt，绑定 exact Ecut、
(Ebase,Ecut] delta root、code/runtime/config/schema/contract hashes、已批准
capability manifest、完整 target inventory/high-water root、WriterAuthority
authority incarnation/sequence 与 readiness epoch、BackupEpoch/restore drill、
migration/reverse plan hash、
全部 gate evidence hash、created_at 和 expires_at。任一绑定或有效期变化都使
receipt stale。

用户对上述 exact receipt bytes/hash 签发 single-use CutoverPermit。WriterAuthority
进入 PREPARED 前必须以 CAS 消费该 permit，同时匹配 current active=G0、
authority incarnation/sequence、readiness epoch、target high-water root、plan
hash 和未过期时间；消费
失败、重复消费或 G0 revoke 前任何绑定漂移都 fail closed。WriterAuthority 不能
仅凭历史 true boolean 退休 G0。

其中 writer_destination_fence_qualification_gap=0 只证明每个 destination 在
隔离 qualification 中能够拒绝 stale generation，不要求切换前已经拒绝仍合法的
G0。Permit 消费后，PREPARED 状态机才执行真实 revoke；只有
G0_revocation_ack_gap=0 才能进入 G0_REVOKED_VERIFIED，随后才允许 mint G1。
因此 pre-cutover eligibility 与 in-cutover revocation acknowledgement 不形成
循环依赖。

开发过程允许 discovered、contracted、implemented 等中间态，但切换时只有
equivalence_verified 才计入完成。

## 六、Oracle 不能只等于旧运行结果

旧 Mnemos 是重要 oracle，但不是唯一真理。否则旧 bug、self-oracle、兼容胶水
和错误终态会一起被复制。

每项能力至少组合以下 oracle：

1. **Legacy black-box oracle**：对当前被证明正确的输入、输出和副作用做记录回放。
2. **Contract oracle**：对旧实现 partial、broken 或未观察到的能力，以仍有效的
   Phase 0–7 合同、source authority、privacy 和 lifecycle 规则为准。
3. **State conservation oracle**：比较 identity、revision、head、cursor、receipt、
   outbox/inbox 和 target observation。
4. **Failure oracle**：在每个事务边、外部 send、target commit、restart 和 restore
   点故障注入。
5. **Performance oracle**：在冻结 workload、hardware profile 和产品 SLO 上比较，
   不能临时发明“现状两倍”或任意秒数。
6. **Independent oracle**：验证代码不得复用生产 reducer、migration mapper 或
   writer 的 success predicate。

当旧行为与有效合同冲突时，记录 adjudication，而不是选择“看起来更绿”的一边。
行为等价是有效能力和状态结果等价，不是逐字节复制旧 bug。

## 七、逻辑架构

~~~mermaid
flowchart LR
    A["CLI / MCP / Source hooks / Daemon / Scheduler / Admin"] --> B["Capture Interface"]
    A --> C["Knowledge Formation Interface"]
    A --> D["Cognition Interface"]
    A --> E["Workflow Interface"]
    A --> F["Query Interface"]
    A --> G["Maintenance Interface"]
    A --> H["Projection Lifecycle Interface"]

    B --> K["Typed Causal Commit Kernel"]
    C --> K
    D --> K
    E --> K
    F --> K
    G --> K
    H --> K

    B --> V["PayloadVault"]
    C --> V
    D --> V
    K --> T["Target-owned effects and journals"]
    K --> P["Confirmed rebuildable projections"]
    V --> P
    H --> P

    Q["Independent Governance / Verifier"] -. reads manifests and receipts .-> K
    Q -. independently compares .-> V
    Q -. independently observes .-> T
    Q -. verifies .-> P
~~~

### 7.1 Deep modules

| Module | 稳定 Interface | 隐藏的复杂度 |
| --- | --- | --- |
| Capture | accept、status、replay | native identity、Raw revision、cursor、dedupe、receipt、handoff |
| Knowledge Formation | form、review、publish-request | distill spec、chunk/checkpoint、merge、quality、trusted decision |
| Cognition | observe、reflect、decide、predict、score、correct | authority、ACL、revision/head、supersedes、outbox |
| Workflow | submit、inspect、reconcile、compensate | inbox、lease、attempt、retry/defer/dead、effect closure |
| Projection Lifecycle | prepare、ready、activate、rebuild | cohort、required consumers、generation、watermark、active pointer |
| Query | raw/search/wiki/graph/persona/context read | projection selection、ACL、freshness、pagination、streaming |
| Maintenance | plan、backup、restore、migrate、verify | exact plan hash、writer fence、epoch、conservation、rollback |
| Governance Verifier | inventory、compare、certify | independent denominator、evidence expiry、release aggregation |

CLI、MCP、daemon 和 scheduler 只是 ingress Adapter。它们不能直接理解表名、
cursor、outbox、lease 或 target journal，也不能自行选择 terminal state。
图中 Query 指向 Kernel 只表示使用 read-only snapshot facade；Query 无权调用
commit、ensure、migration 或 projection refresh。Maintenance 的 mutation 也只能
在 exact plan、授权和 writer fence 下通过封闭 typed operation 执行。

### 7.2 Causal Commit Kernel 的边界

Kernel 只拥有跨模块共用且需要事务一致的机械语义：

- writer_epoch、ledger_generation、replay_generation；
- commit_seq、command_id、revision_id、effect_id、projection_generation；
- expected-head CAS、idempotency、supersession；
- typed event、outbox、inbox、receipt、tombstone；
- state-machine transition 和 transaction commit。

Kernel 不公开 transact(fn)、raw SQL、table name、caller-defined envelope 或任意
dict + type。业务校验留在 Capture、Cognition 和 Workflow 的 typed command
handler 内。这样即便所有 metadata 暂时位于一个物理数据库，也不会形成一个
需要所有调用方理解的 God module。

### 7.3 PayloadVault

PayloadVault 保存：

- 完整 Canonical Raw 和附件；
- prompt/result、fragment、checkpoint 和大正文；
- Wiki/knowledge content；
- model/training artifact 和可复核原始输出。

写入顺序是 stage → hash verify → fsync → no-clobber immutable publish → metadata
transaction 引用。commit 前 crash 可以留下未引用 orphan，后续按 reachability
清扫；metadata 已引用但 payload 缺失必须 fail closed。

隐私方案尚未冻结。默认研究方向是 privacy-domain envelope encryption：
个人数据不能跨 subject 形成不可拆分 plaintext dedupe；Core tombstone 先立即
deny-read，物理或密码学删除随后以 saga 完成，并防止备份 restore 复活。

## 八、Canonical owner 矩阵

下表中的 Core metadata 指逻辑 canonical metadata plane，不预判它是一个物理
文件。候选 A 将这些 owner 放入一个事务数据库；候选 B′ 仅可按各行唯一 owner
放入对应 domain ledger，并为跨 ledger 边提供第九节要求的证明。

| 事实或能力 | vNext canonical owner | 物理落点 | 非 owner 限制 |
| --- | --- | --- | --- |
| Source 定义、格式、接入和 retirement | Capture.SourceRegistry | Core metadata | runtime 不维护第二份 manifest |
| Raw identity、revision、provenance、span、cursor、capture receipt、handoff | Capture | Core metadata | queue/projection 不能补写 Raw |
| Raw、附件和大正文 bytes | PayloadVault | 独立 immutable store | metadata 只存 ref/hash，不内联大 BLOB |
| SourceAuthorityCatalog 和 access binding | Cognition Authority | Core metadata + Vault evidence | caller 不能升级 role/purpose |
| DistillExecutionSpec、prompt/schema/model identity | Knowledge Formation | Core metadata；payload 在 Vault | caller/provider 不改写 execution identity |
| Distill claim、attempt、lease、checkpoint execution state | Workflow | Core metadata；response/checkpoint payload 在 Vault | provider/backend 不自签 terminal |
| Knowledge proposal、trusted decision、semantic knowledge revision | Knowledge Formation | Core metadata + Vault | 是否可把 Wiki 全部降为投影仍待裁决 |
| Observation、Reflection、Persona、Policy、Prediction、Scoring、Decision | Cognition | Core metadata + Vault | KG/ANN/Search/report 只能投影 |
| command、schedule、lease、attempt、retry、defer、dead、compensation intent | Workflow | Core metadata | worker 不能直接写 committed |
| feedback、outcome、supersedes、correction denominator、training obligation | Cognition.FeedbackAggregate | Core metadata | 同事务发出完整 correction obligation；Workflow 只消费 |
| correction command、attempt、target closure | Workflow | Core metadata | 不修改 feedback semantic truth |
| training admission、sample identity/hash/split/exclusion | Cognition.TrainingAdmission | Core metadata + Vault binding | dataset 只能投影 admitted set |
| model run evidence、activation/stale head、retrain obligation | Cognition.ModelLifecycle | Core metadata + Vault binding | trainer/Workflow 不直接切 model head |
| Wiki semantic input、page identity/path/revision、publication journal | adjudication_required | 当前 owner 是 wiki_projection.db；vNext 落点未冻结 | 裁决前禁止在 Core 与 Wiki ledger 双写 |
| KG、Cognitive Graph、ANN、FTS、metrics、MOC、report | Projection owners | rebuildable generation stores | 不反向成为 canonical source |
| projection cohort definition、READY set、active cohort pointer | ProjectionLifecycle.Cohort | Core metadata | consumer 只在 target readback 后提交完整绑定的 READY，不能自行切 aggregate cohort |
| SQLite/file/provider/trainer/notification 真实副作用 | 各 target owner | target-local journal/status | Workflow 只聚合外部证明 |
| writer generation、cutover state | Cutover.WriterAuthority | 独立 fenced control store，不属于任一 engine root | Maintenance 只能调用 typed operation；所有 engine/sink writer 校验 fence |
| subject freeze、deny-read tombstone、no-reingest rule | Kernel.SubjectLifecycle | 原子 metadata island | 各 domain writer 只消费 barrier，不能自建删除真相 |
| backup/restore/migration exact plan 和 epoch | Maintenance | Core + sealed external manifest | 单库 receipt 不能代表全局可恢复 |
| denominator、audit、release evidence | Independent Governance | 独立 evidence store | 不写生产 canonical state |
| CutoverEligibilityRegistry、verification/approval receipt | Independent Governance.CutoverEligibility | 独立 evidence store | WriterAuthority 只消费 exact approved result，不能自签 |

Cutover.WriterAuthority 是 engine-neutral control-plane owner，统一 mint/revoke
G0、G1、G2，并通过短期凭据、OS capability、文件权限或 sink proxy 执行 fence。
D1 必须枚举旧系统每一个 SQLite/file/provider direct writer，证明都经过该边界；
任何无法校验 generation 或可绕过 proxy 的 legacy writer 都阻断切换和回滚。
正常运行时 authority token 使用 append-only
(authority_incarnation_id, sequence)，不随普通 engine backup 倒退。每个可事务
target 在 material mutation 同一事务校验 active incarnation/sequence 和 fencing
lease，并保存最高已接受 token；不可事务 target 必须由受控 proxy/credential
rotation 提供等价拒绝。

control store 灾难恢复不能从 manifest/sink maximum 猜下一个 sequence，因为已
签发但从未触达 sink 的 token 可能不可见。恢复必须在全局 zero-writer 下生成新的
不可复用 incarnation ID 和 signing root，逐一把 required sink/proxy 的 active
trust root 轮换到新 incarnation、明确撤销全部旧 incarnation，并等待旧租约最大
寿命加已冻结 clock-skew 上界后才从新 sequence 开放写入。任一 required sink
无法完成 trust-root rotation 时保持全局写 fence。若 implementation 改用控制库
故障域之外的 durable monotonic issuance service，也必须证明同一“不复用已签发
token”不变量。

每次 mutating admission 和 effect dispatch 还必须持有短租约，绑定
authority incarnation/sequence、rollback_readiness_epoch、capability/target、
lease_id 和 expiry。
domain intent 保存同一 watermark，受控 sink/proxy 在 material send 前再次校验。
进入 ROLLBACK_AT_RISK 时 WriterAuthority 先递增 readiness epoch、停止签发租约，
再等待旧租约过期或进入可枚举的 terminal/uncertain inventory；这不是跨介质
原子事务，但给出了可审计的因果 cutoff。所有进程启动默认 ingress/effect CLOSED，
必须先 reconcile 全部无 terminal observation 的旧 lease/intent，才能签发新的
ROLLBACK_READY 租约。

当前 docs/WIKI_PROJECTION_LIFECYCLE.md:3 将 Wiki Markdown 定义为 mutation 的
业务输入，并把 wiki_projection.db 定义为 page identity、revision 顺序和
consumer completion 的权威账本。因此本文不能先验宣布 Wiki 是纯投影。后继系统
必须先枚举用户编辑、move/delete、trusted publish 和 rebuild 的全部能力，再裁决
semantic input、publication target 与 rebuildable consumer；不能既把 page
lifecycle 移入 Core，又保留旧 ledger 可独立修改。

## 九、读写集与原子性矩阵

| 能力 | Canonical read set | write set | 必须同事务 | 可 pending 的边界 |
| --- | --- | --- | --- | --- |
| Capture 接受 | principal、native identity、Raw head、cursor generation、Vault hash | Raw identity/revision/provenance/span、receipt、cursor/head、handoff/outbox | metadata 六项同一事务 | Vault 先写后引用；未引用 orphan 可清扫 |
| Cognitive commit | expected heads、authority/ACL、immutable input binding | revisions、heads、supersession、event、domain obligation/origin outbox、receipt | 全部同一事务 | Raw/Vault 只能作为 hash-bound immutable precondition |
| Origin → destination | origin outbox | destination inbox + typed command | destination inbox 与 command 同事务 | origin 与 destination 可 at-least-once |
| Workflow claim/finish | command、approval、lease、attempt | lease epoch、attempt、retry/defer、compensation outbox | 每次 transition 与其 outbox 同事务 | target execution 独立；closure 只消费 target observation |
| SQLite target effect | permit、target current row | target mutation + material effect journal | 目标库同一事务 | Core observation 稍后写入 |
| 文件 target effect | exact before hash、intent | PREPARED、temp、atomic publish、readback、COMMITTED | 跨介质无单事务 | durable recovery state machine |
| 外部 API effect | idempotency key、provider status | provider effect + target observation | 无本地事务解 | 无法查询则 effect_uncertain，禁止盲重试 |
| Scheduler tick | schedule revision、due cursor、lease | run ID、typed commands、next due cursor | 同一 Workflow transaction | 实际 command execution 可 pending |
| Recap / 负反馈 | latest feedback head、已提交 effect refs | feedback event、CorrectionObligation、TrainingAdmissionObligation、origin outbox | feedback 与完整 obligation denominator 同一 Cognition transaction | Workflow inbox/command 按 Origin → destination 规则产生；compensation 逐 target 完成 |
| Wiki mutation | expected page revision/hash、semantic input、content ref | 在最终选定的唯一 owner 写 page revision + mutation intent | revision 与 mutation intent 同事务且先于 publication | Wiki file replace/move/delete 是 target effect；按 hash observe/reconcile |
| Projection activation | cohort definition、canonical sequence、previous cohort、required consumer set | 各 target temp generation + READY journal；Core READY observation、watermark、active cohort pointer | READY 只能在 target readback 后写入，并绑定 cohort_id、consumer_id、canonical input root、target generation/root、schema/config/code/model identity、writer epoch；同 READY ID 异绑定冲突；Core 仅在全套 exact READY 后单事务切 pointer | Query 只读一个 active cohort；半成品不激活，缺失/损坏只重建 |
| Training admission | terminal feedback/outcome/prediction chain、authority/privacy/eligibility | admission revision、sample identity/hash/split、exclusion、TrainingRunObligation、origin outbox | admission 与完整 run obligation 同一 Cognition transaction | Workflow inbox/run command 走 Origin → destination；dataset 仅为 projection |
| Training run / model activation | admitted set、immutable run spec、expected model head、Vault samples | trainer effect、Vault artifact/eval、run observation、model head | model head activation 与 exact evidence binding 同一 Core transaction | trainer 是外部 target；unknown 不激活；tombstone 使依赖 model stale |
| Query | principal/ACL、exact commit snapshot、active projection cohort | 无 | read-only snapshot；不得隐式建库、迁移、刷新或写 metrics | 每个 capability facet 预先固定 strong 或 eventual：strong stale 时 fail closed；eventual 返回 typed partial + watermark；Adapter 无权临时选择 |
| Subject freeze/delete | freeze generation、owner generations、dependencies | durable freeze、deny-read tombstone、no-reingest、delete commands | 屏障和命令同一原子岛 | 物理删除和 target compensation 是 saga |
| Writer cutover | active writer、OS/destination fence state | PREPARED、retire G0、fence acknowledgements、activate G1 | 只有 WriterAuthority 状态机是本地原子；OS/destination 不伪装进同一事务 | 允许受控 zero-writer 窗口；绝不允许双 writer |

跨域 pending 只有同时满足以下五点才合法：

1. origin 单独提交后仍是合法业务状态；
2. destination 有稳定 idempotency 和同 ID 异 payload 冲突检测；
3. 公共接口返回 accepted/pending，而不是虚假 committed；
4. 遗漏能由 outbox/inbox/receipt 守恒独立发现；
5. target effect 可以观察或补偿。

缺少任一点，该边就是 same-transaction 候选，不能用“以后 reconcile”代替。

当前代码提供了正反两类参考：

- core/sync_framework/capture_service.py:775–875 当前先写 Raw、再单独 enqueue，
  暴露了后继系统必须消除的 cross-store gap。
- core/cognitive/state_store.py:862–1011 已把 expected heads、revision、event、
  outbox 和 receipt 放在一个 SQLite transaction，是可保留的语义模式。
- core/cognitive/material_effect_ledger.py:29–94 已要求 target mutation 与
  target journal 同事务。
- docs/WIKI_PROJECTION_LIFECYCLE.md:3–17 已区分 mutation、publication 和六类
  required projection receipt。
- core/kia/chronos_scheduler_support.py:99–102 已承认 restart 后 executing 可能
  是 unknown，而不是猜测成功或安全重跑。

## 十、单 Core 与三物理 Ledger 的公平裁决

### 10.1 候选 A：单 MetadataCore

单一物理 metadata transaction，逻辑上仍保留 Capture、Cognition、Workflow
typed interface。Raw 大字节、Wiki 文件、projection 和外部 effect 不进入 Core。

优势：

- 不可分原子岛无需跨库 saga；
- writer generation、subject freeze、delete、rollback envelope 可统一 fencing；
- metadata BackupEpoch 只有一个 root；
- 不需要在多个 owner 间复制 head、receipt 或 authority precondition。

风险：

- 单 writer contention、WAL/checkpoint 和长事务；
- 数据库体积和 backup/restore RTO；
- privacy/retention/failure-domain 隔离可能不足；
- 一个错误 implementation 可能扩大 blast radius。

### 10.2 候选 B′：三个物理主域 Ledger

CaptureLedger、CognitiveLedger、WorkflowLedger；PayloadVault 和 target-owned
effect journals 仍独立。它不是 Kimi 原建议中的巨型 EffectsLedger。

B′ 的候选物理归属必须预先明确：

| B′ store | 唯一 owner family |
| --- | --- |
| CaptureLedger | SourceRegistry、Capture、Raw metadata/cursor/receipt/handoff |
| CognitiveLedger | SourceAuthority、Knowledge Formation、Cognition、Feedback、TrainingAdmission、ModelLifecycle |
| WorkflowLedger | Workflow command/attempt/effect closure、ProjectionLifecycle.Cohort、SubjectLifecycle、Maintenance plan/epoch coordination |
| 各 ledger local tables | 自己的 outbox/inbox、schema marker 和 BackupEpoch marker |
| 外部独立 owner | PayloadVault、Cutover.WriterAuthority、target journals、Governance evidence、sealed GlobalBackupEpochManifest |

这只是供 TransactionGraph 反证的 mapping，不是已证明的拓扑。尤其是
SubjectLifecycle barrier、Projection cohort 和 Maintenance epoch 若不能在
WorkflowLedger 中通过 immutable precondition/fencing 安全约束另外两库，就构成
same-transaction 边并触发最小原子岛合并。Wiki owner 仍按第八节保持
adjudication_required，不能为了凑三库提前归类。

B′ 只有在全部条件满足时才保留：

1. 全功能 read/write census 完成，unknown owner 和 direct writer bypass 为 0。
2. 每个 origin 有事务内 outbox，每个 destination 有事务内 inbox；relay 无
   canonical state。
3. 所有跨域边满足 pending 五条件。
4. 至少一个 ledger 可以独立做 schema migration 或扩缩，并产生可证明的工程
   价值。数据恢复仍从共同 epoch 开始；若只安装某一 ledger 的旧副本，必须用
   其他 ledger 的 immutable outbox/delta data-forward 到当前因果边界，禁止单库
   直接倒退。
5. 现有 producer_consumer_ledger.db 按表和事实重新归属，不能按文件名整体搬迁。
6. Wiki semantic truth 与 target publication truth 完整裁决。
7. writer_generation、ledger_generation、replay_generation 明确区分，destination
   拒绝 retired writer。
8. typed interface 不退化为让 caller 理解 envelope、dedupe、authority 和 target
   类型的通用 submit/claim/finish。

### 10.3 合并为 Core 的触发条件

出现下列任一情况，先合并受影响的最小 atomic island；若传递闭包覆盖 Capture、
Cognition、Workflow 三域，就选择单 MetadataCore：

- public committed 合同要求两个域的 metadata 同时存在且不能降为 pending；
- ACL、subject freeze/delete 或 authorization precondition 可能在检查与提交间
  失效，且违规不可补偿；
- identity、head CAS、receipt、event、outbox 必须同生共死；
- crash window 会形成永久 orphan、重复不可逆 effect 或业务成功但 target truth
  不存在；
- current read 不能替换成 hash-bound immutable precondition；
- 拆分需要复制 canonical state 或制造双 owner；
- 从共同 epoch 恢复后仍无法对某一 ledger 独立做 schema migration/安装，并用
  其他 ledger 的 immutable outbox/delta data-forward 到同一当前因果边界；
- 已冻结的 latency/durability SLO 在跨 ledger common command 上无法满足。

### 10.4 拆分或更换 Implementation 的触发条件

如果单 SQLite Core 在冻结 workload、hardware profile 和产品 SLO 上失败，先区分：

- 若失败来自 SQLite 单 writer、WAL、容量或 RTO，但原子岛不可拆：更换为支持
  同一事务合同的 CoreLedger implementation。
- 若域间没有不可补偿 same-transaction 边，且独立 retention、encryption、
  failure domain 或 scale 确有要求：B′ 才成为合理物理拆分。

不能使用人周、任意 5 秒、现状两倍或“看起来更干净”作为拓扑阈值。SLO 尚未
冻结时，物理拓扑结论保持 blocked。

## 十一、BackupEpoch

共同备份协议必须适用于候选 A 和 B′：

1. 停止 ingress，暂停 relay、scheduler、effect dispatch、Vault GC、retention
   cleanup 和 compaction。
2. 等待 domain transaction 结束；每个外部 in-flight 必须被完整分类为 terminal、
   pending-reconcile 或 effect_uncertain。备份不要求把 uncertain 伪造成 terminal。
3. WriterAuthority 进入 BACKUP_FENCED；普通 writer 全部被拒绝，只授予一个
   single-use maintenance marker generation。Vault 为本次 reachable set 建立
   durable backup pin。
4. maintenance marker 是最后一个 metadata write。它绑定 exact code/runtime
   identity、config/schema/contract hash、frozen capability manifest generation、
   source manifest、完整 target inventory、writer/ledger/replay generation、
   last origin sequence、versioned business-state root、outbox/inbox roots、
   pending/leased/terminal/uncertain counts 和 Vault/file reachability root。
   business-state root 的版本化定义排除 backup marker 本身，避免自引用。
5. 在 marker 后再次确认 writer quiescence，使用 SQLite Backup API；复制
   reachable Vault、配置、authority asset、本地文件和本地 target journal。
   WriterAuthority 只把 signed current incarnation/sequence、issuance high-water
   和 sink high-water observation 封入 manifest 作为证据，普通 restore 绝不能把
   该副本安装到 live authority。
   远端 provider、notification 或 trainer 的真实状态不能被“复制”，只能封存
   Core observation、provider status 和恢复后的 re-observe requirement。
6. 独立验证 integrity、FK、schema hash、outbox/inbox 守恒，并验证：
   每个 payload ref 的 claimed hash 可达 exact bytes；同一 ref 不可变；同一
   envelope ID 不得绑定不同 payload hash。不同 ref 具有相同 content hash 是
   合法 CAS dedupe，不是冲突。
7. 在 copy 后重新枚举 source/WAL/SHM/Vault/file/target inventory、backup pin 和
   writer fence；任何漂移都使本次 epoch 无效。
8. 在唯一 epoch_id 目录内以 PREPARED → MANIFEST_LINKED → COMMITTED 发布：
   temp manifest 用 O_CREAT|O_EXCL 创建并 fsync；final manifest 必须使用
   destination-noreplace 原语发布，例如同文件系统 hard-link（目标存在即失败）
   或经验证的 rename-no-replace，不能使用会覆盖目标的普通 rename；随后 fsync
   epoch 目录，再以 O_EXCL 创建并 fsync COMMITTED marker 和父目录。latest
   pointer 只能是非权威便利索引。Vault pin 与 maintenance fence 保留到
   COMMITTED durable；crash 恢复先处理遗留 PREPARED 状态，不能覆盖旧 manifest。

Manifest 分开给出两个判定：

- restore_eligible：所有 terminal、pending 和 uncertain 状态都被完整保存，恢复
  后可以继续 observe/reconcile；它不要求 uncertain=0。
- epoch_cutover_cohort_eligible：另需 exact cutover cohort 内 required
  uncertain、overdue、dead-letter 和 receipt gap 全为 0；它只是
  CutoverEligibilityRegistry 的一个输入，不能代替全局 cutover_eligible。

Restore 必须保持 writer fenced，在新 root staging，并要求每个 sequence/state
root 精确等于 manifest，不能使用 actual >= H。恢复过程先验证 Vault closure，
再重建 projection；远端 target 必须重新 observe，不能用复制的本地 observation
自签成功。验证完成后通过 PREPARED_RESTORE → ROOT_INSTALLED →
RESTORE_ACTIVATED 的可恢复状态机切换 root，再由 live WriterAuthority mint 一个
严格晚于其 durable issuance high-water 的新 token，并验证全部 sink 已拒绝旧
token；若 authority 本身灾难恢复，则执行前述 incarnation/trust-root rotation。
全部旧 token worker fail closed 后才开放 ingress。required sink 无法提供
high-water 或完成 rotation 时，restore 可以保留为只读，但不得恢复写服务。

只有存在另一个不可变的 (Ebase,Ecut] delta bundle 时，才允许从 snapshot cutoff
之后 data-forward。多个 ledger 的序列数值无需相等，必须相等的是 exact causal
reference、payload binding 和 epoch closure。

单 Core 只简化 metadata root；PayloadVault、本地 target journal、配置、
authority asset 和 external effect observation/reconciliation 仍需相同闭环。

## 十二、不做生产双跑的对等验证

默认且首选方法是“单生产 writer + capture/export 后离线双算”，不是生产双写。

~~~text
旧 Mnemos G0：唯一生产 writer / effect authority
        |
        +-- exact BackupEpoch + canonical outbox/export delta
        |
        +--> isolated legacy replay
        +--> isolated successor replay
                    |
                    +-- state/effect simulation/oracle comparison
~~~

规则：

- 输入尾流优先且默认只能来自 legacy 已有的 canonical outbox/export ledger，或
  连续且守恒的 BackupEpoch delta；事后旁路抓取不能证明完整。若某类输入没有
  canonical 导出来源，该 cohort 先保持 UNOBSERVED 并阻断对等结论，不能仅为比较
  临时改造冻结旧 writer。
- 查询、解析、distill、cognition 和 projection 可在隔离环境对同一 immutable
  input 双算。
- replay bundle 同时绑定输入、模型/provider observation、治理时钟、随机种子/
  identity 和配置；两个 clone 在同一份不完整输入上同时变绿不算对等。
- 写路径只写隔离 clone；notification、Wiki production path、provider、trainer
  等真实 target 使用 fake、recorded observation 或 no-effect adapter。
- 不允许 legacy 和 successor 同时推进生产 cursor、写同一个 Wiki、发同一通知
  或调用同一有副作用 provider。
- 最终切换通过 ingress quiescence、final delta、先 retire 并验证 G0 被所有
  destination 拒绝、再 activate G1 的 fenced transfer 完成；中间允许安全的
  zero-writer 窗口，不是长期 dual-run。

只有某个 exact cohort 因缺少 canonical export 而保持 UNOBSERVED，且独立证据证明
BackupEpoch、outbox/export 和离线恢复均无法补齐该 cohort 时，才允许提出
controlled mirroring 挑战者；它不是默认路径。启用前必须同时满足：

1. exact plan 固定 cohort、入口、字节/顺序/去重合同、开始与结束 watermark、窄时间
   窗、销毁条件、privacy/retention 和 conservation oracle，并由用户批准同一 plan
   bytes/hash；任一绑定变化都要重新批准；
2. G0 始终是唯一生产 writer 与 effect authority；mirroring 只能从 ingress 或已提交
   observation 复制 immutable input，不能让 successor 推进生产 cursor、回写 legacy
   canonical state 或持有生产 writer generation；
3. successor 只写隔离 clone，并对 notification、Wiki production path、provider、
   trainer 等全部真实 target 使用 no-effect adapter；不得发送、发布或训练；
4. start/stop receipt 必须证明窗口已按 plan 开启和拆除、输入完整性与 loss/duplicate
   守恒；窗口外流量仍为 UNOBSERVED，不能外推；
5. controlled mirroring 到期自动 fail closed，不能续成长期双跑，也不能成为切换后
   的兼容写路径。

这种方式保留比较能力，同时避免双写冲突、重复副作用和“两个系统谁是真相”的
治理问题。

## 十三、切换与 data-forward rollback

### 13.1 切换

1. legacy writer generation G0 服务生产；旧仓库冻结功能。
2. 在隔离环境从 immutable Ebase manifest 全量导入 successor。
3. 对 100% capability denominator 执行输入回放、状态守恒、fault injection、
   effect oracle、性能和 restart comparator。
4. 最终停止 ingress 和 legacy effects，quiesce，生成新的 immutable Ecut 和
   exact (Ebase,Ecut] delta；不能覆盖或重命名 Ebase。
5. exact migration plan 绑定 Ecut 与 delta；验证 first-apply conservation、
   same-plan second apply zero、post-gap=0 和 restore drill。
6. WriterAuthority 先按上一节 exact CAS 消费 single-use CutoverPermit，再按
   PREPARED → G0_REVOKED_VERIFIED → G1_MINTED → OPEN 推进。在
   PREPARED 中逐 destination 收集真实 revoke acknowledgement，只有
   G0_revocation_ack_gap=0 才能进入 G0_REVOKED_VERIFIED。在
   每个 dispatch 必须先在 WriterAuthority durable 写 per-destination
   RevokeIntent，绑定 operation_id、destination、expected_active=G0、payload hash
   和 fencing token，再发送；destination 只允许以 expected_active=G0 的 CAS
   revoke，late arrival 在 active generation 已改变时只能返回 stale_noop，绝不能
   撤销新 generation。尚未 durable 记录任何 RevokeIntent 时才可中止并继续 G0；
   一旦首个 intent 落盘，G0 永久 retired，不能用“未收到 ack”推断它仍有效。
   crash 恢复扫描全部 nonterminal intent；任何中止/回退都保持 zero-writer，且在
   所有旧 intent 到达 revoked_g0 或 stale_noop terminal 前不得开放新的 legacy
   recovery generation。随后 mint 新 generation 并逐 sink acceptance，绝不重新
   接受 retired G0，也绝不同时激活 legacy/successor generation。
7. 只有 OS capability 与全部 destination contract 已拒绝 G0，才 mint G1；只有
   全部 destination 接受 G1 后才进入 OPEN 并恢复 ingress。

切换只读取第五节的 CutoverEligibilityRegistry。下列文字只是该 registry 字段的
运行解释，不构造第二套 predicate：

- 数据迁移 gap=0；
- exact cutover-required cohort 中，到 cutoff 应完成的 required command，其
  target receipt、uncertain、overdue、dead-letter 和 projection gap 全为 0；
- cutoff 后未来 schedule 和合同允许的 deferred/pending 作为完整状态迁移，并
  通过 restart/replay，不能为追求 gap=0 伪造 terminal；
- 无 hidden legacy write path；
- production workload、concurrency、crash/restart 通过；
- reverse mapping coverage=100%；
- 未降低任何 release、安全、隐私或独立 verifier 门禁。

### 13.2 回滚窗口

- 保留隔离的 legacy rollback shadow clone；
- successor 在窗口内每个可见 canonical commit 都必须在同一 metadata
  transaction 生成完整 LegacyRollbackEnvelope；target observation 随后通过
  独立 append-only RollbackEffectEnvelope 补齐；
- shadow 禁止生产文件或网络 effect；
- successor target effect 的 terminal/idempotency/status observation 必须可导入
  legacy，避免回滚后重复执行；
- 任一 capability 无法生成 reverse envelope 时，切换本身保持 blocked，不能靠
  在回滚窗口禁用该有效功能取得 100%；
- rollback readiness 是持续计算的状态。effect_uncertain 或 envelope 断裂会使
  WriterAuthority 从 ROLLBACK_READY 进入 ROLLBACK_AT_RISK，立即 fence 新的
  mutating ingress 和 effect dispatch，只保留 read、observe、reconcile 与受控
  maintenance；旧 readiness lease 的完整 inventory 必须逐项 settle。问题被证明
  闭合后才回到 ROLLBACK_READY。若无法闭合，独立 verifier 先生成
  RollbackWindowCloseEvidence，绑定 current commit/E1、authority/readiness
  incarnation/sequence、readiness epoch、未闭合 effect/envelope inventory root、
  target high-water root、原因、
  continuation plan、created_at 和 expires_at；用户对 exact bytes/hash 签发
  绑定 approval principal/scope 的 single-use RollbackWindowCloseApproval。
  WriterAuthority 只有在 ROLLBACK_AT_RISK 时才以 CAS 消费 exact evidence/
  approval hash，并重新匹配 current commit/E1、authority incarnation/sequence、
  readiness epoch、全部 inventory/high-water roots、continuation plan hash、
  approval principal/scope、single-use 状态和未过期时间。任一受控 maintenance
  commit、plan/root 漂移或过期都使 approval stale。全部匹配后才 append
  ROLLBACK_WINDOW_CLOSED、递增 readiness epoch 并允许继续 vNext；未完成该闭环
  前保持安全停写。

### 13.3 真正回滚

1. 停 ingress、relay 和 effects，在 E1 冻结 successor。
2. 生成不可变 (Ecut,E1] delta bundle 和 exact reverse plan。
3. 在 legacy shadow 前滚 delta，并验证 conservation、same-plan second apply
   zero、post-gap=0、target reciprocity 和 100% 功能分母。
4. 任一无法映射的 envelope、effect_uncertain 或缺失 target receipt 都阻断回滚。
5. 在隔离 target root 从 legacy canonical state 重建并验证 Wiki、索引、文件和
   其他本地 target generation；shadow 本身仍不接触生产 target。
6. retire G1 并验证 destination 拒绝后，切换已验证的本地 target generation，
   再 mint legacy G2；绝不复用 G0，且允许受控 zero-writer 窗口。
7. 提升验证后的 shadow；successor 保持冻结以供取证，不删除新数据。
8. 外部 effect 只导入 observation，不能重发；需要撤销时追加显式
   compensation workflow。

这就是 engine rollback、data-forward：回到旧执行引擎，但不把数据倒退到旧时间。

## 十四、必须先做的 falsification prototypes

所有 prototype 只在用户批准后进入独立、无生产凭据、无生产路径的环境；本文不
授权执行。

1. **TransactionGraph**
   - 输入必须是 D0 冻结的 capability manifest ID，不得在 prototype 内另列一个
     更小 surface 清单；EventBus、state/effect sink 和 internal trigger 同样覆盖。
   - 每项记录 read/write set、owner、terminal、failpoint 和 recovery oracle。
   - 通过：unknown owner=0、direct writer bypass=0、每条边为 same_tx 或具备
     pending 五条件。

2. **Capture atomicity**
   - 在 Vault publish、Raw insert、span/catalog、cursor、receipt、handoff、
     outbox、commit 每点 kill。
   - 通过：无 referenced-missing payload；每个 exact native identity +
     source/cursor generation + replay generation 只绑定一个 Raw revision identity、
     capture receipt 和 handoff/outbox；同 key 异 payload 冲突，重复输入 existing；
     只允许未引用 Vault orphan。

3. **Cognition atomicity**
   - 在 expected head、revision、head、event、outbox、receipt 各点 kill。
   - 加入 authority、persona/delivery immutable binding、freeze/delete race。
   - 通过：无 partial commit、无 stale-head success、无 freeze 后派生写。

4. **Workflow/effect**
   - 覆盖 origin commit、relay、inbox、lease、target apply、observe、closure。
   - target 包含 SQLite、filesystem、可查询 external fake、不可查询 external fake。
   - 通过：可查询且有幂等合同的 target 收敛为一个可观察 effect；不可查询结果
     进入 uncertain 并停止自动重试。不得对所有外部 target 宣称 exactly-once。

5. **Target Adapter Qualification**
   - SQLite/file 使用真实 adapter implementation 和隔离 target root；外部 provider
     使用官方 sandbox 或非生产 tenant，以 synthetic data 验证认证、幂等、
     status-query、timeout/unknown、credential rotation 和 generation rejection。
   - fake/no-effect replay 只证明业务编排，不能代签真实协议。没有安全的非生产
     qualification surface 时，该 target 保持 cutover blocker；任何生产 canary
     需要另行 exact 授权，且不能形成第二个生产 writer/effect authority。

6. **Core vs B′ topology**
   - 两候选使用同一业务 trace、数据规模、hardware profile、SLO 和 fault schedule。
   - 比较事务正确性、contention、WAL、backup/restore RTO、隐私删除和 blast radius。
   - 不允许一个候选使用简化能力或更小分母。

7. **BackupEpoch**
   - 在 writer fence、in-flight settle、maintenance marker、SQLite backup、
     Vault/file copy、第二次 inventory、manifest fsync/rename、restore root install
     和 writer_epoch activation 每点 kill。
   - 通过：不完整 epoch 永不 restore-eligible；完整 epoch 精确恢复 terminal、
     pending 和 uncertain；守恒、external re-observe、target reciprocity、
     projection comparator 全部成立。只有 cutover profile 额外要求 uncertain=0。

8. **Data-forward rollback**
   - legacy G0/Ecut → successor G1 → 新数据/effects → E1 delta → legacy
     shadow/local target generation → G2。
   - 通过：reverse mapping=100%、conservation、same-plan second apply zero、
     post-gap=0、target materialization、uncertain-effect 反例 fail closed、
     G0/G1/G2 destination rejection、无数据丢失且无外部 effect 重发；生产 writer
     任一时刻不超过一个，所有 zero-writer 窗口由 fencing 状态机显式记录。

## 十五、分阶段建设，但不分阶段降低终点

建议的 gate 顺序是：

| Gate | 交付物 | 退出条件 |
| --- | --- | --- |
| D0 分母 | requirement、surface、capability、test/oracle 四份 manifest + coverage edges | 新的 freeze-capable schema 实现全部 fixed required-zero predicate；独立 inventory `complete=true`、`pending_families=[]`、exact multiset diff=0；config applicability、constitution、typed adjudication、owner/target 和 oracle 均有 detached typed receipt；exact manifest 经独立复核并获用户批准。v1 `DISCOVERY_ONLY` 不具备退出 D0 的能力 |
| D1 事务图 | canonical owner、read/write set、same_tx/pending 证明 | unknown owner=0，direct bypass=0 |
| D2 骨架原型 | Core 与 B′ 公平 prototype、PayloadVault/target seam | 物理拓扑按同一 SLO/fault trace 裁决 |
| D3 能力簇迁移 | Capture、Formation、Query、Cognition、Workflow、Ops/Governance | 每簇 contract + oracle + failure + data conservation |
| D4 全量演练 | full import、final delta、逐 target adapter qualification、BackupEpoch、restore、data-forward rollback | 填充 CutoverEligibilityRegistry 的全部运行证据字段 |
| D5 切换 | exact reviewed plan 和 writer generation transfer | canonical CutoverEligibilityRegistry=true |

每个能力簇可以独立达到 implemented 或 equivalence_verified，但旧系统不因此
提前下线；只有 D5 才改变生产 writer。

D3/D4 的比较只允许离线 replay、隔离 clone 和第十二节定义的窄范围 controlled
mirroring；D5 前唯一生产 writer 始终是 legacy G0。D5 的 included capability set 与
equivalence-verified set，以及二者的 set root，都必须精确等于获批准的 D0 capability
denominator；任何子集都没有切换资格。

## 十六、仍然阻断架构冻结的问题

1. 204 个 guarded script main、无 guard 但具有 import-time/runpy effect 的脚本、
   tracked shell/setup 入口、facade 之外的 guarded Python main，以及 67 个 facade
   method 的语义归类仍未达到 unclassified=0。
2. 408 个 CLI 参数定义已机械展开为 426 个 leaf-effective facet，但尚未全部完成
   behavior-changing / compatibility-only / governance-only 语义裁决，也尚未映射到
   atomic capability。
3. schema owner manifest 当前是 DDL 文件命中清单，不等同于 target schema 和
   canonical owner census。
4. EventBus wildcard、动态 event name、自由字符串 scheduler task 和可扩展
   module factory 尚未进入 closed registry；现有 21 个 concrete subscription topic
   中有 6 个不在 32 项 policy registry，wildcard consumer identity 还依赖进程内
   `id(self)`，不能作为稳定 D0 ID。
5. 36 个无 direct-main module 中，34 个可由 guarded scripts 静态到达，
   `scripts/__init__.py` 是 package marker；`scripts/wrapper_weekly_report.py` 带
   shebang、import-time I/O 和 runpy 执行，应作为独立入口候选而不是 helper。
6. 单 Core 的 workload、hardware、p99、throughput、WAL、checkpoint、backup
   RTO/RPO 尚未冻结和实测。
7. PayloadVault 的 encryption domain、跨 subject dedupe、key destruction 和
   backup 防复活方案未裁决。
8. Wiki semantic knowledge 与 target publication identity 的迁移边界未裁决。
9. 外部 provider 的 idempotency/status-query 能力尚未逐 target 盘点。
10. rollback window 长度及窗口内被禁止的不可逆 effect 未裁决。
11. successor (Ecut,E1] tail 能否 100% 编码为 legacy rollback envelope 尚未证明。
12. 独立 verifier 已有隔离实现草案且不导入 generator；但 writer site、filesystem/
    Vault、config/keyring、external effect 和 projection activation 等五个反向
    inventory family 仍待实现。v1 固定返回 `complete=false`、`ok=false`，并把五项列入
    `pending_families`；detached verifier 中 `independent_inventory_diff>=1`、
    `independent_inventory_pending_family=5`。exact-byte verification receipt 和最终代码
    归属也未获批准。
13. typed adjudication receipt、canonical owner/target registry 和独立 oracle receipt
    尚未实现；非空状态字符串、任意 `decision_ref`、owner/target 名称或自报
    `independence_class` 都不能清零冻结门。
14. v1 manifest 的 `verification_scope.mode=DISCOVERY_ONLY`、`freeze_capable=false`，且
    `freeze_evaluator_unimplemented=1`。这些缺失机制不能在 v1 内通过任意状态、名称
    或自报 receipt 清零，必须由新的 freeze-capable schema 实现。
15. exact config bytes 只能证明“读了哪份配置”，不能证明该配置适用于冻结范围；
    `config_applicability_attestation_gap=1`。
16. 两条 successor constitution requirement 已存在，但没有 detached exact-byte
    approval；`constitution_requirement_missing=0`、`constitution_approval_missing=1`。
17. 最终产品名尚未选择；名称必须表达 cognition，而不是只表达 memory。

任一项都不能通过“先实现再说”消掉。尤其是能力分母、事务图和 SLO 未冻结时，
不得把单 Core 或三 ledger 写进治理合同。

## 十七、D0 机器账本的当前执行状态

D0 草案已由独立于旧运行时的纯静态生成器产出；生成与校验过程中没有导入或执行
legacy Python，没有启动 daemon、读取默认生产配置、调用真实 API、迁移、重放或修改
生产数据。账本固定绑定：

- legacy commit：`1e36a31a26b0b5baf768815f185d57174e9c59dd`；
- legacy tree：`b1638d73062798bdb033875e0cd6c8ce3b71c301`；
- 当前设计文件的 exact bytes；
- Desktop Phase 0–7 合同 exact bytes，其 SHA-256 为
  `de458fefd424f4b3d0a0db25be3f0656ae840c9b18e4a6026ed09b24fde1d408`；
- 生成器 facade、CLI、package `__init__` 与全部内部实现模块组成的排序 exact file
  set；集合根绑定每个 `(path, sha256, byte_length)` tuple。独立 verifier 还会对这组
  Python bytes 做 AST import-closure 审计：stdlib 之外的 import 必须落在该集合内，动态
  import 一律拒绝，防止把关键逻辑移到未绑定文件；
- legacy tree 的 `ls-tree -z` records 与 `git archive` bytes 都通过 `Popen` 增量消费，
  在进程仍运行时执行固定 record、file、blob、total 与 archive byte 上限；超限立即终止并
  reap 子进程，不能先把无界 stdout 放进内存或磁盘再检查。

正式 artifact family 位于 `docs/acceptance/cognitive_successor_d0/`，由五本 JSONL
账和一份 canonical manifest 构成：

| 账本 | 当前记录数 | 当前含义 |
| --- | ---: | --- |
| requirements | 128 | 126 条旧 Phase/Root challenger 加 2 条 successor constitution requirement；仍不冒充完整产品语义 |
| surfaces | 1,390 | 入口、参数 facet、协议、任务、订阅、脚本和 owner seed 的机械分母 |
| capabilities | 39 | 旧 function matrix 的 capability challenger |
| tests_oracles | 2,100 | 测试、声明式 oracle、运行效果和 release challenger |
| coverage_edges | 426 | 仅保存可由现有精确声明机械证明的候选边 |

当前 manifest 必须保持 `BLOCKED`、`denominator_frozen=false`、
`denominator_approved=false`、`release_eligible=false`。生成器记录 28 个 blocking
finding；结构层面的 duplicate ID/key、invalid record、generator error 和 contract
conflict 当前为零，但这不抵消下列真实缺口：

- `surface_unmapped=1172`，`parameter_mode_unclassified=426`，
  `script_entry_unclassified=73`，`dynamic_trigger_unclassified=3`；
- `freeze_evaluator_unimplemented=1`，`behavior_without_surface=1`，
  `requirement_without_capability_or_adjudication=128`，
  `capability_without_requirement_or_adjudication=39`，
  `capability_without_independent_test_or_oracle=39`；
- `test_without_capability_or_adjudication=1292`，
  `canonical_owner_unknown=148`，`effect_target_unknown=39`；
- `unresolved_adjudication=3309`，`script_parameter_contract_unknown=204`；636 个
  legacy pytest 文件中，286 个仅能由现有声明连接，
  `test_file_without_disposition=350`，`declared_missing_test_file=1`；这些数字不能
  换算成产品功能覆盖率；
- config snapshot 因本轮没有生产配置读取授权而明确 `OMITTED`，所以
  `missing_required_source_binding=1`；冻结前必须由另一次 exact 授权补齐，不能拿
  example config 代签；即使提供 exact bytes，
  `config_applicability_attestation_gap=1` 仍保持阻断；
- qualifier-aware DDL matcher 已修复 `CREATE UNIQUE INDEX` 的 false negative。当前
  reverse census 仍发现旧 schema owner manifest 未列出
  `core/sync_framework/capture_queue.py`、
  `scripts/reconcile_observation_provenance_edges.py` 和
  `scripts/reconcile_raw_index_paths.py`；三项须逐项登记或裁决，不能用旧 regex 问题
  消掉；
- 独立 census v1 尚未枚举 writer site、filesystem/Vault、config/keyring、external
  effect、projection activation 等 required family；报告固定为 `complete=false`，
  并精确列出五项 `pending_families`；
- typed adjudication receipt、canonical owner/target registry、独立 oracle receipt
  尚未实现。已明确接受的 100% 功能分母与旧系统冻结/oracle/rollback 两项原则已编码
  为 `SUCCESSOR-CONSTITUTION-001/002`，其 `contract_status` 为
  `CONTRACTED_PENDING_TYPED_APPROVAL`；因此 `constitution_requirement_missing=0`，但
  `constitution_approval_missing=1`；当前五本账仍只属于 discovery draft；
- MCP tool/schema/policy 均为 57，但 category registry 仅 56，缺
  `session_save`；对 `core/`、`daemon/`、`integrations/`、`scripts/` 与 daemon 入口做
  封闭反向扫描后，EventBus 有 33 条 concrete subscription edge 和 1 条 wildcard
  edge，共 21 个 concrete topic，其中 6 个不在 32 项 policy registry。

独立 verifier 自身也以 facade、audit CLI、package `__init__` 和全部内部实现模块的
排序 exact file set 标识；它已能重算 artifact bytes/hash、封闭 manifest/artifact/closure
wire schema、全局 ID、edge 方向、evidence hash、全部 closure counts 与多类反向
inventory。coverage edge 不再只检查端点和自洽哈希，而是从冻结 function matrix 与独立
AST/registry census 重建完整语义 multiset 后 exact compare；当前诊断没有发现 artifact
bytes、metadata、edge endpoint/direction/multiset、evidence hash、inventory metrics 或
closure 自报不一致。
但 config binding 与 applicability、未完成的 inventory family、真实 schema reverse
gap、缺失的 typed adjudication/owner-target/oracle/constitution approval receipt 都是
fail-closed 阻断项；v1 固定 `complete=false`、`freeze_ready=false`，且尚未产生获批准
的 detached verification receipt，因此 v1 discovery draft 绝不构成 D0 冻结。

哈希链保持单向：设计/合同/legacy/generator exact bytes 进入 manifest，detached
verifier report 再绑定 manifest；设计文件不反向嵌入 manifest hash，避免循环哈希。

## 十八、下一步建议

下一步仍不是写生产系统，也不是提前制作 D1 事务图，而是对 D0 的全部 UNKNOWN、
UNLINKED 和 unclassified 记录逐项裁决：补齐稳定 capability ID、输入/输出/状态/
effect owner、失败恢复和独立 oracle，再修复 generator 与独立反向 census 的 exact
set diff。任何裁决都必须保留原始 challenger 和 decision evidence，不能通过删除
记录、合并统计口径或把 UNKNOWN 改名来缩小分母。

新的 freeze-capable schema 达到全部 required-zero predicate，并证明完整反向
inventory、config exact bytes 与 applicability attestation、constitution exact
approval、typed adjudication、canonical owner/target registry 和独立 oracle receipts
后，才可由独立 verifier 对 exact manifest 签发 detached receipt；用户批准该 exact
hash 后，才开始 transaction graph ledger：为每个 atomic capability 冻结 canonical
owner、read/write set、same_tx/pending、target effect、fault point 和 oracle。D0 与 D1
都闭合后，才运行 Core vs B′ 的公平 prototype，并据此冻结物理拓扑。产品命名可以
并行讨论，但名称不能反向决定数据库或模块边界。

本文与当前机器账本只构成 D0 blocked draft，不构成 architecture frozen、
implementation ready、production ready、release eligible 或 cutover approved。
