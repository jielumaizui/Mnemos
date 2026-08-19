# -*- coding: utf-8 -*-
"""Unit tests for core/frontmatter.py"""

from core.frontmatter import (
    canonical_key,
    normalize_frontmatter,
    fm_get,
    to_chinese_frontmatter,
    parse_frontmatter,
    write_frontmatter,
)

# ---------------------------------------------------------------------------
# Key translation
# ---------------------------------------------------------------------------


class TestCanonicalKey:
    """canonical_key 测试"""

    def test_canonical_key_returns_english(self):
        """中文键应映射到英文 canonical。"""
        assert canonical_key("类型") == "type"
        assert canonical_key("名称") == "name"

    def test_canonical_key_english_passthrough(self):
        """英文键应保持不变。"""
        assert canonical_key("type") == "type"
        assert canonical_key("name") == "name"

    def test_canonical_key_alias(self):
        """别名应映射到 canonical。"""
        assert canonical_key("类别") == "type"
        assert canonical_key("标题") == "name"

    def test_canonical_key_unknown(self):
        """未知键应原样返回。"""
        assert canonical_key("unknown_field") == "unknown_field"

    def test_strategy_items_field_contract(self):
        """策略文档结构化要点应有稳定中英 frontmatter 映射。"""
        assert canonical_key("策略要点") == "strategy_items"
        assert canonical_key("strategy_items") == "strategy_items"

        normalized = normalize_frontmatter({"策略要点": {"key_decisions": ["决策 A"]}})
        assert normalized == {"strategy_items": {"key_decisions": ["决策 A"]}}

        chinese = to_chinese_frontmatter(
            {"strategy_items": {"lessons_learned": ["经验 C"]}}
        )
        assert chinese == {"策略要点": {"lessons_learned": ["经验 C"]}}


# ---------------------------------------------------------------------------
# normalize_frontmatter
# ---------------------------------------------------------------------------


class TestNormalizeFrontmatter:
    """normalize_frontmatter 测试"""

    def test_normalize_mixed_keys(self):
        """混合中英文键应统一为英文。"""
        fm = {"类型": "note", "name": "Test"}
        result = normalize_frontmatter(fm)
        assert result == {"type": "note", "name": "Test"}

    def test_normalize_none_returns_empty(self):
        """None 应返回空字典。"""
        assert normalize_frontmatter(None) == {}

    def test_normalize_non_dict_returns_empty(self):
        """非字典应返回空字典。"""
        assert normalize_frontmatter("not a dict") == {}  # type: ignore[arg-type]

    def test_normalize_preserves_values(self):
        """值应保持不变。"""
        fm = {"摘要": "Summary text", "关键词": ["a", "b"]}
        result = normalize_frontmatter(fm)
        assert result["summary"] == "Summary text"
        assert result["keywords"] == ["a", "b"]


# ---------------------------------------------------------------------------
# fm_get
# ---------------------------------------------------------------------------


class TestFmGet:
    """fm_get 测试"""

    def test_fm_get_canonical_key(self):
        """通过 canonical key 读取。"""
        fm = {"type": "note", "name": "Test"}
        assert fm_get(fm, "type") == "note"

    def test_fm_get_display_key(self):
        """通过 display key 读取。"""
        fm = {"类型": "note"}
        assert fm_get(fm, "type") == "note"

    def test_fm_get_alias_key(self):
        """通过 alias key 读取。"""
        fm = {"类别": "concept"}
        assert fm_get(fm, "type") == "concept"

    def test_fm_get_default(self):
        """键不存在时返回默认值。"""
        fm = {"type": "note"}
        assert fm_get(fm, "missing", "default") == "default"
        assert fm_get(fm, "missing") is None

    def test_fm_get_none_frontmatter(self):
        """frontmatter 为 None 时返回默认值。"""
        assert fm_get(None, "type", "default") == "default"  # type: ignore[arg-type]

    def test_fm_get_non_dict(self):
        """frontmatter 非字典时返回默认值。"""
        assert fm_get("bad", "type", "default") == "default"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# to_chinese_frontmatter
# ---------------------------------------------------------------------------


class TestToChineseFrontmatter:
    """to_chinese_frontmatter 测试"""

    def test_converts_canonical_to_chinese(self):
        """canonical key 应转为中文 display key。"""
        fm = {"type": "note", "name": "Test"}
        result = to_chinese_frontmatter(fm)
        assert "类型" in result
        assert "名称" in result
        assert result["类型"] == "note"

    def test_skips_none_and_empty(self):
        """None 和空字符串值应被跳过。"""
        fm = {"type": "note", "name": None, "summary": ""}
        result = to_chinese_frontmatter(fm)
        assert "类型" in result
        assert "名称" not in result
        assert "摘要" not in result

    def test_merges_defaults(self):
        """defaults 应被 canonical 值覆盖。"""
        fm = {"type": "note"}
        defaults = {"type": "default_type", "name": "Default"}
        result = to_chinese_frontmatter(fm, defaults)
        assert result["类型"] == "note"  # fm 覆盖 defaults
        assert result["名称"] == "Default"  # defaults 补充

    def test_none_input_returns_empty(self):
        """None 输入应返回空字典。"""
        assert to_chinese_frontmatter(None) == {}


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """parse_frontmatter 测试"""

    def test_parse_valid_frontmatter(self):
        """有效的 YAML frontmatter 应被解析。"""
        content = "---\ntype: note\nname: Test\n---\n\nBody text."
        fm, body = parse_frontmatter(content)
        assert fm == {"type": "note", "name": "Test"}
        assert body == "Body text."

    def test_parse_no_frontmatter(self):
        """无前缀 --- 应返回 None frontmatter。"""
        content = "Just body text."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == "Just body text."

    def test_parse_unclosed_frontmatter(self):
        """缺少闭合 --- 应返回 None frontmatter。"""
        content = "---\ntype: note\n\nBody text."
        fm, body = parse_frontmatter(content)
        assert fm is None
        assert body == content

    def test_parse_empty_frontmatter(self):
        """空的 frontmatter 应返回空字典。"""
        content = "---\n---\n\nBody text."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "Body text."

    def test_parse_invalid_yaml(self):
        """无效 YAML 应返回空字典而非抛异常。"""
        content = "---\n{ invalid yaml\n---\n\nBody."
        fm, body = parse_frontmatter(content)
        assert fm == {}
        assert body == "Body."

    def test_parse_non_dict_yaml(self):
        """YAML 解析为非字典时应返回空字典。"""
        content = "---\n- item1\n- item2\n---\n\nBody."
        fm, body = parse_frontmatter(content)
        assert fm == {}

    def test_strips_leading_newlines_in_body(self):
        """body 前导换行符应在解析时被 strip。"""
        content = "---\ntype: note\n---\n\n\n\nBody with leading newlines."
        fm, body = parse_frontmatter(content)
        assert body == "Body with leading newlines."


# ---------------------------------------------------------------------------
# write_frontmatter
# ---------------------------------------------------------------------------


class TestWriteFrontmatter:
    """write_frontmatter 测试"""

    def test_write_basic_frontmatter(self):
        """基本 frontmatter + body 应正确序列化。"""
        fm = {"type": "note", "name": "Test"}
        body = "Body text."
        result = write_frontmatter(fm, body)
        assert result.startswith("---\n")
        assert "type: note" in result
        assert "name: Test" in result
        assert result.endswith("\n\nBody text.")

    def test_write_empty_frontmatter_returns_body(self):
        """空 frontmatter 应只返回 body。"""
        result = write_frontmatter({}, "Body only.")
        assert result == "Body only."

    def test_write_none_frontmatter_returns_body(self):
        """None frontmatter 应只返回 body。"""
        result = write_frontmatter(None, "Body only.")  # type: ignore[arg-type]
        assert result == "Body only."

    def test_write_preserves_unicode(self):
        """中文字符应被正确保留。"""
        fm = {"类型": "笔记", "名称": "测试"}
        body = "中文正文。"
        result = write_frontmatter(fm, body)
        assert "笔记" in result
        assert "测试" in result

    def test_write_roundtrip(self):
        """parse → write → parse 应保持一致。"""
        original_fm = {"type": "note", "name": "Test"}
        original_body = "Body text.\n\nMore text."
        written = write_frontmatter(original_fm, original_body)
        parsed_fm, parsed_body = parse_frontmatter(written)
        assert parsed_fm == original_fm
        assert parsed_body == original_body

    def test_write_allows_chinese_keys(self):
        """中文键应被正确写入。"""
        fm = {"类型": "note", "名称": "Test"}
        body = "Body."
        result = write_frontmatter(fm, body)
        assert "类型: note" in result
        assert "名称: Test" in result
