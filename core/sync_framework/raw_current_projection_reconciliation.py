"""Exact-plan repair for legacy Raw current/revision projection drift."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.native_raw_contract_ledger import (
    NativeRawContractLedger,
)
from core.sync_framework.native_raw_recovery_evidence import (
    _effective_projection_matches,
    _revision_projection_matches,
)
from core.sync_framework.raw_event_identity import (
    _compress_text,
    _revision_id,
    _utcnow,
)

SCHEMA_VERSION = "mnemos.raw_current_projection_reconciliation.v1"


class RawCurrentProjectionReconciliationError(RuntimeError):
    """Fail-closed condition for current/revision projection recovery."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _ProjectionAction:
    action: str
    event_id: str
    current_revision_id: str
    target_revision_id: str
    event_identity_hash: str
    action_fingerprint: str


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _value_digest(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "blob_sha256": hashlib.sha256(value).hexdigest(),
            "byte_count": len(value),
        }
    return value


def _row_projection_hash(
    columns: list[str],
    row: tuple[Any, ...],
) -> str:
    return _canonical_hash(
        {
            "columns": columns,
            "values": [_value_digest(value) for value in row],
        }
    )


def _strict_json(value: Any, expected_type: type) -> Any:
    parsed = json.loads(str(value or "null"))
    if not isinstance(parsed, expected_type):
        raise ValueError("Raw JSON projection has the wrong type")
    return parsed


def _snapshot_from_current_row(
    columns: list[str],
    row: tuple[Any, ...],
) -> dict[str, Any]:
    row_map = dict(zip(columns, row, strict=True))
    if str(row_map.get("compression") or "") != "zlib":
        raise ValueError("Raw compression contract is not zlib")
    snapshot = {
        "event_id": str(row_map["event_id"] or ""),
        "source_agent": str(row_map["source_agent"] or ""),
        "session_id": str(row_map["session_id"] or ""),
        "turn_number": int(row_map["turn_number"]),
        "model_tag": row_map["model_tag"],
        "conversation_at": row_map["conversation_at"],
        "captured_at": str(row_map["captured_at"] or ""),
        "origin": str(row_map["origin"] or ""),
        "source_path": row_map["source_path"],
        "source_files": _strict_json(
            row_map["source_files_json"],
            list,
        ),
        "content_hash": str(row_map["content_hash"] or ""),
        "full_content_hash": row_map["full_content_hash"],
        "completeness_status": str(row_map["completeness_status"] or "partial"),
        "completeness": _strict_json(
            row_map["completeness_json"],
            dict,
        ),
        "metadata": _strict_json(row_map["metadata_json"], dict),
        "tool_calls": _strict_json(row_map["tool_calls_json"], list),
        "tool_results": _strict_json(
            row_map["tool_results_json"],
            list,
        ),
        "attachments": _strict_json(
            row_map["attachments_json"],
            list,
        ),
        "raw_event_refs": _strict_json(
            row_map["raw_event_refs_json"],
            list,
        ),
        "reasoning": zlib.decompress(row_map["reasoning_blob"]).decode("utf-8"),
        "user_content": zlib.decompress(row_map["user_content_blob"]).decode("utf-8"),
        "assistant_content": zlib.decompress(row_map["assistant_content_blob"]).decode("utf-8"),
        "compression": "zlib",
        "raw_bytes": int(row_map["raw_bytes"]),
        "quality_rank": int(row_map["quality_rank"]),
        "updated_at": str(row_map["updated_at"] or ""),
    }
    if not snapshot["event_id"] or not snapshot["content_hash"]:
        raise ValueError("Raw current projection identity is incomplete")
    return snapshot


def _revision_snapshot_repairable(
    revision: tuple[Any, ...],
    *,
    event_id: str,
) -> bool:
    try:
        snapshot = json.loads(zlib.decompress(revision[3]).decode("utf-8"))
        return bool(
            isinstance(snapshot, dict)
            and str(revision[0] or "") == event_id
            and str(snapshot.get("event_id") or "") == event_id
            and snapshot.get("compression") == "zlib"
            and all(
                isinstance(snapshot.get(field), list)
                for field in (
                    "source_files",
                    "tool_calls",
                    "tool_results",
                    "attachments",
                    "raw_event_refs",
                )
            )
            and all(isinstance(snapshot.get(field), dict) for field in ("completeness", "metadata"))
            and isinstance(int(snapshot["turn_number"]), int)
            and isinstance(int(snapshot["raw_bytes"]), int)
            and isinstance(int(snapshot["quality_rank"]), int)
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        zlib.error,
    ):
        return False


def _scan(
    conn: sqlite3.Connection,
) -> tuple[dict[str, Any], list[_ProjectionAction]]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    required = {
        "raw_turns",
        "raw_turn_revisions",
        "raw_native_contract_observations",
    }
    if not required.issubset(tables):
        raise RawCurrentProjectionReconciliationError("raw_current_projection_schema_missing")
    cursor = conn.execute("SELECT * FROM raw_turns ORDER BY rowid")
    columns = [str(item[0]) for item in cursor.description]
    required_columns = {
        "event_id",
        "current_revision_id",
        "content_hash",
        "full_content_hash",
    }
    if not required_columns.issubset(columns):
        raise RawCurrentProjectionReconciliationError("raw_current_projection_schema_invalid")
    actions: list[_ProjectionAction] = []
    blocked: list[dict[str, str]] = []
    for row in cursor:
        row_map = dict(zip(columns, row, strict=True))
        event_id = str(row_map["event_id"] or "")
        current_revision_id = str(row_map["current_revision_id"] or "")
        event_identity_hash = _canonical_hash({"event_id": event_id})
        revision = conn.execute(
            """
            SELECT logical_event_id, content_hash, full_content_hash,
                   snapshot_blob
            FROM raw_turn_revisions WHERE revision_id=?
            """,
            (current_revision_id,),
        ).fetchone()
        if not event_id or revision is None:
            blocked.append(
                {
                    "event_identity_hash": event_identity_hash,
                    "reason": "current_revision_missing",
                }
            )
            continue
        if str(revision[0] or "") != event_id:
            blocked.append(
                {
                    "event_identity_hash": event_identity_hash,
                    "reason": "current_revision_cross_owner",
                }
            )
            continue
        try:
            latest = NativeRawContractLedger.latest(conn, event_id)
        except ValueError:
            blocked.append(
                {
                    "event_identity_hash": event_identity_hash,
                    "reason": "latest_contract_observation_invalid",
                }
            )
            continue
        revision_valid = bool(
            _revision_projection_matches(
                columns=columns,
                row=row,
                revision=revision,
                event_id=event_id,
            )
        )
        effective_valid = bool(
            _effective_projection_matches(
                columns=columns,
                row=row,
                revision=revision,
                latest=latest,
            )
        )
        if revision_valid and effective_valid:
            continue
        row_content_hash = str(row_map["content_hash"] or "")
        row_full_content_hash = str(row_map["full_content_hash"] or "")
        current_header_matches = bool(
            str(revision[0] or "") == event_id
            and str(revision[1] or "") == row_content_hash
            and str(revision[2] or "") == row_full_content_hash
        )
        action = ""
        target_revision_id = current_revision_id
        if current_header_matches:
            if not _revision_snapshot_repairable(
                revision,
                event_id=event_id,
            ):
                blocked.append(
                    {
                        "event_identity_hash": event_identity_hash,
                        "reason": "current_revision_snapshot_unrepairable",
                    }
                )
                continue
            action = "restore_revision"
        else:
            matching = conn.execute(
                """
                SELECT revision_id, logical_event_id, content_hash,
                       full_content_hash, snapshot_blob
                FROM raw_turn_revisions
                WHERE logical_event_id=? AND content_hash=?
                """,
                (event_id, row_content_hash),
            ).fetchone()
            if matching is not None:
                candidate = (
                    matching[1],
                    matching[2],
                    matching[3],
                    matching[4],
                )
                if _revision_projection_matches(
                    columns=columns,
                    row=row,
                    revision=candidate,
                    event_id=event_id,
                ):
                    action = "switch_existing_revision"
                    target_revision_id = str(matching[0] or "")
                else:
                    blocked.append(
                        {
                            "event_identity_hash": event_identity_hash,
                            "reason": "existing_revision_snapshot_conflict",
                        }
                    )
                    continue
            elif latest is None and row_content_hash:
                try:
                    snapshot = _snapshot_from_current_row(columns, row)
                    snapshot_blob = _compress_text(
                        json.dumps(
                            snapshot,
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                    candidate = (
                        event_id,
                        row_content_hash,
                        row_full_content_hash,
                        snapshot_blob,
                    )
                    candidate_valid = _revision_projection_matches(
                        columns=columns,
                        row=row,
                        revision=candidate,
                        event_id=event_id,
                    )
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    UnicodeError,
                    json.JSONDecodeError,
                    zlib.error,
                ):
                    candidate_valid = False
                if candidate_valid:
                    action = "append_revision"
                    target_revision_id = _revision_id(
                        event_id,
                        row_content_hash,
                    )
                else:
                    blocked.append(
                        {
                            "event_identity_hash": event_identity_hash,
                            "reason": ("unversioned_projection_unserializable"),
                        }
                    )
                    continue
            else:
                blocked.append(
                    {
                        "event_identity_hash": event_identity_hash,
                        "reason": (
                            "unversioned_projection_has_contract_observation"
                            if latest is not None
                            else "unversioned_projection_hash_missing"
                        ),
                    }
                )
                continue
        action_material = {
            "action": action,
            "current_revision_hash": _canonical_hash({"revision_id": current_revision_id}),
            "event_identity_hash": event_identity_hash,
            "row_projection_hash": _row_projection_hash(columns, row),
            "target_revision_hash": _canonical_hash({"revision_id": target_revision_id}),
        }
        actions.append(
            _ProjectionAction(
                action=action,
                event_id=event_id,
                current_revision_id=current_revision_id,
                target_revision_id=target_revision_id,
                event_identity_hash=event_identity_hash,
                action_fingerprint=_canonical_hash(action_material),
            )
        )
    actions.sort(
        key=lambda item: (
            item.event_identity_hash,
            item.action,
            item.action_fingerprint,
        )
    )
    blocked.sort(
        key=lambda item: (
            item["event_identity_hash"],
            item["reason"],
        )
    )
    restore_count = sum(
        item.action in {"restore_revision", "switch_existing_revision"} for item in actions
    )
    append_count = sum(item.action == "append_revision" for item in actions)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "ok": not blocked,
        "invalid_count": len(actions) + len(blocked),
        "restore_revision_count": restore_count,
        "append_revision_count": append_count,
        "blocked_count": len(blocked),
        "action_fingerprints": [item.action_fingerprint for item in actions],
        "blocked": blocked,
    }
    return plan, actions


def plan_current_projection_reconciliation(
    raw_db_path: Path,
) -> dict[str, Any]:
    """Return a content-free exact repair plan without mutating Raw."""
    try:
        with connect_readonly_sqlite(Path(raw_db_path)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            try:
                plan, _actions = _scan(conn)
                return plan
            finally:
                conn.rollback()
    except RawCurrentProjectionReconciliationError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise RawCurrentProjectionReconciliationError(
            "raw_current_projection_plan_failed"
        ) from None


def _append_current_projection_revision(
    conn: sqlite3.Connection,
    action: _ProjectionAction,
) -> None:
    cursor = conn.execute(
        """
        SELECT * FROM raw_turns
        WHERE event_id=? AND current_revision_id=?
        """,
        (action.event_id, action.current_revision_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise RawCurrentProjectionReconciliationError("raw_current_projection_plan_drift")
    columns = [str(item[0]) for item in cursor.description]
    data = dict(zip(columns, row, strict=True))
    snapshot = _snapshot_from_current_row(columns, row)
    content_hash = str(data.get("content_hash") or "")
    full_content_hash = str(data.get("full_content_hash") or "")
    current_owner = conn.execute(
        """
        SELECT logical_event_id
        FROM raw_turn_revisions
        WHERE revision_id=?
        """,
        (action.current_revision_id,),
    ).fetchone()
    if (
        current_owner is None
        or str(current_owner[0] or "") != action.event_id
    ):
        raise RawCurrentProjectionReconciliationError(
            "raw_current_projection_plan_drift"
        )
    if (
        not content_hash
        or action.target_revision_id != _revision_id(action.event_id, content_hash)
        or conn.execute(
            "SELECT 1 FROM raw_turn_revisions WHERE revision_id=?",
            (action.target_revision_id,),
        ).fetchone()
        is not None
    ):
        raise RawCurrentProjectionReconciliationError("raw_current_projection_revision_collision")
    revision_number = int(
        conn.execute(
            """
            SELECT COALESCE(MAX(revision_number), -1) + 1
            FROM raw_turn_revisions WHERE logical_event_id=?
            """,
            (action.event_id,),
        ).fetchone()[0]
    )
    conn.execute(
        """
        INSERT INTO raw_turn_revisions (
            revision_id, logical_event_id, revision_number,
            supersedes_revision_id, content_hash, full_content_hash,
            snapshot_blob, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action.target_revision_id,
            action.event_id,
            revision_number,
            action.current_revision_id or None,
            content_hash,
            full_content_hash or None,
            _compress_text(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
            _utcnow(),
        ),
    )
    conn.execute(
        """
        UPDATE raw_turns SET current_revision_id=?
        WHERE event_id=? AND current_revision_id=?
        """,
        (
            action.target_revision_id,
            action.event_id,
            action.current_revision_id,
        ),
    )


def _restore_projection(
    conn: sqlite3.Connection,
    action: _ProjectionAction,
) -> None:
    from core.sync_framework.native_raw_contract_ledger import (
        NativeRawContractLedger,
    )
    from core.sync_framework.raw_event_store import RawEventStore

    metric_before = conn.execute(
        """
        SELECT confidence, survival_score, retention_state,
               next_survival_recalc_at, updated_at
        FROM raw_metrics WHERE event_id=?
        """,
        (action.event_id,),
    ).fetchone()
    if action.action == "switch_existing_revision":
        updated = conn.execute(
            """
            UPDATE raw_turns SET current_revision_id=?
            WHERE event_id=? AND current_revision_id=?
            """,
            (
                action.target_revision_id,
                action.event_id,
                action.current_revision_id,
            ),
        )
        if int(updated.rowcount) != 1:
            raise RawCurrentProjectionReconciliationError("raw_current_projection_plan_drift")
    RawEventStore._restore_current_projection_from_revision(
        conn,
        logical_event_id=action.event_id,
        revision_id=action.target_revision_id,
    )
    latest = conn.execute(
        """
        SELECT observed_at FROM raw_native_contract_observations
        WHERE logical_event_id=? ORDER BY rowid DESC LIMIT 1
        """,
        (action.event_id,),
    ).fetchone()
    if latest is not None:
        NativeRawContractLedger().refresh_effective_state(
            conn,
            logical_event_id=action.event_id,
            observed_at=str(latest[0] or ""),
        )
        metric_after = conn.execute(
            """
            SELECT confidence, survival_score, retention_state,
                   next_survival_recalc_at, updated_at
            FROM raw_metrics WHERE event_id=?
            """,
            (action.event_id,),
        ).fetchone()
        if (
            metric_before is not None
            and metric_after is not None
            and metric_before[:4] == metric_after[:4]
            and metric_before[4] != metric_after[4]
        ):
            restored = conn.execute(
                "UPDATE raw_metrics SET updated_at=? WHERE event_id=?",
                (metric_before[4], action.event_id),
            )
            if int(restored.rowcount) != 1:
                raise RawCurrentProjectionReconciliationError(
                    "raw_current_projection_metric_timestamp_restore_failed"
                )


def apply_current_projection_reconciliation(
    raw_db_path: Path,
    *,
    expected_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one reviewed current-projection plan in a single transaction."""
    try:
        conn = sqlite3.connect(str(Path(raw_db_path)))
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            current_plan, actions = _scan(conn)
            if current_plan != dict(expected_plan):
                raise RawCurrentProjectionReconciliationError("raw_current_projection_plan_drift")
            if current_plan.get("ok") is not True:
                raise RawCurrentProjectionReconciliationError("raw_current_projection_plan_blocked")
            for action in actions:
                if action.action == "append_revision":
                    _append_current_projection_revision(conn, action)
                else:
                    _restore_projection(conn, action)
            after, _remaining = _scan(conn)
            if after.get("ok") is not True or int(after.get("invalid_count") or 0) != 0:
                raise RawCurrentProjectionReconciliationError(
                    "raw_current_projection_repair_incomplete"
                )
            conn.commit()
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is not None and int(checkpoint[0] or 0) != 0:
                raise RawCurrentProjectionReconciliationError(
                    "raw_current_projection_checkpoint_busy"
                )
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    except RawCurrentProjectionReconciliationError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        raise RawCurrentProjectionReconciliationError(
            "raw_current_projection_apply_failed"
        ) from None
    return {
        "schema_version": SCHEMA_VERSION,
        "repaired_count": len(actions),
        "restore_revision_count": sum(
            item.action in {"restore_revision", "switch_existing_revision"} for item in actions
        ),
        "append_revision_count": sum(item.action == "append_revision" for item in actions),
        "invalid_after_count": 0,
    }


__all__ = [
    "RawCurrentProjectionReconciliationError",
    "apply_current_projection_reconciliation",
    "plan_current_projection_reconciliation",
]
