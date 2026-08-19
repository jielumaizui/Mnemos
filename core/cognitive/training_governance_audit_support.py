"""Projection, lineage, and static-scan helpers for governed-training audit."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from core.cognitive.state_contract import LocalConsumerCommand, canonical_json, sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.prediction_ledger import (
    PREDICTION_TERMINAL_COMMAND,
    PREDICTION_TERMINAL_CONSUMER,
)
from core.utils import read_text_value


def _expected_run_receipt(
    run: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    revisions: Mapping[str, Mapping[str, Any]],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    payload = run["payload"]
    state = str(payload["state"])
    model = payload["model_artifact"]
    parent = payload["parent_model_ref"]
    if state == "stale":
        superseded = revisions[str(run["supersedes_revision_id"])]
        before_hash = sha256_json(
            {
                "run_revision_id": superseded["revision_id"],
                "run_payload_hash": superseded["payload_hash"],
                "model_id": model["model_id"],
                "model_hash": model["blob_hash"],
                "state": superseded["payload"]["state"],
            }
        )
        after_hash = sha256_json(
            {
                "run_revision_id": run["revision_id"],
                "run_payload_hash": run["payload_hash"],
                "state": "stale",
            }
        )
        status = "stale"
        receipt_model_id = (
            str(model["model_id"]) if superseded["payload"]["state"] == "applied" else None
        )
    else:
        before_hash = sha256_json(
            {
                "dimension": payload["dimension"],
                "model_id": parent["model_id"],
                "model_hash": parent["model_hash"],
            }
        )
        if state == "applied":
            after_hash = sha256_json(
                {
                    "dimension": payload["dimension"],
                    "model_id": model["model_id"],
                    "model_hash": model["blob_hash"],
                    "run_revision_id": run["revision_id"],
                }
            )
            status = "committed"
            receipt_model_id = str(model["model_id"])
        else:
            after_hash = sha256_json(
                {
                    "run_revision_id": run["revision_id"],
                    "run_payload_hash": run["payload_hash"],
                    "state": state,
                }
            )
            status = state
            receipt_model_id = None
    evidence_refs = [
        f"training-run:{run['revision_id']}",
        f"training-manifest:{payload['dataset_manifest']['manifest_hash']}",
        f"training-projection-command:{command['command_id']}",
    ]
    if receipt_model_id:
        evidence_refs.append(f"governed-scorer-model:{receipt_model_id}")
    identity = {
        "receipt_id": str(command["payload"]["projection_receipt_id"]),
        "command_id": str(command["command_id"]),
        "run_revision_id": str(run["revision_id"]),
        "run_payload_hash": str(run["payload_hash"]),
        "model_id": receipt_model_id,
        "status": status,
        "action_id": str(payload["material_effect_refs"]["action_id"]),
        "effect_id": str(payload["material_effect_refs"]["effect_id"]),
        "before_hash": before_hash,
        "after_hash": after_hash,
        "evidence_refs": evidence_refs,
    }
    receipt_hash = sha256_json(identity)
    return (
        (
            identity["receipt_id"],
            identity["command_id"],
            identity["run_revision_id"],
            identity["run_payload_hash"],
            receipt_model_id,
            status,
            identity["action_id"],
            identity["effect_id"],
            before_hash,
            after_hash,
            canonical_json(evidence_refs),
            receipt_hash,
            str(command["created_at"]),
        ),
        {"before_hash": before_hash, "after_hash": after_hash},
    )


def _expected_aux_projection(
    run: Mapping[str, Any],
    command: Mapping[str, Any],
    *,
    run_before_hash: str,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]], tuple[str, ...]]:
    payload = run["payload"]
    state = str(payload["state"])
    status = "committed" if state == "applied" else state
    effects: list[tuple[Any, ...]] = []
    receipts: list[tuple[Any, ...]] = []
    refs: list[str] = []
    for effect_kind, artifact_key in (
        ("bayesian_prior", "bayesian_prior_artifact"),
        ("rule_optimizer", "rule_optimizer_artifact"),
    ):
        artifact = dict(payload[artifact_key])
        effect_id = str(artifact.get("effect_id") or "")
        artifact_hash = str(artifact.get("artifact_hash") or "")
        input_hash = str(artifact.get("input_hash") or "")
        persisted_effect_id = effect_id if state == "applied" else None
        if state == "applied":
            effects.append(
                (
                    effect_id,
                    effect_kind,
                    str(run["revision_id"]),
                    str(run["payload_hash"]),
                    canonical_json(artifact["admission_revision_ids"]),
                    str(payload["dimension"]),
                    input_hash,
                    canonical_json(artifact),
                    artifact_hash,
                    str(command["created_at"]),
                )
            )
        receipt_id = (
            "governed-training-"
            + effect_kind.replace("_", "-")
            + "-"
            + status.replace("_", "-")
            + "-receipt-"
            + sha256_json(
                {
                    "command_id": command["command_id"],
                    "effect_kind": effect_kind,
                    "status": status,
                }
            ).split(":", 1)[1][:32]
        )
        before_hash = sha256_json({"effect_kind": effect_kind, "run_before_hash": run_before_hash})
        after_hash = sha256_json(
            {
                "effect_kind": effect_kind,
                "run_revision_id": run["revision_id"],
                "run_payload_hash": run["payload_hash"],
                "effect_id": effect_id,
                "artifact_hash": artifact_hash,
                "status": status,
            }
        )
        evidence_refs = [
            f"training-run:{run['revision_id']}",
            f"training-aux-effect:{effect_kind}:{input_hash or 'insufficient'}",
        ]
        if persisted_effect_id:
            evidence_refs.append(f"governed-training-aux-effect:{persisted_effect_id}")
        identity = {
            "receipt_id": receipt_id,
            "command_id": command["command_id"],
            "run_revision_id": run["revision_id"],
            "effect_kind": effect_kind,
            "effect_id": persisted_effect_id,
            "status": status,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "evidence_refs": evidence_refs,
        }
        receipt_hash = sha256_json(identity)
        receipts.append(
            (
                receipt_id,
                str(command["command_id"]),
                str(run["revision_id"]),
                effect_kind,
                persisted_effect_id,
                status,
                before_hash,
                after_hash,
                canonical_json(evidence_refs),
                receipt_hash,
                str(command["created_at"]),
            )
        )
        refs.append(f"governed-training-aux-receipt:{receipt_id}:{receipt_hash}")
    return effects, receipts, tuple(refs)


def _prediction_outcome_identity_matches(
    prediction: Mapping[str, Any],
    outcome: Mapping[str, Any],
    admission: Mapping[str, Any],
) -> bool:
    outcome_prediction = outcome.get("prediction_ref")
    if not isinstance(outcome_prediction, Mapping):
        return False
    return bool(
        str(outcome_prediction.get("revision_id") or "")
        == str(admission["prediction_ref"]["revision_id"])
        and prediction.get("subject") == admission["subject"]
        and str(admission["outcome_ref"]["object_id"])
        == str(outcome.get("outcome_id") or admission["outcome_ref"]["object_id"])
    )


def _terminal_prediction_matches(
    sealed: Mapping[str, Any] | None,
    terminal: Mapping[str, Any] | None,
    outcome: Mapping[str, Any] | None,
    admission: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
) -> bool:
    if sealed is None or terminal is None or outcome is None:
        return False
    ref = admission.get("prediction_terminal_ref")
    if not isinstance(ref, Mapping):
        return False
    terminal_payload = terminal.get("payload")
    sealed_payload = sealed.get("payload")
    outcome_payload = outcome.get("payload")
    if not isinstance(terminal_payload, Mapping):
        return False
    if not isinstance(sealed_payload, Mapping):
        return False
    if not isinstance(outcome_payload, Mapping):
        return False
    current = _current_revision_for_object(
        revisions,
        object_type="prediction_record",
        object_id=str(terminal.get("object_id") or ""),
    )
    temporal = admission.get("temporal_proof")
    if not isinstance(temporal, Mapping):
        return False
    return bool(
        terminal.get("object_type") == "prediction_record"
        and terminal.get("payload_hash") == ref.get("payload_hash")
        and terminal.get("revision_id") == ref.get("revision_id")
        and terminal.get("object_id") == ref.get("object_id")
        and current is not None
        and current.get("revision_id") == terminal.get("revision_id")
        and terminal_payload.get("revision_state") == "terminal"
        and terminal_payload.get("terminal", {}).get("state") == "measured"
        and terminal_payload.get("prediction_input_hash")
        == sealed_payload.get("prediction_input_hash")
        and terminal_payload.get("outcome_ref")
        == {
            "revision_id": outcome.get("revision_id"),
            "payload_hash": outcome.get("payload_hash"),
        }
        and terminal_payload.get("calibration")
        == {"eligible": True, "exclusion_reason": ""}
        and terminal_payload.get("exposure", {}).get("status") == "proven"
        and tuple(terminal_payload.get("exposure", {}).get("evidence_refs") or ())
        == tuple(outcome_payload.get("raw_evidence", {}).get("refs") or ())
        and terminal_payload.get("attribution", {}).get("method")
        == outcome_payload.get("attribution", {}).get("method")
        and not terminal_payload.get("attribution", {}).get(
            "competing_causes"
        )
        and str(sealed.get("created_at") or "")
        == temporal.get("prediction_sealed_at")
        and terminal_payload.get("terminal", {}).get("evaluated_at")
        == temporal.get("prediction_terminal_at")
        and outcome_payload.get("observation_window", {}).get("ends_at")
        == temporal.get("outcome_observed_at")
        and outcome_payload.get("maturity", {}).get("matured_at")
        == temporal.get("outcome_matured_at")
        and _revision_descends_from(terminal, sealed, revisions)
        and ref.get("terminal_state") == "measured"
        and ref.get("outcome_revision_id") == outcome.get("revision_id")
        and ref.get("outcome_payload_hash") == outcome.get("payload_hash")
    )


def _revision_descends_from(
    terminal: Mapping[str, Any],
    sealed: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
) -> bool:
    cursor = terminal
    seen: set[str] = set()
    sealed_revision_id = str(sealed.get("revision_id") or "")
    while str(cursor.get("revision_id") or "") != sealed_revision_id:
        cursor_revision_id = str(cursor.get("revision_id") or "")
        parent_revision_id = str(cursor.get("supersedes_revision_id") or "")
        if cursor_revision_id in seen or not parent_revision_id:
            return False
        seen.add(cursor_revision_id)
        parent = revisions.get(parent_revision_id)
        if (
            parent is None
            or parent.get("object_type") != "prediction_record"
            or parent.get("object_id") != sealed.get("object_id")
        ):
            return False
        cursor = parent
    return True


def _terminal_projection_receipt_matches(
    state_store: CognitiveStateStore,
    terminal: Any,
) -> bool:
    commands = tuple(
        command
        for command in state_store.commands_for_revision(terminal.revision_id)
        if command["consumer_id"] == PREDICTION_TERMINAL_CONSUMER
        and command["command_type"] == PREDICTION_TERMINAL_COMMAND
    )
    if len(commands) != 1:
        return False
    command = commands[0]
    payload = command["payload"]
    recomputed = LocalConsumerCommand.create(
        revision_id=str(command["revision_id"]),
        consumer_id=str(command["consumer_id"]),
        command_type=str(command["command_type"]),
        payload=payload,
        created_at=str(command["created_at"]),
    )
    expected_payload = {
        "schema_version": "mnemos.prediction_terminal_projection.v1",
        "prediction_id": terminal.object_id,
        "terminal_revision_id": terminal.revision_id,
        "terminal_revision_hash": terminal.payload_hash,
        "terminal_state": terminal.payload["terminal"]["state"],
        "projection_effect_id": payload.get("projection_effect_id"),
    }
    before_hash = sha256_json(
        {"prediction_id": terminal.object_id, "state": "unprojected"}
    )
    after_hash = sha256_json(
        {
            "terminal_revision_id": terminal.revision_id,
            "terminal_revision_hash": terminal.payload_hash,
            "terminal_state": terminal.payload["terminal"]["state"],
        }
    )
    receipt = state_store.effect_receipt(str(command["command_id"]))
    return bool(
        recomputed.command_id == command["command_id"]
        and recomputed.payload_hash == command["payload_hash"]
        and payload == expected_payload
        and receipt is not None
        and receipt["status"] == "committed"
        and receipt["target_effect_id"] == payload["projection_effect_id"]
        and receipt["before_hash"] == before_hash
        and receipt["after_hash"] == after_hash
        and tuple(receipt["evidence_refs"])
        == (
            f"prediction-terminal-command:{command['command_id']}",
            f"prediction-revision:{terminal.revision_id}",
            f"prediction-terminal-projection:{after_hash}",
        )
        and receipt["consumption_outcome"]
        == "deterministic prediction terminal read model available"
        and not receipt["reason_code"]
    )


def _revision_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT revision_id, object_type, object_id, payload_json,
               payload_hash, source_revision_id, source_content_hash,
               supersedes_revision_id,
               correction_of_revision_id, created_at
        FROM cognitive_state_revisions
        """
    ).fetchall()
    return {
        str(row["revision_id"]): {
            "revision_id": str(row["revision_id"]),
            "object_type": str(row["object_type"]),
            "object_id": str(row["object_id"]),
            "payload": json.loads(str(row["payload_json"])),
            "payload_hash": str(row["payload_hash"]),
            "source_revision_id": str(row["source_revision_id"] or ""),
            "source_content_hash": str(row["source_content_hash"] or ""),
            "supersedes_revision_id": str(row["supersedes_revision_id"] or ""),
            "correction_of_revision_id": str(row["correction_of_revision_id"] or ""),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    }


def _command_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT command_id, revision_id, event_id, consumer_id, command_type,
               payload_json, payload_hash, created_at
        FROM cognitive_state_outbox
        WHERE consumer_id='governed_training_projection'
        ORDER BY command_id
        """
    ).fetchall()
    return {
        str(row["command_id"]): {
            "command_id": str(row["command_id"]),
            "revision_id": str(row["revision_id"]),
            "event_id": str(row["event_id"]),
            "consumer_id": str(row["consumer_id"]),
            "command_type": str(row["command_type"]),
            "payload": json.loads(str(row["payload_json"])),
            "payload_hash": str(row["payload_hash"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    }


def _state_effect_receipt_index(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
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
        WHERE receipt.consumer_id='governed_training_projection'
        ORDER BY receipt.command_id
        """
    ).fetchall()
    return {
        str(row["command_id"]): {
            "receipt_id": str(row["receipt_id"]),
            "command_id": str(row["command_id"]),
            "revision_id": str(row["revision_id"]),
            "event_id": str(row["event_id"]),
            "consumer_id": str(row["consumer_id"]),
            "consumption_id": str(row["consumption_id"]),
            "status": str(row["status"]),
            "target_effect_id": str(row["target_effect_id"]),
            "before_hash": str(row["before_hash"]),
            "after_hash": str(row["after_hash"]),
            "evidence_refs": tuple(json.loads(str(row["evidence_refs"]))),
            "created_at": str(row["created_at"]),
            "consumption_event_id": str(row["consumption_event_id"]),
            "consumption_consumer_id": str(row["consumption_consumer_id"]),
            "consumption_outcome": str(row["consumption_outcome"]),
            "consumption_status": str(row["consumption_status"]),
            "consumption_target_effect_id": str(row["consumption_target_effect_id"]),
            "consumption_before_hash": str(row["consumption_before_hash"]),
            "consumption_after_hash": str(row["consumption_after_hash"]),
            "consumption_evidence_refs": tuple(json.loads(str(row["consumption_evidence_refs"]))),
            "consumption_action_changed": int(row["consumption_action_changed"]),
            "consumption_metadata": json.loads(str(row["consumption_metadata"])),
            "consumption_idempotency_key": str(row["consumption_idempotency_key"]),
            "consumption_supersedes_id": str(row["consumption_supersedes_id"]),
            "consumption_correction_id": str(row["consumption_correction_id"]),
            "consumption_receipt_state": str(row["consumption_receipt_state"]),
            "consumption_created_at": str(row["consumption_created_at"]),
            "head_consumption_id": str(row["head_consumption_id"] or ""),
        }
        for row in rows
    }


def _current_revisions(
    conn: sqlite3.Connection,
    *,
    object_type: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT revision.revision_id, revision.object_type, revision.object_id,
               revision.payload_json, revision.payload_hash,
               revision.source_revision_id, revision.source_content_hash,
               revision.supersedes_revision_id,
               revision.correction_of_revision_id, revision.created_at
        FROM cognitive_state_heads AS head
        JOIN cognitive_state_revisions AS revision
          ON revision.revision_id=head.revision_id
        WHERE head.object_type=?
        ORDER BY revision.object_id
        """,
        (object_type,),
    ).fetchall()
    return [
        {
            "revision_id": str(row["revision_id"]),
            "object_type": str(row["object_type"]),
            "object_id": str(row["object_id"]),
            "payload": json.loads(str(row["payload_json"])),
            "payload_hash": str(row["payload_hash"]),
            "source_revision_id": str(row["source_revision_id"] or ""),
            "source_content_hash": str(row["source_content_hash"] or ""),
            "supersedes_revision_id": str(row["supersedes_revision_id"] or ""),
            "correction_of_revision_id": str(row["correction_of_revision_id"] or ""),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _current_revision_for_object(
    revisions: Mapping[str, Mapping[str, Any]],
    *,
    object_type: str,
    object_id: str,
) -> Mapping[str, Any] | None:
    candidates = [
        item
        for item in revisions.values()
        if item["object_type"] == object_type and item["object_id"] == object_id
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (item["created_at"], item["revision_id"]),
    )[-1]


def _historical_promotion_count(
    admissions: Sequence[Mapping[str, Any]],
    objects: Sequence[Any],
) -> int:
    forbidden = {value for item in objects for value in (item.source_key, item.row_hash)}
    count = 0
    for admission in admissions:
        serialized = json.dumps(
            admission,
            ensure_ascii=False,
            sort_keys=True,
        )
        if any(value in serialized for value in forbidden):
            count += 1
    return count


def _barrier_guard_gap(repo_root: Path) -> int:
    root = Path(repo_root) / "core" / "cognitive"
    required_by_file = {
        "training_governance.py": {
            "process_admission_intake",
            "reconcile_admission_intakes",
            "admit_training_evidence",
            "reconcile_pending",
            "apply_tombstone_command",
        },
        "training_governance_model_impl.py": {
            "load_applied_model",
            "build_ready_run",
            "rebuild_stale_dimension",
            "apply_run",
        },
    }
    required = set().union(*required_by_file.values())
    guarded: set[str] = set()
    trees: dict[str, ast.Module] = {}
    for file_name, file_required in required_by_file.items():
        path = root / file_name
        try:
            tree = ast.parse(read_text_value(path))
        except (OSError, SyntaxError, UnicodeError):
            continue
        trees[file_name] = tree
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in file_required:
                continue
            if any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_assert_migration_clear"
                for call in ast.walk(node)
            ):
                guarded.add(node.name)
    owner_tree = trees.get("training_governance.py")
    model_mixin_bound = bool(
        owner_tree
        and any(
            isinstance(node, ast.ClassDef)
            and node.name == "TrainingGovernanceStore"
            and any(
                isinstance(base, ast.Name)
                and base.id == "_TrainingGovernanceModelImplementation"
                for base in node.bases
            )
            for node in owner_tree.body
        )
    )
    if not model_mixin_bound:
        guarded.difference_update(
            required_by_file["training_governance_model_impl.py"]
        )
    return len(required - guarded)


def _caller_selectable_correction_count(repo_root: Path) -> int:
    path = Path(repo_root) / "core" / "cognitive" / "training_governance.py"
    try:
        tree = ast.parse(read_text_value(path))
    except (OSError, SyntaxError, UnicodeError):
        return 1
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "correct_admission"
    )


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(path).resolve(strict=True)}?mode=ro",
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    return conn
