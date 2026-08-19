# Wiki 投影生命周期契约

ROOT-20260710-007 将“Wiki 文件写成功”与“所有下游投影已提交”拆成两个可审计阶段。Wiki Markdown 是 mutation 的业务输入；`wiki_projection.db` 是页面 identity、revision 顺序和 per-consumer completion 的权威账本；KG、Cognitive Graph、向量索引、metrics 和 MOC 都是可重建投影。

## 权威状态与不变量

- `wiki_pages` 只保存 stable `page_id` 的 current path/revision pointer；`wiki_mutations` 以全局 `sequence_no` append-only 保存 create/update/move/delete、`parent_revision`、content SHA-256 和 tombstone。
- producer 必须先提交 mutation，再通过 `publish_wiki_page_updated()` 发布携带 `mutation_id/page_id/page_revision` 的 canonical `wiki_page_updated`。只发事件、不记 ledger 的路径不合法。
- required consumers 是 `knowledge_graph`、`cognitive_graph`、`relation_embeddings`、`wiki_search_index`、`wiki_metrics`、`moc_navigation`。每个 consumer 使用稳定 ID 写独立 receipt。
- terminal success 只有 `ack` 和 `noop`。`retry` 保留事件并按持久指数退避重试；`defer` 等待前序 revision 或 trusted-push decision；`dead` 进入 durable DLQ。所有 required receipts terminal 后，`projection_gap` 才为 0。
- move/delete 复用原 `page_id` 并保留 `previous_path`；delete 产生 tombstone。后序 revision 在同页前序 receipt 未闭合时不能越序执行。

## 消费者边界

`daemon/wiki_projection_handlers.py` 以同一份 config 显式绑定 KG DB、Wiki、relation/Wiki embedding index 和 metrics DB。自定义 Wiki、测试或隔离重建不能回落到全局生产路径。

Wiki ANN metadata 的 `id` 是 durable label。缺失、负数、bool、重复、非 deterministic 顺序、chunk 数变化或 memory metadata 缺向量都触发重建；重复 label 的所有 owner 重新嵌入，避免从损坏 label 复用另一页面的 HNSW 向量。小 Wiki 或缺少 hnswlib 时，memory backend 把向量写入 metadata 并在进程重启后恢复。

## COG-050 派生认知投影合同

L2.4 KG、L3 Observation、L4 Reflection 与 L5 Persona 的 Markdown 都属于派生认知投影，但不能因此退化为普通 report 或目录级删除权限。每次全量或增量发布必须经过 `DerivedProjectionLifecycle`：页面 bytes、page role、canonical revision、source refs 与 owned path 集合进入 generation manifest；文件发布前先落 append-only mutation，发布后保存 EventBus trace。全量生成只删除本 lifecycle 既有 active binding 或调用方显式声明的精确 owned path，不能因为文件位于 `L2.4-KG`、`L4-Reflections` 或 Persona history 目录就推断所有权。

回放只允许 read-only canonical facade。Observation、Reflection 与 Entity 回放对象对任何 mutation API 抛出 `PermissionError`；Vault sync 对所有 canonical source（含 `cognitive_graph.db`）比较逻辑 hash，任何漂移直接失败，任一层 error 时禁止 Git commit。Persona calibration 必须先经 Persona persistence owner 提交 `user_confirmed/confirmed_at/calibration_score`，随后从 canonical versions 重放页面；不得直接改写派生 Persona Markdown。自定义 ledger 必须显式绑定同一 `EventBus`，避免 mutation 写到一个库而 required receipts 写到另一个库。

代码与隔离合同门为：

```bash
python3 scripts/audit_cognitive_projection_lifecycle.py --strict --json
```

该命令的 isolated typed-noop consumer 只证明重放、receipt 接口和失败恢复合同，不代表生产 consumer 已处理。生产验收必须显式增加 `--production`，并要求 binding、stale、required-consumer receipt 三类 gap 同时为 0。

## 当前生产边界（2026-07-22）

COG-050 代码与隔离合同已闭合；按用户指令没有执行 `rebuild_wiki_projection_state.py --apply`、COG-050 reconciliation apply 或事件 replay，也没有创建新生产备份。当前 live audit 为 pages 1,182、manifest items 2,014、binding gap 480、stale 480、required-consumer receipt gap 9,204，因此保持 `CODE_CLOSED / RUNTIME_REBUILD_PENDING`。

验证证据保持分层：最终变更相关回归为 `559 passed`；一次隔离 full 为 `7556 passed, 4 failed, 15 subtests passed in 2705.94s`，environment hash=`1b2adc8e375c8ed68921ec4cb9dd0541cbb6019f8a93d73a4fa9ef5aabe5a0f6`、`outside_write_count=0`、`formal_state_diff=[]`。4 项失败均为 EventBus 生成文档或拆分后测试 owner/构造参数断言滞后，修正后精确 `4 passed`、三个相关模块 `103 passed`；按高成本测试策略没有重复 full，故不能把该轮写成全量绿色或发布认证。

主数据库“完整”必须拆成两个独立条件：

1. 物理/事务完整：主文件 `PRAGMA integrity_check=ok`、foreign-key gap=0、canonical schema 通过、最新 migration/generation 没有停在 applying/running；
2. 语义/投影完整：全量、真实增量与隔离 comparator 相等，ANN/receipt/binding/stale gap 全为 0。

2026-07-22 的非备份主库已只读通过第一类检查，但第二类仍等待全链路修复后的统一重建。故现有备份继续受保护：只要状态仍是 `REBUILD_PENDING`，不得把最后一份已验证完整备份纳入空间清理，也不得用未收敛的主库替代恢复基线。

## 重建与回滚

`python3 scripts/rebuild_wiki_projection_state.py --json` 只输出当前快照。真实修复必须先停止 daemon，并显式提供 `--apply --backup-dir <dir> --json`。脚本依次：

1. 备份 KG、Cognitive Graph、metrics、ledger、relation/Wiki ANN 和 Wiki prestate；
2. 以当前 Vault manifest 干净重建所有投影；
3. 通过真实 mutation handlers 做增量 replay，直到文件系统与投影状态饱和；
4. 在隔离目录再次执行增量 comparator；
5. 用 stable business key 比较 SQLite vectors、HNSW labels 和 cosine；
6. 为所有 mutation 写六类真实 rebuild receipt，并要求 reconciliation gap 为 0。

任一 comparator、ANN 语义审计或 receipt 对账失败都返回非零。不得通过清空账本、伪造 receipt、降低 cosine/Wiki budget 或跳过 consumer 来获得成功。

## 2026-07-11 验收快照

- Vault：3,242 pages；ledger：20,366 mutations、122,196 receipts、projection gap 0。
- full/actual incremental 和 isolated comparator 均 `equal=true`。
- relation SQLite/index：8,160 vectors/labels，missing、orphan、duplicate、label mismatch、below-threshold 均为 0；最低 comparator cosine 0.995621215，完整/增量 integrity 最低 cosine 1.0。
- Wiki ANN：11,954 chunks，label mismatch、duplicate、below-threshold 均为 0；最低 cosine 0.999347647。
- 回归：聚焦投影/daemon/KG 测试 150 passed；完整 Quick 5,528 passed、15 subtests passed；mypy 0、Bandit high/medium 0、dependency vulnerabilities 0。

此快照是一次验收证据，不是永久运行态承诺；当前状态应重新运行只读预览、reconciliation 和质量门确认。
