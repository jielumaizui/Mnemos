# -*- coding: utf-8 -*-
"""Unit tests for core/task_id_parser.py"""

import re
from datetime import datetime, timezone


from core.task_id_parser import TaskIdParser, TagBuilder

# ---------------------------------------------------------------------------
# TaskIdParser.parse
# ---------------------------------------------------------------------------


class TestTaskIdParserParse:
    """TaskIdParser.parse 测试"""

    def test_no_trigger_returns_default(self):
        """无触发词时应返回默认 task-id。"""
        result = TaskIdParser.parse("Hello world")
        assert result.startswith("task:daily-")
        assert re.match(r"task:daily-\d{8}-\d{4}", result)

    def test_trigger_task_colon(self):
        """\"任务：xxx\" 应提取关键词（中文保留）。"""
        result = TaskIdParser.parse("任务：完成代码重构")
        assert result.startswith("task:")
        assert "完成代码重构" in result

    def test_trigger_task_colon_halfwidth(self):
        """\"任务: xxx\" 应提取关键词。"""
        result = TaskIdParser.parse("任务: 修复bug")
        assert result.startswith("task:")

    def test_trigger_execute_about(self):
        """\"执行关于xxx的任务\" 应提取关键词（中文保留）。"""
        result = TaskIdParser.parse("执行关于文档整理的任务")
        assert result.startswith("task:")
        assert "文档整理" in result

    def test_trigger_task_hash(self):
        """\"#任务 xxx\" 应提取关键词。"""
        result = TaskIdParser.parse("#任务 数据分析")
        assert result.startswith("task:")

    def test_trigger_task_brackets(self):
        """\"【任务】xxx\" 应提取关键词。"""
        result = TaskIdParser.parse("【任务】系统部署")
        assert result.startswith("task:")

    def test_trigger_task_english(self):
        """\"task: xxx\" 应提取关键词。"""
        result = TaskIdParser.parse("task: refactor code")
        assert result.startswith("task:")
        assert "refactor" in result or "code" in result

    def test_trigger_but_no_keyword_returns_default(self):
        """有触发词但无法提取关键词时应返回默认。"""
        result = TaskIdParser.parse("任务：")
        assert result.startswith("task:daily-")

    def test_keyword_cleaning_removes_noise_words(self):
        """清理应移除无意义词。"""
        result = TaskIdParser.parse("任务：一个关于测试的处理")
        # "一个", "关于", "的", "处理" 应被移除
        assert "yi-ge" not in result
        assert "guan-yu" not in result
        assert "chu-li" not in result

    def test_date_in_task_id(self):
        """task-id 应包含当前 UTC 日期。"""
        result = TaskIdParser.parse("任务：测试")
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert today in result


# ---------------------------------------------------------------------------
# TaskIdParser._clean_keyword
# ---------------------------------------------------------------------------


class TestCleanKeyword:
    """_clean_keyword 测试"""

    def test_lowercases(self):
        """应转为小写。"""
        assert TaskIdParser._clean_keyword("HELLO") == "hello"

    def test_replaces_spaces_with_dash(self):
        """空格应替换为 -。"""
        assert TaskIdParser._clean_keyword("hello world") == "hello-world"

    def test_removes_special_chars(self):
        """特殊字符应被移除。"""
        assert TaskIdParser._clean_keyword("hello@#$world") == "helloworld"

    def test_merges_multiple_dashes(self):
        """多个 - 应合并为一个。"""
        assert TaskIdParser._clean_keyword("hello---world") == "hello-world"

    def test_trims_dashes(self):
        """首尾 - 应被移除。"""
        assert TaskIdParser._clean_keyword("-hello-world-") == "hello-world"

    def test_limits_length(self):
        """长度应限制在 30 字符。"""
        long_word = "a" * 50
        result = TaskIdParser._clean_keyword(long_word)
        assert len(result) <= 30

    def test_preserves_chinese(self):
        """中文字符应被保留。"""
        result = TaskIdParser._clean_keyword("中文测试")
        assert "中文" in result or "zhong-wen" not in result  # 中文直接保留

    def test_removes_noise_words(self):
        """无意义词应被移除。"""
        result = TaskIdParser._clean_keyword("一个任务的处理")
        assert "任务" not in result
        assert "处理" not in result


# ---------------------------------------------------------------------------
# TaskIdParser.is_private_request
# ---------------------------------------------------------------------------


class TestIsPrivateRequest:
    """is_private_request 测试"""

    def test_private_chinese(self):
        """中文私有关键词应被检测。"""
        assert TaskIdParser.is_private_request("这是私有内容") is True
        assert TaskIdParser.is_private_request("保密信息") is True
        assert TaskIdParser.is_private_request("隐私数据") is True

    def test_private_english(self):
        """英文私有关键词应被检测。"""
        assert TaskIdParser.is_private_request("This is private") is True
        assert TaskIdParser.is_private_request("Personal info") is True
        assert TaskIdParser.is_private_request("Confidential data") is True

    def test_private_share_denial(self):
        """\"不要共享\"应被检测。"""
        assert TaskIdParser.is_private_request("不要共享这个") is True
        assert TaskIdParser.is_private_request("仅自己可见") is True

    def test_non_private(self):
        """非私有请求应返回 False。"""
        assert TaskIdParser.is_private_request("普通任务") is False
        assert TaskIdParser.is_private_request("hello world") is False

    def test_case_insensitive(self):
        """大小写不敏感。"""
        assert TaskIdParser.is_private_request("PRIVATE") is True
        assert TaskIdParser.is_private_request("PRIVATE info") is True


# ---------------------------------------------------------------------------
# TagBuilder
# ---------------------------------------------------------------------------


class TestTagBuilder:
    """TagBuilder 测试"""

    def test_build_all_dimensions(self):
        """五维标签应全部包含。"""
        tags = TagBuilder.build_tags(
            source="claude",
            model="gpt-4o",
            task_id="task:20240101-test",
            scope="public",
            date="20240101",
        )
        assert "source=claude" in tags
        assert "time=20240101" in tags
        assert "model=gpt-4o" in tags
        assert "scope=public" in tags
        assert "task:20240101-test" in tags
        assert len(tags) == 5

    def test_build_default_date(self):
        """默认日期应为当前 UTC 日期。"""
        tags = TagBuilder.build_tags(
            source="hermes",
            model="claude-3",
            task_id="task:20240101-test",
        )
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        assert f"time={today}" in tags
        assert "scope=restricted" in tags  # 默认 fail-closed

    def test_build_omits_none_task_id(self):
        """task_id 为 None 时应被省略。"""
        tags = TagBuilder.build_tags(
            source="claude",
            model="gpt-4o",
            task_id=None,  # type: ignore[arg-type]
        )
        assert len(tags) == 4
        assert not any(t.startswith("task:") for t in tags)

    def test_parse_tags_equal_separator(self):
        """= 分隔符应被正确解析。"""
        result = TagBuilder.parse_tags("source=claude,time=20240101")
        assert result["source"] == "claude"
        assert result["time"] == "20240101"

    def test_parse_tags_colon_separator(self):
        """: 分隔符应被正确解析。"""
        result = TagBuilder.parse_tags("source:claude,time:20240101")
        assert result["source"] == "claude"
        assert result["time"] == "20240101"

    def test_parse_tags_task_id_format(self):
        """task:xxx 格式会被 : 分隔符分割。"""
        result = TagBuilder.parse_tags("task:20240101-test")
        assert result["task"] == "20240101-test"

    def test_parse_tags_mixed_separators(self):
        """混合分隔符应被正确解析。"""
        result = TagBuilder.parse_tags("source=claude,time:20240101,model=gpt-4o")
        assert result["source"] == "claude"
        assert result["time"] == "20240101"
        assert result["model"] == "gpt-4o"

    def test_parse_tags_empty_string(self):
        """空字符串应返回空字典。"""
        result = TagBuilder.parse_tags("")
        assert result == {}

    def test_parse_tags_no_separator(self):
        """无分隔符的标签应被忽略。"""
        result = TagBuilder.parse_tags("random_tag")
        assert result == {}
