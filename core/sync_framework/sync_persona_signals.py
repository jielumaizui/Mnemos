"""Transactional persona-signal writes for SyncEngine."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any, Callable


_SYNC_LOG_UPSERT = """
    INSERT OR REPLACE INTO sync_log
    (agent_name, session_id, turn_number, content_hash, backend_uids,
     status, synced_at, distill_status, error, artifact_path)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_PERSONA_DELETE = """
    DELETE FROM user_signals
    WHERE agent=? AND session_id=? AND turn_number=?
"""
_PERSONA_INSERT = """
    INSERT INTO user_signals
    (timestamp, agent, session_id, turn_number, content_length,
     has_code, has_tools, user_questions)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""
_COMMITTED_SYNC_STATUSES = {
    "new",
    "updated",
    "synced",
    "backfilled",
    "skipped_backend",
}


def _assert_write_not_frozen(config: Any, source_name: str, session_id: str) -> None:
    """Keep behavioral source metadata from being recreated after a freeze."""

    from core.privacy.ownership_freeze import cognitive_write_is_frozen

    if cognitive_write_is_frozen(
        config,
        agent=str(source_name),
        session_id=str(session_id),
    ):
        raise PermissionError("sync source metadata write blocked by data ownership freeze")


def record_persona_signal(
    connect: Callable[[], sqlite3.Connection],
    source_name: str,
    session_id: str,
    turn: Any,
    *,
    config: Any,
) -> bool:
    combined = f"{turn.user_content}\n{turn.assistant_content}"
    _assert_write_not_frozen(config, source_name, session_id)
    conn: sqlite3.Connection | None = None
    try:
        conn = connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            DELETE FROM user_signals
            WHERE agent=? AND session_id=? AND turn_number=?
            """,
            (source_name, session_id, turn.turn_number),
        )
        conn.execute(
            """
            INSERT INTO user_signals (
                timestamp, agent, session_id, turn_number,
                content_length, has_code, has_tools, user_questions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(),
                source_name,
                session_id,
                turn.turn_number,
                len(combined),
                1 if "```" in combined else 0,
                1 if "[TOOL_RESULT]" in combined else 0,
                combined.count("?"),
            ),
        )
        conn.commit()
        return True
    except (sqlite3.Error, OSError):
        if conn is not None:
            conn.rollback()
        logging.getLogger(__name__).warning("Sync persona signal write failed", exc_info=True)
        return False


def record_persona_signal_batch(
    connect: Callable[[], sqlite3.Connection],
    signals: list[tuple],
    *,
    config: Any,
) -> frozenset[tuple[str, str, int]]:
    """Commit one batch and return the exact turn identities that now exist."""
    if not signals:
        return frozenset()
    signal_keys = frozenset(
        (str(signal[1]), str(signal[2]), int(signal[3]))
        for signal in signals
    )
    if len(signal_keys) != len(signals):
        raise ValueError("duplicate_persona_turn_identity_in_batch")
    for signal in signals:
        # The fixed tuple contract is timestamp, agent, session, turn, ... .
        _assert_write_not_frozen(config, str(signal[1]), str(signal[2]))
    conn: sqlite3.Connection | None = None
    try:
        conn = connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            _PERSONA_DELETE,
            [(signal[1], signal[2], signal[3]) for signal in signals],
        )
        conn.executemany(_PERSONA_INSERT, signals)
        conn.commit()
        return signal_keys
    except (sqlite3.Error, OSError):
        if conn is not None:
            conn.rollback()
        logging.getLogger(__name__).warning(
            "Sync persona signal batch write failed",
            exc_info=True,
        )
        return frozenset()


def record_sync_and_persona_batch(
    connect: Callable[[], sqlite3.Connection],
    sync_records: list[tuple],
    signals: list[tuple],
    *,
    existing_sync_bindings: list[tuple[str, str, int, str]] | None = None,
    config: Any,
) -> frozenset[tuple[str, str, int]] | None:
    """Commit sync-log rows and their exact persona projections as one unit."""
    existing_sync_bindings = list(existing_sync_bindings or [])
    sync_keys = [
        (str(record[0]), str(record[1]), int(record[2]))
        for record in sync_records
    ]
    signal_keys = [
        (str(signal[1]), str(signal[2]), int(signal[3]))
        for signal in signals
    ]
    if len(sync_keys) != len(set(sync_keys)):
        raise ValueError("duplicate_sync_turn_identity_in_batch")
    if len(signal_keys) != len(set(signal_keys)):
        raise ValueError("duplicate_persona_turn_identity_in_batch")
    binding_keys = [
        (str(binding[0]), str(binding[1]), int(binding[2]))
        for binding in existing_sync_bindings
    ]
    if len(binding_keys) != len(set(binding_keys)):
        raise ValueError("duplicate_existing_sync_binding_in_batch")
    if set(binding_keys) & set(sync_keys):
        raise ValueError("existing_sync_binding_overlaps_sync_record")
    if not set(signal_keys) <= (set(sync_keys) | set(binding_keys)):
        raise ValueError("persona_turn_without_sync_record")
    for agent, session_id, _turn_number in (*sync_keys, *binding_keys):
        _assert_write_not_frozen(config, agent, session_id)
    if not sync_records and not signals and not existing_sync_bindings:
        return frozenset()

    conn: sqlite3.Connection | None = None
    try:
        conn = connect()
        if conn.in_transaction:
            raise RuntimeError("sync_persona_transaction_already_active")
        conn.execute("BEGIN IMMEDIATE")
        for agent, session_id, turn_number, content_hash in existing_sync_bindings:
            row = conn.execute(
                """
                SELECT content_hash, status
                FROM sync_log
                WHERE agent_name=? AND session_id=? AND turn_number=?
                """,
                (agent, session_id, turn_number),
            ).fetchone()
            if (
                row is None
                or str(row[0] or "") != str(content_hash)
                or str(row[1] or "") not in _COMMITTED_SYNC_STATUSES
            ):
                raise ValueError("existing_sync_binding_not_committed")
        if sync_records:
            conn.executemany(_SYNC_LOG_UPSERT, sync_records)
        if signals:
            conn.executemany(
                _PERSONA_DELETE,
                [(signal[1], signal[2], signal[3]) for signal in signals],
            )
            conn.executemany(_PERSONA_INSERT, signals)
        conn.commit()
        return frozenset(signal_keys)
    except (sqlite3.Error, OSError, ValueError):
        if conn is not None:
            conn.rollback()
        logging.getLogger(__name__).warning(
            "Sync log/persona batch write failed",
            exc_info=True,
        )
        return None
