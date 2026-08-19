from argparse import Namespace
from datetime import datetime

from core.cli.commands.reflect import cmd_reflect
from core.reflection.feedback_collector import PendingFeedbackItem


def test_reflect_pending_prints_pending_feedback_items(monkeypatch, capsys):
    item = PendingFeedbackItem(
        reflection_id="ref-123",
        created_at=datetime(2026, 6, 18, 12, 0, 0),
        trigger="manual",
        insight_summary="这是一条待反馈反思摘要",
        dimensions_involved=["decision"],
        hours_ago=3.5,
    )

    class FakeReflectionEngine:
        def get_pending_feedback(self, hours_since, limit):
            assert hours_since == 24
            assert limit == 10
            return [item]

    monkeypatch.setattr(
        "core.reflection.reflection_engine.ReflectionEngine",
        FakeReflectionEngine,
    )

    cmd_reflect(Namespace(reflect_cmd="pending", hours_since=24, limit=10))

    output = capsys.readouterr().out
    assert "待反馈 Reflection: 1 条" in output
    assert "ref-123" in output
    assert "这是一条待反馈反思摘要" in output
