from __future__ import annotations

import json
import sqlite3
import zlib

import pytest

from core.sync_framework.raw_event_reader import CanonicalRawReadError, count_current_raw_turns_readonly
from core.sync_framework.raw_event_store import RawEventStore


def _append_private_turn(store: RawEventStore) -> str:
    return store.upsert_turn(
        source_agent="codex",
        session_id="delete-session",
        turn_number=7,
        user_content="private subject body must be removed",
        assistant_content="private assistant body must be removed",
        reasoning="private reasoning must be removed",
        tool_calls=[{"command": "private tool payload must be removed"}],
        tool_results=[{"result": "private tool result must be removed"}],
        attachments=[{"path": "/private/path"}],
        raw_event_refs=[{"ref": "private reference"}],
        source_files=["/private/source"],
        source_path="/private/source",
        metadata={"project": "private-project", "private": "metadata body"},
        completeness={"visible_text": "full"},
    )


def test_subject_delete_redacts_raw_bodies_blocks_reads_and_is_idempotent(tmp_path):
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path)
    try:
        revision_id = _append_private_turn(store)
        event_id = store.get_logical_event_id(revision_id)
        store.record_access(revision_id, "search", query="private subject search query")
        store.record_provenance_edge(
            source_revision_id=revision_id,
            span_start=0,
            span_end=7,
            consumer_type="observation",
            consumer_id="obs-private",
        )

        result = store.delete_subject_scope(
            request_id="delete-request-raw-1",
            scope_kind="session",
            scope_value="delete-session",
        )

        assert result["status"] == "applied"
        assert result["target_count"] == 1
        assert result["revision_count"] == 1
        assert result["access_log_deleted"] == 1
        assert result["access_log_after_count"] == 0
        assert result["consumer_access_log_verified"] is True
        assert result["pending_dependent_consumers"] == 1
        assert store.get_turn(revision_id) is None
        assert store.get_turn(event_id) is None
        assert store.get_revision_header(revision_id) is None
        assert store.list_current_headers(session_id="delete-session") == []
        assert store.find_event_id(
            source_agent="codex",
            session_id="delete-session",
            turn_number=7,
        ) is None
        assert store.get_metrics(event_id) is None

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT source_agent, session_id, source_path, metadata_json,
                       tool_calls_json, tool_results_json, attachments_json,
                       raw_event_refs_json, user_content_blob, assistant_content_blob,
                       reasoning_blob
                FROM raw_turns WHERE event_id=?
                """,
                (event_id,),
            ).fetchone()
            assert row is not None
            assert row[:3] == ("deleted", "deleted", None)
            assert json.loads(row[3]) == {
                "schema_version": "mnemos.raw_subject_deletion.v1",
                "subject_deletion_receipt": conn.execute(
                    "SELECT receipt_id FROM raw_subject_deletion_receipts WHERE event_id=?",
                    (event_id,),
                ).fetchone()[0],
            }
            assert row[4:8] == ("[]", "[]", "[]", "[]")
            assert zlib.decompress(row[8]).decode("utf-8") == ""
            assert zlib.decompress(row[9]).decode("utf-8") == ""
            assert zlib.decompress(row[10]).decode("utf-8") == ""
            snapshot = json.loads(
                zlib.decompress(
                    conn.execute(
                        "SELECT snapshot_blob FROM raw_turn_revisions WHERE revision_id=?",
                        (revision_id,),
                    ).fetchone()[0]
                ).decode("utf-8")
            )
            assert snapshot["user_content"] == ""
            assert snapshot["assistant_content"] == ""
            assert snapshot["reasoning"] == ""
            assert snapshot["tool_calls"] == []
            assert conn.execute(
                "SELECT COUNT(*) FROM raw_access_log WHERE event_id=?", (event_id,)
            ).fetchone()[0] == 0

        retry = store.delete_subject_scope(
            request_id="delete-request-raw-2",
            scope_kind="session",
            scope_value="delete-session",
        )
        assert retry["status"] == "existing"
        with pytest.raises(PermissionError, match="subject-deleted"):
            _append_private_turn(store)
    finally:
        store.close()


def test_readonly_raw_reader_fails_closed_without_subject_deletion_contract(tmp_path):
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path)
    try:
        _append_private_turn(store)
    finally:
        store.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE raw_subject_deletion_receipts")

    with pytest.raises(CanonicalRawReadError, match="subject deletion schema is missing"):
        count_current_raw_turns_readonly(db_path)


def test_subject_delete_verification_failure_rolls_back_the_entire_redaction(tmp_path):
    db_path = tmp_path / "raw_events.db"
    store = RawEventStore(db_path=db_path)
    try:
        revision_id = _append_private_turn(store)
        event_id = store.get_logical_event_id(revision_id)
        store.record_access(revision_id, "search", query="must survive rollback")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TRIGGER retain_access_oracle
                AFTER DELETE ON raw_access_log
                BEGIN
                    INSERT INTO raw_access_log (
                        event_id, access_type, query, consumer, created_at
                    ) VALUES (
                        OLD.event_id, OLD.access_type, OLD.query, OLD.consumer, OLD.created_at
                    );
                END
                """
            )

        result = store.delete_subject_scope(
            request_id="delete-request-oracle-failure",
            scope_kind="session",
            scope_value="delete-session",
        )

        assert result == {
            "status": "blocked",
            "target_count": 0,
            "error": "raw_subject_access_log_residual",
            "access_log_after_count": 1,
        }
        turn = store.get_turn(revision_id)
        assert turn is not None
        assert turn["user_content"] == "private subject body must be removed"
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM raw_subject_deletion_receipts WHERE event_id=?",
                (event_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM raw_access_log WHERE event_id=?",
                (event_id,),
            ).fetchone()[0] == 1
    finally:
        store.close()
