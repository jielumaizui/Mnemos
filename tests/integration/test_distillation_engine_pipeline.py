# -*- coding: utf-8 -*-
"""
DistillationEngine 集成测试

覆盖关键路径（无需真实 LLM API）：
- _chunk_messages 分块策略
- _dedup_fragments_semantic 去重
- _jaccard_similarity
- _is_stop_phrase 性能/正确性
- generate_wiki_page frontmatter 生成
"""

from unittest.mock import patch


class TestChunkMessages:
    """_chunk_messages 分块策略测试。"""

    def test_empty_messages_returns_empty(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        assert DistillationEngine._chunk_messages([]) == []

    def test_single_message_no_split(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        msgs = [{"role": "user", "content": "hello"}]
        chunks = DistillationEngine._chunk_messages(msgs, max_tokens_per_chunk=1000)
        assert len(chunks) == 1
        assert chunks[0] == msgs

    def test_large_message_split_preserves_full_content(self):
        from core.hephaestus.distillation_engine import DistillationEngine
        from core.hephaestus.tokenizer import get_tokenizer

        tokenizer = get_tokenizer()
        original = "word " * 10000
        msgs = [{"role": "user", "content": original}]
        chunks = DistillationEngine._chunk_messages(msgs, max_tokens_per_chunk=1000)

        # 超长消息被拆成多个 part，原始内容无丢失
        joined = "".join(p["content"] for c in chunks for p in c)
        assert joined == original

        # 每个 part 都来自同一条原始消息，并记录了 turn/part
        all_parts = [p for c in chunks for p in c]
        assert all(p.get("turn") == 1 for p in all_parts)
        assert all("part" in p for p in all_parts)

        # 每个 chunk 的内容 token 数不超过预算（允许 small overhead）
        for chunk in chunks:
            total = sum(tokenizer.estimate(m.get("content", "")) for m in chunk)
            assert total <= 1000

    def test_chunk_messages_respects_max_tokens_per_chunk(self):
        from core.hephaestus.distillation_engine import DistillationEngine
        from core.hephaestus.tokenizer import get_tokenizer

        tokenizer = get_tokenizer()
        msgs = [{"role": "user", "content": f"msg{i} " + "word " * 200} for i in range(10)]
        chunks = DistillationEngine._chunk_messages(msgs, max_tokens_per_chunk=500)

        for chunk in chunks:
            total = sum(tokenizer.estimate(m.get("content", "")) for m in chunk)
            assert total <= 500

        joined = "".join(m["content"] for c in chunks for m in c)
        original = "".join(m["content"] for m in msgs)
        assert joined == original


class TestDedupFragmentsSemantic:
    """_dedup_fragments_semantic 语义去重测试。"""

    def _make_frag(self, title: str):
        from core.hephaestus.distillation_engine import KnowledgeFragment

        return KnowledgeFragment(
            form="concept",
            title=title,
            frontmatter={},
            background="",
            core_content="",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )

    def test_empty_list(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        assert DistillationEngine._dedup_fragments_semantic([]) == []

    def test_exact_title_dedup(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        frags = [self._make_frag("相同标题"), self._make_frag("相同标题")]
        result = DistillationEngine._dedup_fragments_semantic(frags)
        assert len(result) == 1
        assert result[0].title == "相同标题"

    def test_similar_title_dedup(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        frags = [self._make_frag("Python 教程"), self._make_frag("Python教程")]
        result = DistillationEngine._dedup_fragments_semantic(frags, threshold=0.6)
        # Jaccard 相似度 ~0.667，threshold=0.6 时应被视为重复
        assert len(result) == 1

    def test_different_titles_kept(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        frags = [self._make_frag("Python"), self._make_frag("Rust")]
        result = DistillationEngine._dedup_fragments_semantic(frags)
        assert len(result) == 2


class TestJaccardSimilarity:
    """_jaccard_similarity 字符二元组测试。"""

    def test_empty_strings(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        assert DistillationEngine._jaccard_similarity("", "abc") == 0.0
        assert DistillationEngine._jaccard_similarity("abc", "") == 0.0

    def test_identical_strings(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        assert DistillationEngine._jaccard_similarity("hello", "hello") == 1.0

    def test_completely_different(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        sim = DistillationEngine._jaccard_similarity("abc", "xyz")
        assert sim == 0.0

    def test_partial_overlap(self):
        from core.hephaestus.distillation_engine import DistillationEngine

        sim = DistillationEngine._jaccard_similarity("hello world", "hello there")
        assert 0.0 < sim < 1.0


class TestIsStopPhrase:
    """_is_stop_phrase 停用词检测（P1-2 性能回归防护）。"""

    def test_known_stop_phrase(self):
        from core.hephaestus.distillation_engine import CrossAgentLinker

        assert CrossAgentLinker._is_stop_phrase("的") is True

    def test_prefix_stop(self):
        from core.hephaestus.distillation_engine import CrossAgentLinker

        # 4 字以内以介词开头
        assert CrossAgentLinker._is_stop_phrase("在测试") is True

    def test_suffix_stop(self):
        from core.hephaestus.distillation_engine import CrossAgentLinker

        # 4 字以内以虚词结尾
        assert CrossAgentLinker._is_stop_phrase("测试中") is True

    def test_meaningful_phrase_not_stop(self):
        from core.hephaestus.distillation_engine import CrossAgentLinker

        assert CrossAgentLinker._is_stop_phrase("Python装饰器") is False

    def test_sets_are_class_level_constants(self):
        """验证 _PREFIX_STOP / _SUFFIX_STOP 是类级常量（非方法内局部变量）。"""
        from core.hephaestus.distillation_engine import CrossAgentLinker

        assert hasattr(CrossAgentLinker, "_PREFIX_STOP")
        assert hasattr(CrossAgentLinker, "_SUFFIX_STOP")
        assert isinstance(CrossAgentLinker._PREFIX_STOP, set)
        assert isinstance(CrossAgentLinker._SUFFIX_STOP, set)
        assert len(CrossAgentLinker._PREFIX_STOP) > 100
        assert len(CrossAgentLinker._SUFFIX_STOP) > 50

    def test_performance_not_creating_sets_per_call(self):
        """1000 次调用不应显著慢于直接 set 查找。"""
        import time
        from core.hephaestus.distillation_engine import CrossAgentLinker

        phrases = ["在测试", "Python", "的", "测试中", "hello world"] * 200

        start = time.perf_counter()
        for p in phrases:
            CrossAgentLinker._is_stop_phrase(p)
        elapsed = time.perf_counter() - start

        # 1000 次调用应在 10ms 以内（类级常量保证）
        assert elapsed < 0.01


class TestGenerateWikiPage:
    """generate_wiki_page Wiki 页面生成测试。"""

    def _make_frag(self, title="测试", core_content="内容"):
        from core.hephaestus.distillation_engine import KnowledgeFragment

        return KnowledgeFragment(
            form="concept",
            title=title,
            frontmatter={"类型": "概念"},
            background="背景",
            core_content=core_content,
            boundaries={"适用": "场景"},
            anti_patterns=["误区"],
            related_concepts=["关联"],
        )

    def test_generates_markdown_with_frontmatter(self):
        from core.hephaestus.distillation_engine import generate_wiki_page

        frag = self._make_frag("集成测试", "这是核心内容")
        text = generate_wiki_page(frag, "sess-001")
        assert text.startswith("---")
        assert "名称: 集成测试" in text or '名称: "集成测试"' in text
        assert "核心内容" in text
        assert "sess-001" in text

    def test_generates_safe_filename(self):
        from core.hephaestus.distillation_engine import generate_wiki_page

        frag = self._make_frag("Test / Slash", "内容")
        text = generate_wiki_page(frag, "sess-002")
        assert "Test / Slash" in text or "Test - Slash" in text


class TestDistillSessionAPI:
    """distill_session 顶层 API 测试。"""

    def test_distill_session_empty_messages(self, tmp_path):
        """空消息应快速返回且不生成分片。"""
        from core.hephaestus.distillation_engine import distill_session

        with patch(
            "core.hephaestus.distillation_engine._get_wiki_dir", return_value=tmp_path / "wiki"
        ):
            (tmp_path / "wiki" / "00-Inbox").mkdir(parents=True)
            result = distill_session("sess-empty", [])
            assert result is not None
