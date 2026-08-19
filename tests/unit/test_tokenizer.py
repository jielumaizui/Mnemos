# -*- coding: utf-8 -*-
"""
Tokenizer 单元测试

覆盖 Tokenizer 的 token 估算、截断、分片能力。
"""

import pytest


@pytest.fixture
def tokenizer():
    from core.hephaestus.tokenizer import Tokenizer

    return Tokenizer()


def test_estimate_empty_text(tokenizer):
    assert tokenizer.estimate("") == 0


def test_truncate_to_tokens_short_text_unchanged(tokenizer):
    text = "hello world"
    assert tokenizer.truncate_to_tokens(text, 100) == text


def test_split_to_tokens_short_text_returns_single(tokenizer):
    text = "hello world"
    assert tokenizer.split_to_tokens(text, 100) == [text]


def test_split_to_tokens_long_english_within_limit(tokenizer):
    text = "word " * 2000  # ~ 长文本
    parts = tokenizer.split_to_tokens(text, 100)
    assert len(parts) > 1
    assert all(tokenizer.estimate(p) <= 100 for p in parts)
    assert "".join(parts) == text


def test_split_to_tokens_long_chinese_within_limit(tokenizer):
    text = "这是一个测试句子。" * 500
    parts = tokenizer.split_to_tokens(text, 100)
    assert len(parts) > 1
    assert all(tokenizer.estimate(p) <= 100 for p in parts)
    assert "".join(parts) == text


def test_split_to_tokens_preserves_paragraph_boundaries(tokenizer):
    paragraphs = [f"段落{i}：" + "内容 " * 50 for i in range(10)]
    text = "\n\n".join(paragraphs)
    parts = tokenizer.split_to_tokens(text, 200)
    assert "".join(parts) == text
    # 验证至少在一个分段边界处保留了段落分隔
    assert any("\n\n" in p for p in parts)


def test_split_to_tokens_no_content_loss(tokenizer):
    text = "start " + "word " * 3000 + "end"
    parts = tokenizer.split_to_tokens(text, 150)
    joined = "".join(parts)
    assert joined == text
    assert "start " in parts[0]
    assert "end" in parts[-1]


def test_split_to_tokens_with_mocked_heuristic_only(tokenizer, monkeypatch):
    """模拟 tiktoken 不可用时，启发式分片仍能工作且不丢内容。"""
    monkeypatch.setattr(tokenizer, "_get_encoder", lambda: None)
    text = "hello " * 500
    parts = tokenizer.split_to_tokens(text, 100)
    assert len(parts) > 1
    assert all(tokenizer.estimate(p) <= 100 for p in parts)
    assert "".join(parts) == text


def test_module_level_split_to_tokens():
    from core.hephaestus.tokenizer import get_tokenizer, split_to_tokens

    tokenizer = get_tokenizer()
    text = "word " * 2000
    parts = split_to_tokens(text, 100)
    assert len(parts) > 1
    assert all(tokenizer.estimate(p) <= 100 for p in parts)
    assert "".join(parts) == text
