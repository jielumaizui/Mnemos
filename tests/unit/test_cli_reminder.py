# -*- coding: utf-8 -*-
"""Tests for reminder CLI."""

import json
import sqlite3
from argparse import Namespace

import pytest

from core.cli.commands.reminder import cmd_reminder
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.kia.dialog_reminder import DialogReminderQueue


@pytest.fixture
def wiki(tmp_path):
    base = tmp_path / ".mnemos"
    base.mkdir()
    return base


@pytest.fixture  # noqa
def monkeypatch_config(monkeypatch, wiki):
    class Cfg:
        database_dir = wiki

    monkeypatch.setattr("core.cli.commands.reminder._config_mod.get_config", lambda: Cfg())


@pytest.fixture
def queue(wiki, monkeypatch_config):  # noqa
    initialize_cognitive_state_schema(wiki / "producer_consumer_ledger.db")
    q = DialogReminderQueue(db_path=str(wiki / "dialog_reminder.db"))
    q.enqueue(
        issue_id="test:page",
        page_path="page.md",
        severity="high",
        content="test reminder",
        choices=["已更新", "仍有效"],
    )
    return q


def test_reminder_list(capsys, queue):
    args = Namespace(reminder_cmd="list", status="all")
    ret = cmd_reminder(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "test reminder" in out


def test_reminder_status_json(capsys, queue):
    args = Namespace(reminder_cmd="status", json=True)
    ret = cmd_reminder(args)
    assert ret == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["status_counts"]["pending"] == 1


@pytest.mark.usefixtures("monkeypatch_config")
def test_reminder_queue_uses_dialog_reminder_factory(monkeypatch, wiki):
    """CLI 队列入口应复用 dialog_reminder 模块级工厂。"""
    from core.cli.commands import reminder

    calls = []

    class DirectQueue:
        def __init__(self, *args, **kwargs):
            raise AssertionError("_queue should call get_dialog_reminder_queue()")

    def fake_get_dialog_reminder_queue(db_path=None):
        calls.append(db_path)
        return "factory-queue"

    monkeypatch.setattr(reminder, "DialogReminderQueue", DirectQueue)
    monkeypatch.setattr(
        reminder,
        "get_dialog_reminder_queue",
        fake_get_dialog_reminder_queue,
        raising=False,
    )

    assert reminder._queue() == "factory-queue"
    assert calls == [str(wiki / "dialog_reminder.db")]


def test_reminder_push(capsys, queue):
    args = Namespace(reminder_cmd="push", max=5)
    ret = cmd_reminder(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "已推送" in out


def test_reminder_push_uses_reminder_renderer_factory(capsys, queue, monkeypatch):
    """手动推送入口应复用 ReminderRenderer 工厂输出标准对话文本。"""
    from core.cli.commands import reminder

    calls = []

    class FactoryRenderer:
        def render_dialog(self, entry):
            calls.append(entry.reminder_id)
            return f"RENDERED_DIALOG:{entry.reminder_id}"

    def fake_get_reminder_renderer():
        calls.append("renderer_factory")
        return FactoryRenderer()

    monkeypatch.setattr(
        reminder,
        "get_reminder_renderer",
        fake_get_reminder_renderer,
        raising=False,
    )

    args = Namespace(reminder_cmd="push", max=1)
    ret = cmd_reminder(args)

    assert ret == 0
    out = capsys.readouterr().out
    assert "RENDERED_DIALOG:" in out
    assert calls[0] == "renderer_factory"


def test_reminder_resolve(capsys, queue):
    pending = queue.list_reminders(status="pending", limit=1)
    rid = pending[0].reminder_id
    args = Namespace(reminder_cmd="resolve", reminder_id=rid, choice="已处理")
    ret = cmd_reminder(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "已关闭提醒" in out


def test_reminder_resolve_by_issue(capsys, queue):
    args = Namespace(
        reminder_cmd="resolve",
        reminder_id=None,
        issue="test:page",
        choice="已处理",
    )
    ret = cmd_reminder(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "已关闭 issue 提醒" in out
    assert not queue.get_by_issue("test:page")


def test_reminder_dismiss_by_issue(capsys, queue):
    args = Namespace(
        reminder_cmd="dismiss",
        reminder_id=None,
        issue="test:page",
        reason="not relevant",
    )
    ret = cmd_reminder(args)
    assert ret == 0
    out = capsys.readouterr().out
    assert "已忽略提醒" in out
    dismissed = queue.list_reminders(status="dismissed", limit=1)
    assert dismissed[0].resolved_choice == "not relevant"


def test_reminder_expire_stale(capsys, queue, wiki):
    with sqlite3.connect(str(wiki / "dialog_reminder.db")) as conn:
        conn.execute(
            "UPDATE dialog_reminders SET created_at = ?",
            ("2000-01-01T00:00:00",),
        )

    args = Namespace(
        reminder_cmd="expire-stale",
        days=30,
        limit=None,
        severity="",
        json=True,
    )
    ret = cmd_reminder(args)

    assert ret == 0
    result = json.loads(capsys.readouterr().out)
    assert result["expired"] == 1
    assert queue.list_reminders(status="expired", limit=1)


def test_reminder_resolve_rejects_id_and_issue(capsys, queue):
    pending = queue.list_reminders(status="pending", limit=1)
    args = Namespace(
        reminder_cmd="resolve",
        reminder_id=pending[0].reminder_id,
        issue="test:page",
        choice="已处理",
    )
    ret = cmd_reminder(args)
    assert ret == 1
    out = capsys.readouterr().out
    assert "只能二选一" in out
