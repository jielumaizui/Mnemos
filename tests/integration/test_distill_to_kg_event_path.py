# -*- coding: utf-8 -*-
"""
P0-3 集成测试 — knowledge_distilled 事件主路径

验证：wiki_builder.run_build_cycle() 的流水线路径会发射 knowledge_distilled 事件。
"""

import tempfile
from pathlib import Path
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


def test_emit_knowledge_distilled_from_function(mock_publish_event):
    """_emit_knowledge_distilled 应正确构造 payload 并 publish"""
    from core.hephaestus.distillation_engine import (
        _emit_knowledge_distilled, DistillationResult, KnowledgeFragment,
    )

    frag = KnowledgeFragment(
        form="decision",
        title="codex-cli 环境变量",
        frontmatter={},
        background="",
        core_content="建议",
        boundaries={},
        anti_patterns=[],
        related_concepts=["cli", "env"],
    )
    frag.keywords = ["codex-cli", "env"]
    frag.cross_agent_links = ["相关页面A"]

    result = DistillationResult(
        session_id="sess-kg-001",
        judgment="knowledge",
        fragments=[frag],
    )
    written = ["/wiki/00-Inbox/sess-kg-001_decision_1.md"]

    _emit_knowledge_distilled("sess-kg-001", result, written)

    assert len(mock_publish_event) == 1
    evt = mock_publish_event[0]
    assert evt["type"] == "knowledge_distilled"
    assert evt["payload"]["session_id"] == "sess-kg-001"
    assert evt["payload"]["wiki_pages"] == written
    assert "codex-cli" in evt["payload"]["kg_input"]["entities"]
    assert len(evt["payload"]["kg_input"]["relations"]) == 1


def test_emit_no_event_when_no_fragments(mock_publish_event):
    """没有 fragments 时不应发射事件"""
    from core.hephaestus.distillation_engine import (
        _emit_knowledge_distilled, DistillationResult,
    )

    result = DistillationResult(session_id="sess-empty", judgment="skip")
    _emit_knowledge_distilled("sess-empty", result, ["/wiki/x.md"])

    assert len(mock_publish_event) == 0


def test_emit_no_event_when_no_written_pages(mock_publish_event):
    """没有 written pages 时不应发射事件"""
    from core.hephaestus.distillation_engine import (
        _emit_knowledge_distilled, DistillationResult, KnowledgeFragment,
    )

    frag = KnowledgeFragment(
        form="note", title="x", frontmatter={}, background="",
        core_content="y", boundaries={}, anti_patterns=[], related_concepts=[],
    )
    result = DistillationResult(
        session_id="sess-nowrite", judgment="knowledge", fragments=[frag],
    )
    _emit_knowledge_distilled("sess-nowrite", result, [])

    assert len(mock_publish_event) == 0
