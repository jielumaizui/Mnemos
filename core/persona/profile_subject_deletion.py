"""Typed subject deletion owner for ACL-bound cognitive profile objects.

Profile signals, assertions, sealed read authorizations, usage logs, and their
durable usage outbox are derived cognition. Their semantic bodies are
deliberately never inspected to decide a deletion scope: the selector is an
object ACL, an exact source-event header, or a typed signal reference. Legacy
rows without an ACL remain an explicit verification gap for scoped deletion
instead of being guessed public or silently ignored.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.cognitive.access_control import (
    cognitive_access_hash,
    cognitive_access_matches_subject,
    validate_cognitive_access_envelope,
)
from core.db_utils import render_sql

PROFILE_SUBJECT_DELETION_SCHEMA_VERSION = "mnemos.profile_subject_deletion.v1"
PROFILE_SUBJECT_DELETION_TABLE = "profile_subject_deletion_receipts"
_PROFILE_TABLES = (
    "profile_signals",
    "profile_assertions",
    "profile_read_authorizations",
    "profile_usage_log",
    "profile_usage_outbox",
)
_UNMAPPED_LEGACY_PERSONA_TABLES = (
    "session_signals",
    "knowledge_signals",
    "git_signals",
    "file_system_signals",
    "signal_metadata",
    "signal_daily_index",
    "behavior_prompt_signals",
    "persona_versions",
    "note_signals",
    "document_signals",
    "wechat_signals",
    "reflection_signals",
)
_SUPPORTED_SCOPES = frozenset(
    {"all", "agent", "session", "project", "raw_event_id", "persona_signal"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_hash(scope_kind: str, scope_value: str) -> str:
    material = f"{str(scope_kind).strip().lower()}:{str(scope_value).strip()}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _receipt_id(*, request_id: str, object_type: str, object_id: str, scope_value_hash: str) -> str:
    material = "|".join((request_id, object_type, object_id, scope_value_hash))
    return "profile-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _raw_acl_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _parse_acl(value: Any) -> dict[str, Any] | None:
    try:
        access = validate_cognitive_access_envelope(json.loads(str(value or "")))
        return access if access["scope"]["resolution"] == "resolved" else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _json_mapping_keys(value: Any) -> set[str]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    if not isinstance(parsed, dict):
        return set()
    return {str(key) for key in parsed if str(key).strip()}


def _signal_id_from_scope(value: str) -> int | None:
    normalized = str(value or "").strip()
    if normalized.startswith("profile_signals:"):
        normalized = normalized.split(":", 1)[1]
    try:
        number = int(normalized)
    except ValueError:
        return None
    return number if number > 0 else None


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _has_required_columns(conn: sqlite3.Connection, table_name: str, columns: set[str]) -> bool:
    actual = {
        str(row[1])
        for row in conn.execute(
            render_sql(
                "PRAGMA table_info({table})",
                identifiers={"table": table_name},
            )
        )
    }
    return columns <= actual


def _unmapped_historical_persona_row_count(conn: sqlite3.Connection) -> int:
    """Count historical Persona rows whose subject lineage is not machine-provable.

    The V2 profile owner must not quietly certify a scoped deletion while the
    same database still contains old behavioral/aggregate records with no ACL
    or stable source mapping.  This is intentionally a header/schema-only
    count: it neither reads nor emits any unmapped historical body bytes.
    """

    total = 0
    for table_name in _UNMAPPED_LEGACY_PERSONA_TABLES:
        if _table_exists(conn, table_name):
            total += int(
                conn.execute(
                    render_sql(
                        "SELECT COUNT(*) FROM {table}",
                        identifiers={"table": table_name},
                    )
                ).fetchone()[0]
                or 0
            )
    return total


def _ensure_receipt_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        render_sql(
            """
        CREATE TABLE IF NOT EXISTS {table} (
            receipt_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            request_id TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            scope_value_hash TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            before_access_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status='applied'),
            created_at TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            UNIQUE(request_id, object_type, object_id)
        )
        """,
            identifiers={"table": PROFILE_SUBJECT_DELETION_TABLE},
        )
    )
    conn.execute(
        render_sql(
            """
        CREATE INDEX IF NOT EXISTS idx_profile_subject_deletion_scope
        ON {table}(scope_kind, scope_value_hash, status)
        """,
            identifiers={"table": PROFILE_SUBJECT_DELETION_TABLE},
        )
    )


def _insert_receipt(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    scope_kind: str,
    scope_value_hash: str,
    object_type: str,
    object_id: str,
    before_access_hash: str,
    now: str,
) -> None:
    conn.execute(
        render_sql(
            """
        INSERT OR IGNORE INTO {table} (
            receipt_id, schema_version, request_id, scope_kind,
            scope_value_hash, object_type, object_id, before_access_hash,
            status, created_at, applied_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
        """,
            identifiers={"table": PROFILE_SUBJECT_DELETION_TABLE},
        ),
        (
            _receipt_id(
                request_id=request_id,
                object_type=object_type,
                object_id=object_id,
                scope_value_hash=scope_value_hash,
            ),
            PROFILE_SUBJECT_DELETION_SCHEMA_VERSION,
            request_id,
            scope_kind,
            scope_value_hash,
            object_type,
            object_id,
            before_access_hash,
            now,
            now,
        ),
    )


def _subject_matches(
    access: Mapping[str, Any] | None,
    *,
    scope_kind: str,
    scope_value: str,
) -> bool:
    if scope_kind == "all":
        return scope_value == "all"
    return access is not None and cognitive_access_matches_subject(
        access,
        scope_kind=scope_kind,
        scope_value=scope_value,
    )


def delete_profile_subject_scope(
    *,
    db_path: Path | str,
    request_id: str,
    scope_kind: str,
    scope_value: str,
) -> dict[str, Any]:
    """Physically delete only provenance-provable profile objects.

    All selected rows and their append-only receipts commit atomically.  A
    scoped request with any unmapped ACL row is intentionally not ``verified``:
    it cannot prove that the remaining opaque row is unrelated to the subject.
    """

    database = Path(db_path).expanduser()
    kind = str(scope_kind or "").strip().lower()
    value = str(scope_value or "").strip()
    if kind not in _SUPPORTED_SCOPES or (kind == "all" and value != "all"):
        return {
            "status": "unsupported_scope",
            "target_count": 0,
            "receipt_count": 0,
            "verified": False,
            "supported_scopes": sorted(_SUPPORTED_SCOPES),
        }
    if not database.is_file():
        return {
            "status": "not_initialized",
            "target_count": 0,
            "receipt_count": 0,
            "verified": True,
        }
    if not str(request_id or "").strip() or not value:
        raise ValueError("profile subject deletion requires request_id and scope_value")

    normalized_value = value.lower() if kind in {"agent", "project"} else value
    scope_value_hash = _scope_hash(kind, normalized_value)
    try:
        with sqlite3.connect(str(database), timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            present_tables = {name for name in _PROFILE_TABLES if _table_exists(conn, name)}
            if not present_tables:
                return {
                    "status": "not_initialized",
                    "target_count": 0,
                    "receipt_count": 0,
                    "verified": True,
                }
            if present_tables != set(_PROFILE_TABLES):
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "verified": False,
                    "error": "profile_schema_incomplete",
                }
            required_columns = {
                "profile_signals": {"id", "source_event_id", "access_control"},
                "profile_assertions": {
                    "assertion_id",
                    "supporting_signals",
                    "access_control",
                },
                "profile_read_authorizations": {
                    "token_id",
                    "authorized_assertion_revisions",
                    "access_control",
                },
                "profile_usage_log": {"id", "profile_fields_used", "access_control"},
                "profile_usage_outbox": {
                    "command_id",
                    "usage_id",
                    "access_control",
                },
            }
            if any(
                not _has_required_columns(conn, table_name, columns)
                for table_name, columns in required_columns.items()
            ):
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "verified": False,
                    "error": "profile_acl_schema_incomplete",
                }
            _ensure_receipt_schema(conn)
            conn.execute("PRAGMA secure_delete=ON")
            secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
            if not secure_delete or int(secure_delete[0] or 0) < 1:
                return {
                    "status": "blocked",
                    "target_count": 0,
                    "receipt_count": 0,
                    "verified": False,
                    "error": "profile_secure_delete_unavailable",
                }

            signal_headers = conn.execute(
                "SELECT id, source_event_id, access_control FROM profile_signals"
            ).fetchall()
            selected_signals: list[tuple[int, str]] = []
            legacy_signal_count = 0
            requested_signal_id = _signal_id_from_scope(value) if kind == "persona_signal" else None
            for row in signal_headers:
                raw_access = row["access_control"]
                access = _parse_acl(raw_access)
                if access is None:
                    legacy_signal_count += 1
                direct_match = (
                    kind == "raw_event_id" and str(row["source_event_id"] or "") == value
                ) or (kind == "persona_signal" and int(row["id"]) == requested_signal_id)
                if direct_match or _subject_matches(
                    access,
                    scope_kind=kind,
                    scope_value=normalized_value,
                ):
                    selected_signals.append(
                        (
                            int(row["id"]),
                            (
                                cognitive_access_hash(access)
                                if access is not None
                                else _raw_acl_hash(raw_access)
                            ),
                        )
                    )

            selected_signal_refs = {
                f"profile_signals:{signal_id}" for signal_id, _ in selected_signals
            }
            assertion_headers = conn.execute(
                "SELECT assertion_id, supporting_signals, access_control FROM profile_assertions"
            ).fetchall()
            selected_assertions: list[tuple[str, str]] = []
            legacy_assertion_count = 0
            for row in assertion_headers:
                raw_access = row["access_control"]
                access = _parse_acl(raw_access)
                if access is None:
                    legacy_assertion_count += 1
                derived_match = bool(
                    selected_signal_refs.intersection(_json_list(row["supporting_signals"]))
                )
                if derived_match or _subject_matches(
                    access,
                    scope_kind=kind,
                    scope_value=normalized_value,
                ):
                    selected_assertions.append(
                        (
                            str(row["assertion_id"]),
                            (
                                cognitive_access_hash(access)
                                if access is not None
                                else _raw_acl_hash(raw_access)
                            ),
                        )
                    )

            selected_assertion_ids = {assertion_id for assertion_id, _ in selected_assertions}
            read_authorization_headers = conn.execute("""
                SELECT token_id, authorized_assertion_revisions, access_control
                FROM profile_read_authorizations
                """).fetchall()
            selected_read_authorizations: list[tuple[str, str]] = []
            legacy_read_authorization_count = 0
            for row in read_authorization_headers:
                raw_access = row["access_control"]
                access = _parse_acl(raw_access)
                if access is None:
                    legacy_read_authorization_count += 1
                derived_match = bool(
                    selected_assertion_ids.intersection(
                        _json_mapping_keys(row["authorized_assertion_revisions"])
                    )
                )
                if derived_match or _subject_matches(
                    access,
                    scope_kind=kind,
                    scope_value=normalized_value,
                ):
                    selected_read_authorizations.append(
                        (
                            str(row["token_id"]),
                            (
                                cognitive_access_hash(access)
                                if access is not None
                                else _raw_acl_hash(raw_access)
                            ),
                        )
                    )

            usage_headers = conn.execute(
                "SELECT id, profile_fields_used, access_control FROM profile_usage_log"
            ).fetchall()
            selected_usages: list[tuple[int, str]] = []
            legacy_usage_count = 0
            for row in usage_headers:
                raw_access = row["access_control"]
                access = _parse_acl(raw_access)
                if access is None:
                    legacy_usage_count += 1
                derived_match = bool(
                    selected_assertion_ids.intersection(_json_list(row["profile_fields_used"]))
                )
                if derived_match or _subject_matches(
                    access,
                    scope_kind=kind,
                    scope_value=normalized_value,
                ):
                    selected_usages.append(
                        (
                            int(row["id"]),
                            (
                                cognitive_access_hash(access)
                                if access is not None
                                else _raw_acl_hash(raw_access)
                            ),
                        )
                    )

            selected_usage_ids = {usage_id for usage_id, _ in selected_usages}
            outbox_headers = conn.execute("""
                SELECT command_id, usage_id, access_control
                FROM profile_usage_outbox
                """).fetchall()
            selected_outboxes: list[tuple[str, str]] = []
            legacy_outbox_count = 0
            for row in outbox_headers:
                raw_access = row["access_control"]
                access = _parse_acl(raw_access)
                if access is None:
                    legacy_outbox_count += 1
                usage_id = row["usage_id"]
                derived_match = usage_id is not None and int(usage_id) in selected_usage_ids
                if derived_match or _subject_matches(
                    access,
                    scope_kind=kind,
                    scope_value=normalized_value,
                ):
                    selected_outboxes.append(
                        (
                            str(row["command_id"]),
                            (
                                cognitive_access_hash(access)
                                if access is not None
                                else _raw_acl_hash(raw_access)
                            ),
                        )
                    )

            target_count = (
                len(selected_signals)
                + len(selected_assertions)
                + len(selected_read_authorizations)
                + len(selected_usages)
                + len(selected_outboxes)
            )
            unmapped_legacy_persona_count = _unmapped_historical_persona_row_count(conn)
            prior_count = int(
                conn.execute(
                    render_sql(
                        """
                    SELECT COUNT(*) FROM {table}
                    WHERE scope_kind=? AND scope_value_hash=? AND status='applied'
                    """,
                        identifiers={"table": PROFILE_SUBJECT_DELETION_TABLE},
                    ),
                    (kind, scope_value_hash),
                ).fetchone()[0]
            )
            if not target_count:
                unresolved = (
                    legacy_signal_count
                    + legacy_assertion_count
                    + legacy_read_authorization_count
                    + legacy_usage_count
                    + legacy_outbox_count
                    + unmapped_legacy_persona_count
                )
                return {
                    "status": "existing" if prior_count else "no_targets",
                    "target_count": prior_count,
                    "receipt_count": prior_count,
                    "profile_signals_deleted": 0,
                    "profile_assertions_deleted": 0,
                    "profile_read_authorizations_deleted": 0,
                    "profile_usage_logs_deleted": 0,
                    "profile_usage_outboxes_deleted": 0,
                    "unresolved_legacy_count": unresolved,
                    "unmapped_legacy_persona_count": unmapped_legacy_persona_count,
                    "verified": unresolved == 0,
                }

            now = _now()
            conn.execute("BEGIN IMMEDIATE")
            for command_id, before_access_hash in selected_outboxes:
                _insert_receipt(
                    conn,
                    request_id=str(request_id),
                    scope_kind=kind,
                    scope_value_hash=scope_value_hash,
                    object_type="profile_usage_outbox",
                    object_id=command_id,
                    before_access_hash=before_access_hash,
                    now=now,
                )
                conn.execute(
                    "DELETE FROM profile_usage_outbox WHERE command_id=?",
                    (command_id,),
                )
            for usage_id, before_access_hash in selected_usages:
                _insert_receipt(
                    conn,
                    request_id=str(request_id),
                    scope_kind=kind,
                    scope_value_hash=scope_value_hash,
                    object_type="profile_usage_log",
                    object_id=str(usage_id),
                    before_access_hash=before_access_hash,
                    now=now,
                )
                conn.execute("DELETE FROM profile_usage_log WHERE id=?", (usage_id,))
            for token_id, before_access_hash in selected_read_authorizations:
                _insert_receipt(
                    conn,
                    request_id=str(request_id),
                    scope_kind=kind,
                    scope_value_hash=scope_value_hash,
                    object_type="profile_read_authorization",
                    object_id=token_id,
                    before_access_hash=before_access_hash,
                    now=now,
                )
                conn.execute(
                    "DELETE FROM profile_read_authorizations WHERE token_id=?",
                    (token_id,),
                )
            for assertion_id, before_access_hash in selected_assertions:
                _insert_receipt(
                    conn,
                    request_id=str(request_id),
                    scope_kind=kind,
                    scope_value_hash=scope_value_hash,
                    object_type="profile_assertion",
                    object_id=assertion_id,
                    before_access_hash=before_access_hash,
                    now=now,
                )
                conn.execute(
                    """
                    INSERT INTO profile_assertion_revision_delete_permits (
                        assertion_id, request_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (assertion_id, str(request_id), now),
                )
                conn.execute(
                    "DELETE FROM profile_assertion_heads WHERE assertion_id=?",
                    (assertion_id,),
                )
                conn.execute(
                    "DELETE FROM profile_assertions WHERE assertion_id=?",
                    (assertion_id,),
                )
                conn.execute(
                    "DELETE FROM profile_assertion_revisions WHERE assertion_id=?",
                    (assertion_id,),
                )
                conn.execute(
                    "DELETE FROM profile_assertion_revision_delete_permits "
                    "WHERE assertion_id=? AND request_id=?",
                    (assertion_id, str(request_id)),
                )
            for signal_id, before_access_hash in selected_signals:
                _insert_receipt(
                    conn,
                    request_id=str(request_id),
                    scope_kind=kind,
                    scope_value_hash=scope_value_hash,
                    object_type="profile_signal",
                    object_id=str(signal_id),
                    before_access_hash=before_access_hash,
                    now=now,
                )
                conn.execute("DELETE FROM profile_signals WHERE id=?", (signal_id,))
            conn.commit()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {
            "status": "blocked",
            "target_count": 0,
            "receipt_count": 0,
            "verified": False,
            "error": "profile_subject_deletion_failed",
        }

    unresolved = (
        legacy_signal_count
        + legacy_assertion_count
        + legacy_read_authorization_count
        + legacy_usage_count
        + legacy_outbox_count
        + unmapped_legacy_persona_count
    )
    return {
        "status": "applied",
        "target_count": target_count,
        "receipt_count": target_count,
        "profile_signals_deleted": len(selected_signals),
        "profile_assertions_deleted": len(selected_assertions),
        "profile_read_authorizations_deleted": len(selected_read_authorizations),
        "profile_usage_logs_deleted": len(selected_usages),
        "profile_usage_outboxes_deleted": len(selected_outboxes),
        "unresolved_legacy_count": unresolved,
        "unmapped_legacy_persona_count": unmapped_legacy_persona_count,
        "verified": unresolved == 0,
    }
