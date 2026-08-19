"""Tests for persona CLI commands."""

import json
from argparse import Namespace

from core.cli.commands.persona import (
    cmd_persona_daily_summary,
    cmd_persona_projects,
    cmd_persona_project_signals,
    cmd_persona_recent_signals,
)


def test_persona_daily_summary_prints_sources(monkeypatch, capsys):
    closed = []

    class FakeStore:
        def get_daily_summary(self, date):
            assert date == "2026-07-01"
            return {
                "session": {
                    "signal_count": 2,
                    "summary": {"task_type": {"coding": 2}},
                }
            }

        def close(self):
            closed.append(True)

    monkeypatch.setattr("core.persona.psyche.SignalStore", FakeStore)

    ret = cmd_persona_daily_summary(Namespace(date="2026-07-01", json=False))

    assert ret == 0
    out = capsys.readouterr().out
    assert "画像信号日摘要" in out
    assert "session: 2" in out
    assert "task_type" in out
    assert closed == [True]


def test_persona_daily_summary_json(monkeypatch, capsys):
    closed = []

    class FakeStore:
        def get_daily_summary(self, date):
            return {"session": {"signal_count": 1, "summary": {}}}

        def close(self):
            closed.append(True)

    monkeypatch.setattr("core.persona.psyche.SignalStore", FakeStore)

    ret = cmd_persona_daily_summary(Namespace(date="2026-07-01", json=True))

    assert ret == 0
    out = capsys.readouterr().out
    assert '"session"' in out
    assert '"signal_count": 1' in out
    assert closed == [True]


def test_persona_project_signals_json(monkeypatch, capsys):
    closed = []

    class FakeStore:
        def get_project_isolated_signals(self, project_dir, days):
            assert project_dir == "/repo/mnemos"
            assert days == 14
            return {
                "session": [{"session_id": "s1"}],
                "git": [],
                "file_system": [{"file_path": "/repo/mnemos/README.md"}],
            }

        def close(self):
            closed.append(True)

    monkeypatch.setattr("core.persona.psyche.SignalStore", FakeStore)

    ret = cmd_persona_project_signals(
        Namespace(project_dir="/repo/mnemos", days=14, json=True)
    )

    assert ret == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"] == [{"session_id": "s1"}]
    assert payload["file_system"] == [{"file_path": "/repo/mnemos/README.md"}]
    assert closed == [True]


def test_persona_projects_json(monkeypatch, capsys):
    closed = []

    class FakeStore:
        def get_signal_projects(self, days):
            assert days == 14
            return [
                {"type": "session", "identifier": "/repo/mnemos", "signal_count": 3},
                {"type": "git", "identifier": "/repo/mnemos", "signal_count": 2},
            ]

        def close(self):
            closed.append(True)

    monkeypatch.setattr("core.persona.psyche.SignalStore", FakeStore)

    ret = cmd_persona_projects(Namespace(days=14, json=True))

    assert ret == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {"type": "session", "identifier": "/repo/mnemos", "signal_count": 3},
        {"type": "git", "identifier": "/repo/mnemos", "signal_count": 2},
    ]
    assert closed == [True]


def test_persona_recent_signals_json_uses_note_and_wechat_readers(monkeypatch, capsys):
    closed = []

    class FakeStore:
        def get_recent_note_signals(self, days):
            assert days == 14
            return [{"note_uid": "note-1"}]

        def get_recent_wechat_signals(self, days):
            assert days == 14
            return [{"content_hash": "wx-1"}]

        def close(self):
            closed.append(True)

    monkeypatch.setattr("core.persona.psyche.SignalStore", FakeStore)

    ret = cmd_persona_recent_signals(Namespace(source="all", days=14, json=True))

    assert ret == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "notes": [{"note_uid": "note-1"}],
        "wechat": [{"content_hash": "wx-1"}],
    }
    assert closed == [True]
