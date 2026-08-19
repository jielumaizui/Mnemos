# -*- coding: utf-8 -*-
"""Worker delegates cognition projection publication to the write boundary."""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_publish_event(monkeypatch):
    """捕获 publish_event 调用"""
    events = []

    def _capture(event_type, agent, payload, *, trace_id="", subject_provenance=None):
        del subject_provenance
        events.append({"type": event_type, "agent": agent, "payload": payload})
        return trace_id or "worker-path-test-trace"

    monkeypatch.setattr("core.mnemos_bus.publish_event", _capture)
    return events


class FakeDistillTask:
    messages = [{"role": "user", "content": "hello"}]
    meta = {"source": "test"}


def test_sync_distill_does_not_duplicate_write_boundary_event(
    mock_publish_event,
    monkeypatch,
):
    """The engine write boundary owns the durable cognition event."""
    from core.hephaestus_worker import HephaestusWorker
    from core.hephaestus.distillation_engine import (
        DistillationResult,
        KnowledgeFragment,
    )

    worker = HephaestusWorker()

    frag = KnowledgeFragment(
        form="note",
        title="测试",
        frontmatter={},
        background="",
        core_content="内容",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    frag.keywords = ["测试"]
    frag.cross_agent_links = []

    result = DistillationResult(
        session_id="sess-worker-001",
        judgment="knowledge",
        fragments=[frag],
    )

    mock_engine = MagicMock()
    mock_engine.process.return_value = result
    from core.pipeline_receipts import DistillationWriteReceipt

    mock_engine.write_pages_with_receipt.return_value = DistillationWriteReceipt(
        status="committed",
        terminal_reason="test page committed",
        written_pages=("/wiki/00-Inbox/sess-worker-001_note_1.md",),
        expected_count=1,
        written_count=1,
    )

    with (
        patch("core.hephaestus.distillation_engine.HttpApiHostAgentCaller") as MockCaller,
        patch("core.hephaestus.distillation_engine.DistillationEngine") as MockEngine,
        patch("core.kia.amphora.mark_terminal", return_value=True),
    ):

        MockCaller.return_value = MagicMock()
        MockEngine.return_value = mock_engine

        ok = worker._sync_distill_and_complete("sess-worker-001", FakeDistillTask())

    assert ok is True
    assert [e for e in mock_publish_event if e["type"] == "knowledge_distilled"] == []


def test_sync_skill_does_not_duplicate_write_boundary_event(mock_publish_event):
    """COG-013 skill writes use the same single durable write boundary."""
    from core.hephaestus_worker import HephaestusWorker
    from core.hephaestus.distillation_engine import DistillationResult, KnowledgeFragment
    from core.hephaestus.distillation_models import CognitionAssetCommitReceipt
    from core.pipeline_receipts import DistillationWriteReceipt

    worker = HephaestusWorker()
    fragment = KnowledgeFragment(
        form="方法论",
        title="完整认知资产与 Wiki 投影联动",
        frontmatter={},
        background="",
        core_content="认知资产已持久化。",
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
    )
    result = DistillationResult(
        session_id="sess-worker-skill",
        judgment="skill",
        fragments=[fragment],
        cognition_asset_receipt=CognitionAssetCommitReceipt(
            status="committed",
            asset_id="cogasset-worker",
            content_hash="sha256:worker",
        ),
    )
    page = "/wiki/00-Inbox/sess-worker-skill_methodology.md"
    mock_engine = MagicMock()
    mock_engine.process.return_value = result
    mock_engine.write_pages_with_receipt.return_value = DistillationWriteReceipt(
        status="committed",
        terminal_reason="skill asset and page committed",
        written_pages=(page,),
        expected_count=1,
        written_count=1,
        required_consumer_receipts=("cognition_asset:cogasset-worker:committed",),
    )

    with (
        patch("core.hephaestus.distillation_engine.HttpApiHostAgentCaller") as MockCaller,
        patch("core.hephaestus.distillation_engine.DistillationEngine") as MockEngine,
        patch("core.kia.amphora.mark_terminal", return_value=True),
    ):
        MockCaller.return_value = MagicMock()
        MockEngine.return_value = mock_engine
        ok = worker._sync_distill_and_complete("sess-worker-skill", FakeDistillTask())

    assert ok is True
    assert [event for event in mock_publish_event if event["type"] == "knowledge_distilled"] == []


def test_sync_distill_no_event_when_not_knowledge(mock_publish_event, monkeypatch):
    """判定非 knowledge 时不应发射事件"""
    from core.hephaestus_worker import HephaestusWorker
    from core.hephaestus.distillation_engine import DistillationResult

    worker = HephaestusWorker()

    result = DistillationResult(
        session_id="sess-skip-002",
        judgment="skip",
        fragments=[],
    )

    mock_engine = MagicMock()
    mock_engine.process.return_value = result
    from core.pipeline_receipts import DistillationWriteReceipt

    mock_engine.write_pages_with_receipt.return_value = DistillationWriteReceipt(
        status="intentional_skip",
        terminal_reason="test skip",
    )

    with (
        patch("core.hephaestus.distillation_engine.HttpApiHostAgentCaller") as MockCaller,
        patch("core.hephaestus.distillation_engine.DistillationEngine") as MockEngine,
        patch("core.kia.amphora.mark_terminal", return_value=True),
    ):

        MockCaller.return_value = MagicMock()
        MockEngine.return_value = mock_engine

        ok = worker._sync_distill_and_complete("sess-skip-002", FakeDistillTask())

    assert ok is True
    kg_events = [e for e in mock_publish_event if e["type"] == "knowledge_distilled"]
    assert len(kg_events) == 0
