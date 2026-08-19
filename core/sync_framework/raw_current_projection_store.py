"""Current-row repair operations backed by immutable Raw revisions."""

from __future__ import annotations

import json
import sqlite3
import zlib

from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_identity import (
    RawEventIdentitySchemaMigrationRequired,
    _compress_text,
    _json_dumps,
)
from core.sync_framework.raw_event_reader import _decompress_text


def restore_current_projection_from_revision(
    conn: sqlite3.Connection,
    *,
    logical_event_id: str,
    revision_id: str,
) -> bool:
    """Restore one current row from its immutable canonical revision."""
    row = conn.execute(
        """
        SELECT snapshot_blob
        FROM raw_turn_revisions
        WHERE revision_id=? AND logical_event_id=?
        """,
        (revision_id, logical_event_id),
    ).fetchone()
    if row is None:
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_repair_required"
        )
    try:
        snapshot = json.loads(_decompress_text(row[0]) or "{}")
    except (TypeError, UnicodeError, json.JSONDecodeError, zlib.error):
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_repair_required"
        ) from None
    sequence_fields = (
        "source_files",
        "tool_calls",
        "tool_results",
        "attachments",
        "raw_event_refs",
    )
    mapping_fields = ("completeness", "metadata")
    if (
        not isinstance(snapshot, dict)
        or str(snapshot.get("event_id") or "") != logical_event_id
        or snapshot.get("compression") != "zlib"
        or any(
            not isinstance(snapshot.get(field), list)
            for field in sequence_fields
        )
        or any(
            not isinstance(snapshot.get(field), dict)
            for field in mapping_fields
        )
    ):
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_repair_required"
        )
    try:
        turn_number = int(snapshot["turn_number"])
        raw_bytes = int(snapshot["raw_bytes"])
        quality_rank = int(snapshot["quality_rank"])
    except (KeyError, TypeError, ValueError):
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_repair_required"
        ) from None
    updated = conn.execute(
        """
        UPDATE raw_turns
        SET source_agent=?, session_id=?, turn_number=?, model_tag=?,
            conversation_at=?, captured_at=?, origin=?, source_path=?,
            source_files_json=?, content_hash=?, full_content_hash=?,
            completeness_status=?, completeness_json=?, metadata_json=?,
            tool_calls_json=?, tool_results_json=?, attachments_json=?,
            raw_event_refs_json=?, reasoning_blob=?, user_content_blob=?,
            assistant_content_blob=?, compression='zlib', raw_bytes=?,
            quality_rank=?, updated_at=?
        WHERE event_id=? AND current_revision_id=?
        """,
        (
            str(snapshot.get("source_agent") or ""),
            str(snapshot.get("session_id") or ""),
            turn_number,
            str(snapshot.get("model_tag") or ""),
            snapshot.get("conversation_at"),
            str(snapshot.get("captured_at") or ""),
            str(snapshot.get("origin") or ""),
            snapshot.get("source_path"),
            _json_dumps(snapshot["source_files"]),
            str(snapshot.get("content_hash") or ""),
            snapshot.get("full_content_hash"),
            str(snapshot.get("completeness_status") or "partial"),
            _json_dumps(snapshot["completeness"]),
            _json_dumps(snapshot["metadata"]),
            _json_dumps(snapshot["tool_calls"]),
            _json_dumps(snapshot["tool_results"]),
            _json_dumps(snapshot["attachments"]),
            _json_dumps(snapshot["raw_event_refs"]),
            _compress_text(str(snapshot.get("reasoning") or "")),
            _compress_text(str(snapshot.get("user_content") or "")),
            _compress_text(str(snapshot.get("assistant_content") or "")),
            raw_bytes,
            quality_rank,
            str(snapshot.get("updated_at") or ""),
            logical_event_id,
            revision_id,
        ),
    )
    if int(updated.rowcount) != 1:
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_repair_required"
        )
    return True


def repair_current_projection_if_invalid(
    conn: sqlite3.Connection,
    *,
    logical_event_id: str,
    revision_id: str,
) -> bool:
    """Repair only a current projection proven inconsistent with its revision."""
    from core.sync_framework.native_raw_recovery_evidence import (
        _effective_projection_matches,
        _revision_projection_matches,
    )

    cursor = conn.execute(
        "SELECT * FROM raw_turns WHERE event_id=? AND current_revision_id=?",
        (logical_event_id, revision_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_repair_required"
        )
    columns = [str(item[0]) for item in cursor.description]
    revision = conn.execute(
        """
        SELECT logical_event_id, content_hash, full_content_hash,
               snapshot_blob
        FROM raw_turn_revisions WHERE revision_id=?
        """,
        (revision_id,),
    ).fetchone()
    row_map = dict(zip(columns, row, strict=True))
    if (
        revision is None
        or str(revision[0] or "") != logical_event_id
        or str(revision[1] or "") != str(row_map["content_hash"] or "")
        or str(revision[2] or "") != str(row_map["full_content_hash"] or "")
    ):
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_reconciliation_required"
        )
    try:
        latest = NativeRawContractLedger.latest(conn, logical_event_id)
    except ValueError:
        raise RawEventIdentitySchemaMigrationRequired(
            "raw_current_revision_projection_reconciliation_required"
        ) from None
    if (
        _revision_projection_matches(
            columns=columns,
            row=row,
            revision=revision,
            event_id=logical_event_id,
        )
        and _effective_projection_matches(
            columns=columns,
            row=row,
            revision=revision,
            latest=latest,
        )
    ):
        return False
    restore_current_projection_from_revision(
        conn,
        logical_event_id=logical_event_id,
        revision_id=revision_id,
    )
    return True
