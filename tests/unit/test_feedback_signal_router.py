from __future__ import annotations

import sqlite3

import pytest

from core.cognitive.feedback_signal_router import FeedbackSignalRouter


def test_legacy_feedback_signal_router_is_read_only_and_does_not_create_schema(
    tmp_path,
):
    db_path = tmp_path / "feedback_signals.db"
    router = FeedbackSignalRouter(database_dir=tmp_path)

    assert router.list_signals() == []
    assert not db_path.exists()
    with pytest.raises(RuntimeError, match="legacy_feedback_signal_write_retired"):
        router.record_signal(
            source="delivery_feedback",
            subject="redis",
            action="dismiss",
        )
    assert not db_path.exists()


def test_legacy_feedback_signal_router_reads_existing_history_without_trust_fanout(
    tmp_path,
):
    db_path = tmp_path / "feedback_signals.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE feedback_signals (
                signal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL,
                subject TEXT NOT NULL,
                action TEXT NOT NULL,
                polarity TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_value TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO feedback_signals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-1",
                "2026-07-17T00:00:00+00:00",
                "delivery_feedback",
                "redis",
                "dismiss",
                "negative",
                "topic",
                "redis",
                "delivery-1",
                "feedback-1",
                '{"legacy":true}',
            ),
        )

    rows = FeedbackSignalRouter(database_dir=tmp_path).list_signals()

    assert rows == [
        {
            "signal_id": "legacy-1",
            "created_at": "2026-07-17T00:00:00+00:00",
            "source": "delivery_feedback",
            "subject": "redis",
            "action": "dismiss",
            "polarity": "negative",
            "scope_type": "topic",
            "scope_value": "redis",
            "target_ref": "delivery-1",
            "source_event_id": "feedback-1",
            "metadata": {"legacy": True},
        }
    ]
    assert not (tmp_path / "trust_decisions.db").exists()
