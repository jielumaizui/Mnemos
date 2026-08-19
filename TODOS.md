# Mnemos 待办 / 推迟项

本文件由 `/autoplan` 在 Phase 1 CEO Review 中生成。

> 2026-07-08 核验说明：根目录 `PLAN.md` 已在 F24 Repo Sensitive Literal Audit 中作为陈旧计划文件删除；下文 `PLAN.md` 仅保留为历史来源引用，不再是当前待办事实源。当前事实源以本文件、`docs/technical-debt.md`、CI/local gates 和运行态审计输出为准。

## 2026-07-08 代码核验状态

- **已关闭**：TODOS-1、TODOS-11。
- **部分落地但仍需优化**：TODOS-5、TODOS-6、TODOS-10、TODOS-12。
- **仍打开**：TODOS-2、TODOS-3、TODOS-4、TODOS-7、TODOS-8、TODOS-9、TODOS-13。

## 推迟到后续迭代

- [x] **TODOS-1：引入 CI 架构治理门禁**
  - **What：** 在 CI 中加入检查：新的循环依赖、新增 `vulture_whitelist.py` 条目、新增直接 `os.environ`/`open()` 配置读取。
  - **Why：** 没有治理，循环依赖和 whitelist 会在 6 个月内重新长回来。
  - **Pros：** 长期保持架构健康。
  - **Cons：** 需要维护脚本和阈值。
  - **Context：** 参见 `PLAN.md` Phase 1 CEO Review。
  - **Effort：** M（人工 ~1 天 / CC ~20min）
  - **Priority：** P2
  - **Depends on：** Phase 1 依赖图脚本和配置审计脚本完成后。
  - **2026-07-08 核验：** 已关闭。`.github/workflows/ci.yml`、`.pre-commit-config.yaml` 和 `scripts/run_local_gates.py` 已接入 maintainability budget、docs freshness/sensitive、repo sensitive literals、arch dependency graph、CI ratchet、vulture 等门禁；`python3 scripts/ci_ratchet.py --check`、`python3 scripts/arch_dependency_graph.py --check`、`python3 scripts/audit_config_reads.py --check` 均通过。

- [ ] **TODOS-2：服务注册 / 依赖注入层**
  - **What：** 在 `core/` 与 `integrations/` 之间引入显式服务注册或 DI 容器，替代当前直接 import 耦合。
  - **Why：** 当前 `core/kia/`、`core/hephaestus/`、`core/app/` 之间存在循环依赖，需要更高层抽象。
  - **Pros：** 彻底打破循环，便于测试和替换实现。
  - **Cons：** 过早平台化可能增加复杂度。
  - **Context：** 参见 `PLAN.md` 0D 扩展项。
  - **Effort：** L（人工 ~3-5 天 / CC ~1-2h）
  - **Priority：** P3
  - **Depends on：** 本次架构清理完成、依赖图稳定后。

- [ ] **TODOS-3：重命名 `core/kia/` 等希腊神话模块为语义化名称**
  - **What：** 将 `kia/` 内模块（如 `aegis`、`chronos`、`charon`）重命名为反映职责的名称（如 `guard`、`scheduler`、`routing`）。
  - **Why：** 当前命名增加新开发者认知负担。
  - **Pros：** 可读性提升。
  - **Cons：** 破坏性大，需同步更新 import、测试、文档。
  - **Context：** 参见 `PLAN.md` 0D 扩展项。
  - **Effort：** L（人工 ~2-3 天 / CC ~30min）
  - **Priority：** P3
  - **Depends on：** 无。

- [ ] **TODOS-4：拆分超大文件**
  - **What：** 拆分仍超过 1500 行的生产文件。2026-07-08 核验样例：`core/hephaestus/distillation_engine.py`（1715 行）、`core/kia/knowledge_graph.py`（2271 行）、`core/kia/ixion.py`（2051 行）、`core/kia/ingest_helpers.py`（1823 行）、`integrations/apollon.py`（1716 行）、`core/kia/chronos.py`（1788 行）、`core/scoring/adaptive_scorer_v2.py`（2168 行）、`mnemos_daemon.py`（1976 行）等。
  - **Why：** 降低模块复杂度，打破神模块耦合。
  - **Pros：** 维护性、可测试性提升。
  - **Cons：** 大范围改动，破坏现有测试/导入。
  - **Context：** 参见 `PLAN.md` 0D 扩展项。
  - **Effort：** XL（人工 ~1-2 周 / CC ~数小时）
  - **Priority：** P3
  - **Depends on：** 循环依赖打破后，避免拆分引发新的循环。
  - **2026-07-08 核验：** 仍打开。预算门已防止继续增长，但当前 `scripts/check_maintainability_budget.py --json` 仍统计到 `large_files=17`。

- [ ] **TODOS-5：增加用户价值度量**
  - **What：** 测量首值时间、成功捕获率、蒸馏有用性、检索精度、主动推送接受率、guard 误中断率。
  - **Why：** Codex 指出当前计划缺少用户价值验证，应把下一次工作框定为“信任闭环加固”。
  - **Pros：** 指导产品优先级。
  - **Cons：** 需要埋点和数据分析。
  - **Context：** 参见 `PLAN.md` CEO Review Codex 盲点 10。
  - **Effort：** M（人工 ~2-3 天 / CC ~30min）
  - **Priority：** P2
  - **Depends on：** 产品决策。
  - **2026-07-08 核验：** 部分落地。`scripts/e2e_wow_probe.py --mock-llm`、golden benchmark、cognitive readiness、delivery/outcome ledger 已提供链路验收；但首值时间、长期蒸馏有用性、检索点击/打开率、主动推送接受率、guard 误中断率尚未形成统一产品指标和趋势看板，仍打开。

- [ ] **TODOS-6：跟踪 MCP 规范漂移风险**
  - **What：** 建立 MCP 协议版本跟踪机制，确保 `integrations/agora.py` 兼容未来 Claude Code / MCP 版本。
  - **Why：** AI 助手生态快速演进，MCP 不兼容会重写 integrations/ 层。
  - **Pros：** 降低外部兼容性风险。
  - **Cons：** 需要持续关注规范更新。
  - **Context：** 参见 `PLAN.md` CEO Review Codex 盲点 5。
  - **Effort：** S（人工 ~2h / CC ~5min）
  - **Priority：** P3
  - **Depends on：** 无。
  - **2026-07-08 核验：** 部分落地。`tests/unit/test_mcp_protocol_contract.py`、`tests/integration/test_mcp_stdio_protocol.py` 和 doctor 输出覆盖当前 JSON-RPC/MCP 合同；但尚未看到外部 MCP spec 版本矩阵、定期漂移检查或 owner 流程，仍打开。

## Phase 3 历史决策

以下项曾被 Phase 3 Eng Review 提升为实施任务（历史 `PLAN.md` T8-T16；根目录 `PLAN.md` 已删除），当前状态以本文件核验结果为准：

- [x] **TODOS-1** → 已提升为 **T8（P1）** CI 架构治理 ratchet，2026-07-08 核验已关闭。
- [ ] **TODOS-5** → 已提升为 **T14（P2）** 端到端信任闭环 smoke test。
- [ ] **TODOS-6** → 已提升为 **T13（P2）** MCP 协议版本矩阵测试 + 漂移跟踪。

## Phase 3 新增推迟项

- [ ] **TODOS-7：收敛 `core/hephaestus/__init__.py` 的肥胖公共 API**
  - **What：** `core/hephaestus/__init__.py` 从 `distillation_engine.py` re-export 17 个符号，导致任何内部重构都破坏下游导入；将其收敛到最小公共表面。
  - **Why：** 降低肥胖 `__init__.py` 带来的耦合。
  - **Pros：** 内部重构更自由，测试更易定位。
  - **Cons：** 需要同步更新所有消费者 import。
  - **Context：** 参见 `PLAN.md` Phase 3 Claude 发现“肥胖 API”。
  - **Effort：** M（人工 ~1 天 / CC ~20min）
  - **Priority：** P3
  - **Depends on：** TODOS-4 超大文件拆分完成后。

- [ ] **TODOS-8：修复 `core/kia/__init__.py` 的 `__all__` 误导**
  - **What：** `core/kia/__init__.py` 的 `__all__` 是字符串列表，未实际导入对象，导致 `from core.kia import *` 不生效；决定是实际导入对象还是移除 `__all__`。
  - **Why：** 消除误导性模块表面。
  - **Pros：** 显式公共 API。
  - **Cons：** 可能改变现有 import 行为。
  - **Context：** 参见 `PLAN.md` Phase 3 Claude 发现。
  - **Effort：** S（人工 ~2h / CC ~5min）
  - **Priority：** P3
  - **Depends on：** 无。

- [ ] **TODOS-9：长期拆分 `chronos.py` 为 orchestrator + worker modules**
  - **What：** 把 `core/kia/chronos.py` 从“20+ 延迟 import 的大模块”拆分为显式协议层 + 静态导入的 worker 模块。
  - **Why：** 延迟 import 掩盖真实依赖，难以测试和追踪。
  - **Pros：** 依赖清晰、可测试性提升。
  - **Cons：** 改动面较大，需配合事件总线顺序契约验证。
  - **Context：** 参见 `PLAN.md` Phase 3 Claude 发现。
  - **Effort：** L（人工 ~2-3 天 / CC ~30min）
  - **Priority：** P3
  - **Depends on：** 循环依赖打破后。

## Phase 3.5 新增推迟项

- [ ] **TODOS-10：PyPI 发布**
  - **What：** 配置 `pyproject.toml`、版本号、GitHub Actions publish workflow，使用户可通过 `pip install mnemos` 安装。
  - **Why：** TTHW 不达标的核心原因之一是缺少一键安装入口。
  - **Pros：** 显著降低新用户门槛，便于后续分发。
  - **Cons：** 需要处理打包、密钥管理、版本号策略。
  - **Context：** 参见 `PLAN.md` Phase 3.5 DX Review。
  - **Effort：** M（人工 ~1 天 / CC ~30min）
  - **Priority：** P3
  - **Depends on：** 无。
  - **2026-07-08 核验：** 部分落地。`pyproject.toml` 已有项目元数据和 `mnemos` console script；但 `.github/workflows/` 仅有 CI workflow，未发现 PyPI publish workflow 或发布密钥流程，仍打开。

- [x] **TODOS-11：pre-commit hooks + 贡献者快速开始**
  - **What：** 配置 `.pre-commit-config.yaml`，运行 mypy/black/flake8/vulture/shellcheck；更新 `CONTRIBUTING.md` 使用 pytest；README 增加 contributor quickstart。
  - **Why：** 当前贡献者文档与 CI 命令不一致，缺少本地提交前检查。
  - **Pros：** 提升贡献者体验，减少 CI 反馈周期。
  - **Cons：** 需要初始化一次并处理历史文件。
  - **Context：** 参见 `PLAN.md` Phase 3.5 DX Review。
  - **Effort：** S（人工 ~2h / CC ~10min）
  - **Priority：** P3
  - **Depends on：** 无。
  - **2026-07-08 核验：** 已关闭。`.pre-commit-config.yaml` 已接入本地门禁，`CONTRIBUTING.md` 和 `README.md` 已包含 pre-commit / local gates / pytest 快速开始。

- [ ] **TODOS-12：社区入口与真实示例项目**
  - **What：** 在 README 增加社区渠道（Discussions / Issues 模板）；提供一个可运行的示例仓库（包含临时 Obsidian vault + 合成 session）。
  - **Why：** 社区与生态维度评分仅 3/10；示例不足导致新用户难以评估价值。
  - **Pros：** 增加口碑传播与采纳信心。
  - **Cons：** 社区需要长期维护精力。
  - **Context：** 参见 `PLAN.md` Phase 3.5 DX Review。
  - **Effort：** M（人工 ~2-3 天 / CC ~30min）
  - **Priority：** P3
  - **Depends on：** 产品与核心 DX 体验稳定后。
  - **2026-07-08 核验：** 部分落地。`docs/demo/`、`scripts/e2e_wow_probe.py` 和 `tests/e2e/test_wow_path.py` 已提供 mock LLM 可运行演示链路；但 `.github/ISSUE_TEMPLATE`、Discussions 入口和独立真实示例仓库未落地，仍打开。

- [ ] **TODOS-13：CLI 参考文档自动生成**
  - **What：** 从 argparse 定义生成 `docs/CLI_REFERENCE.md`，并在 CI 中验证其是最新的。
  - **Why：** 39+ 命令缺乏集中参考；手写文档容易陈旧。
  - **Pros：** 文档与代码保持同步。
  - **Cons：** 命令分组与帮助文本需要整理。
  - **Context：** 参见 `PLAN.md` Phase 3.5 DX Review。
  - **Effort：** S（人工 ~3h / CC ~15min）
  - **Priority：** P3
  - **Depends on：** CLI 注册表重构完成后。
  - **2026-07-08 核验：** 仍打开。未发现 `docs/CLI_REFERENCE.md`，也未发现自动生成并在 CI 校验的 CLI 参考文档流程。
