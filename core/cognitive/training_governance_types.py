"""Private contracts shared inside the COG-048 deep module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.cognitive.state_contract import CognitiveStateRevision


TRAINING_PROJECTION_CONSUMER = "governed_training_projection"
TRAINING_PROJECTION_COMMAND = "project_governed_training_sample"
TRAINING_PROJECTION_SCHEMA = "mnemos.governed_training_sample_projection.v1"


class TrainingEvidenceNotReady(RuntimeError):
    """Retryable absence of a mature, current terminal Prediction proof."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code or "training_evidence_not_ready")
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class TrainingAdmissionReceipt:
    """Public immutable proof that one objective example was admitted."""

    status: str
    admission_id: str
    admission_revision_id: str
    sample_id: str
    dataset_split: str
    projection_command_id: str
    projection_receipt_id: str


@dataclass(frozen=True)
class TrainingAdmissionIntakeReceipt:
    """Terminal or deferred state of one durable admission obligation."""

    status: str
    command_id: str
    admission_id: str = ""
    admission_revision_id: str = ""
    sample_id: str = ""
    dataset_split: str = ""
    projection_command_id: str = ""
    projection_receipt_id: str = ""
    intake_effect_receipt_id: str = ""


@dataclass(frozen=True)
class TrainingAdmissionReconciliationReport:
    """Bounded restart-safe reconciliation summary for admission intakes."""

    scanned: int
    committed: int
    superseded: int
    deferred: int
    failed: int
    remaining: int
    committed_command_ids: tuple[str, ...]
    superseded_command_ids: tuple[str, ...]
    deferred_command_ids: tuple[str, ...]
    failed_command_ids: tuple[str, ...]


@dataclass(frozen=True)
class TrainingRunReceipt:
    """Public immutable proof for one governed run projection."""

    status: str
    run_id: str
    run_revision_id: str
    model_id: str
    projection_command_id: str
    projection_receipt_id: str


@dataclass(frozen=True)
class TrainingReconciliationReport:
    """Summary of deterministic pending-command reconciliation."""

    scanned: int
    projected: int
    failed: int
    remaining: int
    projected_command_ids: tuple[str, ...]
    failed_command_ids: tuple[str, ...]


@dataclass(frozen=True)
class GovernedModelSnapshot:
    """Verified active model material safe for runtime activation."""

    dimension: str
    model_id: str
    run_revision_id: str
    model_type: str
    model_blob: Mapping[str, Any]
    model_blob_hash: str
    bayesian_prior: Mapping[str, Any]
    bayesian_prior_hash: str
    rule_optimizer: Mapping[str, Any]
    rule_optimizer_hash: str


@dataclass(frozen=True)
class FeedbackEvidence:
    """Internal exact evidence bundle resolved from a COG-038 command."""

    command: Mapping[str, Any]
    attribution: CognitiveStateRevision
    outcome: CognitiveStateRevision
    prediction: CognitiveStateRevision
    prediction_terminal: CognitiveStateRevision
    decision: CognitiveStateRevision
    proposal_id: str
    proposal_hash: str
    domain_effect: Any
    domain_receipt_id: str
    domain_receipt_hash: str
    prediction_effect_receipt: Mapping[str, Any]
    oracle_issuance_hash: str
