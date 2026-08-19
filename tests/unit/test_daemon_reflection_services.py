# -*- coding: utf-8 -*-
"""Tests for daemon.reflection_services."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from daemon import reflection_services


class FakeConfig:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def test_run_reflection_engine_triggers_manual_reflection():
    cfg = FakeConfig(
        {
            "reflection.enabled": True,
            "daemon.services.reflection_engine": True,
            "reflection.manual_query": "review",
        }
    )
    engine = MagicMock()
    engine.reflect_manually.return_value = SimpleNamespace(
        triggered=True,
        insight=SimpleNamespace(summary="a useful insight"),
        feedback_messages=["msg"],
    )
    info_calls = []

    with patch("core.config.get_config", return_value=cfg):
        result = reflection_services.run_reflection_engine(
            lambda: engine,
            lambda service_name, exc: None,
            log_info=lambda *args: info_calls.append(args),
        )

    assert result == {
        "triggered": True,
        "insight_summary": "a useful insight",
        "feedback_messages": ["msg"],
        "errors": 0,
    }
    engine.reflect_manually.assert_called_once_with("review")
    assert len(info_calls) == 1


def test_run_reflection_engine_respects_disabled_flag():
    cfg = FakeConfig({"reflection.enabled": False})
    get_engine = MagicMock()

    with patch("core.config.get_config", return_value=cfg):
        result = reflection_services.run_reflection_engine(
            get_engine,
            lambda service_name, exc: None,
        )

    assert result["triggered"] is False
    assert result["errors"] == 0
    get_engine.assert_not_called()


def test_run_feedback_prompt_publishes_pending_ids():
    cfg = FakeConfig(
        {
            "feedback.enabled": True,
            "daemon.services.feedback_prompt": True,
            "feedback.pending_hours": 12,
            "feedback.pending_limit": 2,
        }
    )
    engine = MagicMock()
    engine.get_pending_feedback.return_value = [SimpleNamespace(id="r1")]
    bus = MagicMock()

    with patch("core.config.get_config", return_value=cfg):
        result = reflection_services.run_feedback_prompt(
            bus,
            lambda: engine,
            lambda service_name, exc: None,
        )

    assert result == {"pending_count": 1, "prompted": True, "errors": 0}
    engine.get_pending_feedback.assert_called_once_with(hours_since=12, limit=2)
    bus.publish.assert_called_once_with(
        "feedback.prompt_due",
        payload={
            "pending_count": 1,
            "reflection_ids": ["r1"],
            "trigger": "daemon_feedback_prompt",
        },
    )


def test_run_feedback_prompt_skips_without_event_bus():
    result = reflection_services.run_feedback_prompt(
        None,
        lambda: None,
        lambda service_name, exc: None,
    )

    assert result == {"pending_count": 0, "prompted": False, "errors": 0}
