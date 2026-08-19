"""Version-aware construction of non-active cognitive migration candidates.

Old semantic envelopes may be complete under the contract that produced them
without satisfying the current active-state contract.  This module preserves
those records as explicitly historical revisions; it never upgrades them to a
current ``mnemos.cognition_episode.v1`` or invents missing Raw provenance.
"""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_contract import (
    COGNITIVE_OBJECT_SCHEMA_VERSIONS,
    COGNITIVE_OBJECT_TYPES,
    CognitiveStateRevision,
    canonical_json,
    now_utc,
    sha256_json,
)
from core.cognition_episode_contract import (
    LEGACY_COGNITION_EPISODE_SCHEMA_VERSION as COG010_EPISODE_SCHEMA_VERSION,
)
from core.privacy.content_redaction import REDACTION_POLICY, redact_persistence_value

LEGACY_COGNITION_EPISODE_SCHEMA_VERSION = "mnemos.cognition_episode.pre_cog010.v1"
_LEGACY_EPISODE_TEXT_FIELDS = ("situation", "goal")
_LEGACY_EPISODE_SEQUENCE_FIELDS = (
    "constraints",
    "facts",
    "hypotheses",
    "causal_links",
    "alternatives",
    "tradeoffs",
    "decisions",
    "actions",
    "outcomes",
    "corrections",
)


def historical_revision(event: Mapping[str, Any]) -> CognitiveStateRevision:
    """Build one validated historical candidate from a prior-contract envelope."""

    object_type = str(event.get("data_type") or "")
    if object_type not in COGNITIVE_OBJECT_TYPES:
        raise ValueError("legacy event is not semantic cognition")
    try:
        metadata = json.loads(str(event.get("metadata") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("legacy semantic metadata is invalid") from exc
    if not isinstance(metadata, Mapping):
        raise ValueError("legacy semantic metadata must be an object")
    source_schema_version = str(metadata.get("schema_version") or "")
    supported_source_versions = {COGNITIVE_OBJECT_SCHEMA_VERSIONS[object_type]}
    if object_type == "cognition_episode":
        supported_source_versions.add(COG010_EPISODE_SCHEMA_VERSION)
    if source_schema_version not in supported_source_versions:
        raise ValueError("legacy semantic schema version is missing or unsupported")
    payload = metadata.get("payload")
    evidence_refs = metadata.get("evidence_refs")
    if not isinstance(payload, Mapping) or not isinstance(evidence_refs, (list, tuple)):
        raise ValueError("legacy semantic payload or evidence refs are incomplete")

    common = _common_fields(event, metadata, evidence_refs)
    payload = dict(payload)
    if "access_control" not in payload:
        # Historical state did not carry an object ACL.  Do not infer one from
        # adjacent metadata: retain the candidate under an explicit restricted
        # envelope so migration never turns an unknown object into a readable
        # one.
        payload["access_control"] = make_cognitive_access_envelope(
            owner_principal_id="migration:unknown",
            owner_agent="system",
            scope_type=common["scope_type"],
            scope_id=common["scope_id"],
            purposes=("migration_reconciliation",),
            consent_provenance_refs=(),
            sensitivity="restricted",
            retention_policy="migration_quarantine",
            source_acl_lineage=(sha256_json({"event_id": common["source_event_id"]}),),
            visibility="restricted",
            scope_resolution="restricted_unknown",
            consent_status="restricted_unknown",
        )
    try:
        return CognitiveStateRevision.create(
            object_type=object_type,
            payload=payload,
            **common,
        )
    except ValueError:
        if object_type != "cognition_episode":
            raise
        return _pre_cog010_episode_revision(
            payload,
            source_schema_version=source_schema_version,
            **common,
        )


def _common_fields(
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
    evidence_refs: Sequence[Any],
) -> dict[str, Any]:
    normalized = tuple(str(value).strip() for value in evidence_refs)
    values = {
        "object_id": str(metadata.get("object_id") or event.get("canonical_subject") or "").strip(),
        "source_event_id": str(event.get("event_id") or "").strip(),
        "source_revision_id": str(metadata.get("source_revision_id") or "").strip(),
        "source_content_hash": str(event.get("content_hash") or "").strip(),
        "scope_type": str(metadata.get("scope_type") or "").strip(),
        "scope_id": str(metadata.get("scope_id") or "").strip(),
        "evidence_refs": normalized,
        "supersedes_revision_id": str(metadata.get("supersedes_revision_id") or ""),
        "correction_of_revision_id": str(metadata.get("correction_of_revision_id") or ""),
        "created_at": str(event.get("created_at") or now_utc()),
    }
    required = (
        "object_id",
        "source_event_id",
        "source_revision_id",
        "source_content_hash",
        "scope_type",
        "scope_id",
    )
    if (
        any(not values[field] for field in required)
        or not normalized
        or any(not value for value in normalized)
    ):
        raise ValueError("legacy semantic lineage or evidence refs are incomplete")
    return values


def _pre_cog010_episode_revision(
    payload: Mapping[str, Any],
    *,
    source_schema_version: str,
    **common: Any,
) -> CognitiveStateRevision:
    """Preserve the old 12-field episode only as a non-active candidate."""

    for field_name in _LEGACY_EPISODE_TEXT_FIELDS:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"legacy cognition episode missing {field_name}")
    for field_name in _LEGACY_EPISODE_SEQUENCE_FIELDS:
        if not isinstance(payload.get(field_name), (list, tuple)):
            raise ValueError(f"legacy cognition episode missing {field_name}")

    migration_payload = dict(payload)
    migration_payload["_migration_contract"] = {
        "classification": "pre_cog010_historical_candidate",
        "source_schema_version": source_schema_version,
        "active_schema_upgrade": False,
    }
    redacted = redact_persistence_value(migration_payload)
    if not isinstance(redacted.value, Mapping):
        raise ValueError("legacy cognition episode redaction is invalid")
    parsed_payload = json.loads(canonical_json(redacted.value))
    if not isinstance(parsed_payload, dict):
        raise ValueError("legacy cognition episode payload is invalid")
    frozen_payload = MappingProxyType(parsed_payload)
    evidence_refs = tuple(common["evidence_refs"])
    payload_hash = sha256_json(frozen_payload)
    evidence_hash = sha256_json(list(evidence_refs))
    identity = {
        "object_type": "cognition_episode",
        "object_id": common["object_id"],
        "schema_version": LEGACY_COGNITION_EPISODE_SCHEMA_VERSION,
        "source_event_id": common["source_event_id"],
        "source_revision_id": common["source_revision_id"],
        "source_content_hash": common["source_content_hash"],
        "scope_type": common["scope_type"],
        "scope_id": common["scope_id"],
        "evidence_hash": evidence_hash,
        "payload_hash": payload_hash,
        "supersedes_revision_id": common["supersedes_revision_id"],
        "correction_of_revision_id": common["correction_of_revision_id"],
    }
    return CognitiveStateRevision(
        revision_id="cogrev-" + sha256_json(identity).split(":", 1)[1][:32],
        object_type="cognition_episode",
        object_id=common["object_id"],
        schema_version=LEGACY_COGNITION_EPISODE_SCHEMA_VERSION,
        source_event_id=common["source_event_id"],
        source_revision_id=common["source_revision_id"],
        source_content_hash=common["source_content_hash"],
        scope_type=common["scope_type"],
        scope_id=common["scope_id"],
        evidence_refs=evidence_refs,
        payload=frozen_payload,
        payload_hash=payload_hash,
        evidence_hash=evidence_hash,
        supersedes_revision_id=common["supersedes_revision_id"],
        correction_of_revision_id=common["correction_of_revision_id"],
        created_at=common["created_at"],
        admission_state="historical_candidate",
        redaction_policy=REDACTION_POLICY,
        redaction_counts=redacted.counts,
    )
