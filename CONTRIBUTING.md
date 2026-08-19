# Contributing to Mnemos

感谢你对 Mnemos 的兴趣！以下是贡献指南。

## 开发环境搭建

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
pip install -e ".[dev]"
pip install pre-commit
pre-commit install
```

`pre-commit install` 后，每次 `git commit` 会自动跑本地门禁（见下节）。如不想安装 pre-commit，可手动运行：

```bash
python3 scripts/run_local_gates.py
```

## 运行测试

按反馈速度分层运行：

```bash
python3 run_tests.py quick        # 日常快速反馈：unit + root smoke，跳过 packaging/benchmark/e2e
python3 run_tests.py integration  # 集成、验收、system test
python3 run_tests.py system       # 仅 system test；CI 三平台共用 hermetic runner
python3 run_tests.py heavy        # packaging、benchmark、e2e
python3 run_tests.py full         # 完整 tests/
```

根目录 `run_tests.py` 与 `scripts/run_tests.py` 共用同一实现；需要保留旧脚本路径的 automation 可继续调用 `python3 scripts/run_tests.py <layer>`。

本地全量覆盖率测试（与 CI 保持一致）：

```bash
python3 -m pytest tests/ \
  --cov=core --cov=integrations --cov=mnemos_cli --cov=mnemos_daemon \
  --cov-fail-under=70 -q
```

单独跑 system test：

```bash
python3 scripts/run_tests.py system
```

`system` 入口与其他 layer 一样由 `HermeticRunEnvironment` 创建唯一沙箱并直接以 argv 调用 pytest，不依赖 POSIX env prefix、`mktemp` 或 shell command substitution；GitHub Actions 的 Linux、macOS、Windows matrix 必须调用这个入口。

## 代码规范

- Python >= 3.10
- 使用 `pathlib.Path` 处理路径，不要硬编码 `/` 或 `\\`
- 数据库表名拼接必须加白名单校验（参考 `signal_store.py` 的 `ALLOWED_SOURCES`）
- `except Exception:` 至少记一条 `logger.warning`，不要裸 `pass`
- f-string 里不能有反斜杠（Python <3.12 的兼容性问题）
- 新增公共函数/类必须带 docstring
- 新增 `TODO/FIXME/DEBT` 注释必须带 `(owner/date/issue)` 或关联 `docs/technical-debt.md` 中的 `Sxx` 编号，例如：
  - `# TODO(2026-06-25): S6 推进类型注解`
  - `# FIXME(2026-06-25,maintainer): S7 重构 cmd_doctor`
  - `# DEBT(S25): 敏感 DB 明文存储`
- 运行 `python3 scripts/check_tech_debt_annotations.py` 检查债务标记格式

提交前请确保以下命令全部通过（pre-commit 已安装时会自动执行本地门禁项）：

```bash
# 方式 1：一键跑本地门禁（不依赖 pre-commit）
python3 scripts/run_local_gates.py

# 方式 2：逐条手动跑关键子门禁（完整清单以 run_local_gates.py 为准）
python3 -m flake8 --count
python3 scripts/mypy_budget.py
python3 -m compileall -q core/ integrations/ daemon/ scripts/ mnemos_cli.py mnemos_daemon.py
python3 scripts/check_bare_except.py
python3 scripts/check_tech_debt_annotations.py core/ integrations/ daemon/ scripts/ mnemos_cli.py mnemos_daemon.py
python3 scripts/audit_document_asset_manifest.py --strict --desktop-mode required --json
python3 scripts/audit_repo_sensitive_literals.py --strict
python3 scripts/audit_release_privacy_security.py --strict
python3 scripts/arch_dependency_graph.py --check
python3 scripts/check_maintainability_budget.py --closure
python3 scripts/check_zombie_code_policy.py --closure
python3 scripts/ci_ratchet.py --closure --strict
python3 -m vulture --min-confidence 80 .
python3 -m bandit -r core/ integrations/ daemon/ scripts/ mnemos_cli.py mnemos_daemon.py -ll -ii
```

pre-commit 不会跑全量测试，提交/推送前仍需手动执行：

```bash
python3 scripts/run_tests.py quick
python3 scripts/run_tests.py heavy
```

涉及配置、secret、安装、迁移、隐私、retention 或磁盘预算的改动，还必须运行 strict 配置验收、secret doctor、health 和发布级隐私安全总门禁；配置报告会写入 `~/.mnemos/config_audit.json`，总门禁输出 `mnemos.release_privacy_security.v1`。该报告通过 `mnemos.secret_inventory.v1` 递归扫描 `api_key/token/secret/password/credential/bearer/key_source`，应只包含字段路径、引用来源或长度统计，不应包含明文 key/token/secret、真实 API URL、本机绝对路径或未脱敏 key source；默认 doctor 文本、config/health/verify JSON、`mnemos_cli.py distill status` 和 `scripts/e2e_probe.py --dry-run --no-api` 都应使用脱敏输出，只有本机私有排错才使用 `--unsafe-debug` 或 `--show-paths`。keyring/env fallback 改动还必须运行 `python3 mnemos_cli.py secrets doctor --json`，确认 `mnemos.keyring_doctor.v1` 中 `secret_inventory_plaintext_count=0`，且 keyring 不可用时只能在 `security.accept_env_secret_fallback=true` 后把 env 降级视为显式接受。SQLite 不再做整库加密；WAL/temp/snapshot/raw_events 体积和增长率由 `checks.sqlite_disk_budget` 监控，`.db-wal` checkpoint 和过期 Mnemos temp 清理可通过 `scripts/repair_sqlite_disk_budget.py` 安全执行，snapshot/raw_events 删除必须人工确认：

```bash
python3 mnemos_cli.py doctor config --strict --json
python3 mnemos_cli.py secrets doctor --json
python3 mnemos_cli.py health --json
python3 scripts/repair_sqlite_disk_budget.py --dry-run
python3 scripts/audit_release_privacy_security.py --strict --json
```

满分/发布复验使用统一入口，默认把报告写到 `/tmp/mnemos-full-score-gates/...`：

```bash
python3 scripts/run_full_score_gates.py --strict --real-api
python3 scripts/check_maintainability_budget.py --closure --strict --json
python3 scripts/check_zombie_code_policy.py --closure --strict --json
```

本地门禁是 development profile：精确且未过期的 accepted residual 会明确输出 `release_eligible=false`，不能当发布证书。发布 profile 要求 maintainability、zombie、vulture residual 全部为 0。普通 baseline update 只能固化改善；新增或替换风险必须显式审批，vulture 非零禁止 rebaseline。

新增或修改任何 tracked Markdown、`prompts/` 模板/schema 或 Desktop `mnemos系统图谱` 资产时，必须更新 `docs/acceptance/document_asset_manifest.json` 并运行 document asset manifest gate。Prompt 合同要同步精确 hash、实际 consumer 和输出 schema/inline contract；Desktop 当前契约要同时列出 current-state 与代码锚点，不能只引用历史扫描或单个 facts 文件。

## Commit Message 规范

本项目采用 `<type>(<scope>): <description>` 格式，便于生成 changelog 与自动化识别。

```
feat(kia): 为 entity_manager 增加批量写入接口
fix(sync): 修复 capture_queue 计数器漂移
refactor(daemon): 将 heartbeat 逻辑抽到 daemon/heartbeat.py
docs(kia): 补充 KIA 模块职责映射表
test(sources): 为 kimi_source 增加归档文件解析测试
chore(ci): 在 CI 中增加 pip-audit 扫描
```

- `type` 必须是以下之一：
  - `feat`：新功能
  - `fix`：缺陷修复
  - `refactor`：代码重构（不改变外部行为）
  - `perf`：性能优化
  - `test`：测试相关
  - `docs`：文档相关
  - `chore`：构建/工具/依赖
  - `security`：安全修复
- `scope` 可选，建议填写受影响的顶层模块，如 `kia`、`hephaestus`、`sync`、`daemon`、`sources`、`cli`、`ci`、`docs`
- `description` 使用祈使句，首字母小写，末尾不加句号
- 中英文均可，但同一 PR 内请保持一致；面向开源场景建议使用英文
- 需要时可使用 `BREAKING CHANGE:` 脚注说明不兼容变更

## 提交 Issue

请包含：

- 操作系统和 Python 版本
- 复现步骤
- `mnemos doctor` 的输出

## 提交 PR

1. 先跑测试确保通过（参考上文“运行测试”）
2. 描述改动内容和原因（PR 模板会自动提示）
3. 如果改的是跨平台相关代码，请说明在哪些平台测试过
4. 每次 PR 尽量聚焦单一目标，避免超大 diff
