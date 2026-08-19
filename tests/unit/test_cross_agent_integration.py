# -*- coding: utf-8 -*-
"""
阶段三测试 — 跨 Agent 关联闭环整合

覆盖：
  1. generate_wiki_page 包含 summary frontmatter
  2. DistillationEngine.write_pages 调用新 CrossAgentLinker
  3. DistillationEngine 发射 distill_complete 事件
  4. _update_frontmatter_field 辅助方法
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.hephaestus.distill_input_spec import DistillInputSpec
from tests.cognition_episode_fixtures import (
    exact_source_message,
    model_cognition_episode,
    model_exact_evidence,
    resolve_model_evidence,
)


def _receipt_config(tmp_path: Path):
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.config import get_config

    config = get_config()
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    return SimpleNamespace(database_dir=tmp_path, get=config.get)


def _structured_output(session_id: str) -> tuple[DistillInputSpec, dict]:
    visible_input = f"cross-agent integration test: {session_id}; 测试写入和事件发射"
    input_spec = DistillInputSpec.build(
        source_agent="codex",
        source_session_id=session_id,
        source_event_ids=("raw-1",),
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        source_messages=(
            exact_source_message(
                role="user",
                content=visible_input,
                revision_id="raw-1",
            ),
        ),
    )
    evidence = model_exact_evidence(input_spec)
    structured = {
        "schema_version": "distill_output_v4",
        **input_spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "测试蒸馏写入和跨 Agent 事件链路。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    **dict(evidence),
                    "reason": "测试输入要求系统处理蒸馏链路。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.7,
            "intent_status": "unverified",
            "behavior_summary": "用户需要验证蒸馏写入和跨 Agent 事件链路。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "测试蒸馏写入会触发跨 Agent linker 或 distill_complete 事件。",
                "claim_type": "technical_fact",
                "scope": {"domain": "test", "applies_to": ["unit test"], "not_applies_to": []},
                "evidence": [dict(evidence)],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "测试临时 vault 中没有既有页面。",
                },
                "recommended_action": "create_page",
                "confidence": 0.8,
            }
        ],
        "cognition_episode": model_cognition_episode(
            evidence,
            claim_id="claim-1",
        ),
    }
    root = {
        "judgment": "knowledge",
        "judgment_reason": "cross-agent fixture",
        "fragments": [],
        "structured_output": structured,
    }
    return input_spec, resolve_model_evidence(root, input_spec)["structured_output"]


def _disable_structured_contract_gate(monkeypatch):
    """Cross-agent tests exercise post-write behavior, not extraction admission."""
    from core.config import get_config as real_get_config

    base = real_get_config()

    class _GateDisabledConfig:
        def get(self, key, default=None):
            if key == "distill.structured_output_contract.enforce":
                return False
            return base.get(key, default)

        def __getattr__(self, name):
            return getattr(base, name)

    monkeypatch.setattr(
        "core.hephaestus.distillation_engine.get_config",
        lambda: _GateDisabledConfig(),
    )


def _attach_admitted_extraction_root(result):
    """Give direct write fixtures the same immutable root proof as extraction."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import FragmentRouteCapability

    assert isinstance(result.input_spec, DistillInputSpec)
    root = canonicalize_extraction_output(
        {
            "judgment": "knowledge",
            "judgment_reason": "跨 Agent 写入测试使用已准入的提取根输出。",
            "structured_output": result.structured_output,
        },
        result.fragments,
    )
    validation = validate_extraction_output(root, result.input_spec)
    assert validation.valid, validation.error_text
    result.extraction_judgment = "knowledge"
    result.extraction_contract_valid = True
    result.extraction_output = root
    result.extraction_output_hash = canonical_extraction_output_hash(canonical_output=root)
    result.fragment_route_capability = FragmentRouteCapability(
        extraction_output_hash=result.extraction_output_hash,
        input_spec_hash=result.input_spec.input_spec_hash,
        fragments=tuple(result.fragments),
    )
    return result


# ==================== 1. generate_wiki_page summary frontmatter ====================


class TestGenerateWikiPage:
    def test_includes_summary_from_title(self):
        from core.hephaestus.distillation_engine import (
            generate_wiki_page,
            KnowledgeFragment,
        )

        frag = KnowledgeFragment(
            form="decision",
            title="Redis Cluster 选举机制深度解析",
            frontmatter={},
            background="",
            core_content="",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        page = generate_wiki_page(frag, "sess-123")
        assert "摘要: Redis Cluster 选举机制深度解析" in page

    def test_includes_summary_from_frontmatter(self):
        from core.hephaestus.distillation_engine import (
            generate_wiki_page,
            KnowledgeFragment,
        )

        frag = KnowledgeFragment(
            form="decision",
            title="x",
            frontmatter={"summary": "自定义摘要"},
            background="",
            core_content="",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        page = generate_wiki_page(frag, "sess-123")
        assert "摘要: 自定义摘要" in page

    def test_summary_truncated_to_150_chars(self):
        from core.hephaestus.distillation_engine import (
            generate_wiki_page,
            KnowledgeFragment,
        )

        long_title = "A" * 200
        frag = KnowledgeFragment(
            form="decision",
            title=long_title,
            frontmatter={},
            background="",
            core_content="",
            boundaries={},
            anti_patterns=[],
            related_concepts=[],
        )
        page = generate_wiki_page(frag, "sess-123")
        # summary 应为前150字符（fallback 截断策略）
        assert "摘要: " + "A" * 150 in page
        assert "摘要: " + "A" * 151 not in page


# ==================== 2. write_pages calls new linker ====================


@pytest.mark.usefixtures("canonical_material_actions")
class TestWritePagesIntegration:
    @pytest.fixture(autouse=True)
    def _patch_scorers_and_bus(self, monkeypatch):  # noqa: Vulture - pytest autouse fixture.
        """禁用耗时的 scorer 初始化和事件总线，避免测试超时。"""
        from core.hephaestus.distillation_engine import ValuePrejudgment

        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        # mock mnemos_bus.publish_event 避免导入耗时
        monkeypatch.setattr(
            "core.mnemos_bus.publish_event",
            lambda *a, **k: k.get("trace_id") or "cross-agent-test-trace",
        )

    def test_write_pages_calls_link_after_distill(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            KnowledgeFragment,
            ValuePrejudgment,
        )

        _disable_structured_contract_gate(monkeypatch)
        engine = DistillationEngine(
            wiki_base=str(tmp_path), receipt_config=_receipt_config(tmp_path)
        )

        # Mock 新 linker
        mock_linker = MagicMock()
        mock_linker.link_after_distill.return_value = [
            MagicMock(
                from_page=tmp_path / "00-Inbox" / "test.md",
                to_page=Path("/wiki/other.md"),
                reason="similar topic",
                similarity=0.82,
            ),
        ]
        monkeypatch.setattr(engine, "_kia_linker", mock_linker)

        input_spec, structured_output = _structured_output("sess-abc")
        result = DistillationResult(
            session_id="sess-abc",
            prejudgment=ValuePrejudgment.CERTAINLY_YES,
            judgment="knowledge",
            structured_output=structured_output,
            input_spec=input_spec,
            fragments=[
                KnowledgeFragment(
                    form="决策记录",
                    title="测试决策方案是什么呢",
                    frontmatter={
                        "摘要": "这是一个测试摘要，用于验证跨Agent关联功能",
                        "领域": "测试",
                    },
                    background="bg",
                    core_content="# 测试内容\n\n这是核心内容，需要超过一百字符才能通过硬校验。" * 5,
                    boundaries={},
                    anti_patterns=[],
                    related_concepts=[],
                    claim_ids=["claim-1"],
                ),
            ],
        )
        _attach_admitted_extraction_root(result)

        written = engine.write_pages(result)

        assert len(written) == 1
        assert mock_linker.link_after_distill.call_count == 1
        # 验证 frontmatter 被更新
        page_path = Path(written[0])
        text = page_path.read_text(encoding="utf-8")
        assert "跨Agent关联" in text

    def test_write_pages_skips_linker_when_no_fragments(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            ValuePrejudgment,
        )

        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        monkeypatch.setattr(
            "core.mnemos_bus.publish_event",
            lambda *a, **k: k.get("trace_id") or "cross-agent-test-trace",
        )

        engine = DistillationEngine(
            wiki_base=str(tmp_path), receipt_config=_receipt_config(tmp_path)
        )
        mock_linker = MagicMock()
        monkeypatch.setattr(engine, "_kia_linker", mock_linker)

        result = DistillationResult(
            session_id="sess-empty",
            judgment="skip",
            fragments=[],
        )
        written = engine.write_pages(result)
        assert written == []
        mock_linker.link_after_distill.assert_not_called()

    def test_kia_linker_lazily_loaded(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import DistillationEngine, ValuePrejudgment

        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        monkeypatch.setattr(
            "core.mnemos_bus.publish_event",
            lambda *a, **k: k.get("trace_id") or "cross-agent-test-trace",
        )

        engine = DistillationEngine(
            wiki_base=str(tmp_path), receipt_config=_receipt_config(tmp_path)
        )
        assert engine._kia_linker is None  # 未初始化

        # 直接 patch _get_kia_linker 的返回值
        monkeypatch.setattr(
            engine,
            "_get_kia_linker",
            lambda: MagicMock(
                link_after_distill=lambda p: [],
            ),
        )
        linker = engine._get_kia_linker()
        assert linker is not None

    def test_kia_linker_failure_is_non_blocking(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            KnowledgeFragment,
            ValuePrejudgment,
        )

        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)
        monkeypatch.setattr(
            "core.mnemos_bus.publish_event",
            lambda *a, **k: k.get("trace_id") or "cross-agent-test-trace",
        )

        _disable_structured_contract_gate(monkeypatch)
        engine = DistillationEngine(
            wiki_base=str(tmp_path), receipt_config=_receipt_config(tmp_path)
        )

        mock_linker = MagicMock()
        mock_linker.link_after_distill.side_effect = RuntimeError("vector index down")
        monkeypatch.setattr(engine, "_kia_linker", mock_linker)

        input_spec, structured_output = _structured_output("sess-err")
        result = DistillationResult(
            session_id="sess-err",
            prejudgment=ValuePrejudgment.CERTAINLY_YES,
            judgment="knowledge",
            structured_output=structured_output,
            input_spec=input_spec,
            fragments=[
                KnowledgeFragment(
                    form="决策记录",
                    title="测试决策方案是什么呢",
                    frontmatter={
                        "摘要": "这是一个测试摘要，用于验证跨Agent关联功能",
                        "领域": "测试",
                    },
                    background="bg",
                    core_content="# 测试内容\n\n这是核心内容，需要超过一百字符才能通过硬校验。" * 5,
                    boundaries={},
                    anti_patterns=[],
                    related_concepts=[],
                    claim_ids=["claim-1"],
                ),
            ],
        )
        _attach_admitted_extraction_root(result)

        written = engine.write_pages(result)
        assert len(written) == 1  # 写文件成功
        # linker 失败不应阻塞


# ==================== 3. distill_complete event ====================


@pytest.mark.usefixtures("canonical_material_actions")
class TestDistillCompleteEvent:
    @pytest.fixture(autouse=True)
    def _patch_scorers(self, monkeypatch):  # noqa: Vulture - pytest autouse fixture.
        from core.hephaestus.distillation_engine import ValuePrejudgment

        monkeypatch.setattr(ValuePrejudgment, "_get_scorer_v2", lambda self: None)

    def test_distill_complete_event_emitted(self, monkeypatch, tmp_path):
        from core.hephaestus.distillation_engine import (
            DistillationEngine,
            DistillationResult,
            KnowledgeFragment,
            ValuePrejudgment,
        )

        _disable_structured_contract_gate(monkeypatch)
        events = []

        class RecordingEventBus:
            def publish(self, event):
                events.append((event.event_type, event.source, event.payload))
                return event.trace_id or "cross-agent-test-trace"

        engine = DistillationEngine(
            wiki_base=str(tmp_path),
            receipt_config=_receipt_config(tmp_path),
            event_bus=RecordingEventBus(),
        )
        monkeypatch.setattr(engine, "_kia_linker", False)  # 禁用 linker

        input_spec, structured_output = _structured_output("sess-event")
        result = DistillationResult(
            session_id="sess-event",
            prejudgment=ValuePrejudgment.CERTAINLY_YES,
            judgment="knowledge",
            structured_output=structured_output,
            input_spec=input_spec,
            fragments=[
                KnowledgeFragment(
                    form="经验法则",
                    title="事件测试方案是什么呢",
                    frontmatter={"摘要": "这是一个测试摘要，用于验证事件发射功能", "领域": "测试"},
                    background="bg",
                    core_content="# 测试内容\n\n这是核心内容，需要超过一百字符才能通过硬校验。" * 5,
                    boundaries={},
                    anti_patterns=[],
                    related_concepts=[],
                    claim_ids=["claim-1"],
                ),
            ],
        )
        _attach_admitted_extraction_root(result)

        engine.write_pages(result)

        assert any(e[0] == "distill_complete" for e in events)
        dc_event = next(e for e in events if e[0] == "distill_complete")
        assert dc_event[2]["session_id"] == "sess-event"
        assert dc_event[2]["title"] == "事件测试方案是什么呢"
        assert "page_path" in dc_event[2]


# ==================== 4. _update_frontmatter_field ====================


class TestUpdateFrontmatterField:
    def test_updates_existing_frontmatter(self, tmp_path):
        from core.hephaestus.distillation_engine import DistillationEngine

        md = tmp_path / "test.md"
        md.write_text(
            """---
type: decision
source_agent: claude
---

# Title

Body content.
""",
            encoding="utf-8",
        )

        DistillationEngine._update_frontmatter_field(
            md,
            "cross_agent_refs",
            [{"page": "other.md", "similarity": 0.8}],
        )

        text = md.read_text(encoding="utf-8")
        assert "跨Agent关联:" in text
        assert "other.md" in text
        assert "Body content." in text  # body 保留

    def test_noop_when_no_frontmatter(self, tmp_path):
        from core.hephaestus.distillation_engine import DistillationEngine

        md = tmp_path / "no_fm.md"
        md.write_text("# No Frontmatter\n\nBody", encoding="utf-8")

        DistillationEngine._update_frontmatter_field(
            md,
            "key",
            "value",
        )

        text = md.read_text(encoding="utf-8")
        assert text == "# No Frontmatter\n\nBody"
