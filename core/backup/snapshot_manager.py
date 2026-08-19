# -*- coding: utf-8 -*-
"""System-wide snapshot and restore manager for Mnemos.

The manager creates a verifiable manifest for config, SQLite databases, raw
vault, mnemos vault, action ledger, migration ledger, and module state.  A
dry-run manifest never writes user data.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SNAPSHOT_SCHEMA_VERSION = "mnemos.snapshot_manifest.v2"
RESTORE_STATUSES = {
    "planned",
    "blocked",
    "restoring",
    "verified",
    "failed",
    "partial_restored",
}
SNAPSHOT_SCOPES = {
    "config",
    "sqlite",
    "mnemos_vault",
    "raw_vault",
    "action_ledger",
    "migration_ledger",
    "module_state",
}
HIGH_RISK_ACTIONS_REQUIRING_SNAPSHOT = {
    "migration.apply",
    "distill.batch_update",
    "raw.purge",
    "wiki.rebuild",
    "auto_heal.apply",
    "module_toggle.apply",
    "data_delete.apply",
}
DATA_DELETE_REQUIRED_SCOPES = frozenset(
    {"sqlite", "mnemos_vault", "raw_vault", "action_ledger"}
)
_SNAPSHOT_ID_PATTERN = re.compile(r"^snap-[A-Za-z0-9._-]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def data_delete_operation_binding_hash(scope_kind: str, scope_value: str) -> str:
    """Bind one snapshot to an exact deletion selector without storing its value."""

    payload = json.dumps(
        {
            "contract": "mnemos.data_delete_snapshot_binding.v1",
            "scope_kind": str(scope_kind).strip().lower(),
            "scope_value": str(scope_value),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _copy_snapshot_payload(source: Path, target: Path, *, database: bool) -> None:
    """Copy one payload, using SQLite's online backup API for live databases."""

    target.parent.mkdir(parents=True, exist_ok=True)
    if database and _is_sqlite_file(source):
        with sqlite3.connect(str(source), timeout=30) as source_conn:
            with sqlite3.connect(str(target), timeout=30) as target_conn:
                source_conn.backup(target_conn)
                integrity = target_conn.execute("PRAGMA integrity_check").fetchone()
                if not integrity or str(integrity[0]).lower() != "ok":
                    raise sqlite3.DatabaseError("snapshot SQLite integrity check failed")
        return
    shutil.copy2(source, target)


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16] + "-" + path.name


def _config_path(config: Any) -> Path:
    path = getattr(config, "config_path", None)
    return Path(path) if path is not None else Path.home() / ".mnemos" / "configs" / "main.json"


def _mnemos_dir(config: Any) -> Path:
    value = getattr(config, "mnemos_dir", None) or getattr(config, "data_dir", None)
    return Path(value) if value is not None else Path.home() / ".mnemos"


def _database_dir(config: Any) -> Path:
    value = getattr(config, "database_dir", None)
    return Path(value) if value is not None else _mnemos_dir(config)


def _vault_dir(config: Any, name: str) -> Path | None:
    method = getattr(config, "vault_dir", None)
    if callable(method):
        try:
            return Path(method(name))
        except (KeyError, TypeError, ValueError):
            return None
    if name == "mnemos" and hasattr(config, "wiki_dir"):
        return Path(config.wiki_dir)
    if name == "raw" and hasattr(config, "obsidian_vault_path"):
        return Path(config.obsidian_vault_path)
    return None


@dataclass(frozen=True)
class SnapshotFileEntry:
    source_path: str
    snapshot_path: str
    kind: str
    size_bytes: int
    sha256: str
    privacy_policy: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotManifest:
    snapshot_id: str
    created_at: str
    reason: str
    trigger_action: str
    scopes: tuple[str, ...]
    file_entries: tuple[SnapshotFileEntry, ...]
    database_entries: tuple[SnapshotFileEntry, ...]
    sensitive_field_policy: str
    restore_preconditions: tuple[str, ...]
    action_ledger_ref: str
    migration_ledger_ref: str
    module_state_ref: str
    operation_binding_hash: str = ""
    retention_expires_at: str = ""
    retention_policy: str = ""
    manifest_path: str = ""
    dry_run: bool = False
    schema_version: str = SNAPSHOT_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.snapshot_id:
            errors.append("snapshot_id is required")
        if not self.reason:
            errors.append("reason is required")
        if not set(self.scopes) <= SNAPSHOT_SCOPES:
            errors.append("unknown snapshot scope")
        if not self.file_entries and not self.database_entries:
            errors.append("snapshot must include at least one file or database entry")
        if "secret" in json.dumps(self.as_dict(), ensure_ascii=False).lower():
            if self.sensitive_field_policy != "hash_or_path_only":
                errors.append("secret-like manifest content requires hash_or_path_only policy")
        if not self.restore_preconditions:
            errors.append("restore_preconditions required")
        if self.trigger_action == "data_delete.apply":
            if self.schema_version != SNAPSHOT_SCHEMA_VERSION:
                errors.append("data delete snapshot schema must be current")
            if not self.operation_binding_hash.startswith("sha256:"):
                errors.append("data delete operation binding is required")
            if not self.retention_expires_at:
                errors.append("data delete retention expiry is required")
            if self.retention_policy != "retain_until_expiry_then_explicit_prune":
                errors.append("data delete retention policy is invalid")
            if not DATA_DELETE_REQUIRED_SCOPES <= set(self.scopes):
                errors.append("data delete snapshot scopes are incomplete")
            if self.dry_run:
                errors.append("data delete snapshot cannot be dry-run")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RestorePlan:
    snapshot_id: str
    status: str
    operations: tuple[str, ...]
    conflicts: tuple[str, ...]
    preconditions: tuple[str, ...]
    manifest_path: str
    schema_version: str = SNAPSHOT_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MnemosSnapshotManager:
    """Create, list, plan restore, apply restore, and verify snapshots."""

    def __init__(self, config: Any):
        self.config = config
        self.root_dir = _mnemos_dir(config) / "backups" / "snapshots"

    def _iter_scope_paths(self, scopes: Sequence[str]) -> Iterable[tuple[Path, str, str]]:
        if "config" in scopes:
            path = _config_path(self.config)
            if path.exists():
                yield path, "config", "secret_redacted_copy"
        db_dir = _database_dir(self.config)
        if "sqlite" in scopes:
            sqlite_roots = (db_dir, _mnemos_dir(self.config))
            seen_sqlite: set[str] = set()
            for sqlite_root in sqlite_roots:
                if not sqlite_root.exists():
                    continue
                for db_path in sorted(sqlite_root.glob("*.db")):
                    normalized = str(db_path.resolve(strict=False))
                    if normalized in seen_sqlite:
                        continue
                    seen_sqlite.add(normalized)
                    yield db_path, "sqlite", "sqlite_backup_api_or_hash_copy"
        if "action_ledger" in scopes:
            path = db_dir / "action_ledger.db"
            if path.exists():
                yield path, "action_ledger", "secret_redacted_copy"
        if "migration_ledger" in scopes:
            path = _mnemos_dir(self.config) / "migrations.db"
            if path.exists():
                yield path, "migration_ledger", "secret_redacted_copy"
        if "module_state" in scopes:
            for name in ("module_toggles.json", "daemon_heartbeat.json"):
                path = db_dir / name
                if path.exists():
                    yield path, "module_state", "hash_and_copy"
        if "mnemos_vault" in scopes:
            vault = _vault_dir(self.config, "mnemos")
            if vault and vault.exists():
                for md_file in sorted(vault.rglob("*.md")):
                    yield md_file, "mnemos_vault", "hash_and_copy"
        if "raw_vault" in scopes:
            vault = _vault_dir(self.config, "raw")
            if vault and vault.exists():
                for raw_file in sorted(vault.rglob("*.md")):
                    yield raw_file, "raw_vault", "hash_and_copy"

    def create(
        self,
        *,
        reason: str,
        trigger_action: str = "manual",
        scopes: Sequence[str] | None = None,
        dry_run: bool = False,
        operation_binding_hash: str = "",
        retention_expires_at: str = "",
        retention_policy: str = "",
    ) -> SnapshotManifest:
        chosen_scopes = tuple(scopes or sorted(SNAPSHOT_SCOPES))
        unknown_scopes = set(chosen_scopes) - SNAPSHOT_SCOPES
        if unknown_scopes:
            raise ValueError(f"unknown snapshot scopes: {sorted(unknown_scopes)}")
        snapshot_id = (
            f"snap-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        snapshot_dir = self.root_dir / snapshot_id
        entries: list[SnapshotFileEntry] = []
        db_entries: list[SnapshotFileEntry] = []
        seen_sources: set[str] = set()
        for source, kind, privacy_policy in self._iter_scope_paths(chosen_scopes):
            if not source.is_file():
                continue
            normalized_source = str(source.resolve(strict=False))
            if normalized_source in seen_sources:
                continue
            seen_sources.add(normalized_source)
            snapshot_rel = str(Path("files") / kind / _safe_rel(source, Path.home()))
            snapshot_payload = snapshot_dir / snapshot_rel
            if not dry_run:
                _copy_snapshot_payload(
                    source,
                    snapshot_payload,
                    database=kind in {"sqlite", "action_ledger", "migration_ledger"},
                )
            measured = source if dry_run else snapshot_payload
            size = measured.stat().st_size
            digest = _sha256_file(measured)
            entry = SnapshotFileEntry(
                source_path=str(source),
                snapshot_path=snapshot_rel,
                kind=kind,
                size_bytes=size,
                sha256=digest,
                privacy_policy=privacy_policy,
            )
            if kind in {"sqlite", "action_ledger", "migration_ledger"}:
                db_entries.append(entry)
            else:
                entries.append(entry)
        manifest_path = snapshot_dir / "manifest.json"
        manifest = SnapshotManifest(
            snapshot_id=snapshot_id,
            created_at=_now_iso(),
            reason=reason,
            trigger_action=trigger_action,
            scopes=chosen_scopes,
            file_entries=tuple(entries),
            database_entries=tuple(db_entries),
            sensitive_field_policy="hash_or_path_only",
            restore_preconditions=(
                "restore plan must be reviewed before apply",
                "newer destination files are reported as conflicts",
                "health and verify_installation must run after restore",
            ),
            action_ledger_ref=str(_database_dir(self.config) / "action_ledger.db"),
            migration_ledger_ref=str(_mnemos_dir(self.config) / "migrations.db"),
            module_state_ref=str(_database_dir(self.config)),
            operation_binding_hash=str(operation_binding_hash),
            retention_expires_at=str(retention_expires_at),
            retention_policy=str(retention_policy),
            manifest_path=str(manifest_path) if not dry_run else "",
            dry_run=dry_run,
        )
        validation_errors = manifest.validate()
        if validation_errors:
            if not dry_run and snapshot_dir.exists():
                shutil.rmtree(snapshot_dir)
            raise ValueError("; ".join(validation_errors))
        if not dry_run:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            manifest_bytes = json.dumps(
                manifest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8")
            manifest_path.write_bytes(manifest_bytes)
            (snapshot_dir / "manifest.sha256").write_text(
                hashlib.sha256(manifest_bytes).hexdigest() + "\n",
                encoding="ascii",
            )
            from core.ops.runtime_flow_telemetry import record_runtime_produced

            record_runtime_produced(
                "snapshot_manifest_to_restore_plan",
                source="core/backup/snapshot_manager.py",
                item_id=snapshot_id,
                intended_consumers=["core/backup/snapshot_manager.py:restore_plan"],
                metadata={"transition": "snapshot_manifest_committed"},
                config_or_path=_database_dir(self.config),
            )
        return manifest

    def create_data_delete_snapshot(
        self,
        *,
        scope_kind: str,
        scope_value: str,
        retention_days: int = 30,
    ) -> SnapshotManifest:
        """Create a retained snapshot bound to one exact data-delete operation."""

        days = int(retention_days)
        if days < 1:
            raise ValueError("data delete snapshot retention_days must be at least 1")
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        return self.create(
            reason="pre-data-delete retained recovery point",
            trigger_action="data_delete.apply",
            scopes=tuple(sorted(SNAPSHOT_SCOPES)),
            dry_run=False,
            operation_binding_hash=data_delete_operation_binding_hash(
                scope_kind, scope_value
            ),
            retention_expires_at=expires_at.isoformat(),
            retention_policy="retain_until_expiry_then_explicit_prune",
        )

    def verify_data_delete_snapshot(
        self,
        snapshot_id: str,
        *,
        scope_kind: str,
        scope_value: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Verify binding, retention, manifest, payload, and SQLite integrity."""

        normalized_id = str(snapshot_id).strip()
        errors: list[str] = []
        if not _SNAPSHOT_ID_PATTERN.fullmatch(normalized_id):
            errors.append("snapshot_id_invalid")
            return {
                "valid": False,
                "status": "blocked",
                "snapshot_id_hash": hashlib.sha256(
                    normalized_id.encode("utf-8")
                ).hexdigest(),
                "retention_status": "unknown",
                "retention_expires_at": "",
                "payload_count": 0,
                "errors": errors,
            }
        try:
            payload, manifest_path = self._load_manifest(normalized_id)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            errors.append("snapshot_manifest_missing_or_invalid")
            return {
                "valid": False,
                "status": "blocked",
                "snapshot_id_hash": hashlib.sha256(
                    normalized_id.encode("utf-8")
                ).hexdigest(),
                "retention_status": "unknown",
                "retention_expires_at": "",
                "payload_count": 0,
                "errors": errors,
            }

        checksum_path = manifest_path.parent / "manifest.sha256"
        if not checksum_path.is_file():
            errors.append("manifest_checksum_missing")
        else:
            expected_manifest_hash = checksum_path.read_text(encoding="ascii").strip()
            if _sha256_file(manifest_path) != expected_manifest_hash:
                errors.append("manifest_checksum_mismatch")
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            errors.append("snapshot_schema_not_current")
        if payload.get("snapshot_id") != normalized_id:
            errors.append("snapshot_id_manifest_mismatch")
        if payload.get("trigger_action") != "data_delete.apply":
            errors.append("snapshot_trigger_action_mismatch")
        if payload.get("dry_run") is not False:
            errors.append("snapshot_is_dry_run")
        expected_binding = data_delete_operation_binding_hash(scope_kind, scope_value)
        if payload.get("operation_binding_hash") != expected_binding:
            errors.append("operation_binding_mismatch")
        if not DATA_DELETE_REQUIRED_SCOPES <= set(payload.get("scopes") or ()):
            errors.append("snapshot_scope_coverage_incomplete")

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expires_at_text = str(payload.get("retention_expires_at") or "")
        retention_status = "unknown"
        try:
            expires_at = _parse_utc(expires_at_text)
            retention_status = "retained_until" if current < expires_at else "expired"
            if retention_status == "expired":
                errors.append("snapshot_retention_expired")
        except (TypeError, ValueError):
            errors.append("snapshot_retention_invalid")
        if payload.get("retention_policy") != "retain_until_expiry_then_explicit_prune":
            errors.append("snapshot_retention_policy_invalid")

        all_entries = list(payload.get("file_entries") or ()) + list(
            payload.get("database_entries") or ()
        )
        snapshot_root = manifest_path.parent.resolve(strict=False)
        for entry in all_entries:
            relative = Path(str(entry.get("snapshot_path") or ""))
            candidate = (snapshot_root / relative).resolve(strict=False)
            try:
                candidate.relative_to(snapshot_root)
            except ValueError:
                errors.append("payload_path_escape")
                continue
            if not candidate.is_file():
                errors.append("payload_missing")
                continue
            if candidate.stat().st_size != int(entry.get("size_bytes") or -1):
                errors.append("payload_size_mismatch")
            if _sha256_file(candidate) != str(entry.get("sha256") or ""):
                errors.append("payload_checksum_mismatch")
            if entry in (payload.get("database_entries") or ()) and _is_sqlite_file(candidate):
                try:
                    uri = f"file:{candidate}?mode=ro"
                    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                        integrity = conn.execute("PRAGMA integrity_check").fetchone()
                    if not integrity or str(integrity[0]).lower() != "ok":
                        errors.append("sqlite_integrity_failed")
                except sqlite3.Error:
                    errors.append("sqlite_integrity_failed")
        errors = sorted(set(errors))
        return {
            "valid": not errors,
            "status": "verified" if not errors else "blocked",
            "snapshot_id_hash": hashlib.sha256(
                normalized_id.encode("utf-8")
            ).hexdigest(),
            "snapshot_created_at": str(payload.get("created_at") or ""),
            "manifest_sha256": _sha256_file(manifest_path),
            "retention_status": retention_status,
            "retention_expires_at": expires_at_text,
            "payload_count": len(all_entries),
            "errors": errors,
        }

    def prune_expired_data_delete_snapshots(
        self,
        *,
        now: datetime | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        """Preview or explicitly remove only expired data-delete snapshots."""

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        candidates: list[str] = []
        deleted: list[str] = []
        if self.root_dir.exists():
            for manifest_path in sorted(self.root_dir.glob("*/manifest.json")):
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if payload.get("trigger_action") != "data_delete.apply":
                        continue
                    if current < _parse_utc(str(payload.get("retention_expires_at") or "")):
                        continue
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue
                snapshot_id = manifest_path.parent.name
                if not _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
                    continue
                candidates.append(snapshot_id)
                if apply:
                    shutil.rmtree(manifest_path.parent)
                    deleted.append(snapshot_id)
        return {
            "status": "applied" if apply else "planned",
            "candidate_snapshot_ids": candidates,
            "deleted_snapshot_ids": deleted,
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        if not self.root_dir.exists():
            return []
        result: list[dict[str, Any]] = []
        for manifest_path in sorted(self.root_dir.glob("*/manifest.json"), reverse=True):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            result.append(
                {
                    "snapshot_id": payload.get("snapshot_id"),
                    "created_at": payload.get("created_at"),
                    "reason": payload.get("reason"),
                    "manifest_path": str(manifest_path),
                    "entries": len(payload.get("file_entries") or [])
                    + len(payload.get("database_entries") or []),
                }
            )
        return result

    def _load_manifest(self, snapshot_id: str) -> tuple[dict[str, Any], Path]:
        normalized_id = str(snapshot_id).strip()
        if normalized_id == "latest":
            snapshots = self.list_snapshots()
            if not snapshots:
                raise FileNotFoundError("no snapshots found")
            normalized_id = str(snapshots[0]["snapshot_id"])
        if not _SNAPSHOT_ID_PATTERN.fullmatch(normalized_id):
            raise ValueError("invalid snapshot_id")
        manifest_path = self.root_dir / normalized_id / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return payload, manifest_path

    def _record_restore_plan(self, plan: RestorePlan) -> RestorePlan:
        from core.ops.runtime_flow_lookup import find_produced_event
        from core.ops.runtime_flow_telemetry import record_runtime_consumed

        ledger_path = _database_dir(self.config) / "producer_consumer_ledger.db"
        if not ledger_path.is_file():
            return plan
        production = find_produced_event(
            ledger_path,
            "snapshot_manifest_to_restore_plan",
            item_id=plan.snapshot_id,
            metadata_match={"transition": "snapshot_manifest_committed"},
        )
        if production is None:
            return plan
        record_runtime_consumed(
            "snapshot_manifest_to_restore_plan",
            source="core/backup/snapshot_manager.py:restore_plan",
            item_id=plan.snapshot_id,
            production_event_id=production["event_id"],
            metadata={
                "transition": "restore_plan_built",
                "status": plan.status,
                "conflict_codes": list(plan.conflicts),
            },
            config_or_path=_database_dir(self.config),
        )
        return plan

    def restore_plan(self, snapshot_id: str) -> RestorePlan:
        normalized_id = str(snapshot_id).strip()
        if normalized_id != "latest" and not _SNAPSHOT_ID_PATTERN.fullmatch(
            normalized_id
        ):
            raise ValueError("invalid snapshot_id")
        try:
            payload, manifest_path = self._load_manifest(normalized_id)
        except FileNotFoundError:
            return self._record_restore_plan(
                RestorePlan(
                    snapshot_id=normalized_id,
                    status="blocked",
                    operations=(),
                    conflicts=("snapshot_manifest_missing",),
                    preconditions=("restore requires an intact snapshot manifest",),
                    manifest_path=str(
                        self.root_dir / normalized_id / "manifest.json"
                    ),
                )
            )
        except json.JSONDecodeError:
            return self._record_restore_plan(
                RestorePlan(
                    snapshot_id=normalized_id,
                    status="blocked",
                    operations=(),
                    conflicts=("snapshot_manifest_invalid",),
                    preconditions=("restore requires a valid snapshot manifest",),
                    manifest_path=str(
                        self.root_dir / normalized_id / "manifest.json"
                    ),
                )
            )
        operations: list[str] = []
        conflicts: list[str] = []
        for entry in (payload.get("file_entries") or []) + (payload.get("database_entries") or []):
            source_path = Path(entry["source_path"])
            snapshot_path = manifest_path.parent / entry["snapshot_path"]
            if not snapshot_path.exists():
                conflicts.append(f"missing snapshot payload: {entry['snapshot_path']}")
                continue
            if source_path.exists() and _sha256_file(source_path) != entry["sha256"]:
                conflicts.append(f"destination changed: {source_path}")
            operations.append(f"restore {entry['kind']}: {source_path}")
        status = "blocked" if conflicts else "planned"
        plan = RestorePlan(
            snapshot_id=str(payload.get("snapshot_id")),
            status=status,
            operations=tuple(operations),
            conflicts=tuple(conflicts),
            preconditions=tuple(payload.get("restore_preconditions") or ()),
            manifest_path=str(manifest_path),
        )
        return self._record_restore_plan(plan)

    def restore_apply(self, snapshot_id: str, *, allow_conflicts: bool = False) -> RestorePlan:
        plan = self.restore_plan(snapshot_id)
        if plan.conflicts and not allow_conflicts:
            return plan
        payload, manifest_path = self._load_manifest(snapshot_id)
        errors: list[str] = []
        for entry in (payload.get("file_entries") or []) + (payload.get("database_entries") or []):
            source_path = Path(entry["source_path"])
            snapshot_path = manifest_path.parent / entry["snapshot_path"]
            try:
                source_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snapshot_path, source_path)
            except OSError as exc:
                errors.append(f"{source_path}: {exc}")
        return RestorePlan(
            snapshot_id=plan.snapshot_id,
            status="failed" if errors else "verified",
            operations=plan.operations,
            conflicts=tuple(errors),
            preconditions=plan.preconditions,
            manifest_path=plan.manifest_path,
        )

    def restore_verify(self, snapshot_id: str) -> RestorePlan:
        payload, manifest_path = self._load_manifest(snapshot_id)
        conflicts: list[str] = []
        operations: list[str] = []
        for entry in (payload.get("file_entries") or []) + (payload.get("database_entries") or []):
            source_path = Path(entry["source_path"])
            operations.append(f"verify {entry['kind']}: {source_path}")
            if not source_path.exists():
                conflicts.append(f"missing restored file: {source_path}")
            elif _sha256_file(source_path) != entry["sha256"]:
                conflicts.append(f"checksum mismatch: {source_path}")
        return RestorePlan(
            snapshot_id=str(payload.get("snapshot_id")),
            status="failed" if conflicts else "verified",
            operations=tuple(operations),
            conflicts=tuple(conflicts),
            preconditions=tuple(payload.get("restore_preconditions") or ()),
            manifest_path=str(manifest_path),
        )


def audit_backup_recovery_contract(*, strict: bool = False) -> list[str]:
    errors: list[str] = []
    required_scopes = {
        "config",
        "sqlite",
        "mnemos_vault",
        "raw_vault",
        "action_ledger",
        "migration_ledger",
        "module_state",
    }
    if not required_scopes <= SNAPSHOT_SCOPES:
        errors.append(f"missing snapshot scopes: {sorted(required_scopes - SNAPSHOT_SCOPES)}")
    required_actions = {
        "migration.apply",
        "raw.purge",
        "wiki.rebuild",
        "module_toggle.apply",
        "data_delete.apply",
    }
    if not required_actions <= HIGH_RISK_ACTIONS_REQUIRING_SNAPSHOT:
        missing = sorted(required_actions - HIGH_RISK_ACTIONS_REQUIRING_SNAPSHOT)
        errors.append(f"missing high-risk snapshot policies: {missing}")
    sample = SnapshotManifest(
        snapshot_id="snap-contract",
        created_at=_now_iso(),
        reason="contract-test",
        trigger_action="audit",
        scopes=("config",),
        file_entries=(
            SnapshotFileEntry(
                source_path="fixtures/configs/main.json",
                snapshot_path="files/config/main.json",
                kind="config",
                size_bytes=1,
                sha256="0" * 64,
                privacy_policy="secret_redacted_copy",
            ),
        ),
        database_entries=(),
        sensitive_field_policy="hash_or_path_only",
        restore_preconditions=("plan before apply",),
        action_ledger_ref="action_ledger.db",
        migration_ledger_ref="migrations.db",
        module_state_ref="module_state",
        dry_run=True,
    )
    errors.extend(sample.validate())
    if strict:
        for status in ("planned", "blocked", "restoring", "verified", "failed", "partial_restored"):
            if status not in RESTORE_STATUSES:
                errors.append(f"missing restore status: {status}")
    return errors


def build_backup_health(config: Any | None = None) -> dict[str, Any]:
    if config is None:
        from core.config import get_config

        config = get_config()
    manager = MnemosSnapshotManager(config)
    errors = audit_backup_recovery_contract(strict=True)
    snapshots = manager.list_snapshots()
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "status": "ok" if not errors else "degraded",
        "snapshot_root": str(manager.root_dir),
        "counts": {
            "snapshots": len(snapshots),
            "scopes": len(SNAPSHOT_SCOPES),
            "high_risk_policies": len(HIGH_RISK_ACTIONS_REQUIRING_SNAPSHOT),
        },
        "latest": snapshots[0] if snapshots else None,
        "errors": errors,
    }
