# Mnemos 安全说明

本文件汇总 Mnemos 的安全策略、已知风险与迁移指南。

## 1. 设计原则

- **默认无跨 Agent grant**：安装器在没有显式 grant 时只签发 `public_metadata`；跨 Agent/project 读取必须由服务端 `AgentAuthorizationStore` 的具体来源/project grant 授权，caller 参数不能扩权。
- **凭据不落盘明文**：API key 优先通过 `keyring:REF` / `keyref:REF` 引用；无法使用 keyring 时才使用 `env:VAR`，并用 `mnemos secrets doctor` 显式接受 env fallback 风险；`llm_key_pool` 只在内存中轮转，不持久化密钥。
- **MCP principal 服务端绑定**：每个宿主使用独立 launch capability；配置与备份只保存 keyring reference，授权库只保存 capability hash。缺失、过期、撤销、store 不可用或 ACL 不完整时均 fail closed。
- **Daemon 信号必须绑定实例身份**：PID 仅代表 liveness，不能授权 stop。SIGTERM/SIGKILL 前必须核对 OS start token、boot session、executable/command hash 与持久 instance；不匹配或暂不可验证时保持记录并零信号退出。
- **敏感文件最小权限**：`~/.mnemos` 及子目录默认 `700`，敏感文件默认 `600`；CLI/Daemon 入口设置 `umask 077`。
- **拒绝 pickle 与弱加密**：生产代码不使用 `pickle` 反序列化用户数据；旧版 XOR 加密已被移除。

## 2. 当前状态

| 检查项 | 状态 | 说明 |
|--------|------|------|
| pickle 反序列化 | ✅ 已移除 | `scripts/health_check.py` 扫描生产代码 |
| 明文 API key 配置 | ✅ 已拒绝 | `core/llm_config.py` 强制 `env:` / `keyring:` |
| XOR/弱加密凭据 | ✅ 已移除 | `core/credential_pool.py` 已删除；`core/llm_config.py` 强制 `env:` / `keyring:` |
| 敏感文件权限 | ✅ 已加固 | `core/utils.py::secure_directory/secure_file` |
| SQLite 整库加密 | ❌ 已删除 | SQLite 只保留同名明文 `.db`；敏感字段、诊断输出和配置 secret 通过 redaction、secret inventory、`env:` / `keyring:` / `keyref:` 引用控制，不再生成 加密副本 或临时解密副本 |
| 诊断报告脱敏 | ✅ 已加固 | doctor 文本和 health/config/verify/migration 默认隐藏真实 API URL、本机路径和 key source 细节 |
| 仓库敏感字面量 | ✅ 已阻断 | `scripts/audit_repo_sensitive_literals.py --strict` 扫描 tracked 与未忽略 untracked 文本，阻断 provider-shaped fake key、本机 home path 和明文 credential literal |
| keyring/env fallback 诊断 | ✅ 已结构化 | `mnemos secrets doctor` 输出 `mnemos.keyring_doctor.v1`，区分 keyring 可用、env 已接受和 safe-but-not-best warning |
| MCP caller 身份与数据 ACL | ✅ 已服务端绑定 | `core/access_policy.py` + `core/agent_kit/authorization.py`；51/51 tool policy 闭合，direct read 先 ACL 后正文，拒绝路径副作用为 0 |
| MCP host 配置 secret | ✅ 已迁移到 keyring reference | 8 个目标 Agent 的 MCP 配置/备份为 `0600`，旧明文 launch env 不再被生产运行时读取 |
| Daemon PID 复用/旧进程假健康 | ✅ 已实例绑定 | `mnemos.daemon_instance.v1` 同时写入 `0600` PID file 与 heartbeat；status/stop/health 交叉验证 live OS identity、代码/配置/数据库/服务指纹，启动成功前先写当前 heartbeat |
| 跨阶段提前成功/丢失确认 | ✅ 已持久化回执 | Capture、Amphora、Hephaestus 与 recap 使用 revision-aware typed receipts；只有匹配入队和持久化页面/明确跳过回执才允许终态，proposal/partial/retry/write failure 保持可恢复 |
| 发布级隐私安全总门禁 | ✅ 已接入 | `scripts/audit_release_privacy_security.py --strict` 聚合 strict security、strict config doctor、health security、docs sensitive、repo sensitive 和诊断脱敏扫描 |
| SQLCipher 原生加密 | ❌ 不启用 | 当前策略是不做 SQLite 整库加密；若未来重新评估，必须先证明不会产生整库写放大和临时明文泄漏 |
| bandit high/critical | ✅ 清零 | `scripts/security_audit.py` 持续扫描 |
| pip-audit high/critical | ✅ 清零 | CI 与 `scripts/security_audit.py` 运行 |

## 3. 从旧版本迁移

### 3.1 旧 MCP 明文 launch capability / caller 自报权限

升级后运行 `python3 mnemos_cli.py agent install`，为已安装宿主原子轮换 keyring reference。默认只授予 `public_metadata`；需要额外能力时先显式配置 grant，再重装对应宿主：

```bash
python3 mnemos_cli.py agent grant-mcp codex --all-tools --project mnemos
python3 mnemos_cli.py agent install codex
python3 mnemos_cli.py agent kit --json
```

跨 Agent 来源必须逐个使用 `--source-agent` 授权；空列表表示无跨 Agent 权限。`--revoke` 或任何 grant 更新都会立即撤销旧 launch capability，防止运行中的旧进程继续持有过宽权限。旧 `agent/allow_cross_agent/authorized_agents/source_agent` tool arguments 与旧明文 launch 环境字段都会被拒绝，不存在兼容全权模式。

### 3.2 旧版 XOR/明文 API key

如果你此前通过旧版 `core/credential_pool.py` 存储过 `enc:` 或明文密钥：

1. 该模块已移除，`core/llm_config.py` 不再接受明文 key，只接受 `keyring:` / `keyref:` / `env:` 引用。
2. 请重新配置：
   ```bash
   python3 mnemos_cli.py config --set llm.api_key_source=keyring:mnemos/llm
   python3 mnemos_cli.py secrets doctor --json

   # keyring 不可用且 secret inventory 证明 plaintext_count=0 时，才显式接受 env fallback：
   python3 mnemos_cli.py config --set llm.api_key_source=env:MNEMOS_LLM_API_KEY
   python3 mnemos_cli.py secrets doctor --accept-env-fallback
   ```
3. 直接删除旧 `credential_pool.db`（如仍存在）。

### 3.3 旧版 pickle 数据

`core/scoring/adaptive_scorer_v2.py` 在加载模型时会拒绝无安全 meta 的旧 pickle blob，并记录警告。请使用支持的模型格式重新训练或导入。

## 4. 如何报告安全问题

请通过仓库 Issue 或邮件向维护者私下报告安全漏洞，避免在公开渠道泄露利用细节。

## 5. SQLite 数据与磁盘预算监控

Mnemos 不再对 SQLite 做整库加密，也不会把 `.db` 解密/加密为 加密副本。原因是整库 Fernet 包装会在每次打开数据库时制造临时明文副本，并在关闭时整库回写；在 `raw_events.db` 这类高频写入库上会放大磁盘写入并造成 temp 泄漏风险。

当前安全边界改为：

1. API key、token、password、credential 等配置项必须是 `env:` / `keyring:` / `keyref:` 引用，`mnemos.secret_inventory.v1` 只报告字段路径和长度统计，不输出值。
2. 诊断输出默认经 `core.privacy.redaction` 脱敏，本机路径、真实 API URL 和 key source 细节只允许在 `--unsafe-debug` 本机排错时出现。
3. 业务内容或敏感字段进入报告前应由调用方脱敏或以 `****` 替代；不要依赖 SQLite 整库加密承担运行时隐私。
4. `checks.sqlite_disk_budget` 监控 `.db-wal`、Mnemos temp、snapshot 总量/增长率和 `raw_events.db` 总量/增长率。异常会出现在 `python3 mnemos_cli.py health --json`，并被 `checks.auto_healing` 标注 `auto_heal_state`、`repair_actions` 和是否需要用户介入。
5. raw 会话读取先用 canonical metadata 做 `PrincipalEnvelope`/session ACL 鉴权，再解压 immutable revision 正文；RawIndex 投影不得作为权限或证据权威。正式 Amphora/Wiki consumer 的 revision/span edge 会增加 reference retention，存在 edge 或 `pending_rebuild` gap 时不得静默删除原始证据。

处置规则：

- `.db-wal` 超预算：可自愈。运行 `python3 scripts/repair_sqlite_disk_budget.py --apply --wal` 执行 `PRAGMA wal_checkpoint(TRUNCATE)`，再运行 `python3 mnemos_cli.py health --json` 复验。
- Mnemos temp 超预算：只有超过 `storage.disk_budget.temp_stale_minutes` 的 Mnemos temp 文件可自愈删除。运行 `python3 scripts/repair_sqlite_disk_budget.py --apply --temp`。如果年轻 temp 仍在增长，应先停止 Mnemos，确认写入来源，再复验。
- snapshot 超预算或增长过快：需要用户手动确认。先运行 `python3 mnemos_cli.py backup list --json`，确认哪些快照可删；系统不会自动删除 snapshot。
- `raw_events.db` 超预算或增长过快：需要用户手动确认。先暂停高频采集源，确认保留范围，再用 `mnemos data delete --dry-run --scope <scope>` 生成删除计划；系统不会自动删除原始事件。
- raw provenance 对账：先运行 `python3 scripts/reconcile_raw_revision_provenance.py --json`。只有复核备份目录和 gap 数量后才使用 `--apply --json`；无法证明 revision/span 的旧页面必须保持 `pending_rebuild`，不得把 session surrogate 推断成精确引用。

```bash
python3 mnemos_cli.py health --json
python3 scripts/repair_sqlite_disk_budget.py --dry-run
python3 scripts/repair_sqlite_disk_budget.py --apply --wal --temp
python3 mnemos_cli.py health --json
```

## 6. 诊断报告脱敏

`python3 mnemos_cli.py doctor`、`python3 mnemos_cli.py health --json`、`python3 mnemos_cli.py doctor config --strict --json`、`python3 mnemos_cli.py secrets doctor --json`、`python3 scripts/verify_installation.py --json`、`python3 mnemos_cli.py distill status` 和 `python3 scripts/e2e_probe.py --dry-run --no-api` 默认只输出脱敏诊断：API URL 的 host 会替换为 `****`，本机路径形如 `<HOME>` / `<REPO>` / `<PATH>`，key source 形如 `env:****` 或 `keyring:****`。`mnemos.keyring_doctor.v1` 只展示 keyring backend、引用来源计数、`secret_inventory_plaintext_count` 和 `safe_but_not_best` / `env_fallback_accepted` 状态，不展示真实 secret。`python3 scripts/audit_release_privacy_security.py --strict --json` 会把 health/config、`distill status` 和 E2E dry-run 诊断输出再扫一遍，发现真实 URL、本机路径、未脱敏 key source 或 provider-shaped token 时写入 `blocking_findings`。本机私有排错才使用 `--unsafe-debug` 或 `--show-paths`，不要把 unsafe 输出粘贴到 issue、PR 或 agent 上下文。

## 7. 运行安全审计

```bash
# 本地完整安全审计
python3 scripts/security_audit.py --strict --json
python3 scripts/audit_release_privacy_security.py --strict --json

# 单独运行 bandit
python3 -m bandit -r core integrations daemon scripts mnemos_cli.py mnemos_daemon.py -ll -ii

# 单独运行 health check 安全项
python3 -c "from scripts.health_check import check_security; import json; print(json.dumps(check_security(), indent=2, default=str))"
```

`security_audit.py` 输出 `mnemos.security_audit.v2`。Bandit、pip-audit 和 health security
风险先归一化为 typed findings，再由 findings 唯一派生 counts/status/`ok`/退出码；必须满足
`ok == (blocking_count == 0)`。发布级聚合器会再次校验 schema、counts、findings 与返回码，
所以任何 blocking finding 或自相矛盾的安全报告都会阻断发布，warning 则保留为非阻断证据。
