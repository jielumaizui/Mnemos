# -*- coding: utf-8 -*-
"""
DocumentDistillationPipeline 单元测试

覆盖公共行为：
1. __init__ — 初始化（wiki_base 传入 / 默认配置）
2. process — 主处理入口（含 skip / index 判断、知识提取、自检、跨 Agent 关联）
3. write_to_wiki — 写入 wiki Inbox（含 slug 去重、来源追踪）
4. _parse_doc_header — 从内容解析标题和文档类型
5. _slugify — 名称转文件安全 slug
6. _record_source_links — 来源追踪写入 SQLite
7. process + write_to_wiki 组合行为
8. 错误处理 — 空消息、空内容、LLM 失败时的优雅降级

Mock 策略：
- LLM 调用：mock HttpApiHostAgentCaller.call，返回预设 JSON
- 文件系统：tmp_path 创建真实临时目录
- 配置：mock get_config 返回 FakeConfig
- 跨 Agent 关联：mock CrossAgentLinker
- 画像信号：mock get_signal_store
"""

import sys
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.hephaestus.document_pipeline import (  # noqa: E402
    DocumentDistillationPipeline,
    DocumentDistillResult,
    DocumentJudgeResult,
    DocumentLLMJudge,
    DocumentKnowledgeExtractor,
)
from core.hephaestus.distillation_engine import (  # noqa: E402
    KnowledgeFragment,
    DistillationAPIError,
)  # noqa: E402
from core.hephaestus.distill_response import DistillBackendResponse  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)  # noqa
def _block_event_bus_leakage(monkeypatch):
    """阻止测试期间发布的事件进入 daemon 共享 events.db / 真实 wiki。

    DocumentDistillationPipeline.write_to_wiki 会发布 distill_complete 等事件；
    运行中的 daemon 可能消费这些事件并把测试临时页面写入生产 KG。
    默认将 publish_event 捕获到空列表，需要断言事件内容的测试可自行覆盖。
    """
    captured = []

    def capture(*args, **kwargs):
        captured.append((args, kwargs))
        return kwargs.get("trace_id") or "document-test-trace"

    monkeypatch.setattr("core.mnemos_bus.publish_event", capture)
    yield captured


@pytest.fixture
def fake_caller():
    """提供一个完全 mock 的 HttpApiHostAgentCaller。"""
    caller = MagicMock()
    caller.call.return_value = {}

    def call_with_evidence(prompt, expect_json=True, **kwargs):
        parsed = caller.call(prompt, expect_json=expect_json, **kwargs)
        return _typed_response(parsed)

    caller.call_with_evidence.side_effect = call_with_evidence
    return caller


def _typed_response(parsed):
    return DistillBackendResponse.create(
        raw_text=json.dumps(parsed, ensure_ascii=False),
        parsed=parsed,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
        provider="test-provider",
        model="test-model",
        parse_path="direct_json",
        attempt_history=({"attempt": 0, "status": "success"},),
    )


@pytest.fixture
def pipeline(tmp_path, fake_caller, monkeypatch):
    """提供一个已初始化、使用临时 wiki 目录的 pipeline。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.DocumentDistillationPipeline._get_wiki_dir",
        lambda self: tmp_path / "wiki",
    )
    p = DocumentDistillationPipeline(caller=fake_caller)
    return p


@pytest.fixture
def sample_doc_content():
    """标准文档内容，带标题头。"""
    return "# PDF: 测试文档\n\n## 第一章\n这是第一章的内容。\n\n## 第二章\n这是第二章的内容。"


@pytest.fixture
def sample_fragment():
    """返回一个标准 KnowledgeFragment（满足硬校验标准）。"""
    return KnowledgeFragment(
        form="方法论",
        title="测试方法论验证与最佳实践",
        frontmatter={"领域": "测试", "摘要": "这是用于测试文档蒸馏管道的摘要内容。"},
        background="背景信息",
        core_content=(
            "## 核心内容\n\n"
            "测试方法论的核心观点包括：\n"
            "1. 自动化测试覆盖核心业务路径\n"
            "2. 单元测试与集成测试分层执行\n"
            "3. 持续集成中集成质量门禁\n\n"
            "```python\n"
            "def test_example():\n"
            "    assert True\n"
            "```\n"
        ),
        boundaries={"applies": "适用场景", "not_applies": "不适用场景"},
        anti_patterns=["反模式1"],
        related_concepts=["相关概念1"],
    )


# ---------------------------------------------------------------------------
# 1. 初始化测试
# ---------------------------------------------------------------------------


def test_init_with_explicit_wiki_base(tmp_path, fake_caller):
    """显式传入 wiki_base 时，应正确设置目录。"""
    wiki = tmp_path / "my_wiki"
    p = DocumentDistillationPipeline(wiki_base=str(wiki), caller=fake_caller)

    assert p.wiki_base == wiki.expanduser()
    assert p.inbox_dir == wiki.expanduser() / "00-Inbox"
    assert p._caller is fake_caller


def test_document_failure_uses_explicit_database_root(
    tmp_path,
    fake_caller,
    sample_fragment,
    monkeypatch,
):
    database_dir = tmp_path / "isolated-database"
    pipeline = DocumentDistillationPipeline(
        wiki_base=str(tmp_path / "wiki"),
        caller=fake_caller,
        database_dir=database_dir,
    )
    captured = {}

    def capture_failure(*_args, **kwargs):
        captured.update(kwargs)
        return tmp_path / "failure.json"

    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._save_failed_distill",
        capture_failure,
    )
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._auto_remediate_fragment",
        lambda _fragment: False,
    )
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._validate_fragment",
        lambda _fragment: ["invalid fragment"],
    )
    result = DocumentDistillResult(
        session_id="document-failure",
        judgment="index",
        fragments=[sample_fragment],
        raw_response='["provider response"]',
        failure_parse_metadata={"source_event_refs": ["raw-document-1"]},
    )

    assert pipeline._filter_valid_fragments(result, "document:test") is None
    assert captured["database_dir"] == database_dir
    assert captured["parse_metadata"] == result.failure_parse_metadata


def test_init_with_default_wiki_dir(tmp_path, fake_caller, monkeypatch):
    """未传入 wiki_base 时，应从配置读取默认 wiki 目录。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.DocumentDistillationPipeline._get_wiki_dir",
        lambda self: tmp_path / "default_wiki",
    )
    p = DocumentDistillationPipeline(caller=fake_caller)

    assert p.wiki_base == tmp_path / "default_wiki"
    assert p.inbox_dir == tmp_path / "default_wiki" / "00-Inbox"


def test_init_components_created(pipeline):
    """初始化时应创建 judge、extractor、self_check 组件。"""
    assert pipeline._judge is not None
    assert isinstance(pipeline._judge, DocumentLLMJudge)
    assert pipeline._extractor is not None
    assert isinstance(pipeline._extractor, DocumentKnowledgeExtractor)
    assert pipeline._self_check is not None


def test_document_distillation_size_limits_are_public_contract():
    """文档蒸馏阈值常量是提示、分块和目标大小的公开契约。"""
    from core.hephaestus import document_pipeline

    assert document_pipeline.PREVIEW == 3000
    assert document_pipeline.PROMPT == 10000
    assert document_pipeline.TARGET_SIZE == 50000
    assert document_pipeline.CHUNK_SIZE == 8000
    assert document_pipeline.PREVIEW < document_pipeline.CHUNK_SIZE < document_pipeline.PROMPT
    assert document_pipeline.PROMPT < document_pipeline.TARGET_SIZE


# ---------------------------------------------------------------------------
# 2. process() 主入口测试
# ---------------------------------------------------------------------------


def test_process_empty_messages(pipeline):
    """空消息列表应返回 skip 结果。"""
    result = pipeline.process("sid-001", [], {})

    assert isinstance(result, DocumentDistillResult)
    assert result.session_id == "sid-001"
    assert result.judgment == "skip"
    assert result.fragments == []


def test_process_empty_content(pipeline):
    """消息内容为空时应返回 skip 结果。"""
    result = pipeline.process("sid-001", [{"content": ""}], {})

    assert result.judgment == "skip"
    assert result.fragments == []


def test_process_skip_judgment(pipeline, sample_doc_content, monkeypatch):
    """judge 返回 skip 时，应提前返回，不执行知识提取。"""
    judge_result = DocumentJudgeResult(judgment="skip", doc_category="reference")
    monkeypatch.setattr(pipeline._judge, "judge", lambda **kw: judge_result)
    # 如果 extractor 被调用，说明没有提前返回
    extract_mock = MagicMock(return_value=([], {}))
    monkeypatch.setattr(pipeline._extractor, "extract", extract_mock)

    result = pipeline.process("sid-002", [{"content": sample_doc_content}], {})

    assert result.judgment == "skip"
    assert result.doc_category == "reference"
    assert result.fragments == []
    extract_mock.assert_not_called()


def test_process_index_judgment_full_flow(
    pipeline, sample_doc_content, sample_fragment, monkeypatch
):
    """judge 返回 index 时，应走完提取、自检、跨 Agent 关联全流程。"""
    judge_result = DocumentJudgeResult(
        judgment="index",
        doc_category="report",
        entity_type="retrospective",
        key_topics=["复盘"],
        confidence=0.9,
    )
    monkeypatch.setattr(pipeline._judge, "judge", lambda **kw: judge_result)
    monkeypatch.setattr(
        pipeline._extractor,
        "extract",
        lambda content, judge, session_id="": ([sample_fragment], {}),
    )
    # mock 画像信号写入，避免导入 core.persona.psyche
    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: MagicMock(),
    )
    # mock 跨 Agent 关联
    fake_linker = MagicMock()
    fake_linker.link_after_distill_for_fragment.return_value = []
    monkeypatch.setattr(pipeline, "_get_cross_linker", lambda: fake_linker)

    result = pipeline.process(
        "sid-003", [{"content": sample_doc_content}], {"filename": "test.pdf"}
    )

    assert result.judgment == "index"
    assert result.doc_category == "report"
    assert len(result.fragments) == 1
    assert result.fragments[0].title == "测试方法论验证与最佳实践"
    # 自检应被执行
    assert hasattr(result.fragments[0], "self_check_passed")
    # 跨 Agent 关联应被执行
    fake_linker.link_after_distill_for_fragment.assert_called_once()


def test_process_cross_linker_failure_graceful(
    pipeline, sample_doc_content, sample_fragment, monkeypatch
):
    """跨 Agent 关联失败时不应抛异常，应继续返回结果。"""
    judge_result = DocumentJudgeResult(
        judgment="index", doc_category="reference", key_topics=["测试"]
    )
    monkeypatch.setattr(pipeline._judge, "judge", lambda **kw: judge_result)
    monkeypatch.setattr(
        pipeline._extractor,
        "extract",
        lambda content, judge, session_id="": ([sample_fragment], {}),
    )
    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: MagicMock(),
    )
    # 模拟跨 Agent 关联抛异常
    monkeypatch.setattr(
        pipeline,
        "_get_cross_linker",
        lambda: MagicMock(
            link_after_distill_for_fragment=MagicMock(side_effect=RuntimeError("boom"))
        ),
    )

    result = pipeline.process("sid-004", [{"content": sample_doc_content}], {})

    assert result.judgment == "index"
    assert len(result.fragments) == 1
    assert result.cross_agent_links == []


def test_document_provider_failure_enters_operational_incident_root(
    tmp_path,
    fake_caller,
    sample_doc_content,
    monkeypatch,
):
    from core.ops.operational_incident import initialize_operational_incident_schema

    database_dir = tmp_path / "database"
    initialize_operational_incident_schema(database_dir / "operational_incidents.db")
    pipeline = DocumentDistillationPipeline(
        wiki_base=str(tmp_path / "wiki"),
        caller=fake_caller,
        database_dir=database_dir,
    )
    monkeypatch.setattr(
        pipeline._judge,
        "judge",
        lambda **_kwargs: DocumentJudgeResult(judgment="index", doc_category="reference"),
    )
    monkeypatch.setattr(
        pipeline._extractor,
        "extract",
        MagicMock(side_effect=DistillationAPIError("provider request failed")),
    )
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.pause_distillation",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.generate_distillation_error_report",
        lambda _error: None,
    )

    result = pipeline.process(
        "document-provider-failure",
        [{"content": sample_doc_content}],
        {"source": "upload", "raw_event_id": "raw-document-provider-failure"},
    )

    assert result.judgment == "error"
    with sqlite3.connect(database_dir / "operational_incidents.db") as conn:
        row = conn.execute(
            """
            SELECT incident.producer, occurrence.error_codes_json,
                   occurrence.source_event_refs_json
            FROM incident_occurrences AS occurrence
            JOIN operational_incidents AS incident
              ON incident.incident_id=occurrence.incident_id
            """
        ).fetchone()
    assert row[0] == "document_distillation"
    assert "provider_failure" in row[1]
    assert "raw-document-provider-failure" in row[2]


def test_generic_long_document_is_extracted_across_all_chunks(monkeypatch, tmp_path):
    """非 book 长文档不能只取前 10000 字符，尾部内容也必须进入 LLM。"""
    prompts = []

    def fake_prompt(name):
        assert name == "generic_extract"
        return "{content} {judge_category} {judge_entity}"

    class FakeCaller:
        def call_with_evidence(self, prompt, expect_json=True, **kwargs):
            assert expect_json is True
            prompts.append(prompt)
            title = "包含尾部" if "TAIL_MARKER_FOR_FULL_DISTILL" in prompt else "普通分块"
            return _typed_response({
                "objective_extraction": {
                    "key_achievements": [
                        {
                            "achievement": title,
                            "metrics": "覆盖验证",
                            "factors": "分块输入",
                        }
                    ]
                },
                "frontmatter": {"关键词": ["分块"]},
            })

    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        fake_prompt,
    )
    extractor = DocumentKnowledgeExtractor(FakeCaller(), wiki_base=tmp_path)
    judge = DocumentJudgeResult(
        judgment="index",
        doc_category="reference",
        entity_type="technology",
    )
    content = "A" * 12000 + "TAIL_MARKER_FOR_FULL_DISTILL"

    fragments, data = extractor.extract(content, judge, session_id="sid-long")

    assert len(prompts) >= 2
    assert any("TAIL_MARKER_FOR_FULL_DISTILL" in prompt for prompt in prompts)
    assert any(fragment.title == "包含尾部" for fragment in fragments)
    assert data["source_coverage"]["mode"] == "full_chunked"


# ---------------------------------------------------------------------------
# 3. write_to_wiki() 测试
# ---------------------------------------------------------------------------


def test_write_to_wiki_creates_files(
    pipeline,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """write_to_wiki 应在 inbox 目录创建 markdown 文件。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.generate_wiki_page",
        lambda frag, sid, source="", **kwargs: f"# {frag.title}\n\n{frag.core_content}",
    )
    result = DocumentDistillResult(
        session_id="sid-005",
        judgment="index",
        fragments=[sample_fragment],
    )

    written = pipeline.write_to_wiki(result, source="test-source")

    assert len(written) == 1
    assert written[0].exists()
    assert written[0].suffix == ".md"
    content = written[0].read_text(encoding="utf-8")
    assert "测试方法论" in content


def test_write_to_wiki_routes_classified_document_to_formal_dir(
    pipeline,
    _canonical_material_actions,
):
    """可确定分类的文档蒸馏页面应直接写入正式目录并记录路由 frontmatter。"""
    frag = KnowledgeFragment(
        form="技术知识",
        title="Redis 文档路由测试",
        frontmatter={
            "type": "tech",
            "name": "Redis 文档路由测试",
            "domain": "redis",
            "summary": "验证文档蒸馏页面可以直接写入正式技术目录。",
        },
        background="背景信息",
        core_content=(
            "## 核心内容\n\n"
            "Redis 文档路由测试说明结构化文档蒸馏结果可以被 Charon 识别。"
            "当 fragment 已经带有 type、name、domain 和 summary 时，"
            "写入链路应直接选择正式技术目录，并把路由状态写入 frontmatter，"
            "避免正式知识长期积压在 Inbox。\n\n"
            "```python\n"
            "print('redis')\n"
            "```\n"
        ),
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    result = DocumentDistillResult(
        session_id="sid-route",
        judgment="index",
        fragments=[frag],
    )

    written = pipeline.write_to_wiki(result, source="test-source")

    assert len(written) == 1
    assert pipeline.wiki_base / "03-Tech" in written[0].parents
    assert pipeline.inbox_dir not in written[0].parents
    content = written[0].read_text(encoding="utf-8")
    assert "Wiki路由状态: direct" in content
    assert "Wiki路由目标: 03-Tech" in content


def test_write_to_wiki_slug_deduplication(
    pipeline,
    monkeypatch,
    _canonical_material_actions,
):
    """同名 fragment 应生成带计数器的 slug，避免覆盖。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.generate_wiki_page",
        lambda frag, sid, source="", **kwargs: f"# {frag.title}",
    )
    frag1 = KnowledgeFragment(
        form="方法论",
        title="同名文档的去重验证测试",
        frontmatter={"领域": "test", "摘要": "用于测试 slug 去重逻辑的知识片段。"},
        background="",
        core_content="## 内容\n\n这是第一段内容，用于验证同名文档的去重逻辑。在知识管理系统中，当多个片段具有相同标题时，系统需要通过添加序号的方式来区分它们，确保每个片段都能被正确写入到独立的文件中。\n\n```python\nprint(1)\n```",  # noqa: E501
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    frag2 = KnowledgeFragment(
        form="方法论",
        title="同名文档的去重验证测试",
        frontmatter={"领域": "test", "摘要": "用于测试 slug 去重逻辑的知识片段。"},
        background="",
        core_content="## 内容\n\n这是第二段内容，用于验证同名文档的去重逻辑。在知识管理系统中，当多个片段具有相同标题时，系统需要通过添加序号的方式来区分它们，确保每个片段都能被正确写入到独立的文件中。\n\n```python\nprint(2)\n```",  # noqa: E501
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    result = DocumentDistillResult(
        session_id="sid-006",
        judgment="index",
        fragments=[frag1, frag2],
    )

    written = pipeline.write_to_wiki(result)

    assert len(written) == 2
    names = [p.name for p in written]
    assert "同名文档的去重验证测试.md" in names
    assert "同名文档的去重验证测试-1.md" in names
    assert len(set(names)) == 2


def test_write_to_wiki_empty_fragments(pipeline):
    """空 fragments 列表不应创建任何文件。"""
    result = DocumentDistillResult(
        session_id="sid-007",
        judgment="skip",
        fragments=[],
    )
    written = pipeline.write_to_wiki(result)

    assert written == []


def test_write_to_wiki_passes_distill_metadata(
    pipeline,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """文档 Wiki 页面也必须带 prompt version 和 source coverage。"""
    captured = {}

    def fake_generate_wiki_page(frag, sid, source="", **kwargs):
        captured.update(kwargs)
        return f"# {frag.title}\n\n{frag.core_content}"

    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.generate_wiki_page",
        fake_generate_wiki_page,
    )
    result = DocumentDistillResult(
        session_id="sid-doc-meta",
        judgment="index",
        fragments=[sample_fragment],
        source_coverage="full_chunked",
        distill_input_mode="chunked",
    )

    pipeline.write_to_wiki(result, source="mcp")

    assert captured["session_coverage"] == "full_chunked"
    assert captured["distill_input_mode"] == "chunked"
    assert captured["distill_prompt_version"]


def test_write_to_wiki_includes_book_meta_frontmatter(
    pipeline,
    sample_fragment,
    _canonical_material_actions,
):
    """book_meta 应进入书籍文档页面 frontmatter。"""
    from core.frontmatter import parse_frontmatter

    result = DocumentDistillResult(
        session_id="sid-book-meta",
        judgment="index",
        doc_category="book",
        fragments=[sample_fragment],
        book_meta={"chapter_count": 1, "concept_count": 1, "key_topics": ["决策"]},
    )

    written = pipeline.write_to_wiki(result, source="book-upload")

    frontmatter, _ = parse_frontmatter(written[0].read_text(encoding="utf-8"))
    assert frontmatter["书籍元数据"]["chapter_count"] == 1
    assert frontmatter["书籍元数据"]["concept_count"] == 1
    assert frontmatter["书籍元数据"]["key_topics"] == ["决策"]


def test_write_to_wiki_includes_data_insights_frontmatter(
    pipeline,
    sample_fragment,
    _canonical_material_actions,
):
    """data_insights 应进入数据文档页面 frontmatter。"""
    from core.frontmatter import parse_frontmatter

    result = DocumentDistillResult(
        session_id="sid-data-insights",
        judgment="index",
        doc_category="data",
        fragments=[sample_fragment],
        data_insights={
            "metric": "retention",
            "trend": "up",
            "segments": ["new_users", "returning_users"],
        },
    )

    written = pipeline.write_to_wiki(result, source="data-upload")

    frontmatter, _ = parse_frontmatter(written[0].read_text(encoding="utf-8"))
    assert frontmatter["数据洞察"]["metric"] == "retention"
    assert frontmatter["数据洞察"]["trend"] == "up"
    assert frontmatter["数据洞察"]["segments"] == ["new_users", "returning_users"]


def test_write_to_wiki_includes_report_items_frontmatter(
    pipeline,
    sample_fragment,
    _canonical_material_actions,
):
    """report_items 应进入报告文档页面 frontmatter。"""
    from core.frontmatter import parse_frontmatter

    result = DocumentDistillResult(
        session_id="sid-report-items",
        judgment="index",
        doc_category="report",
        fragments=[sample_fragment],
        report_items={
            "findings": ["发现 A", "发现 B"],
            "risks": ["风险 C"],
            "recommendations": ["行动 D"],
        },
    )

    written = pipeline.write_to_wiki(result, source="report-upload")

    frontmatter, _ = parse_frontmatter(written[0].read_text(encoding="utf-8"))
    assert frontmatter["报告要点"]["findings"] == ["发现 A", "发现 B"]
    assert frontmatter["报告要点"]["risks"] == ["风险 C"]
    assert frontmatter["报告要点"]["recommendations"] == ["行动 D"]


def test_write_to_wiki_includes_strategy_items_frontmatter(
    pipeline,
    sample_fragment,
    _canonical_material_actions,
):
    """strategy_items 应进入策略文档页面 frontmatter。"""
    from core.frontmatter import parse_frontmatter

    result = DocumentDistillResult(
        session_id="sid-strategy-items",
        judgment="index",
        doc_category="strategy",
        fragments=[sample_fragment],
        strategy_items={
            "key_decisions": ["决策 A"],
            "methodologies": ["方法 B"],
            "lessons_learned": ["经验 C"],
        },
    )

    written = pipeline.write_to_wiki(result, source="strategy-upload")

    frontmatter, _ = parse_frontmatter(written[0].read_text(encoding="utf-8"))
    assert frontmatter["策略要点"]["key_decisions"] == ["决策 A"]
    assert frontmatter["策略要点"]["methodologies"] == ["方法 B"]
    assert frontmatter["策略要点"]["lessons_learned"] == ["经验 C"]


def test_write_to_wiki_includes_table_artifacts_frontmatter(
    pipeline,
    sample_fragment,
    _canonical_material_actions,
):
    """table_artifacts 应进入页面 frontmatter，保留表格回放证据。"""
    from core.frontmatter import parse_frontmatter

    result = DocumentDistillResult(
        session_id="sid-table-frontmatter",
        judgment="index",
        doc_category="data",
        fragments=[sample_fragment],
        table_artifacts=[
            {
                "uri": "mnemos-table://document/sid-table-frontmatter/table/0",
                "row_count": 17,
                "col_count": 3,
                "sha256": "abc123",
                "chunk_uris": [
                    "mnemos-table://document/sid-table-frontmatter/table/0/chunk/0"
                ],
            }
        ],
    )

    written = pipeline.write_to_wiki(result, source="data-upload")

    frontmatter, _ = parse_frontmatter(written[0].read_text(encoding="utf-8"))
    assert frontmatter["表格证据"][0]["uri"] == (
        "mnemos-table://document/sid-table-frontmatter/table/0"
    )
    assert frontmatter["表格证据"][0]["row_count"] == 17


# ---------------------------------------------------------------------------
# 4. _parse_doc_header() 测试
# ---------------------------------------------------------------------------


def test_parse_doc_header_standard(pipeline):
    """标准标题头应正确解析标题和类型。"""
    content = "# doc PDF: 我的文档标题\n\n正文内容"
    title, doc_type = pipeline._parse_doc_header(content)

    assert title == "我的文档标题"
    assert doc_type == "pdf"


def test_parse_doc_header_pptx(pipeline):
    """PPTX 类型应正确解析。"""
    content = "# doc PPTX: 演示文稿\n\n幻灯片内容"
    title, doc_type = pipeline._parse_doc_header(content)

    assert title == "演示文稿"
    assert doc_type == "pptx"


def test_parse_doc_header_no_match(pipeline):
    """无匹配标题头时应返回默认值。"""
    content = "这是没有标题头的普通内容"
    title, doc_type = pipeline._parse_doc_header(content)

    assert title == "未命名文档"
    assert doc_type == "unknown"


# ---------------------------------------------------------------------------
# 5. _slugify() 测试
# ---------------------------------------------------------------------------


def test_slugify_basic(pipeline):
    """基本名称转 slug。"""
    assert pipeline._slugify("Hello World") == "hello-world"


def test_slugify_chinese(pipeline):
    """中文名称保留汉字。"""
    assert pipeline._slugify("测试文档") == "测试文档"


def test_slugify_mixed(pipeline):
    """中英文混合。"""
    assert pipeline._slugify("Test 测试 123") == "test-测试-123"


def test_slugify_special_chars(pipeline):
    """特殊字符转为连字符。"""
    assert pipeline._slugify("a@b#c$d") == "a-b-c-d"


def test_slugify_truncate(pipeline):
    """超长名称截断至 64 字符。"""
    long_name = "a" * 100
    slug = pipeline._slugify(long_name)
    assert len(slug) == 64


def test_slugify_empty(pipeline):
    """空名称返回 untitled。"""
    assert pipeline._slugify("") == "untitled"
    assert pipeline._slugify("   ") == "untitled"


# ---------------------------------------------------------------------------
# 6. _record_source_links() 测试
# ---------------------------------------------------------------------------


def test_record_source_links_creates_table(pipeline, tmp_path, monkeypatch):
    """应创建 document_wiki_link 表并插入记录。"""
    # _record_source_links 使用 get_config().data_dir / "knowledge_graph.db"
    db_file = tmp_path / "knowledge_graph.db"
    fake_cfg = MagicMock()
    fake_cfg.data_dir = tmp_path
    fake_cfg.database_dir = tmp_path
    monkeypatch.setattr("core.config.get_config", lambda: fake_cfg)

    wiki_page = tmp_path / "wiki" / "00-Inbox" / "test.md"
    wiki_page.parent.mkdir(parents=True, exist_ok=True)
    wiki_page.write_text("test")
    pipeline.wiki_base = tmp_path / "wiki"

    pipeline._record_source_links("sid-008", "upload", [wiki_page])

    import sqlite3

    with sqlite3.connect(str(db_file)) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        assert "document_wiki_link" in tables

        cur = conn.execute("SELECT session_id, source, wiki_page_path FROM document_wiki_link")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "sid-008"
        assert rows[0][1] == "upload"


def test_record_source_links_failure_graceful(pipeline, monkeypatch):
    """数据库写入失败时不应抛异常。"""
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: MagicMock(
            data_dir=Path("/nonexistent/path"), database_dir=Path("/nonexistent/path")
        ),
    )
    # 不应抛异常
    pipeline._record_source_links("sid-009", "test", [Path("/fake/path.md")])


# ---------------------------------------------------------------------------
# 7. process + write_to_wiki 组合行为
# ---------------------------------------------------------------------------

def test_process_with_inbox(
    monkeypatch,
    tmp_path,
    fake_caller,
    sample_fragment,
    _canonical_material_actions,
):
    """显式 process + write_to_wiki 会提交 fragment 页面。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.DocumentDistillationPipeline._get_wiki_dir",
        lambda self: tmp_path / "wiki",
    )
    pipeline = DocumentDistillationPipeline(caller=fake_caller)
    monkeypatch.setattr(
        pipeline._judge,
        "judge",
        lambda **kw: DocumentJudgeResult(judgment="index", doc_category="reference"),
    )
    monkeypatch.setattr(
        pipeline._extractor,
        "extract",
        lambda content, judge, session_id="": ([sample_fragment], {}),
    )
    monkeypatch.setattr(
        "core.persona.psyche.get_signal_store",
        lambda: MagicMock(),
    )
    inbox = tmp_path / "custom_inbox"
    pipeline.inbox_dir = inbox

    result = pipeline.process(
        "sid-011",
        [{"content": "# doc PDF: 测试\n内容"}],
        {"source": "upload"},
    )

    assert result.judgment == "index"
    assert len(result.fragments) == 1

    pipeline.write_to_wiki(result, source="upload")
    assert inbox.exists()
    assert any(inbox.iterdir())


# ---------------------------------------------------------------------------
# 8. DocumentLLMJudge 测试
# ---------------------------------------------------------------------------


def test_judge_llm_success(fake_caller, monkeypatch):
    """LLM 返回有效 JSON 时应正确解析。"""
    fake_caller.call.return_value = {
        "judgment": "index",
        "doc_category": "book",
        "entity_type": "concept",
        "key_topics": ["AI", "ML"],
        "audience": "开发者",
        "why": "有价值",
    }
    judge = DocumentLLMJudge(caller=fake_caller)
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        lambda name: "{title} {doc_type} {page_count} {outline} {content_preview}",
    )

    result = judge.judge("测试", "pdf", "内容", {"pages": 100}, "sid")

    assert result.judgment == "index"
    assert result.doc_category == "book"
    assert result.entity_type == "concept"
    assert result.key_topics == ["AI", "ML"]
    assert result.audience == "开发者"
    assert result.why == "有价值"
    assert result.confidence == 0.85


def test_document_judge_result_why_serializes_contract():
    """DocumentJudgeResult.why 是文档价值判断的可序列化合同字段。"""
    result = DocumentJudgeResult(
        judgment="index",
        doc_category="report",
        entity_type="project",
        why="包含可沉淀的项目复盘依据",
        confidence=0.9,
    )

    assert result.why == "包含可沉淀的项目复盘依据"
    assert asdict(result)["why"] == "包含可沉淀的项目复盘依据"


def test_judge_llm_failure_raises_exception(fake_caller, monkeypatch):
    """LLM 调用失败时应抛出 DistillationAPIError，不再规则回退。"""
    fake_caller.call.side_effect = DistillationAPIError("所有 API 不可用")
    judge = DocumentLLMJudge(caller=fake_caller)
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        lambda name: "template",
    )

    with pytest.raises(DistillationAPIError):
        judge.judge("测试", "pdf", "内容", {"pages": 100}, "sid")


def test_judge_llm_failure_data_type_raises(fake_caller, monkeypatch):
    """数据类型文档 LLM 失败也应抛出异常，不再回退。"""
    fake_caller.call.side_effect = DistillationAPIError("所有 API 不可用")
    judge = DocumentLLMJudge(caller=fake_caller)
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        lambda name: "template",
    )

    with pytest.raises(DistillationAPIError):
        judge.judge("数据", "xlsx", "内容", {}, "sid")


def test_judge_llm_failure_ppt_type_raises(fake_caller, monkeypatch):
    """PPT 类型文档 LLM 失败也应抛出异常，不再回退。"""
    fake_caller.call.side_effect = DistillationAPIError("所有 API 不可用")
    judge = DocumentLLMJudge(caller=fake_caller)
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        lambda name: "template",
    )

    with pytest.raises(DistillationAPIError):
        judge.judge("报告", "pptx", "内容", {}, "sid")


# ---------------------------------------------------------------------------
# 9. DocumentKnowledgeExtractor 测试
# ---------------------------------------------------------------------------


def test_extract_generic_llm_success(fake_caller):
    """通用提取应通过 LLM 返回 fragment。"""
    fake_caller.call.return_value = {
        "objective_extraction": {
            "key_achievements": [{"achievement": "测试成果", "metrics": "指标"}],
            "patterns": [{"pattern": "模式", "context": "上下文"}],
        },
        "ai_expansion": {},
        "frontmatter": {
            "boundaries": {},
            "anti_patterns": [],
            "关键词": ["测试"],
            "触发器": [],
            "别名": [],
        },
    }
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    judge = DocumentJudgeResult(
        judgment="index",
        doc_category="reference",
        key_topics=["测试"],
        audience="开发者",
    )
    fragments, data = extractor.extract("一些内容", judge)

    assert len(fragments) >= 1
    assert any(f.title == "测试成果" for f in fragments)
    frag = next(f for f in fragments if f.title == "测试成果")
    assert frag.frontmatter.get("摘要") == "指标"
    assert frag.frontmatter.get("受众") == "开发者"


def test_extract_book_returns_book_meta(fake_caller, monkeypatch):
    """书籍提取结果应携带可写入页面的 book_meta。"""
    fake_caller.call.return_value = {
        "concepts": [
            {
                "title": "有限理性",
                "form": "concept",
                "content": (
                    "## 作者核心论点\n人类决策会受到信息和认知限制影响。\n\n"
                    "## 关键实验与证据\n作者在本章描述了有限信息下的选择案例。\n\n"
                    "## 边界与失效条件\n作者在本章未明确讨论此方面。\n\n"
                    "## 防御策略\n通过外部清单降低遗漏风险。"
                ),
                "frontmatter": {
                    "关键词": ["决策"],
                    "触发器": [],
                    "别名": [],
                    "boundaries": {
                        "applies": "信息不完备的决策",
                        "not_applies": "完全确定的机械规则",
                    },
                    "anti_patterns": ["把有限理性误解为无需验证"],
                },
                "relations": [],
            }
        ]
    }
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        lambda name: "{related_pages}\n{book_content}",
    )
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    judge = DocumentJudgeResult(
        judgment="index",
        doc_category="book",
        key_topics=["决策"],
        audience="研究者",
    )

    fragments, data = extractor.extract("## 第一章\n人类怎样做决定", judge)

    assert len(fragments) == 1
    assert data["book_meta"]["chapter_count"] == 1
    assert data["book_meta"]["concept_count"] == 1
    assert data["book_meta"]["key_topics"] == ["决策"]


def test_extract_book_rejects_unversioned_objective_schema(fake_caller, monkeypatch):
    """书籍提取只接受 prompt 声明的 concepts[] 合同。"""
    fake_caller.call.return_value = {
        "objective_extraction": {"methodologies": [{"name": "旧输出"}]}
    }
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        lambda name: "{related_pages}\n{book_content}",
    )
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    judge = DocumentJudgeResult(judgment="index", doc_category="book")

    with pytest.raises(ValueError, match=r"concepts\[\]"):
        extractor.extract("## 第一章\n正文", judge)


def test_preprocess_large_tables_preserves_replayable_artifact(fake_caller, tmp_path):
    """超大表格 prompt 可采样，但完整表格必须落为可回放 artifact。"""
    extractor = DocumentKnowledgeExtractor(caller=fake_caller, wiki_base=tmp_path)
    # 构造一个 15 行 × 3 列的表格（超过默认 max_rows=12）
    table_lines = ["| A | B | C |", "|---|---|---|"]
    for i in range(15):
        table_lines.append(f"| {i} | v{i} | x{i} |")
    content = "\n".join(table_lines)

    result = extractor._preprocess_large_tables(
        content,
        session_id="sid-table",
        max_rows=12,
        max_cols=8,
    )

    assert "大表格" in result
    assert "17 行" in result
    assert "mnemos-table://document/sid-table/table/0" in result
    assert "已在预处理阶段截断" not in result
    assert result.count("|") < content.count("|")  # prompt 行数减少

    artifact = extractor.last_table_artifacts[0]
    assert artifact["uri"] == "mnemos-table://document/sid-table/table/0"
    assert artifact["row_count"] == 17
    assert artifact["data_row_count"] == 15
    assert artifact["col_count"] == 3
    artifact_path = Path(artifact["artifact_path"])
    assert artifact_path.exists()
    stored = artifact_path.read_text(encoding="utf-8")
    assert '"rows"' in stored
    assert "v14" in stored


def test_process_large_table_attaches_artifact_refs(pipeline, fake_caller, monkeypatch):
    """文档蒸馏结果必须携带大表格 artifact 和 row/cell evidence refs。"""
    prompts = []

    def fake_prompt(name):
        assert name == "data_insight"
        return "{data_content}"

    def fake_call(prompt, expect_json=True, **kwargs):
        prompts.append(prompt)
        return {
            "data_profile": {"scope": "季度指标"},
            "insights": [
                {
                    "observation": "A 区域 GMV 持续增长",
                    "evidence": "见表格证据",
                    "implication": "继续追踪",
                    "confidence": "高",
                }
            ],
            "frontmatter": {"关键词": ["GMV"]},
        }

    monkeypatch.setattr(
        "core.hephaestus.document_pipeline._load_document_prompt",
        fake_prompt,
    )
    fake_caller.call.side_effect = fake_call
    monkeypatch.setattr(
        pipeline._judge,
        "judge",
        lambda **kw: DocumentJudgeResult(
            judgment="index",
            doc_category="data",
            key_topics=["GMV"],
            confidence=0.9,
        ),
    )
    table_lines = ["# doc CSV: GMV 表格", "", "| Region | Month | GMV |", "|---|---|---|"]
    for i in range(15):
        table_lines.append(f"| A | 2026-{i + 1:02d} | {i * 100} |")

    result = pipeline.process(
        "sid-process-table",
        [{"content": "\n".join(table_lines)}],
        {"filename": "gmv.md"},
    )

    assert prompts
    assert "mnemos-table://document/sid-process-table/table/0" in prompts[0]
    table_artifact = result.table_artifacts[0]
    assert table_artifact["row_count"] == 17
    assert table_artifact["data_row_count"] == 15
    assert Path(table_artifact["artifact_path"]).exists()
    assert result.data_insights["table_artifacts"][0]["uri"] == table_artifact["uri"]
    assert result.fragments[0].frontmatter["table_artifacts"][0]["uri"] == table_artifact["uri"]
    assert any("#row=" in ref or "#cell=" in ref for ref in table_artifact["evidence_refs"])


def test_chunk_by_chapters(fake_caller):
    """按章节分块应正确分割 Markdown 标题。"""
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    content = "\n".join(["## 第一章", "内容1", "## 第二章", "内容2", "## 第三章", "内容3"])
    chunks = extractor._chunk_by_chapters(content)

    assert len(chunks) >= 3
    assert any("第一章" in c for c in chunks)
    assert any("第二章" in c for c in chunks)


def test_chunk_by_chapters_pdf_page_mode(fake_caller):
    """PDF 按页模式应合并页面为更大 chunk。"""
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    # 模拟 30 个 "## 第 X 页" 标题
    lines = []
    for i in range(30):
        lines.append(f"## 第 {i+1} 页")
        lines.append(f"这是第 {i+1} 页的内容。" * 50)
    content = "\n".join(lines)

    chunks = extractor._chunk_by_chapters(content)

    # 应合并为较少的 chunk（< 30）
    assert len(chunks) < 30
    assert len(chunks) >= 1


def test_merge_ai_expansions_string_format(fake_caller):
    """字符串格式 AI 扩充应直接拼接。"""
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    result = extractor._merge_ai_expansions(["第一部分", "第二部分"])

    assert "第一部分" in result
    assert "第二部分" in result


def test_merge_ai_expansions_dict_format(fake_caller):
    """字典格式 AI 扩充应按字段合并。"""
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    expansions = [
        {"related_concepts": ["A", "B"], "potential_blindspots": ["X"]},
        {"related_concepts": ["B", "C"], "practice_suggestions": ["Y"]},
    ]
    result = extractor._merge_ai_expansions(expansions)

    assert "相关概念" in result
    assert "A" in result
    assert "C" in result
    assert "盲区提醒" in result
    assert "实践建议" in result


def test_merge_ai_expansions_empty(fake_caller):
    """空列表应返回空字符串。"""
    extractor = DocumentKnowledgeExtractor(caller=fake_caller)
    assert extractor._merge_ai_expansions([]) == ""


# ---------------------------------------------------------------------------
# 10. 边界与错误处理
# ---------------------------------------------------------------------------


def test_process_with_none_messages(pipeline):
    """None 消息应安全处理。"""
    result = pipeline.process("sid-012", None, {})
    assert result.judgment == "skip"


def test_write_to_wiki_missing_inbox_dir_created(
    pipeline,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """inbox 目录不存在时应自动创建。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.generate_wiki_page",
        lambda frag, sid, source="", **kwargs: f"# {frag.title}",
    )
    # 删除 inbox 目录
    import shutil

    if pipeline.inbox_dir.exists():
        shutil.rmtree(pipeline.inbox_dir)

    result = DocumentDistillResult(
        session_id="sid-013",
        judgment="index",
        fragments=[sample_fragment],
    )
    written = pipeline.write_to_wiki(result)

    assert pipeline.inbox_dir.exists()
    assert len(written) == 1


def test_write_to_wiki_emits_distill_complete_per_page(
    pipeline,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """write_to_wiki 应为每个写入的 wiki 页面发射 distill_complete 事件（P1-8）。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.generate_wiki_page",
        lambda frag, sid, source="", **kwargs: f"# {frag.title}",
    )

    events = []

    def _capture(
        event, emitter, payload, *, trace_id="", subject_provenance=None
    ):
        del subject_provenance
        events.append((event, emitter, payload))
        return trace_id or "document-test-trace"

    monkeypatch.setattr("core.mnemos_bus.publish_event", _capture)

    result = DocumentDistillResult(
        session_id="sess-emit",
        fragments=[sample_fragment, sample_fragment],
    )
    written = pipeline.write_to_wiki(result, source="test-source")

    assert len(written) == 2
    complete_events = [e for e in events if e[0] == "distill_complete"]
    assert len(complete_events) == 2
    for (_, _, payload), path in zip(complete_events, written):
        assert payload["page_path"] == str(path)
        assert payload["title"] == sample_fragment.title
        assert payload["session_id"] == "sess-emit"
        assert payload["form"] == sample_fragment.form


def test_write_to_wiki_emits_wiki_page_updated_per_page(
    pipeline,
    sample_fragment,
    monkeypatch,
    _canonical_material_actions,
):
    """write_to_wiki 应为每个写入的 wiki 页面发射 wiki_page_updated 事件（P1-9）。"""
    monkeypatch.setattr(
        "core.hephaestus.document_pipeline.generate_wiki_page",
        lambda frag, sid, source="", **kwargs: f"# {frag.title}",
    )

    events = []

    def _capture(
        event, emitter, payload, *, trace_id="", subject_provenance=None
    ):
        del subject_provenance
        events.append((event, emitter, payload))
        return trace_id or "document-test-trace"

    monkeypatch.setattr("core.mnemos_bus.publish_event", _capture)

    result = DocumentDistillResult(
        session_id="sess-wiki-updated",
        fragments=[sample_fragment, sample_fragment],
    )
    written = pipeline.write_to_wiki(result, source="test-source")

    assert len(written) == 2
    updated_events = [e for e in events if e[0] == "wiki_page_updated"]
    assert len(updated_events) == 2
    for (_, _, payload), path in zip(updated_events, written):
        assert payload["page_path"] == str(path)
        assert payload["update_type"] == "create"


def test_write_single_page_raises_when_filename_collision_exhausted(pipeline, monkeypatch):
    """磁盘文件名碰撞超过最大尝试次数时应抛出 RuntimeError。"""
    from pathlib import Path

    monkeypatch.setattr(Path, "exists", lambda self: True)
    frag = KnowledgeFragment(
        form="concept",
        title="title",
        frontmatter={},
        background="",
        core_content="content",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        relations=[],
    )
    result = DocumentDistillResult()
    with pytest.raises(RuntimeError, match="已尝试 10000 次"):
        pipeline._write_single_page(frag, "sess", "source", result, set())
