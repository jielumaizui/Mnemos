"""Fail-closed barrier shared by every canonical feedback writer."""

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


BARRIER_SCHEMA_VERSION = "mnemos.feedback_migration_barrier.v2"
BARRIER_FILE_NAME = ".feedback_migration_barrier.json"


class FeedbackMigrationInProgress(RuntimeError):
    """Raised when a formal feedback writer is blocked for reconciliation."""


@dataclass(frozen=True)
class FeedbackMigrationBarrier:
    """Exclusive migration owner and immutable inventory binding."""

    owner_id: str
    inventory_hash: str
    activated_at: str
    payload_hash: str


def barrier_path(database_dir: Path) -> Path:
    """Return the single fail-closed feedback migration barrier path."""

    return Path(database_dir).expanduser() / BARRIER_FILE_NAME


def read_feedback_migration_barrier(
    database_dir: Path,
) -> FeedbackMigrationBarrier | None:
    """Read and validate the active barrier without mutating state."""

    path = barrier_path(database_dir)
    if not path.is_file():
        return None
    try:
        payload = load_json_value(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FeedbackMigrationInProgress(
            "feedback_migration_barrier_invalid"
        ) from exc
    normalized = _validate_payload(payload)
    return FeedbackMigrationBarrier(
        owner_id=normalized["owner_id"],
        inventory_hash=normalized["inventory_hash"],
        activated_at=normalized["activated_at"],
        payload_hash=normalized["payload_hash"],
    )


def assert_feedback_writes_enabled(database_dir: Path) -> None:
    """Reject formal feedback writes while reconciliation owns the barrier."""

    if read_feedback_migration_barrier(database_dir) is not None:
        raise FeedbackMigrationInProgress("feedback_migration_in_progress")


def activate_feedback_migration_barrier(
    database_dir: Path,
    *,
    inventory_hash: str,
    activated_at: str | None = None,
) -> FeedbackMigrationBarrier:
    """Create one exclusive barrier without replacing an existing owner."""

    normalized_inventory = str(inventory_hash or "").strip()
    if not normalized_inventory.startswith("sha256:"):
        raise ValueError("feedback migration inventory_hash is required")
    root = Path(database_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    path = barrier_path(root)
    owner_id = "feedback-migration-" + uuid4().hex
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
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return read_feedback_migration_barrier(root)  # type: ignore[return-value]


def deactivate_feedback_migration_barrier(
    database_dir: Path,
    *,
    owner_id: str,
) -> None:
    """Release the barrier only for its exact exclusive owner."""

    path = barrier_path(database_dir)
    active = read_feedback_migration_barrier(database_dir)
    if active is None:
        raise FileNotFoundError("feedback migration barrier is not active")
    if active.owner_id != str(owner_id or "").strip():
        raise PermissionError("feedback migration barrier owner mismatch")
    # trusted-scan: system_state owner=cognitive target=feedback_migration_barrier expires=never exact barrier release
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
        raise ValueError("feedback migration barrier payload is invalid")
    normalized = {key: str(payload[key]) for key in required}
    if (
        normalized["schema_version"] != BARRIER_SCHEMA_VERSION
        or normalized["state"] != "active"
        or not normalized["owner_id"].startswith("feedback-migration-")
        or not normalized["inventory_hash"].startswith("sha256:")
        or not normalized["activated_at"]
    ):
        raise ValueError("feedback migration barrier payload is invalid")
    core = {key: normalized[key] for key in required if key != "payload_hash"}
    if normalized["payload_hash"] != sha256_json(core):
        raise ValueError("feedback migration barrier payload hash mismatch")
    return normalized
