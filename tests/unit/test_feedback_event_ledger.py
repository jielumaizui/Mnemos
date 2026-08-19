from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core.cognitive.feedback_event import FeedbackEventLedger


def _legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE feedback_events (
                feedback_event_id TEXT PRIMARY KEY,
                delivery_event_id TEXT NOT NULL,
                required_consumers_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE feedback_receipts (
                feedback_event_id TEXT NOT NULL,
                consumer TEXT NOT NULL,
                receipt_json TEXT NOT NULL,
                PRIMARY KEY(feedback_event_id, consumer)
            );
            INSERT INTO feedback_events VALUES (
                'feedback-1', 'delivery-1', '["trust"]', '{"legacy":true}'
            );
            INSERT INTO feedback_receipts VALUES (
                'feedback-1', 'trust', '{"status":"historical"}'
            );
            """
        )


def test_legacy_feedback_event_ledger_is_read_only_and_does_not_create_db(tmp_path):
    missing = tmp_path / "missing.db"
    ledger = FeedbackEventLedger(missing)

    assert not missing.exists()
    assert ledger.get_feedback("feedback-1") == {}
    with pytest.raises(RuntimeError, match="legacy_feedback_event_write_retired"):
        ledger.begin_feedback(delivery_event_id="delivery-1")
    assert not missing.exists()


def test_legacy_feedback_event_rows_remain_queryable(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    ledger = FeedbackEventLedger(path)

    event = ledger.get_feedback("feedback-1")
    receipts = ledger.list_receipts("feedback-1")

    assert event["delivery_event_id"] == "delivery-1"
    assert event["required_consumers"] == ["trust"]
    assert event["metadata"] == {"legacy": True}
    assert receipts[0]["receipt"] == {"status": "historical"}
    for method, args in (
        (ledger.claim_consumer, ("feedback-1", "trust")),
        (ledger.complete_consumer, ("feedback-1", "trust")),
        (ledger.fail_consumer, ("feedback-1", "trust")),
        (ledger.finalize, ("feedback-1",)),
    ):
        with pytest.raises(RuntimeError, match="legacy_feedback_event_write_retired"):
            method(*args)
