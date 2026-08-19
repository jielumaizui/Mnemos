from core.hephaestus.content_expression import (
    ContentExpressionFormatter,
    ExpressionForm,
    maybe_format_expression,
)


def test_detects_comparison_table():
    formatter = ContentExpressionFormatter()
    text = "方案A 优点 快速 缺点 粗糙\n方案B 优点 稳定 缺点 成本高"

    suggestion = formatter.detect_form(text)
    formatted = formatter.format_content(text)

    assert suggestion.form == ExpressionForm.COMPARISON_TABLE
    assert "| 选项 | 要点 |" in formatted


def test_detects_mermaid_flow():
    formatter = ContentExpressionFormatter()
    text = "步骤：先收集输入，再判断条件，最后写入 Wiki"

    formatted = formatter.format_content(text)

    assert "```mermaid" in formatted
    assert "flowchart TD" in formatted


def test_detects_config_block():
    formatter = ContentExpressionFormatter()
    text = "配置如下\nprovider=siliconflow\nmodel=BAAI/bge-m3"

    formatted = formatter.format_content(text)

    assert "```yaml" in formatted
    assert "provider: siliconflow" in formatted


def test_detects_checklist():
    formatter = ContentExpressionFormatter()
    text = "检查清单\n- 安装 Obsidian\n- 运行 doctor"

    formatted = formatter.format_content(text)

    assert "- [ ] 安装 Obsidian" in formatted
    assert "- [ ] 运行 doctor" in formatted


def test_low_confidence_and_code_blocks_are_not_changed():
    formatter = ContentExpressionFormatter()
    plain = "这是一段普通说明，没有明显结构。"
    code = "配置示例\n```python\nprint('x')\n```"

    assert formatter.format_content(plain) == plain
    assert formatter.format_content(code) == code


def test_maybe_format_expression_respects_config_switch():
    class _Cfg:
        def __init__(self, enabled):
            self.enabled = enabled

        def get(self, key, default=None):
            if key == "distill.auto_expression_formatting":
                return self.enabled
            return default

    text = "检查清单\n- 安装 Obsidian\n- 运行 doctor"

    assert maybe_format_expression(text, _Cfg(False)) == text
    assert "- [ ] 安装 Obsidian" in maybe_format_expression(text, _Cfg(True))
