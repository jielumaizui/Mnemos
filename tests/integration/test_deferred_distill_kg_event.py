# -*- coding: utf-8 -*-
"""
P0-3 集成测试 — DeferredDistillationQueue 延迟蒸馏路径发射 knowledge_distilled 事件

验证：_distill_record() 在成功蒸馏写页后会发射 knowledge_distilled 事件。
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


class FakeDeferredRecord:
    id = 1
    session_id = "sess-deferred-001"
    content = "延迟蒸馏内容"


def test_deferred_distill_emits_knowledge_distilled(mock_publish_event):
    """_distill_record 成功蒸馏后应发射 knowledge_distilled 事件"""
    from core.hephaestus.deferred_distill import DeferredDistillationQueue
    from core.hephaestus.distillation_engine import (
        DistillationResult, KnowledgeFragment,
    )

    dd = DeferredDistillationQueue()

    frag = KnowledgeFragment(
        form="note", title="延迟", frontmatter={}, background="",
        core_content="内容", boundaries={}, anti_patterns=[], related_concepts=[],
    )
    frag.keywords = ["延迟"]
    frag.cross_agent_links = []

    result = DistillationResult(
        session_id="sess-deferred-001",
        judgment="knowledge",
        fragments=[frag],
    )

    mock_engine = MagicMock()
    mock_engine.process.return_value = result
    mock_engine.write_pages.return_value = ["/wiki/00-Inbox/sess-deferred-001_note_1.md"]

    with patch.object(dd, "_update_status") as mock_update:
        dd._distill_record(mock_engine, FakeDeferredRecord())

    assert len(mock_publish_event) == 1
    evt = mock_publish_event[0]
    assert evt["type"] == "knowledge_distilled"
    assert evt["payload"]["session_id"] == "sess-deferred-001"
    mock_update.assert_called_once_with(1, "done")


def test_deferred_distill_no_event_when_not_knowledge(mock_publish_event):
    """判定非 knowledge 时不应发射事件"""
    from core.hephaestus.deferred_distill import DeferredDistillationQueue
    from core.hephaestus.distillation_engine import DistillationResult

    dd = DeferredDistillationQueue()

    result = DistillationResult(
        session_id="sess-deferred-skip",
        judgment="skip",
        fragments=[],
    )

    mock_engine = MagicMock()
    mock_engine.process.return_value = result

    with patch.object(dd, "_update_status") as mock_update:
        dd._distill_record(mock_engine, FakeDeferredRecord())

    assert len(mock_publish_event) == 0
    mock_update.assert_called_once_with(1, "done")
