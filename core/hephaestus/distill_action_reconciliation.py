"""Explicit COG-014 reconciliation for legacy cognitive-action ledgers.

The runtime store fails closed on the legacy schema.  This module is the only
translation path: it backs up the whole SQLite database, preserves unrelated
tables, replaces the old self-signed action tables with the canonical schema,
and turns valid v1 artifacts into queued v2 commands.  Legacy artifacts are
labelled as projections; they are never represented as exact fragment roots.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from core.hephaestus.distill_action_store import (
    ARTIFACT_SCHEMA_VERSION,
    OWNED_TABLES,
    SCHEMA_VERSION,
    canonical_json,
    initialize_schema,
    now_utc,
    schema_hash,
    sha256_json,
    stable_id,
)
from core.db_utils import render_sql
from core.privacy.content_redaction import REDACTION_POLICY, redact_persistence_value
from core.utils import atomic_write_text, load_json_value


LEGACY_ARTIFACT_SCHEMA = "mnemos.distill_cognitive_action.v1"
LEGACY_TABLE_PREFIX = "cog014_legacy_"


def inspect_reconciliation(db_path: Path) -> dict[str, Any]:
    """Return a count-only, read-only migration report."""
    path = Path(db_path).expanduser()
    empty = {
        "db_path": str(path),
        "exists": path.is_file(),
        "schema_state": "missing_database",
        "integrity_check": "missing",
        "parent_actions": 0,
        "knowledge_actions": 0,
        "cognitive_commands": 0,
        "status_counts": {},
        "consumptions": 0,
        "legacy_self_signed_consumptions": 0,
        "valid_legacy_artifacts": 0,
        "invalid_legacy_artifacts": 0,
        "orphan_cognitive_actions": 0,
        "apply_required": False,
    }
    if not path.is_file():
        return empty
    with _connect_read_only(path) as conn:
        tables = _tables(conn)
        state = _schema_state(conn, tables)
        report = {
            **empty,
            "exists": True,
            "schema_state": state,
            "integrity_check": str(conn.execute("PRAGMA integrity_check").fetchone()[0]),
            "apply_required": state == "legacy_v1",
        }
        if "distill_action_log" in tables:
            report["parent_actions"] = _count(conn, "distill_action_log")
        if "knowledge_action_log" in tables:
            report["knowledge_actions"] = _count(conn, "knowledge_action_log")
        if "cognitive_action_log" in tables:
            report["cognitive_commands"] = _count(conn, "cognitive_action_log")
            if "status" in _columns(conn, "cognitive_action_log"):
                report["status_counts"] = {
                    str(row[0]): int(row[1])
                    for row in conn.execute(
                        "SELECT status, COUNT(*) FROM cognitive_action_log GROUP BY status"
                    )
                }
            if "distill_action_log" in tables:
                report["orphan_cognitive_actions"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM cognitive_action_log AS child
                        LEFT JOIN distill_action_log AS parent
                          ON parent.action_id=child.distill_action_id
                        WHERE parent.action_id IS NULL
                        """
                    ).fetchone()[0]
                )
        if "cognitive_action_consumptions" in tables:
            consumption_count = _count(conn, "cognitive_action_consumptions")
            report["consumptions"] = consumption_count
            if state == "legacy_v1":
                report["legacy_self_signed_consumptions"] = consumption_count
        if state == "legacy_v1":
            valid, invalid = _inspect_historical_artifacts(conn)
            report["valid_legacy_artifacts"] = valid
            report["invalid_legacy_artifacts"] = invalid
        return report


def backup_database(source: Path, backup_dir: Path) -> dict[str, str]:
    """Create an integrity-checked SQLite backup and return safe metadata."""
    source = Path(source).expanduser()
    backup_dir = Path(backup_dir).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = backup_dir / f"{source.stem}.pre-cog014-v2.{stamp}.db"
    if target.exists():
        raise FileExistsError(target)
    with _connect_read_only(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return {
        "path": str(target),
        "sha256": _file_sha256(target),
        "integrity_check": "ok",
    }


def migrate_historical_database(
    db_path: Path,
    *,
    database_dir: Path,
    backup_dir: Path,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Back up and replace a historical v1 ledger with canonical commands."""
    path = Path(db_path).expanduser()
    database_dir = Path(database_dir).expanduser()
    before = inspect_reconciliation(path)
    if before["schema_state"] == "current_v2":
        return {"migrated": False, "before": before, "after": before, "backup": None}
    if before["schema_state"] != "legacy_v1":
        raise RuntimeError(f"unsupported distill action schema: {before['schema_state']}")
    if before["orphan_cognitive_actions"]:
        raise RuntimeError("legacy cognitive actions contain missing parent actions")
    if before["invalid_legacy_artifacts"]:
        raise RuntimeError("legacy cognitive artifacts are missing, corrupt, or identity-drifted")

    backup = backup_database(path, backup_dir)
    backup_path = Path(backup["path"])
    materialized: list[tuple[Path, dict[str, Any]]] = []
    try:
        with sqlite3.connect(path, timeout=30) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=OFF")
            _inject(failure_injector, "before_rename")
            renamed = _rename_owned_tables(conn)
            initialize_schema(conn)
            _inject(failure_injector, "after_schema")
            _copy_parent_actions(conn, renamed)
            _copy_knowledge_actions(conn, renamed)
            materialized = _copy_cognitive_actions(
                conn,
                renamed,
                database_dir=database_dir,
            )
            _inject(failure_injector, "after_copy")
            for table in renamed.values():
                conn.execute(f'DROP TABLE "{table}"')
            conn.commit()
            if str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("migrated database integrity check failed")
        _inject(failure_injector, "after_commit")
    except (sqlite3.Error, OSError, RuntimeError, ValueError, TypeError, KeyError):
        _restore_database(backup_path, path)
        raise

    projection_failures = 0
    for artifact_path, artifact in materialized:
        try:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                artifact_path,
                json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            # The canonical artifact_payload is already durable in SQLite.
            projection_failures += 1

    after = inspect_reconciliation(path)
    if after["schema_state"] != "current_v2" or after["integrity_check"] != "ok":
        _restore_database(backup_path, path)
        raise RuntimeError("COG-014 migration verification failed; backup restored")
    if before["parent_actions"] != after["parent_actions"]:
        _restore_database(backup_path, path)
        raise RuntimeError("parent action row conservation failed; backup restored")
    if before["knowledge_actions"] != after["knowledge_actions"]:
        _restore_database(backup_path, path)
        raise RuntimeError("knowledge action row conservation failed; backup restored")
    if before["cognitive_commands"] != after["cognitive_commands"]:
        _restore_database(backup_path, path)
        raise RuntimeError("cognitive command row conservation failed; backup restored")
    return {
        "migrated": True,
        "backup": backup,
        "before": before,
        "after": after,
        "materialized_artifacts": len(materialized) - projection_failures,
        "projection_failures": projection_failures,
        "legacy_mapping_disposition": "legacy_artifact_projection_not_exact_fragment_mapping",
    }


def _rename_owned_tables(conn: sqlite3.Connection) -> dict[str, str]:
    tables = _tables(conn)
    _drop_owned_indexes(conn, tables)
    renamed: dict[str, str] = {}
    for name in sorted(OWNED_TABLES.intersection(tables)):
        legacy_name = LEGACY_TABLE_PREFIX + name
        if legacy_name in tables:
            raise RuntimeError(f"stale reconciliation table exists: {legacy_name}")
        conn.execute(f'ALTER TABLE "{name}" RENAME TO "{legacy_name}"')
        renamed[name] = legacy_name
    return renamed


def _drop_owned_indexes(conn: sqlite3.Connection, tables: set[str]) -> None:
    owned = OWNED_TABLES.intersection(tables)
    if not owned:
        return
    rows = conn.execute(
        """
        SELECT name, tbl_name FROM sqlite_master
        WHERE type='index' AND sql IS NOT NULL
        """
    ).fetchall()
    for name, table in rows:
        if str(table) in owned:
            safe_name = str(name).replace('"', '""')
            conn.execute(f'DROP INDEX "{safe_name}"')


def _copy_parent_actions(conn: sqlite3.Connection, renamed: Mapping[str, str]) -> None:
    table = renamed.get("distill_action_log")
    if not table:
        return
    rows = conn.execute(
        render_sql(
            "SELECT * FROM {table} ORDER BY created_at, action_id",
            identifiers={"table": table},
        )
    ).fetchall()
    for row in rows:
        value = dict(row)
        evidence = _redacted_json_text(value.get("evidence_refs"), default=[])
        detail = _redacted_json_text(value.get("result_detail"), default={})
        merge_card = _redacted_json_text(value.get("merge_decision_card"), default={})
        conn.execute(
            """
            INSERT INTO distill_action_log (
                action_id, created_at, session_id, source_agent, action,
                distill_intent, claim_id, target_page, target_kind,
                source_event_ids, evidence_refs, backup_path, result_status,
                result_detail, error, merge_decision_card
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value.get("action_id", ""),
                value.get("created_at", ""),
                value.get("session_id", ""),
                value.get("source_agent", ""),
                value.get("action", ""),
                value.get("distill_intent", ""),
                value.get("claim_id", ""),
                value.get("target_page", ""),
                value.get("target_kind", ""),
                _normalized_json_text(value.get("source_event_ids"), default=[]),
                evidence,
                value.get("backup_path", ""),
                value.get("result_status", "error"),
                detail,
                _redacted_text(value.get("error", "")),
                merge_card,
            ),
        )
        conn.execute(
            """
            INSERT INTO distill_action_events (
                event_id, action_id, created_at, event_type, status, detail
            ) VALUES (?, ?, ?, 'legacy_migrated', ?, ?)
            """,
            (
                stable_id("dae", value.get("action_id", ""), "legacy_migrated"),
                value.get("action_id", ""),
                value.get("created_at", ""),
                value.get("result_status", "error"),
                canonical_json({"source_schema": "legacy_v1"}),
            ),
        )


def _copy_knowledge_actions(conn: sqlite3.Connection, renamed: Mapping[str, str]) -> None:
    table = renamed.get("knowledge_action_log")
    if not table:
        return
    rows = conn.execute(
        render_sql(
            "SELECT * FROM {table} ORDER BY id",
            identifiers={"table": table},
        )
    ).fetchall()
    for row in rows:
        value = dict(row)
        detail = _redacted_json_text(value.get("detail"), default={})
        identity = stable_id(
            "ka",
            "legacy_v1",
            value.get("id", 0),
            value.get("action_id", ""),
            value.get("change_type", ""),
            value.get("target_page", ""),
        )
        conn.execute(
            """
            INSERT INTO knowledge_action_log (
                knowledge_action_id, action_id, created_at, change_type,
                target_page, backup_path, event_type, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity,
                value.get("action_id", ""),
                value.get("created_at", ""),
                value.get("change_type", ""),
                value.get("target_page", ""),
                value.get("backup_path", ""),
                value.get("event_type", ""),
                detail,
            ),
        )


def _copy_cognitive_actions(
    conn: sqlite3.Connection,
    renamed: Mapping[str, str],
    *,
    database_dir: Path,
) -> list[tuple[Path, dict[str, Any]]]:
    table = renamed.get("cognitive_action_log")
    if not table:
        return []
    rows = conn.execute(
        render_sql(
            "SELECT * FROM {table} ORDER BY id",
            identifiers={"table": table},
        )
    ).fetchall()
    parent_rows = {
        str(row["action_id"]): dict(row)
        for row in conn.execute("SELECT * FROM distill_action_log").fetchall()
    }
    materialized: list[tuple[Path, dict[str, Any]]] = []
    acl = {
        "visibility": "private",
        "owner": "local_user",
        "redaction_policy": REDACTION_POLICY,
        "encryption": "none",
    }
    for row in rows:
        value = dict(row)
        parent = parent_rows[str(value.get("distill_action_id") or "")]
        source_artifact = _load_historical_artifact(value)
        artifact = _build_v2_artifact(value, parent, source_artifact, acl=acl)
        artifact_path = (
            database_dir
            / "distill_cognitive_actions"
            / "legacy-reconciled-v2"
            / str(value.get("created_at") or "unknown")[:10]
            / f"{value['cognitive_action_id']}.json"
        )
        fragment_ids = list(artifact["fragment_ids"])
        parent_status = str(parent.get("result_status") or "")
        disposition = "command_created" if parent_status == "applied" else "parent_not_committed"
        conn.execute(
            """
            INSERT INTO cognitive_action_intents (
                cognitive_action_id, distill_action_id, created_at, session_id,
                claim_id, cognitive_action, parent_status, disposition,
                episode_id, fragment_ids, source_event_ids, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value["cognitive_action_id"],
                value["distill_action_id"],
                value["created_at"],
                value["session_id"],
                value.get("claim_id", ""),
                value["cognitive_action"],
                parent_status,
                disposition,
                artifact["episode_id"],
                canonical_json(fragment_ids),
                canonical_json(artifact["source_event_ids"]),
                canonical_json(
                    {
                        "migration": "COG-014",
                        "mapping_quality": "legacy_artifact_projection",
                    }
                ),
            ),
        )
        if parent_status != "applied":
            continue
        artifact_hash = sha256_json(artifact)
        conn.execute(
            """
            INSERT INTO cognitive_action_log (
                cognitive_action_id, distill_action_id, created_at, session_id,
                source_agent, claim_id, cognitive_action, target_kind, status,
                source_event_ids, evidence_refs, artifact_path,
                artifact_schema_version, artifact_hash, artifact_payload,
                episode_id, fragment_ids, acl_payload, input_spec_hash,
                extraction_output_hash, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value["cognitive_action_id"],
                value["distill_action_id"],
                value["created_at"],
                value["session_id"],
                value.get("source_agent", ""),
                value.get("claim_id", ""),
                value["cognitive_action"],
                value.get("target_kind", ""),
                canonical_json(artifact["source_event_ids"]),
                canonical_json(artifact["evidence_refs"]),
                str(artifact_path),
                ARTIFACT_SCHEMA_VERSION,
                artifact_hash,
                canonical_json(artifact),
                artifact["episode_id"],
                canonical_json(fragment_ids),
                canonical_json(acl),
                artifact["input_spec_hash"],
                artifact["extraction_output_hash"],
                canonical_json(
                    {
                        "migration": "COG-014",
                        "legacy_status_ignored": value.get("status", ""),
                        "legacy_consumption_disposition": "self_signed_not_copied",
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO cognitive_action_events (
                event_id, cognitive_action_id, created_at, event_type,
                from_status, to_status, detail
            ) VALUES (?, ?, ?, 'legacy_requeued', 'legacy_applied', 'queued', ?)
            """,
            (
                stable_id("cae", value["cognitive_action_id"], "legacy_requeued"),
                value["cognitive_action_id"],
                now_utc(),
                canonical_json({"source_schema": LEGACY_ARTIFACT_SCHEMA}),
            ),
        )
        materialized.append((artifact_path, artifact))
    conn.commit()
    return materialized


def _build_v2_artifact(
    row: Mapping[str, Any],
    parent: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    acl: Mapping[str, Any],
) -> dict[str, Any]:
    source_hash = sha256_json(source)
    source_events = _json_list(source.get("source_event_ids") or row.get("source_event_ids"))
    evidence_refs = _json_list(source.get("evidence_refs") or row.get("evidence_refs"))
    claim_id = str(row.get("claim_id") or source.get("claim_id") or "")
    fragment_id = stable_id(
        "legacy_fragment",
        row.get("cognitive_action_id"),
        source_hash,
    )
    episode_id = stable_id(
        "episode",
        "legacy_v1",
        row.get("session_id"),
        row.get("distill_action_id"),
    )
    evidence = [
        {
            "source_event_id": source_events[index % len(source_events)],
            "quote": text,
        }
        for index, text in enumerate(evidence_refs)
    ]
    claim = {
        "claim_id": claim_id,
        "claim_text": str(source.get("claim_text") or ""),
        "claim_type": str(source.get("claim_type") or "technical_fact"),
        "scope": {
            "domain": "legacy_distillation",
            "provenance_status": "legacy_artifact_projection",
        },
        "evidence": evidence,
        "relation_to_existing": dict(source.get("relation_to_existing") or {}),
        "recommended_action": str(source.get("recommended_action") or parent.get("action") or ""),
        "confidence": 0.0,
        "confidence_status": "not_recorded_in_v1",
    }
    input_hash = "legacy-input:" + sha256_json(
        {
            "source_agent": row.get("source_agent", ""),
            "session_id": row.get("session_id", ""),
            "source_event_ids": source_events,
        }
    )
    extraction_hash = "legacy-artifact:" + source_hash
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "cognitive_action_id": str(row["cognitive_action_id"]),
        "distill_action_id": str(row["distill_action_id"]),
        "episode_id": episode_id,
        "created_at": str(row.get("created_at") or now_utc()),
        "session_id": str(row.get("session_id") or ""),
        "source_agent": str(row.get("source_agent") or ""),
        "claim_id": claim_id,
        "cognitive_action": str(row["cognitive_action"]),
        "target_kind": str(row.get("target_kind") or ""),
        "recommended_action": str(source.get("recommended_action") or parent.get("action") or ""),
        "source_event_ids": source_events,
        "evidence_refs": evidence_refs,
        "input_spec_hash": input_hash,
        "extraction_output_hash": extraction_hash,
        "raw_event_refs": [],
        "fragment_ids": [fragment_id],
        "fragment_refs": [
            {
                "fragment_id": fragment_id,
                "claim_ids": [claim_id],
                "title": "legacy v1 cognitive action artifact projection",
                "content_hash": source_hash,
                "source_kind": "legacy_v1_artifact_projection",
            }
        ],
        "mapping_quality": "legacy_artifact_projection",
        "parent_target_pages": [
            value for value in str(parent.get("target_page") or "").split(";") if value
        ],
        "claim": claim,
        "user_behavior_intent": {
            "content_source": "unknown",
            "user_intent_signal": "unknown",
            "intent_status": "legacy_not_recorded",
        },
        "acl": dict(acl),
        "legacy_reconciliation": {
            "issue": "COG-014",
            "source_schema": LEGACY_ARTIFACT_SCHEMA,
            "source_artifact_hash": source_hash,
            "mapping_disposition": "artifact_projection_not_exact_fragment_mapping",
            "legacy_applied_receipt_trusted": False,
        },
    }
    return dict(redact_persistence_value(artifact).value)


def _inspect_historical_artifacts(conn: sqlite3.Connection) -> tuple[int, int]:
    valid = 0
    invalid = 0
    for row in conn.execute("SELECT * FROM cognitive_action_log"):
        try:
            _load_historical_artifact(dict(row))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            invalid += 1
        else:
            valid += 1
    return valid, invalid


def _load_historical_artifact(row: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(str(row.get("artifact_path") or ""))
    if not path.is_file():
        raise FileNotFoundError(path)
    value = load_json_value(path)
    if not isinstance(value, dict) or value.get("schema_version") != LEGACY_ARTIFACT_SCHEMA:
        raise ValueError("unsupported legacy cognitive artifact")
    bindings = {
        "cognitive_action_id": row.get("cognitive_action_id"),
        "distill_action_id": row.get("distill_action_id"),
        "session_id": row.get("session_id"),
        "claim_id": row.get("claim_id"),
        "cognitive_action": row.get("cognitive_action"),
    }
    drift = [
        key
        for key, expected in bindings.items()
        if str(value.get(key) or "") != str(expected or "")
    ]
    if drift:
        raise ValueError("legacy artifact identity drift")
    source_events = _json_list(value.get("source_event_ids") or row.get("source_event_ids"))
    if not source_events:
        raise ValueError("legacy artifact has no source event identity")
    return value


def _schema_state(conn: sqlite3.Connection, tables: set[str]) -> str:
    if "distill_action_schema_registry" in tables:
        try:
            row = conn.execute(
                """
                SELECT schema_version, schema_hash FROM distill_action_schema_registry
                WHERE schema_name='distill_actions'
                """
            ).fetchone()
        except sqlite3.Error:
            return "unknown"
        if row and str(row[0]) == SCHEMA_VERSION and str(row[1]) == schema_hash():
            return "current_v2"
        return "registry_drift"
    columns = _columns(conn, "cognitive_action_log") if "cognitive_action_log" in tables else set()
    if {"processed_at", "error"}.issubset(columns) and "artifact_hash" not in columns:
        return "legacy_v1"
    if not OWNED_TABLES.intersection(tables):
        return "uninitialized"
    return "unknown"


def _restore_database(backup: Path, destination: Path) -> None:
    with _connect_read_only(backup) as source, sqlite3.connect(destination) as target:
        source.backup(target)
        target.commit()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if str(row[0]) != "sqlite_sequence"
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(
            render_sql(
                "PRAGMA table_info({table})",
                identifiers={"table": table},
            )
        )
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(
        conn.execute(
            render_sql(
                "SELECT COUNT(*) FROM {table}",
                identifiers={"table": table},
            )
        ).fetchone()[0]
    )


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item)))


def _normalized_json_text(value: Any, *, default: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = default
    return canonical_json(value if isinstance(value, type(default)) else default)


def _redacted_json_text(value: Any, *, default: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = default
    normalized = value if isinstance(value, type(default)) else default
    return canonical_json(redact_persistence_value(normalized).value)


def _redacted_text(value: Any) -> str:
    return str(redact_persistence_value(str(value or "")).value)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _inject(callback: Callable[[str], None] | None, phase: str) -> None:
    if callback is not None:
        callback(phase)
