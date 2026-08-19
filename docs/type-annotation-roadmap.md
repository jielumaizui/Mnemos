# 类型注解推进计划

对应审计项 **S6**。本计划目标不是一次性 strict，而是让核心公共 API 的类型可信，并把 mypy 错误数控制在只降不升的预算内。

## 当前基线

- 命令：`python3 -m mypy core/ integrations/ daemon/ scripts/ mnemos_cli.py mnemos_daemon.py --ignore-missing-imports`
- 当前错误数：**0**（352 个源文件）
- 预算文件：`.mypy_budget.json`（由 `scripts/mypy_budget.py` 维护）

## 推进阶段

| 阶段 | 范围 | 目标 | 启用配置 |
|------|------|------|----------|
| P0 | `core/config.py`、`core/utils.py` | 配置与工具函数全类型覆盖 | `disallow_untyped_defs = true` |
| P1 | `core/sync_framework/` | Source / SyncEngine / CaptureQueue 公共接口 | `disallow_untyped_defs = true` |
| P2 | `core/kia/` 公共 API | preflight/guard/recap/graph 入口 | `disallow_untyped_defs = true` |
| P3 | `core/hephaestus/` 公共 API | distillation / prompt / quality gate | `disallow_untyped_defs = true` |
| P4 | `integrations/sources/*` | Agent source 基类与子类 | `disallow_untyped_defs = true` |
| P5 | `mnemos_daemon.py`、`integrations/agora.py` | daemon 入口与 MCP 工具 | `warn_return_any`、`warn_unused_ignores` |
| P6 | 全项目 | 逐步开启 `check_untyped_defs` | `check_untyped_defs = true` |

## 规则

1. **预算只降不升**：`scripts/mypy_budget.py` 在 CI 中运行，当前预算为 0；新增代码若引入 mypy 错误必须同步修复或下调预算。
2. **分层启用**：每阶段只对该包启用 `disallow_untyped_defs`，避免一次性爆炸。
3. **删除过期 ignore**：每次修复一批错误后，运行 `mypy --warn_unused_ignores` 清理多余的 `# type: ignore`。
4. **高频数据结构补类型**：优先为 `Dict[str, Any]` 外泄的公共数据结构补 `TypedDict` 或 dataclass。
5. **新模块默认严格**：新增模块在创建时即要求 `disallow_untyped_defs = true`。

## 运行方式

```bash
# 本地检查
python3 scripts/mypy_budget.py

# 更新预算（仅在主动推进阶段后使用）
python3 scripts/mypy_budget.py --update

# 完整 mypy
python3 -m mypy core/ integrations/ daemon/ scripts/ mnemos_cli.py mnemos_daemon.py --ignore-missing-imports
```

## CI 集成

`.github/workflows/ci.yml` 中 `Type check with mypy` 步骤已改为运行 `python3 scripts/mypy_budget.py`，确保错误数不突破预算。
