# -*- coding: utf-8 -*-
"""
P0-3 集成测试 — HephaestusWorker 同步蒸馏路径发射 knowledge_distilled 事件

验证：_sync_distill_and_complete() 在成功蒸馏写页后会发射 knowledge_distilled
事件并记录 Memos→Wiki 追溯链接。
"""

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def mock_publish_event(monkeypatch):
    """捕获 publish_event 调用"""
    events = []
    def _capture(event_type, agent, payload):
        events.append({"type": event_type, "agent": agent, "payload": payload})
    monkeypatch.setattr("core.mnemos_bus.publish_event", _capture)
    return events


class FakeDistillTask:
    messages = [{"role": "user", "content": "hello"}]
    meta = {"source": "test"}


def test_sync_distill_emits_knowledge_distilled(mock_publish_event, monkeypatch):
    """_sync_distill_and_complete 成功蒸馏后应发射 knowledge_distilled 事件"""
    from core.hephaestus_worker import HephaestusWorker
    from core.hephaestus.distillation_engine import (
        DistillationResult, KnowledgeFragment,
    )

    worker = HephaestusWorker()

    frag = KnowledgeFragment(
        form="note", title="测试", frontmatter={}, background="",
        core_content="内容", boundaries={}, anti_patterns=[], related_concepts=[],
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
    mock_engine.write_pages.return_value = ["/wiki/00-Inbox/sess-worker-001_note_1.md"]

    with patch("core.hephaestus.distillation_engine.HostAgentCaller") as MockCaller, \
         patch("core.hephaestus.distillation_engine.DistillationEngine") as MockEngine, \
         patch("core.hephaestus.distillation_engine._record_memos_wiki_links") as mock_link:

        MockCaller.return_value = MagicMock()
        MockEngine.return_value = mock_engine

        ok = worker._sync_distill_and_complete("sess-worker-001", FakeDistillTask())

    assert ok is True
    kg_events = [e for e in mock_publish_event if e["type"] == "knowledge_distilled"]
    assert len(kg_events) == 1
    evt = kg_events[0]
    assert evt["payload"]["session_id"] == "sess-worker-001"
    assert "/wiki/00-Inbox/sess-worker-001_note_1.md" in evt["payload"]["wiki_pages"]
    mock_link.assert_called_once()


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

    with patch("core.hephaestus.distillation_engine.HostAgentCaller") as MockCaller, \
         patch("core.hephaestus.distillation_engine.DistillationEngine") as MockEngine, \
         patch("core.hephaestus.distillation_engine._record_memos_wiki_links") as mock_link:

        MockCaller.return_value = MagicMock()
        MockEngine.return_value = mock_engine

        ok = worker._sync_distill_and_complete("sess-skip-002", FakeDistillTask())

    assert ok is True
    kg_events = [e for e in mock_publish_event if e["type"] == "knowledge_distilled"]
    assert len(kg_events) == 0
    mock_link.assert_not_called()
