# Contributing to Mnemos

感谢你对 Mnemos 的兴趣！以下是贡献指南。

## 开发环境搭建

```bash
git clone https://github.com/jielumaizui/mnemos.git
cd mnemos
pip install -e ".[dev]"
```

## 运行测试

```bash
python -m unittest tests.test_smoke -v
```

## 代码规范

- Python >= 3.10
- 使用 `pathlib.Path` 处理路径，不要硬编码 `/` 或 `\\`
- 数据库表名拼接必须加白名单校验（参考 `signal_store.py` 的 `ALLOWED_SOURCES`）
- `except Exception:` 至少记一条 `logger.warning`，不要裸 `pass`
- f-string 里不能有反斜杠（Python <3.12 的兼容性问题）

## 提交 Issue

请包含：
- 操作系统和 Python 版本
- 复现步骤
- `mnemos doctor` 的输出

## 提交 PR

1. 先跑测试确保通过
2. 描述改动内容和原因
3. 如果改的是跨平台相关代码，请说明在哪些平台测试过
