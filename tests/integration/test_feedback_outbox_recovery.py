from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core.cognitive.feedback_event import FeedbackEventLedger


def test_pre_cog038_outbox_cannot_be_reactivated_by_legacy_api(tmp_path: Path):
    path = tmp_path / "delivery_events.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE feedback_events (
                feedback_event_id TEXT PRIMARY KEY,
                delivery_event_id TEXT NOT NULL,
                required_consumers_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            INSERT INTO feedback_events VALUES (
                'feedback-old', 'delivery-old', '["penalty"]', '{}'
            );
            """
        )
    ledger = FeedbackEventLedger(path)

    assert ledger.get_feedback("feedback-old")["delivery_event_id"] == "delivery-old"
    with pytest.raises(RuntimeError, match="legacy_feedback_event_write_retired"):
        ledger.claim_consumer("feedback-old", "penalty", stale_after_seconds=0)

    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 1
