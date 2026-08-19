## 变更概要

<!-- 用一句话说明本次 PR 做了什么 -->

## 改动范围

<!-- 列出主要改动的文件/模块 -->

- 
- 
- 

## 类型

<!-- 在对应类型前打勾 -->

- [ ] feat: 新功能
- [ ] fix: 缺陷修复
- [ ] refactor: 代码重构
- [ ] perf: 性能优化
- [ ] test: 测试相关
- [ ] docs: 文档相关
- [ ] chore: 构建/工具/依赖
- [ ] security: 安全修复

## 测试

<!-- 说明如何验证的，列出关键命令和结果 -->

```bash
# 本地验证命令
```

- [ ] `python3 -m pytest tests/ --cov=core --cov=integrations --cov=mnemos_cli --cov=mnemos_daemon --cov-fail-under=70 -q` 通过
- [ ] `python3 -m flake8 core/ integrations/ daemon/ scripts/ mnemos_cli.py mnemos_daemon.py --count` = 0
- [ ] `python3 -m mypy core/ integrations/ daemon/ scripts/ mnemos_cli.py mnemos_daemon.py --ignore-missing-imports` 无新增错误
- [ ] `python3 scripts/arch_dependency_graph.py --check` 通过

## 风险与回滚

<!-- 描述可能的影响及回滚方式 -->

## 关联 Issue / 审计项

<!-- 例如：修复 #123，或对应 MNEMOS_CODE_AUDIT_2026_06_24.md 中的 Sxx -->
