# core/sync_framework — 通用同步框架

SyncFramework 是 Mnemos **L1 原始层**的核心基础设施。它负责把各 AI Agent 的对话、用户导入的文件等原始素材统一采集、去重、标签化后写入 canonical raw store（`raw_events.db`），再由 daemon 的 `raw_projection` 服务投影到 Obsidian raw vault。

> **设计原则**：插件化、统一出口、统一防重、统一画像。

---

## 目录

- [设计目标](#设计目标)
- [8 步同步流水线](#8-步同步流水线)
- [模块地图](#模块地图)
- [Agent 接入方式](#agent-接入方式)
- [对外接口](#对外接口)
- [测试入口](#测试入口)

---

## 设计目标

1. **插件化接入**：新增一个 Agent 只需实现 `AgentSource` 接口，不改框架代码。
2. **统一出口**：所有原始对话经 `CaptureService` / `SyncEngine` 进入 `raw_events.db`。
3. **统一防重**：一个 SQLite 库管理所有 Agent source 的去重状态。
4. **统一画像**：同步过程中自动采集用户行为信号，供 Persona 层使用。
5. **可靠触发**：支持 watchdog（文件变动）、polling（定时轮询）、hybrid（混合）三种触发策略。
6. **跨阶段可证明**：Capture 只有在 source/session/input revision 匹配的 Amphora durable receipt 提交后才进入 `done`；失败和 partial 保持可重试、可观察。
7. **raw 证据不可变**：logical source/session/turn 只作 alias，正文变化追加 `raw_turn_revisions`；Capture handoff 携带 revision/content hash/span，durable provenance edge 参与 retention。
8. **artifact 身份归系统**：Capture 与完整 Session handoff 都把 artifact 绑定 authoritative Raw revision；蒸馏前由系统按完整 SHA-256 构建 chunk-local catalog，模型只选择 opaque ref。

---

## 8 步同步流水线

`SyncEngine` 处理每个会话/文件时执行以下 8 步：

1. **增量跳过** — 对比 `session_state` fingerprint，未变化则跳过。
2. **噪音过滤** — 剔除空内容、系统提示、过短消息。
3. **内容构建** — 将消息/文件内容转换为统一 `Turn` 列表。
4. **脱敏** — 移除或标记可能的敏感信息。
5. **去重** — 基于 source/session/turn 指纹去重。
6. **标签组装** — 合并 source tag、model tag、extra tags、内容标签。
7. **canonical 写入** — 追加 immutable revision 并更新 logical current pointer；Obsidian raw vault 由 `raw_projection` 服务从 current revision 重建。
8. **信号采集** — 记录用户行为信号到 persona/画像系统。

---

## 模块地图

| 文件名 | 核心类 | 一句话职责 |
|---|---|---|
| `agent_source.py` | `AgentSource`, `SessionInfo`, `Turn`, `SyncResult` | Agent 接入接口契约。每个 Agent 实现 `name`、`model_tag`、`discover_sessions`、`parse_turns`；数据库型来源可覆写 `parse_session(SessionInfo)` 保留已发现的物理库和原生会话 identity。 |
| `sync_engine.py` | `SyncEngine` | 统一同步协调层，执行 8 步流水线，默认写 `raw_events.db` 而不直接写 raw vault。 |
| `registry.py` | `SourceRegistry` | Agent Source 插件注册表与自动发现。 |
| `registry.py` | `AgentLifecycleManager` | Agent 生命周期管理：启动发现、定时刷新、崩溃指数退避重启。 |
| `triggers.py` | `TriggerDispatcher`, `WatchdogTrigger`, `PollingTrigger`, `HybridTrigger` | 触发策略统一抽象：看门狗、轮询、混合。 |
| `capture_service.py` | `CaptureService` | 统一捕获入口，接收 MCP / AgentSource / 文件导入请求，校验并入队。目标耗时 < 200ms，不直接写 L1 storage。 |
| `capture_queue.py` | `CaptureQueue` | SQLite 持久化队列，按来源隔离，daemon 重启后可恢复 pending 任务。 |
| `capture_worker.py` | `CaptureWorkerPool` | 全局 Worker 池，按 `source_agent` 隔离并发，同一 session 内按 `turn_number` 顺序处理。 |
| `capture_handoff.py` | `CaptureHandoffStore` | Capture→Amphora transactional outbox；记录 input revision、目标 task/generation 与 handoff_pending/committed/retryable_failed receipt。 |
| `file_ingestor.py` | `FileIngestor` | 用户文件摄入器，支持 PDF/Word/PPT/Excel/HTML/epub/txt/md。 |
| `storage_backend.py` | `StorageBackend` | 存储后端抽象层，屏蔽 L1 storage 与本地 Obsidian 文件差异。 |
| `agent_path_watcher.py` | — | 针对 Agent 会话目录的 watchdog 封装。 |
| `file_watcher.py` | — | 通用文件监控工具。 |

## Raw 投影闭环

当前 raw 链路是：

1. `CaptureService.capture_turn(...)` 或 `SyncEngine.sync_session(...)` 采集 turn。
2. turn 写入 `~/.mnemos/raw_events.db`，这是完整、可重建的 canonical source。
3. daemon 周期性运行 `raw_projection` 服务；当 `raw_events.db` 或投影参数变化时，`scripts/project_raw_vault.py --apply` 用 event/revision 内容哈希 journal 只原子替换受影响 chunk，绝不搬空或整库复制 raw vault，并确保 raw vault 根目录和 `.obsidian` 标记存在。
4. raw vault 路径结构为 `raw/<agent>/<date>/<chunk>.md`，默认每 5 turn 一个文档。正式 Raw 固定使用 `lossless-visible-v1`：user、assistant、reasoning 与 structured 字段都可见、逐字段 SHA-256 绑定 revision；紧凑展示必须另命名为 Raw Preview，不能以 Raw 名义截断。
5. `raw_index.db` 只更新 changed/deleted projection chunk；首次修复索引也只遍历 publisher-owned projection，不扫描或改写用户自己的 vault note。`scripts/audit_raw_projection_fidelity.py --strict --json` 以只读 canonical Raw 反向核验 event、字段字节和哈希。
6. 历史 `raw-vault-projection-*` 全量备份仅由 `scripts/audit_raw_projection_backups.py --json` 做 metadata/manifest/recovery-value 清单；`storage.disk_budget.raw_projection_backup_total_max_mb` 超线只告警，任何删除都要另行获得明确授权。
7. 历史回填入口 `scripts/backfill_raw_event_store.py` 使用 `SessionInfo.canonical_session_id` 写入 `raw_events.db`，并在 metadata 中保留 `source_session_id`、`session_aliases`、`source_kind`，确保拆分会话和多路径会话可追踪、可去重。
8. Agent source 可在 `SessionInfo.metadata` 写入会话级来源证据；`SyncEngine._raw_source_metadata()` 会先合并这些字段，再叠加 turn metadata。Crush 用它保留 `parent_session_id`、title、message_count 和创建/更新时间等会话树信息。
   `SyncEngine`、daemon、CLI 与 raw-only backfill 均从同一个 `parse_session(SessionInfo)` seam 解析已发现会话：文件型 source 默认回落到 `parse_turns(source_path)`，数据库型 source 必须使用 `SessionInfo` 中的原生 session/database identity，不能依赖跨调用共享队列或“最新会话”猜测。
9. Capture worker 为有序 turn id/content hash 计算 `input_revision`，通过 `CaptureHandoffStore` 持久化 outbox，再调用 Amphora `enqueue_with_receipt()`；只有匹配 receipt durable 后 capture queue 才能 `done`。批量或 session end 出现局部失败时返回 partial/retryable-failed 结果，不吞错。
10. 每个 outbox turn 还携带 immutable `revision_id`、logical alias、content hash 和非空字符 span；Amphora/Wiki durable consumer 写入 `raw_provenance_edges`。历史对账用 `scripts/reconcile_raw_revision_provenance.py` 默认 dry-run；`--apply` 先做数据库备份，只为可证明 refs 写 edge，其余登记 `raw_provenance_gaps.pending_rebuild`。
11. `CaptureHandoffStore` 与 `SyncEngine` 的 complete-session handoff 都把附件、工具结果、reasoning/test artifact 绑定到上述 authoritative revision。复用 revision 必须匹配 source/session/turn/hash，handoff 回读 header hash；malformed refs 不能静默丢弃。`DistillInputSpec v2` 只允许当前 input/chunk 的引用进入 path-free `ArtifactCatalog`，文件现场重算 hash，pathless tool result 对 canonical inline payload 重算；相同 type+内容 hash 跨路径/轮次收敛为同一 ref，模型不能生成 canonical URI/type/hash/ACL，未知或跨 revision 选择 fail closed。

旧的 `ObsidianBackend.save()` 直写 raw vault 路径仍保留为兼容兜底：当 `raw_projection.enabled=false` 或 canonical raw store 不可用时才应使用。默认链路不再依赖它生成 raw vault 文件。

Raw vault 是 canonical Raw 的只读投影，不再作为 daemon 摄入源；原生 Agent 来源统一由 `agent_path_watch` 与 Raw sync owner 处理。

---

## Agent 接入方式

要实现一个新的 Agent source，只需继承 `AgentSource`：

```python
from core.sync_framework.agent_source import AgentSource, SessionInfo, Turn

class MyAgentSource(AgentSource):
    @property
    def name(self) -> str:
        return "myagent"

    @property
    def model_tag(self) -> str:
        return "myagent"

    def discover_sessions(self) -> List[SessionInfo]:
        ...

    def parse_turns(self, session_path: Path) -> List[Turn]:
        ...
```

然后在 `integrations/sources/` 下放置该文件，框架会自动发现（具体注册机制见 `registry.py`）。

---

## 对外接口

- `SyncEngine.sync_source(source)` — 同步某个 Agent source 的全部新会话。
- `SyncEngine.sync_session(source, session_info)` — 同步单个会话。
- `CaptureService.capture_turn(...)` / `capture_session(...)` — MCP 入口，快速入队。
- `CaptureWorkerPool.start()` / `stop()` — 启停后台消费 Worker。
- `TriggerDispatcher.register(source, callback)` — 注册触发器。
- `FileIngestor.ingest(file_path, ...)` — 用户文件摄入。

---

## 测试入口

```bash
# SyncFramework 单元测试
pytest tests/unit/test_sync_engine.py
pytest tests/unit/test_sync_framework.py
pytest tests/unit/test_capture_queue.py
pytest tests/unit/test_capture_service.py
pytest tests/unit/test_capture_worker.py
pytest tests/integration/test_capture_distill_receipts.py
pytest tests/integration/test_pipeline_receipt_reconciliation.py
pytest tests/unit/test_file_ingestor.py
pytest tests/unit/test_registry.py
pytest tests/unit/test_triggers.py

# Agent source 单元测试
pytest tests/unit/test_hermes_source.py
pytest tests/unit/test_claude_source.py
pytest tests/unit/test_codex_source.py
pytest tests/unit/test_kimi_source.py
pytest tests/unit/test_openclaw_source.py
pytest tests/unit/test_opencode_source.py

# SyncFramework 集成/基准测试
pytest tests/integration/test_sync_loop.py
pytest tests/benchmark/test_benchmark_sync.py
```
