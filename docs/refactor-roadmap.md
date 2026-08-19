# 复杂函数重构路线图

对应审计项 **S7**。目标：降低圈复杂度最高函数的维护风险，优先治理真实变更热点。

## 当前 Top 10 复杂函数（按 radon cc）

| 排名 | 文件 | 函数 | 当前复杂度 | 目标 |
|------|------|------|------------|------|
| 1 | `mnemos_cli.py` | `main` | F (70) | 拆分为子命令分发器 + 各子命令处理函数 |
| 2 | `scripts/refactor_magic_numbers.py` | `derive_semantic_name` | D (28) | 抽取规则表与命名策略对象 |
| 3 | `mnemos_daemon.py` | `run_daemon` | D (29) | 按生命周期阶段拆分为 init/event-bus/modules/loop |
| 4 | `scripts/health_check.py` | `check_git_uncommitted` | D (21) | ✅ 已拆分为 `_git_*` helper |
| 5 | `scripts/migrate_vault_layout.py` | `migrate` | C (20) | 抽取目录迁移步骤与回滚逻辑 |
| 6 | `scripts/clean_relations.py` | `main` | C (19) | 抽取清理阶段与统计输出 |
| 7 | `scripts/refactor_magic_numbers.py` | `unit_suffix` | C (18) | 抽取单位映射表 |
| 8 | `scripts/health_check.py` | `generate_health_report` | C (18) | 按检查项拆分为独立 report section builder |
| 9 | `scripts/refactor_magic_numbers.py` | `is_in_string_or_comment` | C (17) | 抽取 token 状态机 |
| 10 | `scripts/clean_kg.py` | `get_bad_entity_names` | C (17) | 抽取实体质量检查规则 |

## 重构原则

1. **先补 characterization tests**：锁定当前输入输出，再动手拆分。
2. **早返回 + 策略对象**：用子命令表、规则表、步骤函数替代深层 if/else 嵌套。
3. **禁止扩大外部 API**：重构期间不新增公开函数签名；必须新增时单独写迁移说明。
4. **复杂度降到 B 以下**：单个函数 radon cc <= 10。
5. **一函数一提交**：保持回滚粒度小。

## 已完成的重构

- `scripts/health_check.py::check_git_uncommitted` 已拆分为 `_run_git`、`_filter_sensitive`、`_git_last_commit`、`_git_uncommitted_files`、`_git_diff_summary`、`_git_untracked_files`；主函数仅负责编排，复杂度从 D 降至 B。

## 监控

```bash
# 本地检查平均复杂度与高风险函数
python3 -m radon cc core integrations scripts mnemos_cli.py mnemos_daemon.py -s -a -nc

# CI 中可设置阈值，例如平均复杂度不超过 C 且不允许 F 级函数
python3 -m radon cc core integrations scripts mnemos_cli.py mnemos_daemon.py -nc --min=C
```
