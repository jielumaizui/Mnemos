#!/usr/bin/env python3
"""Migrate the AgentSource cursor ledger to disposition-bound schema v4.

The v1 cursor lacks Raw receipt evidence; v2 lacks the NativeSourceSnapshot
binding; v3 lacks explicit parsed/empty/excluded disposition evidence.
Migration never fabricates any of those proofs.  After apply, run controlled
Raw-only reconciliation to rebuild each source generation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Config
from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive as _shared_runtime_is_inactive,
)
from core.ops.durable_io import (
    DurableIOError,
    fsync_directory,
    fsync_regular_file,
    inspect_path_kind,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    private_sqlite_sidecars,
    regular_file_sha256,
    validate_private_sqlite_copy,
)
from core.ops.durable_io import read_native_bytes
from core.ops.offline_migration_lock import offline_migration_lock
from core.ops.readiness_query_budget import connect_readonly_sqlite
from daemon.agent_sync_cursor import (
    CURSOR_FILE_NAME,
    CURSOR_SCHEMA_VERSION,
    FINGERPRINTLESS_CURSOR_SCHEMA_VERSION,
    LEGACY_CURSOR_SCHEMA_VERSION,
    PREVIOUS_CURSOR_SCHEMA_VERSION,
    SNAPSHOTLESS_CURSOR_SCHEMA_VERSION,
    AgentSyncCursorError,
    _validate_cursor_schema_v5,
    migrate_historical_cursor_schema,
)

SCHEMA_VERSION = "mnemos.agent_sync_cursor_reconciliation.v3"
PLAN_VERSION = "mnemos.agent_sync_cursor_migration_plan.v3"
RECEIPT_VERSION = "mnemos.agent_sync_cursor_migration_receipt.v3"
_SOURCE_TABLE_QUERIES: dict[str, str] = {
    "session_raw_cursors": 'SELECT * FROM "session_raw_cursors" ORDER BY rowid',
    "source_reconciliation_cursors": (
        'SELECT * FROM "source_reconciliation_cursors" ORDER BY rowid'
    ),
    "source_denominator_state": (
        'SELECT * FROM "source_denominator_state" ORDER BY rowid'
    ),
    "source_denominator_sessions": (
        'SELECT * FROM "source_denominator_sessions" ORDER BY rowid'
    ),
    "source_capture_generations": (
        'SELECT * FROM "source_capture_generations" ORDER BY rowid'
    ),
    "source_capture_expected_turns": (
        'SELECT * FROM "source_capture_expected_turns" ORDER BY rowid'
    ),
    "source_capture_raw_receipts": (
        'SELECT * FROM "source_capture_raw_receipts" ORDER BY rowid'
    ),
}
_SOURCE_TABLE_PROJECTION_QUERIES: dict[
    str,
    dict[tuple[str, ...], str],
] = {
    "session_raw_cursors": {
        (
            "source_name",
            "canonical_session_id",
            "next_turn_number",
            "last_raw_commit_at",
        ): (
            'SELECT "source_name", "canonical_session_id", '
            '"next_turn_number", "last_raw_commit_at" '
            'FROM "session_raw_cursors" ORDER BY rowid'
        ),
    },
    "source_reconciliation_cursors": {
        (
            "source_name",
            "after_canonical_session_id",
            "updated_at",
        ): (
            'SELECT "source_name", "after_canonical_session_id", "updated_at" '
            'FROM "source_reconciliation_cursors" ORDER BY rowid'
        ),
    },
    "source_denominator_state": {
        (
            "source_name",
            "roster_hash",
            "session_count",
            "observed_session_count",
            "observed_turn_count",
            "complete",
            "completed_at",
            "updated_at",
        ): (
            'SELECT "source_name", "roster_hash", "session_count", '
            '"observed_session_count", "observed_turn_count", "complete", '
            '"completed_at", "updated_at" '
            'FROM "source_denominator_state" ORDER BY rowid'
        ),
    },
    "source_denominator_sessions": {
        (
            "source_name",
            "canonical_session_id",
            "roster_hash",
            "turn_count",
            "observed_at",
        ): (
            'SELECT "source_name", "canonical_session_id", "roster_hash", '
            '"turn_count", "observed_at" '
            'FROM "source_denominator_sessions" ORDER BY rowid'
        ),
    },
    "source_capture_generations": {
        (
            "source_name",
            "generation_id",
            "roster_hash",
            "started_at",
        ): (
            'SELECT "source_name", "generation_id", "roster_hash", "started_at" '
            'FROM "source_capture_generations" ORDER BY rowid'
        ),
        (
            "source_name",
            "generation_id",
            "roster_hash",
            "native_source_snapshot_hash",
            "snapshot_binding_eligible",
            "started_at",
        ): (
            'SELECT "source_name", "generation_id", "roster_hash", '
            '"native_source_snapshot_hash", "snapshot_binding_eligible", '
            '"started_at" FROM "source_capture_generations" ORDER BY rowid'
        ),
        (
            "source_name",
            "generation_id",
            "roster_hash",
            "started_at",
            "native_source_snapshot_hash",
        ): (
            'SELECT "source_name", "generation_id", "roster_hash", '
            '"started_at", "native_source_snapshot_hash" '
            'FROM "source_capture_generations" ORDER BY rowid'
        ),
        (
            "source_name",
            "generation_id",
            "roster_hash",
            "native_source_snapshot_hash",
            "started_at",
        ): (
            'SELECT "source_name", "generation_id", "roster_hash", '
            '"native_source_snapshot_hash", "started_at" '
            'FROM "source_capture_generations" ORDER BY rowid'
        ),
    },
    "source_capture_expected_turns": {
        (
            "source_name",
            "generation_id",
            "canonical_session_id",
            "turn_number",
            "observed_at",
        ): (
            'SELECT "source_name", "generation_id", "canonical_session_id", '
            '"turn_number", "observed_at" '
            'FROM "source_capture_expected_turns" ORDER BY rowid'
        ),
    },
    "source_capture_raw_receipts": {
        (
            "source_name",
            "generation_id",
            "canonical_session_id",
            "turn_number",
            "raw_revision_id",
            "recorded_at",
        ): (
            'SELECT "source_name", "generation_id", "canonical_session_id", '
            '"turn_number", "raw_revision_id", "recorded_at" '
            'FROM "source_capture_raw_receipts" ORDER BY rowid'
        ),
    },
}


class CursorSchemaReconciliationError(RuntimeError):
    """Fail-closed condition for v1-v4 to v5 cursor evidence migration."""


def _sha256(path: Path) -> str:
    return regular_file_sha256(path)


def _canonical_sqlite_path(path: Path) -> Path:
    candidate = Path(path).expanduser()
    try:
        return candidate.parent.resolve(strict=True) / candidate.name
    except OSError:
        raise CursorSchemaReconciliationError(
            "cursor_database_parent_unavailable"
        ) from None


def _sqlite_snapshot_sha256(path: Path, *, immutable: bool = False) -> str:
    """Hash one logical SQLite snapshot, including committed WAL state."""
    try:
        source = connect_readonly_sqlite(path, immutable=immutable)
        try:
            source.execute("BEGIN")
            digest = hashlib.sha256()
            for statement in source.iterdump():
                digest.update(statement.encode("utf-8"))
                digest.update(b"\n")
            return digest.hexdigest()
        finally:
            source.close()
    except (OSError, sqlite3.Error):
        raise CursorSchemaReconciliationError("cursor_snapshot_hash_failed") from None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _create_private_sqlite_target(path: Path) -> None:
    """Create an empty SQLite target privately before any sensitive write."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _read_schema_version(path: Path) -> str:
    try:
        with connect_readonly_sqlite(path) as conn:
            row = conn.execute(
                "SELECT value FROM cursor_schema WHERE key='schema_version'"
            ).fetchone()
    except (OSError, sqlite3.Error):
        return ""
    return str(row[0] or "") if row else ""


def _integrity(path: Path, *, immutable: bool = False) -> str:
    try:
        with connect_readonly_sqlite(path, immutable=immutable) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except (OSError, sqlite3.Error):
        return "unreadable"
    return str(row[0] or "") if row else "unreadable"


def _foreign_key_errors(path: Path, *, immutable: bool = False) -> list[list[Any]]:
    try:
        with connect_readonly_sqlite(path, immutable=immutable) as conn:
            return [list(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
    except (OSError, sqlite3.Error):
        return [["unreadable"]]


def _snapshot_binding_eligible_count(path: Path) -> int:
    """Count pre-v5 generations whose snapshot proof must be invalidated."""

    try:
        with connect_readonly_sqlite(path) as conn:
            columns = {
                str(row[1])
                for row in conn.execute(
                    'PRAGMA table_info("source_capture_generations")'
                ).fetchall()
            }
            if "snapshot_binding_eligible" not in columns:
                return 0
            row = conn.execute(
                """
                SELECT COUNT(*) FROM source_capture_generations
                WHERE snapshot_binding_eligible != 0
                """
            ).fetchone()
            return int(row[0] if row is not None else 0)
    except (OSError, sqlite3.Error):
        raise CursorSchemaReconciliationError(
            "snapshot_binding_invalidation_count_failed"
        ) from None


def _migration_plan(
    *,
    cursor_path: Path,
    backup_dir: Path | None,
    before_schema_version: str,
    before_integrity: str,
    foreign_key_errors: list[list[Any]],
    daemon_inactive: bool,
) -> dict[str, Any]:
    resolved = _canonical_sqlite_path(cursor_path)
    plan_material: dict[str, Any] = {
        "plan_version": PLAN_VERSION,
        "root_id": "COG-045",
        "substate": "RM-SCHEMA",
        "apply_scope": {
            "cursor_db": str(resolved),
            "source_snapshot_sha256": f"sha256:{_sqlite_snapshot_sha256(resolved)}",
            "backup_dir": (
                str(Path(backup_dir).expanduser().resolve(strict=False))
                if backup_dir is not None
                else ""
            ),
        },
        "before_schema_version": before_schema_version,
        "target_schema_version": CURSOR_SCHEMA_VERSION,
        "before_integrity": before_integrity,
        "foreign_key_errors": foreign_key_errors,
        "writer_lock_state": "inactive" if daemon_inactive else "active_or_unverified",
        "allowed_delta": {
            "schema_version": {
                "from": before_schema_version,
                "to": CURSOR_SCHEMA_VERSION,
            },
            "historical_snapshot_bindings_created": 0,
            "historical_snapshot_bindings_invalidated": (
                _snapshot_binding_eligible_count(resolved)
            ),
            "requires_raw_rebuild": True,
        },
        "ambiguous": [],
        "unresolved": [],
        "apply_eligible": bool(daemon_inactive and backup_dir is not None),
    }
    return {**plan_material, "plan_hash": _canonical_hash(plan_material)}


def _backup_sqlite(path: Path, backup_dir: Path) -> Mapping[str, Any]:
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    target = backup_dir / (
        "pre-agent-sync-cursor-schema."
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}."
        f"{uuid.uuid4().hex[:12]}.sqlite"
    )
    created = False
    try:
        try:
            _create_private_sqlite_target(target)
        except FileExistsError:
            raise
        except BaseException:
            created = True
            raise
        created = True
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(path),
            lambda: sqlite3.connect(str(target)),
        ) as (source, destination):
            source.backup(destination)
            row = destination.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]) != "ok":
                raise CursorSchemaReconciliationError("backup_integrity_check_failed")
            if destination.execute("PRAGMA foreign_key_check").fetchall():
                raise CursorSchemaReconciliationError("backup_foreign_key_check_failed")
        normalize_private_sqlite_copy(target)
        if (
            _integrity(target, immutable=True) != "ok"
            or _foreign_key_errors(target, immutable=True)
        ):
            raise CursorSchemaReconciliationError("backup_integrity_check_failed")
        os.chmod(target, 0o600)
        return {
            "filename": target.name,
            "integrity": "ok",
            "foreign_key_errors": [],
            "sha256": _sha256(target),
        }
    except CursorSchemaReconciliationError:
        if created:
            for candidate in (*private_sqlite_sidecars(target), target):
                candidate.unlink(missing_ok=True)
        raise
    except (DurableIOError, OSError, sqlite3.Error):
        if created:
            for candidate in (*private_sqlite_sidecars(target), target):
                candidate.unlink(missing_ok=True)
        raise CursorSchemaReconciliationError("cursor_backup_failed") from None


def _restore_sqlite_backup(
    *,
    backup_path: Path,
    cursor_path: Path,
    expected_snapshot_hash: str,
) -> None:
    """Atomically restore the exact cursor database through SQLite backup."""
    temporary = cursor_path.with_name(f".{cursor_path.name}.{uuid.uuid4().hex}.restore")
    temporary_created = False
    try:
        if not _restore_drill(backup_path, expected_snapshot_hash):
            raise CursorSchemaReconciliationError("rollback_backup_invalid")
        try:
            _create_private_sqlite_target(temporary)
        except FileExistsError:
            raise
        except BaseException:
            temporary_created = True
            raise
        temporary_created = True
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(backup_path, immutable=True),
            lambda: sqlite3.connect(str(temporary)),
        ) as (source, destination):
            source.backup(destination)
        normalize_private_sqlite_copy(temporary)
        if (
            _integrity(temporary, immutable=True) != "ok"
            or _foreign_key_errors(temporary, immutable=True)
            or f"sha256:{_sqlite_snapshot_sha256(temporary, immutable=True)}"
            != expected_snapshot_hash
        ):
            raise CursorSchemaReconciliationError("rollback_restore_invalid")
        os.chmod(temporary, 0o600)
        os.replace(temporary, cursor_path)
        for sidecar in private_sqlite_sidecars(cursor_path):
            sidecar.unlink(missing_ok=True)
        fsync_regular_file(cursor_path)
        fsync_directory(cursor_path.parent)
        if (
            _integrity(cursor_path) != "ok"
            or _foreign_key_errors(cursor_path)
            or f"sha256:{_sqlite_snapshot_sha256(cursor_path)}" != expected_snapshot_hash
        ):
            raise CursorSchemaReconciliationError("rollback_restore_invalid")
    except CursorSchemaReconciliationError:
        raise
    except (DurableIOError, OSError, sqlite3.Error):
        raise CursorSchemaReconciliationError("rollback_failed") from None
    finally:
        if temporary_created:
            for candidate in (*private_sqlite_sidecars(temporary), temporary):
                candidate.unlink(missing_ok=True)


def _runtime_writers_are_inactive(database_dir: Path) -> bool:
    return bool(_shared_runtime_is_inactive(Path(database_dir)))


def _source_table_evidence(
    path: Path,
    *,
    table_contract: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, dict[str, Any]]:
    candidates = tuple(_SOURCE_TABLE_QUERIES)
    evidence: dict[str, dict[str, Any]] = {}
    try:
        with connect_readonly_sqlite(path) as conn:
            if table_contract is None:
                present = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                tables = tuple(table for table in candidates if table in present)
            else:
                tables = tuple(table_contract)
                if not set(tables) <= set(candidates):
                    raise CursorSchemaReconciliationError(
                        "legacy_table_comparator_failed"
                    )
            for table in tables:
                expected_columns = (
                    tuple(table_contract[table])
                    if table_contract is not None
                    else None
                )
                if table_contract is None and table == "source_capture_generations":
                    actual_columns = tuple(
                        str(row[1])
                        for row in conn.execute(
                            'PRAGMA table_info("source_capture_generations")'
                        ).fetchall()
                    )
                    if "snapshot_binding_eligible" in actual_columns:
                        expected_columns = tuple(
                            column
                            for column in actual_columns
                            if column
                            not in {
                                "native_source_snapshot_hash",
                                "snapshot_binding_eligible",
                            }
                        )
                query: str | None
                if expected_columns is None:
                    query = _SOURCE_TABLE_QUERIES[table]
                else:
                    projection_queries = (
                        _SOURCE_TABLE_PROJECTION_QUERIES.get(table)
                    )
                    query = (
                        projection_queries.get(expected_columns)
                        if projection_queries is not None
                        else None
                    )
                if query is None:
                    raise CursorSchemaReconciliationError(
                        "legacy_table_comparator_failed"
                    )
                cursor = conn.execute(query)
                columns = tuple(str(item[0]) for item in cursor.description or ())
                if expected_columns is not None and columns != expected_columns:
                    raise CursorSchemaReconciliationError(
                        "legacy_table_comparator_failed"
                    )
                rows = cursor.fetchall()
                encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                evidence[table] = {
                    "columns": list(columns),
                    "row_count": len(rows),
                    "content_hash": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
                }
    except (OSError, sqlite3.Error, TypeError):
        raise CursorSchemaReconciliationError("legacy_table_comparator_failed") from None
    return evidence


def _receipt_path(backup_dir: Path, plan_hash: str) -> Path:
    suffix = plan_hash.removeprefix("sha256:")
    return Path(backup_dir).expanduser().resolve(strict=False) / (
        f"agent-sync-cursor-migration.{suffix}.json"
    )


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary_created = False
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            temporary_created = True
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _restore_drill(backup_path: Path, expected_snapshot_hash: str) -> bool:
    temporary = backup_path.with_name(f".{backup_path.name}.{uuid.uuid4().hex}.restore-drill")
    temporary_created = False
    try:
        validate_private_sqlite_copy(backup_path)
        if (
            _integrity(backup_path, immutable=True) != "ok"
            or _foreign_key_errors(backup_path, immutable=True)
            or f"sha256:{_sqlite_snapshot_sha256(backup_path, immutable=True)}"
            != expected_snapshot_hash
        ):
            return False
        try:
            _create_private_sqlite_target(temporary)
        except FileExistsError:
            raise
        except BaseException:
            temporary_created = True
            raise
        temporary_created = True
        with owned_sqlite_connection_pair(
            lambda: connect_readonly_sqlite(backup_path, immutable=True),
            lambda: sqlite3.connect(str(temporary)),
        ) as (source, destination):
            source.backup(destination)
        normalize_private_sqlite_copy(temporary)
        return bool(
            _integrity(temporary, immutable=True) == "ok"
            and not _foreign_key_errors(temporary, immutable=True)
            and f"sha256:{_sqlite_snapshot_sha256(temporary, immutable=True)}"
            == expected_snapshot_hash
        )
    except (
        DurableIOError,
        OSError,
        sqlite3.Error,
        CursorSchemaReconciliationError,
    ):
        return False
    finally:
        if temporary_created:
            for candidate in (*private_sqlite_sidecars(temporary), temporary):
                candidate.unlink(missing_ok=True)


def _verify_same_plan_receipt(
    *,
    cursor_path: Path,
    backup_dir: Path,
    expected_plan_hash: str,
) -> dict[str, Any] | None:
    receipt_path = _receipt_path(backup_dir, expected_plan_hash)
    try:
        receipt_kind = inspect_path_kind(receipt_path)
    except DurableIOError:
        raise CursorSchemaReconciliationError("migration_receipt_unreadable") from None
    if receipt_kind == "missing":
        return None
    if receipt_kind != "file":
        raise CursorSchemaReconciliationError(
            "migration_receipt_permissions_invalid"
        )
    try:
        receipt = json.loads(read_native_bytes(receipt_path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CursorSchemaReconciliationError("migration_receipt_unreadable") from None
    if (
        receipt.get("schema_version") != RECEIPT_VERSION
        or receipt.get("plan_hash") != expected_plan_hash
        or receipt.get("cursor_db") != str(cursor_path.resolve())
        or receipt.get("backup_dir") != str(backup_dir.resolve())
        or receipt.get("status") != "completed"
        or receipt.get("restore_drill_ok") is not True
        or receipt.get("required_gap") != 0
        or receipt.get("first_apply_comparator", {}).get("ok") is not True
        or receipt.get("post_apply_dry_run", {}).get("required_gap") != 0
        or not isinstance(receipt.get("backup"), Mapping)
    ):
        raise CursorSchemaReconciliationError("migration_receipt_binding_mismatch")
    comparator = receipt["first_apply_comparator"]
    backup = receipt["backup"]
    backup_path = backup_dir / str(backup.get("filename") or "")
    pre_snapshot_hash = str(receipt.get("pre_snapshot_hash") or "")
    try:
        validate_private_sqlite_copy(backup_path)
    except DurableIOError:
        raise CursorSchemaReconciliationError(
            "migration_receipt_evidence_invalid"
        ) from None
    backup_tables = _source_table_evidence(backup_path) if backup_path.is_file() else {}
    invalidated_count = receipt.get("historical_snapshot_bindings_invalidated")
    if (
        comparator.get("before") != comparator.get("after")
        or not comparator.get("before")
        or comparator.get("before") != backup_tables
        or not isinstance(invalidated_count, int)
        or isinstance(invalidated_count, bool)
        or invalidated_count < 0
        or (
            backup_path.is_file()
            and invalidated_count
            != _snapshot_binding_eligible_count(backup_path)
        )
        or _snapshot_binding_eligible_count(cursor_path) != 0
        or not backup_path.is_file()
        or str(backup.get("sha256") or "") != _sha256(backup_path)
        or not _restore_drill(backup_path, pre_snapshot_hash)
    ):
        raise CursorSchemaReconciliationError("migration_receipt_evidence_invalid")
    if (
        _read_schema_version(cursor_path) != CURSOR_SCHEMA_VERSION
        or _integrity(cursor_path) != "ok"
        or _foreign_key_errors(cursor_path)
        or f"sha256:{_sqlite_snapshot_sha256(cursor_path)}" != receipt.get("post_snapshot_hash")
    ):
        raise CursorSchemaReconciliationError("same_plan_post_state_drift")
    if _post_schema_dry_run(cursor_path) != receipt.get("post_apply_dry_run"):
        raise CursorSchemaReconciliationError("migration_receipt_post_gap_drift")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "same_plan_second_apply",
        "ok": True,
        "reviewed_plan_hash": expected_plan_hash,
        "physical_delta": 0,
        "semantic_delta": 0,
        "required_gap": 0,
        "receipt_filename": receipt_path.name,
    }


def _post_schema_dry_run(cursor_path: Path) -> dict[str, Any]:
    try:
        with connect_readonly_sqlite(cursor_path) as conn:
            _validate_cursor_schema_v5(conn)
    except (OSError, sqlite3.Error, AgentSyncCursorError):
        raise CursorSchemaReconciliationError("post_migration_schema_invalid") from None
    gaps = {
        "schema": int(_read_schema_version(cursor_path) != CURSOR_SCHEMA_VERSION),
        "integrity": int(_integrity(cursor_path) != "ok"),
        "foreign_key": len(_foreign_key_errors(cursor_path)),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "ok": sum(gaps.values()) == 0,
        "before_schema_version": CURSOR_SCHEMA_VERSION,
        "target_schema_version": CURSOR_SCHEMA_VERSION,
        "apply_required": False,
        "apply_eligible": False,
        "gaps": gaps,
        "required_gap": sum(gaps.values()),
        "unresolved": [],
    }


def _execute_unresolved_cursor_schema_for_test(
    *,
    cursor_path: Path,
    backup_dir: Path | None,
    apply: bool,
    daemon_inactive: bool,
    expected_plan_hash: str = "",
) -> dict[str, Any]:
    """Preview or execute a verified, backup-first v1-v4 to v5 migration."""
    cursor_path = Path(cursor_path).expanduser()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry_run",
        "cursor_file_present": cursor_path.is_file(),
        "before_schema_version": _read_schema_version(cursor_path) if cursor_path.is_file() else "",
        "before_integrity": _integrity(cursor_path) if cursor_path.is_file() else "not_applicable",
        "requires_rebuild": True,
        "rebuild_command": (
            "scripts/reconcile_agent_source_raw_capture.py --apply "
            "--confirm-read-native-history --expected-plan-hash <sha256:...> "
            "--backup-dir <dir> --json"
        ),
    }
    if not cursor_path.is_file():
        result.update({"ok": False, "error": "cursor_database_missing"})
        return result
    if result["before_integrity"] != "ok":
        result.update({"ok": False, "error": "cursor_database_integrity_failed"})
        return result
    foreign_key_errors = _foreign_key_errors(cursor_path)
    result["before_foreign_key_errors"] = foreign_key_errors
    if foreign_key_errors:
        result.update({"ok": False, "error": "cursor_database_foreign_key_check_failed"})
        return result
    if result["before_schema_version"] not in {
        LEGACY_CURSOR_SCHEMA_VERSION,
        SNAPSHOTLESS_CURSOR_SCHEMA_VERSION,
        PREVIOUS_CURSOR_SCHEMA_VERSION,
        FINGERPRINTLESS_CURSOR_SCHEMA_VERSION,
    }:
        result.update({"ok": False, "error": "legacy_v1_v2_v3_or_v4_cursor_schema_required"})
        return result
    plan = _migration_plan(
        cursor_path=cursor_path,
        backup_dir=backup_dir,
        before_schema_version=str(result["before_schema_version"]),
        before_integrity=str(result["before_integrity"]),
        foreign_key_errors=foreign_key_errors,
        daemon_inactive=daemon_inactive,
    )
    result.update(plan)
    if not apply:
        result["ok"] = True
        return result
    if backup_dir is None:
        raise CursorSchemaReconciliationError("backup_directory_required")
    if not expected_plan_hash:
        raise CursorSchemaReconciliationError("expected_plan_hash_required")
    if not daemon_inactive:
        raise CursorSchemaReconciliationError("daemon_not_inactive")
    try:
        with offline_migration_lock(
            cursor_path.parent,
            daemon_check=lambda _database_dir: daemon_inactive,
        ):
            locked_schema_version = _read_schema_version(cursor_path)
            locked_integrity = _integrity(cursor_path)
            locked_foreign_key_errors = _foreign_key_errors(cursor_path)
            if (
                locked_integrity != "ok"
                or locked_foreign_key_errors
                or locked_schema_version
                not in {
                    LEGACY_CURSOR_SCHEMA_VERSION,
                    SNAPSHOTLESS_CURSOR_SCHEMA_VERSION,
                    PREVIOUS_CURSOR_SCHEMA_VERSION,
                    FINGERPRINTLESS_CURSOR_SCHEMA_VERSION,
                }
            ):
                raise CursorSchemaReconciliationError("locked_source_state_invalid")
            locked_plan = _migration_plan(
                cursor_path=cursor_path,
                backup_dir=backup_dir,
                before_schema_version=locked_schema_version,
                before_integrity=locked_integrity,
                foreign_key_errors=locked_foreign_key_errors,
                daemon_inactive=daemon_inactive,
            )
            if expected_plan_hash != locked_plan["plan_hash"]:
                raise CursorSchemaReconciliationError("expected_plan_hash_mismatch")
            result["backup"] = _backup_sqlite(
                cursor_path,
                Path(backup_dir).expanduser(),
            )
            backup_path = Path(backup_dir).expanduser().resolve(strict=False) / str(
                result["backup"]["filename"]
            )
            expected_snapshot_hash = str(locked_plan["apply_scope"]["source_snapshot_sha256"])
            if not _restore_drill(backup_path, expected_snapshot_hash):
                raise CursorSchemaReconciliationError("backup_restore_drill_failed")
            migration_completed = False
            try:
                with sqlite3.connect(str(cursor_path)) as conn:
                    migrate_historical_cursor_schema(conn)
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()
                    if integrity is None or str(integrity[0]) != "ok":
                        raise CursorSchemaReconciliationError("post_migration_integrity_failed")
                    if conn.execute("PRAGMA foreign_key_check").fetchall():
                        raise CursorSchemaReconciliationError(
                            "post_migration_foreign_key_check_failed"
                        )
                    _validate_cursor_schema_v5(conn)
                migration_completed = True
            except CursorSchemaReconciliationError:
                raise
            except (OSError, sqlite3.Error, AgentSyncCursorError):
                raise CursorSchemaReconciliationError("cursor_schema_migration_failed") from None
            finally:
                if not migration_completed:
                    try:
                        _restore_sqlite_backup(
                            backup_path=backup_path,
                            cursor_path=cursor_path,
                            expected_snapshot_hash=expected_snapshot_hash,
                        )
                    except CursorSchemaReconciliationError:
                        raise CursorSchemaReconciliationError("rollback_failed") from None
    except CursorSchemaReconciliationError:
        raise
    except KeyboardInterrupt:
        raise CursorSchemaReconciliationError("cursor_schema_migration_interrupted") from None
    except RuntimeError:
        raise CursorSchemaReconciliationError("writer_lock_unavailable") from None
    result.update(
        {
            "after_schema_version": _read_schema_version(cursor_path),
            "after_integrity": _integrity(cursor_path),
            "reviewed_plan_hash": expected_plan_hash,
        }
    )
    result["ok"] = bool(
        result["after_schema_version"] == CURSOR_SCHEMA_VERSION
        and result["after_integrity"] == "ok"
    )
    return result


def reconcile_cursor_schema(
    *,
    cursor_path: Path,
    backup_dir: Path | None,
    apply: bool,
    daemon_inactive: bool,
    expected_plan_hash: str = "",
) -> dict[str, Any]:
    """Plan, execute, or verify one exact COG-045 cursor migration."""
    cursor_path = Path(cursor_path).expanduser()
    if not apply and _read_schema_version(cursor_path) == CURSOR_SCHEMA_VERSION:
        return _post_schema_dry_run(cursor_path)
    if not apply:
        return _execute_unresolved_cursor_schema_for_test(
            cursor_path=cursor_path,
            backup_dir=backup_dir,
            apply=False,
            daemon_inactive=daemon_inactive,
            expected_plan_hash=expected_plan_hash,
        )
    if backup_dir is None:
        raise CursorSchemaReconciliationError("backup_directory_required")
    if not expected_plan_hash:
        raise CursorSchemaReconciliationError("expected_plan_hash_required")
    if not daemon_inactive:
        raise CursorSchemaReconciliationError("daemon_not_inactive")
    resolved_backup = Path(backup_dir).expanduser().resolve(strict=False)
    try:
        with offline_migration_lock(
            cursor_path.parent,
            daemon_check=lambda _database_dir: daemon_inactive,
        ):
            repeated = _verify_same_plan_receipt(
                cursor_path=cursor_path,
                backup_dir=resolved_backup,
                expected_plan_hash=expected_plan_hash,
            )
            if repeated is not None:
                return repeated
            if _read_schema_version(cursor_path) == CURSOR_SCHEMA_VERSION:
                raise CursorSchemaReconciliationError("migration_receipt_missing")
            preview = _execute_unresolved_cursor_schema_for_test(
                cursor_path=cursor_path,
                backup_dir=resolved_backup,
                apply=False,
                daemon_inactive=True,
            )
            if preview.get("plan_hash") != expected_plan_hash:
                raise CursorSchemaReconciliationError("expected_plan_hash_mismatch")
            before_tables = _source_table_evidence(cursor_path)
            applied = _execute_unresolved_cursor_schema_for_test(
                cursor_path=cursor_path,
                backup_dir=resolved_backup,
                apply=True,
                daemon_inactive=True,
                expected_plan_hash=expected_plan_hash,
            )
            backup_path = resolved_backup / str(applied["backup"]["filename"])
            pre_snapshot_hash = str(preview["apply_scope"]["source_snapshot_sha256"])
            receipt_path = _receipt_path(resolved_backup, expected_plan_hash)
            certification_completed = False
            try:
                after_tables = _source_table_evidence(
                    cursor_path,
                    table_contract={
                        table: tuple(item["columns"]) for table, item in before_tables.items()
                    },
                )
                if before_tables != after_tables:
                    raise CursorSchemaReconciliationError("first_apply_conservation_failed")
                if not _restore_drill(backup_path, pre_snapshot_hash):
                    raise CursorSchemaReconciliationError("backup_restore_drill_failed")
                post_dry_run = _post_schema_dry_run(cursor_path)
                if not post_dry_run["ok"]:
                    raise CursorSchemaReconciliationError("post_apply_gap_nonzero")
                post_snapshot_hash = f"sha256:{_sqlite_snapshot_sha256(cursor_path)}"
                invalidated_count = int(
                    preview["allowed_delta"][
                        "historical_snapshot_bindings_invalidated"
                    ]
                )
                if _snapshot_binding_eligible_count(cursor_path) != 0:
                    raise CursorSchemaReconciliationError(
                        "snapshot_binding_invalidation_failed"
                    )
                _write_receipt(
                    receipt_path,
                    {
                        "schema_version": RECEIPT_VERSION,
                        "status": "completed",
                        "plan_hash": expected_plan_hash,
                        "cursor_db": str(cursor_path.resolve()),
                        "backup_dir": str(resolved_backup),
                        "backup": applied["backup"],
                        "pre_snapshot_hash": pre_snapshot_hash,
                        "post_snapshot_hash": post_snapshot_hash,
                        "first_apply_comparator": {
                            "before": before_tables,
                            "after": after_tables,
                            "ok": True,
                        },
                        "restore_drill_ok": True,
                        "historical_snapshot_bindings_invalidated": (
                            invalidated_count
                        ),
                        "post_apply_dry_run": post_dry_run,
                        "required_gap": post_dry_run["required_gap"],
                    },
                )
                second = _verify_same_plan_receipt(
                    cursor_path=cursor_path,
                    backup_dir=resolved_backup,
                    expected_plan_hash=expected_plan_hash,
                )
                if second is None:
                    raise CursorSchemaReconciliationError("second_apply_receipt_missing")
                certification_completed = True
            except CursorSchemaReconciliationError:
                raise
            except (OSError, sqlite3.Error):
                raise CursorSchemaReconciliationError("migration_evidence_write_failed") from None
            finally:
                if not certification_completed:
                    receipt_path.unlink(missing_ok=True)
                    try:
                        _restore_sqlite_backup(
                            backup_path=backup_path,
                            cursor_path=cursor_path,
                            expected_snapshot_hash=pre_snapshot_hash,
                        )
                    except CursorSchemaReconciliationError:
                        raise CursorSchemaReconciliationError("rollback_failed") from None
            applied.update(
                {
                    "first_apply": {"comparator_ok": True},
                    "restore_drill_ok": True,
                    "second_apply_changed": False,
                    "post_apply_dry_run": post_dry_run,
                    "required_gap": post_dry_run["required_gap"],
                    "receipt_filename": receipt_path.name,
                }
            )
            return applied
    except CursorSchemaReconciliationError:
        raise
    except KeyboardInterrupt:
        raise CursorSchemaReconciliationError("cursor_schema_migration_interrupted") from None
    except RuntimeError:
        raise CursorSchemaReconciliationError("writer_lock_unavailable") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cursor-db", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-plan-hash", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.apply and args.backup_dir is None:
            raise CursorSchemaReconciliationError("backup_directory_required")
        if args.apply and not args.expected_plan_hash:
            raise CursorSchemaReconciliationError("expected_plan_hash_required")
        config = Config(config_path=args.config, provision=False)
        cursor_path = args.cursor_db or (Path(config.database_dir) / CURSOR_FILE_NAME)
        result = reconcile_cursor_schema(
            cursor_path=cursor_path,
            backup_dir=args.backup_dir,
            apply=bool(args.apply),
            daemon_inactive=_runtime_writers_are_inactive(Path(config.database_dir)),
            expected_plan_hash=str(args.expected_plan_hash),
        )
    except CursorSchemaReconciliationError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry_run",
            "ok": False,
            "error": str(exc),
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
