"""Object-level provenance migration for pre-activation predictive deliveries."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote

from core.cognitive.state_contract import canonical_json, sha256_json
from core.cognitive.state_schema import (
    PREDICTION_ENFORCEMENT_COMPONENT,
    inspect_cognitive_state_schema,
    prediction_enforcement_enabled,
    write_prediction_enforcement_marker,
)
from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive
from core.db_utils import render_sql, validate_sql_identifier
from core.ops.exclusive_file_lock import exclusive_file_lock


MIGRATION_SCHEMA_VERSION = "mnemos.prediction_history_migration.v1"
HISTORICAL_OBJECT_SCHEMA_VERSION = "mnemos.prediction_historical_object.v1"
RESTORE_MANIFEST_SCHEMA_VERSION = "mnemos.prediction_restore_manifest.v1"
REASON_CODE = "historical_unverifiable_prediction"
SOURCE_TABLE = "delivery_events.delivery_events"


@dataclass(frozen=True)
class PredictiveDeliveryProvenance:
    """Immutable provenance identity for one pre-activation delivery."""

    source_database_id: str
    source_database_path: str
    source_schema_fingerprint: str
    source_field_manifest: tuple[str, ...]
    event_id: str
    source_content_hash: str
    decision: str
    created_at: str
    migration_identity: str

    def identity(self) -> dict[str, Any]:
        """Return the fields that define the migration object identity."""

        return {
            "source_database_id": self.source_database_id,
            "source_table": SOURCE_TABLE,
            "source_primary_key": "event_id",
            "source_primary_key_value": self.event_id,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "source_content_hash": self.source_content_hash,
            "decision": self.decision,
            "created_at": self.created_at,
            "migration_identity": self.migration_identity,
        }

    def quarantine_payload(self) -> dict[str, Any]:
        """Build the exact inactive quarantine payload for this object."""

        return {
            "schema_version": HISTORICAL_OBJECT_SCHEMA_VERSION,
            "target_type": "prediction_record",
            "status": REASON_CODE,
            "reason_code": REASON_CODE,
            "source_database_id": self.source_database_id,
            "source_database_path": self.source_database_path,
            "source_table": SOURCE_TABLE,
            "source_primary_key": "event_id",
            "source_primary_key_value": self.event_id,
            "source_schema_fingerprint": self.source_schema_fingerprint,
            "source_field_manifest": list(self.source_field_manifest),
            "source_content_hash": self.source_content_hash,
            "source_created_at": self.created_at,
            "historical_decision": self.decision,
            "migration_identity": self.migration_identity,
            "active_eligible": False,
            "creates_prediction_record": False,
            "creates_outcome_measurement": False,
            "creates_terminal_state": False,
            "creates_calibration_input": False,
        }


@dataclass(frozen=True)
class PredictionHistoryInventory:
    """Reviewed object inventory bound to source schema and row hashes."""

    delivery_db: str
    source_database_id: str
    source_schema_fingerprint: str
    source_field_manifest: tuple[str, ...]
    objects: tuple[PredictiveDeliveryProvenance, ...]
    inventory_hash: str
    object_manifest_hash: str

    def report(self, *, target: Mapping[str, Any]) -> dict[str, Any]:
        """Render the read-only inventory report with target state."""

        decisions: dict[str, int] = {}
        for item in self.objects:
            decisions[item.decision] = decisions.get(item.decision, 0) + 1
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "mode": "dry_run",
            "inventory_hash": self.inventory_hash,
            "object_manifest_hash": self.object_manifest_hash,
            "total_objects": len(self.objects),
            "counts_by_decision": dict(sorted(decisions.items())),
            "source": {
                "database_path": self.delivery_db,
                "database_id": self.source_database_id,
                "table": "delivery_events",
                "primary_key": "event_id",
                "schema_fingerprint": self.source_schema_fingerprint,
                "field_manifest": list(self.source_field_manifest),
                "integrity_check": "ok",
            },
            "target": dict(target),
            "historical_policy": {
                "reason_code": REASON_CODE,
                "creates_prediction_record": False,
                "creates_outcome_measurement": False,
                "creates_terminal_state": False,
                "creates_calibration_input": False,
                "active_eligible": False,
            },
            "ok": True,
        }


def build_prediction_history_inventory(
    delivery_db: Path,
) -> PredictionHistoryInventory:
    """Hash every exact historical predictive delivery without writing."""

    path = Path(delivery_db).expanduser().resolve(strict=True)
    with _connect_read_only(path) as conn:
        if not _table_exists(conn, "delivery_events"):
            raise RuntimeError("prediction source table is missing")
        if str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("prediction source integrity check failed")
        schema_hash, fields = _schema_fingerprint(conn, "delivery_events")
        if "event_id" not in fields or "channel" not in fields:
            raise RuntimeError("prediction source schema lacks canonical fields")
        database_id = sha256_json(
            {
                "resolved_path": str(path),
                "schema_fingerprint": schema_hash,
                "application_id": int(conn.execute("PRAGMA application_id").fetchone()[0]),
                "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            }
        )
        cursor = conn.execute(
            "SELECT * FROM delivery_events WHERE channel=? ORDER BY event_id",
            ("predictive_push",),
        )
        columns = tuple(str(value[0]) for value in cursor.description or ())
        objects: list[PredictiveDeliveryProvenance] = []
        for raw in cursor:
            row = dict(zip(columns, raw))
            event_id = str(row.get("event_id") or "").strip()
            if not event_id:
                raise RuntimeError("historical predictive delivery has a blank key")
            normalized = {
                key: _canonical_cell(value) for key, value in sorted(row.items())
            }
            content_hash = sha256_json(normalized)
            identity = {
                "source_database_id": database_id,
                "source_table": SOURCE_TABLE,
                "source_primary_key_value": event_id,
                "source_schema_fingerprint": schema_hash,
                "source_content_hash": content_hash,
            }
            objects.append(
                PredictiveDeliveryProvenance(
                    source_database_id=database_id,
                    source_database_path=str(path),
                    source_schema_fingerprint=schema_hash,
                    source_field_manifest=fields,
                    event_id=event_id,
                    source_content_hash=content_hash,
                    decision=str(row.get("decision") or ""),
                    created_at=str(row.get("created_at") or ""),
                    migration_identity=(
                        "prediction-history-"
                        + sha256_json(identity).split(":", 1)[1][:32]
                    ),
                )
            )
    canonical_objects = tuple(objects)
    object_manifest_hash = _manifest_hash(item.identity() for item in canonical_objects)
    inventory_core = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "source_database_id": database_id,
        "source_schema_fingerprint": schema_hash,
        "source_field_manifest": list(fields),
        "object_manifest_hash": object_manifest_hash,
        "total_objects": len(canonical_objects),
    }
    return PredictionHistoryInventory(
        delivery_db=str(path),
        source_database_id=database_id,
        source_schema_fingerprint=schema_hash,
        source_field_manifest=fields,
        objects=canonical_objects,
        inventory_hash=sha256_json(inventory_core),
        object_manifest_hash=object_manifest_hash,
    )


def inspect_prediction_target(target_db: Path) -> dict[str, Any]:
    """Inspect canonical schema, activation, and active prediction counts."""

    path = Path(target_db).expanduser().resolve(strict=False)
    if not path.is_file():
        return {
            "path": str(path),
            "status": "not_initialized",
            "schema_classification": "absent",
            "activation_marker": False,
            "integrity_check": "not_run",
        }
    with _connect_read_only(path) as conn:
        state = inspect_cognitive_state_schema(conn)
        marker_row = conn.execute(
            "SELECT applied_at FROM mnemos_schema_registry WHERE component=?",
            (PREDICTION_ENFORCEMENT_COMPONENT,),
        ).fetchone()
        quarantine_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_migration_quarantine "
                "WHERE reason_code=?",
                (REASON_CODE,),
            ).fetchone()[0]
        )
        active_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_state_revisions "
                "WHERE object_type='prediction_record'"
            ).fetchone()[0]
        )
        return {
            "path": str(path),
            "status": "available",
            "schema_classification": state.classification,
            "migration_required": state.migration_required,
            "activation_marker": prediction_enforcement_enabled(conn),
            "activation_applied_at": str(marker_row[0]) if marker_row else "",
            "historical_quarantine_count": quarantine_count,
            "active_prediction_revision_count": active_count,
            "integrity_check": str(conn.execute("PRAGMA integrity_check").fetchone()[0]),
        }


def inspect_prediction_history_coverage(
    delivery_db: Path,
    target_db: Path,
) -> dict[str, Any]:
    """Compare exact source objects with their quarantine projections."""

    inventory = build_prediction_history_inventory(delivery_db)
    target = inspect_prediction_target(target_db)
    activation_applied_at = str(target.get("activation_applied_at") or "")
    linked_runtime_ids = _linked_runtime_prediction_ids(delivery_db)
    historical_objects = tuple(
        item
        for item in inventory.objects
        if item.event_id not in linked_runtime_ids
        and (
            not activation_applied_at
            or _timestamp_not_after(item.created_at, activation_applied_at)
        )
    )
    covered = 0
    unexpected = 0
    target_path = Path(target_db).expanduser().resolve(strict=False)
    if target_path.is_file():
        expected = {item.event_id: item for item in historical_objects}
        with _connect_read_only(target_path) as conn:
            rows = conn.execute(
                "SELECT source_key, payload_json, payload_hash "
                "FROM cognitive_state_migration_quarantine "
                "WHERE source_table=? AND reason_code=?",
                (SOURCE_TABLE, REASON_CODE),
            ).fetchall()
        actual = {str(row[0]): row for row in rows}
        for event_id, historical in expected.items():
            row = actual.get(event_id)
            if row is None:
                continue
            try:
                payload = json.loads(str(row[1]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            expected_payload = historical.quarantine_payload()
            if (
                isinstance(payload, Mapping)
                and dict(payload) == expected_payload
                and str(row[2]) == sha256_json(expected_payload)
            ):
                covered += 1
        unexpected = len(set(actual) - set(expected))
    ok = (
        target.get("schema_classification") == "canonical"
        and target.get("integrity_check") == "ok"
        and bool(target.get("activation_marker"))
        and int(target.get("active_prediction_revision_count", 0)) == 0
        and covered == len(historical_objects)
        and unexpected == 0
    )
    return {
        "historical_predictive_object_count": len(historical_objects),
        "historical_quarantine_count": covered,
        "historical_predictive_object_uncovered": len(historical_objects) - covered,
        "unexpected_historical_quarantine_count": unexpected,
        "inventory_hash": inventory.inventory_hash,
        "target": target,
        "ok": ok,
    }


def apply_prediction_history_migration(
    *,
    delivery_db: Path,
    target_db: Path,
    expected_inventory_hash: str,
    backup_dir: Path,
    database_dir: Path,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Quarantine the reviewed inventory and activate enforcement atomically."""

    if not expected_inventory_hash:
        raise ValueError("the reviewed inventory_hash is required")
    target = Path(target_db).expanduser().resolve(strict=True)
    root = Path(database_dir).expanduser().resolve(strict=False)
    if not daemon_check(root):
        raise RuntimeError("Mnemos daemon must be conclusively stopped before apply")
    with _exclusive_runtime_lock(root):
        inventory = build_prediction_history_inventory(delivery_db)
        if inventory.inventory_hash != expected_inventory_hash:
            raise RuntimeError("prediction source inventory drifted before apply")
        before = inspect_prediction_target(target)
        if (
            before.get("schema_classification") != "canonical"
            or before.get("integrity_check") != "ok"
            or int(before.get("active_prediction_revision_count", 0)) != 0
        ):
            raise RuntimeError("prediction target is not a zero-active canonical store")
        backup = _backup_database(target, Path(backup_dir))
        if failpoint:
            failpoint("after_backup")
        inserted = 0
        existing = 0
        manifest_path: Path | None = None
        post_snapshot = ""
        conn = sqlite3.connect(str(target), timeout=60)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            if inspect_cognitive_state_schema(conn).classification != "canonical":
                raise RuntimeError("prediction target schema drifted before transaction")
            for historical in inventory.objects:
                if _insert_historical_object(conn, historical):
                    inserted += 1
                else:
                    existing += 1
            if failpoint:
                failpoint("after_inventory")
            fresh = build_prediction_history_inventory(delivery_db)
            if fresh.inventory_hash != inventory.inventory_hash:
                raise RuntimeError("prediction source drifted immediately before commit")
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cognitive_state_revisions "
                    "WHERE object_type='prediction_record'"
                ).fetchone()[0]
            )
            if active:
                raise RuntimeError("historical migration cannot create active predictions")
            write_prediction_enforcement_marker(conn)
            if not prediction_enforcement_enabled(conn):
                raise RuntimeError("prediction activation marker did not verify")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("prediction target foreign-key verification failed")
            post_snapshot = _connection_snapshot_hash(conn)
            manifest_path = _write_restore_manifest(
                backup=backup,
                target=target,
                inventory=inventory,
                before=before,
                postapply_snapshot_hash=post_snapshot,
            )
            if failpoint:
                failpoint("before_commit")
            conn.commit()
        except BaseException:
            conn.rollback()
            # Keep the immutable pre-commit restore manifest as failed-attempt
            # evidence. Restore independently requires the exact post-apply
            # target snapshot, so a rolled-back attempt cannot be restored.
            raise
        finally:
            conn.close()
        after = inspect_prediction_target(target)
        if (
            after.get("integrity_check") != "ok"
            or not after.get("activation_marker")
            or int(after.get("active_prediction_revision_count", 0)) != 0
            or _database_snapshot_hash(target) != post_snapshot
        ):
            raise RuntimeError("prediction post-apply verification failed")
    report = inventory.report(target=before)
    report.update(
        {
            "mode": "apply",
            "applied": True,
            "inserted": inserted,
            "existing": existing,
            "before": before,
            "after": after,
            "activation_component": PREDICTION_ENFORCEMENT_COMPONENT,
            "backup": {
                **backup,
                "restore_manifest": str(manifest_path),
            },
            "ok": inserted + existing == len(inventory.objects),
        }
    )
    return report


def restore_prediction_backup(
    *,
    target_db: Path,
    restore_manifest: Path,
    database_dir: Path,
    daemon_check: Callable[[Path], bool] = runtime_writers_are_inactive,
) -> dict[str, Any]:
    """Restore an apply preimage after validating the signed manifest facts."""

    target = Path(target_db).expanduser().resolve(strict=True)
    root = Path(database_dir).expanduser().resolve(strict=False)
    manifest_path = Path(restore_manifest).expanduser().resolve(strict=True)
    manifest = _load_manifest(manifest_path)
    if Path(str(manifest["target_path"])).resolve() != target:
        raise RuntimeError("prediction restore target does not match manifest")
    if not daemon_check(root):
        raise RuntimeError("Mnemos daemon must be conclusively stopped before restore")
    with _exclusive_runtime_lock(root):
        backup = Path(str(manifest["backup_path"])).resolve(strict=True)
        if _file_sha256(backup) != manifest["backup_file_sha256"]:
            raise RuntimeError("prediction restore backup hash mismatch")
        if _database_integrity(backup) != "ok":
            raise RuntimeError("prediction restore backup integrity failed")
        if _database_snapshot_hash(backup) != manifest["target_preimage_snapshot_hash"]:
            raise RuntimeError("prediction restore backup snapshot mismatch")
        if _database_snapshot_hash(target) != manifest["target_postapply_snapshot_hash"]:
            raise RuntimeError("prediction restore target drifted after apply")
        with _connect_read_only(backup) as source:
            with sqlite3.connect(str(target), timeout=60) as destination:
                source.backup(destination)
        restored = _database_snapshot_hash(target)
        if restored != manifest["target_preimage_snapshot_hash"]:
            raise RuntimeError("prediction restore did not reproduce the preimage")
    return {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "mode": "restore",
        "target": str(target),
        "backup": str(backup),
        "restore_manifest": str(manifest_path),
        "inventory_hash": manifest["inventory_hash"],
        "object_manifest_hash": manifest["object_manifest_hash"],
        "integrity_check": _database_integrity(target),
        "snapshot_hash": restored,
        "ok": True,
    }


def _insert_historical_object(
    conn: sqlite3.Connection,
    historical: PredictiveDeliveryProvenance,
) -> bool:
    payload = historical.quarantine_payload()
    payload_hash = sha256_json(payload)
    quarantine_id = "cogquarantine-" + sha256_json(
        {
            "source_table": SOURCE_TABLE,
            "source_key": historical.event_id,
            "reason_code": REASON_CODE,
            "payload_hash": payload_hash,
        }
    ).split(":", 1)[1][:32]
    expected = (
        quarantine_id,
        canonical_json(list(historical.source_field_manifest)),
        canonical_json(payload),
        payload_hash,
    )
    row = conn.execute(
        "SELECT quarantine_id, field_manifest, payload_json, payload_hash "
        "FROM cognitive_state_migration_quarantine "
        "WHERE source_table=? AND source_key=? AND reason_code=?",
        (SOURCE_TABLE, historical.event_id, REASON_CODE),
    ).fetchone()
    if row is not None:
        if tuple(str(value) for value in row) != expected:
            raise RuntimeError("immutable historical prediction provenance conflict")
        return False
    conn.execute(
        "INSERT INTO cognitive_state_migration_quarantine ("
        "quarantine_id, source_table, source_key, reason_code, field_manifest, "
        "payload_json, payload_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            quarantine_id,
            SOURCE_TABLE,
            historical.event_id,
            REASON_CODE,
            expected[1],
            expected[2],
            payload_hash,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def _backup_database(source: Path, backup_dir: Path) -> dict[str, Any]:
    root = Path(backup_dir).expanduser().resolve(strict=False)
    if root.exists():
        raise RuntimeError("prediction backup directory must not already exist")
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = root / f"{source.stem}-before-prediction-history-{stamp}.db"
    with _connect_read_only(source) as source_conn:
        with sqlite3.connect(str(destination)) as destination_conn:
            source_conn.backup(destination_conn)
    destination.chmod(0o600)
    if _database_integrity(destination) != "ok":
        raise RuntimeError("prediction backup integrity failed")
    snapshot = _database_snapshot_hash(source)
    if _database_snapshot_hash(destination) != snapshot:
        raise RuntimeError("prediction backup snapshot mismatch")
    return {
        "path": str(destination),
        "integrity_check": "ok",
        "sha256": _file_sha256(destination),
        "size_bytes": destination.stat().st_size,
        "snapshot_hash": snapshot,
    }


def _write_restore_manifest(
    *,
    backup: Mapping[str, Any],
    target: Path,
    inventory: PredictionHistoryInventory,
    before: Mapping[str, Any],
    postapply_snapshot_hash: str,
) -> Path:
    payload = {
        "schema_version": RESTORE_MANIFEST_SCHEMA_VERSION,
        "target_path": str(target),
        "target_preimage_snapshot_hash": str(backup["snapshot_hash"]),
        "target_postapply_snapshot_hash": postapply_snapshot_hash,
        "backup_path": str(backup["path"]),
        "backup_file_sha256": str(backup["sha256"]),
        "backup_size_bytes": int(backup["size_bytes"]),
        "inventory_hash": inventory.inventory_hash,
        "object_manifest_hash": inventory.object_manifest_hash,
        "before_report_hash": sha256_json(dict(before)),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    path = Path(str(backup["path"]) + ".restore.json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("prediction restore manifest is invalid") from exc
    required = {
        "schema_version",
        "target_path",
        "target_preimage_snapshot_hash",
        "target_postapply_snapshot_hash",
        "backup_path",
        "backup_file_sha256",
        "backup_size_bytes",
        "inventory_hash",
        "object_manifest_hash",
        "before_report_hash",
        "created_at",
    }
    if (
        not isinstance(parsed, dict)
        or set(parsed) != required
        or parsed.get("schema_version") != RESTORE_MANIFEST_SCHEMA_VERSION
    ):
        raise RuntimeError("prediction restore manifest contract is invalid")
    return parsed


@contextmanager
def _exclusive_runtime_lock(database_dir: Path) -> Iterator[None]:
    with exclusive_file_lock(
        database_dir / ".prediction_history_migration.lock",
        unavailable_message="prediction history migration lock is already held",
    ):
        with exclusive_file_lock(
            database_dir / "daemon.pid",
            unavailable_message="Mnemos daemon started before prediction migration",
        ):
            yield


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(Path(path).resolve(strict=True)), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _schema_fingerprint(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[str, tuple[str, ...]]:
    safe_table = validate_sql_identifier(table)
    fields = tuple(
        str(row[1])
        for row in conn.execute(f'PRAGMA table_xinfo("{safe_table}")')
    )
    objects = [
        {"type": str(row[0]), "name": str(row[1]), "sql": " ".join(str(row[2]).split())}
        for row in conn.execute(
            "SELECT type, name, sql FROM sqlite_schema "
            "WHERE tbl_name=? AND type IN ('table','index','trigger') "
            "ORDER BY type, name",
            (table,),
        )
    ]
    return sha256_json({"fields": list(fields), "objects": objects}), fields


def _connection_snapshot_hash(conn: sqlite3.Connection) -> str:
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    digest = hashlib.sha256()
    for table in tables:
        safe_table = validate_sql_identifier(table)
        digest.update(safe_table.encode())
        for row in conn.execute(
            render_sql(
                "SELECT * FROM {table} ORDER BY rowid",
                identifiers={"table": safe_table},
            )
        ):
            digest.update(canonical_json([_canonical_cell(value) for value in row]).encode())
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _database_snapshot_hash(path: Path) -> str:
    with _connect_read_only(path) as conn:
        return _connection_snapshot_hash(conn)


def _database_integrity(path: Path) -> str:
    with _connect_read_only(path) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _manifest_hash(values: Iterator[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _canonical_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    if isinstance(value, (str, int, float)) or value is None:
        return value
    return str(value)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _timestamp_not_after(first: str, second: str) -> bool:
    try:
        left = datetime.fromisoformat(str(first).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(second).replace("Z", "+00:00"))
    except ValueError:
        return str(first) <= str(second)
    if left.tzinfo is None or right.tzinfo is None:
        return str(first) <= str(second)
    return left <= right


def _linked_runtime_prediction_ids(delivery_db: Path) -> set[str]:
    linked: set[str] = set()
    with _connect_read_only(Path(delivery_db)) as conn:
        for row in conn.execute(
            "SELECT event_id, metadata_json FROM delivery_events "
            "WHERE channel='predictive_push'"
        ):
            try:
                metadata = json.loads(str(row[1]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(metadata, Mapping) and isinstance(
                metadata.get("prediction_record"),
                Mapping,
            ):
                linked.add(str(row[0]))
    return linked
