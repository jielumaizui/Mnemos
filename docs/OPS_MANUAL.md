# Mnemos 运维手册

> 版本: v2.0.0 | 最后更新: 2026-07-20

---

## 一、快速诊断

### 1.1 一键健康检查

```bash
cd ~/mnemos
python3 -m core.ops.health_check
# JSON 输出（用于脚本集成）
python3 -m core.ops.health_check --json
python3 mnemos_cli.py health --json
python3 mnemos_cli.py secrets doctor --json
python3 mnemos_cli.py doctor repair --dry-run --json
python3 verify_installation.py --json
```

以上诊断入口默认只读：不存在的目录、SQLite 数据库或表只报告未初始化/blocked，不得在查询过程中 provision、DDL、写 usage metrics 或复用固定探针文件。需要验证目录写权限时显式运行 `python3 scripts/verify_installation.py --write-probes --json`；探针使用唯一文件名与 `O_EXCL`，只删除本次创建的文件。`mnemos status` 和 `mnemos distill status` 直接使用 SQLite `mode=ro`/文件快照，不构造会初始化存储的服务对象；health 的配置快照通过线程隔离作用域传递，不改写进程全局 Config。

测试/门禁必须通过 hermetic runner：

```bash
python3 scripts/run_tests.py quick
python3 scripts/run_tests.py integration
python3 scripts/run_tests.py system
python3 scripts/run_tests.py heavy
python3 scripts/audit_gate_hermeticity.py --suite diagnostics --strict --json --output-dir /tmp/mnemos-diagnostics-hermetic
python3 scripts/run_full_score_gates.py --strict --real-api --output-dir /tmp/mnemos-full-score-release
python3 scripts/verify_full_score_certificate.py /tmp/mnemos-full-score-release/full_score_gates.json
```

`system` 是 GitHub Actions Linux/macOS/Windows matrix 的唯一 system-test 入口。它通过 Python `tempfile` 和 `mnemos.hermetic_run_environment.v1` 建立唯一沙箱，再以 argv 运行 pytest；workflow 不得内联 POSIX env prefix、`mktemp` 或其他 shell 专属替代路径。验收 manifest 必须保持 `outside_write_count=0`、`formal_state_diff=[]`。

每次运行的 `--output-dir` 就是唯一 `sandbox_root`，必须不存在或为空；所有 HOME/Mnemos/database/wiki/XDG/temp/pycache/artifact/log/report 路径都必须位于其下。非 real-api 运行默认没有 API 凭据，real-api full-score 才显式继承。验收 JSON 必须包含非空 `environment_hash`、`outside_write_count=0`、`formal_state_diff=[]`；pytest 还会在每个用例直接阻断正式暂停库、配置、benchmark 和 KG/metrics/projection/ANN 写入。正式 daemon/用户进程并发写造成的运行账本变化不能通过清账、扩预算或伪造 receipt 处理，必须作为独立运行态问题记录。

检查项：进程状态、Obsidian/raw vault、Amphora 队列、recap/reminder 队列预算、EventBus 积压、磁盘空间、LLM/embedding/reranker 配置、可选 multimodal 配置、schema bootstrap、系统级统一契约、runtime producer/consumer closure、golden benchmark 基准状态、安装/升级/卸载生命周期状态、adaptive policy coverage、cognitive readiness 预算、SQLite disk budget、distill JSON quality、distill cognitive actions、wiki route 和 security 运维状态。顶层 `status` 分为 `ok/warning/degraded/failed`；`ok=false` 代表不是完全健康，`usable=true` 代表可用但有非阻断 warning。storage/wiki/agent/disk/api/schema/heartbeat/wiki_route/runtime_producer_consumer/install_lifecycle/amphora/queues/cognitive_readiness/sqlite_disk_budget 属于 strict health checks，当前 service error、Wiki 路由预算超线、runtime producer/consumer required flow 未观测、orphan outputs、no-source consumers、item mismatches、extra consumers、过期持续流或 dead letters 超预算、installed_partial 或未完成 required install step、Amphora failed task、distill failed 超预算、distill processing 超过 stale 预算、high/critical recap pending 超预算、dialog reminder pending/active 超预算、认知就绪度预算失败或 SQLite 磁盘预算超线会进入 `strict_failures` 并返回 degraded。`checks.install_lifecycle` 输出 `mnemos.install_lifecycle.v1`、`incomplete_required_steps`、repair actions 和 `install_lifecycle_state` 错误，不能把 partial setup plan 视为完全健康；若真实 `mnemos setup` 已在 `ActionLedger(action_type=install_setup)` 记录 verified `installed_ready` 状态，health 可用该证据闭环 runtime step，但仍会重新检查当前配置、Vault 和必填模型端点是否 blocked。`checks.sqlite_disk_budget` 输出 `mnemos.sqlite_disk_budget.v1`，列出 `.db-wal`、Mnemos temp、snapshot 和 `raw_events.db` 的体积/增长率；WAL checkpoint 与过期 Mnemos temp 删除属于安全修复，可用 `python3 scripts/repair_sqlite_disk_budget.py --apply --wal --temp`，snapshot/raw_events 删除必须用户手动确认。`checks.multimodal` 不属于 strict，未配置时显示 `skipped` 和恢复动作，配置后显示 `endpoint_status=configured`。`checks.runtime_producer_consumer` 只读输出 `mnemos.runtime_producer_consumer.v2` 摘要，按 `docs/acceptance/adaptive_data_flows.json` 汇总不可变 produced event、generation、intended consumer、append-only receipt、event × intended-consumer coverage、last produced/consumed、pending、freshness 和预算；required flow 的 0/0 是 `unobserved`，不是 green，事件触发型 flow 无事件时允许 N/A。health 不得建表、迁移、注册或补数据；缺库/旧 schema 直接 blocked，并提示执行 `python3 scripts/bootstrap_runtime_producer_consumer_ledger.py`。`checks.security` 属于非 strict 运维安全面，报告敏感目录/配置权限、`mnemos.secret_inventory.v1` 明文 secret-like 字段、旧 credential 行、pickle/weak hash 与 keyring 状态；secret inventory 递归覆盖 `api_key/token/secret/password/credential/bearer/key_source` 并过滤 `token_budget`、`max_tokens`、`tokenizer` 等非密钥字段，输出不得包含 secret 值。`python3 mnemos_cli.py doctor config --strict --json` 是配置/secret/磁盘预算的发布前闭环验收，输出默认脱敏的 `mnemos.config_audit.v1` 到 `~/.mnemos/config_audit.json`；默认 doctor 文本、health/config/verify JSON、`mnemos_cli.py distill status` 和 `scripts/e2e_probe.py --dry-run --no-api` 不得包含真实 API URL、本机绝对路径或未脱敏 key source，只有本机私有排错才使用 `--unsafe-debug` 或 `--show-paths`。

完整运行态验收必须同时满足：`mnemos_cli.py health --json` 的 `ok=true`、`strict_ok=true`、`strict_failures=[]`；daemon PID 存活且 heartbeat 新鲜；`mnemos_cli.py distill status` 中 pending、processing、failed 均为 0；daemon 日志没有持续 `database is locked`、`closed database`、测试临时目录文件蒸馏或重复重型 KG 构建；进程 CPU 在启动收敛后没有持续异常占用；`mnemos_cli.py doctor`、`scripts/verify_installation.py --json` 和 `scripts/e2e_probe.py --dry-run --no-api` 通过基础安装/链路验证。默认 `scripts/verify_installation.py --json` 只代表 basic 验证；当 `results.integration_tests="skipped"`、`skipped_checks=["integration_tests"]` 或 `full_verification_ok=false` 时，不得标记为 full ready；完整安装验收必须运行 `scripts/verify_installation.py --full --json` 并看到真实 `tests_passed` 与 `full_verification_ok=true`。pytest 用例必须通过全局 fixture 隔离 Amphora 队列，禁止把测试临时文件写入用户真实 `distill_queue.db`。2026-07-06 复审后，`WikiMetrics` 这类会读写SQLite 的诊断对象必须在 CLI/doctor 路径显式关闭或使用 transient connection 释放边界；`doctor` 的 Wiki 待复核页判断必须与 health 的 `wiki_route_budgets` 同源，预算内 backlog 只能作为 info；`scripts/verify_installation.py --json` 调用 doctor 的等待预算为 60 秒，30 秒级超时会在 SQLite 并发打开时产生误报。

### Daemon instance identity 与安全重启

`daemon.pid` 不再是整数，而是 `mnemos.daemon_instance.v2` JSON，默认 `0600`；heartbeat schema 为 `mnemos.daemon_heartbeat.v3`。`mnemos daemon status` 与 `checks.heartbeat` 会核对 `instance_id`、PID start token、boot session、executable/command hash、Python、runtime-code fingerprint、配置文件字节 `config_hash`、canonical 有效配置 `config_fingerprint`、database identity 和当前精确 service manifest。仅 commit 变化但 runtime-code fingerprint 相同会显示 `commit_match=false/build_compatible=true`；代码内容、配置文件、env/performance tier 形成的有效配置、数据库、服务清单漂移或 PID reuse 会 degraded/non-zero。

正式 Markdown mutation 的发布验收必须运行 `python3 -m core.trust.static_scan`。v4 报告中的 `site_count` 是 AST 真实调用点分母，`unknown_count`、`registry_stale_count` 和 `known_bypass` 必须为 0；`guarded_trusted_push` 只能由同一控制流中的 typed submission receipt 推导，不能写入 registry 或靠同文件其他 marker 继承。非正式/恢复 sink registry 每项必须有 exact sink ID、owner、target class、expiry、reason；新增或改写 call expression 后 registry 会 stale/unknown 并阻断。正式 write/delete/move 的 commit receipt 必须与 target/content/expected-existing hash 匹配，move 还必须与 source/source hash 匹配；并发修改、串用 receipt 或目标碰撞必须失败且保留源文件。

配置事实源是 `core/config_registry.py` 的 `mnemos.config_registry.v1`，不是 caller fallback 的并集。`Config` 默认拒绝 unknown、removed、alias、错误类型、损坏/非对象 JSON 和非法 performance tier；先用 `mnemos migrate plan --json` 预览全部迁移，再用 `mnemos migrate apply config.stale_keys.v1 --json` 迁移 alias/removed key，不能继续保留生产读取路径。迁移只修改原始持久化文档，canonical 值优先，写入前创建 `0600` backup，并在 ledger 中记录移除、映射、冲突和可安全数值转换。发布前必须同时运行：

```bash
python3 scripts/verify_config_examples.py --strict
python3 scripts/audit_config_registry_closure.py --strict
python3 mnemos_cli.py doctor config --strict --json
```

闭环报告应满足 flattened JSON/YAML leaf、test/doc 覆盖与 registry 相等，`unknown_reader_count=0`、`removed_reader_count=0`、`divergent_fallback_count=0`、`live_config_error_count=0`，并提供非空 `live_config_fingerprint`。新增或重命名配置时必须先改 registry，再改调用方与示例；不得在调用点补第二套默认值。

受控恢复顺序是 `python3 mnemos_daemon.py status` → `python3 mnemos_daemon.py stop` → `python3 mnemos_daemon.py start` → `python3 mnemos_cli.py health --json`。stop 在 SIGTERM 与 SIGKILL 前都重验实例；身份不符或 PID 仍存在但指纹暂不可读时不会发后续信号，也不会删除当前 instance record。旧整数 PID 只在 OS start/executable/command 可证明为 Mnemos daemon 时一次性迁移停止；无法证明时需要人工核对进程，不得直接 `kill`。`start` 只有在新 instance heartbeat 写入后才返回成功，因此返回后 `checks.heartbeat.identity_match` 应立即为 true；完整 health 仍可能因其他 strict check（如 queues）降级。

F25 后，keyring 不可用不再只给泛化 env fallback 提示。`checks.security.keyring` 与 `mnemos secrets doctor` 都输出 `mnemos.keyring_doctor.v1`：必须能看到 `keyring_status`、`keyring_risk_level`、`safe_but_not_best`、`secret_inventory_plaintext_count` 和 `env_fallback_accepted`。只有 `plaintext_count=0` 且运行 `python3 mnemos_cli.py secrets doctor --accept-env-fallback` 或等价设置 `security.accept_env_secret_fallback=true` 后，env fallback 才算显式接受；否则应优先安装/授权 active Python keyring backend 或迁移到 `keyring:` / `keyref:` 引用。

问题 26 起，`checks.auto_healing` 是统一自动愈合编排面。它不会直接吞掉 health warning，而是把每个非 ok check 转成决策卡，状态必须是 `auto_fixed`、`auto_fix_failed`、`needs_user`、`ignored_with_reason` 或 `blocked`，并包含 risk、repair action、rollback plan、verification command 和 `auto_heal.user_intervention_budget`。`mnemos doctor repair --dry-run --json` 输出同一计划；真正 apply 的低风险 handler 必须带 rollback_ref、复验结果并写入 `ActionLedger(action_type=auto_heal)`。带 agent 名称的 `mnemos doctor repair <agent>` 仍只修复 Agent 主动接入。

本机敏感路径默认权限：`~/.mnemos`、`~/.mnemos/configs`、`~/.mnemos/configs/main.json`、database dir、`~/.mnemos/logs` 和 database logs 应分别收敛为目录 `700`、文件 `600`。`Config` 初始化、`scripts/auto_setup.py` 的运行时配置写入和 launchd 日志目录创建都会主动收敛权限；若 health 或 `doctor config --strict` 报 `permission_violations`，按 `repair_actions` 修复后重新运行 `python3 mnemos_cli.py health --json` 与 `python3 mnemos_cli.py doctor config --strict --json`。

队列闭环修复入口：

```bash
python3 mnemos_cli.py distill retry-failed --all
python3 mnemos_cli.py distill reset-timeouts --minutes 30 --json
python3 mnemos_cli.py distill archive-failed --all --reason "已审计且无需重试"
python3 mnemos_cli.py recap list --status pending --severity high --json
python3 mnemos_cli.py recap dismiss --all --severity high --reason "已审计"
python3 mnemos_cli.py reminder status --json
python3 mnemos_cli.py reminder expire-stale --days 30 --json

# 跨阶段回执对账：默认只读；先备份数据库、审阅计划，再显式修复
python3 scripts/reconcile_pipeline_receipts.py
python3 scripts/reconcile_pipeline_receipts.py --apply

# 历史 Amphora source span 对账：先预览；若只剩 Capture→Raw 前置缺口，先执行专用 apply
python3 scripts/reconcile_amphora_source_spans.py --json
python3 scripts/reconcile_amphora_source_spans.py \
  --apply-capture-raw \
  --backup-dir <capture_raw_backup_dir> \
  --expected-capture-raw-manifest-hash <reviewed_manifest_hash> \
  --json

# 再次 dry-run 取得新 inventory hash；仅在 writer 全停、备份目录为空时 apply
python3 scripts/reconcile_amphora_source_spans.py --json
python3 scripts/reconcile_amphora_source_spans.py \
  --apply \
  --backup-dir <backup_dir> \
  --expected-inventory-hash <reviewed_inventory_hash> \
  --json
```

回执对账成功标准是 `reconciliation_gap=0`：Capture 的历史 `done` 必须有对应 handoff receipt，旧 Amphora 无输出的伪 `done` 必须回到可处理状态，recap 不得在页面、proposal decision 或 consumer receipt 缺失时保持 consumed/confirmed。历史 `committed` 页若当前已不存在，只能在 `wiki_projection.db.wiki_mutations` 同一 page identity 证明“任务完成前已有 canonical create/update、完成后发生 canonical move/delete”时恢复 terminal receipt；删除早于任务完成、不同 page identity、损坏账本或无生命周期记录仍须 fail closed。`--apply` 会修改运行库，执行前应受控停止 daemon 并对 `capture_queue.db`、`distill_queue.db`、`recap_tasks.db` 做 SQLite 一致性备份，执行后逐库运行 `PRAGMA integrity_check`、再次 dry-run，并重启 daemon 验证 `checks.amphora.reconciliation_required=0`。队列 backlog 可以让整体 health 因容量预算 degraded，但不能与跨阶段 receipt gap 混为同一个问题。

`reconcile_amphora_source_spans.py` 只修复可由 canonical Raw revision、role-local message、可见正文哈希和 exact span 共同证明的历史任务。缺少 Capture→Raw revision 的任务必须先走专用前置 reconcile；ambiguous、缺消息、哈希不一致或旧 schema 一律留在 blocked/candidate 集合，不能猜 span、伪造 authority 或直接改成 terminal。apply 必须绑定 dry-run inventory hash，使用 SQLite backup API 同时备份队列和 producer/consumer ledger，逐条写 migration provenance；完成后再次 dry-run 应满足 `missing_span_tasks=0`、`candidate_tasks=0`、blocked 为空，并核对备份 manifest、数据库 integrity、任务守恒和 `scripts/reconcile_distill_runtime_receipts.py --json`。source span 对账只恢复可处理输入，不等于模型已蒸馏、Wiki 已写入或 runtime producer/consumer backlog 已清零；真实 drain 必须小批量观察费用、模型合同失败、terminal receipt 和持久化结果，不能用批量状态改写制造绿色。

`reconcile_distill_runtime_receipts.py` v4 必须把 source-span supersession 与普通 committed/intentional-skip/failed terminal 分开。旧 generation 的 runtime terminal 由 intended distillation consumer 的 canonical `skipped` receipt 关闭，同时以 `recorded_by` 保留实际 reconciliation actor；被替代的旧 runtime receipts 只通过同 event/item/generation、显式 `supersession_reason` 和 append-only `supersedes_receipt_ids` 退出 current aggregation。缺少 reason 的旧 v2 successor 必须视为未纠正，由新 idempotency generation 追加 successor 并引用旧 recorder/v2 receipt，禁止原位补 metadata。若旧通用 reconciler 曾把 migration retirement 当成认知 `distill` 完成，则追加 `revoked + reopen_required` receipt，并同时绑定 `supersedes_consumption_id == correction_of_consumption_id`；不得删除历史行、原位改 consumer 或把 replacement 标成 consumed。`distillation_tasks.terminal_outbox_anchor_sha256` 及其 immutable trigger 的唯一 DDL owner 是 `core/kia/amphora.py`；reconciler 只能在已完成 reviewed-plan、停 writer、prepared receipt 和双库备份之后调用该 owner 的 schema helper，迁移脚本本身不得复制 `ALTER TABLE`/trigger DDL。已绑定的非空 anchor 不允许重写；同名但 SQL 不一致的 trigger、缺列或缺 trigger 都必须 fail closed。caught failure 回滚必须把列、trigger、数据和账本一起恢复到原 preimage，不能留下半升级 schema。

v4 dry-run 同时返回可逐条审阅且不含正文的 `reviewed_plan.entries`，生成其 `semantic_plan_sha256`，并另外绑定 queue/ledger DB、WAL、SHM preimage、代码/runtime identity、双库备份范围为 `plan_sha256`；entries 数量、语义 hash 与返回计划必须精确一致，不能只给 opaque hash 要求操作员盲签。历史 Amphora 的无 offset `completed_at` 不得按运行 reconciler 时的宿主时区猜测：缺少显式 `--legacy-naive-timezone <IANA-zone>` 时 lifecycle 证明必须 fail closed；操作员核实历史写入时区后，dry-run 与 apply 必须传相同 IANA zone，它会进入 reviewed semantic plan、plan hash 和 prepared/completed migration receipt。旧 `value_prejudgment_completed` 假 runtime terminal、缺少 `receipt_sha256` 的旧 typed terminal，以及无 task binding 的 cognitive prejudgment/amphora handoff head 都不能原位接受或删除；只有同一 production/item/generation 恰好一个、consumer 为 canonical owner、cognitive event 唯一映射到该 task、且新 task artifact/lifecycle proof 完整时，reviewed entry 才能声明 `append_legacy_supersession`，分别列出 exact runtime receipt 与 cognitive consumption predecessor IDs 和类型化 reasons。apply 追加当前 exact runtime/cognitive heads，通过 `supersedes_receipt_ids` / `supersedes_consumption_id` 退出旧 head。未知、多重、跨 generation、跨 task 或已有不明 supersession 的 terminal 一律 manual/fail closed。apply 必须停止 daemon/Mnemos MCP writer、使用一个新的空备份目录并传回该精确 hash；脚本先以 SQLite backup API 备份 queue 与 ledger，并在任何账本变更前 durable 写入 `prepared` migration receipt，再只为可证明的历史 terminal 生成 pending queue outbox，待 runtime/cognitive exact proof 验证后 CAS 为 committed。所有可捕获异常或最终 `ok=false` 都必须从双备份恢复 queue/ledger、验证完整 conservation，并把 receipt 终态写成 `rolled_back`；绝不能写 `completed + outcome.ok=false`。真实进程硬中断会保留 `prepared` receipt 与备份，由新 preimage plan 在新目录恢复，不能覆盖旧证据。成功 receipt 绑定 reviewed plan、双备份、integrity、conservation 与全部 apply 计数。共享 cognitive event、损坏 JSON、缺失 artifact/lifecycle、未耗尽 retry 或任何身份漂移都保留为 manual，不得合成证明。执行命令：

```bash
python3 scripts/reconcile_distill_runtime_receipts.py \
  --legacy-naive-timezone Asia/Shanghai \
  --json
python3 scripts/reconcile_distill_runtime_receipts.py \
  --apply \
  --backup-dir <new-backup-dir> \
  --plan-sha256 <reviewed-plan-sha256> \
  --legacy-naive-timezone Asia/Shanghai \
  --json
python3 scripts/reconcile_distill_runtime_receipts.py \
  --legacy-naive-timezone Asia/Shanghai \
  --json
```

apply 后要求 migration receipt 为 `completed` 且其 self hash、reviewed plan、双备份和结果字段复核一致，queue/ledger SQLite integrity 均为 `ok`、`conservation.identity_and_status_conserved=true`、runtime/cognitive correction required=0、deferred=0；第二次 dry-run 取得新 preimage-bound plan 后再次 apply，必须 prepared/committed/receipts delta 全为 0。还必须从两份备份各做一次 SQLite restore + integrity drill，再跑 strict runtime closure。`missing_consumers`、pending/overdue 和 cognitive missing 会诚实包含 replacement 尚未真实处理的分母，不能用旧 migration skip 抵消。

### 1.2 CLI 诊断

```bash
# 全面诊断（含来源分布、截断数据、KG 统计等）
mnemos doctor
mnemos doctor --json

# 认知系统就绪度只读审计
python3 scripts/audit_cognitive_readiness.py --json
python3 scripts/audit_cognitive_readiness.py --json --budget
python3 scripts/audit_cognitive_readiness.py --budget --record-gaps
mnemos doctor --cognitive-readiness --json

# 证据回链修复：默认 dry-run，确认后再 --apply
python3 scripts/backfill_wiki_evidence.py --json
mnemos distill evidence-backfill --json
mnemos distill evidence-backfill --apply

# 蒸馏 action 路由日志：只读查看 create/update/merge/dispute/reinforce 的结果
mnemos distill actions --json
mnemos distill actions --session-id <session_id> --json
mnemos distill actions --action-id <action_id> --json

# 认知压缩：默认 dry-run，Raw 删除不属于本命令
python3 scripts/plan_cognitive_consolidation.py --json
python3 scripts/plan_cognitive_consolidation.py --json --record-run
python3 scripts/plan_cognitive_consolidation.py --apply --method-page <method_page>
python3 scripts/plan_cognitive_consolidation.py --submit-run <run_id>
python3 scripts/plan_cognitive_consolidation.py --reconcile-run <run_id> --trusted-proposal-id <proposal_id>

# 投递策略回放：默认使用临时 DB，不污染真实 delivery_events.db
python3 scripts/replay_delivery_decisions.py --json

# 系统级统一契约：认知资产、质量门、能力、隐私、状态、ActionLedger、领域语言、scorecard
python3 scripts/generate_config_examples.py
python3 scripts/verify_config_examples.py --strict
python3 scripts/security_audit.py
python3 scripts/security_audit.py --strict
python3 scripts/security_audit.py --strict-env
python3 scripts/audit_release_privacy_security.py --strict --json
python3 scripts/audit_adaptive_policy_matrix.py --strict
python3 scripts/audit_cognitive_asset_schema.py --strict
python3 scripts/audit_quality_decision_contract.py --strict
python3 scripts/audit_capability_registry.py --strict
python3 scripts/audit_privacy_retention_policy.py --strict
python3 scripts/audit_lifecycle_status_contract.py --strict
python3 scripts/audit_action_ledger.py --strict
python3 scripts/audit_domain_glossary.py --strict
python3 scripts/audit_mnemos_scorecard.py --strict
python3 scripts/audit_module_toggle_registry.py --strict
python3 scripts/audit_cold_start_toggle_matrix.py --strict
python3 scripts/audit_toggle_auto_disable_policy.py --strict
python3 scripts/audit_toggle_output_consumers.py --strict
python3 scripts/audit_data_interface_registry.py --strict
python3 scripts/audit_runtime_producer_consumer_closure.py --strict
python3 scripts/audit_golden_benchmark_contract.py --strict
python3 scripts/audit_install_upgrade_contract.py --strict
python3 scripts/run_golden_benchmark.py --strict --mock-llm
mnemos doctor modules --json

# 产品级安装、升级、卸载入口：默认只读/保留数据；apply 会写 ActionLedger
mnemos setup --dry-run --json
mnemos upgrade plan --json
mnemos doctor repair-all --dry-run --json
mnemos uninstall --preserve-data --dry-run --json
python3 scripts/e2e_install_probe.py --tmp-home
python3 scripts/e2e_upgrade_probe.py --tmp-home --preserve-existing

# 受控求证队列：plan/run 默认不改代码和 Wiki 正文
mnemos verify plan --json
mnemos verify run --json
mnemos verify run --apply --json
```

认知就绪度审计默认不会写入 DB 或 Vault。它用于检查 raw、Wiki metrics、KG/CognitiveGraph、dialog reminder、recap、search click/open/ignore/no_result、delivery/outcome、observation、reflection、policy patch 与 cognitive_consolidation 账本是否足够支撑后续认知系统能力；`mnemos.learning_signal.v2` 会汇总 raw/feedback/search/reflection 到 observation、policy patch/no_patch 证据和 consolidation run 的转化率，`mnemos health --json` 的 strict `checks.cognitive_readiness` 复用同一预算并在失败时降级，非 strict `checks.cognitive_learning` 暴露同一学习信号缺口；`--budget` 按来源、证据、消费者、行为四段预算返回非 0，`--record-gaps` 才会把当前缺口写入 ActionLedger 的 `cognitive_readiness_gap`。来源预算只约束真实知识页：`WikiMetrics.scan_all_pages()` 会把 `page_metrics.page_role` 写为 `knowledge`、`derived_artifact:*`、`system_report:*`、`vault_index`、`generated_placeholder`、`generated_skeleton` 或 `test_artifact`，readiness 只要求 source-required knowledge 页有非空 `source_refs`，并在报告中暴露 `source_required_total`、`source_exempt_total`、`stale_metric_rows`、豁免原因和样本。若 `delivery_events` 或 `cognitive_outcomes` 缺失，表示当前运行态还没有真实投递/outcome 事件或账本尚未被创建，不是脚本失败。

ROOT-013 起，上段最后一种情况不再是“无事发生”：required DB/表缺失、不可读或 schema 不完整必须 blocked；required evidence 已建表但为空、或任一 required lineage 为 0/0，必须 unobserved/degraded 且 score 小于 100。`lineage_coverage` 固定包含 `delivery_to_effect`、`raw_to_observation`、`driver_to_policy_effect`、`consolidation_candidate_to_applied`；每项输出 denominator/covered/uncovered/coverage_ratio、lineage_refs sample、`freshness_at`、`freshness_state` 和 `cold_start_state`。delivery 分母只含真正显示给用户的非 silent delivery，feedback 只计非空 feedback 或双向 event/outcome id 精确匹配；consolidation dry-run 永不算 applied。默认时效窗口为 `cognitive_readiness.freshness_window_seconds=2592000`，过期、坏时间或无法证明来源都保留 gap。完整证据应让 `python3 scripts/audit_cognitive_readiness.py --json --budget` 返回 0；缺失/空/旧/stale/unlinked/dry-run fixture 的被测命令必须返回非 0。

EvidenceBackfill 默认只允许强关系证据类型 `anti_pattern_quote`、`distill_extraction` 参与 source refs，避免把 `keyword_match`、`directory_proximity` 等弱关系误当来源；同时会消费页面已有 frontmatter provenance，把 `来源事件ID`、`来源会话`、`source_session*`、`evidence_refs`、带蒸馏上下文的 `来源/source_agent` 转为 `raw_event:*` 或 `frontmatter:*` refs，不为缺失 provenance 的页面生成伪来源。配置路径在 `~/.mnemos/configs/main.json` 的 `evidence_backfill.*`：`max_refs_per_page`、`frontmatter_ref_limit`、`change_sample_limit`、`unresolved_sample_limit`、`relation_evidence_types`、`write_frontmatter`、`write_report`、`report_dir` 均可调整；临时覆盖可用 `mnemos distill evidence-backfill --max-refs-per-page N --relation-evidence-type TYPE`。

raw revision/provenance 升级先执行 `python3 scripts/reconcile_raw_revision_provenance.py --json`。报告的 `raw_turns/revisions/missing_current_revision` 必须闭合；`provable_edges` 只有页面已持有有效 `revision_id + span` 时才允许写入。显式 `--apply --json` 会先把 `raw_events.db` 备份到配置数据库目录下的 `backups/root004-<timestamp>/`，再写 edge 或 `raw_provenance_gaps.pending_rebuild`，最后执行 integrity check。`pending_rebuild` 不是 pass，也不能通过 session id 猜 span；后续新页面形成真实 edge 后才可 resolve gap。投影丢失/陈旧时 `session_search` 仍应从 canonical revision 返回；canonical DB 不可用必须显式失败。

Observation 的 current Raw edge 出现 dangling consumer、错误 `source_id` 或越界 span 时，使用 `python3 scripts/reconcile_observation_provenance_edges.py --json` 先只读统计。确认 daemon 已停止后，才运行 `python3 scripts/reconcile_observation_provenance_edges.py --apply --backup-dir <backup-dir> --json`：工具会以 SQLite backup API 建立并验证备份、重扫候选以拒绝 drift、在一个事务中只删当前 canonical Raw 的无效 Observation edge，并重算受影响 logical event 的 `raw_metrics.reference_count`。apply 后必须再次 dry-run 为 `status=clean`、`invalid_current_edge_count=0`，并执行 `PRAGMA integrity_check`；旧 revision edge、有效 edge、Observation 行和 typed no-observation terminal 都不在该工具的写入范围内。

Observation 校准绑定 schema 不在构造器里静默升级。旧 `observations.db` 先运行 `python3 scripts/reconcile_observation_calibration_state.py --json` 只读预览；apply 前必须停止 daemon 并指定备份目录，再运行 `python3 scripts/reconcile_observation_calibration_state.py --apply --backup-dir <backup-dir> --json`。该工具使用 SQLite backup API 并验证 integrity，只补 canonical 字段/索引/注册；不完整 pointer 或没有 revision pointer 却覆盖 base 的 posterior 会解除绑定。字段看似齐全但 canonical record 缺失/不匹配的 pointer 不得由迁移器猜测，必须由 strict audit 阻断并通过受控恢复或重新提取处理。旧实现已覆盖 confidence、无法反查 prior 时，行会保守标成 `base_measurement_status=historical_unverified`，不会把 posterior 伪装成已验证 base，也不能绑定新 posterior；只有 canonical 重新提取才能转为 `verified` 并创建 `CalibrationRecord`。apply 后再次 dry-run、执行 `PRAGMA integrity_check`，然后运行 `python3 scripts/audit_cognitive_calibration_lineage.py --strict --json`，要求 schema owner 恰好一处、私有 binder 无旁路、派生重复计权、无 record 校准、hash/pointer/spec/span 漂移、未验证 base 校准、stale 当前绑定、投影 ID/omission gap 和 orphan current record 全部为 0；报告中的 `historical_unverified_base_count` 是待重测分母，不得隐藏。`ObservationStore.clear_all()` 和 retention 会拒绝删除仍绑定 record 的行，必须先完成 coordinated CalibrationRecord retirement。本链路仅使用 `pii_credentials_only_v1` 窄脱敏，不做整库或字段加密。

完整蒸馏内容排查使用 `python3 -m pytest tests/integration/test_lossless_distill_e2e.py -q`：长代码中段、第 4/6 条 shell 命令、编号/空行/首尾格式、附件占位、WikiBuilder 500 字后尾部和 chunk 首/中/尾 sentinel 必须全部存在。极小总预算与单消息预算下 canonical meta 应为 `lossless=true`、`truncated=false`、`silent_omission_count=0`，并只输出 private exclusion 的类型/span/计数；`budget_overflow_tokens` 交给上游 chunking 处理。禁止通过增大阈值、恢复前缀截断或修改断言绕过。检查 `distillation_chunks.db.distill_chunk_results.chunk_info_json` 时，缺少 `input_contract_version` 的记录属于旧有损候选；当前运行必须以 `lossless-visible-v1` 产生不同哈希并重跑，不能手动把旧记录补版本后继续复用。

分块检查点执行规格排查先停止 daemon，再运行 `python3 scripts/reconcile_distill_execution_checkpoints.py --json`；`schema_state=legacy_v1` 或 `legacy_rows>0` 表示记录缺少可信 execution spec，不能直接复用。显式迁移使用 `python3 scripts/reconcile_distill_execution_checkpoints.py --apply --backup-dir <backup-dir> --json`：脚本先执行 SQLite backup 与 `integrity_check`，再事务重建三列主键并核对 row/session/status 计数。迁移只把旧行标为不可复用，不补造 `execution_spec_hash`，对应 session 在下一次真实 retry 时重提取。运行态 `chunk_info` 必须含 `execution_spec_hash`、`prompt_hash`、`schema_hash`、`model_id`、`cache_hit`、`miss_reason`、`spec_diff_fields`，以及 COG-011 的 `input_spec_hash`、`output_contract_version`、`canonical_output_hash`、`output_judgment`；save 和 lookup 都要以完整 `DistillInputSpec` 重跑根 union，`legacy_execution_spec_missing`、`corrupt_execution_spec`、`corrupt_checkpoint_payload`、`execution_spec_changed`、`legacy_root_output_missing`、`legacy_output_admission_missing`、`checkpoint_input_spec_changed`、`checkpoint_output_contract_changed`、`checkpoint_output_contract_invalid` 与 `corrupt_output_admission` 都是正常的 fail-closed miss。验收运行 `python3 -m pytest tests/unit/test_distill_execution_spec.py tests/unit/test_chunk_checkpoint.py tests/integration/test_checkpoint_execution_spec.py tests/integration/test_reconcile_distill_execution_checkpoints.py -q`。

COG-011 的输出准入排查以 `python3 scripts/audit_distill_output_contract.py --strict --json` 为静态入口。每次提取在 Prompt 渲染前生成不可变 `DistillInputSpec`；模型返回的根对象必须通过同一份 `extract.json` Draft 2020-12 union 和 typed runtime validator，检查发生在首次输出进入 correction 前、每次 correction 后、checkpoint 保存前、checkpoint 读取后及正式写页前。合法 skip 只在 `judgment=skip`、`fragments=[]`、`structured_output.distill_intent=skip`、`claims=[]`、非空 `skip_reason` 和至少一条绑定 `source_event_ids` 的 `no_value_evidence` 同时成立时成立；knowledge/skill 的空 fragments 或空 claims 是错误而非 skip。已准入根对象以 canonical root hash、judgment、input-spec hash 和输出契约版本形成 `CheckpointAdmission`；正式写入会再核对 root/hash/judgment/structured output 与当前 `DistillInputSpec`，不得把单独的解析 fragments 或模型猜测的 source agent 当作证明。

COG-013 的 skill 写入排查以 `distill_actions.db` 的 `cognition_asset_commits`、`cognitive_decision_asset_proposals` 和 `cognitive_decision_proposal_attempts` 为证据。顺序必须是完整 cognition asset commit → 可选 proposal → Wiki/action-router receipt → Wiki/search event；`skill_asset_without_cognition` 必须为 0。proposal 失败可以是 `optional_failed`，但不能删除或阻断已经提交的资产和页面；asset commit 失败则 session 保持 retryable，不能 processed。资产 payload 应含全部最终 fragments、chunk aggregate、source spans、private ACL 和 `pii_credentials_only_v1` 计数，尾部证据不得只剩 suggestion 标题。该隐私策略仅脱敏个人隐私、API key/凭据、银行卡和密码，不要求加密。旧 `distill.skill_suggestion_max_chars` 只会被配置加载器忽略并告警，不能恢复截断行为。

COG-010 的正式非 skip 写入必须先在 `producer_consumer_ledger.db` 的 canonical cognitive state schema 中提交 `mnemos.cognition_episode.v2` revision（完整 19 字段、claims catalog/hash、user behavior intent）、对应 `CognitiveDataEvent` 和 `wiki/knowledge_graph/cognitive_graph` 三类 outbox。历史 v1 仅可读取，不能作为新 revision 写入。构造器不会自动建表或迁移；store 缺失/过期、episode root 复验失败、claim catalog 映射/哈希失败或 committed revision 反查失败时，写 receipt 必须是 `retryable_failed/cognition_episode_commit_failed`，且 action/Wiki 零调用。排查先运行 `python3 scripts/audit_distill_output_contract.py --strict --json`，确认四类 golden corpus、57/57 字段、Raw/context mismatch=0 和三个负向 probe 均通过；再检查目标配置的 `database_dir`，不能用全局配置或自定义 Wiki 的 `.mnemos` 目录替代 canonical cognition store。隐私只执行 `pii_credentials_only_v1` 窄脱敏，不启用整库/字段加密。

COG-030 起，committed episode 只由 `CognitionEpisodeDispatch` 发布 ID-only `cognition_episode_committed`；`wiki/knowledge_graph/cognitive_graph` 各自提交稳定 target effect 后才写 reciprocal receipt。排障不能只看 EventBus `done`：同时运行 `python3 scripts/audit_cognitive_event_dispatch.py --strict --json` 和 `python3 scripts/audit_evidence_graph_direction.py --strict --json`，要求 consumer/command/terminal/effect/hash/ACL/omission gap 与 direction finding 全为 0。生产 schema 对账必须先停止 daemon 及 Mnemos MCP writer，运行 `python3 scripts/reconcile_cognition_episode_projections.py --json` 获取 inventory hash；确认五库 backup target 后才使用 `--apply --expected-inventory-hash <hash> --backup-dir <dir> --json`。apply 后必须二次 dry-run 得到 `apply_required=false/actions=[]`，逐库执行 `PRAGMA integrity_check`，且不得把无法证明完整 episode 的历史 fixture 批量伪造成图节点；只能留下 typed intentional omission。

COG-015 的检索验收分为两类证据。仓库/CI/full-score 运行 hermetic `python3 scripts/audit_cognitive_search.py --strict --json`，冻结 36 条正向 query（28 条 holdout）和 7 条跨项目/未知 ACL 负向 query，要求 critical Recall@5=1.0、Recall@10≥0.95、MRR≥0.90、unauthorized=0、field/current gap=0、query order invariant 且 production answer leakage=0。生产验收运行 `python3 scripts/audit_cognitive_search.py --production --strict --json`，另要求 `acl_metadata_missing=0`、`acl_reconciliation_required=0`、`acl_unknown=0`，并逐表核对历史 exclusion ledger；`restricted_unknown` 是 fail-closed quarantine，不计 active page，也不能伪装成已证明 ACL。存量修复先停止 daemon/writer，dry-run `python3 scripts/reconcile_access_metadata.py --target wiki`，确认范围后以新/空目录运行 `--apply --target wiki --backup-dir <backup-dir>`，再 dry-run 和 production strict audit。Wiki apply 会在同一 ACL backup 下生成 `wiki-projection/` 的两库备份：只有 exact lifecycle mutation 与 durable pending event 全部落账才提交文件批次，失败会恢复 SQLite 和 Markdown。若 dry-run 产生无法证明 ACL 的历史对象，还要先运行 `scripts/reconcile_cognitive_search_exclusions.py --target wiki --json`，人工核对两个 inventory hash 后，再以 `--apply --target wiki --expected-inventory-hash <hash> --expected-object-manifest-hash <hash> --backup-dir <new-dir> --json` 写 exact append-only exclusion；未完成这一步时 `acl_reconciliation_required` 不得归零。熵减报告旧 frontmatter 超限时先 dry-run `scripts/reconcile_entropy_report_frontmatter.py`，仅在备份目录明确后 apply；该工具使用同一 lifecycle/event 批次提交，且新报告以 row count/range/hash 和 SQLite digest locator 保留可回查证据，不再把全部 row IDs 塞入 frontmatter。

canonical state 检索还要求 `mnemos.cognitive_search_state_headers.v4`。三条授权入口必须先从 immutable header 与 binding 校验 ACL、scope、object identity 和 revision payload hash，授权后才 hydration `cognitive_state_revisions.payload_json`；binding 首次插入时由 trigger 对照 canonical revision 的 ACL preimage，不能由 caller 自报 hash。旧库先运行 `python3 scripts/reconcile_cognitive_search_state_headers.py --json`；确认 `invalid_current_acl_count=0` 后，停止 daemon/MCP writer，以新目录运行 `--apply --backup-dir <new-dir> --json`，再执行 second dry-run 与 `PRAGMA integrity_check`。state header、Wiki search index、exclusion、ACL 与 entropy apply 都通过同一 offline migration lock 排斥 runtime writer；禁止并发迁移或在读取路径隐式重建。本机 v4 对账结果为 revision=3103、header/binding=1534、typed exclusion=1569，coverage/hash/schema/current gap=0，备份与当前库 integrity 均为 ok。生产 strict audit 此后只剩真实 population 阻断：Wiki=728，cognitive_state/cognitive_graph/evidence_graph=0；这一状态不得写成生产检索已上线。

2026-07-20 的本机 COG-030 对账使用 dry-run inventory `sha256:0cc2ed29b586f01f7e92bb923dee9c1fb2744a94d896925e5eed2afb68f95696`，五库备份目录为 `~/.mnemos/backups/cog030-20260720-0cc2ed29`；apply 后五库 source/backup hash 与 `PRAGMA integrity_check` 均通过。二次 dry-run inventory 为 `sha256:9e0de187911b6abf988293821b0520122095c177dd57de01a1ed3fb5fca46ad7`，`actions=[]`、`apply_required=false`。独立 dispatch audit 的全部 gap 为 0，唯一 episode 是已隔离 synthetic fixture，三个固定 consumer 均为 typed intentional omission；direction audit 为 138,671 nodes、149,668 edges、legacy direction candidate 为空。isolated Quick 为 `6974 passed, 2 skipped, 15 subtests`，environment hash `bd3904f13b34c8ed6c240e16f83f33fd79289fdcf19ef8e0dacd0de7df7b6851`、`outside_write_count=0`、`formal_state_diff=[]`。local gates 仍被历史 `raw_quality_to_distill_gate` 的 1,763 条 overdue pending 阻断；这是独立生产 drain/reconciliation 问题，不是 COG-030 投影 gap，也不能通过补假 receipt 关闭。

DistillActionRouter 在 `distill.structured_output_contract.enforce=true` 时只接管已通过 `distill_output_v4` 根准入且绑定 `DistillInputSpec` 的 action：`create_page` 另须收到 Engine 在 merge/link/quality 受控末段签发的 `FragmentRouteCapability(root_hash,input_spec_hash,object_refs)`，且只接受其中的有序、无重复对象子序列；因此格式化同一对象可写，direct caller 替换片段、缺 capability 或 root/input 绑定漂移一律拒绝。`route_to_dispute` 写 `08-Disputes`；`record_reinforcement` 只更新目标页 frontmatter/metrics，不新建重复页；`merge_into_page`/`update_page` 写入前创建版本备份和 `MergeDecisionCard`，低置信或高冲突样本只落 `07-Shadow/distill-actions`。发布/strict profile 必须同时保持 `distill.structured_output_contract.enforce=true` 和 `distill.action_router.enabled=true`；诊断期关闭任一项不能获得 release eligible 结论。

COG-014 起，高价值 claim 的每个最终 fragment 都必须显式列出非空、无重复 `claim_ids`，union validator 要求 claims 全量且恰好映射，不能用标题/位置猜测。router 将父动作、intent、leased command、append-only attempt、effect 和 consumption 写入 canonical `mnemos.distill_action_store.v2`；只有 Observation/Reflection/PolicyPatch/Relation 目标数据库先写 reciprocal receipt、稳定 effect id 与 before/after hash，action store 独立只读复核后才允许 `applied`。action DB 自签、shadow/proposal 派发正式命令、关闭 router 后直写正式 Wiki、命令参数漂移或 replay 重复 effect 都 fail closed。普通技术事实无动作时正式页标记 `认知动作状态: ordinary_knowledge`。旧 v1 数据先在 daemon 停止状态运行 `python3 scripts/reconcile_cognitive_action_effects.py --json` 预览；确认行数、artifact 与备份目录后使用 `python3 scripts/reconcile_cognitive_action_effects.py --apply --process --backup-dir <backup-dir> --json`。完成后运行 `python3 scripts/audit_cognitive_action_effects.py --strict --json`，要求 `applied_without_effect`、`effect_without_action`、target receipt/state/hash 缺口、dead/nonterminal 和 lineage gap 全为 0。`mnemos distill actions` 与 `mnemos health --json` 均只读，缺库不会创建 schema。

CognitiveConsolidator 默认只做计划，不删除 Raw/Wiki/KG，也不会在普通 dry-run 时创建 `cognitive_consolidation.db`。`--apply` 只冻结方法论页 hash 与每个候选的 exact revision/hash；`--submit-run` 仅创建 trusted-push proposal；人工批准后 `--reconcile-run` 才依次核验可信提交、页面字节、逐候选 exact source ref、Wiki lifecycle mutation 与六类 required consumer receipt，并写入 `consolidation_coverage_receipts`。任一缺口、冲突、重放或页面 hash 漂移均不写 coverage；已覆盖重放幂等。daemon 仅重试已绑定 proposal 的 receipt 核验，不能审批或删除 Raw。Raw 删除必须由独立 DataOwnership 工作流授权。配置路径为 `~/.mnemos/configs/main.json` 的 `cognitive_consolidation.*`：`db_path`、`raw_vault_dir`、`method_pages_dir`、`candidate_limit`、`raw_purge_limit`、`min_key_details`、`max_key_details` 均可调整。

KnowledgeTrustScorer 默认写 `~/.mnemos/trust_decisions.db`，包含 `trust_decisions` 与 `negative_evidence`。`DistillActionRouter` 的 create_page 写 audit-only extraction trust decision，`CognitiveConsolidator` 的 method page 在 apply 覆盖/清理前写 extraction trust decision，update/merge 写 Wiki 前必须有 merge trust decision；MCP `predictive_push` 只返回 delivery decision 为 `deliver` 的候选；`push_feedback(ignore/dismiss/inaccurate/outdated)` 写 scoped negative evidence，其中 inaccurate→contradicted、outdated→outdated。`mnemos proposal push` 是手动 whitebox 决策卡请求，默认绕过 quiet-hour；自动投递方要尊重 quiet-hour 必须显式传 `respect_quiet_hours=True`，无卡时返回 `surface=none`。配置路径为 `~/.mnemos/configs/main.json` 的 `trust.*`：`min_merge_score`、`min_delivery_score`、`min_delivery_task_fit`、`min_guard_score`、`ignore_penalty`、`dismiss_penalty`、`harmful_cooldown_days` 等均可调整，调用方不得硬编码阈值。

`push_feedback` 生产契约是 exact event identity，不是 topic 便利命令：客户端必须提交 `delivery_event_id/topic/action`，并在 predictive push 使用 project/session narrowing 时原样提交同一 scope；服务端以启动时解析的 principal 校验 delivery metadata。`delivery_events.db.feedback_events` 是 append-only command，`feedback_receipts` 是 durable fan-out outbox；required consumers 全 committed 才返回 complete。partial/failed receipt 可由同一幂等命令重试，processing 超过 300 秒 lease 后可重领；`push_penalty.db`、`mnemos.db`、`reflections.db`、`rule_weight_optimizer.db`、`feedback_signals.db`、`trust_decisions.db` 和 `delivery_events.db` 的投影均使用同一 `feedback_event_id` 去重。迁移前备份相关 SQLite；无法唯一绑定 delivery/principal 的历史 topic-only 信号保持 unbound/unknown，不猜测回填。

Recap consumption 使用 `recap_tasks.db` 内的 plan/command/receipt 与 feedback correction outbox，daemon service `recap_consumption` 默认每 60 秒重试 pending、retryable_failed 或 lease 过期的 processing command。`recap_finalize`/`recap_skip` 只有在 required receipt 全部 `committed` 或有原因的 `intentional_skip` 后才返回 terminal；未知 target 直接拒绝。负 feedback 会实际取消 `dialog_reminder`、恢复错误 skip 导致的 task pending、把 policy/retrieval effect 标为 superseded，并写 persona/scoring 补偿。生产升级先停 daemon，运行 `python3 scripts/reconcile_recap_consumption.py --json`；确认 `historical_unknown_count` 和四库备份范围后执行 `--apply --json`，验证 `integrity=ok`，再运行一次 dry-run，必须得到 `schema_changes_required=0`。不得根据旧 target label 伪造 committed receipt。

PolicyPatchStore 默认写 `~/.mnemos/policy_patches.db`，包含 `policy_patches` 与 `policy_patch_feedback`。用户确认的 recap 会由 `RetrospectiveConsumptionRouter.route_after_finalize()` 编译成候选 policy patch，并把 proposed/skipped/error 写入 `recap_consumption_outcomes`；没有明确 trigger 的 recap 只记录 skipped/missing_trigger，不创建全局泛化 patch。`ReflectionPolicyPatchConsumer` 是默认 Layer 5 consumer 之一，会把高置信 Reflection insight/shift 转成候选 policy patch，但生成式 `key_points` 不进入 trigger；不满足置信度或 trigger 条件时写 `policy_patch_feedback:no_patch`。策略补丁只在 `preflight_inject` 与 `guard_check` 中生效：匹配仅使用当前 task/subtype/context，patch content 不参与；非 global patch 要求显式 project scope；候选按 task-fit/命中 trigger 排序、去重后受 `max_active` 干扰预算约束，并返回 why-matched 字段。不会改宿主 system prompt，也不会绕过 DeliveryRouter。配置路径为 `~/.mnemos/configs/main.json` 的 `policy_patch.*`：`enabled`、`db_path`、`ttl_days`、`min_confidence`、`max_active` 均可调整，调用方不得硬编码补丁存活时间或最大注入数量。历史 trigger 清理先运行 `python3 scripts/reconcile_policy_patch_triggers.py --json`；确认后使用 `--apply --json`，工具会先备份 `policy_patches.db`，apply 后必须再次 dry-run 得到 `changed=0` 并执行 SQLite `integrity_check`。

Guard 分析循环守护默认读取 `~/.mnemos/configs/main.json` 的 `guard.analysis_loop.*`。`enabled=false` 会关闭连续纯分析/重复读取提醒；默认 `max_analysis_turns_without_action=2`、`max_repeated_reads_per_target=2`，因此第 2 轮纯分析无行动或同一文件/工具第 2 次重复读取即触发。需要旧式第三次触发时设为 3。`guard_check` 响应、checklist detail 和 `guard_alert` metadata 会返回 `threshold_source`、`threshold_value`、`current_count`，用于核对触发语义和配置来源；这些阈值不得在 MCP handler、Agent 策略文件或测试 fixture 中硬编码为 3。

KnowledgeDeliveryRouter 默认写 `~/.mnemos/delivery_events.db`，包含 `delivery_events` 与 `cognitive_outcomes`。`predictive_push`、`guard_check`、`check_pending_recaps` 和 dialog reminder 会把候选路由到 trust gate、profile 预算、同 topic 冷却和 dismiss/ignore 冷却；`preflight_inject` 的 silent preload 只入账，不消耗也不受可见预算或同 topic 可见冷却阻断；search click/open/ignore/no_result 和 reminder 响应会写 outcome。每条 decision 都记录 `reason`，用于区分低信任、低适配、预算耗尽、降级和冷却。投递偏好在 `~/.mnemos/configs/main.json` 的 `delivery.preference`，默认 `balanced`；可选 `quiet`、`balanced`、`active`。次数、冷却和 reminder 溢出推迟均来自 `delivery.profiles.<profile>`：`daily_total`、`per_task_total`、`per_task_hint`、`per_task_warn`、`force_open_daily`、`same_topic_cooldown_hours`、`dismiss_cooldown_days`、`overflow_defer_hours`，不要在 router、MCP 工具或 reminder 中硬编码。

AdaptiveConfig 默认写 `~/.mnemos/adaptive_config.db`，包含 `usage_metrics`、`config_adaptation_log` 和 `policy_shadow`。全局覆盖矩阵由 `core/kia/adaptive_policy_matrix.py` 生成，并同步到 `docs/acceptance/adaptive_policy_matrix.json`；当前规则覆盖 distill、quality_gate、scoring、delivery、search、raw、document_process、intent 和 cognitive_decision。后台 `daemon/adaptive_service.py` 会从 search/no_result、raw completeness、distill action、delivery feedback、document rejection、stale page 与 scorer feedback 等账本记录 EWMA 指标。运行时消费者只在存在 active shadow 时读取覆盖值；没有 shadow 时继续使用当前调用方配置或默认值，避免全局默认覆盖测试/嵌入式配置。修改规则、指标来源或消费者后运行 `python3 scripts/audit_adaptive_policy_matrix.py --strict`、`python3 -m pytest tests/unit/kia/test_adaptive_policy_matrix.py tests/unit/kia/test_adaptive_config_effective.py tests/unit/test_daemon_adaptive_service.py -q`，并用 `mnemos status` 或 `mnemos health --json` 检查 active_shadow/overdue。

Trusted user document import 统一由 `core/application/document_import_service.py` 和 `core/document_import.py` 承担。`mnemos import <path> --mode parse|capture|distill|watch`、MCP `document_process`、daemon `FileIngestor` 和 KnowledgeInbox 复用路径安全、系统临时目录阻断、raw vault 自身拒绝、隐私预扫描和 `document_process.max_file_size_mb` 大小限制。默认 `mode=distill` 为 trusted_user_document → canonical raw → capture outbox → Amphora → quality gate → Wiki；`mode=capture` 只写 raw 并由 raw projection 生成 Obsidian 视图，`mode=parse` 或旧参数 `write_to_wiki=false` 才只预览。raw projection 是 Obsidian raw vault 唯一 writer；不得恢复 FileIngestor/DocumentProcessor direct backend save 或入口内 direct Amphora。成功结果必须显式包含来源字段、`ingestion_status=accepted`、`handoff_status=pending|existing|not_requested`、`projection_status`、`asset_kind/asset_id`、`raw_revision_id`、`capture_queue_ref`、空的即时 `wiki_paths` 和 ActionLedger ref；下游失败保留 retryable pending，不能回滚 canonical raw。修改该链路后至少运行 dry-run、真实默认栈 integration、`scripts/reconcile_pipeline_receipts.py`、受影响测试、Quick 和 local gates；重复导入必须收敛为 1 revision/event/handoff，reconciliation gap 必须为 0。

Cognitive Decision Flywheel 的主产物是 `cognitive_decision_asset.v1`，不是 automation skill。`core/kia/ixion.py::CognitiveDecisionFlywheel` 负责编排，`core/kia/cognitive_decision_assets.py` 提供资产 DTO、行为生成器、Wiki 候选扫描和资产持久化 mixin；链路会从 Wiki 方法论、重复行为模式和 Skill 失败/新场景中提炼 `asset_type`、证据、适用条件、失败模式和验证 recipe。旧 `SkillWikiFlywheel`、`wiki_to_skill`、`behavior_to_skill`、`skill_to_wiki` 键只作为兼容读取面保留。修改该链路后至少运行 `python3 -m pytest tests/unit/test_cognitive_decision_flywheel.py tests/unit/test_ixion_flywheel.py tests/unit/test_ixion.py tests/unit/test_domain_language_contract.py -q`，并确认 `automation_derivative_allowed=false` 时不会创建 SkillRecord。

VerificationQueue 默认消费 unresolved dispute、active blindspot 和 stale freshness alert，输出 `mnemos.verification_report.v1`。每个 task 必须带 `evidence_refs` 或 `verification_commands`；命令只是提议，不会自动执行。`mnemos verify run` 默认 dry-run，`--apply` 只写 `~/.mnemos/verification_queue.db` 和 data-dir report，不改代码或 Wiki 正文。后台 Chronos 步骤 `verification_queue` 读取 `verification_queue.cron`，并在 `verification_queue.respect_resource_budget=true` 时受 `ResourceBudget` 的 `verification_queue` 服务优先级约束。需要改运行态路径时，用 `verification_queue.db_path`、`verification_queue.report_path`、`verification_queue.blindspots_db_path`，不要改代码里的默认路径。

多模态 evidence 的运行边界是“raw 保全、Wiki 摘要引用”。`CaptureService.capture_turn()` 会把完整采集 artifact、reasoning artifact、工具结果和附件写成 `metadata.artifact_refs`，URI 格式固定为 `mnemos-artifact://<agent>/<session>/turn/<turn_number>/<artifact_type>[/<index>]`；URI 不应包含本机绝对路径，路径只可保存在内部 raw metadata。蒸馏输出可在 claim evidence 上引用 `artifact_uri/artifact_type/artifact_summary`，但不能省略 `source_event_id` 和短 quote；Wiki 来源追踪正文只渲染摘要链接，不直接嵌入截图、终端全文或测试报告正文。

系统级统一契约由 `core/system_contracts.py`、`core/module_toggles.py`、`core/ops/cognitive_data_contract.py`、`core/ops/producer_consumer_ledger.py`、`core/migrations/registry.py`、`core/backup/snapshot_manager.py`、`core/privacy/data_ownership.py`、`core/benchmarks/golden.py` 和 `core/setup/install_lifecycle.py` 提供，health 中对应 `checks.system_contracts`、`checks.module_toggles`、`checks.runtime_producer_consumer`、`checks.migrations`、`checks.backup`、`checks.data_ownership`、`checks.golden_benchmark` 与 `checks.install_lifecycle`。该契约不是新的业务入口，而是把认知资产、统一质量决策、能力发现、隐私保留、生命周期状态、失败分类、ActionLedger、领域语言、满分 scorecard、模块开关、冷启动产物消费契约、统一 cognitive data event、运行态 producer/consumer 闭环、迁移账本、快照恢复、数据所有权、可重复认知质量基准和安装/升级/卸载旅程汇总到一处；新增自动写入、自动修复、配置变更、文档导入、模块开关、迁移、备份、恢复、导出、冻结、删除、安装、升级、卸载或认知质量评分能力时，必须先补 registry/manifest/contract/runtime ledger 和 strict 审计，再接入业务代码。

问题 34 起，`producer_consumer_ledger.db` 除 runtime flow events 外，还包含 `runtime_flow_receipts`、`cognitive_data_events`、`cognitive_data_consumptions` 和 `cognitive_data_reconciliations`。ROOT-20260710-012 起 runtime schema 为 v2：producer event 不可变，generation 与 intended consumers 是事件身份的一部分，消费/dead-letter 作为 append-only receipt 记录；同 event_id 的冲突内容必须拒绝，精确 replay 必须幂等且不得重置生命周期。异步消费者用 `receipt_grace_seconds` 声明终态承诺窗口，窗口内分别输出 `pending_count`/`in_flight_count`，超时后进入 `overdue_pending_count`、missing consumer 和 strict failure；默认 0，KG projection 为 60 秒。统一 cognitive 事件字段为 event_id、source_id、asset_id、source_kind、source_uri、content_hash、canonical_subject、data_type、producer、intended_consumers、privacy_level、confidence、evidence_refs、dedupe_key、created_at、retention_policy；同 content_hash + canonical_subject 记为 duplicate，同 source 不同 interpretation 记为 derived，不同来源同结论记为 reinforcement。初始化、v1 迁移和 `0600` JSONL outbox 的有序重放必须显式执行 `python3 scripts/bootstrap_runtime_producer_consumer_ledger.py`，不能借 health 隐式写入。修改 capture/sync/persona/scorer/reflection/distill 数据入口或消费者后，必须运行 `python3 scripts/audit_data_interface_registry.py --strict`、`python3 scripts/audit_runtime_producer_consumer_closure.py --strict` 和问题 34 的三条 pytest；完整 Quick 还必须以生产 `producer_consumer_ledger.db` 测试前后哈希一致证明无测试污染。

Install lifecycle 是用户部署入口，不替代底层 `auto_setup.py`、migration、backup 或 data ownership。推荐入口是 `mnemos setup`；`setup.sh`、`setup.bat` 和 `scripts/auto_setup.py` 仅作为兼容/高级入口保留。系统 Python 触发 venv re-exec 时，`mnemos setup --json` 必须重新进入 `mnemos_cli.py setup ... --venv-reexec`，不能绕过 `InstallLifecycleState`；Homebrew/PEP 668 路径必须回到 repo `.venv`，pip 升级超时只允许 warning，editable install 的 build isolation 下载失败要用现有 venv `--no-build-isolation` 重试。`scripts/auto_setup.py --yes --preserve-config` 通过 `scripts/setup_model_endpoints.py` 复用运行时 LLM/embedding/reranker 解析结果做 smoke，可选 multimodal 缺失只跳过，写配置后保持 `0600`，最后运行 `scripts/e2e_probe.py --dry-run --no-api`，不把真实 API E2E 超时作为配置安装阻断项。交互模式的必填模型 smoke 默认最多 3 次，`--max-smoke-attempts` 可调；失败后可重试、打印 env 示例、保存配置退出或停止到 dry-run 检查，非 TTY 必须 fail fast。`InstallLifecycleState.metadata.required_model_endpoints_failed` 是机器可读缺口字段，不允许只靠日志解析。`mnemos upgrade plan --json` 必须同时返回 migration plan 和 backup preflight；`upgrade apply` 先创建 snapshot，再写 ActionLedger。`mnemos uninstall` 默认 `--preserve-data`，`--purge-data` 只能生成数据所有权删除计划，真正删除仍必须先 freeze、提供 snapshot ref 并确认。

Golden benchmark 是质量回归入口，不替代 `scripts/e2e_probe.py` 的运行态探针。固定样本位于 `benchmarks/golden/manifest.json`，baseline 位于 `benchmarks/golden/baseline/mnemos_benchmark_scorecard.json`；`scripts/run_golden_benchmark.py --strict --mock-llm` 只使用 deterministic mock LLM/embedding/reranker/multimodal provider，默认把运行产物写到 `~/.mnemos/benchmarks/golden/latest`，输出 `mnemos_benchmark_scorecard.json`、临时 Wiki 投影、persona delta 和 ActionLedger。任何 prompt、schema、router、scorer 或 quality gate 改动都应先运行该 benchmark，并检查 `trend_comparison` 是否出现 regression。

用户认知画像 v2 默认写 `~/.mnemos/user_signals.db`，实现入口为 `core/persona/cognitive_profile.py`，核心表为 `profile_signals`、`profile_assertions`、不可变 `profile_assertion_revisions` 和 `profile_usage_log`。`profile_assertions` 只是当前读取投影；每个纠错、撤销或 material change 都必须先 append revision/content hash/supersedes，旧历史不能原位覆盖。画像断言必须保留 evidence refs、confidence、privacy_level、expires/status、revision_policy 和 contradicting_signals；只有具备 authenticated principal/scope 的消费点才能写 usage receipt。receipt 的 assertion revision、scope snapshot 和实际 read purpose 必须由 store 从 canonical assertion/ACL 推导，调用方自报的值一律校验拒绝。无 principal/scope 的背景 quality gate、auto-healing 或 flywheel 不得以 `action_changed=true` 冒充画像生效。修改该链路后运行 `python3 scripts/audit_persona_profile_contract.py --strict`、`python3 scripts/audit_persona_runtime_effectiveness.py --strict --json`、`python3 -m pytest tests/unit/test_user_cognitive_profile_v2.py tests/integration/test_profile_signal_assertion_usage_loop.py -q` 和 `python3 scripts/audit_runtime_producer_consumer_closure.py --strict`；生产历史表先 dry-run `python3 scripts/reconcile_profile_assertion_revisions.py --json`，apply 要求 daemon/MCP writers 停止和显式备份目录。

Wow-path E2E 是用户价值验收入口，不替代底层 `scripts/e2e_probe.py`。`scripts/e2e_wow_probe.py --mock-llm` 会在隔离临时目录中验证首次配置三项必填模型、可选多模态跳过、可信用户文档 100MB gate、默认 distill、行为/意图字段、Obsidian 路由、ContextAwareSearch/preflight 召回、runtime consumer ledger 和 auto-heal dry-run；报告必须显示 `user_intervention_count=0`、Wiki 页面、搜索命中、preflight 提醒和 consumer ledger 闭环。发布/本机验收可用 `--real-api`，但普通 CI 不应强制真实 API。

Distillation admission gate 由通用 `QualityGate` 和 `CognitiveValueGate` 叠加组成。`QualityGate` 负责长度、结构、清晰度和基础 useful markers；`CognitiveValueGate` 负责来源证据、认知贡献类型、未来触发场景、消费者影响和 raw lifecycle 信号。低认知贡献但格式良好的内容不得直接成为正式 Wiki；高价值但证据不足的内容应 pending verification。运行态有 `database_dir` 时，写页前最终 accept/review/reject 决策必须写入 `ActionLedger(action_type=quality_gate)`，正式页面 frontmatter 应包含 `质量门禁账本ID`。修改该链路时至少运行 `pytest tests/unit/test_cognitive_value_gate.py tests/unit/test_quality_gate.py tests/unit/test_distillation_engine.py -q`、`python3 scripts/verify_config_examples.py --strict` 和 `python3 scripts/e2e_probe.py --dry-run --no-api`。

Full-score gate 是发布/满分复验入口，不替代日常 `run_local_gates.py`。`run_local_gates.py` 已包含 `scripts/verify_config_examples.py --strict`、`scripts/audit_hardcoded_paths.py --strict`、`scripts/audit_docs_freshness.py --strict`、`scripts/audit_desktop_system_map_facts.py`、`scripts/audit_docs_sensitive_info.py --strict`、`scripts/audit_docs_stale_service_keys.py`、`scripts/audit_repo_sensitive_literals.py --strict`、`scripts/audit_release_privacy_security.py --strict`、`scripts/audit_distill_response_budget.py` 和 `python3 -m core.trust.static_scan`；其中 docs freshness 默认覆盖 AGENTS、CLAUDE、CONTRIBUTING、README、README-en、SECURITY、docs 和可发现的 `~/Desktop/mnemos系统图谱`，需要固定集合时用 `--paths`；Desktop facts audit 会在 `99-代码扫描-facts.json` 存在时校验 `current_state` 的 repo commit、schema 和绑定该 commit 的成功 Quick 结果。它不把 `run_local_gates.py` 的未来自我回执当作前置条件，因为该命令本身包含 facts audit；实际 local-gates 成功只能在执行后记录为历史证据。历史 scan 字段不能代表当前状态。生产 Python 代码中新增本机绝对路径、旧 Obsidian wiki 默认、绕过 Config 的 Mnemos/raw vault 路径，Markdown 文档新增本机路径、裸 `python` 调脚本、调模块、执行内联代码、缺失 repo 相对路径、未登记的 `mnemos config set <key>` 示例、真实 API endpoint、raw key/JWT、明文 credential 赋值、个人邮箱/手机号/身份证或 PII 赋值，Desktop facts 缺失 current-state 契约或指向旧 repo commit，源码/测试/文档新增完整 provider-shaped fake key、本机 home path 或明文 credential literal，health/config/`distill status`/E2E dry-run 诊断输出真实 URL、本机路径、未脱敏 key source 或 provider-shaped token，公开配置示例重新出现旧 daemon service key，配置样例低于 100% 覆盖，把蒸馏输出预算降回旧四档，或让 trusted push static scan 出现 unknown、stale registry、`known_bypass`、伪造 guarded 分类或未绑定 receipt 的 formal direct write，都会直接失败；标准默认路径只允许在 `core/config.py` 与 setup 布局 helper 中定义，daemon services 示例必须使用 canonical `eventbus`，redaction/secret 测试样例必须使用运行时拼接或 `DUMMY_CREDENTIAL_*` 哨兵，蒸馏输出预算四档必须保持 `6000/8000/12000/16000`。`python3 scripts/run_full_score_gates.py --strict --real-api` 会汇总测试层、local gates、health、security strict、release privacy/security、wow-path E2E、配置样例、认知就绪度、Wiki budget、golden benchmark、安装/升级探针和 contract audits；默认产物写入 `/tmp/mnemos-full-score-gates/...`，包含 JSON、Markdown 和每个子命令的 stdout/stderr。任何必需 gate 失败都会让入口返回非 0；其中 `health.strict` 采用满分语义，必须同时满足 `status=ok`、`ok=true`、`usable=true`、`strict_ok=true`，且 `strict_failures`、`failed_checks`、`degraded_checks`、`warning_checks` 与 critical skipped 列表均为空。开发阶段可在非发布运行中用 `--skip-slow --skip-e2e` 或 `--only` 缩小范围；`--strict --real-api` 发布/满分运行会拒绝 `--skip`、`--skip-slow`、`--skip-tests`、`--skip-e2e`、`--skip-wiki` 和 `--skip-readiness`。

Full-score gate 是发布/满分复验入口，不替代日常 `run_local_gates.py`。本地门禁继续负责配置、schema、路径、文档、隐私、安全、架构和可信写入；历史 scan 或 partial 运行不能代表发布状态。`python3 scripts/run_full_score_gates.py --strict --real-api` 的发布分母固定为当前代码 canonical 62-gate manifest，并拒绝 `--only` 和全部 skip 参数；独立 `docs/acceptance/phase5_required_full_score_gates.json` 要求 `contracts.persona_runtime_effectiveness`、`contracts.blindspot_asset_boundaries` 与 `contracts.phase5_failure_contracts` 同时进入并由证书验证器复核。最后一项绑定 frozen baseline failure evidence，并要求 PH5-031 四个验收计数为 `1/0/0/0`。三个 quality gate 分别要求 maintainability/zombie/vulture zero closure，`contracts.cognitive_action_effects` 要求认知动作真实 effect closure，`contracts.cognitive_calibration_lineage` 要求校准 lineage/record/projection 零缺口，`contracts.cognitive_event_dispatch`/`contracts.evidence_graph_direction` 要求 episode dispatch 与 evidence direction 闭环，`contracts.cognitive_search` 要求冻结 benchmark 的召回、溯源与 ACL 指标通过，`model_call_ledger.static` 还要求全部直接 provider 边界有预留/结算/失败保留证据。只有 expected/selected/executed 完全相等、omitted 为空、required receipt 全通过、工作树干净且绑定完整 commit 时才 `release_eligible=true`。每个 gate 的 stdout/stderr 均记录 SHA-256；随后必须在同一干净 commit 上运行 `python3 scripts/verify_full_score_certificate.py <full_score_gates.json>`。旧 v1 报告、partial、mock、dirty tree、unknown gate、empty selection、timeout、缺 artifact 或 hash/manifest 不一致均不得用于发布。开发阶段 `--only` 可以返回 0 供聚焦排错，但报告必须 `certifying=false`。测试文件分母用 `python3 scripts/audit_test_suite_denominator.py --strict --json` 验证，认知行为场景用 `python3 scripts/run_cognitive_behavior_scenarios.py --json` 实际执行。

`scripts/e2e_probe.py` 的运行态验收必须输出分层证据：canonical raw 模式检查 `raw_events.db.raw_turns` 的本次 `event_id` 和 `sync_log` row；外部 backend 模式要求 `backend_uids` 非空，并能通过 backend `get_by_id()` 反查到含本次 `session_id` 的记录。`skipped_backend`、空 `backend_uids` 或只存在 `sync_log.status=new` 都不能单独判 pass。`--dry-run --no-api` 的路径诊断默认脱敏，`--unsafe-debug` / `--show-paths` 只用于本机排错；`--no-api` 下 distill/Wiki 为 skip；`--real-api` 下 Wiki 页面必须包含本次 `session_id`，cleanup 必须分开报告 `raw/sync_log/wiki/backend` 清理数量。

安全审计入口固定为 `python3 scripts/security_audit.py --strict --json`，发布级聚合入口固定为 `python3 scripts/audit_release_privacy_security.py --strict --json`。direct 运行会优先选择仓库 `.venv/bin/python`，并用同一解释器执行 bandit、pip-audit 与 `scripts.health_check.check_security()`。安全报告固定为 `mnemos.security_audit.v2`：每个风险包含 `source/code/severity/message/repair_action`，`blocking_count`、`warning_count`、`status`、`ok` 和退出码只能由 typed findings 推导，并严格满足 `ok == (blocking_count == 0)`；旧 credential row、plaintext secret、pickle/weak hash、degraded/failed/error/unknown health 状态全部是 blocking，health warning 只形成 warning。发布级聚合器必须调用安全报告的同一 validator，schema、counts、findings、status、`ok` 或返回码矛盾都直接阻断，然后才汇总 strict config doctor、health privacy、docs sensitive、repo sensitive，以及 health/config、`distill status`、E2E dry-run 诊断脱敏。动态 SQL identifier 只能通过 `validate_sql_identifier()` 和固定 allowlist 后拼接，无法参数化的安全拼接必须有精确 `# nosec B608` 与非法 identifier 单测。`--strict-env`/`--no-venv-autodetect` 仅用于确认当前解释器自身已经安装 dev tools；缺少 `bandit` 或 `pip_audit` 时，脚本必须输出 `uv pip install -r requirements-dev.txt` 或对应 `python3 -m pip install -r requirements-dev.txt` 修复命令。

可维护性入口固定为 `python3 scripts/check_maintainability_budget.py --closure`。v2 baseline 除当前精确计数外，还保存每个 broad catch 的 AST fingerprint 与每个 residual 的 owner/expiry/telemetry/remove condition；当前扫描为 16 个超大文件、478 个 broad catch、120 个未分类、required-path 0。解析失败、same-count replacement、过期接受、改善后未收紧 baseline 都会失败；普通 `--update` 只能固化改善，新增/替换风险必须显式 `--accept-risk-changes`，且不会自动续期。No Zombie 入口固定为 `python3 scripts/check_zombie_code_policy.py --closure`；当前 131 个未记录 candidate 直接阻断 closure，不能被普通 update 吸收。local/pre-commit/CI 与 strict release evidence 分开；满分发布另外运行三个 `--closure --strict --json` gate，maintainability/zombie residual 必须为 0，vulture current/baseline 必须为 0/0。

审计报告写入策略固定为“默认只读，显式写入”。`scripts/audit_orphan_modules.py` 默认把 orphan module 报告输出到 stdout，不修改 `docs/orphan-modules-report.md`；`--check` 只比较当前报告是否同步，不写文件。需要刷新 repo 内报告时必须显式运行 `python3 scripts/audit_orphan_modules.py --output docs/orphan-modules-report.md --apply`，并记录 ActionLedger；要把报告交给外部审计或桌面产物时，优先用 `--output /tmp/...` 或桌面路径，避免污染代码仓库。

模块开关治理以 `mnemos doctor modules --json` 为只读核对入口。默认关闭、冷启动关闭、隐私关闭、成本关闭、watcher/daemon、legacy/stale 开关必须声明默认关闭原因、自动开启策略、自动关闭策略、产物 schema、消费者、效果指标、互斥关系和回滚策略；没有消费者的模块只能是 `registered_but_unwired`，不得自动开启。高成本模块的 activation policy 必须包含 budget/cost/network/migration gate，raw-vault watcher 类开关必须声明与 `raw_projection.enabled` 的互斥关系。

---

### MCP principal、grant 与 ACL 对账

`mnemos agent install` 为每个宿主原子签发/轮换 keyring-backed launch capability；宿主配置和 `.mnemos.bak` 只能出现 reference，文件权限必须为 `0600`。默认 grant 只有 `public_metadata`。按最小权限配置额外 policy/project/source 后必须重装宿主；更新或撤销 grant 会立即使旧 capability 失效：

```bash
python3 mnemos_cli.py agent grant-mcp codex --capability memory_read --project mnemos
python3 mnemos_cli.py agent install codex
python3 mnemos_cli.py agent kit --json

python3 mnemos_cli.py agent grant-mcp codex --revoke
```

存量 ACL 对账先 dry-run；可证明 provenance 的 Wiki/raw 项回填严格 envelope，无法证明的保持 restricted。`--apply --rebuild-raw-index` 后必须重跑 dry-run 并确认 `would_change=0`、`parse_errors=0`、`unresolved=0`：

```bash
python3 scripts/reconcile_access_metadata.py
python3 scripts/reconcile_access_metadata.py --apply --rebuild-raw-index
python3 scripts/reconcile_access_metadata.py
```

验收还应确认 52/52 tool-policy/schema/handler 集合相等，缺/伪造/过期/撤销 capability 的 handler 调用数为 0，拒绝候选不会读正文或写热度、训练、画像、搜索会话、点击、提醒冷却和推送历史。旧明文 launch 环境字段不会被生产运行时读取，也不能作为降级兼容入口。

Agent Kit v2 的 `conformance_ok` 只证明安装、MCP/Policy、passive source fidelity 与认知能力声明。运行 `full_power` 必须由已授权宿主先调用 MCP `health_check`，再在 5 分钟内按 `mnemos agent kit --json` 的 `runtime_probe_contract` 调用 `agent_runtime_probe`；回执 24 小时后过期。`agent_authorization.db` 的 `agent_health_roundtrips` 与 `agent_runtime_receipts` 只保存 agent、时间、check-set hash、状态与 completeness JSON，不保存固定样本文本或用户正文。以下任一情况均应让 `checks.agent` 进入 strict failure：授权缺失/撤销、无回执、握手或回执过期、样本畸形、旧 daemon/check-set hash 不匹配。`mnemos agent repair` 只修静态接入，不会自动授权或伪造运行回执。

### AgentSource 连续 Raw 完整性对账

`daemon.raw_sync` 采用“近期 tail 加速 + 持久化全分母 round-robin reconciliation”。`sync.raw_sync_sessions_per_source` 与 `sync.raw_sync_turns_per_session` 只限制单轮工作量，不能成为 Native 分母、完成状态或历史保留边界。每个 source/canonical-session 的 Raw high-water 由 `agent_sync_cursors.db` 持久化，且只在相邻 turn 已取得 canonical Raw receipt 后推进；schema v4 还把 complete generation 与 exact `native_source_snapshot_hash` 绑定，并要求每个 session 有 content-bound `parsed`、`typed_empty` 或 `evidence_excluded` 处置。Raw 写入失败、重启或重放必须从第一个未确认 turn 继续，不能跳过或重复宣布完成，也不能只替换 heartbeat hash 来伪造 Snapshot→Raw 关系。

存量 v1/v2/v3 cursor ledger 必须先停止全部 Mnemos daemon/MCP writer，再执行
`python3 scripts/reconcile_agent_sync_cursor_schema.py --backup-dir <dir> --json`，
审阅输出的 exact
`plan_hash`、DB scope、source hash、integrity/FK、writer-state 和 allowed delta。
取得仅绑定该 `COG-045/RM-SCHEMA` plan hash 与备份目录的生产 mutation 授权后，才可执行：

```bash
python3 scripts/reconcile_agent_sync_cursor_schema.py \
  --apply \
  --expected-plan-hash <sha256:...> \
  --backup-dir <dir> \
  --json
```

apply 会取得共享 offline migration lock，并在锁内重新计算 plan；任何源状态或
writer-lock 前提变化都会在备份/写入前拒绝。迁移增加空 proof 字段、将旧
session 标记为 `legacy_unverified` 并撤销旧 snapshot eligibility，不伪造历史
证据。首次 apply 前会验证 SQLite backup 可恢复；apply 后逐表比较不可变的
legacy 数据、单独守恒 eligibility invalidation、验证 v4 schema/integrity/FK、restore drill 和
`required_gap=0`。迁移或 comparator 失败会从备份恢复原逻辑快照。同一
plan 的第二次 apply 只有在 completion receipt 与 exact post-state 均匹配时才
返回 physical/semantic delta=`0`，不会再次写库。后续 fresh Raw-only
reconciliation 属于独立的
`COG-045/RM-IDENTITY` 授权；它的 dry-run 会读取本机完整 12-source native
history（8 host + 4 ingestion-only），因此连 dry-run 都必须先取得覆盖该
12-source 范围的 native-history access 授权。授权后先审阅：

```bash
python3 scripts/reconcile_agent_source_raw_capture.py \
  --confirm-read-native-history --backup-dir <dir> --json
```

若需要把约 29 万 token 的完整 dry-run 结果落盘，可额外使用
`--output-json <new-file>`。目标文件必须尚不存在；父目录必须尚不存在（工具会以
`0700` 创建）或已是当前用户独占的 `0700` 目录。工具不会把现有 `0755`/共享目录
静默 `chmod`，这类路径会以 `output_json_write_failed` 明确拒绝。

再把输出的 exact plan hash、同一 backup dir 和 apply scope 绑定到独立生产
mutation 授权，才可执行：

```bash
python3 scripts/reconcile_agent_source_raw_capture.py \
  --apply \
  --confirm-read-native-history \
  --expected-plan-hash <sha256:...> \
  --backup-dir <dir> \
  --json
```

新一轮 apply 创建 Raw、cursor、coverage 三个目标的备份前，必须先按 dry-run
冻结的三个精确目标盘点备份；若同一目标仍有上一代备份，只在取得本轮明确清理
授权后删除该目标的上一份 payload/sidecar，不得用 glob 删除、不得触碰其他目标
或仍被 prepared/completed receipt 绑定的恢复材料。删除与目录 fsync 完成后才
允许创建新备份，以避免低磁盘机器累积同目标副本。

RM-IDENTITY plan 使用 parser-owned `NativeArtifactInventory`：每个 parser
必须声明所有影响解析结果的 side artifact；例如 Kimi context session 会绑定
全部 `context*.jsonl` segment。对外只记录 resolved root、artifact identity、
source/session mapping 与 logical content 的 privacy-safe hash，不写 native
path 或正文；SQLite source 的 logical hash 包含 committed WAL state。执行前
在隔离、RSS 监控的 parser worker 中展开 native input，worker 通过 private spool
交付结果；父进程按 session 边界懒迭代 private spool，只保留当前有界批次，
不得把 4GB 级完整历史重新物化到内存。超过固定 256 MiB worker RSS /
1,000,000 turns 计划预算即在正式写入前终止。inventory 前后两次取证共同绑定
该私有 spool generation，并把实际 turns、logical input bytes 与估算 bytes
绑定到 reviewed plan；超限时在备份/写入前拒绝。零 session source 必须以实际
存在的 resolved root、空 roster、
snapshot binding 与空 receipt 集合形成明确 verified-empty；缺失 root 仍是
not detected。任何 native 漂移同样在写入前拒绝，实际 Raw writer 只消费该
immutable generation。

首次 apply 只允许改 Raw/cursor/coverage 三类明确目标，并要求 Native→Raw
challenger、逐 source capture receipt、全部既有 Raw row/hash/provenance/ACL/
retention conservation、unexpected mutation、写入前即为 `0600` 的 clone backup
restore drill 和独立重跑的 post-gap 全部通过；mutation 后任一中断或
evidence/receipt/second-verify 失败也会实际恢复三类目标、核对 exact pre-state
并使内层 completed receipt 失效。
process-owned 写入边界覆盖数据库目录本身及目录内全部非 allowlist 目标：
create/write/replace/unlink/chmod/chown、预先打开的可写 descriptor，以及 apply
期间的 `subprocess`/`posix_spawn`/`exec` 都必须计入
`blocked_process_mutation_*` 并触发回滚；descriptor flags 无法读取时 fail
closed。backup scope 等于数据库目录或包含数据库目录时必须在创建备份前拒绝。
另一个已在 apply 前启动、且只写 disjoint 数据库文件的外部 writer 不冒充本次
事务错误：它只进入 content-free `foreign_concurrent_mutation_*` 诊断，不能
抵消或覆盖任何 process-owned violation。
外层 migration completion receipt 还必须绑定内层 reconciliation receipt 的
私有文件名、SHA-256、exact reviewed plan hash、support manifest、active source
分母、backup/pre-state、终态 challenger、逐 source capture、Raw-only mutation
boundary 与 session-identity 结果。第二次 apply 会重新读取并语义复核内层
receipt；仅重算外层哈希也不能让被改写、降级或伪造的内层 receipt 获得信用。
prepared outer receipt 先绑定内层 prepared 文件的 exact SHA；内层转为
completed 后，外层仍在 certification 前追加绑定其终态 SHA。中断恢复和
`recovered_rollback` 也必须复核 schema/plan/code、backup/pre-state、prepared
lineage 与当前内层 exact SHA。失败后同 plan 重试会先把 terminal rollback
receipt 按 plan hash + receipt hash 原字节归档，再创建新 attempt；成功不得
覆盖或洗掉旧失败节点。完成态的 same-plan verifier 会逐个复核归档文件名、
私有权限、receipt hash、plan hash、rollback 终态，以及每个归档节点声明的
`prior_terminal_receipts` 是否精确等于此前有序前缀；任意次数失败重试都必须
继承完整祖先链。归档后、写新 prepared intent 前退出时，只允许当前回执哈希
对应的唯一末端归档，恢复后并入同一有序链。归档缺失、额外、重排、分叉或漂移
都直接失败。内层 receipt 失效前必须先把原字节归档到同一私有 backup scope；
源文件名必须匹配 canonical receipt 模式，且必须是当前 owner 的 `0600`、
非 symlink、link-count=1 regular file。source hash、归档 hash 或 durable
directory fsync 任一不成立时，不得覆盖原 marker。
单轮 parser/RSS budget 等可重试故障不会被静默丢弃：每轮 receipt 保留
content-free typed error，后续 generation 只有在同一 frozen snapshot 下让
最终 challenger、全 source capture 与 mutation boundary 全部闭合时，才可记录
`recovered_retryable_error_count` 并继续验收。只有明确分类为瞬态且非 Native
确定性错误的代际失败可以重试；parser、budget、identity、schema、安全边界等
`native_*`/non-retryable code 在首次出现时立即停止并回滚。若最终 source gap
仍存在，则必须以 `raw_reconciliation_incomplete` 回滚。
每轮 `cycle.errors` 还必须与 typed `error_evidence.count` 精确守恒；漏记、
多记或无法归属 source 的错误均不能借后续 capture green 获得 completion，
统一保留 `cycle_error_evidence_mismatch` 或 unrecovered retry 原因并回滚。
同一 plan 的第二次 apply 必须同时匹配原 native inventory 与 exact target
post-state，且 first-apply before comparator 必须能从 sealed backup 独立重算，
才以 physical/semantic delta=`0` 返回。上述能力现已具备隔离测试
合同，覆盖 clean first/same-plan zero、早期 retry 后恢复、retry 耗尽、
non-retryable immediate stop、plan drift、writer lock、守恒与 post-gap 失败、
restore drill、证据写入、second verify、中断恢复、rollback failure，以及内层
receipt 字节/语义/source-key 篡改、未归因错误、失败代际保留和删除回滚目录
fsync；任何新增生产重试前必须先让这组有穷状态矩阵全绿。
当前没有执行本机 v3→v4 生产迁移或 Agent history rebuild。当前 production
apply 仍保持暂停；只有取得两个分别绑定 exact plan/scope/backup 的新授权后才能
执行，也不能用代码绿灯关闭 COG-045。

COG-045 的 change budget 显式扩展两次 schema 和两次 migration：
`append_only_native_session_identity_reconciliation_ledger`、
`raw_rebuild_exact_plan_identity_preflight_and_rollback` 与
`native_session_disposition_conservation_cursor_v4`。前两者只能由上述
backup-first rebuild 创建并追加 receipt，普通 `RawEventStore` 启动不得建表；
receipt 必须绑定 legacy identity set、source artifact、历史 Raw exact row、
current revision、logical-content 和完整 revision set 的 hash，任一后续漂移都
失效。DDL 唯一 owner 是
`core/sync_framework/raw_session_identity_reconciliation.py`，表上的
no-update/no-delete trigger 同属该 owner。该预算不授权其他 schema/migration
扩展，也不等于授权 live apply。

运维核验连续采集时，先确认 daemon 已按获授权的有效配置运行并完成至少一轮 reconciliation，再运行：

```bash
python3 scripts/audit_agent_source_coverage.py --strict --json
```

该审计使用 12 个 active Source 的唯一分母，只读核对 cursor generation、snapshot binding、expected turn、Raw receipt 与 canonical Raw header；不会读取 transcript body。它会拒绝不完整的 canonical session roster、过期 coverage、缺失或伪造 NativeSourceSnapshot/Raw receipt、只做 tail 的短窗口和 CLI 的 `partial`/`dry_run`。其中 8 个 `host_agent` 与 4 个 `ingestion_only` 分型输出；ingestion-only 必须闭合 Native→Raw，但不得计入 8 Host full-power。`--max`、`--since`、source filter 或 dry-run 只能用于诊断或受控工作批，不能写成 source 的 global `done`，也不能触发 partial session handoff。启用生产 `raw_sync`、重启 daemon 或读取本机 Agent history 都可能访问用户本地会话，必须先取得明确授权；隔离测试的成功不能替代该授权。

## 二、日常检查清单

### 每日
- `python3 -m core.ops.health_check`
- 关注 amphora pending（<50）、failed（必须为 0 或进入明确归档/owner）、EventBus pending（<1000）、磁盘（<90%）

### 每周
- `mnemos doctor`
- `mnemos doctor --cognitive-readiness --json`
- `python3 scripts/audit_cognitive_readiness.py --json --budget`
- `mnemos distill evidence-backfill --json`
- `python3 scripts/plan_cognitive_consolidation.py --json --record-run`
- `python3 -m pytest tests/unit/test_knowledge_trust_scorer.py -q`
- `python3 -m pytest tests/unit/test_policy_patch_store.py tests/unit/test_kia_policy_patches.py tests/unit/test_retrospective_workflow.py -q`
- `python3 scripts/replay_delivery_decisions.py --json`
- `mnemos verify plan --json`
- `python3 scripts/run_golden_benchmark.py --strict --mock-llm`
- `python3 scripts/audit_install_upgrade_contract.py --strict`
- `python3 scripts/e2e_install_probe.py --tmp-home`
- 检查 `~/.mnemos/alerts/` 是否有新告警
- 查看 daemon 日志中的 ERROR/FAIL

---

## 三、核心服务管理

### Daemon 启停

```bash
# 前台启动
python3 mnemos_daemon.py run

# 后台启动
python3 mnemos_daemon.py start

# 停止
python3 mnemos_daemon.py stop

# 检查
python3 mnemos_daemon.py status
```

daemon 启动时自动写入 PID 文件到 `~/.mnemos/daemon.pid`。

### 服务模块

| 服务 | 间隔 | 功能 |
|------|------|------|
| capture_worker | 配置控制 | 消费采集队列并写入 Raw Vault |
| 收件箱扫描 | 10min | 扫描 `data/inbox`，处理文件到 Wiki |
| 心跳 | 60s | 健康评分 + 争议扫描 + 新鲜度 + 评分器训练 + 搜索健康 |
| 蒸馏 Worker | 事件驱动 | 消费 amphora 队列，执行七层蒸馏流水线 |

### 心跳关键调度
- 每 5 次（5min）：蒸馏评分器状态报告
- 每 30 次（30min）：搜索索引健康检查 + 缓存刷新
- 每 720 次（12h）：synthetic ground_truth 注入 + 评分器训练调度
- 每 1440 次（24h）：争议扫描 + 知识新鲜度检查

---

## 四、队列与任务管理

### Amphora 蒸馏队列

```bash
python3 -m core.kia.amphora --stats
python3 -m core.kia.amphora --list
python3 mnemos_cli.py distill reset-timeouts --minutes 30 --json
python3 -m core.kia.amphora --cleanup      # 清理 7 天前的完成/失败任务
```

### EventBus 事件队列

数据库：`~/.mnemos/events.db`

```bash
# 查看 pending
sqlite3 ~/.mnemos/events.db "SELECT COUNT(*) FROM events WHERE status='pending'"

# 查看死信
sqlite3 ~/.mnemos/events.db "SELECT COUNT(*) FROM dead_letters"

# 清理普通旧事件（canonical cognition_episode_committed 永久保留，不在此分母）
sqlite3 ~/.mnemos/events.db "DELETE FROM events WHERE created_at < datetime('now', '-30 days') AND status IN ('done', 'archived')"
```

告警阈值：pending > 1000，dead_letters > 10。`event_bus.lease_seconds` 默认 300 秒；processing 事件携带 `lease_owner/lease_expires_at/lease_epoch`，只有当前 owner+epoch 可提交 terminal。不要用手工 UPDATE 抢占、ACK 或清理 `cognition_episode_committed`。

### Cognition Episode 投影对账

运行库为 `~/.mnemos/state/producer_consumer_ledger.db`、`evidence_graph.db`、`cognitive_graph.db`、`wiki_projection.db` 与 `events.db`。正常构造器只验证 schema，不隐式升级历史库。

```bash
# 只读盘点 schema、event/consumer/effect/receipt 与 evidence direction
python3 scripts/reconcile_cognition_episode_projections.py --json
python3 scripts/audit_cognitive_event_dispatch.py --strict --json
python3 scripts/audit_evidence_graph_direction.py --strict --json

# 停止所有 writer，绑定只读 inventory 后备份并 apply
python3 mnemos_cli.py daemon stop
python3 scripts/reconcile_cognition_episode_projections.py \
  --apply --expected-inventory-hash <sha256> \
  --backup-dir ~/.mnemos/backups/cognition-episode-projections --json

# 必须再次得到零动作，再独立检查五个 live/backup SQLite
python3 scripts/reconcile_cognition_episode_projections.py --json
```

### Wiki 投影生命周期与重建

运行库：`~/.mnemos/wiki_projection.db`。`wiki_mutations` 是 append-only 权威历史，`wiki_pages` 只是 current pointer，`projection_receipts` 保存 `knowledge_graph`、`cognitive_graph`、`relation_embeddings`、`wiki_search_index`、`wiki_metrics`、`moc_navigation` 的独立结果。只有 `ack/noop` 是 terminal success；`retry/defer/dead` 不得被健康检查解释为完成。

```bash
# 默认只读：查看当前投影快照，不写库
python3 scripts/rebuild_wiki_projection_state.py --json

# 先停止 daemon，再显式备份和重建
python3 mnemos_cli.py daemon stop
python3 scripts/rebuild_wiki_projection_state.py \
  --apply --backup-dir ~/.mnemos/backups/wiki-projection-rebuild --json

# 重建后复验事件、Wiki quality 与本地门禁
python3 scripts/wiki_lint.py --summary --json --budget
python3 scripts/run_tests.py quick
python3 scripts/run_local_gates.py
```

apply 会备份 KG、Cognitive Graph、metrics、ledger、relation/Wiki ANN 和 Wiki prestate，然后从当前 Vault manifest 干净重建；成功必须同时满足饱和收敛、full vs actual incremental hash 相等、isolated incremental comparator 相等、ANN label/vector 语义阈值通过、六类 `projection_gap=0`。失败时保持 daemon 停止并从 backup 恢复，不得删除 ledger、降低质量预算或手工伪造 receipt。完整字段与故障语义见 `docs/WIKI_PROJECTION_LIFECYCLE.md`。

COG-050 另提供精确、默认 dry-run 的认知投影 reconciliation：

```bash
python3 scripts/reconcile_cognitive_projection_lifecycle.py --json
python3 scripts/audit_cognitive_projection_lifecycle.py --strict --json
```

只有完整链路已修复、所有 writer 已停止、dry-run 计划 hash 已人工确认且备份目录为全新路径时，才允许使用 `--apply --expected-plan-hash <hash> --backup-dir <dir>`。当前 2026-07-22 状态为 `RUNTIME_REBUILD_PENDING`，因此上述 apply、KG/ANN/projection rebuild 与 replay 均暂停；修代码和隔离测试不得顺手触发生产重建。

存储空间不足也不能放宽恢复基线。删除任何 Wiki/KG/ANN/认知数据库备份前，必须同时证明：非备份主库 full integrity 与 foreign key 检查通过、canonical schema 和最新 migration/generation transaction 完整、COG-050 production audit 三类 gap 为 0、full/incremental/isolated comparator 与 ANN 语义审计通过。任一项失败或标记 `REBUILD_PENDING` 时，最后一份已验证完整备份必须保留。当前主库只满足物理/事务完整性，不满足投影语义收敛，所以本轮禁止清理相关备份。

---

## 五、常见问题排查

### Q1: daemon 无法启动
```bash
rm ~/.mnemos/daemon.pid        # 清理残留 PID
ls -la ~/.mnemos/              # 检查目录权限
python3 mnemos_cli.py health --json
```

### Q2: 蒸馏不产出 wiki
1. `python3 -m core.kia.amphora --stats` — 确认有 pending 任务
2. 查看 daemon 日志中的 hephaestus worker 输出
3. 检查 `~/.mnemos/wiki_state.db` 的 `processed_sessions` — 是否已被标记
4. `python3 -m core.ops.health_check` — 检查 LLM / Embedding / Reranker 三类必填模型 API 与可选 Multimodal 状态

### Q2.1: LLM / Embedding / Reranker 未配置
```bash
export MNEMOS_LLM_MODEL=your_llm_model_id
export MNEMOS_LLM_BASE_URL=https://your-llm-api.example/v1
export MNEMOS_LLM_API_KEY=your_llm_key

export MNEMOS_EMBEDDING_MODEL=your_embedding_model_id
export MNEMOS_EMBEDDING_BASE_URL=https://your-embedding-api.example/v1
export MNEMOS_EMBEDDING_API_KEY=your_embedding_key

export MNEMOS_RERANKER_MODEL=your_reranker_model_id
export MNEMOS_RERANKER_BASE_URL=https://your-reranker-api.example/v1
export MNEMOS_RERANKER_API_KEY=your_reranker_key

# 可选：图片/截图/视觉证据解析；不配置不会阻断 setup/health
export MNEMOS_MULTIMODAL_MODEL=your_vision_model_id
export MNEMOS_MULTIMODAL_BASE_URL=https://your-vision-api.example/v1
export MNEMOS_MULTIMODAL_API_KEY=your_vision_key
```

然后运行：

```bash
mnemos doctor --json
python3 verify_installation.py --json
```

### Q3: 评分器一直处于 COLD 模式
- WARM 阈值：总样本 ≥ 30（已修复）
- synthetic ground_truth 每 12h 自动注入
- 手动加速：
  ```bash
  python3 -c "from core.scoring.adaptive_scorer_v2 import AdaptiveScorerV2; AdaptiveScorerV2._bootstrap_if_needed()"
  ```

### Q4: EventBus 队列深度超过 1000
- 确认 daemon 在运行：`pgrep -f mnemos_daemon.py`
- 清理旧 pending 事件（见上文）
- 如持续积压，检查事件消费端日志

### Q5: Wiki 页面堆积在 Inbox
- 先看 `mnemos health --json` 的 `checks.wiki_route`：该 strict check 会列出可分类 Inbox、needs_review、正式区 source-prefixed 文件和标题/basename 冲突是否超预算。
- 确认 daemon `wiki_route` 服务启用后运行；它会周期性调用 Charon route-only connect，把可分类页面移动到正式目录，并在 heartbeat 暴露 classified/moved/review。daemon 默认传入 `write_relations=False`，不写 KG cooccurrence 关系，也不触发 embedding-heavy 图谱构建。
- 手工复核前先运行 `python3 scripts/reorganize_wiki.py --dry-run` 与 `python3 mnemos_cli.py vaults audit-content --json`，正式区同 basename 冲突或 `needs_review` 页面需要人工确认。

### Q7: 历史截断数据
```bash
# 检查当前蒸馏、队列、raw vault 与模型配置健康状态
python3 mnemos_cli.py health --json

# 检查安装与运行时依赖
python3 verify_installation.py --json
```
新逻辑已移除截断（`save_long_content` 自动分片），历史 stale 标记已清理；公开运维入口只保留健康检查和安装验证。

---

## 六、数据库维护

### 主要数据库

| 数据库 | 路径 | 用途 |
|--------|------|------|
| events.db | ~/.mnemos/events.db | EventBus 事件队列 |
| wiki_state.db | ~/.mnemos/wiki_state.db | 已处理 session、wiki 页面索引 |
| user_signals.db | ~/.mnemos/user_signals.db | 用户行为信号、用户认知画像 v2 的 signal/assertion/usage 数据 |
| distill_queue.db | ~/.mnemos/distill_queue.db | Amphora 蒸馏队列 |
| mnemos.db | ~/.mnemos/mnemos.db | 评分模型、训练队列、搜索/反馈等应用状态 |
| model_call_ledger.db | ~/.mnemos/model_call_ledger.db | 唯一 billable model-call 预留/结算账本；不保存原始 prompt、response 或调用方错误文本，只保留本地 opaque reference、freeze barrier 与不可重置 spend tombstone。完整 schema 在 runtime fail-close，详见 `docs/MODEL_CALL_LEDGER.md`。 |
| producer_consumer_ledger.db | ~/.mnemos/producer_consumer_ledger.db | `mnemos.runtime_producer_consumer.v2` 不可变 producer event、generation/intended consumer、append-only consumption/dead-letter receipt 与 freshness 对账，以及统一认知数据事件、消费和 duplicate/derived/reinforcement 对账；只能由显式 bootstrap 初始化/迁移，health 只读 |
| evidence_graph.db | ~/.mnemos/evidence_graph.db | EvidenceGraph canonical source→derived edges、认知 episode 投影 effect 与 exact omission receipt；COG-030 schema 只经显式 reconciliation 升级 |
| cognitive_graph.db | ~/.mnemos/cognitive_graph.db | 认知关系拓扑与 committed episode projection effect；不是 CognitionEpisode source of truth |
| wiki_projection.db | ~/.mnemos/wiki_projection.db | Wiki mutation/consumer receipt，以及 committed episode 的 Wiki projection effect |
| knowledge_graph.db | ~/.mnemos/knowledge_graph.db | KG relations/entities/FTS/evidence；`relation_evidence` 的 version/hash 写入 `mnemos_schema_registry`，DDL 由 `core/kia/relation_evidence_schema.py` 单一所有 |

### relation_evidence schema 迁移与回滚

```bash
# 1. 只读识别 current / prior / unknown schema、NULL 计数与 would_rebuild
python3 scripts/reconcile_relation_evidence_schema.py --json

# 2. 停止 daemon 后，显式备份并事务迁移/登记
python3 mnemos_cli.py daemon stop
python3 scripts/reconcile_relation_evidence_schema.py \
  --apply --backup-dir ~/.mnemos/backups/schema-registry --json

# 3. 对账实际 columns/defaults/FK/index/hash、registry 和 DDL owner
python3 scripts/audit_schema_registry.py --strict --json
```

apply 前必须确认 `null_evidence_type_count=0` 且 `blank_evidence_type_count=0`。非零时停止，不得用 `quote`、空字符串或其他猜测值填充；先人工分类。`--apply` 缺 `--backup-dir` 会拒绝，备份用 SQLite backup API 并执行 `integrity_check`；识别到旧 RelationManager defaults schema 时在单事务内重建，旧 KnowledgeGraph signature 只登记版本/hash，未知 schema 拒绝自动迁移。异常会 rollback；需要人工回滚时先停 daemon，以报告中的 backup path 恢复完整 DB，再执行 integrity 与 strict schema audit。构造器只做 fresh create 或 existing validation，不承担存量迁移。

### model-call ledger 对账与恢复（COG-018 实现契约）

```bash
# 1. 只读：审阅 COG-018 项的 execution_plan_hash。
python3 mnemos_cli.py migrate plan --json

# 2. 维护窗口内停止 daemon，并把审阅过的精确 hash 传给 wrapped apply。
python3 mnemos_cli.py daemon stop
python3 mnemos_cli.py migrate apply database.model_call_ledger.v1 \
  --execute-wrapped --expected-plan-hash "<execution_plan_hash>" --json

# 3. 再次 plan 必须显示 clean/noop；再检查 provider-boundary 静态覆盖。
python3 mnemos_cli.py migrate plan --json
python3 scripts/audit_model_call_ledger.py --json

# 4. 恢复先只读预览 sealed v3 manifest；真正恢复才显式 apply。
python3 mnemos_cli.py migrate rollback database.model_call_ledger.v1 \
  --recovery-manifest "<MNEMOS_DIR>/.../mcl-recovery-<id>.json" --json
python3 mnemos_cli.py migrate rollback database.model_call_ledger.v1 \
  --recovery-manifest "<MNEMOS_DIR>/.../mcl-recovery-<id>.json" --apply --execute-wrapped --json
```

`execution_plan_hash` 必须来自刚审阅的 COG-018 plan；缺失或不匹配时 apply 以零写入 `blocked` 返回。无 retired source 的 clean 状态以零写入 `noop` 返回，不会为了“完成迁移”创建账本、备份或 migration record。只有计划明确要求时才额外传 `--discard-unattributable-legacy` 或 `--discard-unrecoverable-run-tombstone-history`，不得根据 run root 猜测 entry 归属。非 clean apply 使用普通本地 SQLite backup、完整性检查、source-drift 复核和 sealed `mnemos.model_call_ledger_recovery.v3` manifest；它们都是本地恢复正确性证据。rollback 还验证 manifest 的 migration binding、append-only journal completion/interruption 状态和 postimage，tampered/legacy manifest 不能用作 rollback。manifest 仅列出本次计划实际范围内的 target，并绑定每个 target 的 SQLite sidecar；任何 orphan、缺失、漂移或篡改均 fail closed。迁移后的 second plan、health、backup integrity 和 restore drill 都是独立证据。

`scripts/reconcile_model_call_ledger.py` 是可从仓库外直接运行的只读诊断 wrapper，内部实现位于 `core.migrations.model_call_ledger_reconcile`。它的 direct `--apply` 没有 registry-issued capability，因此必须保持零写入 `blocked`；正式 mutation 只允许上面的 `mnemos migrate apply database.model_call_ledger.v1 --execute-wrapped ...`。`mnemos health --json` 中的 ledger 与 heartbeat 检查均为有界只读，不会因诊断创建账本、DDL 或运行态记录。

2026-07-14 的本机演练已经按此顺序完成：daemon stopped → registered apply → ledger health 与 plan verified → sealed-v3 restore → registered reapply → final plan 与 ledger health verified；同轮 isolated Quick 为 `6156 passed, 15 subtests`。这只是 COG-018 的本机运行证据，不是全仓 release certificate。

保留/删除会在本地 SQLite active DB/WAL 语义内要求 `journal_mode=DELETE` 与 `secure_delete=ON`；这不是对文件系统快照、Time Machine、复制文件、备份或 provider 记录的取证级清除承诺。恢复所需的本地 backup 有意保留，并应按单独的备份保留策略管理。

### Canonical cognitive state 与 ActionLedger 迁移

`CognitiveStateStore` 是 CognitionEpisode、BeliefRevision、CognitiveStateSnapshot、DecisionTrace、PredictionRecord、ValueContext、CalibrationRecord、UserReactionEvent、OutcomeMeasurement 和 CognitiveUpdateReceipt 的唯一 typed owner。`ProducerConsumerLedger` 只存 envelope、event × consumer receipt 与 reconciliation proof；Wiki、KG、CognitiveGraph 和 ActionLedger 只能消费 committed revision/outbox，不能保存可独立修改的第二份 canonical state。`build_cognitive_state` 是零写入 read model；`record_decision`/`apply_outcome` 必须经同一 `CognitiveStateUnitOfWork` 提交 revision、event 和 outbox。ActionLedger 的状态推进必须追加新 action ID 并引用前一条，不能覆盖旧行。

构造器、health 和审计不会创建或修改 schema。旧账本升级固定按以下顺序执行；两个 apply 都要求 daemon 已停止和显式备份目录：

```bash
python3 scripts/reconcile_cognitive_state_store.py --json
python3 scripts/reconcile_action_ledger.py --json
python3 mnemos_daemon.py stop
python3 scripts/reconcile_cognitive_state_store.py \
  --apply --backup-dir ~/.mnemos/backups/cognitive-state --json
python3 scripts/reconcile_action_ledger.py \
  --apply --backup-dir ~/.mnemos/backups/action-ledger --json
python3 scripts/reconcile_cognitive_state_store.py --json
python3 scripts/reconcile_action_ledger.py --json
python3 scripts/audit_cognitive_state_store.py --strict --json
python3 mnemos_daemon.py start
```

迁移只把字段、来源和 evidence 完整的旧语义 event 保留为 `historical_candidate`，不会自动成为 current state；不完整、孤儿或 lineage 不可证明的数据进入 append-only quarantine。验收必须同时证明唯一 DDL owner、三处事务 failpoint 均无单边提交、per-consumer terminal 完整、current state 从 immutable revisions 重建 hash 一致、ActionLedger UPDATE/DELETE 被拒绝，以及 second apply 为 zero-change。内容持久化仅执行 `pii_credentials_only_v1` 窄脱敏，不做整库或字段加密。

#### BeliefRevision 与对象级 provenance migration

`BeliefRevisionStore` 通过 `CognitiveStateApplicationService.revise_belief()` 追加 canonical revision，并通过 `explain_belief()` 在 ACL header 鉴权后返回当前状态、支持/反对 evidence、有效期、confidence method、uncertainty 与完整 revision lineage。调用方不能指定 belief/claim ID 或 stance。`daemon.kia_services` 只消费已提交的 `project_belief_revision` outbox；CognitiveGraph 是可重建投影，只有绑定 revision、projection identity 和 before/after hash 的 effect receipt 才能终结命令。

存量 Wiki page、CognitiveGraph relation 与 Reflection 只按对象级 provenance 迁移：稳定 source identity、精确字段分母和完整内容 SHA-256 进入 `unverified_candidate` quarantine；迁移不读取正文推断 belief、stance、confidence 或 supersedes，也不会增加 active head/revision。先保存 dry-run 的 `inventory_hash` 与逐域计数；apply 前停止 daemon，并指定新的备份目录：

```bash
python3 scripts/reconcile_belief_revision_candidates.py --json
python3 mnemos_daemon.py stop
python3 scripts/reconcile_belief_revision_candidates.py \
  --apply \
  --expected-inventory-hash 'sha256:<dry-run inventory_hash>' \
  --backup-dir ~/.mnemos/backups/belief-revision-candidates \
  --json
python3 scripts/reconcile_belief_revision_candidates.py --json
python3 scripts/audit_belief_revision_lineage.py --strict --json
python3 mnemos_daemon.py start
```

apply 必须绑定刚审阅的 dry-run `inventory_hash`；对象集合或内容在两步之间变化时，在备份和写入前 fail closed。apply 使用 SQLite backup API 并校验 integrity；同 source identity 和相同 content hash 的重放必须返回 `existing`，同 identity 但内容变化必须 conflict 并回滚整批。回滚使用报告中的已验证备份，不删除 canonical revisions。strict 验收要求 19 项行为合同全过、所有 lineage/ACL/projection/migration 指标为 0；这只关闭 COG-035，不代表后续 Phase 3 根问题或 release certificate 已完成。

#### DecisionTrace 与动作效果闭环

所有会改变认知状态、正式 Wiki、关系、策略、画像、调度、提醒或投递状态的 material action，必须先由 `DecisionTraceCoordinator` 在同一 canonical transaction 中封存 `ValueContext`、pre-action `CognitiveStateSnapshot`、`DecisionTrace` 和 action command；目标执行器只接受绑定 decision/action/target/executor/precondition 的 single-use permit，并把 reciprocal terminal effect 写入目标库。`ActionLedger`、delivery、trusted push 以及各目标服务不能补签或猜测 DecisionTrace；strict activation marker 未启用时，material sink 必须 fail closed。

COG-036 有两个相互独立的生产迁移面。先为四个 target store 安装统一 material-effect schema，再把 ActionLedger、delivery event 与 formal cognitive mutation 三域的历史对象按精确 source identity、schema fingerprint、主键和完整 content hash 写入现有 cognitive-state quarantine。历史迁移只产生 `historical_incomplete` 记录，不创建 ValueContext、snapshot、DecisionTrace、active head、action command 或 terminal effect。两个 apply 都要求 daemon 已确定停止、全新的备份目录，以及停止后重新生成并人工审阅的 inventory hash；不得复用 daemon 运行期间生成的旧 hash。

```bash
# 1. 先停止 daemon，再生成四个 target store 的新鲜 schema inventory。
python3 mnemos_daemon.py stop
python3 scripts/reconcile_material_effect_schema.py --json

# 2. 使用上一步报告中的精确 inventory_hash；备份目录必须尚不存在。
python3 scripts/reconcile_material_effect_schema.py \
  --apply \
  --expected-inventory-hash 'sha256:<material inventory_hash>' \
  --backup-dir ~/.mnemos/backups/cog036-material-<run-id> \
  --json
python3 scripts/reconcile_material_effect_schema.py --json

# 3. 在 daemon 仍停止时盘点三域历史对象，再用独立的新备份目录 apply。
python3 scripts/reconcile_decision_trace_history.py --json
python3 scripts/reconcile_decision_trace_history.py \
  --apply \
  --inventory-hash 'sha256:<decision inventory_hash>' \
  --backup-dir ~/.mnemos/backups/cog036-decision-<run-id> \
  --json

# 4. 重放必须是 zero insertion / all existing；strict 审计七项主指标均为 0。
python3 scripts/reconcile_decision_trace_history.py --json
python3 scripts/audit_decision_trace_effects.py --strict --json
python3 scripts/audit_schema_registry.py --strict --json
```

`reconcile_decision_trace_history.py` 的 apply 报告包含 sealed restore manifest。恢复是显式的状态变更，必须保持 daemon 停止，并只使用该次 apply 生成且校验通过的 manifest：

```bash
python3 scripts/reconcile_decision_trace_history.py \
  --restore-manifest '<decision-backup>.restore.json' \
  --json
```

恢复演练后必须重新 dry-run，使用新的 inventory hash 和新的备份目录再次 apply，最后复验 strict audit；不能把已恢复的 preimage 当作最终迁移状态。material-effect apply 任一步失败会自动恢复所有已验证备份并核对逻辑快照。最终验收要求 `decision_without_action_terminal`、`action_without_decision`、`decision_without_value_context`、`value_context_revision_missing`、`decision_snapshot_unresolvable`、`snapshot_hash_mismatch`、`value_ref_missing` 全为 0，sink bypass/permit mismatch/多 terminal/non-reciprocal/historical activation 也全部为 0。该证据只关闭 COG-036，不代表 Phase 3 或 release certificate 已完成。

2026-07-19 复验发现 12 条旧 delivery event 缺少 exact material/non-material proof；当前 writer 已正确写 proof，故没有放宽 runtime 合同，而是只对旧对象执行增量 quarantine。最终三域 denominator 为 146,967（ActionLedger 1,966、delivery 2,325、formal mutation 142,676），inventory 为 `sha256:238be50c6fda42b62a528fcac975207d271174f70881303daea7c4ac0b9dc747`，object manifest 为 `sha256:5b86a8307bb937d50a85254396c447de0e013fb01c488a31524b4d72e838cfc1`。首轮 apply 新增 15 个 exact historical identity（12 个缺口及 3 个已有 runtime provenance 的历史 identity），随后真实 restore、独立目录 reapply 和 replay；replay 前后 report hash 与 target snapshot hash 均相同。最终 historical=146,967、uncovered=0、七项主指标=0、33/33 sink guarded、integrity=`ok`、`migration_required=false`。备份根位于 `~/Desktop/mnemos-cog036-decision-gap-20260719/`，在完成独立保留策略前不得删除。

#### PredictionLedger、成熟结果与历史 provenance

`PredictionRecordStore` 是预测身份、评测窗、OutcomeMeasurement 绑定、终态、误差与校准分母的唯一 deep owner，物理上仍写入 `producer_consumer_ledger.db` 的 `CognitiveStateStore`。预测推送在 route effect 前冻结 categorical usefulness prediction：material `deliver/silent` 与 DecisionTrace/action command 同事务，非 material `suppress` 先提交 PredictionRecord 再写 verified suppression event。点击、dismiss、沉默、timeout 和旧 `cognitive_outcomes` 只能作为 reaction/运营历史，不能关闭 `measured` 或进入客观校准分母。

predictive route 必须携带 application 从 canonical Wiki frontmatter 解析并授权的 `source_access_control` 以及 server-resolved write principal；ACL hash 进入 material input binding，PredictionRecord ACL 只从该 exact source envelope 收窄派生。OutcomeMeasurement 只能由固定 `TaskResultOracle` 从 exact canonical Raw tool observation 签发；authority catalog 必须逐字验证 revision、role-local span、content hash、issuance hash 与 committed projection receipt。calibration report 会重新读取 Raw 并重算这些绑定，不接受 caller 或存量行自报的 `eligible`。Raw 暂时不可读时保持 open/retry，恢复后再测量；只有 `sqlite3.OperationalError` 等临时存储故障可重试，永久 schema/constraint 错误进入 terminal censored 并让 daemon service degraded。终态只有 `measured/unknown/censored/confounded`；纠错必须追加 superseding OutcomeMeasurement 与 terminal revision，并再次通过 write authorization。

`project_prediction_terminal` 的 committed receipt 必须精确绑定 command schema、canonical terminal revision ID/hash/state、projection target、确定性 before/after hash，以及 command/revision/projection 三条 reciprocal refs。`CognitiveStateStore.record_effect_receipt()` 在写入时验证，`scripts/audit_prediction_outcome_lineage.py --strict --json` 再独立重算；仅有一条 receipt 或 transport success 不构成终态证明。crash 留下的 pending command 只能由 exact replay 补齐，伪造 receipt 不会让 reconciliation 误判完成。

存量 `delivery_events.channel='predictive_push'` 只做对象级 provenance migration。逐行稳定 source identity、schema fingerprint、主键、字段分母和完整内容 hash 进入 `historical_unverifiable_prediction` quarantine；不得补造 prediction、window、confidence、DecisionTrace、outcome、error、terminal 或 calibration input。正式操作顺序如下：

```bash
# 1. daemon 必须先停止；审阅 fresh inventory 的逐项分母与完整 hash。
python3 mnemos_daemon.py stop
python3 scripts/reconcile_prediction_history.py --json

# 2. 只有用户明确确认上一步 exact inventory_hash 后才允许 apply。
python3 scripts/reconcile_prediction_history.py \
  --apply \
  --inventory-hash 'sha256:<reviewed inventory_hash>' \
  --backup-dir ~/.mnemos/backups/prediction-history-<unique-stamp> \
  --json
python3 scripts/audit_prediction_outcome_lineage.py --strict --json

# 3. 用 apply 报告中的 immutable restore manifest 演练精确恢复。
python3 scripts/reconcile_prediction_history.py \
  --restore-manifest '<backup.db.restore.json>' \
  --json

# 4. 恢复后重新 dry-run、重新审阅 hash、使用全新备份目录最终 apply。
python3 scripts/reconcile_prediction_history.py --json
python3 scripts/reconcile_prediction_history.py \
  --apply \
  --inventory-hash 'sha256:<fresh reviewed inventory_hash>' \
  --backup-dir ~/.mnemos/backups/prediction-history-<new-unique-stamp> \
  --json
python3 scripts/reconcile_prediction_history.py \
  --apply \
  --inventory-hash 'sha256:<same final inventory_hash>' \
  --backup-dir ~/.mnemos/backups/prediction-history-<replay-unique-stamp> \
  --json
python3 scripts/audit_prediction_outcome_lineage.py --strict --json
```

apply/replay 每次都重算 source inventory、持有 daemon PID lock、创建新的 SQLite backup 并验证 integrity/snapshot；source drift、未知 schema、非零 active prediction 或 restore postimage 不一致均 fail closed。历史 quarantine 永远不满足 post-activation runtime PredictionRecord 分母。PredictionLedger 的实现 identity 同时覆盖 `prediction_ledger.py`、`prediction_ledger_support.py` 与 `prediction_outcome_support.py`；任一 oracle/outcome/calibration 逻辑变更都会使 code hash 改变。该闭环只关闭 COG-037，不自动关闭 COG-038/048、全局发布门或 real-API certificate。

#### Feedback attribution、correction 与历史 provenance

`FeedbackAttributionStore` 是 `UserReactionEvent`、attribution revision、correction state 和 downstream command 的唯一 deep owner。reaction 只记录 exact subject/delivery/search/decision/prediction/action、principal/scope、观察时间和 evidence；它不能自报 reward、objective label、materiality、eligible target 或 effect success。`OutcomeMeasurement` 仍由 COG-037 的客观证据合同独立签发。weak click/open/ignore/silence 只能累计观察；达到系统 materiality 规则时也只生成 proposal command，不能直接改 trust、policy、persona、belief、reflection、scorer 或 optimizer。

correction 必须精确 supersede 当前 event。owner 会重建完整 attribution revision chain：尚未执行的旧 command 在同一个 cognitive-state UOW 内写 `superseded_before_effect` terminal consumption/effect receipt；已经 committed 的每个 target effect 必须由 domain owner 提供 revoke、compensate 或 suppress receipt。只有所有旧 effect 已证明 inactive，replacement target 才能激活。crash/restart 按 canonical keyset 分页重放缺失 command，exact replay 不增加 reaction、command、effect 或 receipt。

正式入口必须使用服务端 principal 或 CLI 的 OS-bound local principal。private attribution identity 同时绑定 subject、exact scope、`principal_id` 与 agent，reaction 聚合不得跨 principal；recap 使用持久化 session/project，caller narrowing 只能匹配或收窄，dialog proposal/reminder 使用持久化对象 ID 派生的稳定 session scope，调用方不能重绑定。proposal、reminder、predictive push、Context Search、recap feedback/skip、adaptive scorer 与 reflection 都不得恢复 direct fanout。legacy scoring/reflection rows 在迁移后仍是只读历史：active reader 必须按 source class 排除，quarantine 本身不构成 exclusion 的替代品。

生产迁移分两步，daemon 在整个 schema/history apply、审计和 replay 期间保持停止。先显式把 `producer_consumer_ledger.db` 从 v2 升到 canonical v3；再盘点全部 legacy feedback 对象。history inventory 用 database/table/primary key/schema fingerprint/完整 row hash 固定对象，不按主题、时间、label 或相邻表猜 identity。apply 同时锁定 source 和 target，锁内备份与校验 source snapshot，target append 与 coverage/active-state 检查位于一个事务；任一步失败必须回滚 exact preimage。备份目录为 `0700`，SQLite/manifest 为 `0600`。

```bash
# 1. 停止 daemon，并预览/安装 cognitive-state v3。
python3 mnemos_daemon.py stop
python3 scripts/reconcile_cognitive_state_store.py --json
python3 scripts/reconcile_cognitive_state_store.py \
  --apply --backup-dir ~/.mnemos/backups/cog038-state-v3-<unique-stamp> --json
python3 scripts/reconcile_cognitive_state_store.py --json

# 2. 审阅 exact object denominator 和两个 hash 后，才执行 history apply。
python3 scripts/reconcile_feedback_attribution_history.py --json
python3 scripts/reconcile_feedback_attribution_history.py \
  --apply \
  --expected-inventory-hash 'sha256:<reviewed inventory_hash>' \
  --expected-object-manifest-hash 'sha256:<reviewed object_manifest_hash>' \
  --backup-dir ~/.mnemos/backups/cog038-feedback-<unique-stamp> \
  --json

# 3. second dry-run、strict audit 和 source/target integrity 必须全部通过。
python3 scripts/reconcile_feedback_attribution_history.py --json
python3 scripts/audit_feedback_attribution.py --strict --json
python3 scripts/audit_cognitive_source_authority.py --strict --json
python3 scripts/audit_schema_registry.py --strict --json
```

`reconcile_feedback_attribution_history.py` 的 restore 只接受 apply 生成的 immutable `feedback-history-manifest.*.json`，并要求 daemon 仍停止。restore 在旧 target 与已验证 staged inode 上同时持有排他锁，验证通过后原子替换；SQLite backup 与 manifest 从 inode 创建时即为 `0600`：

```bash
python3 scripts/reconcile_feedback_attribution_history.py \
  --restore-manifest '<backup-dir>/feedback-history-manifest.<hash>.<stamp>.json' --json
```

最终 strict 验收必须同时满足：legacy object `uncovered=0`、active promotion=0、command without receipt=0、receipt without command=0、pending superseded command=0、current target terminal gap=0、formal user seam bypass=0、legacy active reader=0、unauthorized cognitive update=0；并证明超过 10,000 个真实 SQLite command 的 bounded replay convergence 和零重复。该证据只关闭 COG-038，不代表 COG-048、全局 maintainability/zombie closure 或 release certificate 已完成。

2026-07-18 的生产闭环已按上述顺序完成。`producer_consumer_ledger.db` 从 v2 registry 显式升级为 canonical `mnemos.cognitive_state_store.v3`，升级前备份位于 `~/Desktop/Mnemos-migration-backups/COG-038-20260718/state-v3`。历史 denominator 固定为 3,625 个对象：delivery feedback 236、scoring/search 3,005、reflection/optimizer 384；inventory 为 `sha256:0b9854759e4ea51696063152c32caea635f18388e212b0f7a55dd53a70569b15`，object manifest 为 `sha256:6c307444608e13a6d39330b3b2eb86b983599b1ae1a2c4f7589604a5d490806b`。

首轮 apply 的 sealed manifest 位于 `~/Desktop/Mnemos-migration-backups/COG-038-20260718/history-apply/feedback-history-manifest.0b9854759e4ea516.20260718T125002271840Z.json`，manifest hash 为 `sha256:13f38f62d68a93cd74757e2baa164adde6fe7b6c37b20b17fdeeee90aabd3891`。该 manifest 已真实 restore，并验证恢复后 uncovered 回到 3,625、schema 仍为 canonical v3；随后使用新的 `history-reapply` 备份重新 apply，再以 `history-replay` 备份执行幂等 replay。最终 replay 为 `inserted=0`、`existing=3625`，coverage 为 covered=3,625、uncovered=0、unexpected=0、active promotion=0，active head/revision delta 均为 0。`audit_feedback_attribution.py --strict --json` 无 finding、全指标为 0；`producer_consumer_ledger.db`、`delivery_events.db`、`feedback_signals.db`、`mnemos.db`、`reflections.db`、`rule_weight_optimizer.db` 的 `PRAGMA integrity_check` 均为 `ok`，迁移 barrier 已清除。操作期间 daemon 与全部 Mnemos MCP 服务保持停止；不要删除这些备份或用后续普通运行覆盖本段作为迁移完成证据。

最终 clean-commit hermetic 验证为 isolated Quick `6822 passed in 1302.29s`，environment hash `04cb169670cbe287264fb271c4a11fde8bc876a5771428bd34ea8054548185b6`、`outside_write_count=0`、`formal_state_diff=[]`；Integration `350 passed in 167.48s`，environment hash `1db35bced3b284ac62b5eb16401accab8daaa5e71568759bf49f37e75849bfbc`；Heavy `19 passed in 205.81s`，environment hash `89de8fcb318db3310429795f2766545ff32b45f2451187fca07b9698b07e8fb7`。最终双轴 review 的 hard/spec finding 均为 0；唯一保留的判断项是固定点 diff 过大导致的可审阅性。writer 与独立 audit 都会把 available decision/prediction/action ref 解析到真实 canonical revision/action spec，并复核 prediction principal/project/session 与 prediction→decision/action 绑定；不存在的 available ref fail closed。project scope 可在空 session ID 时以独立 exposure identity 满足 weak materiality 的“session 或 exposure”阈值。Reflection 活跃读取的回归用例明确证明：退役 `outcome_feedback` 对象被隔离，正常 Layer5/shift 即使带非空 `source_event_id` 也保持可读。

target adapter 的 replay 必须先调用 exact domain oracle 复核既有 proposal、receipt、DecisionTrace/action refs 与 material terminal；验证通过直接返回同一 effect，不得重新运行 trusted/material gate。跨秒回归固定首轮与 replay 的 gate 时间相差一秒，证明 wall-clock 不再改变同一 command 的 DecisionTrace payload；既有 proof 损坏、缺失或不匹配仍 fail closed。

#### Trustworthy training admission、model governance 与历史 provenance

`TrainingGovernanceStore` 是训练 admission、稳定 split、correction/tombstone、run sealing/apply、model activation 与 reciprocal receipt 的唯一 deep owner。正式 label 必须来自 COG-038 `training_evidence` command，并精确绑定 attribution、PredictionRecord、成熟 OutcomeMeasurement、DecisionTrace/action、subject/principal/scope 与 source authority；点击、忽略、沉默、同一 reaction 的 expected/actual、未成熟或 confounded outcome 都不能成为训练真值。split 只由 outcome 之前已冻结的 identity 决定，holdout 不能参与 fit/tuning。model manifest 必须绑定输入集合、实现/spec、train/validation/holdout denominator 与 hash；correction 会先 tombstone 旧 sample、使受影响 model stale，再由 deterministic rebuild 产生新 lineage。

`daemon/training_governance_service.py` 只做 bounded reconcile 和 ready/stale run；Chronos 的旧 `scorer_training` step 只能委托该 service。`AdaptiveScorerV2.process_training_queue()`、旧 ground-truth/feedback queue writer、Bayesian direct update、RuleWeightOptimizer 读写与旧 model activation 均永久 fail closed，错误码固定为 `training_admission_receipt_required`。不得通过 alias、兼容开关、caller-signed permit、测试 allowlist 或恢复 legacy table reader 绕过。

生产迁移必须保持 daemon 与 Mnemos MCP 停止，并按 dry-run、apply、真实 restore、reapply、zero-insert replay 顺序执行：

```bash
python3 scripts/reconcile_training_governance_history.py --database-dir ~/.mnemos --json
python3 scripts/reconcile_training_governance_history.py \
  --database-dir ~/.mnemos \
  --apply \
  --expected-inventory-hash 'sha256:<reviewed inventory_hash>' \
  --expected-object-manifest-hash 'sha256:<reviewed object_manifest_hash>' \
  --backup-dir ~/.mnemos/backups/cog048-training-<run-id> \
  --json
python3 scripts/reconcile_training_governance_history.py --database-dir ~/.mnemos --json
python3 scripts/audit_training_governance.py --database-dir ~/.mnemos --strict --json
```

restore 只接受首轮 apply 生成的 immutable `training-history-manifest.*.json`；恢复后必须验证 exact preimage，再用新备份目录 reapply。最终 dry-run 必须显示 `apply_required=false`，strict audit 必须同时满足历史 `uncovered/invalid/unexpected=0`、active promotion=0、legacy active reader=0、producer/scheduler/model-reader bypass=0、admission/split/receipt/model manifest/correction 全部零缺口。

2026-07-19 生产 denominator 为 25,139，inventory `sha256:edc48c2dcb39f3f406a83652d5e6e2a67f446fd75f76aef5bdf28f2d95b63fec`、object manifest `sha256:d1f03b507addd5e2820bc6d3b277d966be840ca70825ce37d9159bc2ac4ea1b3`。首轮 manifest 位于 `~/Desktop/mnemos-cog048-training-migration-20260719/01-apply/training-history-manifest.edc48c2dcb39f3f4.20260718T224636973206Z.json`；真实 restore 的 recovery manifest、独立 reapply 和 replay 均已验证。最终 covered=25,139、uncovered/invalid/unexpected=0、25 项 strict 指标=0、state schema canonical v4、training projection schema canonical v1、四个生产数据库 integrity=`ok`。备份根 `~/Desktop/mnemos-cog048-training-migration-20260719/` 在完成独立保留策略前不得删除。该证据关闭 COG-048 与 Phase 3 root，不代表全局 maintainability、mypy、security、runtime backlog、zombie/vulture 或 release certificate 已通过。

### 定期维护

```bash
# 清理 events.db
sqlite3 ~/.mnemos/events.db "VACUUM"

# 清理旧 processed_sessions（跳过 90 天前的 skipped）
sqlite3 ~/.mnemos/wiki_state.db \
  "DELETE FROM processed_sessions WHERE processed_at < datetime('now', '-90 days') AND distill_method LIKE 'skipped%'"

# 检查数据库大小
du -h ~/.mnemos/*.db
```

---

## 七、备份与恢复

### 备份

```bash
python3 mnemos_cli.py backup create --dry-run --json
python3 mnemos_cli.py backup create --reason before-migration --json
```

全局快照由 `MnemosSnapshotManager` 生成 `mnemos.snapshot_manifest.v2`，覆盖配置、SQLite、mnemos vault、raw vault、Action Ledger、迁移账本和模块状态。manifest 只记录路径、hash、大小和敏感字段策略，不写明文 secret。数据删除快照额外绑定精确 subject operation hash、完整 payload 校验、SQLite integrity、保留期限和显式过期清理策略；过期只会使删除申请 fail closed，不会自动清除恢复点。迁移、批量蒸馏更新、raw purge、Wiki 重建、自动修复、模块自动开关和加密迁移都必须有 snapshot 前置策略或明确豁免。

### 恢复

```bash
python3 mnemos_cli.py restore plan latest --json
python3 mnemos_cli.py restore apply <snapshot_id> --json
python3 mnemos_cli.py restore verify <snapshot_id> --json
```

恢复必须先 plan，再 apply，再 verify。目标文件与 manifest hash 不一致时，plan 返回 `blocked` 并列出冲突；没有快照时 `restore plan latest --json` 返回可解释的 `blocked`，不是静默失败。手工 `rsync/cp` 仅作为底层灾难恢复兜底，不能替代 manifest、checksum 和恢复后 health/verify_installation 复验。

---

## 八、监控与日志

### 关键日志

```bash
tail -f daemon.log                              # 实时日志
grep -E "ERROR|FAIL|异常" daemon.log            # 只看错误
grep -E "蒸馏|distill|hephaestus" daemon.log    # 蒸馏相关
```

### 外部集成

如需 Prometheus/Grafana，可扩展 `core/ops/health_check.py --json` 输出，配合 node_exporter textfile collector 或 pushgateway。

---

## 九、性能调优

### API 限流
- 按用户配置的 LLM / Embedding / Reranker 服务商限额设置 `rate_limits`
- 多 LLM 节点时可使用 `llm.chain` 与 `routing_strategy` 控制顺序、并发或降级
- 所有模型单次请求 timeout 统一为 120s，且蒸馏 LLM 调用已改为流式输出：服务端逐 token 推送，客户端边收边拼，避免模型尚未生成完整响应时因非流式等待超时而整体取消
- 默认 `llm.routing_strategy = "sequential"` 会先用 DeepSeek V4 Flash，失败后再走免费后备；频繁限流时调低 `distill.max_tasks_per_cycle`（默认 5）

### EventBus 配置
```toml
# ~/.mnemos/config.toml
# EventBus settings group
queue_depth_alert = 1000
max_queue_depth = 10000
max_recover_events = 1000
```

### 数据库
- SQLite WAL 模式已启用
- 数据库 > 100MB 时考虑 `VACUUM`

### Vault 展示/分类审计

```bash
python3 scripts/audit_wiki_quality_contract.py --strict
python3 scripts/wiki_lint.py --summary --json
python3 scripts/wiki_lint.py --summary --json --budget
python3 mnemos_cli.py vaults audit-content
python3 mnemos_cli.py vaults audit-content --json
python3 mnemos_cli.py vaults audit-placement
```

- `wiki_lint --summary --json` 输出稳定的 `mnemos.wiki_quality.v1`，包含页面/issue 统计、missing_meta/orphan/broken_link/stub 到统一生命周期的映射、`obsidian_experience` scorecard 指标、预算线和人工确认样本；`--budget` 用预算线阻断重建后仍超线的 vault。
- `wiki_lint --fix` 只自动补齐已有 frontmatter 页面的缺失元数据；每次真实写入都会记录 `wiki_quality_fix` 到 Action Ledger。断链、孤岛和 stub 不自动改正文，只进入 owner 标记的人工清单。
- `audit-content` 只读检查 Obsidian 展示、分类和结构化输出问题；用于定位长文件名、source/session 前缀文件、已结构化但仍在 Inbox 的页面、needs_review 页面、标题碰撞和 frontmatter 缺字段。
- `audit-placement` 只读检查同 basename、大小写同名和 KG entity 投影撞名；需要实际归档相同内容同名页时再用 `repair-placement --apply`，且默认会拒绝 dirty vault。

### KG 一致性

```bash
python3 mnemos_cli.py kg consistency --json
python3 mnemos_cli.py kg consistency --apply --json
python3 mnemos_cli.py kg normalize-endpoints --json
python3 mnemos_cli.py kg normalize-endpoints --apply --json
python3 mnemos_cli.py kg doctor
```

- `kg consistency` 输出 `mnemos.kg_consistency.v1` 与 `mnemos.kg_consistency_repair.v1`，硬一致性范围包括 `relation_evidence`、`relation_context_embeddings`、`relations_fts` 不能指向不存在的 `relations.id`，且每条 relation 必须有 FTS 行。
- `--apply` 只清硬孤儿并补缺失 FTS，默认先在 `~/.mnemos/backups/kg-consistency/` 创建定向 `knowledge_graph.db` 备份，不创建全局 snapshot；执行后复验 `integrity_check=ok`、三类 hard_orphans 均为 0、`relations_missing_fts=0`。
- `endpoint_gaps` 表示 relation 端点未映射到 entity 或现有 Wiki 文件。该项可能混有概念节点、旧链接、重命名页面和抽取噪声，不能为了清零而直接删除；需要通过实体归一化、路径迁移、明确非法端点清理或人工裁决分流处理。
- `kg normalize-endpoints` 输出 `mnemos.kg_endpoint_normalization.v1`，默认 dry-run。它默认只处理两类低风险端点：能唯一匹配到现有 Wiki basename 的旧路径会迁移 relation source/target 并同步重写 `relations_fts`；被多条 relation 引用、不是路径且未命中现有 entity 的概念端点会补为 `kg_endpoint_auto` / `semantic_normalization` entity。`--apply` 默认先在 `~/.mnemos/backups/kg-endpoints/` 创建定向备份；唯一匹配冲突、自指关系、旧 Inbox 路径残留和无法判定概念都必须保留为 unresolved，不允许为了让 `endpoint_gaps` 归零而强行迁移或建实体。`--prune-invalid` 是显式删除开关，只能清理 marker、多行片段、附件、`07-Shadow/` 和 `L2.4-KG/Relations/` 投影、短中文半句等 `core/kia/relation_endpoint_quality.py` 判定为 prunable 的端点，并同步删除对应 relation 的 evidence/FTS/embedding 行。`RelationManager`、`KnowledgeGraph`、Charon 等生产写入入口必须统一走 `relation_writer.upsert_relation_row()`，不得直接 `INSERT INTO relations`。
- 2026-07-08/09 真实库已执行并追补两轮 KG endpoint 治理：第一轮语义归一化将 `endpoint_gaps.count` 从 499 降至 342，迁移 relation 321 条并补齐 69 个概念 entity；第二轮根因修复后执行 `--prune-invalid --apply`，备份到 `~/.mnemos/backups/kg-endpoints/knowledge_graph-20260708155009.db`，删除 invalid relations 1153 条，endpoint gaps 降至 291。追补修复将 `RelationManager.add_from_distill()` / `apply_implicit_relations()` 纳入同一 endpoint gate，并让自动关系发现跳过 `07-Shadow`、`L2.4-KG/Relations`、`99-Reports`、`99-Archive` 和 entropy suggestion 等派生产物。执行后 `integrity_check=ok`、三类 hard_orphans 均为 0、`relations_missing_fts=0`，复跑 dry-run 的 `would_apply.path_migrations=0`、`would_apply.concept_entities=0`、`would_apply.invalid_relations_deleted=0`。

---

### 文档、Prompt 与 Desktop 资产门禁

`python3 scripts/audit_document_asset_manifest.py --strict --desktop-mode required --json` 是完整文档资产验收入口。它从 Git tracked 文件自动发现所有 repo Markdown，按 manifest 对账 Prompt/schema 的 SHA-256、consumer symbol、loader binding 和 output contract，并核对 Desktop 图谱资产：`00–10` 必须在同一 evidence 行同时引用 current-state 与存在的 repo 锚点，`86–98` 头部必须是当前 repo commit，`99` 必须对齐 current_state。当前 exclude=0、unverified=0。CI 无 Desktop 时只能使用显式 `--desktop-mode skip` 做 repo/Prompt 和静态分类校验；full-score 的 `docs.asset_manifest.strict` gate 使用 required profile，不能以 CI skip 取得发布证书。freshness 与 sensitive 复用同一 tracked Markdown 发现。

## 十、资源速查

| 组件 | 检查命令 | 日志/路径 |
|------|----------|-----------|
| Daemon | `pgrep -f mnemos_daemon.py` | `~/.mnemos/logs/daemon.log` |
| Amphora | `python3 -m core.kia.amphora --stats` | `~/.mnemos/distill_queue.db` |
| EventBus | `sqlite3 ~/.mnemos/events.db` | `~/.mnemos/events.db` |
| LLM API | `python3 -m core.ops.health_check` | daemon.log |
| 安装生命周期 | `python3 mnemos_cli.py setup --dry-run --json` | `~/.mnemos/install_state.json` |
| 安装验证 | `python3 verify_installation.py --json` | Obsidian/raw vault |
