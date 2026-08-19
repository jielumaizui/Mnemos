"""Private projection identities for Bayesian and rule-optimizer effects."""

from __future__ import annotations

from typing import Any

from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    canonical_json,
    sha256_json,
)


class _TrainingGovernanceAuxImplementation:
    """Build immutable train-only auxiliary effect and receipt rows."""

    @staticmethod
    def _aux_projection_rows(
        revision: CognitiveStateRevision,
        command: LocalConsumerCommand,
        *,
        run_before_hash: str,
    ) -> tuple[
        tuple[tuple[Any, ...], ...],
        tuple[tuple[Any, ...], ...],
        tuple[str, ...],
    ]:
        run = revision.payload
        state = str(run["state"])
        status = "committed" if state == "applied" else state
        effect_rows: list[tuple[Any, ...]] = []
        receipt_rows: list[tuple[Any, ...]] = []
        reciprocal_refs: list[str] = []
        for effect_kind, artifact_key in (
            ("bayesian_prior", "bayesian_prior_artifact"),
            ("rule_optimizer", "rule_optimizer_artifact"),
        ):
            artifact = dict(run[artifact_key])
            effect_id = str(artifact.get("effect_id") or "")
            artifact_hash = str(artifact.get("artifact_hash") or "")
            input_hash = str(artifact.get("input_hash") or "")
            if state not in {"insufficient_sample", "stale"} and not effect_id:
                raise ValueError("governed training auxiliary artifact is missing")
            persisted_effect_id = effect_id if state == "applied" else None
            if state == "applied":
                effect_rows.append(
                    (
                        effect_id,
                        effect_kind,
                        revision.revision_id,
                        revision.payload_hash,
                        canonical_json(artifact["admission_revision_ids"]),
                        str(run["dimension"]),
                        input_hash,
                        canonical_json(artifact),
                        artifact_hash,
                        command.created_at,
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
                        "command_id": command.command_id,
                        "effect_kind": effect_kind,
                        "status": status,
                    }
                ).split(":", 1)[1][:32]
            )
            before_hash = sha256_json(
                {
                    "effect_kind": effect_kind,
                    "run_before_hash": run_before_hash,
                }
            )
            after_hash = sha256_json(
                {
                    "effect_kind": effect_kind,
                    "run_revision_id": revision.revision_id,
                    "run_payload_hash": revision.payload_hash,
                    "effect_id": effect_id,
                    "artifact_hash": artifact_hash,
                    "status": status,
                }
            )
            evidence_refs = [
                f"training-run:{revision.revision_id}",
                f"training-aux-effect:{effect_kind}:{input_hash or 'insufficient'}",
            ]
            if persisted_effect_id:
                evidence_refs.append(f"governed-training-aux-effect:{persisted_effect_id}")
            identity = {
                "receipt_id": receipt_id,
                "command_id": command.command_id,
                "run_revision_id": revision.revision_id,
                "effect_kind": effect_kind,
                "effect_id": persisted_effect_id,
                "status": status,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "evidence_refs": evidence_refs,
            }
            receipt_hash = sha256_json(identity)
            receipt_rows.append(
                (
                    receipt_id,
                    command.command_id,
                    revision.revision_id,
                    effect_kind,
                    persisted_effect_id,
                    status,
                    before_hash,
                    after_hash,
                    canonical_json(evidence_refs),
                    receipt_hash,
                    command.created_at,
                )
            )
            reciprocal_refs.append(f"governed-training-aux-receipt:{receipt_id}:{receipt_hash}")
        return tuple(effect_rows), tuple(receipt_rows), tuple(reciprocal_refs)
