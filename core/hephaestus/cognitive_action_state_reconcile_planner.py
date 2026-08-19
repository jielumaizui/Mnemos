"""Read-only planner for exact cognitive-action target-state reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from core.hephaestus.cognitive_action_state_reconcile_contracts import (
    CognitiveActionStateCandidate,
    CognitiveActionStateReconciliationPaths,
    CognitiveActionStateReconciliationPlan,
    MIGRATION_CONTRACT_HASH,
    RECONCILIATION_BATCH_TABLE,
    RECONCILIATION_SCHEMA_VERSION,
    RECONCILIATION_TABLE,
    finalize_plan_hashes,
    make_batch_id,
    make_reconciliation_id,
    reconciliation_schema_is_valid,
    validated_inventory_manifest,
)
from core.hephaestus.cognitive_action_targets import (
    TARGET_RECEIPT_SCHEMA_VERSION,
    TARGET_STATE_HASH_CONTRACT_VERSION,
    CognitiveActionTargetError,
    expected_action_owned_target_state,
    project_action_owned_target_state,
    target_state_hash,
    target_state_hash_for_contract,
    validated_cognitive_action_artifact,
)
from core.hephaestus.distill_action_store import sha256_json


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _receipt_hash(receipt: Mapping[str, Any]) -> str:
    return str(sha256_json(dict(receipt)))


def _receipt_matches(
    *,
    effect: Mapping[str, Any],
    command: Mapping[str, Any],
    receipt: Mapping[str, Any],
    target_path: Path,
) -> bool:
    expected = {
        "effect_id": effect["effect_id"],
        "cognitive_action_id": effect["cognitive_action_id"],
        "action": command["cognitive_action"],
        "target": effect["target"],
        "target_object_id": effect["target_object_id"],
        "before_hash": effect["before_hash"],
        "after_hash": effect["after_hash"],
        "expected_delta_hash": effect["expected_delta_hash"],
        "artifact_hash": command["artifact_hash"],
        "committed_at": effect["committed_at"],
        "schema_version": TARGET_RECEIPT_SCHEMA_VERSION,
    }
    if any(str(receipt.get(key) or "") != str(value) for key, value in expected.items()):
        return False
    expected_ref = (
        f"{target_path.name}:cognitive_action_target_receipts:{effect['effect_id']}"
    )
    return str(effect.get("reciprocal_receipt") or "") == expected_ref


def _recorded_contract(receipt: Mapping[str, Any]) -> str:
    try:
        detail = json.loads(str(receipt.get("detail") or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError("target receipt detail is invalid JSON") from exc
    if not isinstance(detail, Mapping):
        raise ValueError("target receipt detail is not an object")
    return str(detail.get("target_state_hash_contract") or "")


def build_reconciliation_candidate(
    *,
    command: Mapping[str, Any],
    effect: Mapping[str, Any],
    receipt: Mapping[str, Any],
    state: Mapping[str, Any],
    recorded_contract: str,
) -> CognitiveActionStateCandidate:
    """Reconstruct one exact eligible reconciliation from immutable sources."""

    artifact = validated_cognitive_action_artifact(command)
    expected = expected_action_owned_target_state(
        str(effect["target"]),
        command,
        artifact,
    )
    current = project_action_owned_target_state(str(effect["target"]), state)
    if current is None or expected != current:
        raise ValueError("action_owned_state_mismatch")
    expected_hash = target_state_hash(str(effect["target"]), expected)
    current_hash = target_state_hash(str(effect["target"]), state)
    if expected_hash != current_hash:
        raise ValueError("action_owned_state_hash_mismatch")
    effect_id = str(effect["effect_id"])
    recorded_after_hash = str(effect["after_hash"])
    return CognitiveActionStateCandidate(
        reconciliation_id=make_reconciliation_id(effect_id, recorded_after_hash),
        effect_id=effect_id,
        cognitive_action_id=str(effect["cognitive_action_id"]),
        action=str(command["cognitive_action"]),
        target=str(effect["target"]),
        target_object_id=str(effect["target_object_id"]),
        recorded_contract_version=recorded_contract,
        recorded_after_hash=recorded_after_hash,
        artifact_hash=str(command["artifact_hash"]),
        command_hash=sha256_json(dict(command)),
        effect_hash=sha256_json(dict(effect)),
        original_receipt_hash=_receipt_hash(receipt),
        target_row_hash=sha256_json(dict(state)),
        expected_state_hash=expected_hash,
        current_state_hash=current_hash,
    )


def _manifest_from_reconciliation(row: Mapping[str, Any]) -> dict[str, str]:
    keys = (
        "reconciliation_id",
        "effect_id",
        "cognitive_action_id",
        "action",
        "target",
        "target_object_id",
        "recorded_contract_version",
        "recorded_after_hash",
        "artifact_hash",
        "command_hash",
        "effect_hash",
        "original_receipt_hash",
        "target_row_hash",
        "expected_state_hash",
        "current_state_hash",
        "state_contract_version",
        "migration_contract_hash",
    )
    return {key: str(row.get(key) or "") for key in keys}


def validate_existing_reconciliation(
    connection: sqlite3.Connection,
    *,
    candidate: CognitiveActionStateCandidate,
) -> bool:
    """Verify one append-only reconciliation and its complete batch manifest."""

    if not reconciliation_schema_is_valid(connection):
        return False
    row = connection.execute(
        f"SELECT * FROM {RECONCILIATION_TABLE} WHERE effect_id=?",  # nosec B608
        (candidate.effect_id,),
    ).fetchone()
    if row is None:
        return False
    stored = dict(row)
    expected_current = candidate.manifest()
    expected_current.pop("target_row_hash")
    if any(
        str(stored.get(key) or "") != str(value)
        for key, value in expected_current.items()
    ):
        return False
    batch = connection.execute(
        f"SELECT * FROM {RECONCILIATION_BATCH_TABLE} WHERE batch_id=?",  # nosec B608
        (str(stored.get("batch_id") or ""),),
    ).fetchone()
    if batch is None:
        return False
    batch_row = dict(batch)
    if (
        str(batch_row.get("schema_version") or "") != RECONCILIATION_SCHEMA_VERSION
        or str(batch_row.get("migration_contract_hash") or "")
        != MIGRATION_CONTRACT_HASH
        or str(batch_row.get("state_contract_version") or "")
        != TARGET_STATE_HASH_CONTRACT_VERSION
        or str(stored.get("inventory_hash") or "")
        != str(batch_row.get("inventory_hash") or "")
        or str(stored.get("object_manifest_hash") or "")
        != str(batch_row.get("object_manifest_hash") or "")
        or str(stored.get("applied_at") or "")
        != str(batch_row.get("applied_at") or "")
        or str(stored.get("batch_id") or "")
        != make_batch_id(
            str(batch_row.get("inventory_hash") or ""),
            str(batch_row.get("object_manifest_hash") or ""),
        )
    ):
        return False
    try:
        manifest = json.loads(str(batch_row.get("object_manifest_json") or ""))
    except json.JSONDecodeError:
        return False
    if not isinstance(manifest, list):
        return False
    if (
        int(batch_row.get("object_count") or -1) != len(manifest)
        or sha256_json(manifest) != str(batch_row.get("object_manifest_hash") or "")
    ):
        return False
    inventory_manifest = validated_inventory_manifest(
        str(batch_row.get("inventory_manifest_json") or ""),
        inventory_hash=str(batch_row.get("inventory_hash") or ""),
        object_manifest_hash=str(batch_row.get("object_manifest_hash") or ""),
    )
    if inventory_manifest is None or manifest != inventory_manifest["candidates"]:
        return False
    stored_manifest = _manifest_from_reconciliation(stored)
    if stored_manifest not in manifest:
        return False
    inventory_entries = [
        value
        for value in inventory_manifest["inventory"]
        if isinstance(value, Mapping)
        and str(value.get("effect_id") or "") == candidate.effect_id
    ]
    if len(inventory_entries) != 1:
        return False
    source_snapshot = inventory_entries[0]
    expected_source_snapshot = {
        "effect_id": candidate.effect_id,
        "cognitive_action_id": candidate.cognitive_action_id,
        "command_hash": candidate.command_hash,
        "effect_hash": candidate.effect_hash,
        "receipt_hash": candidate.original_receipt_hash,
        "target_row_hash": str(stored.get("target_row_hash") or ""),
        "reconciliation_hash": sha256_json({}),
    }
    if dict(source_snapshot) != expected_source_snapshot:
        return False
    batch_rows = connection.execute(
        f"SELECT * FROM {RECONCILIATION_TABLE} WHERE batch_id=?",  # nosec B608
        (str(stored["batch_id"]),),
    ).fetchall()
    stored_batch_manifest = sorted(
        (_manifest_from_reconciliation(dict(value)) for value in batch_rows),
        key=lambda value: str(value.get("effect_id") or ""),
    )
    expected_batch_manifest = sorted(
        manifest,
        key=lambda value: str(value.get("effect_id") or ""),
    )
    return stored_batch_manifest == expected_batch_manifest


def _blocked(effect_id: str, action_id: str, reason: str) -> dict[str, str]:
    return {
        "effect_id": effect_id,
        "cognitive_action_id": action_id,
        "reason": reason,
    }


def build_cognitive_action_state_reconciliation_plan(
    paths: CognitiveActionStateReconciliationPaths,
) -> CognitiveActionStateReconciliationPlan:
    """Inventory all Observation effects without creating or changing a store."""

    for path in (paths.action_path, paths.observations_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    candidates: list[CognitiveActionStateCandidate] = []
    current: list[Mapping[str, str]] = []
    blocked: list[Mapping[str, str]] = []
    inventory_entries: list[Mapping[str, str]] = []
    with _connect_read_only(paths.action_path) as action_connection:
        if str(action_connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("action database integrity check failed")
        commands = {
            str(row["cognitive_action_id"]): dict(row)
            for row in action_connection.execute(
                "SELECT * FROM cognitive_action_log ORDER BY cognitive_action_id"
            )
        }
        effects = [
            dict(row)
            for row in action_connection.execute(
                """
                SELECT * FROM cognitive_action_effects
                WHERE target='observation_store'
                ORDER BY effect_id
                """
            )
        ]
    with _connect_read_only(paths.observations_path) as target_connection:
        if str(target_connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("Observation database integrity check failed")
        reconciliation_tables_present = any(
            _table_exists(target_connection, table)
            for table in (RECONCILIATION_BATCH_TABLE, RECONCILIATION_TABLE)
        )
        reconciliation_schema_valid = bool(
            not reconciliation_tables_present
            or reconciliation_schema_is_valid(target_connection)
        )
        if not reconciliation_schema_valid and not effects:
            blocked.append(
                _blocked("", "", "target_state_reconciliation_schema_drift")
            )
        for effect in effects:
            effect_id = str(effect["effect_id"])
            action_id = str(effect["cognitive_action_id"])
            command = commands.get(action_id)
            receipt = target_connection.execute(
                "SELECT * FROM cognitive_action_target_receipts WHERE effect_id=?",
                (effect_id,),
            ).fetchone()
            state = target_connection.execute(
                "SELECT * FROM observations WHERE id=?",
                (str(effect["target_object_id"]),),
            ).fetchone()
            receipt_dict = dict(receipt) if receipt is not None else {}
            state_dict = dict(state) if state is not None else {}
            reconciliation = None
            if reconciliation_schema_valid and _table_exists(
                target_connection,
                RECONCILIATION_TABLE,
            ):
                reconciliation = target_connection.execute(
                    f"SELECT * FROM {RECONCILIATION_TABLE} WHERE effect_id=?",  # nosec B608
                    (effect_id,),
                ).fetchone()
            inventory_entries.append(
                {
                    "effect_id": effect_id,
                    "cognitive_action_id": action_id,
                    "command_hash": sha256_json(command or {}),
                    "effect_hash": sha256_json(effect),
                    "receipt_hash": sha256_json(receipt_dict),
                    "target_row_hash": sha256_json(state_dict),
                    "reconciliation_hash": sha256_json(
                        dict(reconciliation) if reconciliation is not None else {}
                    ),
                }
            )
            if command is None or str(command.get("status") or "") != "applied":
                blocked.append(_blocked(effect_id, action_id, "missing_applied_command"))
                continue
            if not reconciliation_schema_valid:
                blocked.append(
                    _blocked(
                        effect_id,
                        action_id,
                        "target_state_reconciliation_schema_drift",
                    )
                )
                continue
            if Path(str(effect.get("receipt_db_path") or "")).resolve() != (
                paths.observations_path.resolve()
            ):
                blocked.append(_blocked(effect_id, action_id, "unexpected_target_database"))
                continue
            if receipt is None or not _receipt_matches(
                effect=effect,
                command=command,
                receipt=receipt_dict,
                target_path=paths.observations_path,
            ):
                blocked.append(_blocked(effect_id, action_id, "target_receipt_mismatch"))
                continue
            if state is None:
                blocked.append(_blocked(effect_id, action_id, "target_state_missing"))
                continue
            try:
                recorded_contract = _recorded_contract(receipt_dict)
                recorded_hash = target_state_hash_for_contract(
                    str(effect["target"]),
                    state_dict,
                    recorded_contract,
                )
            except (CognitiveActionTargetError, ValueError) as exc:
                blocked.append(_blocked(effect_id, action_id, str(exc)))
                continue
            if recorded_hash == str(effect["after_hash"]):
                if reconciliation is not None:
                    blocked.append(
                        _blocked(effect_id, action_id, "unexpected_reconciliation")
                    )
                else:
                    current.append({"effect_id": effect_id, "proof": "recorded_hash"})
                continue
            try:
                candidate = build_reconciliation_candidate(
                    command=command,
                    effect=effect,
                    receipt=receipt_dict,
                    state=state_dict,
                    recorded_contract=recorded_contract,
                )
            except (CognitiveActionTargetError, ValueError) as exc:
                blocked.append(_blocked(effect_id, action_id, str(exc)))
                continue
            if reconciliation is not None:
                if validate_existing_reconciliation(
                    target_connection,
                    candidate=candidate,
                ):
                    current.append({"effect_id": effect_id, "proof": "reconciliation"})
                else:
                    blocked.append(
                        _blocked(effect_id, action_id, "invalid_reconciliation")
                    )
                continue
            candidates.append(candidate)

    candidates.sort(key=lambda value: value.effect_id)
    current.sort(key=lambda value: str(value["effect_id"]))
    blocked.sort(key=lambda value: str(value["effect_id"]))
    inventory_entries.sort(key=lambda value: str(value["effect_id"]))
    inventory_manifest_json, inventory_hash, object_manifest_hash = finalize_plan_hashes(
        inventory_entries=inventory_entries,
        candidate_manifests=[value.manifest() for value in candidates],
        current=current,
        blocked=blocked,
    )
    return CognitiveActionStateReconciliationPlan(
        paths=paths,
        candidates=tuple(candidates),
        current=tuple(current),
        blocked=tuple(blocked),
        inventory_entries=tuple(inventory_entries),
        inventory_manifest_json=inventory_manifest_json,
        inventory_hash=inventory_hash,
        object_manifest_hash=object_manifest_hash,
    )


__all__ = [
    "build_reconciliation_candidate",
    "build_cognitive_action_state_reconciliation_plan",
    "validate_existing_reconciliation",
]
