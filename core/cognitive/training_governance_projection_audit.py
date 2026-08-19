"""Independent exact audit for governed-training sample projections."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from core.cognitive.access_control import cognitive_access_hash
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
)
from core.cognitive.state_types import (
    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
    COGNITIVE_TOMBSTONE_SCHEMA_VERSION,
)
from core.cognitive.training_governance_types import (
    TRAINING_PROJECTION_COMMAND,
    TRAINING_PROJECTION_CONSUMER,
    TRAINING_PROJECTION_SCHEMA,
)
from core.cognitive.training_migration_barrier import (
    assert_training_governance_enabled,
)
from core.scoring.training_schema import inspect_training_schema


_EXCLUSION_COMMAND = "exclude_governed_training_sample"
_EXCLUSION_SCHEMA = "mnemos.governed_training_sample_exclusion.v1"


def validate_admission_projection_receipt(
    *,
    state_db_path: Path,
    scoring_db_path: Path,
    revision: CognitiveStateRevision,
    command: Mapping[str, Any],
) -> str:
    """Re-prove one admission projection from both canonical databases."""

    state_path = Path(state_db_path).resolve(strict=True)
    scoring_path = Path(scoring_db_path).resolve(strict=True)
    if state_path.parent != scoring_path.parent:
        raise ValueError("training projection database directory mismatch")
    assert_training_governance_enabled(state_path.parent)
    with sqlite3.connect(
        f"file:{scoring_path}?mode=ro",
        uri=True,
    ) as conn:
        conn.row_factory = sqlite3.Row
        schema = inspect_training_schema(conn)
        if not schema.ok:
            raise RuntimeError("governed training projection schema is not canonical")
        sample_rows = {
            str(row["sample_id"]): row
            for row in conn.execute(
                "SELECT * FROM governed_training_samples WHERE admission_revision_id=?",
                (revision.revision_id,),
            ).fetchall()
        }
        action_rows = {
            str(row["action_id"]): row
            for row in conn.execute(
                "SELECT * FROM governed_training_sample_actions "
                "WHERE admission_revision_id=?",
                (revision.revision_id,),
            ).fetchall()
        }
        receipt_rows = {
            str(row["receipt_id"]): row
            for row in conn.execute(
                "SELECT * FROM governed_training_sample_receipts "
                "WHERE admission_revision_id=?",
                (revision.revision_id,),
            ).fetchall()
        }
    try:
        state_receipt = _load_state_effect_receipt(
            state_path,
            command_id=str(command["command_id"]),
        )
        revision_row = {
            "revision_id": revision.revision_id,
            "object_type": revision.object_type,
            "object_id": revision.object_id,
            "payload": revision.payload,
            "payload_hash": revision.payload_hash,
        }
        return _audit_admission_projection(
            command,
            revisions={revision.revision_id: revision_row},
            state_effect_receipts={str(command["command_id"]): state_receipt},
            sample_rows=sample_rows,
            action_rows=action_rows,
            receipt_rows=receipt_rows,
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RuntimeError("training admission projection proof mismatch") from exc


def _load_state_effect_receipt(
    state_db_path: Path,
    *,
    command_id: str,
) -> dict[str, Any]:
    """Load one effect receipt together with its exact consumption proof."""

    with sqlite3.connect(
        f"file:{state_db_path}?mode=ro",
        uri=True,
    ) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT receipt.receipt_id, receipt.command_id, receipt.revision_id,
                   receipt.event_id, receipt.consumer_id, receipt.consumption_id,
                   receipt.status, receipt.target_effect_id, receipt.before_hash,
                   receipt.after_hash, receipt.evidence_refs, receipt.created_at,
                   consumption.event_id AS consumption_event_id,
                   consumption.consumer_id AS consumption_consumer_id,
                   consumption.outcome AS consumption_outcome,
                   consumption.status AS consumption_status,
                   consumption.target_effect_id AS consumption_target_effect_id,
                   consumption.before_hash AS consumption_before_hash,
                   consumption.after_hash AS consumption_after_hash,
                   consumption.effect_evidence_refs AS consumption_evidence_refs,
                   consumption.action_changed AS consumption_action_changed,
                   consumption.metadata AS consumption_metadata,
                   consumption.idempotency_key AS consumption_idempotency_key,
                   COALESCE(consumption.supersedes_consumption_id, '')
                       AS consumption_supersedes_id,
                   COALESCE(consumption.correction_of_consumption_id, '')
                       AS consumption_correction_id,
                   consumption.receipt_state AS consumption_receipt_state,
                   consumption.created_at AS consumption_created_at,
                   head.consumption_id AS head_consumption_id
            FROM cognitive_state_effect_receipts AS receipt
            JOIN cognitive_data_consumptions AS consumption
              ON consumption.consumption_id=receipt.consumption_id
            LEFT JOIN cognitive_data_consumer_heads AS head
              ON head.event_id=consumption.event_id
             AND head.consumer_id=consumption.consumer_id
            WHERE receipt.command_id=?
            """,
            (command_id,),
        ).fetchone()
    if row is None:
        raise ValueError("training projection effect receipt is unavailable")
    receipt = dict(row)
    receipt["evidence_refs"] = tuple(json.loads(str(receipt["evidence_refs"])))
    receipt["consumption_evidence_refs"] = tuple(
        json.loads(str(receipt["consumption_evidence_refs"]))
    )
    receipt["consumption_metadata"] = json.loads(
        str(receipt["consumption_metadata"])
    )
    return receipt


def audit_sample_projection_receipts(
    conn: sqlite3.Connection,
    *,
    revisions: Mapping[str, Mapping[str, Any]],
    commands: Mapping[str, Mapping[str, Any]],
    state_effect_receipts: Mapping[str, Mapping[str, Any]],
    metrics: dict[str, int],
    denominators: dict[str, int],
) -> None:
    """Reconstruct each sample projection without using its production writer."""

    projection_commands = sorted(
        (
            command
            for command in commands.values()
            if command.get("command_type")
            in {
                TRAINING_PROJECTION_COMMAND,
                _EXCLUSION_COMMAND,
                COGNITIVE_TOMBSTONE_COMMAND_TYPE,
            }
        ),
        key=lambda item: str(item.get("command_id") or ""),
    )
    admission_commands = [
        command
        for command in projection_commands
        if command.get("command_type") == TRAINING_PROJECTION_COMMAND
    ]
    exclusion_commands = [
        command
        for command in projection_commands
        if command.get("command_type") == _EXCLUSION_COMMAND
    ]
    tombstone_commands = [
        command
        for command in projection_commands
        if command.get("command_type") == COGNITIVE_TOMBSTONE_COMMAND_TYPE
    ]
    sample_rows = {
        str(row["sample_id"]): row
        for row in conn.execute(
            "SELECT * FROM governed_training_samples ORDER BY sample_id"
        ).fetchall()
    }
    action_rows = {
        str(row["action_id"]): row
        for row in conn.execute(
            "SELECT * FROM governed_training_sample_actions ORDER BY action_id"
        ).fetchall()
    }
    receipt_rows = {
        str(row["receipt_id"]): row
        for row in conn.execute(
            "SELECT * FROM governed_training_sample_receipts ORDER BY receipt_id"
        ).fetchall()
    }
    denominators["sample_projection_commands"] = len(projection_commands)
    denominators["sample_admission_commands"] = len(admission_commands)
    denominators["sample_exclusion_commands"] = len(exclusion_commands)
    denominators["sample_tombstone_commands"] = len(tombstone_commands)
    denominators["sample_projection_receipts"] = len(receipt_rows)

    expected_sample_ids: set[str] = set()
    expected_action_ids: set[str] = set()
    expected_receipt_ids: set[str] = set()
    for command in admission_commands:
        revision = revisions.get(str(command.get("revision_id") or ""))
        if revision is not None and revision.get("object_type") == ("training_admission_record"):
            try:
                suffix = str(revision["object_id"]).rsplit("-", 1)[1]
                expected_sample_ids.add("training-sample-" + suffix)
                expected_action_ids.add("training-sample-action-" + suffix)
                expected_receipt_ids.add(
                    str(revision["payload"]["target_effect_refs"]["reciprocal_receipt_id"])
                )
            except (KeyError, IndexError):
                pass
        try:
            _audit_admission_projection(
                command,
                revisions=revisions,
                state_effect_receipts=state_effect_receipts,
                sample_rows=sample_rows,
                action_rows=action_rows,
                receipt_rows=receipt_rows,
            )
        except (KeyError, TypeError, ValueError, IndexError):
            metrics["training_effect_without_receipt"] += 1

    # The aggregate row-level check already counts an extra sample whose latest
    # action or receipt is missing. Count only the stronger forgery case here:
    # a fully populated sample projection with no immutable admission command.
    action_sample_ids = {str(row["sample_id"]) for row in action_rows.values()}
    receipt_sample_ids = {str(row["sample_id"]) for row in receipt_rows.values()}
    metrics["training_effect_without_receipt"] += len(
        (set(sample_rows) - expected_sample_ids) & action_sample_ids & receipt_sample_ids
    )

    for command in exclusion_commands:
        correction = revisions.get(str(command.get("revision_id") or ""))
        if correction is not None:
            try:
                suffix = sha256_json(
                    {
                        "admission_revision_id": correction["payload"]["correction_of_revision_id"],
                        "corrected_outcome_revision_id": correction["source_revision_id"],
                    }
                ).split(":", 1)[1][:32]
                expected_action_ids.add("training-sample-exclude-action-" + suffix)
                expected_receipt_ids.add("training-sample-exclude-receipt-" + suffix)
            except (KeyError, TypeError, ValueError):
                pass
        try:
            _audit_exclusion_projection(
                command,
                revisions=revisions,
                state_effect_receipts=state_effect_receipts,
                sample_rows=sample_rows,
                action_rows=action_rows,
                receipt_rows=receipt_rows,
            )
        except (KeyError, TypeError, ValueError, IndexError):
            metrics["training_effect_without_receipt"] += 1

    expected_tombstone_action_ids: set[str] = set()
    expected_tombstone_receipt_ids: set[str] = set()
    for command in tombstone_commands:
        try:
            target_ids = {str(value) for value in command["payload"]["target_revision_ids"]}
            for sample in sample_rows.values():
                if str(sample["admission_revision_id"]) not in target_ids:
                    continue
                sample_id = str(sample["sample_id"])
                suffix = sha256_json(
                    {"command_id": command["command_id"], "sample_id": sample_id}
                ).split(":", 1)[1][:32]
                expected_tombstone_action_ids.add("training-sample-tombstone-action-" + suffix)
                expected_tombstone_receipt_ids.add("training-sample-tombstone-receipt-" + suffix)
        except (KeyError, TypeError, ValueError):
            pass
        try:
            action_ids, receipt_ids = _audit_tombstone_projection(
                conn,
                command,
                state_effect_receipts=state_effect_receipts,
                sample_rows=sample_rows,
                action_rows=action_rows,
                receipt_rows=receipt_rows,
            )
            expected_tombstone_action_ids.update(action_ids)
            expected_tombstone_receipt_ids.update(receipt_ids)
        except (KeyError, TypeError, ValueError, IndexError, json.JSONDecodeError):
            metrics["training_effect_without_receipt"] += 1
    expected_action_ids.update(expected_tombstone_action_ids)
    expected_receipt_ids.update(expected_tombstone_receipt_ids)
    metrics["training_effect_without_receipt"] += len(
        expected_action_ids.symmetric_difference(set(action_rows))
    ) + len(expected_receipt_ids.symmetric_difference(set(receipt_rows)))


def _audit_admission_projection(
    command: Mapping[str, Any],
    *,
    revisions: Mapping[str, Mapping[str, Any]],
    state_effect_receipts: Mapping[str, Mapping[str, Any]],
    sample_rows: Mapping[str, sqlite3.Row],
    action_rows: Mapping[str, sqlite3.Row],
    receipt_rows: Mapping[str, sqlite3.Row],
) -> str:
    revision = revisions[str(command["revision_id"])]
    payload = revision["payload"]
    if (
        revision["object_type"] != "training_admission_record"
        or payload["lifecycle_state"] != "admitted"
        or command["consumer_id"] != TRAINING_PROJECTION_CONSUMER
    ):
        raise ValueError("admission projection source is invalid")
    suffix = str(revision["object_id"]).rsplit("-", 1)[1]
    sample_id = "training-sample-" + suffix
    action_id = "training-sample-action-" + suffix
    targets = payload["target_effect_refs"]
    expected_payload = {
        "schema_version": TRAINING_PROJECTION_SCHEMA,
        "projection_command_key": str(targets["projection_command_key"]),
        "admission_id": str(revision["object_id"]),
        "admission_revision_id": str(revision["revision_id"]),
        "admission_payload_hash": str(revision["payload_hash"]),
        "sample_id": sample_id,
        "dimension": payload["feature_snapshot"]["dimension"],
        "metric_id": payload["label"]["metric_id"],
        "feature_snapshot": dict(payload["feature_snapshot"]),
        "label_numeric": payload["label"]["numeric_value"],
        "label_value": payload["label"]["observed_value"],
        "dataset_assignment": dict(payload["dataset_assignment"]),
        "access_control_hash": cognitive_access_hash(payload["access_control"]),
        "projection_effect_id": str(targets["projection_effect_id"]),
        "projection_receipt_id": str(targets["reciprocal_receipt_id"]),
    }
    _assert_exact_command(command, expected_payload=expected_payload)

    created_at = str(command["created_at"])
    before_hash = sha256_json({"sample_id": sample_id, "state": "absent"})
    after_hash = sha256_json(
        {
            "sample_id": sample_id,
            "admission_revision_id": revision["revision_id"],
            "admission_payload_hash": revision["payload_hash"],
            "feature_snapshot_hash": payload["feature_snapshot"]["snapshot_hash"],
            "label_numeric": payload["label"]["numeric_value"],
            "dataset_split": payload["dataset_assignment"]["split"],
        }
    )
    expected_sample = (
        sample_id,
        str(revision["revision_id"]),
        str(revision["payload_hash"]),
        str(expected_payload["dimension"]),
        str(expected_payload["metric_id"]),
        canonical_json(expected_payload["feature_snapshot"]),
        str(expected_payload["feature_snapshot"]["snapshot_hash"]),
        int(expected_payload["label_numeric"]),
        str(expected_payload["label_value"]),
        str(expected_payload["dataset_assignment"]["group_id"]),
        str(expected_payload["dataset_assignment"]["group_hash"]),
        str(expected_payload["dataset_assignment"]["split"]),
        str(expected_payload["access_control_hash"]),
        created_at,
    )
    expected_action = (
        action_id,
        sample_id,
        str(revision["revision_id"]),
        "admit",
        "objective_outcome_verified",
        None,
        str(revision["payload_hash"]),
        created_at,
    )
    evidence_refs = [
        f"training-admission:{revision['revision_id']}",
        f"training-projection-command:{command['command_id']}",
    ]
    receipt_id = str(expected_payload["projection_receipt_id"])
    receipt_identity = {
        "receipt_id": receipt_id,
        "command_id": str(command["command_id"]),
        "admission_revision_id": str(revision["revision_id"]),
        "sample_id": sample_id,
        "action_id": action_id,
        "status": "committed",
        "before_hash": before_hash,
        "after_hash": after_hash,
        "evidence_refs": evidence_refs,
    }
    receipt_hash = sha256_json(receipt_identity)
    expected_receipt = (
        receipt_id,
        str(command["command_id"]),
        str(revision["revision_id"]),
        sample_id,
        action_id,
        "committed",
        before_hash,
        after_hash,
        canonical_json(evidence_refs),
        receipt_hash,
        created_at,
    )
    _assert_row(sample_rows.get(sample_id), expected_sample)
    _assert_row(action_rows.get(action_id), expected_action)
    _assert_row(receipt_rows.get(receipt_id), expected_receipt)
    _assert_state_receipt(
        state_effect_receipts.get(str(command["command_id"])),
        command=command,
        target_effect_id=str(expected_payload["projection_effect_id"]),
        before_hash=before_hash,
        after_hash=after_hash,
        evidence_refs=(
            f"training-admission:{revision['revision_id']}",
            f"governed-training-sample:{sample_id}",
            f"governed-training-receipt:{receipt_id}:{receipt_hash}",
        ),
        outcome="governed training sample projection committed",
    )
    return sample_id


def _audit_exclusion_projection(
    command: Mapping[str, Any],
    *,
    revisions: Mapping[str, Mapping[str, Any]],
    state_effect_receipts: Mapping[str, Mapping[str, Any]],
    sample_rows: Mapping[str, sqlite3.Row],
    action_rows: Mapping[str, sqlite3.Row],
    receipt_rows: Mapping[str, sqlite3.Row],
) -> None:
    correction = revisions[str(command["revision_id"])]
    payload = correction["payload"]
    original_revision_id = str(payload["correction_of_revision_id"])
    original = revisions[original_revision_id]
    corrected_outcome_revision_id = str(correction["source_revision_id"])
    suffix = sha256_json(
        {
            "admission_revision_id": original_revision_id,
            "corrected_outcome_revision_id": corrected_outcome_revision_id,
        }
    ).split(":", 1)[1][:32]
    sample_id = "training-sample-" + str(correction["object_id"]).rsplit("-", 1)[1]
    action_id = "training-sample-exclude-action-" + suffix
    effect_id = "training-sample-exclude-effect-" + suffix
    receipt_id = "training-sample-exclude-receipt-" + suffix
    expected_payload = {
        "schema_version": _EXCLUSION_SCHEMA,
        "admission_id": str(correction["object_id"]),
        "original_admission_revision_id": original_revision_id,
        "correction_revision_id": str(correction["revision_id"]),
        "correction_payload_hash": str(correction["payload_hash"]),
        "corrected_outcome_revision_id": corrected_outcome_revision_id,
        "sample_id": sample_id,
        "action_id": action_id,
        "reason_code": "outcome_corrected",
        "projection_effect_id": effect_id,
        "projection_receipt_id": receipt_id,
    }
    if (
        correction["object_type"] != "training_admission_record"
        or payload["lifecycle_state"] != "excluded"
        or original["object_type"] != "training_admission_record"
        or original["object_id"] != correction["object_id"]
        or original["payload"]["lifecycle_state"] != "admitted"
        or command["consumer_id"] != TRAINING_PROJECTION_CONSUMER
        or payload["target_effect_refs"]
        != {
            "projection_command_key": "training-exclusion:" + suffix,
            "projection_effect_id": effect_id,
            "reciprocal_receipt_id": receipt_id,
        }
    ):
        raise ValueError("exclusion projection source is invalid")
    _assert_exact_command(command, expected_payload=expected_payload)

    prior_action_id = "training-sample-action-" + sample_id.rsplit("-", 1)[1]
    before_hash = sha256_json(
        {
            "sample_id": sample_id,
            "admission_revision_id": original_revision_id,
            "state": "admitted",
        }
    )
    after_hash = sha256_json(
        {
            "sample_id": sample_id,
            "admission_revision_id": original_revision_id,
            "correction_revision_id": correction["revision_id"],
            "state": "excluded",
            "reason_code": "outcome_corrected",
        }
    )
    created_at = str(command["created_at"])
    expected_action = (
        action_id,
        sample_id,
        original_revision_id,
        "exclude",
        "outcome_corrected",
        prior_action_id,
        str(correction["payload_hash"]),
        created_at,
    )
    evidence_refs = [
        f"training-admission:{original_revision_id}",
        f"training-correction:{correction['revision_id']}",
        f"corrected-outcome:{corrected_outcome_revision_id}",
    ]
    receipt_identity = {
        "receipt_id": receipt_id,
        "command_id": str(command["command_id"]),
        "admission_revision_id": original_revision_id,
        "sample_id": sample_id,
        "action_id": action_id,
        "status": "revoked",
        "before_hash": before_hash,
        "after_hash": after_hash,
        "evidence_refs": evidence_refs,
    }
    receipt_hash = sha256_json(receipt_identity)
    expected_receipt = (
        receipt_id,
        str(command["command_id"]),
        original_revision_id,
        sample_id,
        action_id,
        "revoked",
        before_hash,
        after_hash,
        canonical_json(evidence_refs),
        receipt_hash,
        created_at,
    )
    sample = sample_rows.get(sample_id)
    if sample is None or str(sample["admission_revision_id"]) != original_revision_id:
        raise ValueError("exclusion projection sample mismatch")
    _assert_row(action_rows.get(action_id), expected_action)
    _assert_row(receipt_rows.get(receipt_id), expected_receipt)
    _assert_state_receipt(
        state_effect_receipts.get(str(command["command_id"])),
        command=command,
        target_effect_id=effect_id,
        before_hash=before_hash,
        after_hash=after_hash,
        evidence_refs=(
            f"training-correction:{correction['revision_id']}",
            f"governed-training-sample:{sample_id}",
            f"governed-training-exclusion-receipt:{receipt_id}:{receipt_hash}",
        ),
        outcome="governed training sample exclusion committed",
    )


def _audit_tombstone_projection(
    conn: sqlite3.Connection,
    command: Mapping[str, Any],
    *,
    state_effect_receipts: Mapping[str, Mapping[str, Any]],
    sample_rows: Mapping[str, sqlite3.Row],
    action_rows: Mapping[str, sqlite3.Row],
    receipt_rows: Mapping[str, sqlite3.Row],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    payload = command["payload"]
    raw_targets = payload["target_revision_ids"]
    target_revision_ids = tuple(sorted(str(value) for value in raw_targets))
    if (
        command["consumer_id"] != TRAINING_PROJECTION_CONSUMER
        or command["command_type"] != COGNITIVE_TOMBSTONE_COMMAND_TYPE
        or payload["schema_version"] != COGNITIVE_TOMBSTONE_SCHEMA_VERSION
        or not target_revision_ids
        or len(target_revision_ids) != len(set(target_revision_ids))
        or TRAINING_PROJECTION_CONSUMER not in set(payload["required_consumers"])
        or not str(payload["request_id"])
        or not str(payload["before_hash"]).startswith("sha256:")
        or not str(payload["tombstone_hash"]).startswith("sha256:")
    ):
        raise ValueError("training tombstone command contract mismatch")
    _assert_exact_command(command, expected_payload=payload)

    target_set = set(target_revision_ids)
    samples = sorted(
        (row for row in sample_rows.values() if str(row["admission_revision_id"]) in target_set),
        key=lambda row: str(row["sample_id"]),
    )
    model_ids: list[str] = []
    for model in conn.execute(
        "SELECT model_id, run_revision_id, admission_revision_ids_json "
        "FROM governed_scorer_models ORDER BY model_id"
    ).fetchall():
        admission_ids = {
            str(value) for value in json.loads(str(model["admission_revision_ids_json"]))
        }
        if str(model["run_revision_id"]) in target_set or admission_ids & target_set:
            model_ids.append(str(model["model_id"]))
    model_ids = sorted(set(model_ids))
    remaining_model_head_count = 0
    if model_ids:
        remaining_model_head_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM governed_scorer_model_heads "
                "WHERE model_id IN (SELECT value FROM json_each(?))",
                (canonical_json(model_ids),),
            ).fetchone()[0]
        )

    action_ids: list[str] = []
    receipt_ids: list[str] = []
    for sample in samples:
        sample_id = str(sample["sample_id"])
        admission_revision_id = str(sample["admission_revision_id"])
        suffix = sha256_json({"command_id": command["command_id"], "sample_id": sample_id}).split(
            ":", 1
        )[1][:32]
        action_id = "training-sample-tombstone-action-" + suffix
        receipt_id = "training-sample-tombstone-receipt-" + suffix
        prior_actions = sorted(
            (
                row
                for candidate_id, row in action_rows.items()
                if str(row["sample_id"]) == sample_id and candidate_id != action_id
            ),
            key=lambda row: (str(row["created_at"]), str(row["action_id"])),
        )
        if not prior_actions:
            raise ValueError("training tombstone prior action is missing")
        prior = prior_actions[-1]
        before_hash = sha256_json(
            {
                "sample_id": sample_id,
                "latest_action_id": str(prior["action_id"]),
                "latest_action_type": str(prior["action_type"]),
            }
        )
        after_hash = sha256_json(
            {
                "sample_id": sample_id,
                "admission_revision_id": admission_revision_id,
                "action_id": action_id,
                "state": "excluded",
                "reason_code": "subject_tombstone",
                "tombstone_hash": str(payload["tombstone_hash"]),
            }
        )
        expected_action = (
            action_id,
            sample_id,
            admission_revision_id,
            "exclude",
            "subject_tombstone",
            str(prior["action_id"]),
            str(payload["tombstone_hash"]),
            str(command["created_at"]),
        )
        evidence_refs = [
            f"tombstone-command:{command['command_id']}",
            f"training-admission:{admission_revision_id}",
            f"governed-training-sample:{sample_id}",
        ]
        receipt_identity = {
            "receipt_id": receipt_id,
            "command_id": str(command["command_id"]),
            "admission_revision_id": admission_revision_id,
            "sample_id": sample_id,
            "action_id": action_id,
            "status": "revoked",
            "before_hash": before_hash,
            "after_hash": after_hash,
            "evidence_refs": evidence_refs,
        }
        expected_receipt = (
            receipt_id,
            str(command["command_id"]),
            admission_revision_id,
            sample_id,
            action_id,
            "revoked",
            before_hash,
            after_hash,
            canonical_json(evidence_refs),
            sha256_json(receipt_identity),
            str(command["created_at"]),
        )
        _assert_row(action_rows.get(action_id), expected_action)
        _assert_row(receipt_rows.get(receipt_id), expected_receipt)
        action_ids.append(action_id)
        receipt_ids.append(receipt_id)

    oracle = {
        "command_id": str(command["command_id"]),
        "target_revision_ids": list(target_revision_ids),
        "sample_ids": [str(row["sample_id"]) for row in samples],
        "action_ids": action_ids,
        "receipt_ids": receipt_ids,
        "deactivated_model_ids": model_ids,
        "remaining_model_head_count": remaining_model_head_count,
    }
    state_evidence = (
        f"tombstone-command:{command['command_id']}",
        "tombstone-oracle:governed-training:" + sha256_json(oracle),
        *(f"governed-training-tombstone-receipt:{receipt_id}" for receipt_id in receipt_ids),
    )
    _assert_state_receipt(
        state_effect_receipts.get(str(command["command_id"])),
        command=command,
        target_effect_id=(f"tombstone:{TRAINING_PROJECTION_CONSUMER}:{payload['request_id']}"),
        before_hash=str(payload["before_hash"]),
        after_hash=str(payload["tombstone_hash"]),
        evidence_refs=state_evidence,
        outcome="governed training projection tombstoned",
    )
    if remaining_model_head_count:
        raise ValueError("training tombstone model head survived")
    return tuple(action_ids), tuple(receipt_ids)


def _assert_exact_command(
    command: Mapping[str, Any],
    *,
    expected_payload: Mapping[str, Any],
) -> None:
    reconstructed = LocalConsumerCommand.create(
        revision_id=str(command["revision_id"]),
        consumer_id=str(command["consumer_id"]),
        command_type=str(command["command_type"]),
        payload=expected_payload,
        created_at=str(command["created_at"]),
    )
    if (
        command["payload"] != expected_payload
        or str(command["payload_hash"]) != reconstructed.payload_hash
        or str(command["command_id"]) != reconstructed.command_id
    ):
        raise ValueError("sample projection command identity mismatch")


def _assert_row(row: sqlite3.Row | None, expected: tuple[Any, ...]) -> None:
    if row is None or tuple(row) != expected:
        raise ValueError("sample projection row mismatch")


def _assert_state_receipt(
    receipt: Mapping[str, Any] | None,
    *,
    command: Mapping[str, Any],
    target_effect_id: str,
    before_hash: str,
    after_hash: str,
    evidence_refs: tuple[str, ...],
    outcome: str,
) -> None:
    identity = {
        "command_id": str(command["command_id"]),
        "status": "committed",
        "target_effect_id": target_effect_id,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "evidence_refs": list(evidence_refs),
        "terminal_reason_code": "",
        "retry_exhausted": False,
    }
    expected_receipt_id = "cogeffect-" + sha256_json(identity).split(":", 1)[1][:32]
    consumption_id = str((receipt or {}).get("consumption_id") or "")
    expected_consumption_refs = (
        *evidence_refs,
        f"cognitive-effect-receipt:{expected_receipt_id}",
    )
    expected_metadata = {
        "command_id": str(command["command_id"]),
        "effect_receipt_id": expected_receipt_id,
        "terminal_reason_code": "",
        "retry_exhausted": False,
    }
    if (
        receipt is None
        or str(receipt.get("receipt_id") or "") != expected_receipt_id
        or receipt.get("command_id") != command["command_id"]
        or receipt.get("revision_id") != command["revision_id"]
        or receipt.get("event_id") != command["event_id"]
        or receipt.get("consumer_id") != command["consumer_id"]
        or receipt.get("status") != "committed"
        or receipt.get("target_effect_id") != target_effect_id
        or receipt.get("before_hash") != before_hash
        or receipt.get("after_hash") != after_hash
        or tuple(receipt.get("evidence_refs") or ()) != evidence_refs
        or receipt.get("created_at") != command["created_at"]
        or not consumption_id
        or receipt.get("consumption_event_id") != command["event_id"]
        or receipt.get("consumption_consumer_id") != command["consumer_id"]
        or receipt.get("consumption_outcome") != outcome
        or receipt.get("consumption_status") != "committed"
        or receipt.get("consumption_target_effect_id") != target_effect_id
        or receipt.get("consumption_before_hash") != before_hash
        or receipt.get("consumption_after_hash") != after_hash
        or tuple(receipt.get("consumption_evidence_refs") or ()) != expected_consumption_refs
        or receipt.get("consumption_action_changed") != int(before_hash != after_hash)
        or receipt.get("consumption_metadata") != expected_metadata
        or receipt.get("consumption_idempotency_key") != f"cognitive-effect:{expected_receipt_id}"
        or receipt.get("consumption_supersedes_id")
        or receipt.get("consumption_correction_id")
        or receipt.get("consumption_receipt_state") != "active"
        or receipt.get("consumption_created_at") != command["created_at"]
        or receipt.get("head_consumption_id") != consumption_id
    ):
        raise ValueError("sample projection reciprocal receipt mismatch")
