"""Deterministic derivation for durable objective-training intake commands."""

from __future__ import annotations

from core.cognitive.access_control import (
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
)
from core.cognitive.training_contract import (
    TRAINING_ADMISSION_COMMAND,
    TRAINING_ADMISSION_CONSUMER,
    TRAINING_ADMISSION_INTAKE_CONTRACT_HASH,
    TRAINING_ADMISSION_INTAKE_SCHEMA_VERSION,
    training_admission_intake_command_key,
    validate_training_admission_intake_payload,
)


def derive_training_admission_intake_command(
    attribution: CognitiveStateRevision,
    *,
    outcome_revision: CognitiveStateRevision,
    target_commands: tuple[LocalConsumerCommand, ...],
    recorded_at: str,
) -> LocalConsumerCommand:
    """Bind one immutable admission obligation to exact committed source refs."""

    if (
        attribution.object_type != "feedback_attribution_record"
        or attribution.payload.get("evidence_class") != "objective_outcome"
        or attribution.payload.get("disposition") != "objective_only"
        or outcome_revision.object_type != "outcome_measurement"
    ):
        raise ValueError("training admission intake requires objective attribution")
    access = validate_cognitive_access_envelope(
        attribution.payload["access_control"],
        expected_scope_type=attribution.scope_type,
        expected_scope_id=attribution.scope_id,
    )
    training_targets = tuple(
        command
        for command in target_commands
        if command.consumer_id == "training_evidence"
    )
    if len(training_targets) != 1:
        raise ValueError("objective attribution requires one training target command")
    training_target = training_targets[0]
    authority = outcome_revision.payload["source_authority"]
    source_authority_refs = sorted(
        {
            str(authority["source_authority_id"]),
            str(authority["source_id"]),
            str(authority["source_revision_id"]),
            str(authority["source_authority_catalog_hash"]),
        }
    )
    payload = {
        "schema_version": TRAINING_ADMISSION_INTAKE_SCHEMA_VERSION,
        "contract_hash": TRAINING_ADMISSION_INTAKE_CONTRACT_HASH,
        "command_key": "",
        "attribution_ref": {
            "object_id": attribution.object_id,
            "revision_id": attribution.revision_id,
            "payload_hash": attribution.payload_hash,
        },
        "outcome_ref": {
            "object_id": outcome_revision.object_id,
            "revision_id": outcome_revision.revision_id,
            "payload_hash": outcome_revision.payload_hash,
        },
        "training_target_ref": {
            "command_id": training_target.command_id,
            "payload_hash": training_target.payload_hash,
        },
        "required_feedback_commands": [
            {
                "command_id": command.command_id,
                "consumer_id": command.consumer_id,
                "command_type": command.command_type,
                "payload_hash": command.payload_hash,
            }
            for command in sorted(
                target_commands,
                key=lambda item: (item.consumer_id, item.command_id),
            )
        ],
        "source_identity": {
            "principal_id": str(access["owner"]["principal_id"]),
            "agent": str(access["owner"]["agent"]),
        },
        "source_access": {
            "access_control_hash": cognitive_access_hash(access),
            "scope_type": attribution.scope_type,
            "scope_id": attribution.scope_id,
            "project": str(access["scope"]["project"]),
            "session_id": str(access["scope"]["session_id"]),
            "visibility": str(access["visibility"]),
            "consent_status": str(access["consent"]["status"]),
            "sensitivity": str(access["sensitivity"]),
            "retention_policy": str(access["retention_policy"]),
        },
        "source_authority_refs": source_authority_refs,
        "correction_lineage": {
            "supersedes_revision_id": attribution.supersedes_revision_id,
            "correction_of_revision_id": attribution.correction_of_revision_id,
        },
    }
    payload["command_key"] = training_admission_intake_command_key(payload)
    validate_training_admission_intake_payload(payload)
    return LocalConsumerCommand.create(
        revision_id=attribution.revision_id,
        consumer_id=TRAINING_ADMISSION_CONSUMER,
        command_type=TRAINING_ADMISSION_COMMAND,
        payload=payload,
        created_at=recorded_at,
    )


__all__ = ["derive_training_admission_intake_command"]
