"""Canonical persistence and replay for Observation CalibrationRecord values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Protocol, Sequence

from core.cognitive.auto_calibration import CalibrationReport, ValidationResult
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.models import Observation
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    now_utc,
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_store import CognitiveStateStore
from core.ops.cognitive_data_contract import CognitiveDataEvent


CALIBRATION_CONSUMERS = ("observation_index", "wiki_projection")


class CalibrationObservationStore(Protocol):
    """Projection seam required after a calibration revision commits."""

    def _apply_committed_calibration(
        self,
        observation_id: str,
        *,
        prior: float,
        posterior: float,
        revision_id: str,
        input_hash: str,
        spec_hash: str,
        record_hash: str,
    ) -> Dict[str, Any]: ...


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + "-" + sha256_json(value).split(":", 1)[1][:32]


def _evidence_refs(report: CalibrationReport) -> tuple[str, ...]:
    refs = [f"observation:{report.observation_id}"]
    refs.extend(report.source_span_ids)
    refs.extend(f"lineage:{value}" for value in report.supporting_evidence)
    refs.extend(f"counter-lineage:{value}" for value in report.counter_evidence)
    return tuple(dict.fromkeys(str(value) for value in refs if value))


def calibration_record_payload(
    observation: Observation,
    report: CalibrationReport,
) -> Dict[str, Any]:
    """Build the exact durable payload used by a calibration commit."""

    evidence_refs = _evidence_refs(report)
    payload = report.canonical_record_payload()
    # Observation sources do not yet carry a complete agent/project/session
    # ACL.  Preserve the calibration record for internal reconciliation but
    # make its object ACL explicitly restricted rather than guessing a public
    # or user-owned scope.
    payload["access_control"] = make_cognitive_access_envelope(
        owner_principal_id=f"system:observation:{observation.id}",
        owner_agent="system",
        scope_type="observation",
        scope_id=observation.id,
        purposes=("calibration_internal",),
        consent_provenance_refs=(),
        sensitivity="restricted",
        retention_policy="cognitive_state",
        source_acl_lineage=(sha256_json(list(evidence_refs)),),
        visibility="restricted",
        scope_resolution="restricted_unknown",
        consent_status="restricted_unknown",
    )
    return payload


def _same_revision(
    current: CognitiveStateRevision | None,
    probe: CognitiveStateRevision,
) -> bool:
    return bool(
        current is not None
        and current.payload_hash == probe.payload_hash
        and current.evidence_hash == probe.evidence_hash
        and current.source_event_id == probe.source_event_id
        and current.source_revision_id == probe.source_revision_id
        and current.source_content_hash == probe.source_content_hash
        and current.scope_type == probe.scope_type
        and current.scope_id == probe.scope_id
    )


@dataclass(frozen=True)
class CalibrationCommitReceipt:
    status: str
    event_id: str
    revision_id: str
    observation_id: str
    outbox_ids: tuple[str, ...]
    command_ids: Mapping[str, str]
    transaction_hash: str
    payload_hash: str


class CalibrationRecordStore:
    """Deep module over CognitiveStateStore's ``calibration_record`` type."""

    def __init__(self, state_store: CognitiveStateStore):
        self.state_store = state_store

    def commit(
        self,
        observation: Observation,
        report: CalibrationReport,
    ) -> tuple[CalibrationCommitReceipt, CalibrationReport]:
        if report.observation_id != observation.id:
            raise ValueError("calibration report observation identity mismatch")
        if not report.calculation_input_hash or not report.validator_spec_hash:
            raise ValueError("calibration report is missing immutable input/spec identity")
        if report.derived_source_double_count != 0:
            raise ValueError("derived source evidence was counted more than once")
        observation_snapshot = report.input_snapshot.get("observation")
        expected_measurement_hash = (
            observation.calibration_measurement_hash
            or sha256_json(observation.calibration_measurement_payload())
        )
        if (
            not isinstance(observation_snapshot, Mapping)
            or observation_snapshot.get("observation_id") != observation.id
            or float(observation_snapshot.get("base_confidence", -1.0))
            != observation.base_confidence_value()
            or observation_snapshot.get("base_measurement_status")
            != observation.base_measurement_status
            or observation_snapshot.get("measurement_hash")
            != expected_measurement_hash
        ):
            raise ValueError("calibration report input does not match the Observation")
        report.finalize_hash()
        evidence_refs = _evidence_refs(report)
        payload = calibration_record_payload(observation, report)
        object_id = observation.id
        event_id = _stable_id(
            "calibration-event",
            {
                "observation_id": observation.id,
                "calculation_input_hash": report.calculation_input_hash,
                "validator_spec_hash": report.validator_spec_hash,
            },
        )
        # Version counters and timestamps are not calibration input.  Exact
        # input/spec replay must therefore retain the same immutable revision.
        source_revision_id = (
            f"calibration-input:{report.calculation_input_hash}:"
            f"spec:{report.validator_spec_hash}"
        )
        current = self.state_store.current_revision("calibration_record", object_id)
        probe = CognitiveStateRevision.create(
            object_type="calibration_record",
            object_id=object_id,
            source_event_id=event_id,
            source_revision_id=source_revision_id,
            source_content_hash=report.calculation_input_hash,
            scope_type="observation",
            scope_id=observation.id,
            evidence_refs=evidence_refs,
            payload=payload,
            created_at=now_utc(),
        )
        if _same_revision(current, probe):
            assert current is not None
            revision = current
        else:
            revision = CognitiveStateRevision.create(
                object_type="calibration_record",
                object_id=object_id,
                source_event_id=event_id,
                source_revision_id=source_revision_id,
                source_content_hash=report.calculation_input_hash,
                scope_type="observation",
                scope_id=observation.id,
                evidence_refs=evidence_refs,
                payload=payload,
                supersedes_revision_id=current.revision_id if current is not None else "",
                created_at=now_utc(),
            )

        event = CognitiveDataEvent(
            event_id=event_id,
            source_id=observation.id,
            asset_id=report.calculation_input_hash,
            source_kind="observation_calibration",
            source_uri=f"mnemos://observation/{observation.id}/calibration",
            content_hash=report.calculation_input_hash,
            canonical_subject=f"calibration_record:{observation.id}",
            data_type="calibration_record",
            producer="observation_calibrator",
            intended_consumers=CALIBRATION_CONSUMERS,
            privacy_level="private",
            confidence=report.calibrated_confidence,
            evidence_refs=evidence_refs,
            dedupe_key=f"cognitive-state:{event_id}",
            created_at=revision.created_at,
            retention_policy="cognitive_state",
            metadata={
                "revision_ids": [revision.revision_id],
                "contract_version": "mnemos.calibration_record.v1",
            },
        )
        commands = tuple(
            LocalConsumerCommand.create(
                revision_id=revision.revision_id,
                consumer_id=consumer_id,
                command_type="project_calibration_record",
                payload={
                    "calibration_revision_id": revision.revision_id,
                    "observation_id": observation.id,
                    "calibration_record_hash": revision.payload_hash,
                },
                created_at=revision.created_at,
            )
            for consumer_id in CALIBRATION_CONSUMERS
        )
        committed = self.state_store.unit_of_work().commit(
            revisions=(revision,),
            event=event,
            commands=commands,
        )
        persisted_report = report_from_revision(revision)
        command_ids = {
            command.consumer_id: command.command_id for command in commands
        }
        receipt = CalibrationCommitReceipt(
            status=committed.status,
            event_id=committed.event_id,
            revision_id=revision.revision_id,
            observation_id=observation.id,
            outbox_ids=committed.outbox_ids,
            command_ids=command_ids,
            transaction_hash=committed.transaction_hash,
            payload_hash=revision.payload_hash,
        )
        return receipt, persisted_report

    def current_reports(
        self,
        observation_ids: Iterable[str],
        *,
        expected_spec_hash: str = "",
    ) -> Dict[str, CalibrationReport]:
        reports: Dict[str, CalibrationReport] = {}
        for observation_id in sorted(set(str(value) for value in observation_ids if value)):
            revision = self.state_store.current_revision("calibration_record", observation_id)
            if revision is None:
                continue
            report = report_from_revision(revision)
            report.stale = bool(
                expected_spec_hash and report.validator_spec_hash != expected_spec_hash
            )
            reports[observation_id] = report
        return reports

    def apply_to_observation(
        self,
        observation_store: CalibrationObservationStore,
        receipt: CalibrationCommitReceipt,
    ) -> Dict[str, Any]:
        """Verify a committed revision, then bind its posterior projection."""

        if receipt.status not in {"committed", "existing"}:
            raise RuntimeError("CalibrationRecord receipt is not committed")
        revision = self.state_store.revision(receipt.revision_id)
        current = self.state_store.current_revision(
            "calibration_record", receipt.observation_id
        )
        if (
            revision is None
            or current is None
            or current.revision_id != receipt.revision_id
            or revision.object_type != "calibration_record"
            or revision.object_id != receipt.observation_id
            or revision.source_event_id != receipt.event_id
            or revision.payload_hash != receipt.payload_hash
        ):
            raise RuntimeError(
                "committed current CalibrationRecord failed read-after-write validation"
            )
        report = report_from_revision(revision)
        return observation_store._apply_committed_calibration(  # noqa: SLF001
            report.observation_id,
            prior=report.original_confidence,
            posterior=report.calibrated_confidence,
            revision_id=report.calibration_revision_id,
            input_hash=report.calculation_input_hash,
            spec_hash=report.validator_spec_hash,
            record_hash=report.calibration_record_hash,
        )

    def record_effect(
        self,
        receipt: CalibrationCommitReceipt,
        *,
        consumer_id: str,
        target_effect_id: str,
        before_hash: str,
        after_hash: str,
        evidence_refs: Sequence[str],
    ) -> None:
        command_id = receipt.command_ids.get(consumer_id)
        if not command_id:
            raise ValueError("calibration consumer command is unavailable")
        self.record_command_effect(
            command_id,
            target_effect_id=target_effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=evidence_refs,
        )

    def record_command_effect(
        self,
        command_id: str,
        *,
        target_effect_id: str,
        before_hash: str,
        after_hash: str,
        evidence_refs: Sequence[str],
    ) -> None:
        self.state_store.record_effect_receipt(
            command_id,
            status="committed",
            target_effect_id=target_effect_id,
            before_hash=before_hash,
            after_hash=after_hash,
            evidence_refs=evidence_refs,
            outcome="calibration projection committed",
        )

    def pending_commands(self, consumer_id: str) -> Dict[str, Dict[str, Any]]:
        return {
            str(value["revision_id"]): value
            for value in self.state_store.pending_commands(consumer_id)
        }


def report_from_revision(revision: CognitiveStateRevision) -> CalibrationReport:
    if revision.object_type != "calibration_record":
        raise ValueError("revision is not a calibration record")
    payload = revision.payload
    if sha256_json(payload) != revision.payload_hash:
        raise ValueError("calibration revision payload hash mismatch")
    validate_cognitive_state_payload("calibration_record", payload)
    if (
        str(payload.get("observation_id") or "") != revision.object_id
        or str(payload.get("calculation_input_hash") or "")
        != revision.source_content_hash
    ):
        raise ValueError("calibration revision identity binding mismatch")
    validations = [
        ValidationResult(
            validator_name=str(value["validator_name"]),
            score=float(value["score"]),
            verdict=str(value["verdict"]),
            reason=str(value["reason"]),
            weight=float(value.get("weight", 1.0)),
            supporting_cluster_ids=tuple(
                str(item) for item in value.get("supporting_cluster_ids", ())
            ),
            counter_cluster_ids=tuple(
                str(item) for item in value.get("counter_cluster_ids", ())
            ),
            input_hash=str(value.get("input_hash") or ""),
        )
        for value in payload["validations"]
    ]
    report = CalibrationReport(
        observation_id=str(payload["observation_id"]),
        original_confidence=float(payload["prior"]),
        calibrated_confidence=float(payload["posterior"]),
        overall_verdict=str(payload["overall_verdict"]),
        validations=validations,
        suggestions=[str(value) for value in payload.get("suggestions", ())],
        schema_version=str(payload["schema_version"]),
        validator_spec_version=str(payload["validator_version"]),
        validator_spec_hash=str(payload["validator_spec_hash"]),
        validator_code_hashes={
            str(key): str(value)
            for key, value in dict(payload["validator_code_hashes"]).items()
        },
        calculation_input_hash=str(payload["calculation_input_hash"]),
        input_snapshot=dict(payload["input_snapshot"]),
        independent_evidence_clusters=[
            dict(value) for value in payload["independent_evidence_clusters"]
        ],
        supporting_evidence=[str(value) for value in payload["supporting_evidence"]],
        counter_evidence=[str(value) for value in payload["counter_evidence"]],
        source_span_ids=[str(value) for value in payload["source_span_ids"]],
        valid_from=str(payload["valid_from"]),
        valid_until=str(payload["valid_until"]),
        omission_receipts=[dict(value) for value in payload["omission_receipts"]],
        derived_source_double_count=int(payload["derived_source_double_count"]),
        derived_members_deduplicated=int(payload["derived_members_deduplicated"]),
        calibration_revision_id=revision.revision_id,
        calibration_record_hash=revision.payload_hash,
    )
    return report


__all__ = [
    "CALIBRATION_CONSUMERS",
    "CalibrationCommitReceipt",
    "CalibrationRecordStore",
    "calibration_record_payload",
    "report_from_revision",
]
