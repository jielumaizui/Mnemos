# Raw Event Contract

状态：第 1 步验收产物。

本文档定义原始采集层必须能表达的数据字段。可执行定义位于 `core/agent_kit/acceptance_contracts.py` 的 `RAW_EVENT_FIELD_CONTRACTS`；校验入口是 `python3 scripts/verify_acceptance_contracts.py`。

| 字段 | 来源 | 含义 | 必填性 | 缺失降级策略 |
|---|---|---|---|---|
| `source_agent` | `AgentSource.name` 或 `capture_turn.source_agent` | 归一化 Agent id | required | 拒绝记录 |
| `source_kind` | `SessionInfo.source_kind` 或 source metadata | 原生来源格式，如 jsonl/sqlite/trajectory/mcp_turn | required | 置为 `unknown` 并标记非满血 |
| `source_file` | `SessionInfo.source_path` / `Turn.source_files` | 文件型来源路径 | conditional | 仅当 `source_db` 存在时可缺失 |
| `source_db` | SQLite source metadata | 数据库型来源路径或逻辑 DB 来源 | conditional | 仅当 `source_file` 存在时可缺失 |
| `canonical_session_id` | `SessionInfo.canonical_session_id` 或归一化 session id | 合并别名后的稳定会话 id | required | 回退到 `session_id` 并记录 fallback dedupe |
| `session_aliases` | `SessionInfo.session_aliases` / parser metadata | 同一会话的路径或宿主别名 | optional | 空列表 |
| `turn_id` | 原生 event id 或派生 id | 可追踪 evidence id | required | 从 agent/session/turn 派生 |
| `turn_number` | `Turn.turn_number` 或原生顺序 | 会话内 turn 顺序 | required | 拒绝记录 |
| `role` | 原生 role 或 Turn 的 user/assistant 侧 | 重建 raw event 的说话方 | required | paired Turn 映射为 user/assistant |
| `content` | 原生 content 或 Turn content | 当前 role 的原始可见内容 | required | 只有显式 `loss_reasons` 时允许为空 |
| `visible_text` | parser 抽取的人可读文本 | raw 投影和蒸馏入口文本 | required | 标记缺失并阻止自动蒸馏 |
| `tool_calls` | 原生 tool call blocks / `Turn.tool_calls` | 工具调用结构 | required | 空列表 + `completeness.tool_calls=unavailable` |
| `tool_results` | 原生 tool result blocks / `Turn.tool_results` | 工具结果结构 | required | 空列表 + `completeness.tool_results=unavailable` |
| `reasoning_metadata` | 宿主暴露的 thinking 摘要、metadata 或引用 | reasoning 证据，不保存私有思维链 | required | 空值 + `completeness.reasoning=unavailable` |
| `attachments` | 原生附件、媒体、文件上下文或 raw refs | 文件/附件上下文证据 | required | 空列表 + `completeness.attachments=unavailable` |
| `artifact_refs` | `CaptureService.metadata.artifact_refs` 或 source parser artifact refs | 标准化 `mnemos-artifact://` URI，指向工具结果、附件、终端、截图、测试报告等证据 | conditional | 空列表；若有附件/工具结果但无法标准化，标记 `completeness.artifact_refs=unavailable` |
| `created_at` | 原生事件时间或 capture 时间 | 事件创建时间 | required | 回退到 capture 时间并标注 |
| `updated_at` | store 写入时间 | 归一化记录更新时间 | required | 写入时设置 |
| `working_dir` | `SessionInfo.working_dir` / capture metadata | 项目目录 | conditional | 留空，不做项目推断 |
| `project` | `working_dir` 派生或 metadata | 搜索和权限过滤项目 key | conditional | 从目录名派生或留空 |
| `content_hash` | `compute_raw_content_hash` | 文本、工具、附件、metadata 的稳定哈希 | required | 写入前计算；失败则拒绝 |
| `logical_event_id` | source agent + canonical session + turn number | 同一 logical turn 的稳定 alias，只用于 current pointer/metrics 聚合 | required | 无法计算时拒绝写入；不得作为 immutable evidence id |
| `revision_id` | logical event id + content hash | 一组不可变 raw bytes 的稳定证据 id | required | 每次内容变化追加 revision；禁止覆盖旧 snapshot |
| `asset_kind` / `asset_id` | trusted document producer metadata | 把外部文档作为独立资产关联到 raw revision；document id 基于文件 SHA-256 稳定派生 | required for document ingest | 缺失时不得把文档入口报告为 accepted |
| `supersedes_revision_id` | 前一 current revision | revision 有向链；允许旧 citation 精确回查 | conditional | 首个 revision 为空；后续缺失则拒绝更新 current pointer |
| `source_span` | Capture handoff message-local metadata + full-revision `raw_event_refs` catalog | consumer 引用 revision 内与可见 message 等长、role/turn/content hash 一致的非空有序字符范围；chunk input catalog 只能包含实际覆盖的 revision | required for distillation handoff | 缺失、越界、长度/hash/catalog 不一致在 LLM 前失败；chunked page 缺 claim/fragment exact refs 时在物理写入前失败，禁止回退到整 session 来源 |
| `provenance_edge` | Amphora task / Wiki page durable consumer | revision/span 到 consumer 的可审计边，并参与 retention | required for durable derived consumer | edge 写入失败保持 handoff/写页路径失败可重试 |
| `provenance_gap` | reconciliation ledger | 无法证明 revision/span 的历史 consumer | conditional for historical data | 标记 `pending_rebuild`，禁止推断或伪造 edge |
| `observation_terminal` | ObservationEngine / RawProvenanceStore | eligible current Raw revision 对 Observation consumer 的终态：exact revision/span `observation_created` edge，或限定原因的 `intentional_no_observation` | required for canonical Raw Observation consumer | extractor/persist/source-contract failure 不得写 terminal 或推进 cursor，保持 retryable；全可见 Raw 都是 no-observation 会被 readiness budget 拒绝 |
| `observation_cursor` | ObservationStore | 每 source stream 的 durable current-revision cursor；只在对应 terminal receipt 成功后推进 | required for incremental canonical Raw Observation | cursor shape/Raw contract 不匹配时拒绝复用，不能以路径/时间近似跳过 backlog |
| `input_revision` | ordered turn id/content hash fingerprint | 同一 source/session 的输入修订标识，用于区分新增内容与重复投递 | required for distillation handoff | 无法计算时不得把 capture 标为完成 |
| `distillation_handoff_receipt` | `CaptureHandoffStore` + Amphora enqueue receipt | Capture 已被目标 Amphora generation 持久接收的证明 | required before capture `done` | 保持 `handoff_pending`/`retryable_failed` 并暴露错误；不得吞错 |
| `session_end_receipt` | session end receipt store | session end 的 `handoff_pending`/`retryable_failed`/`committed` 状态与目标 receipt | required when ending a session | retryable failure 必须对调用方可见，不得伪装成功 |
| `source_fidelity` | source capabilities + turn completeness | `full`/`derived`/`experimental`/`partial` | required | 置为 `unknown`/`partial`，阻断满血状态 |
| `compression` | source capabilities + RawEventStore | 原生或 Mnemos 存储压缩说明 | required | 置为 `unknown` 并保留 raw bytes |
| `dedupe_strategy` | source capabilities + SyncEngine | 去重规则 | required | 默认 `canonical_session_id+turn_number+content_hash` |

## 通过标准

- 每个字段都有来源、含义、必填性和缺失降级策略。
- `source_file` 和 `source_db` 至少有一个能定位原始来源。
- artifact URI 标准为 `mnemos-artifact://<agent>/<session>/turn/<turn_number>/<artifact_type>[/<index>]`；URI 不应暴露本机绝对路径，路径只可作为内部 metadata 保存。
- `source_fidelity=full` 只能用于可追踪、可重跑、未静默丢上下文的来源。
- `content_hash` 必须覆盖可见文本、工具、附件、reasoning metadata 和 metadata，不能只哈希纯文本。
- 同一 logical turn 的不同内容必须得到不同 `revision_id`；重复相同 bytes 必须幂等返回原 revision；`get_turn(old_revision)` 在 current pointer 更新、投影重建和进程重启后仍返回原 hash/bytes。
- raw projection 和 RawIndex 只可作为显示/候选层。`session_search` 必须先对 metadata-only header 授权，再读取 canonical revision body；投影截断、延迟或删除不得导致 canonical hit 消失，canonical DB 缺失必须显式失败。
- retention metrics 可按 logical event 聚合，但任一 revision 的 durable provenance edge 必须增加 reference retention 并阻止 logical event 物理删除；未解决 gap 不得被当成可安全清理证明。
- Observation 的 canonical Raw 输入必须经只读 typed current-revision API 取得；logical event ID、Raw Markdown 路径、目录 session 或宽松正文 regex 都不能替代 `revision_id`。Markdown 若保留，只能使用 v2 parser/frontmatter 和逐字段 hash parity 审计，不能作为 source failure 的 fallback。
- 每条 eligible current Raw revision 必须由同一运行批次写入 exact Observation edge 或限定原因的 `intentional_no_observation` terminal；提取、存储或 provenance 写入失败保持可重试且不推进 source cursor。readiness 只认可 exact edge/terminal，且 `all_visible_raw_skipped` 不能绿灯。
- 历史 Observation 重放若遗留指向不存在或错误 `source_id` 的 current edge，只能先以 `python3 scripts/reconcile_observation_provenance_edges.py --json` 做零写审计；停止 daemon 后，显式 `--apply --backup-dir <backup-dir> --json` 才可修复。该工具先验证 SQLite backup，再只删除 readiness 也会判无效的 current edge、重算受影响 logical event 的 `raw_metrics.reference_count`、复跑 canonical Raw 审计和 integrity check；它不补造 Observation、terminal receipt 或历史 revision edge。
- Capture 写入 raw store 只是本阶段成功；只有 source/session/input revision 匹配的 Amphora durable enqueue receipt 已提交，capture event 才可进入 `done`。同 session 新 revision 必须得到新的 generation，不得被 session-only 或进程内永久去重吞掉。
- 分块蒸馏的每个 local `DistillInputSpec.source_event_ids` 必须等于该 chunk 的 message-local spans 有序 revision 并集；session aggregate 的 input/output ids 必须等于所有 chunk 的有序并集。create page 使用 producing fragment refs，update/merge/shadow/dispute/reinforcement 使用 claim aggregate origin 还原的 refs；任一路径都不得把全 session refs 伪装成页面级证据。
- trusted document 默认入口只允许 producer 写一次 canonical revision；CaptureWorker/SyncEngine 看到有效 `raw_event_id` 必须复用，raw projection 独占 Obsidian 写入，capture outbox 独占 Amphora。相同文件重复导入必须保持 1 个 revision、1 个 capture event 和 1 个 handoff。
- `capture_session` / session end 的部分成功必须返回结构化 partial receipt；任一目标入队失败时保留可重试状态和错误，不能通过异常吞噬把整个批次报告为成功。
