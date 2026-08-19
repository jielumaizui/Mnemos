"""Close canonical cognitive tombstones for receipt-only projection owners."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Sequence

from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.prediction_ledger import (
    PREDICTION_OUTCOME_COMMAND,
    PREDICTION_OUTCOME_CONSUMER,
    PREDICTION_TERMINAL_COMMAND,
    PREDICTION_TERMINAL_CONSUMER,
)
from core.cognitive.state_contract import LocalConsumerCommand, sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.state_types import (
    COGNITIVE_TOMBSTONE_COMMAND_TYPE,
    COGNITIVE_TOMBSTONE_SCHEMA_VERSION,
)
from core.cognitive.training_contract import (
    TRAINING_ADMISSION_COMMAND,
    TRAINING_ADMISSION_CONSUMER,
)
from core.cognitive.training_governance_types import (
    TRAINING_PROJECTION_CONSUMER,
)


_RECEIPT_ONLY_CONSUMERS = frozenset(
    {
        *FEEDBACK_TARGETS,
        TRAINING_ADMISSION_CONSUMER,
        PREDICTION_OUTCOME_CONSUMER,
        PREDICTION_TERMINAL_CONSUMER,
    }
)
_TERMINAL_RECEIPT_STATUSES = frozenset(
    {
        "committed",
        "failed_terminal",
        "intentional_skip",
        "rejected",
        "revoked",
        "dead_letter",
    }
)


def apply_receipt_only_cognitive_tombstones(
    state: CognitiveStateStore,
    *,
    request_id: str,
) -> dict[str, Any]:
    """Retire receipt-only projections after independently checking their sources.

    Governed training samples have an external scoring projection and remain
    owned by ``TrainingGovernanceStore.apply_tombstone_command``.  This owner
    closes only consumers whose durable effect is already represented by an
    immutable state/domain receipt and whose canonical source head has been
    excluded by the tombstone plan.
    """

    normalized_request = str(request_id or "").strip()
    if not normalized_request:
        raise ValueError("cognitive tombstone request_id is required")
    pending_commands = tuple(
        command
        for command in state.pending_commands()
        if command["command_type"] == COGNITIVE_TOMBSTONE_COMMAND_TYPE
        and command["payload"].get("request_id") == normalized_request
        and command["consumer_id"] != TRAINING_PROJECTION_CONSUMER
    )
    unsupported = sorted(
        {
            str(command["consumer_id"])
            for command in pending_commands
            if command["consumer_id"] not in _RECEIPT_ONLY_CONSUMERS
        }
    )
    commands = tuple(
        command
        for command in pending_commands
        if command["consumer_id"] in _RECEIPT_ONLY_CONSUMERS
    )

    receipt_ids: list[str] = []
    proof_refs: list[str] = []
    for command in commands:
        proof_ref = _consumer_retirement_proof(state, command)
        payload = command["payload"]
        receipt = state.record_effect_receipt(
            str(command["command_id"]),
            status="committed",
            target_effect_id=(
                f"tombstone:{command['consumer_id']}:{normalized_request}"
            ),
            before_hash=str(payload["before_hash"]),
            after_hash=str(payload["tombstone_hash"]),
            evidence_refs=(
                f"tombstone-command:{command['command_id']}",
                proof_ref,
            ),
            outcome="canonical cognitive projection retired from current reads",
            created_at=str(command["created_at"]),
        )
        receipt_ids.append(receipt.receipt_id)
        proof_refs.append(proof_ref)
    status = state.tombstone_status(normalized_request)
    return {
        "status": (
            "unsupported_consumers"
            if unsupported
            else "applied" if commands else "existing"
        ),
        "request_id": normalized_request,
        "consumer_count": len(commands),
        "receipt_ids": tuple(receipt_ids),
        "proof_refs": tuple(proof_refs),
        "unsupported_consumers": tuple(unsupported),
        "verified": bool(status["verified"]),
        "terminal_count": int(status["terminal_count"]),
        "required_count": int(status["required_count"]),
    }


def _consumer_retirement_proof(
    state: CognitiveStateStore,
    tombstone_command: Mapping[str, Any],
) -> str:
    payload = tombstone_command["payload"]
    if (
        payload.get("schema_version") != COGNITIVE_TOMBSTONE_SCHEMA_VERSION
        or tombstone_command["command_type"]
        != COGNITIVE_TOMBSTONE_COMMAND_TYPE
        or tombstone_command["consumer_id"]
        not in payload.get("required_consumers", ())
    ):
        raise ValueError("cognitive tombstone command contract mismatch")
    recomputed = LocalConsumerCommand.create(
        revision_id=str(tombstone_command["revision_id"]),
        consumer_id=str(tombstone_command["consumer_id"]),
        command_type=str(tombstone_command["command_type"]),
        payload=payload,
        created_at=str(tombstone_command["created_at"]),
    )
    if (
        recomputed.command_id != tombstone_command["command_id"]
        or recomputed.payload_hash != tombstone_command["payload_hash"]
    ):
        raise ValueError("cognitive tombstone command identity mismatch")

    target_ids = tuple(sorted(str(value) for value in payload["target_revision_ids"]))
    if not target_ids:
        raise ValueError("cognitive tombstone target set is empty")
    source_proofs: list[dict[str, str]] = []
    retired_revisions: list[str] = []
    for revision_id in target_ids:
        revision_identity = _revision_identity_including_tombstoned(
            state,
            revision_id,
        )
        if revision_identity is None:
            raise ValueError("cognitive tombstone target revision is unavailable")
        current = state.current_revision(
            revision_identity["object_type"],
            revision_identity["object_id"],
        )
        if current is not None and current.revision_id == revision_id:
            raise RuntimeError("cognitive tombstone target remains current")
        retired_revisions.append(revision_id)
        for source_command in state.commands_for_revision(revision_id):
            if source_command["consumer_id"] != tombstone_command["consumer_id"]:
                continue
            source_proofs.append(
                _source_projection_proof(
                    state,
                    source_command,
                    consumer_id=str(tombstone_command["consumer_id"]),
                )
            )
    if not source_proofs:
        raise RuntimeError("cognitive tombstone consumer has no source projection")
    proof_hash = sha256_json(
        {
            "schema_version": "mnemos.cognitive_tombstone_consumer_proof.v1",
            "command_id": tombstone_command["command_id"],
            "consumer_id": tombstone_command["consumer_id"],
            "retired_revision_ids": retired_revisions,
            "source_projection_proofs": sorted(
                source_proofs,
                key=lambda item: item["command_id"],
            ),
        }
    )
    return (
        "tombstone-oracle:receipt-only-projection-retired:"
        + str(proof_hash)
    )


def _source_projection_proof(
    state: CognitiveStateStore,
    command: Mapping[str, Any],
    *,
    consumer_id: str,
) -> dict[str, str]:
    recomputed = LocalConsumerCommand.create(
        revision_id=str(command["revision_id"]),
        consumer_id=str(command["consumer_id"]),
        command_type=str(command["command_type"]),
        payload=command["payload"],
        created_at=str(command["created_at"]),
    )
    receipt = state.effect_receipt(str(command["command_id"]))
    if (
        recomputed.command_id != command["command_id"]
        or recomputed.payload_hash != command["payload_hash"]
        or receipt is None
        or receipt["status"] not in _TERMINAL_RECEIPT_STATUSES
        or receipt["revision_id"] != command["revision_id"]
        or receipt["consumer_id"] != consumer_id
    ):
        raise RuntimeError("cognitive source projection receipt mismatch")
    if consumer_id in FEEDBACK_TARGETS:
        if receipt["status"] in {"committed", "revoked"}:
            state.validate_feedback_effect_receipt(
                str(command["command_id"])
            )
        elif receipt["status"] == "intentional_skip":
            _validate_feedback_skip_receipt(command, receipt)
    elif consumer_id == TRAINING_ADMISSION_CONSUMER:
        if command["command_type"] != TRAINING_ADMISSION_COMMAND:
            raise ValueError("training admission tombstone source mismatch")
        if receipt["status"] not in {"committed", "rejected"}:
            raise ValueError("training admission tombstone receipt is not terminal")
    elif consumer_id == PREDICTION_OUTCOME_CONSUMER:
        if command["command_type"] != PREDICTION_OUTCOME_COMMAND:
            raise ValueError("prediction outcome tombstone source mismatch")
    elif consumer_id == PREDICTION_TERMINAL_CONSUMER:
        if command["command_type"] != PREDICTION_TERMINAL_COMMAND:
            raise ValueError("prediction terminal tombstone source mismatch")
    else:
        raise RuntimeError("unsupported cognitive tombstone source consumer")
    return {
        "command_id": str(command["command_id"]),
        "command_payload_hash": str(command["payload_hash"]),
        "receipt_id": str(receipt["receipt_id"]),
        "receipt_status": str(receipt["status"]),
        "receipt_after_hash": str(receipt["after_hash"]),
    }


def _revision_identity_including_tombstoned(
    state: CognitiveStateStore,
    revision_id: str,
) -> dict[str, str] | None:
    with sqlite3.connect(
        f"file:{state.db_path.resolve(strict=True)}?mode=ro",
        uri=True,
    ) as conn:
        row = conn.execute(
            "SELECT object_type, object_id FROM cognitive_state_revisions "
            "WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
    if row is None:
        return None
    return {"object_type": str(row[0]), "object_id": str(row[1])}


def _validate_feedback_skip_receipt(
    command: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    payload = command["payload"]
    unchanged_hash = sha256_json(
        {
            "attribution_revision_id": command["revision_id"],
            "target_id": command["consumer_id"],
            "state": "unchanged",
        }
    )
    expected_refs = (
        f"feedback-command:{command['command_id']}",
        f"feedback-attribution:{command['revision_id']}",
    )
    evidence_refs = tuple(receipt["evidence_refs"])
    if (
        command["command_type"] != "evaluate_feedback_target"
        or payload.get("eligible") is not False
        or payload.get("target_id") != command["consumer_id"]
        or receipt["target_effect_id"]
        != (
            f"feedback-skip:{command['consumer_id']}:"
            + str(command["revision_id"]).removeprefix("cogrev-")
        )
        or receipt["before_hash"] != unchanged_hash
        or receipt["after_hash"] != unchanged_hash
        or receipt["consumption_outcome"] != payload.get("exclusion_reason")
        or receipt["reason_code"] != "feedback_target_ineligible"
        or evidence_refs[:2] != expected_refs
        or len(evidence_refs) != 3
        or not evidence_refs[2].startswith("feedback-target-registry:sha256:")
    ):
        raise ValueError("feedback tombstone skip proof mismatch")


def receipt_only_tombstone_consumers() -> Sequence[str]:
    """Expose the exact owned consumer registry for audits and tests."""

    return tuple(sorted(_RECEIPT_ONLY_CONSUMERS))
