from types import SimpleNamespace


def _fragment(title: str):
    from core.hephaestus.distillation_engine import KnowledgeFragment

    return KnowledgeFragment(
        form="decision",
        title=title,
        frontmatter={"领域": "测试", "置信度": 0.8},
        background="采用分块蒸馏保留长会话覆盖范围。",
        core_content="中长会话应逐块提取后合并，不应因为旧变量名丢失片段。",
        boundaries={"applies": "长会话蒸馏", "not_applies": "短会话"},
        anti_patterns=[],
        related_concepts=[],
    )


class _FakeExtractor:
    def __init__(self):
        self.calls = []
        self._titles = [
            "Redis 吞吐策略",
            "Docker 网络边界",
            "向量索引刷新",
            "后台预算调度",
        ]

    def extract(self, session_text, session_id, analysis_type):
        self.calls.append((session_text, session_id, analysis_type))
        title = self._titles[(len(self.calls) - 1) % len(self._titles)]
        return [_fragment(title)]


def test_chunked_distillation_uses_merged_fragments_and_writes_coverage(tmp_path):
    from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment

    engine = DistillationEngine(wiki_base=str(tmp_path))
    extractor = _FakeExtractor()
    engine._extractor = extractor
    engine._noise_filter = SimpleNamespace(filter=lambda messages: (messages, {"kept": len(messages)}))
    engine._value_prejudgment = SimpleNamespace(
        judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.9)
    )
    engine._self_check = SimpleNamespace(check=lambda fragments, messages: (True, []))
    engine._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
    engine._feedback_loop = SimpleNamespace(evaluate=lambda result: [])
    engine._kia_linker = False

    messages = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"第 {i} 轮：决定采用稳定的后台预算和完整覆盖追踪。" + (" 分块蒸馏证据" * 220),
        }
        for i in range(70)
    ]

    result = engine.process("sess-chunked", messages, meta={"source": "test"})

    assert result.judgment == "knowledge"
    assert result.analysis_type == "chunked"
    assert result.distill_input_mode == "chunked"
    assert "分块蒸馏" in result.session_coverage
    assert len(extractor.calls) >= 2
    assert result.fragments

    paths = engine.write_pages(result)
    assert paths
    page = tmp_path.joinpath("00-Inbox", "redis-吞吐策略.md").read_text(encoding="utf-8")
    assert "distill_input_mode: chunked" in page
    assert "source_coverage:" in page
    assert "分块蒸馏" in page
