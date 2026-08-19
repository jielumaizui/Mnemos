"""Exclusive fail-closed barrier for COG-048 history reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from core.cognitive.state_contract import sha256_json
from core.utils import load_json_value


BARRIER_SCHEMA_VERSION = "mnemos.training_governance_migration_barrier.v1"
BARRIER_FILE_NAME = ".training_governance_migration_barrier.json"


class TrainingGovernanceMigrationInProgress(RuntimeError):
    """Raised when migration owns the governed-training boundary."""


@dataclass(frozen=True)
class TrainingGovernanceMigrationBarrier:
    """Validated identity of the process that owns a live migration."""

    owner_id: str
    inventory_hash: str
    activated_at: str
    payload_hash: str


def barrier_path(database_dir: Path) -> Path:
    """Return the exclusive barrier path for a database directory."""

    return Path(database_dir).expanduser() / BARRIER_FILE_NAME


def read_training_migration_barrier(
    database_dir: Path,
) -> TrainingGovernanceMigrationBarrier | None:
    """Read and validate the active barrier without mutating the directory."""

    path = barrier_path(database_dir)
    if not path.is_file():
        return None
    try:
        payload = load_json_value(path)
        normalized = _validate_payload(payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TrainingGovernanceMigrationInProgress(
            "training_governance_migration_barrier_invalid"
        ) from exc
    return TrainingGovernanceMigrationBarrier(
        owner_id=normalized["owner_id"],
        inventory_hash=normalized["inventory_hash"],
        activated_at=normalized["activated_at"],
        payload_hash=normalized["payload_hash"],
    )


def assert_training_governance_enabled(database_dir: Path) -> None:
    """Fail closed while an object-level history migration owns the store."""

    if read_training_migration_barrier(database_dir) is not None:
        raise TrainingGovernanceMigrationInProgress("training_governance_migration_in_progress")


def activate_training_migration_barrier(
    database_dir: Path,
    *,
    inventory_hash: str,
    activated_at: str | None = None,
) -> TrainingGovernanceMigrationBarrier:
    """Acquire an exclusive durable migration barrier with inventory binding."""

    normalized_inventory = str(inventory_hash or "").strip()
    if not normalized_inventory.startswith("sha256:"):
        raise ValueError("training migration inventory_hash is required")
    root = Path(database_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    path = barrier_path(root)
    owner_id = "training-governance-migration-" + uuid4().hex
    core: dict[str, Any] = {
        "schema_version": BARRIER_SCHEMA_VERSION,
        "state": "active",
        "owner_id": owner_id,
        "inventory_hash": normalized_inventory,
        "activated_at": activated_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    payload = {**core, "payload_hash": sha256_json(core)}
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    active = read_training_migration_barrier(root)
    if active is None:
        raise RuntimeError("training migration barrier was not persisted")
    return active


def deactivate_training_migration_barrier(
    database_dir: Path,
    *,
    owner_id: str,
) -> None:
    """Release only the exact barrier owned by ``owner_id``."""

    path = barrier_path(database_dir)
    active = read_training_migration_barrier(database_dir)
    if active is None:
        raise FileNotFoundError("training migration barrier is not active")
    if active.owner_id != str(owner_id or "").strip():
        raise PermissionError("training migration barrier owner mismatch")
    # trusted-scan: system_state owner=cognitive target=training_governance_migration_barrier expires=never exact barrier release  # noqa: E501
    path.unlink()


def _validate_payload(payload: Any) -> dict[str, str]:
    required = {
        "schema_version",
        "state",
        "owner_id",
        "inventory_hash",
        "activated_at",
        "payload_hash",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ValueError("training migration barrier payload is invalid")
    normalized = {key: str(payload[key]) for key in required}
    if (
        normalized["schema_version"] != BARRIER_SCHEMA_VERSION
        or normalized["state"] != "active"
        or not normalized["owner_id"].startswith("training-governance-migration-")
        or not normalized["inventory_hash"].startswith("sha256:")
        or not normalized["activated_at"]
    ):
        raise ValueError("training migration barrier payload is invalid")
    core = {key: normalized[key] for key in required if key != "payload_hash"}
    if normalized["payload_hash"] != sha256_json(core):
        raise ValueError("training migration barrier payload hash mismatch")
    return normalized
