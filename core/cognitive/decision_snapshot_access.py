"""Typed source-purpose authorization for canonical Decision snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.cognitive.access_control import (
    cognitive_access_hash,
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.cognitive.state_contract import CognitiveStateRevision, sha256_json
from core.cognitive.state_store import CognitiveStateStore


DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION = "mnemos.decision_snapshot_source_purposes.v1"
DECISION_SNAPSHOT_SOURCE_PURPOSES: Mapping[str, str] = MappingProxyType(
    {
        "belief_revision": "belief_read",
        "calibration_record": "calibration_internal",
        "cognition_episode": "cognitive_state_read",
        "prediction_record": "prediction_read",
    }
)
DECISION_SNAPSHOT_OUTPUT_PURPOSE = "cognitive_state_read"
DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH = sha256_json(
    {
        "schema_version": DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION,
        "consumer": "decision_snapshot",
        "source_purposes": dict(DECISION_SNAPSHOT_SOURCE_PURPOSES),
        "output_purpose": DECISION_SNAPSHOT_OUTPUT_PURPOSE,
    }
)


@dataclass(frozen=True)
class AuthorizedDecisionSnapshotSource:
    """One body fetched only after its type-specific purpose was authorized."""

    revision: CognitiveStateRevision
    source_read_purpose: str
    access_control_hash: str
    source_purpose_contract_hash: str = DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH


def authorize_decision_snapshot_sources(
    state_store: CognitiveStateStore,
    *,
    principal: PrincipalEnvelope,
    narrowing: AccessNarrowing,
    scope_type: str,
    scope_id: str,
) -> tuple[tuple[AuthorizedDecisionSnapshotSource, ...], dict[str, Any]]:
    """Read each supported source through its one fixed purpose contract."""

    if not isinstance(state_store, CognitiveStateStore):
        raise TypeError("Decision snapshot authorization requires CognitiveStateStore")
    if not isinstance(principal, PrincipalEnvelope):
        raise TypeError("Decision snapshot authorization requires PrincipalEnvelope")
    if not isinstance(narrowing, AccessNarrowing):
        raise TypeError("Decision snapshot authorization requires AccessNarrowing")

    authorized: list[AuthorizedDecisionSnapshotSource] = []
    by_object_type: dict[str, dict[str, Any]] = {}
    aggregate_denials: dict[str, int] = {}
    candidate_count = 0
    authorized_count = 0
    for object_type, purpose in DECISION_SNAPSHOT_SOURCE_PURPOSES.items():
        revisions, summary = state_store.authorized_current_revisions(
            principal=principal,
            narrowing=narrowing,
            purpose=purpose,
            object_type=object_type,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        summary_candidate_count = int(summary["candidate_count"])
        summary_authorized_count = int(summary["authorized_count"])
        denied_by_reason = {
            str(reason): int(count)
            for reason, count in summary["denied_by_reason"].items()
        }
        typed_summary: dict[str, Any] = {
            "purpose": purpose,
            "candidate_count": summary_candidate_count,
            "authorized_count": summary_authorized_count,
            "denied_by_reason": denied_by_reason,
        }
        by_object_type[object_type] = typed_summary
        candidate_count += summary_candidate_count
        authorized_count += summary_authorized_count
        for reason, count in denied_by_reason.items():
            aggregate_denials[str(reason)] = aggregate_denials.get(str(reason), 0) + int(count)
        authorized.extend(
            AuthorizedDecisionSnapshotSource(
                revision=revision,
                source_read_purpose=purpose,
                access_control_hash=cognitive_access_hash(revision.payload["access_control"]),
            )
            for revision in revisions
        )

    ordered = tuple(
        sorted(
            authorized,
            key=lambda item: (
                item.revision.object_type,
                item.revision.object_id,
                item.revision.revision_id,
            ),
        )
    )
    return ordered, {
        "candidate_count": candidate_count,
        "authorized_count": authorized_count,
        "denied_by_reason": dict(sorted(aggregate_denials.items())),
        "contract": {
            "schema_version": DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION,
            "contract_hash": DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH,
            "output_purpose": DECISION_SNAPSHOT_OUTPUT_PURPOSE,
        },
        "by_object_type": by_object_type,
    }


def derive_decision_snapshot_access(
    source_access: Mapping[str, Any],
    consumed: Sequence[AuthorizedDecisionSnapshotSource],
    *,
    owner_principal_id: str,
    owner_agent: str,
    scope_type: str,
    scope_id: str,
    retention_policy: str,
) -> dict[str, Any]:
    """Derive Decision access after exact source-purpose authorization.

    This is deliberately separate from the generic same-purpose derivation.
    It permits only the fixed Decision consumer translation while retaining
    every ownership and scope boundary from the source ACLs.
    """

    normalized_source = validate_cognitive_access_envelope(source_access)
    if DECISION_SNAPSHOT_OUTPUT_PURPOSE not in normalized_source["purposes"]:
        raise PermissionError("decision source lacks its fixed output purpose")

    normalized_sources = [normalized_source]
    for item in consumed:
        if not isinstance(item, AuthorizedDecisionSnapshotSource):
            raise TypeError("Decision access requires typed authorized sources")
        expected_purpose = DECISION_SNAPSHOT_SOURCE_PURPOSES.get(item.revision.object_type)
        if (
            expected_purpose is None
            or item.source_read_purpose != expected_purpose
            or item.source_purpose_contract_hash != DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH
        ):
            raise PermissionError("decision source-purpose contract mismatch")
        access = validate_cognitive_access_envelope(
            item.revision.payload["access_control"],
            expected_scope_type=item.revision.scope_type,
            expected_scope_id=item.revision.scope_id,
        )
        if expected_purpose not in access[
            "purposes"
        ] or item.access_control_hash != cognitive_access_hash(access):
            raise PermissionError("decision authorized-source proof mismatch")
        normalized_sources.append(access)

    source_contexts = {
        (
            item["owner"]["agent"],
            item["owner"]["principal_id"] if item["visibility"] == "private" else "",
            item["scope"]["project"],
            item["scope"]["session_id"],
            item["scope"]["resolution"],
            item["visibility"],
            item["consent"]["status"],
            item["redaction_policy"],
        )
        for item in normalized_sources
    }
    compatible = len(source_contexts) == 1
    first = normalized_sources[0]
    if compatible:
        source_scope = first["scope"]
        compatible = bool(
            str(owner_agent).strip().lower() == str(first["owner"]["agent"])
            and (
                first["visibility"] != "private"
                or str(owner_principal_id) == str(first["owner"]["principal_id"])
            )
            and (str(scope_type) != "session" or str(scope_id) == str(source_scope["session_id"]))
            and (
                str(scope_type) != "project"
                or str(scope_id).strip().lower() == str(source_scope["project"]).strip().lower()
            )
        )

    lineage = tuple(
        sorted(
            {
                DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH,
                *(cognitive_access_hash(item) for item in normalized_sources),
                *(value for item in normalized_sources for value in item["source_acl_lineage"]),
            }
        )
    )
    if not compatible:
        return make_cognitive_access_envelope(
            owner_principal_id=owner_principal_id,
            owner_agent=owner_agent,
            scope_type=scope_type,
            scope_id=scope_id,
            purposes=(DECISION_SNAPSHOT_OUTPUT_PURPOSE,),
            consent_provenance_refs=(),
            sensitivity="restricted",
            retention_policy=retention_policy,
            source_acl_lineage=lineage,
            visibility="restricted",
            scope_resolution="restricted_unknown",
            consent_status="restricted_unknown",
        )

    sensitivity_rank = {"sensitive": 0, "private": 1, "restricted": 2}
    sensitivity = max(
        (str(item["sensitivity"]) for item in normalized_sources),
        key=sensitivity_rank.__getitem__,
    )
    source_scope = first["scope"]
    return make_cognitive_access_envelope(
        owner_principal_id=owner_principal_id,
        owner_agent=owner_agent,
        scope_type=scope_type,
        scope_id=scope_id,
        session_id=str(source_scope["session_id"]),
        project=str(source_scope["project"]),
        purposes=(DECISION_SNAPSHOT_OUTPUT_PURPOSE,),
        consent_provenance_refs=tuple(
            sorted(
                {ref for item in normalized_sources for ref in item["consent"]["provenance_refs"]}
            )
        ),
        sensitivity=sensitivity,
        retention_policy=retention_policy,
        source_acl_lineage=lineage,
        visibility=str(first["visibility"]),
        scope_resolution=str(source_scope["resolution"]),
        consent_status=str(first["consent"]["status"]),
        redaction_policy=str(first["redaction_policy"]),
    )


__all__ = [
    "AuthorizedDecisionSnapshotSource",
    "DECISION_SNAPSHOT_OUTPUT_PURPOSE",
    "DECISION_SNAPSHOT_SOURCE_PURPOSE_CONTRACT_HASH",
    "DECISION_SNAPSHOT_SOURCE_PURPOSE_SCHEMA_VERSION",
    "DECISION_SNAPSHOT_SOURCE_PURPOSES",
    "authorize_decision_snapshot_sources",
    "derive_decision_snapshot_access",
]
