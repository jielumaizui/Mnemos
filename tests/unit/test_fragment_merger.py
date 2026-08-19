# -*- coding: utf-8 -*-
"""
FragmentMerger 单元测试

覆盖：
- 基于标题 + keywords 的 Jaccard 聚类
- LLM 合成路径（mock）
- LLM 失败时的规则回退合并
- 与 DistillationEngine.process() 的集成
"""

import logging
from types import SimpleNamespace

import pytest

from tests.cognition_episode_fixtures import (
    exact_source_message,
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


@pytest.fixture(autouse=True)  # noqa
def _reset_distillation_pause_state():
    """每个测试前重置蒸馏暂停状态，避免测试隔离问题。"""
    from core.hephaestus.distillation_pause import resume_distillation

    resume_distillation()


def _make_fragment(
    title: str,
    core_content: str,
    keywords=None,
    *,
    frontmatter=None,
    background="背景说明",
    boundaries=None,
    anti_patterns=None,
    related_concepts=None,
    relations=None,
    self_check_passed=True,
    self_check_issues=None,
    self_check_severity="ok",
    cross_agent_links=None,
    ai_expansion="",
    claim_ids=None,
):
    from core.hephaestus.distillation_engine import KnowledgeFragment

    if len(core_content) < 120:
        core_content = (
            f"## {title}\n\n{core_content}\n\n"
            "这是用于验证片段合并和路由准入的完整上下文说明。"
            "它明确保留问题、证据、边界和后续动作，确保测试片段满足正式硬校验。"
            "合并后的页面必须仍然可追溯到这些已准入的局部片段。"
        )
    return KnowledgeFragment(
        form="问题-解决",
        title=title,
        frontmatter=frontmatter
        if frontmatter is not None
        else {
            "领域": "backend",
            "置信度": 0.85,
            "摘要": title + "的摘要",
        },
        background=background,
        core_content=core_content,
        boundaries=boundaries
        if boundaries is not None
        else {"applies": "适用场景", "not_applies": "不适用场景"},
        anti_patterns=anti_patterns if anti_patterns is not None else ["反模式1"],
        related_concepts=related_concepts if related_concepts is not None else ["概念A"],
        relations=relations if relations is not None else [],
        self_check_passed=self_check_passed,
        self_check_issues=self_check_issues if self_check_issues is not None else [],
        self_check_severity=self_check_severity,
        cross_agent_links=cross_agent_links if cross_agent_links is not None else [],
        keywords=keywords if keywords is not None else ["redis", "pool"],
        ai_expansion=ai_expansion,
        claim_ids=claim_ids if claim_ids is not None else ["redis-pool-merge"],
    )


def _valid_merged_json(title: str, core_content: str, *, preserved_fragment=None) -> str:
    import json

    # 硬校验要求 core_content >= 100 字符，这里保证长度
    full_content = (
        f"# {title}\n\n{core_content}\n\n"
        "## 补充说明\n\n"
        "这是为了保证内容长度超过一百字符而添加的说明文字，"
        "因此需要继续补充一些内容，直到总长度满足硬校验的最低要求为止。"
        "如果还不够长，就再补一句完全没有信息量的废话。"
    )
    payload = {
        "form": "problem-solution",
        "title": title,
        "frontmatter": {
            "领域": "backend",
            "置信度": 0.85,
            "摘要": title + "的摘要",
        },
        "background": "综合背景",
        "core_content": full_content,
        "boundaries": {"applies": "适用场景", "not_applies": "不适用场景"},
        "anti_patterns": ["反模式1"],
        "related_concepts": ["概念A"],
        "relations": [],
        "self_check_passed": True,
        "self_check_issues": [],
        "self_check_severity": "ok",
        "cross_agent_links": [],
        "keywords": ["redis", "pool"],
        "ai_expansion": "",
        "claim_ids": ["redis-pool-merge"],
    }
    if preserved_fragment is not None:
        payload.update(
            {
                "background": preserved_fragment.background,
                "core_content": preserved_fragment.core_content,
                "frontmatter": preserved_fragment.frontmatter,
                "boundaries": preserved_fragment.boundaries,
                "anti_patterns": preserved_fragment.anti_patterns,
                "related_concepts": preserved_fragment.related_concepts,
                "relations": preserved_fragment.relations,
                "self_check_passed": preserved_fragment.self_check_passed,
                "self_check_issues": preserved_fragment.self_check_issues,
                "self_check_severity": preserved_fragment.self_check_severity,
                "cross_agent_links": preserved_fragment.cross_agent_links,
                "keywords": preserved_fragment.keywords,
                "ai_expansion": preserved_fragment.ai_expansion,
                "claim_ids": preserved_fragment.claim_ids,
            }
        )
    return json.dumps(payload, ensure_ascii=False)


def _sensitive_provider_error_marker() -> str:
    """Build a provider-response diagnostic without committed credentials."""
    return "|".join(
        (
            "api" + "_key" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "pass" + "word" + "=" + "DUMMY_CREDENTIAL_VALUE",
            "prompt" + "=" + "PRIVATE_PROMPT_BODY",
            "response" + "=" + "PRIVATE_RESPONSE_BODY",
        )
    )


@pytest.fixture
def merger(tmp_path):
    from core.hephaestus.fragment_merger import FragmentMerger

    return FragmentMerger()


class TestFragmentMergerCluster:
    def test_cluster_fragments_groups_similar_titles(self, merger):
        frags = [
            _make_fragment("Redis 连接池耗尽排查", "内容A"),
            _make_fragment("Redis 连接池问题解决方案", "内容B"),
            _make_fragment("Docker 网络边界", "内容C", keywords=["docker"]),
        ]
        clusters = merger.cluster_fragments(frags)
        # Redis 两个片段应聚为一类，Docker 单独一类
        assert len(clusters) == 2
        titles_per_cluster = [sorted(f.title for f in c) for c in clusters]
        assert ["Redis 连接池耗尽排查", "Redis 连接池问题解决方案"] in titles_per_cluster
        assert ["Docker 网络边界"] in titles_per_cluster

    def test_cluster_fragments_keeps_different_topics_separate(self, merger):
        frags = [
            _make_fragment("Python 装饰器", "内容A", keywords=["python", "decorator"]),
            _make_fragment("Rust 所有权", "内容B", keywords=["rust", "ownership"]),
        ]
        clusters = merger.cluster_fragments(frags, threshold=0.6)
        assert len(clusters) == 2

    def test_empty_fragment_list_returns_empty(self, merger):
        assert merger.cluster_fragments([]) == []


class TestFragmentMergerRuleMerge:
    def test_rule_merge_combines_content_and_keywords(self, merger):
        frags = [
            _make_fragment("Redis 连接池", "## 原因\n\n上限过低。", keywords=["redis"]),
            _make_fragment(
                "Redis 连接池方案", "## 解决\n\n增加 max_connections。", keywords=["pool"]
            ),
        ]
        merged = merger._rule_merge_cluster(frags)
        assert "Redis" in merged.title
        assert "上限过低" in merged.core_content
        assert "增加 max_connections" in merged.core_content
        assert set(merged.keywords) == {"redis", "pool"}

    def test_rule_merge_single_cluster_unchanged(self, merger):
        frags = [_make_fragment("单一", "内容")]
        merged = merger._rule_merge_cluster(frags)
        assert merged.title == "单一"

    def test_rule_merge_preserves_structured_metadata_and_conservative_confidence(self, merger):
        relation_a = {
            "target": "[[Redis 连接池]]",
            "type": "causes",
            "context": "连接上限过低会造成耗尽。",
        }
        relation_b = {
            "target": "[[max_connections]]",
            "type": "mitigates",
            "context": "提高上限可缓解耗尽。",
        }
        frags = [
            _make_fragment(
                "Redis 连接池问题",
                "第一段证据。",
                frontmatter={"领域": "backend", "置信度": 0.9, "摘要": "高置信度证据"},
                relations=[relation_a],
                self_check_passed=False,
                self_check_issues=["需要补充容量边界"],
                self_check_severity="warning",
                cross_agent_links=["[[容量评估代理]]"],
                ai_expansion="扩充线索 A",
            ),
            _make_fragment(
                "Redis 连接池修复",
                "第二段证据。",
                frontmatter={"领域": "backend", "置信度": 0.4, "摘要": "保守证据"},
                relations=[relation_b],
                self_check_passed=True,
                cross_agent_links=["[[配置审计代理]]"],
                ai_expansion="扩充线索 B",
            ),
        ]

        merged = merger._rule_merge_cluster(frags)

        assert merged.relations == [relation_a, relation_b]
        assert merged.cross_agent_links == ["[[容量评估代理]]", "[[配置审计代理]]"]
        assert merged.ai_expansion == "扩充线索 A\n\n扩充线索 B"
        assert merged.self_check_passed is False
        assert merged.self_check_issues == ["需要补充容量边界"]
        assert merged.self_check_severity == "warning"
        assert merged.frontmatter["置信度"] == 0.4
        assert merged.frontmatter["置信度审计值"] == [0.9, 0.4]

    def test_merge_unions_exact_raw_event_refs_and_chunk_source_spans(self, merger):
        first_ref = {
            "revision_id": "raw-revision-1",
            "logical_event_id": "event-1",
            "turn_number": 1,
            "content_hash": "sha256:first",
            "role": "user",
            "span_start": 0,
            "span_end": 18,
        }
        second_ref = {
            "revision_id": "raw-revision-2",
            "logical_event_id": "event-2",
            "turn_number": 2,
            "content_hash": "sha256:second",
            "role": "assistant",
            "span_start": 18,
            "span_end": 46,
        }
        first_span = {**first_ref, "chunk_index": 0}
        second_span = {**second_ref, "chunk_index": 1}
        fragments = [
            _make_fragment(
                "Redis 连接池来源一",
                "第一分块的精确来源。",
                frontmatter={
                    "领域": "backend",
                    "置信度": 0.85,
                    "摘要": "第一分块来源",
                    "raw_event_refs": [first_ref],
                    "chunk_source_spans": [first_span],
                },
            ),
            _make_fragment(
                "Redis 连接池来源二",
                "第二分块的精确来源。",
                frontmatter={
                    "领域": "backend",
                    "置信度": 0.8,
                    "摘要": "第二分块来源",
                    "raw_event_refs": [first_ref, second_ref],
                    "chunk_source_spans": [first_span, second_span],
                },
            ),
        ]
        merger._enable_llm = False

        merged = merger.merge(fragments)

        assert len(merged) == 1
        assert merged[0].frontmatter["raw_event_refs"] == [first_ref, second_ref]
        assert merged[0].frontmatter["chunk_source_spans"] == [first_span, second_span]

    def test_rule_merge_keeps_complete_source_blocks_and_repeated_lines(self, merger):
        first_block = (
            "```python\n"
            "value = 1\n"
            "\n"
            "value = 1\n"
            "```\n\n"
            + "第一块的可见内容必须逐字保留。" * 12
        )
        second_block = "\n\n第二块开始。\n\n" + "第二块的可见内容必须按顺序保留。" * 12
        merged = merger._rule_merge_cluster(
            [
                _make_fragment("代码片段一", first_block),
                _make_fragment("代码片段二", second_block),
            ]
        )

        assert merged.core_content == f"{first_block}\n\n{second_block}"
        assert merged.core_content.count("value = 1") == 2
        assert "value = 1\n\nvalue = 1" in merged.core_content


class TestFragmentMergerLLM:
    def test_merge_cluster_calls_llm_and_returns_single_fragment(self, merger):
        frags = [
            _make_fragment("Redis 连接池耗尽", "原因分析"),
            _make_fragment("Redis 连接池解决", "解决方案"),
        ]
        expected_metadata = merger._rule_merge_cluster(frags)
        merger._call_llm = lambda prompt: _valid_merged_json(
            "Redis 连接池完整方案",
            "原因分析 + 解决方案",
            preserved_fragment=expected_metadata,
        )
        result = merger._llm_merge_cluster(frags)
        assert result is not None
        assert result.title == "Redis 连接池完整方案"
        assert "原因分析" in result.core_content
        assert "解决方案" in result.core_content

    def test_merge_falls_back_to_rule_merge_on_llm_failure(self, merger):
        frags = [
            _make_fragment("Redis 连接池耗尽", "原因分析"),
            _make_fragment("Redis 连接池解决", "解决方案"),
        ]
        merger._call_llm = lambda prompt: None
        result = merger.merge(frags)
        assert len(result) == 1
        merged = result[0]
        assert "Redis" in merged.title
        assert "原因分析" in merged.core_content
        assert "解决方案" in merged.core_content

    def test_merge_passes_through_single_fragment(self, merger):
        frags = [_make_fragment("单一话题", "内容")]
        result = merger.merge(frags)
        assert len(result) == 1
        assert result[0].title == "单一话题"

    def test_prompt_and_dict_conversion_include_lossless_structured_metadata(self, merger):
        import json

        relation = {
            "target": "[[Redis 缓存]]",
            "type": "related_to",
            "context": "连接池依赖缓存服务。",
        }
        fragment = _make_fragment(
            "Redis 连接池元数据",
            "需要完整保留结构化字段。",
            relations=[relation],
            self_check_passed=False,
            self_check_issues=["缺少压测证据"],
            self_check_severity="warning",
            cross_agent_links=["[[压测代理]]"],
            ai_expansion="AI 扩充内容",
        )

        prompt = merger._build_merge_prompt([fragment])
        restored = merger._dict_to_fragment(
            json.loads(
                _valid_merged_json(
                    "Redis 连接池元数据方案",
                    "完整的元数据转换内容。",
                    preserved_fragment=fragment,
                )
            )
        )

        assert '"relations"' in prompt
        assert "[[Redis 缓存]]" in prompt
        assert '"self_check_passed"' in prompt
        assert '"self_check_issues"' in prompt
        assert '"self_check_severity"' in prompt
        assert '"cross_agent_links"' in prompt
        assert '"ai_expansion"' in prompt
        assert "置信度审计值" in prompt
        assert restored is not None
        assert restored.relations == [relation]
        assert restored.self_check_passed is False
        assert restored.self_check_issues == ["缺少压测证据"]
        assert restored.self_check_severity == "warning"
        assert restored.cross_agent_links == ["[[压测代理]]"]
        assert restored.ai_expansion == "AI 扩充内容"

    def test_llm_metadata_loss_falls_back_to_rule_merge(self, merger):
        relation = {
            "target": "[[Redis 连接池]]",
            "type": "causes",
            "context": "上限过低会造成耗尽。",
        }
        frags = [
            _make_fragment(
                "Redis 连接池耗尽",
                "原因分析。",
                relations=[relation],
                self_check_passed=False,
                self_check_issues=["仍需容量验证"],
                self_check_severity="warning",
                cross_agent_links=["[[容量评估代理]]"],
                ai_expansion="扩充线索",
            ),
            _make_fragment("Redis 连接池解决", "解决方案。"),
        ]
        merger._call_llm = lambda prompt: _valid_merged_json(
            "LLM 丢失元数据的标题", "看似完整但遗漏了结构化字段。"
        )

        result = merger.merge(frags)

        assert len(result) == 1
        assert result[0].title != "LLM 丢失元数据的标题"
        assert result[0].relations == [relation]
        assert result[0].self_check_passed is False
        assert result[0].self_check_issues == ["仍需容量验证"]
        assert result[0].cross_agent_links == ["[[容量评估代理]]"]
        assert result[0].ai_expansion == "扩充线索"

    def test_llm_visible_body_loss_falls_back_to_exact_rule_merge(self, merger):
        first = _make_fragment(
            "Redis 连接池正文一",
            "## 第一块\n\n第一块正文必须保留。\n\n```python\nvalue = 1\n```",
            background="第一块背景必须保留。",
        )
        second = _make_fragment(
            "Redis 连接池正文二",
            "## 第二块\n\n第二块正文必须保留。\n\n```python\nvalue = 1\n```",
            background="第二块背景必须保留。",
        )
        expected = merger._rule_merge_cluster([first, second])

        def _lossy_llm_response(_prompt):
            import json

            payload = json.loads(
                _valid_merged_json(
                    "LLM 看似合法但丢失正文",
                    "只保留第二块正文。",
                    preserved_fragment=expected,
                )
            )
            payload["background"] = second.background
            payload["core_content"] = second.core_content
            return json.dumps(payload, ensure_ascii=False)

        merger._call_llm = _lossy_llm_response

        result = merger.merge([first, second])

        assert len(result) == 1
        assert result[0].title == expected.title
        assert result[0].background == f"{first.background}\n\n{second.background}"
        assert result[0].core_content == f"{first.core_content}\n\n{second.core_content}"

    def test_llm_provenance_loss_falls_back_without_losing_later_chunk(self, merger):
        import json

        first_ref = {
            "revision_id": "raw-revision-1",
            "logical_event_id": "event-1",
            "span_start": 0,
            "span_end": 18,
        }
        second_ref = {
            "revision_id": "raw-revision-2",
            "logical_event_id": "event-2",
            "span_start": 18,
            "span_end": 46,
        }
        first_span = {**first_ref, "chunk_index": 0}
        second_span = {**second_ref, "chunk_index": 1}
        fragments = [
            _make_fragment(
                "Redis 连接池来源一",
                "第一分块的精确来源。",
                frontmatter={
                    "领域": "backend",
                    "置信度": 0.85,
                    "摘要": "第一分块来源",
                    "raw_event_refs": [first_ref],
                    "chunk_source_spans": [first_span],
                },
            ),
            _make_fragment(
                "Redis 连接池来源二",
                "第二分块的精确来源。",
                frontmatter={
                    "领域": "backend",
                    "置信度": 0.8,
                    "摘要": "第二分块来源",
                    "raw_event_refs": [second_ref],
                    "chunk_source_spans": [second_span],
                },
            ),
        ]
        expected = merger._rule_merge_cluster(fragments)

        def _lossy_llm_response(_prompt):
            payload = json.loads(
                _valid_merged_json(
                    "LLM 丢失后续分块来源",
                    "看似完整但遗漏后续分块来源。",
                    preserved_fragment=expected,
                )
            )
            payload["frontmatter"].pop("raw_event_refs")
            payload["frontmatter"].pop("chunk_source_spans")
            return json.dumps(payload, ensure_ascii=False)

        merger._call_llm = _lossy_llm_response

        result = merger.merge(fragments)

        assert len(result) == 1
        assert result[0].title != "LLM 丢失后续分块来源"
        assert result[0].frontmatter["raw_event_refs"] == [first_ref, second_ref]
        assert result[0].frontmatter["chunk_source_spans"] == [first_span, second_span]

    def test_validation_diagnostics_do_not_log_llm_output(self, merger, monkeypatch, caplog):
        """Validation errors may quote model output, so logs keep only a category."""
        import core.hephaestus.fragment_merger as fragment_merger

        marker = _sensitive_provider_error_marker()
        source = _make_fragment("Redis 连接池", "原因")
        expected_metadata = merger._rule_merge_cluster([source])
        merger._call_llm = lambda _prompt: _valid_merged_json(
            "Redis 连接池完整方案",
            "原因分析 + 解决方案",
            preserved_fragment=expected_metadata,
        )
        monkeypatch.setattr(
            fragment_merger,
            "_strict_validate_fragments",
            lambda _fragments: (False, [marker]),
        )
        caplog.set_level(logging.DEBUG)

        assert merger._llm_merge_cluster([source]) is None

        assert marker not in caplog.text
        assert "category=invalid_fragment_output" in caplog.text


class TestFragmentMergerIntegration:
    def test_fragment_merger_integration_in_process(self, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            ValuePrejudgment,
        )
        from core.hephaestus.fragment_merger import FragmentMerger

        engine = DistillationEngine(wiki_base=str(tmp_path))
        engine._noise_filter = SimpleNamespace(  # noqa
            filter=lambda messages: (messages, {"kept": len(messages)})
        )
        engine._value_prejudgment = SimpleNamespace(  # noqa
            judge=lambda messages: (ValuePrejudgment.CERTAINLY_YES, 0.9)
        )
        engine._self_check = SimpleNamespace(check=lambda fragments, messages: (True, []))
        engine._cross_linker = SimpleNamespace(link=lambda fragments: fragments)
        engine._feedback_loop = SimpleNamespace(evaluate=lambda result: [])  # noqa
        engine._kia_linker = False
        extracted_fragments = []

        # extractor 返回一个已通过根 union 合同的两个同一话题局部片段。
        def _extract(request, *, prepared=None):
            del prepared
            from core.hephaestus.distillation_contract import (
                canonical_extraction_output_hash,
                canonicalize_extraction_output,
                validate_extraction_output,
            )
            from core.hephaestus.distillation_models import ExtractionOutcome

            fragments = [
                _make_fragment("Redis 连接池耗尽排查", "原因是上限过低。"),
                _make_fragment("Redis 连接池解决方案", "需要增加 max_connections。"),
            ]
            extracted_fragments[:] = fragments
            evidence = model_exact_evidence(request.input_spec)
            structured = {
                "schema_version": "distill_output_v4",
                **request.input_spec.prompt_contract(),
                "distill_intent": "create",
                "candidate_summary": "Redis 连接池问题需要合并为完整排查和修复方案。",
                "user_behavior_intent": {
                    "content_source": "native_dialogue",
                    "user_intent_signal": "seeking_judgment",
                    "intent_hypothesis": "seeking_judgment",
                    "intent_evidence": [
                        dict(evidence)
                    ],
                    "intent_verification_events": [],
                    "intent_confidence": 0.8,
                    "intent_status": "unverified",
                    "behavior_summary": "用户需要可执行的 Redis 连接池排查和修复方案。",
                },
                "claims": [
                    {
                        "claim_id": "redis-pool-merge",
                        "claim_text": "Redis 连接池耗尽需要同时检查上限和超时，并形成完整修复方案。",
                        "claim_type": "technical_fact",
                        "scope": {"domain": "backend"},
                        "evidence": [dict(evidence)],
                        "relation_to_existing": {"type": "new"},
                        "recommended_action": "create_page",
                        "confidence": 0.85,
                    }
                ],
                "cognition_episode": model_cognition_episode(
                    evidence,
                    claim_id="redis-pool-merge",
                ),
            }
            root = canonicalize_extraction_output(
                {
                    "judgment": "knowledge",
                    "judgment_reason": "局部片段共同构成可复用的 Redis 排查知识。",
                    "structured_output": structured,
                },
                fragments,
            )
            root = resolve_model_evidence(root, request.input_spec)
            structured = root["structured_output"]
            admission = validate_extraction_output(root, request.input_spec)
            assert admission.valid, admission.error_text
            return ExtractionOutcome(
                judgment="knowledge",
                fragments=tuple(fragments),
                structured_output=structured,
                canonical_output=root,
                admission=admission,
                canonical_output_hash=canonical_extraction_output_hash(
                    canonical_output=root
                ),
            )

        def _prepare(request):
            from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

            return PreparedExtractionPrompt.build("fragment merger fixture", request)

        engine._extractor = SimpleNamespace(
            extract=_extract,
            prepare_prompt=_prepare,
        )

        # 注入 mock FragmentMerger：LLM 总是成功
        merger = FragmentMerger()
        merger._call_llm = lambda prompt: _valid_merged_json(
            "Redis 连接池完整方案",
            "上限过低，需增加 max_connections。",
            preserved_fragment=merger._rule_merge_cluster(extracted_fragments),
        )
        engine._fragment_merger = merger  # noqa

        message = exact_source_message(
            role="user",
            content="Redis 连接池耗尽了怎么办？",
            revision_id="raw-merge-1",
        )
        result = engine.process(
            "sess-merge",
            [message],
            meta={"raw_event_refs": [dict(message["source_span"])]},
        )

        assert result.judgment == "knowledge"
        assert len(result.fragments) == 1
        assert result.fragments[0].title == "Redis 连接池完整方案"
        assert result.fragment_route_capability is not None
        assert result.fragment_route_capability.fragments == (result.fragments[0],)
