# Mnemos 技术债务清单

> 本文件跟踪当前已知技术债务，对应 `MNEMOS_CODE_AUDIT_2026_06_24.md` 中的改进项。
> 新增 TODO/FIXME/DEBT 注释必须带 `(owner/date/issue)` 或关联本文档条目，否则 CI 检查会失败。

## 债务标记规范

代码中临时注释统一使用以下格式：

```python
# TODO(2026-06-25): S6 推进 core/embeddings 类型注解
# FIXME(2026-06-25,maintainer): S7 将 cmd_doctor 复杂度降到 B 以下
# DEBT(S25): SQLite 磁盘预算、WAL/temp 修复和 raw_events 保留策略持续评估
```

- `TODO`：计划要做的改进
- `FIXME`：已知缺陷但短期未修复
- `DEBT`：架构/设计层面的长期债务
- 括号内至少包含日期；多人协作时建议同时包含 owner；与本文档条目关联时写 `Sxx` 编号

## 按审计项分类的债务

### 代码质量

| 编号 | 债务项 | 位置 | 状态 | 备注 |
|------|--------|------|------|------|
| S6 | 类型注解推进 | 全项目 | 待处理 | 核心公共 API 优先，逐步启用 `check_untyped_defs` |
| S7 | Top 10 复杂函数重构 | `mnemos_cli.py:main`、`core/cli/commands/doctor.py:cmd_doctor`、`core/hephaestus/distillation_wiki_page.py:generate_wiki_page` 等 | 已处理（P1 5.1 完成） | 先补 characterization tests 再拆分 |
| S8 | 异常处理专项 | 全项目 | 进行中（accepted debt，发布阻断） | 当前 478 个 broad catch、120 个未分类、required-path 0；v2 baseline 对已接受项存 exact fingerprint 与时限接受，same-count replacement/parse failure/expiry/baseline drift fail closed；strict full-score 要求 residual=0 |
| S12 | 超大文件拆分预算 | `core/kia/knowledge_graph.py`、`core/scoring/adaptive_scorer_v2.py`、`core/kia/ixion.py`、`core/kia/charon.py`、`mnemos_daemon.py`、`scripts/auto_setup.py` 等 | 已处理（Phase 2，19/19 清零） | 2026-07-16 原 19 个超大生产文件全部拆到 1500 行以内，`large_files=0`，未新增豁免或扩大 baseline；这只关闭 oversized-file 分母，全局维护性仍有 474 个 broad catch、119 个未分类项，不能据此获得 release certificate |
| S13 | 审计报告写入污染 | `scripts/audit_orphan_modules.py`、`docs/orphan-modules-report.md` | 已处理 | 默认 stdout 只读，`--check` 只比较不写；repo 内报告刷新必须 `--output ... --apply` 并写 ActionLedger；报告头使用 `<repo>`，不写本机路径 |
| S9 | 清理死代码 | `vulture_whitelist.py`、零引用模块 | 已处理（vulture 白名单清零） | W0-1~W2-228 已累计迁出 292 条白名单项；W2-228 将 `DistillProgress.WRITING` 固化为 Amphora 队列 `writing` 进度阶段契约；当前 `0/310` |
| S10 | Agent source 重复实现 | `integrations/sources/*.py` | 已处理（P2 6.2 完成） | 抽象 `BaseAgentSource` 通用 helper |
| S17 | 依赖注入推广 | 核心服务构造函数 | 待处理 | 构造函数接受 `config=None`，入口层默认 `get_config()` |

Phase 2 闭环（2026-07-16）：正式 Root `COG-027 / COG-018 / COG-011 / COG-028 / COG-029 / COG-044 / COG-047 / COG-010 / COG-012 / COG-013 / COG-049` 已 11/11 关闭，S12 的 19 个超大生产文件也已 19/19 清零。该结论是 Phase 2 issue/root closure，不是 full-score 或 release certificate；COG-050 与 effect/runtime-interface 的后续阶段红灯仍按各自分母保留。

### 架构与性能

| 编号 | 债务项 | 位置 | 状态 | 备注 |
|------|--------|------|------|------|
| S11/S46 | 命名映射与认知门槛 | `core/kia/` | 已修复 | `core/kia/README.md` 与 `core/README.md` 已补，后续新模块优先直观业务名 |
| S18 | Runtime dependency cycles | `scripts/arch_dependency_waivers.json`、`docs/core-integrations-dependencies.md` | 进行中（预算门已落地） | Runtime-only cycle 豁免必须写明 owner、target interface、resolution 和具体 arch-debt issue；`core.cli.helpers` 已改为依赖 `core.vaults.obsidian_registry`，不再反向依赖 integrations backend |
| S29 | Obsidian 后端真正索引 | `integrations/backends/obsidian_backend.py` | 已修复 | 已接入 RawIndex、扫描缓存 LRU、search/list_by_tags/update_tags 增量索引；2026-07-05 起 legacy backend 的 RawIndex DB 跟随当前 vault/chatlog 的 `.raw_index.db`，避免多 vault 共享全局锁和相对路径冲突 |
| S30 | 批量 flush 减少写放大 | `core/embeddings/relation_manager.py` | 已修复 | batch_flush 默认 True，atomic rename flush，close 强制 flush |

### 安全与隐私

| 编号 | 债务项 | 位置 | 状态 | 备注 |
|------|--------|------|------|------|
| S25 | SQLite 磁盘预算监控 | `core/ops/sqlite_disk_budget.py`、`scripts/repair_sqlite_disk_budget.py`、`core/ops/health_check.py`、`core/config.py` | 已修复 | 整库 SQLite 加密已删除；`.db-wal`、Mnemos temp、snapshot、`raw_events.db` 的体积/增长率进入 strict health，WAL/temp 有安全修复入口，snapshot/raw_events 需要用户确认 |

### 产品与落地

| 编号 | 债务项 | 位置 | 状态 | 备注 |
|------|--------|------|------|------|
| S44 | 聚焦核心链路打磨 | capture → distill → wiki → search → push | 进行中（黄金路径 gate 已落地） | `scripts/e2e_wow_probe.py --mock-llm` 与 `scripts/run_golden_benchmark.py --strict --mock-llm` 已通过；真实运行态仍需继续压低 evidence/source/behavior 缺口 |
| S47 | 安全加固整体收尾 | S18-S26 | 已处理（需定期复验） | 2026-07-08 `scripts/audit_release_privacy_security.py --strict --json` 通过，blocking/warning 均为 0 |
| S48 | 可演示惊艳场景 | `README.md`、`docs/demo/` | 已处理（mock demo/e2e 已落地） | `docs/demo/`、`scripts/e2e_wow_probe.py --mock-llm` 和 `tests/e2e/test_wow_path.py` 已覆盖端到端演示链路；社区入口/独立示例仓库仍归 TODOS-12 |

## 已修复债务

| 编号 | 债务项 | 修复日期 | 备注 |
|------|--------|----------|------|
| S25 | SQLite 磁盘预算与字段脱敏 | 2026-07-07 | SQLite 整库加密路径已删除；隐私边界改为字段脱敏、secret inventory、key reference 与 `mnemos.sqlite_disk_budget.v1` 运行态磁盘预算监控 |
| S30 | 批量 flush 减少写放大 | 2026-06-25 | `relation_embedding.batch_flush=True`，atomic flush |
| S37 | 核心模块 README | 2026-06-25 | 新增 `core/README.md` |
| S38 | 统一 commit 规范 | 2026-06-25 | 重写 `CONTRIBUTING.md`，新增 PR 模板 |
| S39 | 显式标记技术债务 | 2026-06-25 | `scripts/check_tech_debt_annotations.py` + 测试 |
| S8/S12 | 可维护性 ratchet / release closure 分层 | 2026-07-04 / 2026-07-12 | `mnemos.maintainability_closure.v1`；15 large + 510 broad accepted residual，124 unclassified、required-path 0；local 显式非发布，full-score strict residual=0 |
| S13 | 审计报告写入污染 | 2026-07-04 / 2026-07-05 | `scripts/audit_orphan_modules.py` 默认只读；`--check` 不写文件；repo 内 `--output docs/orphan-modules-report.md --apply` 写报告并记录 ActionLedger；生成报告头固定为 `<repo>`，避免本机路径写入 docs |
| S18 | CLI helper 反向依赖 integrations backend | 2026-07-05 | 新增 `core/vaults/obsidian_registry.py`，`core.cli.helpers` 和 `integrations.backends.obsidian_backend` 共同依赖 core vault registry port；`tests/test_arch_dependency_graph.py` 防止 helper 再导入 integrations |
| S40 | 可复制敏感测试值与本机路径字面量 | 2026-07-05 | F24 新增 `scripts/audit_repo_sensitive_literals.py --strict` 与 `tests/unit/test_repo_sensitive_literals_audit.py`，扫描 tracked 与未忽略的 untracked 文本，阻断完整 provider-shaped fake key、本机 home path 和明文 credential literal；已接入 local gates、pre-commit 和 CI |
| S47 | 安全加固整体收尾 | 2026-07-08 | `scripts/audit_release_privacy_security.py --strict --json` 聚合 strict security、strict config doctor、health security/privacy、docs sensitive 和 repo sensitive literal audit，当前 blocking/warning 均为 0 |
| S48 | 可演示惊艳场景 | 2026-07-08 | `scripts/e2e_wow_probe.py --mock-llm` 在隔离临时目录通过三项模型配置、可信文档导入、mock 蒸馏、Obsidian 路由、搜索/preflight 消费者对账和自愈 dry-run |
| S49 | 文档/Prompt/Desktop 资产分母 | 2026-07-12 | ROOT-022 已用 `mnemos.document_asset_manifest.v1` 闭合：70/70 tracked Markdown、23/23 Prompt/schema、25/25 Desktop assets，exclude=0、unverified=0；新增资产、hash/consumer/schema 漂移、Desktop 未分类/旧 commit 均 fail closed |

## 新增债务流程

1. 在代码中标记时，尽量指向本文档中的 `Sxx` 编号或具体日期/owner。
2. 如果是新发现的重要债务，先更新本文档，再写代码注释。
3. 每月Review一次本清单，将已修复项移到"已修复债务"表。
