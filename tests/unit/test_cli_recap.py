"""Tests for recap CLI."""

from argparse import Namespace

from core.cli.commands import recap


class FakeTask:
    def __init__(self, task_id="task-1", severity="high", status="pending", topic="topic"):
        self.task_id = task_id
        self.severity = severity
        self.status = status
        self.topic = topic
        self.source = "system"
        self.created_at = "2026-07-04T00:00:00"
        self.due_date = ""
        self.target_page = ""
        self.user_request = ""
        self.age_days = 0
        self.same_type_count = 0
        self.user_promised = False
        self.current_file = ""
        self.context = ""
        self.suggested_points = ""


def test_recap_list_prints_tasks(monkeypatch, capsys):
    class FakeForced:
        def list_recap_tasks(self, **kwargs):
            return [FakeTask()]

    monkeypatch.setattr(recap, "_forced", lambda: FakeForced())
    ret = recap.cmd_recap(
        Namespace(
            recap_cmd="list",
            status="pending",
            severity="",
            source="",
            limit=50,
            json=False,
        )
    )

    assert ret == 0
    assert "task-1" in capsys.readouterr().out


def test_recap_dismiss_all_delegates(monkeypatch, capsys):
    calls = []

    class FakeForced:
        def close_pending_recaps(self, status, **kwargs):
            calls.append((status, kwargs))
            return 3

    monkeypatch.setattr(recap, "_forced", lambda: FakeForced())
    ret = recap.cmd_recap(
        Namespace(
            recap_cmd="dismiss",
            task_id="",
            all=True,
            severity="high",
            source="system",
            limit=10,
            reason="historical failures audited",
            actor="test",
            json=False,
        )
    )

    assert ret == 0
    assert calls == [
        (
            "dismissed",
            {
                "severity": "high",
                "source": "system",
                "reason": "historical failures audited",
                "actor": "test",
                "limit": 10,
            },
        )
    ]
    assert "recap dismiss: 3" in capsys.readouterr().out


def test_recap_action_requires_task_or_all(capsys):
    ret = recap.cmd_recap(
        Namespace(
            recap_cmd="resolve",
            task_id="",
            all=False,
            severity="",
            source="system",
            limit=None,
            reason="",
            actor="cli",
            json=False,
        )
    )

    assert ret == 1
    assert "需要指定 task_id" in capsys.readouterr().out
