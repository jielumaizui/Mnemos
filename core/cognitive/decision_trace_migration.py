"""Object-level provenance inventory and migration for legacy material actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator, Mapping, Sequence

from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.delivery_router import (
    resolve_delivery_db_path,
    verify_delivery_nonmaterial_row,
)
from core.cognitive.state_schema import (
    DECISION_TRACE_ENFORCEMENT_COMPONENT,
    decision_trace_enforcement_enabled,
    inspect_cognitive_state_schema,
    upgrade_canonical_v1_for_decision_trace_in_transaction,
    write_decision_trace_enforcement_marker,
)
from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive
from core.ops.action_ledger import verify_action_ledger_diagnostic_row
from core.utils import load_json_value
from core.cognitive.decision_trace_migration_runtime import (
    _connect_read_only,
    _database_integrity,
    _exclusive_migration_runtime_lock,
    _file_sha256,
    _safe_identifier,
    _sqlite_connection_snapshot_hash,
    _sqlite_snapshot_hash,
    _table_exists,
)


MIGRATION_SCHEMA_VERSION = "mnemos.decision_trace_history_migration.v1"
HISTORICAL_OBJECT_SCHEMA_VERSION = "mnemos.decision_trace_historical_object.v1"
RESTORE_MANIFEST_SCHEMA_VERSION = "mnemos.decision_trace_restore_manifest.v1"
REASON_CODE = "historical_incomplete"

_CANONICAL_SOURCE_DOMAIN_CONTRACTS: Mapping[str, Mapping[str, str]] = {
    "action_ledger": {
        "table": "action_ledger",
        "primary_key": "action_id",
        "created_at": "created_at",
        "evidence_column": "evidence_refs_json",
        "metadata_column": "verification_json",
        "direct_ref_column": "quality_decision_id",
    },
    "delivery_events": {
        "table": "delivery_events",
        "primary_key": "event_id",
        "created_at": "created_at",
        "evidence_column": "",
        "metadata_column": "metadata_json",
        "direct_ref_column": "trust_decision_id",
    },
    "formal_cognitive_mutations": {
        "table": "formal_cognitive_mutations",
        "primary_key": "event_id",
        "created_at": "created_at",
        "evidence_column": "evidence_refs",
        "metadata_column": "metadata_json",
        "direct_ref_column": "",
    },
}


@dataclass(frozen=True)
class SourceDomain:
    """Describe one immutable historical material-action source table."""

    domain: str
    path: Path
    table: str
    primary_key: str
    created_at: str
    evidence_column: str = ""
    metadata_column: str = ""
    direct_ref_column: str = ""


@dataclass(frozen=True)
class HistoricalObject:
    """Freeze the provenance and content identity of one historical object."""

    domain: str
    source_database_id: str
    source_database_path: str
    source_table: str
    source_primary_key: str
    source_primary_key_value: str
    source_schema_fingerprint: str
    source_field_manifest: tuple[str, ...]
    source_content_hash: str
    source_input_hash: str
    source_created_at: str
    provenance_refs: tuple[str, ...]
    typed_decision_provenance: Mapping[str, str]
    runtime_material_action: Mapping[str, str]
    source_binding: Mapping[str, str]
    migration_identity: str

    def source_identity(self) -> dict[str, Any]:
        """Return the canonical source identity included in inventory hashes."""

        return {
            "domain": self.domain,
            "source_database_id": self.source_database_id,
            "source_table": self.source_table,
            "source_primary_key": self.source_primary_key,
            "source_primary_key_value": self.source_primary_key_value,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "source_content_hash": self.source_content_hash,
            "source_created_at": self.source_created_at,
            "provenance_refs": list(self.provenance_refs),
            "typed_decision_provenance": dict(self.typed_decision_provenance),
            "migration_identity": self.migration_identity,
        }

    def quarantine_payload(self, *, canonical_link_status: str) -> dict[str, Any]:
        """Build a non-active quarantine payload without inventing cognition."""

        return {
            "schema_version": HISTORICAL_OBJECT_SCHEMA_VERSION,
            "target_type": "decision_trace",
            "status": REASON_CODE,
            "reason_code": REASON_CODE,
            "domain": self.domain,
            "source_database_id": self.source_database_id,
            "source_database_path": self.source_database_path,
            "source_table": self.source_table,
            "source_primary_key": self.source_primary_key,
            "source_primary_key_value": self.source_primary_key_value,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "source_field_manifest": list(self.source_field_manifest),
            "source_content_hash": self.source_content_hash,
            "source_created_at": self.source_created_at,
            "provenance_refs": list(self.provenance_refs),
            "typed_decision_provenance": dict(self.typed_decision_provenance),
            "canonical_link_status": canonical_link_status,
            "migration_identity": self.migration_identity,
            "admission_state": "historical_candidate",
            "active_eligible": False,
        }


@dataclass(frozen=True)
class DecisionTraceInventory:
    """Hold an exact three-domain inventory and its reviewed hashes."""

    domains: tuple[dict[str, Any], ...]
    objects: tuple[HistoricalObject, ...]
    inventory_hash: str
    object_manifest_hash: str

    def report(self, *, target: Mapping[str, Any]) -> dict[str, Any]:
        """Render a read-only migration report for review."""

        counts = {
            domain["domain"]: int(domain["row_count"])
            for domain in self.domains
        }
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "mode": "dry_run",
            "inventory_hash": self.inventory_hash,
            "object_manifest_hash": self.object_manifest_hash,
            "total_objects": len(self.objects),
            "counts": counts,
            "domains": [dict(value) for value in self.domains],
            "target": dict(target),
            "historical_policy": {
                "status": REASON_CODE,
                "creates_value_context": False,
                "creates_snapshot": False,
                "creates_decision_trace": False,
                "creates_action_command": False,
                "creates_terminal_effect": False,
                "active_eligible": False,
            },
            "ok": True,
        }


def default_source_domains(
    *,
    database_dir: Path,
    delivery_db_path: Path | None = None,
    trusted_push_db_path: Path | None = None,
) -> tuple[SourceDomain, ...]:
    """Return the canonical three historical provenance domains for one data root."""

    root = Path(database_dir).expanduser()
    return (
        SourceDomain(
            domain="action_ledger",
            path=root / "action_ledger.db",
            table="action_ledger",
            primary_key="action_id",
            created_at="created_at",
            evidence_column="evidence_refs_json",
            metadata_column="verification_json",
            direct_ref_column="quality_decision_id",
        ),
        SourceDomain(
            domain="delivery_events",
            path=Path(delivery_db_path or root / "delivery_events.db").expanduser(),
            table="delivery_events",
            primary_key="event_id",
            created_at="created_at",
            metadata_column="metadata_json",
            direct_ref_column="trust_decision_id",
        ),
        SourceDomain(
            domain="formal_cognitive_mutations",
            path=Path(trusted_push_db_path or root / "trusted_push.db").expanduser(),
            table="formal_cognitive_mutations",
            primary_key="event_id",
            created_at="created_at",
            evidence_column="evidence_refs",
            metadata_column="metadata_json",
        ),
    )


def configured_source_domains(
    *,
    config: Any,
    database_dir: Path | None = None,
    delivery_db_path: Path | None = None,
    trusted_push_db_path: Path | None = None,
) -> tuple[SourceDomain, ...]:
    """Resolve the three production domains from the canonical config owner."""

    from core.trust.config import load_trusted_push_config

    root = Path(database_dir or config.database_dir).expanduser()
    delivery = resolve_delivery_db_path(
        config=config,
        database_dir=root,
        explicit=delivery_db_path,
    )
    trusted = Path(
        trusted_push_db_path
        or load_trusted_push_config(config).db_path
    ).expanduser()
    return default_source_domains(
        database_dir=root,
        delivery_db_path=delivery,
        trusted_push_db_path=trusted,
    )


def build_decision_trace_inventory(
    domains: Sequence[SourceDomain],
) -> DecisionTraceInventory:
    """Read and hash every exact historical object without mutating any database."""

    _validate_source_domains(domains)
    domain_reports: list[dict[str, Any]] = []
    objects: list[HistoricalObject] = []
    for domain in domains:
        path = domain.path.resolve(strict=False)
        if not path.is_file():
            raise FileNotFoundError(f"decision-trace source database is missing: {path}")
        with _connect_read_only(path) as conn:
            if not _table_exists(conn, domain.table):
                raise RuntimeError(
                    f"decision-trace source table is missing: {domain.domain}.{domain.table}"
                )
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(
                    f"decision-trace source integrity failed: {domain.domain}"
                )
            schema_fingerprint, field_manifest = _schema_fingerprint(
                conn,
                domain.table,
            )
            database_id = _database_identity(
                conn,
                path=path,
                schema_fingerprint=schema_fingerprint,
            )
            domain_objects = tuple(
                _inventory_rows(
                    conn,
                    domain=domain,
                    database_id=database_id,
                    schema_fingerprint=schema_fingerprint,
                    field_manifest=field_manifest,
                )
            )
            source_row_count = int(
                conn.execute(
                    f'SELECT COUNT(*) FROM "{domain.table}"'  # nosec B608
                ).fetchone()[0]
            )
        objects.extend(domain_objects)
        keys = [value.source_primary_key_value for value in domain_objects]
        domain_reports.append(
            {
                "domain": domain.domain,
                "database_path": str(path),
                "database_id": database_id,
                "table": domain.table,
                "primary_key": domain.primary_key,
                "schema_fingerprint": schema_fingerprint,
                "field_manifest": list(field_manifest),
                "row_count": len(domain_objects),
                "source_row_count": source_row_count,
                "diagnostic_observation_count": (
                    source_row_count - len(domain_objects)
                    if domain.domain == "action_ledger"
                    else 0
                ),
                "nonmaterial_suppression_count": (
                    source_row_count - len(domain_objects)
                    if domain.domain == "delivery_events"
                    else 0
                ),
                "first_key": keys[0] if keys else "",
                "last_key": keys[-1] if keys else "",
                "integrity_check": "ok",
            }
        )
    canonical_objects = tuple(
        sorted(
            objects,
            key=lambda value: (
                value.domain,
                value.source_primary_key_value,
            ),
        )
    )
    object_manifest_hash = _manifest_hash(
        value.source_identity() for value in canonical_objects
    )
    inventory_payload = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "domains": [
            {
                key: value
                for key, value in report.items()
                if key not in {"database_path", "first_key", "last_key"}
            }
            for report in domain_reports
        ],
        "object_manifest_hash": object_manifest_hash,
        "total_objects": len(canonical_objects),
    }
    return DecisionTraceInventory(
        domains=tuple(domain_reports),
        objects=canonical_objects,
        inventory_hash=sha256_json(inventory_payload),
        object_manifest_hash=object_manifest_hash,
    )


def inspect_decision_trace_target(target_db: Path) -> dict[str, Any]:
    """Inspect target schema, activation, quarantine count, and integrity read-only."""

    path = Path(target_db).resolve(strict=False)
    if not path.is_file():
        return {
            "path": str(path),
            "status": "not_initialized",
            "schema_classification": "absent",
            "activation_marker": False,
            "integrity_check": "not_run",
        }
    with _connect_read_only(path) as conn:
        return _inspect_decision_trace_connection(conn, path=path)


def inspect_decision_trace_history_coverage(
    domains: Sequence[SourceDomain],
    target_db: Path,
) -> dict[str, Any]:
    """Compare exact source-object counts with target quarantine coverage."""

    _validate_source_domains(domains)
    expected: dict[str, int] = {}
    errors: list[str] = []
    initialized_sources = 0
    for domain in domains:
        path = domain.path.resolve(strict=False)
        source_name = f"{domain.domain}.{domain.table}"
        if not path.is_file():
            expected[source_name] = 0
            continue
        initialized_sources += 1
        with _connect_read_only(path) as conn:
            if not _table_exists(conn, domain.table):
                expected[source_name] = 0
                errors.append(f"source table is missing: {source_name}")
                continue
            table = _canonical_source_contract(domain)["table"]
            if domain.domain in {"action_ledger", "delivery_events"}:
                cursor = conn.execute(
                    f'SELECT * FROM "{table}"'  # nosec B608
                )
                columns = tuple(
                    str(value[0]) for value in (cursor.description or ())
                )
                expected[source_name] = sum(
                    not _excluded_nonmaterial_source_row(
                        domain,
                        dict(zip(columns, row)),
                    )
                    for row in cursor
                )
            else:
                expected[source_name] = int(
                    conn.execute(
                        f'SELECT COUNT(*) FROM "{table}"'  # nosec B608
                    ).fetchone()[0]
                )

    target = inspect_decision_trace_target(target_db)
    actual: dict[str, int] = {}
    target_path = Path(target_db).resolve(strict=False)
    if target_path.is_file():
        with _connect_read_only(target_path) as conn:
            if _table_exists(conn, "cognitive_state_migration_quarantine"):
                actual = {
                    str(row[0]): int(row[1])
                    for row in conn.execute(
                        "SELECT source_table, COUNT(*) "
                        "FROM cognitive_state_migration_quarantine "
                        "WHERE reason_code=? GROUP BY source_table",
                        (REASON_CODE,),
                    ).fetchall()
                }
    relevant_actual = {key: int(actual.get(key, 0)) for key in expected}
    unexpected = sorted(set(actual) - set(expected))
    coverage_matches = expected == relevant_actual and not unexpected
    target_verified = (
        target.get("status") == "available"
        and target.get("integrity_check") == "ok"
        and target.get("schema_classification") == "canonical"
        and not target.get("migration_required")
        and bool(target.get("activation_marker"))
    )
    return {
        "initialized_source_count": initialized_sources,
        "expected_by_source": expected,
        "covered_by_source": relevant_actual,
        "unexpected_source_tables": unexpected,
        "target": target,
        "coverage_matches": coverage_matches,
        "errors": errors,
        "ok": bool(target_verified and coverage_matches and not errors),
    }


def _inspect_decision_trace_connection(
    conn: sqlite3.Connection,
    *,
    path: Path,
) -> dict[str, Any]:
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    state = inspect_cognitive_state_schema(conn)
    marker = decision_trace_enforcement_enabled(conn)
    quarantine_count = (
        int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_migration_quarantine "
                "WHERE reason_code=?",
                (REASON_CODE,),
            ).fetchone()[0]
        )
        if _table_exists(conn, "cognitive_state_migration_quarantine")
        else 0
    )
    return {
        "path": str(path),
        "status": "available",
        "schema_classification": state.classification,
        "schema_version": state.registry_version,
        "ddl_hash": state.ddl_hash,
        "migration_required": state.migration_required,
        "activation_marker": marker,
        "historical_incomplete_count": quarantine_count,
        "integrity_check": integrity,
    }


def apply_decision_trace_history_migration(
    *,
    domains: Sequence[SourceDomain],
    target_db: Path,
    expected_inventory_hash: str,
    backup_dir: Path,
    database_dir: Path,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Quarantine one reviewed inventory and activate strict enforcement atomically."""

    if not expected_inventory_hash:
        raise ValueError("the reviewed inventory_hash is required")
    target = Path(target_db).resolve(strict=False)
    if not target.is_file():
        raise FileNotFoundError(f"decision-trace target database is missing: {target}")
    if not daemon_check(Path(database_dir)):
        raise RuntimeError("Mnemos daemon must be conclusively stopped before apply")
    with _exclusive_migration_runtime_lock(Path(database_dir)):
        inventory = build_decision_trace_inventory(domains)
        if inventory.inventory_hash != expected_inventory_hash:
            raise RuntimeError("decision-trace source inventory drifted before apply")
        before = inspect_decision_trace_target(target)
        if before["integrity_check"] != "ok":
            raise RuntimeError("decision-trace target integrity check failed")
        backup = _backup_database(target, Path(backup_dir))
        if failpoint:
            failpoint("after_backup")
        inserted = 0
        existing = 0
        linked = 0
        upgraded_counts: dict[str, int] = {}
        restore_manifest: dict[str, Any] | None = None
        restore_manifest_path: Path | None = None
        committed = False
        postapply_snapshot_hash = ""
        conn = sqlite3.connect(str(target), timeout=60)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("PRAGMA busy_timeout = 60000")
            conn.execute("BEGIN IMMEDIATE")
            state = inspect_cognitive_state_schema(conn)
            if state.classification == "canonical_v1_decision_trace_upgrade_required":
                upgraded_counts = upgrade_canonical_v1_for_decision_trace_in_transaction(
                    conn
                )
            elif state.classification != "canonical":
                raise RuntimeError(
                    "decision-trace target schema is not a recognized canonical version"
                )
            if failpoint:
                failpoint("after_schema")
            for historical in inventory.objects:
                link_status = _canonical_link_status(conn, historical)
                linked += int(link_status == "verified_existing")
                was_inserted = _insert_historical_object(
                    conn,
                    historical,
                    canonical_link_status=link_status,
                )
                inserted += int(was_inserted)
                existing += int(not was_inserted)
            if failpoint:
                failpoint("after_inventory")
            write_decision_trace_enforcement_marker(conn)
            if not decision_trace_enforcement_enabled(conn):
                raise RuntimeError("decision-trace activation marker did not verify")
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(
                    "decision-trace target foreign-key verification failed"
                )
            final_state = inspect_cognitive_state_schema(conn)
            if not final_state.ok:
                raise RuntimeError("decision-trace target schema is not canonical after apply")
            prepared_after = _inspect_decision_trace_connection(conn, path=target)
            postapply_snapshot_hash = _sqlite_connection_snapshot_hash(conn)
            restore_manifest = _write_restore_manifest(
                backup=backup,
                target=target,
                inventory=inventory,
                before=before,
                after=prepared_after,
                postapply_snapshot_hash=postapply_snapshot_hash,
            )
            restore_manifest_path = Path(str(restore_manifest["path"]))
            if failpoint:
                failpoint("after_restore_manifest")
            if failpoint:
                failpoint("before_commit")
            conn.commit()
            committed = True
        except BaseException:
            conn.rollback()
            if not committed and restore_manifest_path is not None:
                restore_manifest_path.unlink(missing_ok=True)
                _fsync_directory(restore_manifest_path.parent)
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.close()
        after = inspect_decision_trace_target(target)
        if (
            after["integrity_check"] != "ok"
            or not after["activation_marker"]
            or _sqlite_snapshot_hash(target) != postapply_snapshot_hash
        ):
            raise RuntimeError("decision-trace post-apply verification failed")
        if restore_manifest is None:
            raise RuntimeError("decision-trace restore manifest was not prepared")
        backup = {**backup, "restore_manifest": restore_manifest}
    report = inventory.report(target=before)
    report.update(
        {
            "mode": "apply",
            "applied": True,
            "inserted": inserted,
            "existing": existing,
            "linked_existing": linked,
            "backup": backup,
            "schema_upgrade_source_counts": upgraded_counts,
            "before": before,
            "after": after,
            "activation_component": DECISION_TRACE_ENFORCEMENT_COMPONENT,
            "ok": inserted + existing == len(inventory.objects),
        }
    )
    return report


def restore_decision_trace_backup(
    *,
    target_db: Path,
    restore_manifest: Path,
    database_dir: Path,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
) -> dict[str, Any]:
    """Restore a verified pre-apply snapshot while holding the migration lock."""

    with _exclusive_migration_runtime_lock(Path(database_dir)):
        return _restore_decision_trace_backup_locked(
            target_db=target_db,
            restore_manifest=restore_manifest,
            database_dir=database_dir,
            daemon_check=daemon_check,
        )


def _restore_decision_trace_backup_locked(
    *,
    target_db: Path,
    restore_manifest: Path,
    database_dir: Path,
    daemon_check: Callable[[Path], bool],
) -> dict[str, Any]:
    target = Path(target_db).resolve(strict=False)
    manifest_path = Path(restore_manifest).resolve(strict=True)
    manifest = _load_restore_manifest(manifest_path)
    if Path(str(manifest["target_path"])).resolve(strict=False) != target:
        raise RuntimeError("decision-trace restore target does not match its manifest")
    backup = Path(str(manifest["backup_path"])).resolve(strict=True)
    if not daemon_check(Path(database_dir)):
        raise RuntimeError("Mnemos daemon must be conclusively stopped before restore")
    if _file_sha256(backup) != manifest["backup_file_sha256"]:
        raise RuntimeError("decision-trace restore backup hash does not match manifest")
    if int(backup.stat().st_size) != int(manifest["backup_size_bytes"]):
        raise RuntimeError("decision-trace restore backup size does not match manifest")
    if _database_integrity(backup) != "ok":
        raise RuntimeError("decision-trace restore backup integrity failed")
    if _sqlite_snapshot_hash(backup) != manifest["target_preimage_snapshot_hash"]:
        raise RuntimeError("decision-trace restore backup snapshot does not match manifest")
    if not target.is_file():
        raise FileNotFoundError("decision-trace restore target is missing")
    if _sqlite_snapshot_hash(target) != manifest["target_postapply_snapshot_hash"]:
        raise RuntimeError(
            "decision-trace restore target drifted from the reviewed apply result"
        )
    with _connect_read_only(backup) as source:
        with sqlite3.connect(str(target), timeout=60) as destination:
            source.backup(destination)
    integrity = _database_integrity(target)
    if integrity != "ok":
        raise RuntimeError("decision-trace restored target integrity failed")
    restored_snapshot_hash = _sqlite_snapshot_hash(target)
    if restored_snapshot_hash != manifest["target_preimage_snapshot_hash"]:
        raise RuntimeError(
            "decision-trace restored target does not equal the verified preimage"
        )
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "mode": "restore",
        "target": str(target),
        "backup": str(backup),
        "restore_manifest": str(manifest_path),
        "inventory_hash": manifest["inventory_hash"],
        "object_manifest_hash": manifest["object_manifest_hash"],
        "integrity_check": integrity,
        "sha256": _file_sha256(target),
        "snapshot_hash": restored_snapshot_hash,
        "ok": True,
    }


def _inventory_rows(
    conn: sqlite3.Connection,
    *,
    domain: SourceDomain,
    database_id: str,
    schema_fingerprint: str,
    field_manifest: tuple[str, ...],
) -> Iterator[HistoricalObject]:
    primary = _safe_identifier(domain.primary_key, field_manifest)
    table = _canonical_source_contract(domain)["table"]
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY "{primary}"')  # nosec B608
    columns = tuple(str(value[0]) for value in cursor.description or ())
    for raw in cursor:
        row = dict(zip(columns, raw))
        if _excluded_nonmaterial_source_row(domain, row):
            continue
        primary_value = str(row.get(domain.primary_key) or "")
        if not primary_value:
            raise RuntimeError(f"blank primary key in {domain.domain}")
        normalized_row = {
            key: _canonical_cell(value)
            for key, value in sorted(row.items())
        }
        content_hash = sha256_json(normalized_row)
        source_input_hash = historical_source_input_hash(
            row,
            metadata_column=domain.metadata_column,
        )
        refs = set(_parse_string_array(row.get(domain.evidence_column)))
        direct_ref = str(row.get(domain.direct_ref_column) or "").strip()
        if direct_ref:
            refs.add(f"{domain.direct_ref_column}:{direct_ref}")
        typed = _typed_decision_provenance(row.get(domain.metadata_column))
        runtime_material_action = _runtime_material_action(
            row.get(domain.metadata_column)
        )
        refs.update(typed.values())
        identity = {
            "source_database_id": database_id,
            "source_table": domain.table,
            "source_primary_key": domain.primary_key,
            "source_primary_key_value": primary_value,
            "source_schema_fingerprint": schema_fingerprint,
            "source_content_hash": content_hash,
        }
        yield HistoricalObject(
            domain=domain.domain,
            source_database_id=database_id,
            source_database_path=str(domain.path.resolve(strict=False)),
            source_table=domain.table,
            source_primary_key=domain.primary_key,
            source_primary_key_value=primary_value,
            source_schema_fingerprint=schema_fingerprint,
            source_field_manifest=field_manifest,
            source_content_hash=content_hash,
            source_input_hash=source_input_hash,
            source_created_at=str(row.get(domain.created_at) or ""),
            provenance_refs=tuple(sorted(value for value in refs if value)),
            typed_decision_provenance=typed,
            runtime_material_action=runtime_material_action,
            source_binding={
                key: str(row.get(key) or "")
                for key in (
                    "action_type",
                    "action",
                    "target",
                    "target_ref",
                    "asset_kind",
                    "decision",
                    "quality_decision_id",
                    "channel",
                    "subject",
                )
                if key in row
            },
            migration_identity="decision-history-"
            + sha256_json(identity).split(":", 1)[1][:32],
        )


def _insert_historical_object(
    conn: sqlite3.Connection,
    historical: HistoricalObject,
    *,
    canonical_link_status: str,
) -> bool:
    payload = historical.quarantine_payload(
        canonical_link_status=canonical_link_status
    )
    payload_hash = sha256_json(payload)
    source_table = f"{historical.domain}.{historical.source_table}"
    source_key = historical.source_primary_key_value
    identity = {
        "source_table": source_table,
        "source_key": source_key,
        "reason_code": REASON_CODE,
        "payload_hash": payload_hash,
    }
    quarantine_id = "cogquarantine-" + sha256_json(identity).split(":", 1)[1][:32]
    expected = (
        quarantine_id,
        canonical_json(list(historical.source_field_manifest)),
        canonical_json(payload),
        payload_hash,
    )
    row = conn.execute(
        """
        SELECT quarantine_id, field_manifest, payload_json, payload_hash
        FROM cognitive_state_migration_quarantine
        WHERE source_table=? AND source_key=? AND reason_code=?
        """,
        (source_table, source_key, REASON_CODE),
    ).fetchone()
    if row is not None:
        if tuple(str(value) for value in row) != expected:
            raise RuntimeError("immutable historical decision provenance conflict")
        return False
    conn.execute(
        """
        INSERT INTO cognitive_state_migration_quarantine (
            quarantine_id, source_table, source_key, reason_code,
            field_manifest, payload_json, payload_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quarantine_id,
            source_table,
            source_key,
            REASON_CODE,
            expected[1],
            expected[2],
            payload_hash,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def _canonical_link_status(
    conn: sqlite3.Connection,
    historical: HistoricalObject,
) -> str:
    provenance = historical.typed_decision_provenance
    required = {
        "decision_revision_id",
        "decision_hash",
        "snapshot_revision_id",
        "snapshot_hash",
        "value_context_revision_id",
        "value_context_hash",
        "command_id",
        "action_id",
        "effect_id",
        "action_type",
        "owner",
        "executor_id",
        "target_ref",
        "input_hash",
        "source_domain",
        "source_table",
        "source_primary_key",
        "source_primary_key_value",
        "source_input_hash",
    }
    if set(provenance) != required:
        return "not_declared"
    expected_source = {
        "domain": historical.domain,
        "table": historical.source_table,
        "primary_key": historical.source_primary_key,
        "primary_key_value": historical.source_primary_key_value,
        "input_hash": historical.source_input_hash,
    }
    if {
        "domain": provenance["source_domain"],
        "table": provenance["source_table"],
        "primary_key": provenance["source_primary_key"],
        "primary_key_value": provenance["source_primary_key_value"],
        "input_hash": provenance["source_input_hash"],
    } != expected_source:
        return "declared_unresolvable"
    revisions: dict[str, tuple[str, Mapping[str, Any], str, str]] = {}
    for label, expected_type, hash_key in (
        ("decision", "decision_trace", "decision_hash"),
        ("snapshot", "cognitive_state_snapshot", "snapshot_hash"),
        ("value_context", "value_context", "value_context_hash"),
    ):
        revision_id = provenance[f"{label}_revision_id"]
        row = conn.execute(
            """
            SELECT object_type, payload_json, payload_hash, created_at
            FROM cognitive_state_revisions WHERE revision_id=?
            """,
            (revision_id,),
        ).fetchone()
        if row is None or str(row["object_type"]) != expected_type:
            return "declared_unresolvable"
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            return "declared_unresolvable"
        payload_hash = str(row["payload_hash"])
        claimed_hash = payload_hash
        if label == "snapshot":
            snapshot_payload = dict(payload)
            claimed_hash = str(snapshot_payload.pop("snapshot_hash", ""))
            if sha256_json(snapshot_payload) != claimed_hash:
                return "declared_unresolvable"
        if (
            claimed_hash != provenance[hash_key]
            or sha256_json(payload) != payload_hash
        ):
            return "declared_unresolvable"
        revisions[label] = (
            revision_id,
            payload,
            payload_hash,
            str(row["created_at"]),
        )
    decision = revisions["decision"][1]
    if (
        decision.get("snapshot_revision_id") != revisions["snapshot"][0]
        or decision.get("snapshot_hash") != provenance["snapshot_hash"]
        or decision.get("value_context_revision_id") != revisions["value_context"][0]
        or decision.get("value_context_hash") != revisions["value_context"][2]
        or not _timestamp_not_after(
            revisions["decision"][3],
            historical.source_created_at,
        )
    ):
        return "declared_unresolvable"

    action_matches = [
        dict(value)
        for value in decision.get("action_specs", ())
        if isinstance(value, Mapping)
        and value.get("action_id") == provenance["action_id"]
    ]
    if len(action_matches) != 1:
        return "declared_unresolvable"
    action = action_matches[0]
    expected_action = {
        "action_id": provenance["action_id"],
        "effect_id": provenance["effect_id"],
        "action_type": provenance["action_type"],
        "owner": provenance["owner"],
        "executor": provenance["executor_id"],
        "target_ref": provenance["target_ref"],
        "input_hash": provenance["input_hash"],
        "source_object": expected_source,
    }
    if any(action.get(key) != value for key, value in expected_action.items()):
        return "declared_unresolvable"
    if (
        provenance["action_id"] not in decision.get("action_refs", ())
        or provenance["effect_id"] not in decision.get("effect_refs", ())
    ):
        return "declared_unresolvable"

    command = conn.execute(
        """
        SELECT revision_id, consumer_id, command_type, payload_json,
               payload_hash, created_at
        FROM cognitive_state_outbox WHERE command_id=?
        """,
        (provenance["command_id"],),
    ).fetchone()
    if command is None:
        return "declared_unresolvable"
    try:
        command_payload = json.loads(str(command["payload_json"]))
    except json.JSONDecodeError:
        return "declared_unresolvable"
    command_payload_hash = str(command["payload_hash"])
    expected_consumer = (
        "material-action:"
        + provenance["owner"]
        + ":"
        + provenance["action_id"].removeprefix("material-action-")[:16]
    )
    command_identity = {
        "revision_id": provenance["decision_revision_id"],
        "consumer_id": expected_consumer,
        "command_type": "execute_material_action",
        "payload_hash": command_payload_hash,
    }
    expected_command_id = (
        "cogcmd-" + sha256_json(command_identity).split(":", 1)[1][:32]
    )
    if (
        provenance["command_id"] != expected_command_id
        or str(command["revision_id"]) != provenance["decision_revision_id"]
        or str(command["consumer_id"]) != expected_consumer
        or str(command["command_type"]) != "execute_material_action"
        or sha256_json(command_payload) != command_payload_hash
        or not _timestamp_not_after(
            str(command["created_at"]),
            historical.source_created_at,
        )
    ):
        return "declared_unresolvable"
    command_fields = {
        "decision_revision_id": provenance["decision_revision_id"],
        "decision_hash": provenance["decision_hash"],
        "snapshot_revision_id": provenance["snapshot_revision_id"],
        "snapshot_hash": provenance["snapshot_hash"],
        "value_context_revision_id": provenance["value_context_revision_id"],
        "value_context_hash": provenance["value_context_hash"],
        **expected_action,
    }
    if any(command_payload.get(key) != value for key, value in command_fields.items()):
        return "declared_unresolvable"

    receipt = conn.execute(
        """
        SELECT r.receipt_id, r.status, r.target_effect_id, r.before_hash,
               r.after_hash, r.evidence_refs, r.created_at,
               c.metadata AS consumption_metadata
        FROM cognitive_state_effect_receipts r
        JOIN cognitive_data_consumptions c
          ON c.consumption_id=r.consumption_id
        WHERE r.command_id=?
        """,
        (provenance["command_id"],),
    ).fetchone()
    if receipt is None:
        return "declared_unresolvable"
    try:
        parsed_receipt_refs = json.loads(str(receipt["evidence_refs"]))
    except (TypeError, json.JSONDecodeError):
        return "declared_unresolvable"
    if not isinstance(parsed_receipt_refs, list) or any(
        not isinstance(value, str) or not value
        for value in parsed_receipt_refs
    ):
        return "declared_unresolvable"
    receipt_refs = set(parsed_receipt_refs)
    try:
        consumption_metadata = json.loads(
            str(receipt["consumption_metadata"])
        )
    except (TypeError, json.JSONDecodeError):
        return "declared_unresolvable"
    if not isinstance(consumption_metadata, Mapping):
        return "declared_unresolvable"
    before_hash = str(receipt["before_hash"])
    after_hash = str(receipt["after_hash"])
    source_oracle_ref = (
        "target-oracle:source-object:"
        f"{historical.domain}:{historical.source_table}:"
        f"{historical.source_primary_key}:"
        f"{historical.source_primary_key_value}:"
        f"{historical.source_input_hash}"
    )
    required_receipt_refs = {
        f"material-command:{provenance['command_id']}",
        f"decision-revision:{provenance['decision_revision_id']}",
        f"material-effect:{provenance['effect_id']}",
        f"target-after:{after_hash}",
        source_oracle_ref,
    }
    if (
        str(receipt["status"]) != "committed"
        or str(consumption_metadata.get("terminal_reason_code") or "")
        or consumption_metadata.get("retry_exhausted") is not False
        or str(receipt["target_effect_id"]) != provenance["effect_id"]
        or not _canonical_sha256(before_hash)
        or not _canonical_sha256(after_hash)
        or not required_receipt_refs.issubset(receipt_refs)
        or not _timestamp_not_after(
            historical.source_created_at,
            str(receipt["created_at"]),
        )
    ):
        return "declared_unresolvable"
    return "verified_existing"


def _canonical_sha256(value: str) -> bool:
    return (
        value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _typed_decision_provenance(raw: Any) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(str(raw)) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    envelope = parsed.get("decision_trace_provenance")
    if not isinstance(envelope, Mapping) or envelope.get(
        "schema_version"
    ) != "mnemos.decision_trace_provenance.v1":
        return {}
    keys = (
        "decision_revision_id",
        "decision_hash",
        "snapshot_revision_id",
        "snapshot_hash",
        "value_context_revision_id",
        "value_context_hash",
        "command_id",
        "action_id",
        "effect_id",
        "action_type",
        "owner",
        "executor_id",
        "target_ref",
        "input_hash",
        "source_domain",
        "source_table",
        "source_primary_key",
        "source_primary_key_value",
        "source_input_hash",
    )
    result = {key: str(envelope.get(key) or "").strip() for key in keys}
    if any(not value for value in result.values()):
        return {}
    return result


def _runtime_material_action(raw: Any) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(str(raw)) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    material = parsed.get("material_action")
    required = (
        "command_id",
        "decision_revision_id",
        "action_id",
        "effect_id",
        "action_type",
        "owner",
        "executor_id",
        "target_ref",
        "input_hash",
    )
    if not isinstance(material, Mapping) or set(material) != set(required):
        return {}
    result = {key: str(material.get(key) or "").strip() for key in required}
    if any(not value for value in result.values()):
        return {}
    return result


def historical_source_input_hash(
    row: Mapping[str, Any],
    *,
    metadata_column: str,
) -> str:
    """Hash an exact source row while excluding only its provenance envelope."""

    normalized = {
        key: _canonical_cell(value)
        for key, value in sorted(row.items())
    }
    if metadata_column and metadata_column in row:
        raw_metadata = row.get(metadata_column)
        try:
            metadata = (
                json.loads(str(raw_metadata))
                if isinstance(raw_metadata, str)
                else dict(raw_metadata or {})
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = None
        if isinstance(metadata, Mapping):
            metadata_without_provenance = dict(metadata)
            metadata_without_provenance.pop("decision_trace_provenance", None)
            normalized[metadata_column] = canonical_json(
                metadata_without_provenance
            )
    return str(sha256_json(normalized))


def _timestamp_not_after(first: str, second: str) -> bool:
    try:
        first_value = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
        second_value = datetime.fromisoformat(str(second).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if first_value.tzinfo is None:
        first_value = first_value.replace(tzinfo=timezone.utc)
    if second_value.tzinfo is None:
        second_value = second_value.replace(tzinfo=timezone.utc)
    return first_value.astimezone(timezone.utc) <= second_value.astimezone(timezone.utc)


def _parse_string_array(raw: Any) -> tuple[str, ...]:
    if raw in (None, ""):
        return ()
    try:
        parsed = json.loads(str(raw)) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list) or any(
        not isinstance(value, str) or not value.strip() for value in parsed
    ):
        return ()
    return tuple(sorted(set(value.strip() for value in parsed)))


def _schema_fingerprint(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[str, tuple[str, ...]]:
    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "sql": " ".join(str(row[2] or "").split()),
        }
        for row in conn.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE name=? OR tbl_name=?
            ORDER BY type, name
            """,
            (table, table),
        ).fetchall()
    ]
    columns = [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM pragma_table_xinfo(?) ORDER BY cid",
            (table,),
        )
    ]
    foreign_keys = [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id, seq",
            (table,),
        )
    ]
    manifest = tuple(str(row[1]) for row in columns)
    return (
        sha256_json(
            {
                "table": table,
                "objects": objects,
                "columns": columns,
                "foreign_keys": foreign_keys,
            }
        ),
        manifest,
    )


def _database_identity(
    conn: sqlite3.Connection,
    *,
    path: Path,
    schema_fingerprint: str,
) -> str:
    return str(sha256_json(
        {
            "resolved_path": str(path),
            "application_id": int(conn.execute("PRAGMA application_id").fetchone()[0]),
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
            "schema_fingerprint": schema_fingerprint,
        }
    ))


def _canonical_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "sqlite_type": "blob",
            "sha256": "sha256:" + hashlib.sha256(value).hexdigest(),
            "length": len(value),
        }
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


def _manifest_hash(values: Iterator[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _backup_database(source: Path, backup_dir: Path) -> dict[str, Any]:
    destination_root = Path(backup_dir).expanduser().resolve(strict=False)
    destination_root_existed = destination_root.exists()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root.chmod(0o700)
    if not destination_root_existed:
        _fsync_directory(destination_root.parent)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = destination_root / (
        f"{source.stem}-before-decision-trace-history-{stamp}.db"
    )
    with _connect_read_only(source) as source_conn:
        with sqlite3.connect(str(destination)) as destination_conn:
            source_conn.backup(destination_conn)
            integrity = str(
                destination_conn.execute("PRAGMA integrity_check").fetchone()[0]
            )
    destination.chmod(0o600)
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    if integrity != "ok":
        raise RuntimeError("decision-trace target backup integrity failed")
    source_snapshot_hash = _sqlite_snapshot_hash(source)
    backup_snapshot_hash = _sqlite_snapshot_hash(destination)
    if backup_snapshot_hash != source_snapshot_hash:
        raise RuntimeError("decision-trace target backup snapshot mismatch")
    return {
        "path": str(destination),
        "integrity_check": integrity,
        "sha256": _file_sha256(destination),
        "size_bytes": destination.stat().st_size,
        "snapshot_hash": backup_snapshot_hash,
    }


def _write_restore_manifest(
    *,
    backup: Mapping[str, Any],
    target: Path,
    inventory: DecisionTraceInventory,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    postapply_snapshot_hash: str,
) -> dict[str, Any]:
    backup_path = Path(str(backup["path"])).resolve(strict=True)
    target_path = Path(target).resolve(strict=True)
    payload = {
        "schema_version": RESTORE_MANIFEST_SCHEMA_VERSION,
        "target_path": str(target_path),
        "target_preimage_snapshot_hash": str(backup["snapshot_hash"]),
        "target_postapply_snapshot_hash": str(postapply_snapshot_hash),
        "backup_path": str(backup_path),
        "backup_file_sha256": str(backup["sha256"]),
        "backup_size_bytes": int(backup["size_bytes"]),
        "inventory_hash": inventory.inventory_hash,
        "object_manifest_hash": inventory.object_manifest_hash,
        "before_report_hash": sha256_json(dict(before)),
        "after_report_hash": sha256_json(dict(after)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = backup_path.with_suffix(backup_path.suffix + ".restore.json")
    descriptor = os.open(
        manifest_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        manifest_path.unlink(missing_ok=True)
        _fsync_directory(manifest_path.parent)
        raise
    _fsync_directory(manifest_path.parent)
    return {
        "path": str(manifest_path),
        "sha256": _file_sha256(manifest_path),
        "schema_version": RESTORE_MANIFEST_SCHEMA_VERSION,
    }


def _fsync_file(path: Path) -> None:
    descriptor = os.open(Path(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        Path(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_restore_manifest(path: Path) -> dict[str, Any]:
    try:
        parsed = load_json_value(Path(path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("decision-trace restore manifest is invalid") from exc
    if not isinstance(parsed, dict) or parsed.get(
        "schema_version"
    ) != RESTORE_MANIFEST_SCHEMA_VERSION:
        raise RuntimeError("decision-trace restore manifest schema is invalid")
    required = {
        "target_path",
        "target_preimage_snapshot_hash",
        "target_postapply_snapshot_hash",
        "backup_path",
        "backup_file_sha256",
        "backup_size_bytes",
        "inventory_hash",
        "object_manifest_hash",
        "before_report_hash",
        "after_report_hash",
        "created_at",
    }
    if set(parsed) != required | {"schema_version"}:
        raise RuntimeError("decision-trace restore manifest fields are invalid")
    for key in (
        "target_preimage_snapshot_hash",
        "target_postapply_snapshot_hash",
        "backup_file_sha256",
        "inventory_hash",
        "object_manifest_hash",
        "before_report_hash",
        "after_report_hash",
    ):
        value = str(parsed[key])
        if not value.startswith("sha256:") or len(value) != 71:
            raise RuntimeError("decision-trace restore manifest hash is invalid")
    if int(parsed["backup_size_bytes"]) <= 0:
        raise RuntimeError("decision-trace restore manifest backup size is invalid")
    return parsed


def _validate_source_domains(domains: Sequence[SourceDomain]) -> None:
    names = [domain.domain for domain in domains]
    expected = set(_CANONICAL_SOURCE_DOMAIN_CONTRACTS)
    if len(names) != len(expected) or set(names) != expected:
        raise ValueError(
            "decision-trace migration requires each canonical source domain exactly once"
        )
    for domain in domains:
        _canonical_source_contract(domain)


def _canonical_source_contract(domain: SourceDomain) -> Mapping[str, str]:
    contract = _CANONICAL_SOURCE_DOMAIN_CONTRACTS.get(domain.domain)
    if contract is None:
        raise ValueError(
            f"unsupported decision-trace source domain: {domain.domain}"
        )
    actual = {
        "table": domain.table,
        "primary_key": domain.primary_key,
        "created_at": domain.created_at,
        "evidence_column": domain.evidence_column,
        "metadata_column": domain.metadata_column,
        "direct_ref_column": domain.direct_ref_column,
    }
    if actual != dict(contract):
        raise ValueError(
            f"non-canonical decision-trace source contract: {domain.domain}"
        )
    return contract


def _excluded_nonmaterial_source_row(
    domain: SourceDomain,
    row: Mapping[str, Any],
) -> bool:
    if domain.domain == "action_ledger":
        return verify_action_ledger_diagnostic_row(row)
    if domain.domain == "delivery_events":
        return verify_delivery_nonmaterial_row(row)
    return False
