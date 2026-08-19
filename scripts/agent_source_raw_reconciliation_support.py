"""Content-free cycle reports and Raw session-identity reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive as _shared_runtime_is_inactive,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.agent_source import canonicalize_session_info
from core.sync_framework.raw_session_identity_reconciliation import (
    RawSessionIdentityReconciliationError,
    build_receipt_material as build_session_identity_receipt_material,
    initialize_schema as initialize_session_identity_reconciliation_schema,
    record_receipt as record_session_identity_reconciliation_receipt,
    receipt_allows_current_fingerprint as session_identity_receipt_allows,
    table_exists as session_identity_reconciliation_table_exists,
    validate_schema as validate_session_identity_reconciliation_schema,
)
from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
)
from scripts.agent_source_raw_recovery_support import _canonical_hash


def _safe_cycle_report(result: Mapping[str, Any], source_names: Iterable[str]) -> dict[str, Any]:
    coverage_sources = result.get("source_coverage", {})
    if isinstance(coverage_sources, Mapping):
        coverage_sources = coverage_sources.get("sources", {})
    if not isinstance(coverage_sources, Mapping):
        coverage_sources = {}
    source_snapshots = result.get("source_snapshots", {})
    if not isinstance(source_snapshots, Mapping):
        source_snapshots = {}
    sources: dict[str, dict[str, Any]] = {}
    for source_name in source_names:
        entry = coverage_sources.get(source_name, {})
        cursor = entry.get("cursor", {}) if isinstance(entry, Mapping) else {}
        snapshot = source_snapshots.get(source_name, {})
        denominator = (
            snapshot.get("native_denominator", {}) if isinstance(snapshot, Mapping) else {}
        )
        sources[source_name] = {
            "status": str(entry.get("status") or "") if isinstance(entry, Mapping) else "",
            "gap": str(entry.get("gap") or "") if isinstance(entry, Mapping) else "",
            "native_sessions": (
                int(entry.get("native_sessions") or 0) if isinstance(entry, Mapping) else 0
            ),
            "native_turns": (
                int(entry.get("native_turns") or 0) if isinstance(entry, Mapping) else 0
            ),
            "denominator_complete": (
                bool(cursor.get("denominator_complete")) if isinstance(cursor, Mapping) else False
            ),
            "denominator_turns": (
                int(cursor.get("denominator_turns") or 0) if isinstance(cursor, Mapping) else 0
            ),
            "snapshot_denominator_turns": (
                int(denominator.get("turns") or 0) if isinstance(denominator, Mapping) else 0
            ),
        }
    return {"errors": int(result.get("errors") or 0), "sources": sources}


def _safe_sync_error_evidence(
    records: Iterable[tuple[str, BaseException]],
) -> list[dict[str, Any]]:
    """Retain typed failure identity without native paths or payload text."""
    counts: dict[tuple[str, str, str, str], int] = {}
    for service, error in records:
        raw_code = str(getattr(error, "code", "") or "")
        code = raw_code if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", raw_code) else ""
        error_type = error.__class__.__name__
        message_hash = hashlib.sha256((code or error_type).encode("utf-8")).hexdigest()
        key = (
            str(service).split(":", 1)[-1],
            error_type,
            code,
            message_hash,
        )
        counts[key] = counts.get(key, 0) + 1
    return [
        {
            "source": source,
            "error_type": error_type,
            "error_code": code,
            "message_hash": message_hash,
            "count": counts[(source, error_type, code, message_hash)],
        }
        for source, error_type, code, message_hash in sorted(counts)
    ]


def _database_file_state(database_dir: Path, allowed_names: set[str]) -> dict[str, tuple[int, int]]:
    state: dict[str, tuple[int, int]] = {}
    try:
        children = list(Path(database_dir).iterdir())
    except OSError:
        raise AgentSourceRawReconciliationError("database_scope_inspection_unavailable") from None
    for child in children:
        if child.name in allowed_names:
            continue
        try:
            metadata = child.stat()
        except OSError:
            raise AgentSourceRawReconciliationError(
                "database_scope_inspection_unavailable"
            ) from None
        if not stat.S_ISREG(metadata.st_mode):
            continue
        state[child.name] = (
            int(metadata.st_size),
            int(metadata.st_mtime_ns),
        )
    return state


def _unexpected_database_mutations(
    before: Mapping[str, tuple[int, int]],
    after: Mapping[str, tuple[int, int]],
) -> list[str]:
    names = set(before) | set(after)
    return sorted(name for name in names if before.get(name) != after.get(name))


def _default_runtime_writers_are_inactive(database_dir: Path) -> bool:
    """Refuse apply unless daemon and MCP runtimes are proven inactive."""
    return _shared_runtime_is_inactive(Path(database_dir))


def _coverage_generation_complete(coverage: Mapping[str, Any], source_names: Iterable[str]) -> bool:
    sources = coverage.get("sources")
    if not isinstance(sources, Mapping):
        return False
    for source_name in source_names:
        entry = sources.get(source_name)
        if not isinstance(entry, Mapping):
            return False
        cursor = entry.get("cursor")
        if not isinstance(cursor, Mapping) or cursor.get("denominator_complete") is not True:
            return False
        native_sessions = int(entry.get("native_sessions") or 0)
        native_turns = int(entry.get("native_turns") or 0)
        denominator_sessions = int(cursor.get("denominator_observed_sessions") or 0)
        denominator_turns = int(cursor.get("denominator_turns") or 0)
        if (
            native_sessions != denominator_sessions
            or native_turns != denominator_turns
            or (native_sessions == 0) != (native_turns == 0)
            or str(entry.get("error") or "")
            or not str(entry.get("native_source_snapshot_hash") or "")
        ):
            return False
    return True


def _session_identity_reconciliation_plan(
    raw_db_path: Path,
    sources: Iterable[Any],
) -> dict[str, Any]:
    """Build exact append-only approvals required before native replay."""
    entries: list[dict[str, Any]] = []
    unresolved: list[str] = []
    try:
        conn = connect_readonly_sqlite(Path(raw_db_path))
    except (OSError, sqlite3.Error):
        raise AgentSourceRawReconciliationError("raw_database_unreadable") from None
    try:
        for source in sources:
            source_name = str(getattr(source, "name", "") or "")
            try:
                sessions = list(source.discover_sessions() or [])
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                ImportError,
                AttributeError,
                RuntimeError,
            ):
                unresolved.append(f"{source_name}:native_discovery_failed")
                continue
            for session in sessions:
                try:
                    canonical = canonicalize_session_info(session)
                    metadata = dict(getattr(canonical, "metadata", {}) or {})
                except (AttributeError, TypeError, ValueError):
                    unresolved.append(f"{source_name}:native_session_metadata_invalid")
                    continue
                if metadata.get("identity_reconciliation_required") is not True:
                    continue
                version = str(metadata.get("identity_contract_version") or "")
                artifact_id = str(metadata.get("source_artifact_id") or "")
                legacy_ids = metadata.get("legacy_canonical_session_ids")
                if not isinstance(legacy_ids, list):
                    legacy_ids = []
                try:
                    material = build_session_identity_receipt_material(
                        conn,
                        source_agent=source_name,
                        identity_contract_version=version,
                        canonical_session_id=str(canonical.session_id or ""),
                        legacy_session_ids=[
                            *list(getattr(canonical, "session_aliases", []) or []),
                            *legacy_ids,
                        ],
                        source_artifact_id=artifact_id,
                    )
                except (
                    RawSessionIdentityReconciliationError,
                    sqlite3.Error,
                    TypeError,
                    ValueError,
                ):
                    unresolved.append(f"{source_name}:session_identity_reconciliation_invalid")
                    continue
                if material is not None:
                    entries.append(material)
    finally:
        conn.close()
    unique = {_canonical_hash(entry): entry for entry in entries}
    ordered = [unique[key] for key in sorted(unique)]
    return {
        "schema_version": "mnemos.raw_session_identity_reconciliation_plan.v1",
        "mode": "append_only_exact_historical_event_set_approval",
        "required_receipt_count": len(ordered),
        "receipt_material_hash": _canonical_hash(ordered),
        "receipts": ordered,
        "unresolved": sorted(set(unresolved)),
        "ok": not unresolved,
    }


def _apply_session_identity_reconciliation_plan(
    raw_db_path: Path,
    *,
    plan: Mapping[str, Any],
    reviewed_plan_hash: str,
) -> dict[str, Any]:
    """Apply exact approvals in the Raw backup/rollback transaction boundary."""
    receipts = plan.get("receipts")
    if (
        plan.get("schema_version") != "mnemos.raw_session_identity_reconciliation_plan.v1"
        or plan.get("ok") is not True
        or not isinstance(receipts, list)
        or int(plan.get("required_receipt_count") or 0) != len(receipts)
        or plan.get("receipt_material_hash") != _canonical_hash(receipts)
    ):
        raise AgentSourceRawReconciliationError("session_identity_reconciliation_plan_invalid")
    if not receipts:
        return {
            "required_receipt_count": 0,
            "recorded_receipt_count": 0,
            "receipt_id_set_hash": _canonical_hash([]),
            "schema_created": False,
            "ok": True,
        }
    try:
        conn = sqlite3.connect(str(raw_db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            schema_created = not session_identity_reconciliation_table_exists(conn)
            initialize_session_identity_reconciliation_schema(conn)
            receipt_ids: list[str] = []
            for material in receipts:
                if not isinstance(material, Mapping):
                    raise RawSessionIdentityReconciliationError(
                        "raw_session_identity_reconciliation_material_mismatch"
                    )
                identities = json.loads(str(material.get("legacy_identity_set_json") or "[]"))
                current = build_session_identity_receipt_material(
                    conn,
                    source_agent=str(material.get("source_agent") or ""),
                    identity_contract_version=str(material.get("identity_contract_version") or ""),
                    canonical_session_id=str(material.get("canonical_session_id") or ""),
                    legacy_session_ids=(identities if isinstance(identities, list) else []),
                    source_artifact_id=str(material.get("source_artifact_id") or ""),
                )
                if current != dict(material):
                    raise RawSessionIdentityReconciliationError(
                        "raw_session_identity_reconciliation_preimage_drift"
                    )
                receipt_ids.append(
                    record_session_identity_reconciliation_receipt(
                        conn,
                        material=material,
                        plan_hash=reviewed_plan_hash,
                    )
                )
            validate_session_identity_reconciliation_schema(conn)
            conn.commit()
        except (
            sqlite3.Error,
            RawSessionIdentityReconciliationError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            KeyboardInterrupt,
        ):
            conn.rollback()
            raise
        finally:
            conn.close()
    except RawSessionIdentityReconciliationError as exc:
        raise AgentSourceRawReconciliationError(str(exc)) from None
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        raise AgentSourceRawReconciliationError(
            "session_identity_reconciliation_apply_failed"
        ) from None
    return {
        "required_receipt_count": len(receipts),
        "recorded_receipt_count": len(receipt_ids),
        "receipt_id_set_hash": _canonical_hash(sorted(receipt_ids)),
        "schema_created": schema_created,
        "ok": len(receipt_ids) == len(receipts),
    }


def _verify_session_identity_reconciliation_plan(
    raw_db_path: Path,
    plan: Mapping[str, Any],
) -> bool:
    receipts = plan.get("receipts")
    if (
        plan.get("schema_version") != "mnemos.raw_session_identity_reconciliation_plan.v1"
        or plan.get("ok") is not True
        or not isinstance(receipts, list)
        or int(plan.get("required_receipt_count") or 0) != len(receipts)
        or plan.get("receipt_material_hash") != _canonical_hash(receipts)
    ):
        return False
    if not receipts:
        return True
    try:
        with connect_readonly_sqlite(Path(raw_db_path)) as conn:
            validate_session_identity_reconciliation_schema(conn)
            for material in receipts:
                if not isinstance(material, Mapping):
                    return False
                identities = json.loads(str(material.get("legacy_identity_set_json") or "[]"))
                if not isinstance(identities, list):
                    return False
                current = build_session_identity_receipt_material(
                    conn,
                    source_agent=str(material.get("source_agent") or ""),
                    identity_contract_version=str(material.get("identity_contract_version") or ""),
                    canonical_session_id=str(material.get("canonical_session_id") or ""),
                    legacy_session_ids=identities,
                    source_artifact_id=str(material.get("source_artifact_id") or ""),
                )
                if current != dict(material) or not session_identity_receipt_allows(
                    conn,
                    source_agent=str(material.get("source_agent") or ""),
                    identity_contract_version=str(material.get("identity_contract_version") or ""),
                    canonical_session_id=str(material.get("canonical_session_id") or ""),
                    legacy_session_ids=identities,
                    source_artifact_id=str(material.get("source_artifact_id") or ""),
                ):
                    return False
    except (
        OSError,
        sqlite3.Error,
        RawSessionIdentityReconciliationError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False
    return True
