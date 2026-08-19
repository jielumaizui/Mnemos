# ROOT-20260710-001 MCP 可信 Principal 与前置授权设计

> 历史范围说明：本设计关闭 ROOT-001 时公开工具集为 51。ROOT-011 后当前集合为 52/52，本文当时的静态 “full power” 已改名为 `conformance_ok`；当前运行能力证明以 `2026-07-11-agent-runtime-capability-receipts-design.md` 为准。

- 日期：2026-07-10
- 状态：✅ 已实现并验收（代码提交 `ee0fa91f`）
- 审计来源：Desktop `mnemos深度全量审计-2026-07-10.md`
- 对应问题：`ROOT-20260710-001`

## 1. 目标与 Done 定义

修复 MCP 把 caller 提供的业务参数当作身份和授权来源的问题。完成后必须同时满足：

1. MCP principal 只能由服务端在进程启动时从已签发的 launch capability 解析，tool arguments 不能覆盖。
2. 51/51 MCP tools 都经过同一个 tool-policy seam；未登记工具不能执行。
3. `agent`、`allow_cross_agent`、`authorized_agents` 不再是可扩权的公开参数；`session_id`、`project` 只能收窄已有 grant。
4. `wiki_search`、`memory_search`、`context_aware_search`、`session_search`、`wiki_read` 在返回内容或产生副作用前完成授权。
5. ACL metadata 在候选、`SearchResult`、序列化和 direct read 中不可丢失；缺失时 fail closed。
6. 拒绝请求的热度、训练、画像、搜索会话和点击等副作用增量均为 0。
7. 存量 Wiki/raw/index ACL 完成扫描、可证明字段回填和 reconciliation；无法证明来源的项标记 restricted，不伪造 public。
8. 正向、负向、故障、重启和真实 stdio 双主体测试全部通过。

### 1.1 实现与验收证据

- `core/access_policy.py`：`PrincipalEnvelope`、`AccessNarrowing`、严格 ACL envelope、51/51 `MCP_TOOL_POLICIES`、caller identity/ACL override 与 project narrowing。
- `core/agent_kit/authorization.py`、`integrations/mcp_config_security.py`、`integrations/active.py`：hash-only capability/grant、keyring reference、`0600` 配置/备份、prepare → durable config → activate、grant 更新/撤销即时失效。
- `integrations/agora.py` 与 `core/application/*`：每次 tool call 重验 principal，搜索/direct read/写入/recap/predictive/freshness 等均绑定服务端身份；拒绝候选在正文读取和副作用前退出。
- `scripts/reconcile_access_metadata.py`：真实库 7050 项对账为 proven 4258 / restricted 2792 / parse error 0 / unresolved 0；RawIndex 3608 项重建后 dry-run `would_change=0`。
- 测试：专项 227 passed；integration 175 passed；Quick 5436 passed、15 subtests；mypy 0；maintainability failures 0；Bandit high/medium 0；pip-audit 0 vulnerability。
- 运行：8/8 Agent 重新安装并为 full power，真实生产 stdio 8/8 `intent_route` 成功；8 份配置及备份均为 `0600`、reference present、legacy plaintext launch field absent。

## 2. 修复前根因（已消除）

当前身份和授权责任分散在 caller schema、`MCPServer`、Facade、应用服务和结果过滤器中：

- `MCPServer._call_tool()` 直接把业务 arguments 传给 handler，没有 server principal。
- 四类搜索 schema 暴露 `agent`、`allow_cross_agent`、`authorized_agents`；空授权集合还能被解释为不限来源。
- `wiki_read` 不调用 `AccessPolicy`。
- `SearchResult` 丢失 `scope/source_agent/session_id/project/tags`，缺失值随后默认成 public。
- `ContextAwareSearch` 和 Facade 在 ACL 过滤之前写热度、搜索会话、训练及画像信号。
- `AgentAuthorizationStore` 已有用户授权状态，但尚未承担 MCP launch capability 和跨 Agent grant。

这不是缺少第二套安全模块，而是现有 `AccessPolicy` 没有成为唯一授权 seam。修复应深化现有模块并删除旧扩权路径。

## 3. 方案比较

### 方案 A：capability-bound stdio principal（采用）

安装器为每个 Agent/host 签发高熵 launch capability。配置只把 capability 交给该 host 启动的 Mnemos stdio 子进程；服务端在启动时用存储的 hash、状态和 grant 解析一次，得到不可变 `PrincipalEnvelope`。tool arguments 永远不能改变 principal。

优点：兼容现有 stdio host；改动集中；能直接删除 caller 扩权路径。限制：本机 OS 账户完全失陷仍属于系统信任根，不声称抵御同账户任意文件读取和进程注入。

### 方案 B：固定 `--agent` 或环境变量（拒绝）

只把 Agent 名从 tool arguments 移到 CLI/env。改动最小，但任意 caller 仍可自行启动 `mcp serve --agent codex`，没有可信度提升。

### 方案 C：常驻 broker + Unix socket/named pipe（暂不采用）

用 OS peer credential 和 stdio proxy 建立独立 broker。隔离更强，但会同时改变所有 Agent 安装、进程生命周期、跨平台 transport 和故障恢复，超出本 Root 的最小完整修复范围。若未来威胁模型包含同账户恶意进程，再单独设计。

## 4. 模块与 Interface

### 4.1 深化 `core.access_policy`

现有模块成为唯一授权 seam，对外保持两个主要 interface：

```python
authorize_tool_call(
    principal: PrincipalEnvelope,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> AuthorizedToolCall

authorize_item(
    principal: PrincipalEnvelope,
    item: Mapping[str, Any],
    narrowing: AccessNarrowing,
) -> AccessDecision
```

`AuthorizedToolCall` 只包含服务端派生 principal、经过收窄和校验的 arguments、tool policy 及 decision evidence。caller 不能构造 `PrincipalEnvelope`。

### 4.2 深化 `core.agent_kit.authorization`

沿用 `AgentAuthorizationStore`，增加 versioned launch capability/grant 存储，而不是新建平行授权数据库：

```text
capability_id
secret_hash
agent
host_kind
state
capabilities
allowed_projects
allowed_source_agents
issued_at / expires_at / revoked_at
schema_version
```

明文 secret 只在签发时返回给安装器，不写日志、JSON 报告或数据库。比较使用 constant-time hash comparison。撤销、过期、缺库、schema 不兼容均 fail closed。

### 4.3 `PrincipalEnvelope`

```text
principal_id
agent
host_kind
capability_id
capabilities
allowed_projects
allowed_source_agents
source = "server"
issued_at / expires_at
```

`session_id` 和当前 `project` 是请求级 narrowing，不是 principal 身份。跨 Agent 只允许 grant 集合内的具体来源；空集合永远表示没有授权。

### 4.4 统一 MCP tool policy registry

每个已注册 tool 必须有且只有一个 policy：

- `public_metadata`
- `memory_read`
- `memory_write`
- `capture_write`
- `admin_runtime`
- `feedback_write`

`MCPServer._call_tool()` 的第一步是调用 `authorize_tool_call()`；policy registry 与 handler registry 集合不相等时启动和测试均失败。错误返回结构化 deny，不进入 handler。

## 5. 请求与数据流

### 5.1 启动

```text
Agent config
  -> launch capability
  -> MCPServer startup resolver
  -> AgentAuthorizationStore validation
  -> immutable PrincipalEnvelope
  -> JSON-RPC loop
```

未携带、无效、过期或撤销 capability 的 server 不能提供受保护工具；不能回退匿名 full access。

### 5.2 搜索

```text
tool call
  -> tool policy authorization
  -> pure candidate retrieval with complete ACL envelope
  -> per-item authorization
  -> authorized results only
  -> post-authorization heat/training/persona/search-session effects
  -> response
```

`ContextAwareSearch.search()` 不再在候选仍混有未授权项时写副作用。现有副作用实现被移动到一个只接受 authorized results 的内部方法；不是新增旁路。

### 5.3 Direct read

```text
wiki_read(page_path)
  -> normalize path and block traversal
  -> read frontmatter/ACL envelope only
  -> authorize_item
  -> read page content
  -> authorized post-read effects
```

metadata 不存在、解析失败或字段冲突时拒绝读取；不会先读正文再决定。

## 6. ACL metadata 契约

所有候选和 direct-read metadata 至少携带：

```text
scope
source_agent
session_id
project
page_id/event_revision_id
acl_schema_version
acl_metadata_complete
```

路径只能收窄 scope，不能把 frontmatter 的 restricted 项提升为 public。`scope` 缺失时：

- 能从 canonical raw/provenance 证明 owner 的，按证明结果回填；
- 不能证明的，写 reconciliation finding 并按 restricted 处理；
- 不允许以历史目录、空 source 或兼容 fallback 默认 public。

## 7. 迁移与兼容删除

1. 为现有安装生成 host-specific capability，并原子更新各 Agent MCP 配置。
2. 扩展现有授权 store；旧授权记录迁移为无 launch capability 的 inactive record，不能自动获得读取权限。
3. 删除公开 schema 中的 `agent/allow_cross_agent/authorized_agents`。
4. 删除 Agora、Facade、Memory/Intelligence 服务中以这些 caller 参数构造 `AccessContext` 的路径。
5. 保留 `session_id/project` 作为收窄参数，但必须由 `authorize_tool_call` 校验。
6. ACL backfill 先 dry-run 生成 count/category 报告，再对可证明项应用；unknown 保持 restricted。
7. 旧 host 未重新安装时返回明确 `principal_required`，不提供兼容全权模式。

## 8. 错误和恢复

- capability store 不可用、token 不匹配、过期、撤销：deny，handler call count=0。
- tool policy 缺失：server 启动失败或该调用 deny，不能默认 public。
- ACL metadata 缺失/冲突：deny 并记录脱敏 finding，不记录搜索/训练/画像行为。
- post-authorization 副作用失败：读取结果可按既有产品契约返回，但显式列 degraded receipt；不能倒置为授权前写。
- capability rotation：新旧短窗口并存必须有明确 expiry；完成后旧 token 失效。
- 配置更新中断：保留旧可验证 capability 或整体失败，不留下配置/DB 不匹配的假安装状态。

## 9. 测试设计

先写失败测试，再修改实现：

1. `test_mcp_principal_resolver.py`：有效、缺失、伪造、过期、撤销、store unavailable。
2. `test_mcp_tool_policy_registry.py`：51/51 handler-policy 闭合，未知/漏登工具失败。
3. `test_mcp_authorization_boundary.py`：真实 stdio 两主体、伪造业务参数、空 cross-agent grant、project/session mismatch。
4. `test_wiki_read_authorization.py`：direct read、path traversal、metadata 缺失/冲突、同主体与明确 grant。
5. `test_search_acl_projection.py`：`SearchResult` 和 fallback reader 保留完整 ACL。
6. `test_auth_before_side_effects.py`：拒绝路径五类 side-effect delta 为 0；允许路径只记录授权结果。
7. `test_acl_backfill_reconciliation.py`：可证明回填、unknown restricted、幂等重跑、不中途提升 scope。
8. 各 Agent 安装配置测试：每 host capability 唯一，secret 不出现在日志/报告，撤销后不能读取。

测试不得删除原有负向标准、降低 strict gate、增加 broad allowlist，或把缺 principal 改成 warning。

## 10. 验收与证据

专项验收：

```text
python3 -m pytest \
  tests/unit/test_access_policy.py \
  tests/unit/test_agent_authorization_store.py \
  tests/unit/test_mcp_principal_resolver.py \
  tests/unit/test_mcp_tool_policy_registry.py \
  tests/integration/test_mcp_authorization_boundary.py \
  tests/integration/test_wiki_read_authorization.py \
  tests/integration/test_auth_before_side_effects.py -q
python3 scripts/security_audit.py --strict
```

上层验收：

```text
python3 scripts/run_tests.py integration
python3 scripts/run_tests.py quick
python3 scripts/run_local_gates.py
git diff --check
```

必须能机器断言：

```text
principal.source == "server"
caller_expandable_grant_count == 0
tool_policy_coverage == 51/51
acl_metadata_complete == true
unauthorized_side_effect_delta == 0
unknown_acl_public_count == 0
reconciliation_unresolved_count == 0
```

## 11. 深度复审范围

首轮测试通过后，必须重新审查：

- 所有 MCP schema、handler、Facade 和 application service 是否还接受可扩权字段；
- `WikiReader` fallback、raw/session search、preflight/guard 内部检索是否存在 direct bypass；
- 副作用是否在任何 fallback/error path 提前发生；
- capability 是否泄露到命令行、日志、测试快照、诊断或 Desktop 文档；
- 配置更新是否覆盖全部已安装 Agent；
- 拒绝语义是否误伤合法同主体/private/project读取；
- 是否引入兼容 shim、永久 alias 或第二套授权 owner。

发现遗留或回归后继续修复并重跑专项与上层验收，直至复审无新增问题。

## 12. 文档和提交边界

产品代码与测试通过、深审无遗留后先提交代码。随后原位修正：

- repo 中 SECURITY、Agent/MCP、架构、运维、配置与测试相关正式文档；
- Desktop `mnemos系统图谱` 中权限、入口、Agent、链路、配置、测试和代码扫描事实页；
- Desktop 审计报告中 `ROOT-20260710-001` 标题、状态、实际行为、证据、评分、不变量、DAG和附录。

审计标题改为 `### ✅ [P0]...[ROOT-20260710-001]...（已修复）`，正文给出代码、测试、运行、迁移、提交和文档证据，不在文末追加孤立说明。文档验证后独立提交，再开始下一项。
