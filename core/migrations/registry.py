# -*- coding: utf-8 -*-
"""Unified migration registry and ledger for Mnemos upgrades.

The registry gives old config aliases, standalone scripts, schema upgrades, and
vault layout changes one plan/apply/rollback surface.  It does not run risky
standalone scripts unless the caller explicitly opts in.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.config_registry import CONFIG_KEY_ALIASES, CONFIG_REGISTRY, REMOVED_CONFIG_KEYS
from core.migrations.cognitive_state_registration import (
    ACTION_LEDGER_MIGRATION_ID,
    COGNITIVE_STATE_STORE_MIGRATION_ID,
    DECISION_TRACE_HISTORY_MIGRATION_ID,
    DEDICATED_DATABASE_MIGRATIONS,
    MATERIAL_EFFECT_SCHEMA_MIGRATION_ID,
    dedicated_migration_spec_kwargs,
    inspect_dedicated_migration,
)

MIGRATION_SCHEMA_VERSION = "mnemos.migration_registry.v1"
MIGRATION_STATUSES = {
    "pending",
    "planned",
    "noop",
    "blocked",
    "applying",
    "applied",
    "verified",
    "rolled_back",
    "failed",
}
MIGRATION_SCOPES = {"config", "database", "vault", "privacy", "system"}
MIGRATION_RISK_LEVELS = {"low", "medium", "high", "critical"}
MODEL_CALL_LEDGER_MIGRATION_ID = "database.model_call_ledger.v1"
_MODEL_CALL_LEDGER_CAPABILITY_NONCE = object()
_SAFE_MODEL_CALL_LEDGER_ERROR = re.compile(r"^[a-z][a-z0-9_]{2,120}$")
_SAFE_MODEL_CALL_LEDGER_PREFIXES = (
    "backup_",
    "canonical_",
    "daemon_",
    "expected_",
    "legacy_",
    "migration_ledger_",
    "model_call_ledger_",
    "post_apply_",
    "reconciliation_",
    "recovery_",
    "registered_",
    "retired_",
    "runtime_",
    "sealed_",
    "source_",
    "sqlite_",
    "unattributable_",
    "unrecoverable_",
    "verified_",
    "wrapped_",
)
_MODEL_CALL_LEDGER_CAPABILITY_AUTHORITY = object()
_ACTIVE_MODEL_CALL_LEDGER_CAPABILITIES: dict[int, tuple[object, str, str, Any]] = {}


def _safe_model_call_ledger_error(value: object, *, fallback: str) -> str:
    """Return a fixed public code, never arbitrary exception/result text."""
    candidate = str(value or "")
    if (
        _SAFE_MODEL_CALL_LEDGER_ERROR.fullmatch(candidate)
        and candidate.startswith(_SAFE_MODEL_CALL_LEDGER_PREFIXES)
    ):
        return candidate
    return fallback


def _safe_model_call_ledger_exception(exc: BaseException, *, operation: str) -> str:
    if isinstance(exc, sqlite3.Error):
        return f"{operation}_sqlite_error"
    if isinstance(exc, OSError):
        return f"{operation}_io_error"
    return f"{operation}_validation_error"


class _ModelCallLedgerApplyCapability:
    """One-use registry capability; public script entrypoints cannot mint it."""

    __slots__ = ("nonce", "used")

    def __init__(self) -> None:
        self.nonce = _MODEL_CALL_LEDGER_CAPABILITY_NONCE
        self.used = False


def _mint_model_call_ledger_apply_capability(
    *,
    attempt_ledger_id: str,
    expected_plan_hash: str,
    lifecycle: Any,
    _authority: object | None = None,
) -> _ModelCallLedgerApplyCapability:
    """Issue a capability only from the registry's active migration path."""
    if (
        _authority is not _MODEL_CALL_LEDGER_CAPABILITY_AUTHORITY
        or not attempt_ledger_id
        or not expected_plan_hash
        or not callable(lifecycle)
    ):
        raise ValueError("registered_migration_capability_required")
    capability = _ModelCallLedgerApplyCapability()
    _ACTIVE_MODEL_CALL_LEDGER_CAPABILITIES[id(capability)] = (
        capability,
        attempt_ledger_id,
        expected_plan_hash,
        lifecycle,
    )
    return capability


def _consume_model_call_ledger_apply_capability(
    capability: object,
    *,
    expected_plan_hash: str,
) -> tuple[str, Any]:
    record = _ACTIVE_MODEL_CALL_LEDGER_CAPABILITIES.pop(id(capability), None)
    if not isinstance(capability, _ModelCallLedgerApplyCapability) or record is None:
        raise ValueError("registered_migration_capability_required")
    registered, attempt_ledger_id, registered_hash, lifecycle = record
    if (
        registered is not capability
        or capability.nonce is not _MODEL_CALL_LEDGER_CAPABILITY_NONCE
        or capability.used
        or registered_hash != expected_plan_hash
        or not attempt_ledger_id
        or not callable(lifecycle)
    ):
        raise ValueError("registered_migration_capability_required")
    capability.used = True
    return attempt_ledger_id, lifecycle


def _revoke_model_call_ledger_apply_capability(capability: object) -> None:
    """Discard an unconsumed one-use capability after an aborted dispatch."""
    record = _ACTIVE_MODEL_CALL_LEDGER_CAPABILITIES.pop(id(capability), None)
    if isinstance(capability, _ModelCallLedgerApplyCapability) and record is not None:
        if record[0] is capability:
            capability.used = True


LEGACY_ENV_ALIASES = {
    "L1_STORAGE_TOKEN": "raw_event_store/local raw vault",
    "L1_STORAGE_API_URL": "raw_event_store/local raw vault",
    "MNEMOS_DAEMON__SERVICES__L1_SYNC": "daemon.services.raw_sync",
    "MNEMOS_DAEMON__SERVICES__DISTILL_MERGE": "daemon.services.distill_and_merge",
    "MNEMOS_DAEMON__SERVICES__EVENT_BUS": "daemon.services.eventbus",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _config_to_dict(config: Any) -> dict[str, Any]:
    # Migration planning must inspect the exact persisted source document.  A
    # runtime Config may defensively sanitize a historical value before exposing
    # ``persisted_data()``, but that does not make the source noncompliant or
    # remove the migration obligation.
    if hasattr(config, "persisted_source_data") and callable(
        config.persisted_source_data
    ):
        source_data = config.persisted_source_data()
        if isinstance(source_data, Mapping):
            return dict(source_data)
    if hasattr(config, "persisted_data") and callable(config.persisted_data):
        return dict(config.persisted_data())
    if hasattr(config, "to_dict") and callable(config.to_dict):
        return dict(config.to_dict())
    data = getattr(config, "_data", None)
    if isinstance(data, dict):
        parsed = json.loads(json.dumps(data, ensure_ascii=False))
        if isinstance(parsed, dict):
            return parsed
    return {}


def _replace_config_data(config: Any, data: dict[str, Any]) -> None:
    if hasattr(config, "replace_persisted_data") and callable(
        config.replace_persisted_data
    ):
        config.replace_persisted_data(data)
        return
    if hasattr(config, "_data"):
        config._data = data
    if hasattr(config, "save") and callable(config.save):
        config.save()


def _config_path(config: Any) -> Path:
    path = getattr(config, "config_path", None)
    if path is None:
        return Path.home() / ".mnemos" / "configs" / "main.json"
    return Path(path)


def _mnemos_dir(config: Any) -> Path:
    value = getattr(config, "mnemos_dir", None) or getattr(config, "data_dir", None)
    return Path(value) if value is not None else Path.home() / ".mnemos"


def _database_dir(config: Any) -> Path:
    value = getattr(config, "database_dir", None)
    return Path(value) if value is not None else _mnemos_dir(config)


def _dotted_exists(data: Mapping[str, Any], key: str) -> bool:
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False
        current = current[part]
    return True


def _delete_dotted(data: dict[str, Any], key: str) -> bool:
    current: Any = data
    parents: list[tuple[dict[str, Any], str]] = []
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(current, dict) or not isinstance(current.get(part), dict):
            return False
        parents.append((current, part))
        current = current[part]
    if isinstance(current, dict) and parts[-1] in current:
        del current[parts[-1]]
        for parent, part in reversed(parents):
            child = parent.get(part)
            if isinstance(child, dict) and not child:
                del parent[part]
            else:
                break
        return True
    return False


def _set_dotted(data: dict[str, Any], key: str, value: Any) -> None:
    current = data
    parts = key.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _canonical_type_coercions(data: Mapping[str, Any]) -> dict[str, int]:
    """Return safe integral-float to canonical-int migrations."""
    values = CONFIG_REGISTRY.flatten_override(data)
    result: dict[str, int] = {}
    for key, value in values.items():
        try:
            spec = CONFIG_REGISTRY.require(key)
        except KeyError:
            continue
        if spec.value_types == (int,) and isinstance(value, float) and value.is_integer():
            result[key] = int(value)
    return result


def _canonical_value_normalizations(data: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Return persisted values that violate a current non-lossy contract.

    ``raw_projection.max_turn_chars`` remains a registered configuration key so
    consumers can assert the canonical value, but non-zero persisted values are
    a historical lossy profile and must never survive migration as an apparently
    valid override.
    """
    raw_projection = data.get("raw_projection")
    if not isinstance(raw_projection, Mapping):
        return {}
    max_turn_chars = raw_projection.get("max_turn_chars")
    if (
        isinstance(max_turn_chars, int)
        and not isinstance(max_turn_chars, bool)
        and max_turn_chars != 0
    ):
        return {"raw_projection.max_turn_chars": {"from": max_turn_chars, "to": 0}}
    return {}


def _read_config_aliases() -> dict[str, str]:
    return dict(CONFIG_KEY_ALIASES)


@dataclass(frozen=True)
class MigrationSpec:
    migration_id: str
    from_version: str
    to_version: str
    scope: str
    risk_level: str
    summary: str
    affected_paths: tuple[str, ...]
    requires_backup: bool
    stale_keys: tuple[str, ...] = ()
    deprecated_aliases: tuple[str, ...] = ()
    wrapper_command: tuple[str, ...] = ()
    rollback_command: tuple[str, ...] = ()
    capability_refs: tuple[str, ...] = ()
    privacy_policy: str = "migration_ledger"
    schema_version: str = MIGRATION_SCHEMA_VERSION

    def validate(self, *, root: Path | None = None) -> list[str]:
        errors: list[str] = []
        if not self.migration_id:
            errors.append("migration_id is required")
        if self.scope not in MIGRATION_SCOPES:
            errors.append(f"{self.migration_id}: unknown scope {self.scope}")
        if self.risk_level not in MIGRATION_RISK_LEVELS:
            errors.append(f"{self.migration_id}: unknown risk_level {self.risk_level}")
        if not self.from_version or not self.to_version:
            errors.append(f"{self.migration_id}: from_version and to_version required")
        if not self.summary:
            errors.append(f"{self.migration_id}: summary required")
        if not self.affected_paths:
            errors.append(f"{self.migration_id}: affected_paths required")
        if self.scope in {"privacy", "database", "vault"} and not self.requires_backup:
            errors.append(f"{self.migration_id}: {self.scope} migrations require backup")
        if root is not None:
            for ref in self.affected_paths:
                if ref.startswith(("config:", "env:", "vault:", "database:", "ledger:")):
                    continue
                if ref.endswith(".py") or "/" in ref:
                    if not (root / ref).exists():
                        errors.append(f"{self.migration_id}: missing path {ref}")
            if self.wrapper_command:
                script = self.wrapper_command[1] if len(self.wrapper_command) > 1 else ""
                if script.endswith(".py") and not (root / script).exists():
                    errors.append(f"{self.migration_id}: missing wrapper {script}")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationPlanItem:
    migration_id: str
    status: str
    scope: str
    risk_level: str
    requires_backup: bool
    affected_paths: tuple[str, ...]
    operations: tuple[str, ...]
    stale_keys: tuple[str, ...] = ()
    deprecated_aliases: tuple[str, ...] = ()
    value_normalizations: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    backup_required_before_apply: bool = False
    wrapper_command: tuple[str, ...] = ()
    execution_plan_hash: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationPlan:
    current_version: str
    target_version: str
    generated_at: str
    items: tuple[MigrationPlanItem, ...]
    schema_version: str = MIGRATION_SCHEMA_VERSION
    plan_hash: str = ""

    def with_hash(self) -> "MigrationPlan":
        payload = {
            "current_version": self.current_version,
            "target_version": self.target_version,
            "items": [item.as_dict() for item in self.items],
            "schema_version": self.schema_version,
        }
        return MigrationPlan(
            current_version=self.current_version,
            target_version=self.target_version,
            generated_at=self.generated_at,
            items=self.items,
            schema_version=self.schema_version,
            plan_hash=_json_hash(payload),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MigrationLedgerRecord:
    ledger_id: str
    migration_id: str
    status: str
    plan_hash: str
    from_version: str
    to_version: str
    backup_ref: str
    actor: str
    verification: Mapping[str, Any] = field(default_factory=dict)
    rollback_ref: str = ""
    error: str = ""
    created_at: str = field(default_factory=_now_iso)
    schema_version: str = MIGRATION_SCHEMA_VERSION

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.ledger_id:
            errors.append("ledger_id is required")
        if not self.migration_id:
            errors.append("migration_id is required")
        if self.status not in MIGRATION_STATUSES:
            errors.append(f"unknown migration status: {self.status}")
        if not self.plan_hash:
            errors.append("plan_hash is required")
        if not self.actor:
            errors.append("actor is required")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _public_model_call_ledger_ref(config: Any, value: str, *, fallback: str) -> str:
    if not value:
        return ""
    root = _mnemos_dir(config).expanduser().absolute()
    try:
        relative = Path(value).expanduser().absolute().relative_to(root)
    except (OSError, ValueError):
        return fallback
    return "<MNEMOS_DIR>/" + relative.as_posix()


def _resolve_model_call_ledger_ref(config: Any, value: str | Path) -> str:
    raw = str(value or "")
    prefix = "<MNEMOS_DIR>/"
    if raw.startswith(prefix):
        return str((_mnemos_dir(config) / raw[len(prefix) :]).expanduser().absolute())
    return str(Path(raw).expanduser().absolute())


_PUBLIC_MODEL_CALL_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PUBLIC_MODEL_CALL_LEDGER_ID = re.compile(
    r"^(?:mig|transient|mig-attempt|mig-failed)-[0-9a-f]{8,64}$"
)
_PUBLIC_MODEL_CALL_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_PUBLIC_MODEL_CALL_RECONCILIATION_STATUS = frozenset(
    {"clean", "noop", "applied", "blocked", "reconciliation_required"}
)
_PUBLIC_MODEL_CALL_RECOVERY_STATE = frozenset(
    {"prepared", "started", "sealed", "interrupted"}
)
_PUBLIC_MODEL_CALL_BOOLEAN_FACTS = frozenset(
    {
        "reviewed_plan_hash_present",
        "reviewed_plan_hash_matches",
        "discard_unattributable_legacy",
        "discard_unrecoverable_run_tombstone_history",
        "recovery_manifest_supplied",
    }
)
_PUBLIC_MODEL_CALL_RECOVERY_BOOLEAN_FACTS = frozenset(
    {
        "backup_bindings_verified",
        "preimage_sealed_before_mutation",
        "mutation_start_durable",
        "postimage_bound",
        "append_only_apply_receipt",
        "interruption_recorded",
    }
)
_PUBLIC_MODEL_CALL_RECONCILIATION_COUNTS = frozenset(
    {
        "imported_count",
        "backup_count",
        "cleanup_count",
        "discarded_unattributable_source_count",
        "discarded_unrecoverable_run_tombstone_history",
    }
)


def _public_model_call_hash(value: object) -> str:
    candidate = str(value or "")
    return candidate if _PUBLIC_MODEL_CALL_HASH.fullmatch(candidate) else ""


def _public_model_call_ledger_verification(value: object) -> dict[str, Any]:
    """Project COG-018 verification facts through an explicit public schema."""
    source = dict(value) if isinstance(value, Mapping) else {}
    rendered: dict[str, Any] = {}
    execution_plan_hash = _public_model_call_hash(source.get("execution_plan_hash"))
    if execution_plan_hash:
        rendered["execution_plan_hash"] = execution_plan_hash
    supplied_review_hash = str(
        source.get("reviewed_plan_hash") or source.get("expected_plan_hash") or ""
    )
    reviewed_matches = bool(
        supplied_review_hash
        and execution_plan_hash
        and supplied_review_hash == execution_plan_hash
    )
    rendered["reviewed_plan_hash_present"] = bool(
        source.get("reviewed_plan_hash_present") or supplied_review_hash
    )
    rendered["reviewed_plan_hash_matches"] = reviewed_matches
    if reviewed_matches and execution_plan_hash:
        rendered["reviewed_plan_hash"] = execution_plan_hash
    for fact_name in _PUBLIC_MODEL_CALL_BOOLEAN_FACTS - {
        "reviewed_plan_hash_present",
        "reviewed_plan_hash_matches",
    }:
        if isinstance(source.get(fact_name), bool):
            rendered[fact_name] = bool(source[fact_name])
    reconciliation_status = str(source.get("reconciliation_status") or "")
    if reconciliation_status in _PUBLIC_MODEL_CALL_RECONCILIATION_STATUS:
        rendered["reconciliation_status"] = reconciliation_status
    recovery_state = str(source.get("recovery_state") or "")
    if recovery_state in _PUBLIC_MODEL_CALL_RECOVERY_STATE:
        rendered["recovery_state"] = recovery_state
    for hash_field in (
        "recovery_manifest_sha256",
        "recovery_prepare_chain_head",
        "recovery_chain_head",
        "preimage_semantic_hash",
    ):
        safe_hash = _public_model_call_hash(source.get(hash_field))
        if safe_hash:
            rendered[hash_field] = safe_hash
    reconciliation = source.get("reconciliation")
    if isinstance(reconciliation, Mapping):
        public_reconciliation: dict[str, Any] = {}
        status = str(reconciliation.get("status") or "")
        if status in _PUBLIC_MODEL_CALL_RECONCILIATION_STATUS:
            public_reconciliation["status"] = status
        if isinstance(reconciliation.get("ok"), bool):
            public_reconciliation["ok"] = bool(reconciliation["ok"])
        plan_fingerprint = _public_model_call_hash(reconciliation.get("plan_fingerprint"))
        if plan_fingerprint:
            public_reconciliation["plan_fingerprint"] = plan_fingerprint
        for count_field in _PUBLIC_MODEL_CALL_RECONCILIATION_COUNTS:
            number = reconciliation.get(count_field)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                public_reconciliation[count_field] = number
        if public_reconciliation:
            rendered["reconciliation"] = public_reconciliation
    recovery_verification = source.get("recovery_verification")
    if isinstance(recovery_verification, Mapping):
        public_recovery = {
            recovery_field: bool(recovery_verification[recovery_field])
            for recovery_field in _PUBLIC_MODEL_CALL_RECOVERY_BOOLEAN_FACTS
            if isinstance(recovery_verification.get(recovery_field), bool)
        }
        if public_recovery:
            rendered["recovery_verification"] = public_recovery
    return rendered


def _public_model_call_created_at(value: object) -> str:
    raw = str(value or "")
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat()


def public_migration_record(record: MigrationLedgerRecord, *, config: Any) -> dict[str, Any]:
    """Render a record for CLI output without exposing COG-018 local paths.

    The durable ledger keeps its exact private references because rollback must
    bind to them.  The public command surface needs only their presence and
    type, not a workstation-specific directory name.
    """
    if record.migration_id != MODEL_CALL_LEDGER_MIGRATION_ID:
        return record.as_dict()
    return {
        "ledger_id": (
            record.ledger_id
            if _PUBLIC_MODEL_CALL_LEDGER_ID.fullmatch(record.ledger_id)
            else "protected_model_call_ledger_record"
        ),
        "migration_id": MODEL_CALL_LEDGER_MIGRATION_ID,
        "status": record.status if record.status in MIGRATION_STATUSES else "failed",
        "plan_hash": _public_model_call_hash(record.plan_hash),
        "from_version": (
            record.from_version
            if _PUBLIC_MODEL_CALL_VERSION.fullmatch(record.from_version)
            else "protected_migration_version"
        ),
        "to_version": (
            record.to_version
            if _PUBLIC_MODEL_CALL_VERSION.fullmatch(record.to_version)
            else "protected_migration_version"
        ),
        "backup_ref": _public_model_call_ledger_ref(
            config,
            record.backup_ref,
            fallback="protected_model_call_ledger_backup",
        ),
        "actor": "migration_operator",
        "verification": _public_model_call_ledger_verification(record.verification),
        "rollback_ref": _public_model_call_ledger_ref(
            config,
            record.rollback_ref,
            fallback="sealed_or_manual_recovery_manifest",
        ),
        "error": (
            _safe_model_call_ledger_error(
                record.error,
                fallback="model_call_ledger_record_error",
            )
            if record.error
            else ""
        ),
        "created_at": _public_model_call_created_at(record.created_at),
        "schema_version": MIGRATION_SCHEMA_VERSION,
    }


class MigrationLedger:
    """SQLite ledger for migration plans, applies, verification, and rollback."""

    def __init__(self, db_path: Path, *, initialize: bool = True):
        self.db_path = Path(db_path)
        if initialize:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_schema()

    @staticmethod
    def _decode_verification(raw: object, *, strict: bool = False) -> dict[str, Any]:
        try:
            value = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            if strict:
                raise ValueError("migration_ledger_verification_invalid") from exc
            return {"status": "migration_ledger_verification_invalid"}
        if not isinstance(value, dict):
            if strict:
                raise ValueError("migration_ledger_verification_invalid")
            return {"status": "migration_ledger_verification_invalid"}
        return value

    @classmethod
    def from_config(
        cls, config: Any, *, initialize: bool = True
    ) -> "MigrationLedger":
        return cls(_mnemos_dir(config) / "migrations.db", initialize=initialize)

    def _ensure_schema(self) -> None:
        with sqlite3.connect(str(self.db_path), timeout=5) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS migration_ledger (
                    ledger_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    migration_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    from_version TEXT NOT NULL,
                    to_version TEXT NOT NULL,
                    backup_ref TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    rollback_ref TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_migration_ledger_migration "
                "ON migration_ledger(migration_id, status)"
            )

    def record(self, record: MigrationLedgerRecord) -> str:
        errors = record.validate()
        if errors:
            raise ValueError("; ".join(errors))
        with sqlite3.connect(str(self.db_path), timeout=5) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO migration_ledger (
                    ledger_id, schema_version, migration_id, status, plan_hash,
                    from_version, to_version, backup_ref, actor, verification_json,
                    rollback_ref, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.ledger_id,
                    record.schema_version,
                    record.migration_id,
                    record.status,
                    record.plan_hash,
                    record.from_version,
                    record.to_version,
                    record.backup_ref,
                    record.actor,
                    json.dumps(dict(record.verification), ensure_ascii=False),
                    record.rollback_ref,
                    record.error,
                    record.created_at,
                ),
            )
        return record.ledger_id

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        with sqlite3.connect(
            self.db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM migration_ledger ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["verification"] = self._decode_verification(
                item.pop("verification_json") or "{}"
            )
            result.append(item)
        return result

    def latest_for_migration(self, migration_id: str) -> dict[str, Any] | None:
        if not self.db_path.is_file():
            return None
        with sqlite3.connect(
            self.db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM migration_ledger
                WHERE migration_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (migration_id,),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["verification"] = self._decode_verification(item.pop("verification_json") or "{}")
        return item

    def find_by_id(self, ledger_id: str) -> dict[str, Any] | None:
        if not ledger_id or not self.db_path.is_file():
            return None
        with sqlite3.connect(
            self.db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM migration_ledger WHERE ledger_id = ?", (ledger_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["verification"] = self._decode_verification(item.pop("verification_json") or "{}")
        return item

    def find_applied_by_rollback_ref(
        self, migration_id: str, rollback_ref: str
    ) -> dict[str, Any] | None:
        if not migration_id or not rollback_ref or not self.db_path.is_file():
            return None
        with sqlite3.connect(
            self.db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM migration_ledger
                WHERE migration_id = ? AND status = 'applied' AND rollback_ref = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (migration_id, rollback_ref),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["verification"] = self._decode_verification(item.pop("verification_json") or "{}")
        return item

    def find_recovery_by_rollback_ref(
        self, migration_id: str, rollback_ref: str
    ) -> dict[str, Any] | None:
        """Find a completed or durable in-flight model-call recovery receipt."""
        if not migration_id or not rollback_ref or not self.db_path.is_file():
            return None
        with sqlite3.connect(
            self.db_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM migration_ledger
                WHERE migration_id = ? AND rollback_ref = ?
                  AND status IN ('applied', 'applying', 'failed')
                ORDER BY CASE status WHEN 'applied' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,
                         created_at DESC
                LIMIT 1
                """,
                (migration_id, rollback_ref),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["verification"] = self._decode_verification(
            item.pop("verification_json") or "{}", strict=True
        )
        return item


def _default_specs() -> tuple[MigrationSpec, ...]:
    aliases = tuple(sorted(_read_config_aliases()))
    stale_keys = tuple(sorted(set(aliases) | set(REMOVED_CONFIG_KEYS)))
    return (
        MigrationSpec(
            migration_id="config.stale_keys.v1",
            from_version="legacy-config",
            to_version="canonical-config",
            scope="config",
            risk_level="low",
            summary="Remove deprecated config keys and record legacy alias cleanup.",
            affected_paths=("config:main.json", "core/config.py"),
            requires_backup=True,
            stale_keys=stale_keys,
            deprecated_aliases=aliases,
            capability_refs=("module_toggle_registry", "migration_registry"),
        ),
        MigrationSpec(
            migration_id="database.sync_log_schema.v1",
            from_version="sync-log-schema-0",
            to_version="sync-log-schema-1",
            scope="database",
            risk_level="medium",
            summary="Wrap scripts/migrate_db.py in the system migration registry.",
            affected_paths=("scripts/migrate_db.py", "database:sync_log.db"),
            requires_backup=True,
            wrapper_command=("python3", "scripts/migrate_db.py"),
            rollback_command=("python3", "scripts/migrate_db.py", "--rollback"),
            capability_refs=("obsidian_vault", "raw_vault"),
        ),
        MigrationSpec(
            migration_id=MODEL_CALL_LEDGER_MIGRATION_ID,
            from_version="split-prompt-call-stores-v1",
            to_version="model-call-ledger-privacy-v2",
            scope="database",
            risk_level="high",
            summary="Reconcile retired prompt-call stores into the canonical provider-bound ledger.",
            affected_paths=(
                "core/migrations/model_call_ledger_reconcile",
                "database:model_call_ledger.db",
                "database:wiki_state.db",
                "database:prompt_calls.db",
                "database:sync_log.db",
            ),
            requires_backup=True,
            wrapper_command=("python3", "scripts/reconcile_model_call_ledger.py", "--json"),
            capability_refs=("model_call_ledger", "data_ownership", "migration_registry"),
        ),
        *(MigrationSpec(**values) for values in dedicated_migration_spec_kwargs()),
        MigrationSpec(
            migration_id="vault.layout.v2",
            from_version="single-vault-layout",
            to_version="dual-vault-layout-v2",
            scope="vault",
            risk_level="high",
            summary="Wrap scripts/migrate_vault_layout.py in the migration registry.",
            affected_paths=("scripts/migrate_vault_layout.py", "vault:mnemos", "vault:raw"),
            requires_backup=True,
            wrapper_command=("python3", "scripts/migrate_vault_layout.py", "--dry-run"),
            capability_refs=("obsidian_vault", "raw_vault"),
        ),
    )


class MigrationRegistry:
    """Registry facade for status, plan, apply, rollback, and verification."""

    def __init__(self, specs: Sequence[MigrationSpec] | None = None):
        self.specs = {spec.migration_id: spec for spec in (specs or _default_specs())}

    def list_specs(self) -> list[MigrationSpec]:
        return [self.specs[key] for key in sorted(self.specs)]

    def _plan_item_for_spec(self, spec: MigrationSpec, config: Any) -> MigrationPlanItem:
        data = _config_to_dict(config)
        operations: list[str] = []
        stale_found: list[str] = []
        execution_plan_hash = ""
        affected_paths = spec.affected_paths
        status = "planned"
        if spec.migration_id == "config.stale_keys.v1":
            for key in spec.stale_keys:
                if _dotted_exists(data, key):
                    stale_found.append(key)
            alias_found = [key for key in CONFIG_KEY_ALIASES if _dotted_exists(data, key)]
            removed_found = [key for key in REMOVED_CONFIG_KEYS if _dotted_exists(data, key)]
            env_found = [key for key in LEGACY_ENV_ALIASES if os.getenv(key)]
            type_coercions = _canonical_type_coercions(data)
            value_normalizations = _canonical_value_normalizations(data)
            if alias_found:
                operations.append("migrate deprecated aliases to canonical config keys")
            if removed_found:
                operations.append("delete stale config keys from main.json")
            if env_found:
                operations.append("report legacy environment variables")
            if type_coercions:
                operations.append("coerce integral numeric values to canonical integer types")
            if value_normalizations:
                operations.append("normalize non-lossy configuration contract values")
            if not stale_found and not env_found and not type_coercions and not value_normalizations:
                status = "verified"
                operations.append("no stale config keys detected")
        elif spec.migration_id == MODEL_CALL_LEDGER_MIGRATION_ID:
            from core.migrations.model_call_ledger_migration import (
                inspect_registered_model_call_ledger_plan,
            )

            details = inspect_registered_model_call_ledger_plan(config)
            execution_plan_hash = details.execution_plan_hash
            affected_paths = details.affected_paths
            status = details.status
            operations.extend(details.operations)
        elif spec.migration_id in DEDICATED_DATABASE_MIGRATIONS:
            status, operation = inspect_dedicated_migration(
                spec.migration_id,
                config,
            )
            operations.append(operation)
        elif spec.wrapper_command:
            operations.append("delegate to wrapped migration command")
            status = "planned"
        else:
            operations.append("registry-only migration")
        return MigrationPlanItem(
            migration_id=spec.migration_id,
            status=status,
            scope=spec.scope,
            risk_level=spec.risk_level,
            requires_backup=spec.requires_backup,
            affected_paths=affected_paths,
            operations=tuple(operations),
            stale_keys=tuple(sorted(stale_found)),
            deprecated_aliases=spec.deprecated_aliases,
            value_normalizations=value_normalizations if spec.migration_id == "config.stale_keys.v1" else {},
            backup_required_before_apply=spec.requires_backup,
            wrapper_command=spec.wrapper_command,
            execution_plan_hash=execution_plan_hash,
        )

    def plan(self, config: Any, *, target_version: str = "current") -> MigrationPlan:
        items = tuple(self._plan_item_for_spec(spec, config) for spec in self.list_specs())
        return MigrationPlan(
            current_version="detected",
            target_version=target_version,
            generated_at=_now_iso(),
            items=items,
        ).with_hash()

    def status(self, config: Any, *, read_only: bool = False) -> dict[str, Any]:
        plan = self.plan(config)
        ledger = MigrationLedger.from_config(config, initialize=not read_only)
        recent_ledger: list[dict[str, Any]] = []
        for row in ledger.recent(limit=5):
            if row.get("migration_id") != MODEL_CALL_LEDGER_MIGRATION_ID:
                recent_ledger.append(row)
                continue
            record = MigrationLedgerRecord(
                ledger_id=str(row.get("ledger_id") or ""),
                migration_id=MODEL_CALL_LEDGER_MIGRATION_ID,
                status=str(row.get("status") or "failed"),
                plan_hash=str(row.get("plan_hash") or ""),
                from_version=str(row.get("from_version") or ""),
                to_version=str(row.get("to_version") or ""),
                backup_ref=str(row.get("backup_ref") or ""),
                actor=str(row.get("actor") or ""),
                verification=dict(row.get("verification") or {}),
                rollback_ref=str(row.get("rollback_ref") or ""),
                error=str(row.get("error") or ""),
                created_at=str(row.get("created_at") or ""),
                schema_version=str(row.get("schema_version") or MIGRATION_SCHEMA_VERSION),
            )
            public = public_migration_record(record, config=config)
            # Status is informational only.  It must not hand an older path
            # back as a usable recovery reference, even in redacted form.
            if record.backup_ref:
                public["backup_ref"] = "protected_model_call_ledger_backup"
            if record.rollback_ref:
                public["rollback_ref"] = "sealed_or_manual_recovery_manifest"
            recent_ledger.append(public)
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "status": "ok",
            "plan_hash": plan.plan_hash,
            "counts": {
                "registered": len(self.specs),
                "planned": sum(1 for item in plan.items if item.status == "planned"),
                "verified": sum(1 for item in plan.items if item.status == "verified"),
                "blocked": sum(1 for item in plan.items if item.status == "blocked"),
                "requires_backup": sum(1 for item in plan.items if item.requires_backup),
            },
            "recent_ledger": recent_ledger,
            "items": [item.as_dict() for item in plan.items],
        }

    def _backup_config_json(self, config: Any, migration_id: str) -> str:
        source = _config_path(config)
        backup_dir = _mnemos_dir(config) / "migrations" / "config_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{migration_id}-{uuid.uuid4().hex[:12]}.json"
        if source.exists():
            shutil.copy2(source, backup_path)
        else:
            backup_path.write_text(
                json.dumps(_config_to_dict(config), ensure_ascii=False, indent=2)
            )
        return str(backup_path)

    @staticmethod
    def _transient_record(
        spec: MigrationSpec,
        *,
        status: str,
        plan_hash: str,
        actor: str,
        verification: Mapping[str, Any],
        error: str = "",
        backup_ref: str = "",
        rollback_ref: str = "",
    ) -> MigrationLedgerRecord:
        return MigrationLedgerRecord(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status=status,
            plan_hash=plan_hash or _json_hash({"migration_id": spec.migration_id}),
            from_version=spec.from_version,
            to_version=spec.to_version,
            backup_ref=backup_ref,
            actor=actor,
            verification=dict(verification),
            rollback_ref=rollback_ref,
            error=error,
        )

    @staticmethod
    def _issue_model_call_ledger_apply_capability(
        *,
        attempt_ledger_id: str,
        expected_plan_hash: str,
        lifecycle: Any,
    ) -> _ModelCallLedgerApplyCapability:
        """Keep capability mint authority inside the generic registry."""
        return _mint_model_call_ledger_apply_capability(
            attempt_ledger_id=attempt_ledger_id,
            expected_plan_hash=expected_plan_hash,
            lifecycle=lifecycle,
            _authority=_MODEL_CALL_LEDGER_CAPABILITY_AUTHORITY,
        )

    @staticmethod
    def _revoke_model_call_ledger_apply_capability(capability: object) -> None:
        _revoke_model_call_ledger_apply_capability(capability)

    def _model_call_ledger_hooks(self) -> Any:
        """Build the only bridge available to COG-018 migration internals."""
        from core.migrations.model_call_ledger_migration import (
            ModelCallLedgerRegistryHooks,
        )

        return ModelCallLedgerRegistryHooks(
            make_transient_record=self._transient_record,
            make_record=MigrationLedgerRecord,
            ledger_from_config=MigrationLedger.from_config,
            issue_apply_capability=self._issue_model_call_ledger_apply_capability,
            revoke_apply_capability=self._revoke_model_call_ledger_apply_capability,
            json_hash=_json_hash,
            mnemos_dir=_mnemos_dir,
            resolve_recovery_ref=_resolve_model_call_ledger_ref,
            safe_error=_safe_model_call_ledger_error,
            safe_exception=_safe_model_call_ledger_exception,
        )

    def apply(
        self,
        config: Any,
        migration_id: str,
        *,
        actor: str = "mnemos_cli",
        execute_wrapped: bool = False,
        expected_plan_hash: str | None = None,
        discard_unattributable_legacy: bool = False,
        discard_unrecoverable_run_tombstone_history: bool = False,
    ) -> MigrationLedgerRecord:
        if migration_id not in self.specs:
            raise KeyError(f"unknown migration: {migration_id}")
        spec = self.specs[migration_id]
        if migration_id == MODEL_CALL_LEDGER_MIGRATION_ID:
            from core.migrations.model_call_ledger_migration import (
                apply_registered_model_call_ledger,
            )

            record = apply_registered_model_call_ledger(
                self._model_call_ledger_hooks(),
                config,
                spec,
                actor=actor,
                execute_wrapped=execute_wrapped,
                expected_plan_hash=expected_plan_hash,
                discard_unattributable_legacy=discard_unattributable_legacy,
                discard_unrecoverable_run_tombstone_history=(
                    discard_unrecoverable_run_tombstone_history
                ),
            )
            if not isinstance(record, MigrationLedgerRecord):
                raise TypeError("registered model-call migration returned an invalid record")
            return record
        if migration_id in DEDICATED_DATABASE_MIGRATIONS:
            spec = self.specs[migration_id]
            return self._transient_record(
                spec,
                status="blocked",
                plan_hash=self.plan(config).plan_hash,
                actor=actor,
                verification={"dedicated_reconciliation_required": True},
                error="dedicated migration requires reviewed --backup-dir and explicit --apply",
            )
        plan = self.plan(config)
        item = next(item for item in plan.items if item.migration_id == migration_id)
        from core.ops.runtime_flow_telemetry import (
            record_runtime_produced,
            runtime_item_id,
        )

        migration_flow_item_id = runtime_item_id("migration-plan", migration_id, plan.plan_hash)
        record_runtime_produced(
            "migration_plan_to_ledger",
            source="core/migrations/registry.py",
            item_id=migration_flow_item_id,
            intended_consumers=["core/migrations/registry.py:MigrationLedger"],
            metadata={"transition": "migration_plan_selected", "migration_id": migration_id},
            config_or_path=_database_dir(config),
        )
        ledger = MigrationLedger.from_config(config)
        backup_ref = ""
        verification: dict[str, Any] = {
            "operations": list(item.operations),
            "discard_unattributable_legacy": bool(discard_unattributable_legacy),
            "discard_unrecoverable_run_tombstone_history": bool(
                discard_unrecoverable_run_tombstone_history
            ),
        }
        rollback_ref = ""
        status = "applied"
        error = ""
        if spec.requires_backup and migration_id != MODEL_CALL_LEDGER_MIGRATION_ID:
            backup_ref = self._backup_config_json(config, migration_id)
            rollback_ref = backup_ref
        try:
            if migration_id == "config.stale_keys.v1":
                data = _config_to_dict(config)
                data, alias_migrations, alias_conflicts = CONFIG_REGISTRY.migrate_aliases(
                    data
                )
                removed = [key for key in item.stale_keys if _delete_dotted(data, key)]
                type_coercions = _canonical_type_coercions(data)
                for key, value in type_coercions.items():
                    _set_dotted(data, key, value)
                value_normalizations = _canonical_value_normalizations(data)
                for key, change in value_normalizations.items():
                    _set_dotted(data, key, int(change["to"]))
                _replace_config_data(config, data)
                verification["removed_stale_keys"] = removed
                verification["alias_migrations"] = alias_migrations
                verification["alias_conflicts"] = list(alias_conflicts)
                verification["type_coercions"] = type_coercions
                verification["value_normalizations"] = value_normalizations
            elif spec.wrapper_command:
                if not execute_wrapped:
                    status = "blocked"
                    error = "wrapped migration requires --execute-wrapped"
                    verification["wrapper_command"] = list(spec.wrapper_command)
                else:
                    result = subprocess.run(
                        list(spec.wrapper_command),
                        cwd=Path(__file__).resolve().parents[2],
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    verification["returncode"] = result.returncode
                    verification["stdout"] = result.stdout[-2000:]
                    verification["stderr"] = result.stderr[-2000:]
                    if result.returncode != 0:
                        status = "failed"
                        error = result.stderr.strip() or result.stdout.strip()
            else:
                verification["registry_only"] = True
        except (
            AttributeError,
            ImportError,
            OSError,
            sqlite3.Error,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
        ) as exc:  # pragma: no cover - defensive ledger path
            status = "failed"
            error = str(exc)
        record = MigrationLedgerRecord(
            ledger_id=f"mig-{uuid.uuid4().hex[:16]}",
            migration_id=migration_id,
            status=status,
            plan_hash=plan.plan_hash,
            from_version=spec.from_version,
            to_version=spec.to_version,
            backup_ref=backup_ref,
            actor=actor,
            verification=verification,
            rollback_ref=rollback_ref,
            error=error,
        )
        ledger.record(record)
        from core.ops.runtime_flow_telemetry import record_runtime_consumed

        record_runtime_consumed(
            "migration_plan_to_ledger",
            source="core/migrations/registry.py:MigrationLedger",
            item_id=migration_flow_item_id,
            metadata={"transition": "migration_ledger_recorded", "status": status},
            config_or_path=_database_dir(config),
        )
        return record

    def rollback(
        self,
        config: Any,
        migration_id: str,
        *,
        actor: str = "mnemos_cli",
        recovery_manifest: str | Path | None = None,
        apply: bool = False,
        execute_wrapped: bool = False,
    ) -> MigrationLedgerRecord:
        if migration_id not in self.specs:
            raise KeyError(f"unknown migration: {migration_id}")
        spec = self.specs[migration_id]
        if migration_id == MODEL_CALL_LEDGER_MIGRATION_ID:
            from core.migrations.model_call_ledger_migration import (
                rollback_registered_model_call_ledger,
            )

            record = rollback_registered_model_call_ledger(
                self._model_call_ledger_hooks(),
                config,
                spec,
                actor=actor,
                recovery_manifest=recovery_manifest,
                apply=apply,
                execute_wrapped=execute_wrapped,
            )
            if not isinstance(record, MigrationLedgerRecord):
                raise TypeError("registered model-call rollback returned an invalid record")
            return record
        if migration_id in DEDICATED_DATABASE_MIGRATIONS:
            return self._transient_record(
                spec,
                status="blocked",
                plan_hash=self.plan(config).plan_hash,
                actor=actor,
                verification={"dedicated_backup_restore_required": True},
                error="dedicated rollback requires reviewed backup restore",
            )
        plan = self.plan(config)
        ledger = MigrationLedger.from_config(config)
        latest = ledger.latest_for_migration(migration_id) or {}
        rollback_ref = str(latest.get("rollback_ref") or "")
        status = "rolled_back"
        error = ""
        verification: dict[str, Any] = {"source_ledger_id": latest.get("ledger_id", "")}
        if migration_id == "config.stale_keys.v1" and rollback_ref:
            try:
                data = json.loads(Path(rollback_ref).read_text(encoding="utf-8"))
                _replace_config_data(config, data)
                verification["restored_from"] = rollback_ref
            except (OSError, json.JSONDecodeError) as exc:
                status = "failed"
                error = str(exc)
        elif spec.wrapper_command:
            status = "blocked"
            error = "wrapped migration rollback requires the dedicated script and backup review"
        record = MigrationLedgerRecord(
            ledger_id=f"mig-{uuid.uuid4().hex[:16]}",
            migration_id=migration_id,
            status=status,
            plan_hash=plan.plan_hash,
            from_version=spec.to_version,
            to_version=spec.from_version,
            backup_ref=str(latest.get("backup_ref") or ""),
            actor=actor,
            verification=verification,
            rollback_ref=rollback_ref,
            error=error,
        )
        ledger.record(record)
        return record

    def verify(self, config: Any) -> dict[str, Any]:
        plan = self.plan(config)
        errors = audit_migration_registry(strict=True)
        blocked = [
            item.migration_id
            for item in plan.items
            if item.status == "blocked" or (item.status == "planned" and item.requires_backup)
        ]
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "ok": not errors,
            "errors": errors,
            "plan_hash": plan.plan_hash,
            "blocked_until_apply": blocked,
        }


def audit_migration_registry(*, strict: bool = False, root: Path | None = None) -> list[str]:
    root = root or Path(__file__).resolve().parents[2]
    registry = MigrationRegistry()
    errors: list[str] = []
    required = {
        "config.stale_keys.v1",
        "database.sync_log_schema.v1",
        MODEL_CALL_LEDGER_MIGRATION_ID,
        COGNITIVE_STATE_STORE_MIGRATION_ID,
        ACTION_LEDGER_MIGRATION_ID,
        DECISION_TRACE_HISTORY_MIGRATION_ID,
        MATERIAL_EFFECT_SCHEMA_MIGRATION_ID,
        "vault.layout.v2",
    }
    missing = required - set(registry.specs)
    if missing:
        errors.append(f"missing migration specs: {sorted(missing)}")
    aliases = set(_read_config_aliases())
    config_spec = registry.specs.get("config.stale_keys.v1")
    if config_spec is None:
        errors.append("config stale key migration is missing")
    else:
        if not aliases <= set(config_spec.stale_keys):
            errors.append("config stale key migration does not cover Config aliases")
        if not set(REMOVED_CONFIG_KEYS) <= set(config_spec.stale_keys):
            errors.append("config stale key migration does not cover removed keys")
    for spec in registry.list_specs():
        errors.extend(spec.validate(root=root if strict else None))
        if strict and spec.wrapper_command and not spec.requires_backup:
            errors.append(f"{spec.migration_id}: wrapped migrations require backup")
        if strict and not spec.capability_refs:
            errors.append(f"{spec.migration_id}: capability_refs required")
    return errors


def build_migration_health(config: Any | None = None) -> dict[str, Any]:
    if config is None:
        from core.config import get_config

        config = get_config()
    registry = MigrationRegistry()
    errors = audit_migration_registry(strict=True)
    status = registry.status(config, read_only=True)
    status.update(
        {
            "status": "ok" if not errors else "degraded",
            "errors": errors,
            "ledger_path": "<MNEMOS_DIR>/migrations.db",
        }
    )
    return status


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Mnemos migration registry")
    parser.add_argument("action", choices=["status", "plan", "apply", "rollback", "verify"])
    parser.add_argument("migration_id", nargs="?")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--execute-wrapped", action="store_true")
    parser.add_argument("--expected-plan-hash")
    parser.add_argument("--recovery-manifest")
    parser.add_argument(
        "--apply",
        dest="restore_apply",
        action="store_true",
        help="execute a reviewed rollback; without it rollback is read-only",
    )
    parser.add_argument("--discard-unattributable-legacy", action="store_true")
    parser.add_argument("--discard-unrecoverable-run-tombstone-history", action="store_true")
    args = parser.parse_args(argv)

    from core.config import Config

    is_model_call_ledger_mutation = (
        args.action in {"apply", "rollback"}
        and args.migration_id == MODEL_CALL_LEDGER_MIGRATION_ID
    )
    cfg = Config(
        strict=False,
        provision=args.action in {"apply", "rollback"}
        and not is_model_call_ledger_mutation,
    )
    registry = MigrationRegistry()
    if args.action == "status":
        payload = registry.status(cfg, read_only=True)
    elif args.action == "plan":
        payload = registry.plan(cfg).as_dict()
    elif args.action == "verify":
        payload = registry.verify(cfg)
    elif args.action == "apply":
        if not args.migration_id:
            parser.error("apply requires migration_id")
        payload = public_migration_record(
            registry.apply(
                cfg,
                args.migration_id,
                execute_wrapped=args.execute_wrapped,
                expected_plan_hash=args.expected_plan_hash,
                discard_unattributable_legacy=args.discard_unattributable_legacy,
                discard_unrecoverable_run_tombstone_history=(
                    args.discard_unrecoverable_run_tombstone_history
                ),
            ),
            config=cfg,
        )
    else:
        if not args.migration_id:
            parser.error("rollback requires migration_id")
        payload = public_migration_record(
            registry.rollback(
                cfg,
                args.migration_id,
                recovery_manifest=args.recovery_manifest,
                apply=args.restore_apply,
                execute_wrapped=args.execute_wrapped,
            ),
            config=cfg,
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") not in {"failed", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
