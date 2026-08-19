"""Dry-run-first reconciliation of pre-contract objective training intakes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping

from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_schema import inspect_cognitive_state_schema
from core.cognitive.state_store import (
    CognitiveStateStore,
    CognitiveStateUnitOfWork,
)
from core.cognitive.training_contract import (
    TRAINING_ADMISSION_COMMAND,
    TRAINING_ADMISSION_CONSUMER,
    validate_training_admission_intake_payload,
)
from core.cognitive.training_intake_derivation import (
    derive_training_admission_intake_command,
)
from core.db_utils import render_sql
from core.ops.cognitive_data_contract import CognitiveDataEvent
from core.ops.cognitive_event_ledger import insert_data_event_in_connection
from core.migrations.model_call_ledger_reconcile.runtime import (
    mnemos_runtime_is_active,
)
from core.scoring.training_schema import inspect_training_schema


RECONCILIATION_SCHEMA_VERSION = "mnemos.phase3_training_intake_inventory.v1"
RECONCILIATION_SPEC_HASH = sha256_json(
    {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "source": "current_objective_feedback_attribution",
        "identity": [
            "attribution_revision",
            "training_evidence_command",
            "training_evidence_receipt",
            "current_outcome_revision",
        ],
        "effect": "append_exact_training_admission_intake_only",
    }
)
_STATE_TABLES = (
    "cognitive_data_events",
    "cognitive_state_revisions",
    "cognitive_state_heads",
    "cognitive_state_outbox",
    "cognitive_state_effect_receipts",
)


@dataclass(frozen=True)
class Phase3TrainingIntakeCandidate:
    """One exact missing intake whose complete source proof was recomputed."""

    attribution_revision_id: str
    attribution_payload_hash: str
    outcome_revision_id: str
    outcome_payload_hash: str
    target_command_id: str
    target_command_payload_hash: str
    target_receipt_id: str
    target_receipt_hash: str
    intake_command: LocalConsumerCommand

    def public_manifest(self) -> dict[str, str]:
        """Return an exact identity manifest without semantic payload bytes."""

        return {
            "attribution_revision_id": self.attribution_revision_id,
            "attribution_payload_hash": self.attribution_payload_hash,
            "outcome_revision_id": self.outcome_revision_id,
            "outcome_payload_hash": self.outcome_payload_hash,
            "target_command_id": self.target_command_id,
            "target_command_payload_hash": self.target_command_payload_hash,
            "target_receipt_id": self.target_receipt_id,
            "target_receipt_hash": self.target_receipt_hash,
            "intake_command_id": self.intake_command.command_id,
            "intake_payload_hash": self.intake_command.payload_hash,
        }


def build_phase3_training_intake_inventory(
    database_dir: Path,
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Inventory exact missing intakes without writing state or diagnostics."""

    root = Path(database_dir).expanduser()
    state_path = root / "producer_consumer_ledger.db"
    if not state_path.is_file():
        return _empty_inventory(status="not_initialized")
    state = CognitiveStateStore(state_path)
    owns_connection = connection is None
    conn = connection or _connect(state_path, read_only=True)
    try:
        inspection = inspect_cognitive_state_schema(conn)
        if not inspection.ok:
            raise RuntimeError("phase3 intake reconciliation requires canonical state schema")
        objects: list[Phase3TrainingIntakeCandidate] = []
        unresolved: list[dict[str, str]] = []
        existing_count = 0
        objective_count = 0
        eligible_count = 0
        attributions = state.current_revisions(
            object_type="feedback_attribution_record"
        )
        for attribution in attributions:
            if (
                attribution.payload.get("evidence_class") != "objective_outcome"
                or attribution.payload.get("disposition") != "objective_only"
            ):
                continue
            objective_count += 1
            try:
                candidate, existing = _candidate_for_attribution(
                    conn,
                    state,
                    attribution,
                )
            except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                unresolved.append(
                    {
                        "attribution_revision_id": attribution.revision_id,
                        "reason": _reason_code(exc),
                    }
                )
                continue
            eligible_count += 1
            if existing:
                existing_count += 1
            elif candidate is not None:
                objects.append(candidate)

        ordered = tuple(
            sorted(objects, key=lambda item: item.intake_command.command_id)
        )
        public_objects = [item.public_manifest() for item in ordered]
        unresolved_counts = dict(
            sorted(Counter(item["reason"] for item in unresolved).items())
        )
        schema_fingerprints = _schema_fingerprints(conn)
        row_counts = {
            table: int(
                conn.execute(
                    render_sql(
                        "SELECT COUNT(*) FROM {table}",
                        identifiers={"table": table},
                    )
                ).fetchone()[0]
            )
            for table in _STATE_TABLES
        }
        material = {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "spec_hash": RECONCILIATION_SPEC_HASH,
            "status": "ready",
            "objective_attribution_count": objective_count,
            "eligible_objective_attributions": eligible_count,
            "proposed_count": len(ordered),
            "existing_count": existing_count,
            "unresolved_count": len(unresolved),
            "unresolved_by_reason": unresolved_counts,
            "object_manifest_hash": sha256_json(public_objects),
            "source_schema_fingerprints": schema_fingerprints,
            "source_row_counts": row_counts,
        }
        return {
            **material,
            "inventory_hash": sha256_json(material),
            "objects": ordered,
            "unresolved": tuple(unresolved),
            "sensitive_bytes_in_report": 0,
        }
    finally:
        if owns_connection:
            conn.close()


def public_phase3_training_intake_inventory(
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Strip internal command payloads from a machine-readable preview."""

    return {
        key: value
        for key, value in inventory.items()
        if key not in {"objects", "unresolved"}
    } | {
        "proposed_command_ids": [
            item.intake_command.command_id
            for item in inventory.get("objects", ())
        ],
        "unresolved": list(inventory.get("unresolved", ())),
    }


def reconcile_phase3_training_admission_intakes(
    *,
    database_dir: Path,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
    backup_dir: Path,
    failpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Back up, append exact commands atomically, and prove zero-change replay."""

    root = Path(database_dir).expanduser()
    state_path = root / "producer_consumer_ledger.db"
    scoring_path = root / "mnemos.db"
    if _runtime_is_active():
        raise RuntimeError(
            "mnemos daemon and MCP services must be inactive before phase3 intake apply"
        )
    if not state_path.is_file() or not scoring_path.is_file():
        raise RuntimeError("phase3 intake apply requires state and scoring databases")
    preview = build_phase3_training_intake_inventory(root)
    _require_expected_inventory(
        preview,
        expected_inventory_hash=expected_inventory_hash,
        expected_object_manifest_hash=expected_object_manifest_hash,
    )
    if int(preview["unresolved_count"]):
        raise RuntimeError("phase3 intake inventory contains unresolved obligations")
    with _connect(state_path, read_only=True) as state_snapshot, _connect(
        scoring_path,
        read_only=True,
    ) as scoring_conn:
        if _integrity_in_connection(state_snapshot) != "ok":
            raise RuntimeError("phase3 intake state integrity failed")
        scoring_inspection = inspect_training_schema(scoring_conn)
        if not scoring_inspection.ok:
            raise RuntimeError("phase3 intake apply requires canonical scoring schema")
        if _integrity_in_connection(scoring_conn) != "ok":
            raise RuntimeError("phase3 intake scoring integrity failed")
        backup_manifest = _backup_databases(
            state_conn=state_snapshot,
            state_path=state_path,
            scoring_conn=scoring_conn,
            scoring_path=scoring_path,
            backup_dir=backup_dir,
            inventory=preview,
        )
    _call_failpoint(failpoint, "after_backup")

    state_conn = _connect(state_path)
    try:
        state_conn.execute("BEGIN IMMEDIATE")
        inventory = build_phase3_training_intake_inventory(
            root,
            connection=state_conn,
        )
        _require_expected_inventory(
            inventory,
            expected_inventory_hash=expected_inventory_hash,
            expected_object_manifest_hash=expected_object_manifest_hash,
        )
        with _connect(scoring_path, read_only=True) as scoring_conn:
            scoring_inspection = inspect_training_schema(scoring_conn)
            if not scoring_inspection.ok:
                raise RuntimeError("phase3 intake apply requires canonical scoring schema")
            if _integrity_in_connection(scoring_conn) != "ok":
                raise RuntimeError("phase3 intake scoring integrity failed")

        inserted = 0
        existing = 0
        for candidate in inventory["objects"]:
            event = _reconciliation_event(candidate)
            _, event_inserted = insert_data_event_in_connection(
                state_conn,
                event,
                lifecycle_status="produced",
                allow_semantic=True,
            )
            _call_failpoint(failpoint, "after_event")
            command_inserted = CognitiveStateUnitOfWork._insert_outbox(
                state_conn,
                event.event_id,
                candidate.intake_command,
            )
            _call_failpoint(failpoint, "after_outbox")
            if event_inserted != command_inserted:
                raise RuntimeError("phase3 intake event/outbox replay state diverged")
            if command_inserted:
                inserted += 1
            else:
                existing += 1

        replay = build_phase3_training_intake_inventory(
            root,
            connection=state_conn,
        )
        if int(replay["proposed_count"]) or int(replay["unresolved_count"]):
            raise RuntimeError("phase3 intake apply did not reach a zero-change replay")
        if _integrity_in_connection(state_conn) != "ok":
            raise RuntimeError("phase3 intake state integrity failed")
        _call_failpoint(failpoint, "before_commit")
        state_conn.commit()
    except BaseException:
        state_conn.rollback()
        raise
    finally:
        state_conn.close()

    return {
        "schema_version": "mnemos.phase3_training_intake_reconciliation.v1",
        "status": "applied" if inserted else "existing",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "object_count": inventory["proposed_count"],
        "effect": {"inserted": inserted, "existing": existing},
        "backup_manifest": str(backup_manifest),
        "state_integrity": "ok",
        "scoring_integrity": "ok",
        "replay": public_phase3_training_intake_inventory(replay),
        "sensitive_bytes_in_report": 0,
    }


def _candidate_for_attribution(
    conn: sqlite3.Connection,
    state: CognitiveStateStore,
    attribution: CognitiveStateRevision,
) -> tuple[Phase3TrainingIntakeCandidate | None, bool]:
    validate_cognitive_state_payload(
        "feedback_attribution_record",
        attribution.payload,
    )
    commands = _commands_for_revision(conn, attribution.revision_id)
    feedback_commands = tuple(
        command
        for command in commands
        if command.consumer_id != TRAINING_ADMISSION_CONSUMER
    )
    targets = tuple(
        command
        for command in feedback_commands
        if command.consumer_id == "training_evidence"
        and command.command_type == "evaluate_feedback_target"
    )
    if len(targets) != 1:
        raise ValueError("training_target_command_count")
    target = targets[0]
    state.validate_feedback_effect_receipt(target.command_id)
    target_receipt = state.effect_receipt(target.command_id)
    if target_receipt is None or target_receipt["status"] != "committed":
        raise ValueError("training_target_receipt_not_committed")
    outcome_ref = target.payload.get("objective_outcome_ref")
    if not isinstance(outcome_ref, Mapping) or outcome_ref.get("state") != "available":
        raise ValueError("objective_outcome_ref_missing")
    outcome = state.revision(str(outcome_ref.get("revision_id") or ""))
    if (
        outcome is None
        or outcome.object_type != "outcome_measurement"
        or outcome.object_id != outcome_ref.get("outcome_id")
        or outcome.payload_hash != outcome_ref.get("payload_hash")
        or state.current_revision("outcome_measurement", outcome.object_id) != outcome
    ):
        raise ValueError("objective_outcome_not_current")
    derived = derive_training_admission_intake_command(
        attribution,
        outcome_revision=outcome,
        target_commands=feedback_commands,
        recorded_at=attribution.created_at,
    )
    intakes = tuple(
        command
        for command in commands
        if command.consumer_id == TRAINING_ADMISSION_CONSUMER
        or command.command_type == TRAINING_ADMISSION_COMMAND
    )
    if intakes:
        if len(intakes) != 1 or intakes[0] != derived:
            raise ValueError("existing_training_intake_identity_mismatch")
        validate_training_admission_intake_payload(intakes[0].payload)
        return None, True
    return (
        Phase3TrainingIntakeCandidate(
            attribution_revision_id=attribution.revision_id,
            attribution_payload_hash=attribution.payload_hash,
            outcome_revision_id=outcome.revision_id,
            outcome_payload_hash=outcome.payload_hash,
            target_command_id=target.command_id,
            target_command_payload_hash=target.payload_hash,
            target_receipt_id=str(target_receipt["receipt_id"]),
            target_receipt_hash=sha256_json(
                {
                    "command_id": target_receipt["command_id"],
                    "revision_id": target_receipt["revision_id"],
                    "status": target_receipt["status"],
                    "target_effect_id": target_receipt["target_effect_id"],
                    "before_hash": target_receipt["before_hash"],
                    "after_hash": target_receipt["after_hash"],
                    "evidence_refs": target_receipt["evidence_refs"],
                }
            ),
            intake_command=derived,
        ),
        False,
    )


def _commands_for_revision(
    conn: sqlite3.Connection,
    revision_id: str,
) -> tuple[LocalConsumerCommand, ...]:
    rows = conn.execute(
        "SELECT * FROM cognitive_state_outbox WHERE revision_id=? "
        "ORDER BY consumer_id, command_id",
        (revision_id,),
    ).fetchall()
    commands: list[LocalConsumerCommand] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        command = LocalConsumerCommand.create(
            revision_id=str(row["revision_id"]),
            consumer_id=str(row["consumer_id"]),
            command_type=str(row["command_type"]),
            payload=payload,
            created_at=str(row["created_at"]),
        )
        if (
            command.command_id != row["command_id"]
            or command.payload_hash != row["payload_hash"]
        ):
            raise ValueError("stored_feedback_command_identity_mismatch")
        commands.append(command)
    return tuple(commands)


def _reconciliation_event(
    candidate: Phase3TrainingIntakeCandidate,
) -> CognitiveDataEvent:
    suffix = candidate.intake_command.command_id.rsplit(":", 1)[-1]
    event_id = "phase3-training-intake-event-" + suffix
    evidence_refs = (
        candidate.attribution_revision_id,
        f"feedback-command:{candidate.target_command_id}",
        candidate.outcome_revision_id,
        f"feedback-receipt:{candidate.target_receipt_id}",
    )
    return CognitiveDataEvent(
        event_id=event_id,
        source_id=candidate.attribution_revision_id,
        asset_id=candidate.intake_command.command_id,
        source_kind="phase3_training_intake_reconciliation",
        source_uri=(
            "mnemos://cognitive/reconciliation/training-intake/"
            + candidate.attribution_revision_id
        ),
        content_hash=candidate.attribution_payload_hash,
        canonical_subject=(
            "training_admission_intake:" + candidate.attribution_revision_id
        ),
        data_type="feedback_attribution_record",
        producer="phase3_training_intake_reconciliation",
        intended_consumers=(TRAINING_ADMISSION_CONSUMER,),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=evidence_refs,
        dedupe_key=(
            "phase3-training-intake:" + candidate.intake_command.command_id
        ),
        created_at=candidate.intake_command.created_at,
        retention_policy="cognitive_state",
        metadata={"revision_ids": [candidate.attribution_revision_id]},
    )


def _empty_inventory(*, status: str) -> dict[str, Any]:
    public_objects: list[dict[str, str]] = []
    material = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "spec_hash": RECONCILIATION_SPEC_HASH,
        "status": status,
        "objective_attribution_count": 0,
        "eligible_objective_attributions": 0,
        "proposed_count": 0,
        "existing_count": 0,
        "unresolved_count": 0,
        "unresolved_by_reason": {},
        "object_manifest_hash": sha256_json(public_objects),
        "source_schema_fingerprints": {},
        "source_row_counts": {},
    }
    return {
        **material,
        "inventory_hash": sha256_json(material),
        "objects": (),
        "unresolved": (),
        "sensitive_bytes_in_report": 0,
    }


def _require_expected_inventory(
    inventory: Mapping[str, Any],
    *,
    expected_inventory_hash: str,
    expected_object_manifest_hash: str,
) -> None:
    if inventory["inventory_hash"] != expected_inventory_hash:
        raise RuntimeError("phase3 intake inventory changed after review")
    if inventory["object_manifest_hash"] != expected_object_manifest_hash:
        raise RuntimeError("phase3 intake object manifest changed after review")


def _schema_fingerprints(conn: sqlite3.Connection) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for table in _STATE_TABLES:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name=? AND type IN ('table','index','trigger') "
            "ORDER BY type, name",
            (table,),
        ).fetchall()
        if not rows:
            raise RuntimeError(f"phase3 intake source table is missing: {table}")
        fingerprints[table] = sha256_json(
            [[str(value or "") for value in row] for row in rows]
        )
    return fingerprints


def _backup_databases(
    *,
    state_conn: sqlite3.Connection,
    state_path: Path,
    scoring_conn: sqlite3.Connection,
    scoring_path: Path,
    backup_dir: Path,
    inventory: Mapping[str, Any],
) -> Path:
    destination = Path(backup_dir).expanduser().resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    suffix = str(inventory["inventory_hash"]).split(":", 1)[1][:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    entries: list[dict[str, Any]] = []
    for database_class, source_path, source_conn in (
        ("state", state_path, state_conn),
        ("scoring", scoring_path, scoring_conn),
    ):
        backup_path = destination / f"{database_class}.{suffix}.{stamp}.db"
        _backup_connection(source_conn, backup_path)
        with _connect(backup_path, read_only=True) as backup_conn:
            integrity = _integrity_in_connection(backup_conn)
        if integrity != "ok":
            raise RuntimeError(f"phase3 intake backup integrity failed: {database_class}")
        entries.append(
            {
                "database_class": database_class,
                "source_path": str(source_path.resolve(strict=True)),
                "backup_path": str(backup_path),
                "file_hash": _file_hash(backup_path),
                "integrity": integrity,
            }
        )
    manifest = {
        "schema_version": "mnemos.phase3_training_intake_backup.v1",
        "inventory_hash": inventory["inventory_hash"],
        "object_manifest_hash": inventory["object_manifest_hash"],
        "object_count": inventory["proposed_count"],
        "backups": entries,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest["manifest_hash"] = sha256_json(manifest)
    manifest_path = destination / f"phase3-training-intake.{suffix}.{stamp}.json"
    _write_private_file(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest_path


def _backup_connection(source: sqlite3.Connection, destination: Path) -> None:
    descriptor = os.open(
        destination,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    os.close(descriptor)
    with _connect(destination) as target:
        source.backup(target)


def _runtime_is_active() -> bool:
    return mnemos_runtime_is_active()


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(
            f"file:{Path(path).resolve(strict=True)}?mode=ro",
            uri=True,
            timeout=30,
        )
    else:
        conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _integrity_in_connection(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _write_private_file(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _reason_code(exc: BaseException) -> str:
    value = str(exc).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return normalized[:96] or type(exc).__name__.lower()


def _call_failpoint(callback: Callable[[str], None] | None, stage: str) -> None:
    if callback is not None:
        callback(stage)


__all__ = [
    "Phase3TrainingIntakeCandidate",
    "RECONCILIATION_SCHEMA_VERSION",
    "build_phase3_training_intake_inventory",
    "public_phase3_training_intake_inventory",
    "reconcile_phase3_training_admission_intakes",
]
