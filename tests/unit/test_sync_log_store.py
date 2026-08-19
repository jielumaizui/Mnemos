from __future__ import annotations

import sqlite3
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from core.sync_framework.sync_log_store import (
    SyncLogStore,
    SyncLogUnavailableError,
)
from core.sync_framework import sync_persona_signals


class _Config:
    database_dir = None


def _broken_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.close()
    return connection


@pytest.mark.parametrize(
    ("operation", "call"),
    [
        ("synced_batch", lambda store: store.synced_batch("codex", "s", [0])),
        (
            "exact_persona_turns",
            lambda store: store.exact_persona_turns("codex", "s", [0]),
        ),
        ("last_synced_turn", lambda store: store.last_synced_turn("codex", "s")),
        ("synced_turns", lambda store: store.synced_turns("codex", "s")),
        ("audit_summary", lambda store: store.audit_summary()),
        ("synced_record", lambda store: store.synced_record("codex", "s", 0)),
        ("failed_records", lambda store: store.failed_records(None, 10)),
    ],
)
def test_read_failure_is_typed_unavailable_not_semantic_empty(
    operation: str,
    call: Callable[[SyncLogStore], Any],
) -> None:
    store = SyncLogStore(_broken_connection, config=_Config())

    with pytest.raises(SyncLogUnavailableError, match=f"sync_log_unavailable:{operation}"):
        call(store)


def test_persona_signal_retry_replaces_one_turn_projection_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE user_signals (
            timestamp TEXT,
            agent TEXT,
            session_id TEXT,
            turn_number INTEGER,
            content_length INTEGER,
            has_code INTEGER,
            has_tools INTEGER,
            user_questions INTEGER
        )
        """
    )
    monkeypatch.setattr(
        sync_persona_signals,
        "_assert_write_not_frozen",
        lambda *_args, **_kwargs: None,
    )
    first = SimpleNamespace(
        user_content="first?",
        assistant_content="answer",
        turn_number=7,
    )
    second = SimpleNamespace(
        user_content="replacement with more bytes??",
        assistant_content="new answer",
        turn_number=7,
    )

    assert sync_persona_signals.record_persona_signal(
        lambda: connection,
        "codex",
        "session",
        first,
        config=_Config(),
    )
    assert sync_persona_signals.record_persona_signal(
        lambda: connection,
        "codex",
        "session",
        second,
        config=_Config(),
    )

    assert connection.execute(
        """
        SELECT COUNT(*), content_length, user_questions
        FROM user_signals
        WHERE agent='codex' AND session_id='session' AND turn_number=7
        """
    ).fetchone() == (
        1,
        len(f"{second.user_content}\n{second.assistant_content}"),
        2,
    )


def test_persona_signal_late_insert_failure_restores_previous_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE user_signals (
            timestamp TEXT,
            agent TEXT,
            session_id TEXT,
            turn_number INTEGER,
            content_length INTEGER,
            has_code INTEGER,
            has_tools INTEGER,
            user_questions INTEGER
        )
        """
    )
    connection.execute(
        """
        INSERT INTO user_signals VALUES (
            'before', 'codex', 'session', 7, 11, 0, 0, 1
        )
        """
    )
    connection.commit()
    connection.execute(
        """
        CREATE TRIGGER reject_replacement
        BEFORE INSERT ON user_signals
        BEGIN
            SELECT RAISE(ABORT, 'synthetic persona failure');
        END
        """
    )
    monkeypatch.setattr(
        sync_persona_signals,
        "_assert_write_not_frozen",
        lambda *_args, **_kwargs: None,
    )
    replacement = SimpleNamespace(
        user_content="replacement?",
        assistant_content="answer",
        turn_number=7,
    )

    assert sync_persona_signals.record_persona_signal(
        lambda: connection,
        "codex",
        "session",
        replacement,
        config=_Config(),
    ) is False
    assert connection.in_transaction is False
    assert connection.execute(
        """
        SELECT timestamp, content_length
        FROM user_signals
        WHERE agent='codex' AND session_id='session' AND turn_number=7
        """
    ).fetchone() == ("before", 11)


def test_sync_and_persona_batch_late_failure_restores_both_preimages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE sync_log (
            agent_name TEXT,
            session_id TEXT,
            turn_number INTEGER,
            content_hash TEXT,
            backend_uids TEXT,
            status TEXT,
            synced_at TEXT,
            distill_status TEXT,
            error TEXT,
            artifact_path TEXT,
            PRIMARY KEY (agent_name, session_id, turn_number)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE user_signals (
            timestamp TEXT,
            agent TEXT,
            session_id TEXT,
            turn_number INTEGER,
            content_length INTEGER,
            has_code INTEGER,
            has_tools INTEGER,
            user_questions INTEGER
        )
        """
    )
    connection.execute(
        """
        INSERT INTO sync_log VALUES (
            'codex', 'session', 7, 'before-hash', '[]', 'new',
            'before', 'pending', NULL, NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO user_signals VALUES (
            'before', 'codex', 'session', 7, 11, 0, 0, 1
        )
        """
    )
    connection.commit()
    connection.execute(
        """
        CREATE TRIGGER reject_persona_replacement
        BEFORE INSERT ON user_signals
        BEGIN
            SELECT RAISE(ABORT, 'synthetic persona failure');
        END
        """
    )
    monkeypatch.setattr(
        sync_persona_signals,
        "_assert_write_not_frozen",
        lambda *_args, **_kwargs: None,
    )
    sync_record = (
        "codex",
        "session",
        7,
        "candidate-hash",
        "[]",
        "updated",
        "candidate",
        "pending",
        None,
        None,
    )
    signal = ("candidate", "codex", "session", 7, 22, 0, 0, 2)

    assert sync_persona_signals.record_sync_and_persona_batch(
        lambda: connection,
        [sync_record],
        [signal],
        config=_Config(),
    ) is None
    assert connection.in_transaction is False
    assert connection.execute(
        """
        SELECT content_hash, status FROM sync_log
        WHERE agent_name='codex' AND session_id='session' AND turn_number=7
        """
    ).fetchone() == ("before-hash", "new")
    assert connection.execute(
        """
        SELECT timestamp, content_length FROM user_signals
        WHERE agent='codex' AND session_id='session' AND turn_number=7
        """
    ).fetchone() == ("before", 11)


def test_sync_and_persona_batch_rejects_duplicate_turn_identity_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(
        sync_persona_signals,
        "_assert_write_not_frozen",
        lambda *_args, **_kwargs: None,
    )
    record = (
        "codex",
        "session",
        7,
        "hash",
        "[]",
        "new",
        "now",
        "pending",
        None,
        None,
    )
    signal = ("now", "codex", "session", 7, 22, 0, 0, 2)

    with pytest.raises(ValueError, match="duplicate_sync_turn_identity_in_batch"):
        sync_persona_signals.record_sync_and_persona_batch(
            lambda: connection,
            [record, record],
            [signal],
            config=_Config(),
        )

    assert connection.in_transaction is False


def test_persona_repair_requires_the_exact_preexisting_sync_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE sync_log (
            agent_name TEXT,
            session_id TEXT,
            turn_number INTEGER,
            content_hash TEXT,
            backend_uids TEXT,
            status TEXT,
            synced_at TEXT,
            distill_status TEXT,
            error TEXT,
            artifact_path TEXT,
            PRIMARY KEY (agent_name, session_id, turn_number)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE user_signals (
            timestamp TEXT,
            agent TEXT,
            session_id TEXT,
            turn_number INTEGER,
            content_length INTEGER,
            has_code INTEGER,
            has_tools INTEGER,
            user_questions INTEGER
        )
        """
    )
    connection.execute(
        """
        INSERT INTO sync_log VALUES (
            'codex', 'session', 7, 'actual-hash', '[]', 'new',
            'before', 'pending', NULL, NULL
        )
        """
    )
    connection.commit()
    monkeypatch.setattr(
        sync_persona_signals,
        "_assert_write_not_frozen",
        lambda *_args, **_kwargs: None,
    )
    signal = ("candidate", "codex", "session", 7, 22, 0, 0, 2)

    assert sync_persona_signals.record_sync_and_persona_batch(
        lambda: connection,
        [],
        [signal],
        existing_sync_bindings=[
            ("codex", "session", 7, "different-hash")
        ],
        config=_Config(),
    ) is None

    assert connection.in_transaction is False
    assert connection.execute("SELECT COUNT(*) FROM user_signals").fetchone() == (0,)
    assert connection.execute(
        "SELECT content_hash, status, synced_at FROM sync_log"
    ).fetchone() == ("actual-hash", "new", "before")
