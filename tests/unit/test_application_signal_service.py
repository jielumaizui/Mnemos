from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core.app.application_signal_service import ApplicationSignalService


class _Cfg:
    def __init__(self, tmp_path, *, auto_notify=False):
        self.database_dir = tmp_path / ".mnemos"
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.wiki_dir = tmp_path / "wiki"
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        self.auto_notify = auto_notify

    def get(self, key, default=None):
        values = {
            "application_signals.enabled": True,
            "application_signals.auto_notify": self.auto_notify,
            "application_signals.avoidance.enabled": True,
            "application_signals.cross_agent_divergence.enabled": True,
            "application_signals.freshness.enabled": True,
        }
        return values.get(key, default)


def _old_page():
    old = datetime.now(timezone.utc) - timedelta(days=120)
    return {"title": "Redis 架构", "updated_at": old.isoformat(), "path": "redis.md"}


def test_application_signal_service_detects_all_signal_types(tmp_path):
    service = ApplicationSignalService(config=_Cfg(tmp_path))
    avoidance_history = [
        {"query": "redis architecture", "results_shown": ["redis architecture"], "clicked_results": []},
        {"query": "redis architecture", "results_shown": ["redis architecture"], "clicked_results": []},
        {"query": "redis architecture", "results_shown": ["redis architecture"], "clicked_results": []},
        {"query": "python testing", "results_shown": ["python testing"], "clicked_results": ["python testing"]},
        {"query": "python testing", "results_shown": ["python testing"], "clicked_results": ["python testing"]},
        {"query": "python testing", "results_shown": ["python testing"], "clicked_results": ["python testing"]},
    ]
    divergence_outputs = [
        {"topic": "embedding", "agent": "codex", "output": "必须使用 BGE M3", "confidence": 0.9},
        {"topic": "embedding", "agent": "kimi", "output": "应该禁用向量模型", "confidence": 0.1},
    ]

    signals = service.detect(
        avoidance_history=avoidance_history,
        divergence_outputs=divergence_outputs,
        freshness_pages=[_old_page()],
    )
    kinds = {signal.kind for signal in signals}

    assert {"avoidance", "cross_agent_divergence", "freshness"}.issubset(kinds)


def test_application_signal_service_persists_and_respects_cooldown(tmp_path):
    service = ApplicationSignalService(config=_Cfg(tmp_path))

    first = service.run(freshness_pages=[_old_page()])
    second = service.run(freshness_pages=[_old_page()])

    assert first["persisted"] == 1
    assert second["persisted"] == 0
    assert second["cooldown_skipped"] == 1
    assert service.report_path.exists()
    with sqlite3.connect(str(service.db_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM application_signals").fetchone()[0]
    assert count == 1


def test_application_signal_service_auto_notify_enqueues_reminder(tmp_path):
    calls = []

    class Queue:
        def enqueue(self, **kwargs):
            calls.append(kwargs)
            return "reminder-id"

    service = ApplicationSignalService(config=_Cfg(tmp_path, auto_notify=True), reminder_queue=Queue())
    result = service.run(freshness_pages=[_old_page()])

    assert result["reminders_enqueued"] == 1
    assert calls[0]["issue_id"].startswith("app_signal:freshness:")
    assert "建议动作" in calls[0]["content"]


def test_signals_cli_outputs_json(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from core.cli.commands.signals import cmd_signals

    cfg = _Cfg(tmp_path)
    service = ApplicationSignalService(config=cfg)
    service.run(freshness_pages=[_old_page()])
    monkeypatch.setattr("core.app.application_signal_service.get_config", lambda: cfg)

    ret = cmd_signals(Namespace(signals_cmd="list", limit=5, json=True))
    payload = json.loads(capsys.readouterr().out)

    assert ret == 0
    assert payload[0]["kind"] == "freshness"
    assert payload[0]["evidence"]
