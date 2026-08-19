"""Typed contracts for object-level calibration provenance reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.cognitive.auto_calibration import CalibrationReport
from core.cognitive.models import Observation
from core.cognitive.state_contract import sha256_json


RECONCILIATION_SCHEMA_VERSION = "mnemos.calibration_provenance_reconciliation.v1"
MIGRATION_ID = "database.calibration_provenance.v1"


@dataclass(frozen=True)
class CalibrationReconciliationPaths:
    """Exact stores and projection root participating in reconciliation."""

    database_dir: Path
    wiki_dir: Path

    @property
    def state_path(self) -> Path:
        return self.database_dir / "producer_consumer_ledger.db"

    @property
    def observations_path(self) -> Path:
        return self.database_dir / "observations.db"

    @property
    def raw_path(self) -> Path:
        return self.database_dir / "raw_events.db"

    @property
    def migrations_path(self) -> Path:
        return self.database_dir / "migrations.db"

    @property
    def projection_dir(self) -> Path:
        return self.wiki_dir / "L3-Observations"


@dataclass(frozen=True)
class CalibrationReplay:
    """One exact old-head to current-spec replay operation."""

    object_id: str
    old_revision_id: str
    old_payload_hash: str
    observation_row_hash: str
    raw_revision_id: str
    raw_content_hash: str
    expected_input_hash: str
    expected_payload_hash: str
    dimension: str
    observation: Observation
    report: CalibrationReport

    def manifest(self) -> dict[str, str]:
        return {
            "action": "replay_frozen_snapshot",
            "object_id": self.object_id,
            "old_revision_id": self.old_revision_id,
            "old_payload_hash": self.old_payload_hash,
            "observation_row_hash": self.observation_row_hash,
            "raw_revision_id": self.raw_revision_id,
            "raw_content_hash": self.raw_content_hash,
            "expected_input_hash": self.expected_input_hash,
            "expected_payload_hash": self.expected_payload_hash,
            "dimension": self.dimension,
        }


@dataclass(frozen=True)
class CalibrationRetirement:
    """One exact invalid mutable head/Observation pair to retire."""

    object_id: str
    old_revision_id: str
    old_payload_hash: str
    observation_row_hash: str
    collision_contract_hash: str

    def manifest(self) -> dict[str, str]:
        return {
            "action": "retire_legacy_system_identity_collision",
            "object_id": self.object_id,
            "old_revision_id": self.old_revision_id,
            "old_payload_hash": self.old_payload_hash,
            "observation_row_hash": self.observation_row_hash,
            "collision_contract_hash": self.collision_contract_hash,
        }


@dataclass(frozen=True)
class CalibrationCommandClosure:
    """One pending command owned by a provably superseded object generation."""

    command_id: str
    consumer_id: str
    object_id: str
    old_revision_id: str
    old_payload_hash: str
    current_revision_id: str
    current_payload_hash: str

    def manifest(self) -> dict[str, str]:
        return {
            "action": "close_superseded_generation_command",
            "command_id": self.command_id,
            "consumer_id": self.consumer_id,
            "object_id": self.object_id,
            "old_revision_id": self.old_revision_id,
            "old_payload_hash": self.old_payload_hash,
            "current_revision_id": self.current_revision_id,
            "current_payload_hash": self.current_payload_hash,
        }


@dataclass(frozen=True)
class CalibrationReconciliationPlan:
    """Prepared plan with private runtime objects and a public hash surface."""

    paths: CalibrationReconciliationPaths
    validator_spec_hash: str
    current_count: int
    replays: tuple[CalibrationReplay, ...]
    retirements: tuple[CalibrationRetirement, ...]
    command_closures: tuple[CalibrationCommandClosure, ...]
    current_object_manifests: tuple[Mapping[str, Any], ...]
    blocked: tuple[Mapping[str, str], ...]
    object_manifest_hash: str
    inventory_hash: str

    @property
    def ok(self) -> bool:
        return not self.blocked

    @property
    def requires_apply(self) -> bool:
        return bool(self.replays or self.retirements or self.command_closures)

    def as_dict(self) -> dict[str, Any]:
        counts = {
            "replay_frozen_snapshot": len(self.replays),
            "retire_legacy_system_identity_collision": len(self.retirements),
            "close_superseded_generation_command": len(self.command_closures),
            "already_current": len(self.current_object_manifests),
            "blocked": len(self.blocked),
            "current_calibration_heads": self.current_count,
        }
        return {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "ok": self.ok,
            "status": (
                "blocked"
                if not self.ok
                else "reconciliation_required"
                if self.requires_apply
                else "clean"
            ),
            "validator_spec_hash": self.validator_spec_hash,
            "inventory_hash": self.inventory_hash,
            "object_manifest_hash": self.object_manifest_hash,
            "counts": counts,
            "blocked": [dict(value) for value in self.blocked],
            "paths": {
                "database_dir": str(self.paths.database_dir),
                "wiki_dir": str(self.paths.wiki_dir),
            },
        }


def finalize_plan_hashes(
    *,
    validator_spec_hash: str,
    replay_manifests: Sequence[Mapping[str, Any]],
    retirement_manifests: Sequence[Mapping[str, Any]],
    command_closure_manifests: Sequence[Mapping[str, Any]],
    current_manifests: Sequence[Mapping[str, Any]],
    blocked: Sequence[Mapping[str, str]],
) -> tuple[str, str]:
    object_manifest = {
        "replays": replay_manifests,
        "retirements": retirement_manifests,
        "command_closures": command_closure_manifests,
        "current": current_manifests,
        "blocked": blocked,
    }
    object_manifest_hash = sha256_json(object_manifest)
    inventory_hash = sha256_json(
        {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "validator_spec_hash": validator_spec_hash,
            "object_manifest_hash": object_manifest_hash,
        }
    )
    return object_manifest_hash, inventory_hash


__all__ = [
    "CalibrationReconciliationPaths",
    "CalibrationReconciliationPlan",
    "CalibrationCommandClosure",
    "CalibrationReplay",
    "CalibrationRetirement",
    "MIGRATION_ID",
    "RECONCILIATION_SCHEMA_VERSION",
    "finalize_plan_hashes",
]
