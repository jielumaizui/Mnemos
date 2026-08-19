# -*- coding: utf-8 -*-
"""Tests for daemon.adaptive_service."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from daemon import adaptive_service


def test_run_service_returns_defaults_without_adaptive_config():
    result = adaptive_service.run_service(lambda: None, lambda service_name, exc: None)

    assert result == {"rollback": 0, "applied": 0, "suggested": 0, "recorded": 0}


def test_run_service_applies_suggestions_and_logs():
    adaptive_config = MagicMock()
    adaptive_config.suggest_adjustments.return_value = {"a": 1}
    adaptive_config.apply_adjustments.return_value = {"a": 1}
    info_calls = []

    with patch("daemon.adaptive_service.collect_metrics") as collect_metrics:
        result = adaptive_service.run_service(
            lambda: adaptive_config,
            lambda service_name, exc: None,
            log_info=lambda *args: info_calls.append(args),
        )

    assert result == {"rollback": 0, "applied": 1, "suggested": 1, "recorded": 0}
    collect_metrics.assert_called_once()
    adaptive_config.check_and_rollback.assert_called_once()
    adaptive_config.refresh_metrics_from_db.assert_called_once()
    assert len(info_calls) == 1


def test_collect_metrics_records_available_sqlite_sources(tmp_path):
    mnemos_db = tmp_path / "mnemos.db"
    with sqlite3.connect(mnemos_db) as conn:
        conn.execute("CREATE TABLE governed_training_samples " "(sample_id TEXT, created_at TEXT)")
        conn.execute(
            "INSERT INTO governed_training_samples VALUES " "('sample-1', datetime('now'))"
        )
        conn.execute(
            "CREATE TABLE governed_training_sample_actions "
            "(action_id TEXT, sample_id TEXT, action_type TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO governed_training_sample_actions VALUES "
            "('action-1', 'sample-1', 'admit', datetime('now'))"
        )
        conn.execute(
            "CREATE TABLE search_sessions "
            "(created_at TEXT, clicked_path TEXT, outcome_status TEXT)"
        )
        conn.execute("INSERT INTO search_sessions VALUES (datetime('now'), '', 'no_result')")

    wiki_state_db = tmp_path / "wiki_state.db"
    with sqlite3.connect(wiki_state_db) as conn:
        conn.execute(
            "CREATE TABLE evolution_alerts " "(alert_type TEXT, resolved INTEGER, entity TEXT)"
        )
        conn.execute("INSERT INTO evolution_alerts VALUES ('version_outdated', 0, 'A')")

    adaptive_db = tmp_path / "adaptive_config.db"
    with sqlite3.connect(adaptive_db) as conn:
        conn.execute(
            "CREATE TABLE usage_metrics " "(feature TEXT, metric TEXT, ewma REAL, recorded_at TEXT)"
        )
        conn.execute(
            "INSERT INTO usage_metrics VALUES "
            "('distill', 'false_positive_rate', 0.25, datetime('now'))"
        )

    raw_db = tmp_path / "raw_events.db"
    with sqlite3.connect(raw_db) as conn:
        conn.execute("CREATE TABLE raw_turns (completeness_status TEXT)")
        conn.execute("INSERT INTO raw_turns VALUES ('partial')")
        conn.execute("INSERT INTO raw_turns VALUES ('complete')")

    actions_db = tmp_path / "distill_actions.db"
    with sqlite3.connect(actions_db) as conn:
        conn.execute("CREATE TABLE distill_action_log " "(created_at TEXT, result_status TEXT)")
        conn.execute("INSERT INTO distill_action_log VALUES (datetime('now'), 'skipped')")
        conn.execute("INSERT INTO distill_action_log VALUES (datetime('now'), 'pending_review')")

    delivery_db = tmp_path / "delivery_events.db"
    with sqlite3.connect(delivery_db) as conn:
        conn.execute("CREATE TABLE delivery_events (created_at TEXT, feedback TEXT)")
        conn.execute("INSERT INTO delivery_events VALUES (datetime('now'), 'dismiss')")

    rejected = tmp_path / "rejected_documents"
    rejected.mkdir()
    (rejected / "doc.json").write_text("{}", encoding="utf-8")

    cfg = SimpleNamespace(database_dir=tmp_path)
    adaptive_config = MagicMock()
    result = {"recorded": 0}

    with patch("core.config.get_config", return_value=cfg):
        adaptive_service.collect_metrics(adaptive_config, result)

    assert result["recorded"] == 10
    adaptive_config.record_usage.assert_any_call("scoring", "feedback_rate", 1.0)
    adaptive_config.record_usage.assert_any_call("app", "push_ignore_rate", 1.0)
    adaptive_config.record_usage.assert_any_call("search", "no_result_rate", 1.0)
    adaptive_config.record_usage.assert_any_call("knowledge_graph", "stale_page_rate", 1.0)
    adaptive_config.record_usage.assert_any_call("raw", "partial_rate", 0.5)
    adaptive_config.record_usage.assert_any_call("quality_gate", "rejection_rate", 0.5)
    adaptive_config.record_usage.assert_any_call("quality_gate", "review_rate", 0.5)
    adaptive_config.record_usage.assert_any_call("delivery", "dismiss_rate", 1.0)
    adaptive_config.record_usage.assert_any_call("document_process", "rejection_rate", 0.1)
    adaptive_config.record_usage.assert_any_call("distill", "false_positive_rate", 0.25)


def test_feedback_rate_ignores_legacy_training_queue(tmp_path):
    with sqlite3.connect(tmp_path / "mnemos.db") as conn:
        conn.execute(
            "CREATE TABLE scorer_training_queue "
            "(created_at TEXT, session_id TEXT, features_json TEXT)"
        )
        conn.execute(
            "INSERT INTO scorer_training_queue VALUES "
            "(datetime('now'), 'feedback-legacy', '{\"source\":\"push_feedback\"}')"
        )
    cfg = SimpleNamespace(database_dir=tmp_path)
    adaptive_config = MagicMock()
    result = {"recorded": 0}

    with patch("core.config.get_config", return_value=cfg):
        adaptive_service.collect_metrics(adaptive_config, result)

    assert not any(
        call.args[:2] == ("scoring", "feedback_rate")
        for call in adaptive_config.record_usage.call_args_list
    )
