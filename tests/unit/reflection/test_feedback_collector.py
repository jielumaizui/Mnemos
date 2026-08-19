from datetime import datetime, timedelta
import sqlite3

import pytest

from core.reflection.feedback_collector import FeedbackCollector
from core.reflection.models import (
    FeedbackType,
    InsightSnapshot,
    ReflectionRecord,
    ReflectionTrigger,
)
from core.reflection.reflection_store import ReflectionStore


def _make_record(record_id: str, created_at: datetime | None = None):
    return ReflectionRecord(
        id=record_id,
        created_at=created_at or datetime.now(),
        trigger=ReflectionTrigger.MAJOR_DECISION,
        mirror_dimensions=["decisions"],
        insight=InsightSnapshot(
            summary="洞察摘要",
            key_points=["要点"],
            dimensions_involved=["decisions"],
        ),
    )


def _seed_legacy_feedback(
    path,
    reflection_id: str,
    feedback_type: str,
    comment: str = "",
):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE reflection_records
            SET feedback_type=?, feedback_comment=?, feedback_given_at=?
            WHERE id=?
            """,
            (feedback_type, comment, datetime.now().isoformat(), reflection_id),
        )


def test_direct_reflection_feedback_writer_is_retired(tmp_path):
    store = ReflectionStore(str(tmp_path / "reflections.db"))
    collector = FeedbackCollector(store)
    store.save_record(_make_record("r1"))

    with pytest.raises(RuntimeError, match="legacy_reflection_feedback_write_retired"):
        collector.submit_feedback("r1", FeedbackType.ACCURATE, comment="非常准确")
    with pytest.raises(RuntimeError, match="legacy_reflection_feedback_write_retired"):
        store.add_feedback("r1", object())

    assert store.get_by_id("r1").user_feedback is None


def test_get_pending_feedback_reads_unfeedbacked_recent_records(tmp_path):
    path = tmp_path / "reflections.db"
    store = ReflectionStore(str(path))
    collector = FeedbackCollector(store)
    store.save_record(_make_record("r-recent", datetime.now() - timedelta(hours=1)))
    store.save_record(_make_record("r-old", datetime.now() - timedelta(hours=48)))
    store.save_record(_make_record("r-feedback", datetime.now() - timedelta(hours=1)))
    _seed_legacy_feedback(path, "r-feedback", "insightful")

    pending = collector.get_pending_feedback(hours_since=24, limit=10)
    ids = [item.reflection_id for item in pending]

    assert "r-recent" in ids
    assert "r-old" not in ids
    assert "r-feedback" not in ids


def test_historical_feedback_summary_and_history_are_quarantined(tmp_path):
    path = tmp_path / "reflections.db"
    store = ReflectionStore(str(path))
    collector = FeedbackCollector(store)
    for record_id, hours in (("r1", 3), ("r2", 2), ("r3", 1)):
        store.save_record(_make_record(record_id, datetime.now() - timedelta(hours=hours)))
    _seed_legacy_feedback(path, "r1", "accurate", "准")
    _seed_legacy_feedback(path, "r2", "inaccurate", "不准")

    summary = collector.get_feedback_summary(days=1)
    history = collector.get_feedback_history(
        limit=2,
        feedback_type=FeedbackType.ACCURATE,
    )

    assert summary["total_reflections"] == 0
    assert summary["with_feedback"] == 0
    assert summary["feedback_breakdown"] == {}
    assert summary["status"] == "legacy_feedback_quarantined_use_canonical_feedback_audit"
    assert history == []
