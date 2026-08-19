"""Fail-closed object ACLs for typed cognitive state.

This module owns the *object* access contract.  It deliberately does not
reuse Wiki's permissive legacy metadata: a cognitive object is readable only
after this envelope is validated against a server-resolved principal, an
explicit purpose, and the caller's narrowing scope.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.privacy.content_redaction import REDACTION_POLICY


COGNITIVE_ACCESS_SCHEMA_VERSION = "mnemos.cognitive_access.v1"
_VISIBILITIES = frozenset({"private", "project", "agent", "system", "restricted"})
_SENSITIVITIES = frozenset({"private", "sensitive", "restricted"})
_SCOPE_RESOLUTIONS = frozenset({"resolved", "restricted_unknown"})
_CONSENT_STATUSES = frozenset({"granted", "restricted_unknown"})
_DECLARIFICATION_STATES = frozenset({"not_requested", "approved"})


@dataclass(frozen=True)
class CognitiveAccessDecision:
    """One authorization result that contains no cognitive body bytes."""

    allowed: bool
    reason: str


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _canonical_strings(value: Any, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be a sequence")
    normalized = tuple(_required_text(item, field_name) for item in value)
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must be non-empty")
    if normalized != tuple(sorted(set(normalized))):
        raise ValueError(f"{field_name} must be sorted and unique")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cognitive_access_hash(envelope: Mapping[str, Any]) -> str:
    """Return a stable identity for an ACL lineage without exposing body data."""

    normalized = validate_cognitive_access_envelope(envelope)
    return "sha256:" + hashlib.sha256(
        _canonical_json(normalized).encode("utf-8")
    ).hexdigest()


def make_cognitive_access_envelope(
    *,
    owner_principal_id: str,
    owner_agent: str,
    scope_type: str,
    scope_id: str,
    purposes: Sequence[str],
    consent_provenance_refs: Sequence[str],
    sensitivity: str,
    retention_policy: str,
    source_acl_lineage: Sequence[str],
    session_id: str = "",
    project: str = "",
    visibility: str = "private",
    scope_resolution: str = "resolved",
    consent_status: str = "granted",
    redaction_policy: str = REDACTION_POLICY,
    declassification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the one canonical ACL envelope accepted for a new object.

    Callers may only make an object broader through an audited declassification
    record.  ``public`` is intentionally not a representable visibility.
    """

    requested_declassification = dict(declassification or {"state": "not_requested"})
    envelope = {
        "schema_version": COGNITIVE_ACCESS_SCHEMA_VERSION,
        "owner": {
            "principal_id": str(owner_principal_id),
            "agent": str(owner_agent),
        },
        "scope": {
            "scope_type": str(scope_type),
            "scope_id": str(scope_id),
            "project": str(project),
            "session_id": str(session_id),
            "resolution": str(scope_resolution),
        },
        "purposes": sorted(str(value) for value in purposes),
        "consent": {
            "status": str(consent_status),
            "provenance_refs": sorted(str(value) for value in consent_provenance_refs),
        },
        "sensitivity": str(sensitivity),
        "retention_policy": str(retention_policy),
        "redaction_policy": str(redaction_policy),
        "source_acl_lineage": sorted(str(value) for value in source_acl_lineage),
        "visibility": str(visibility),
        "declassification": requested_declassification,
    }
    return validate_cognitive_access_envelope(envelope)


def validate_cognitive_access_envelope(
    value: Mapping[str, Any],
    *,
    expected_scope_type: str = "",
    expected_scope_id: str = "",
) -> dict[str, Any]:
    """Validate and normalize a persisted ACL envelope.

    Unknown schema, incomplete ownership, unresolved scope, or an unaudited
    broadening are all errors.  Readers turn those errors into a denied result;
    writers fail before the immutable revision is committed.
    """

    if not isinstance(value, Mapping):
        raise ValueError("access_control must be an object")
    required_fields = {
        "schema_version",
        "owner",
        "scope",
        "purposes",
        "consent",
        "sensitivity",
        "retention_policy",
        "redaction_policy",
        "source_acl_lineage",
        "visibility",
        "declassification",
    }
    if set(value) != required_fields:
        missing = sorted(required_fields - set(value))
        unknown = sorted(set(value) - required_fields)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise ValueError("access_control fields invalid: " + "; ".join(details))
    if value.get("schema_version") != COGNITIVE_ACCESS_SCHEMA_VERSION:
        raise ValueError("access_control schema version is unsupported")

    owner = value.get("owner")
    if not isinstance(owner, Mapping) or set(owner) != {"principal_id", "agent"}:
        raise ValueError("access_control owner is invalid")
    owner_principal_id = _required_text(owner.get("principal_id"), "access_control.owner.principal_id")
    owner_agent = _required_text(owner.get("agent"), "access_control.owner.agent").lower()

    scope = value.get("scope")
    required_scope_fields = {"scope_type", "scope_id", "project", "session_id", "resolution"}
    if not isinstance(scope, Mapping) or set(scope) != required_scope_fields:
        raise ValueError("access_control scope is invalid")
    scope_type = _required_text(scope.get("scope_type"), "access_control.scope.scope_type")
    scope_id = _required_text(scope.get("scope_id"), "access_control.scope.scope_id")
    project = str(scope.get("project") or "").strip().lower()
    session_id = str(scope.get("session_id") or "").strip()
    resolution = _required_text(scope.get("resolution"), "access_control.scope.resolution")
    if resolution not in _SCOPE_RESOLUTIONS:
        raise ValueError("access_control scope resolution is invalid")
    if expected_scope_type and scope_type != expected_scope_type:
        raise ValueError("access_control scope type does not match the revision")
    if expected_scope_id and scope_id != expected_scope_id:
        raise ValueError("access_control scope id does not match the revision")

    purposes = _canonical_strings(value.get("purposes"), "access_control.purposes")
    consent = value.get("consent")
    if not isinstance(consent, Mapping) or set(consent) != {"status", "provenance_refs"}:
        raise ValueError("access_control consent is invalid")
    consent_status = _required_text(consent.get("status"), "access_control.consent.status")
    if consent_status not in _CONSENT_STATUSES:
        raise ValueError("access_control consent status is invalid")
    provenance_refs = _canonical_strings(
        consent.get("provenance_refs"),
        "access_control.consent.provenance_refs",
        allow_empty=consent_status == "restricted_unknown",
    )
    if consent_status == "granted" and not provenance_refs:
        raise ValueError("access_control granted consent requires provenance")

    sensitivity = _required_text(value.get("sensitivity"), "access_control.sensitivity")
    if sensitivity not in _SENSITIVITIES:
        raise ValueError("access_control sensitivity is invalid")
    retention_policy = _required_text(value.get("retention_policy"), "access_control.retention_policy")
    redaction_policy = _required_text(value.get("redaction_policy"), "access_control.redaction_policy")
    lineage = _canonical_strings(
        value.get("source_acl_lineage"),
        "access_control.source_acl_lineage",
    )
    visibility = _required_text(value.get("visibility"), "access_control.visibility")
    if visibility not in _VISIBILITIES:
        raise ValueError("access_control visibility is invalid")

    declassification = value.get("declassification")
    if not isinstance(declassification, Mapping):
        raise ValueError("access_control declassification is invalid")
    state = _required_text(declassification.get("state"), "access_control.declassification.state")
    if state not in _DECLARIFICATION_STATES:
        raise ValueError("access_control declassification state is invalid")
    if state == "not_requested":
        if set(declassification) != {"state"}:
            raise ValueError("access_control unrequested declassification carries audit fields")
        normalized_declassification: dict[str, str] = {"state": state}
    else:
        if set(declassification) != {"state", "receipt_ref", "approved_by"}:
            raise ValueError("access_control approved declassification is incomplete")
        normalized_declassification = {
            "state": state,
            "receipt_ref": _required_text(
                declassification.get("receipt_ref"),
                "access_control.declassification.receipt_ref",
            ),
            "approved_by": _required_text(
                declassification.get("approved_by"),
                "access_control.declassification.approved_by",
            ),
        }

    if resolution == "resolved":
        if not project and not session_id:
            raise ValueError("access_control resolved scope requires project or session")
        if scope_type == "session" and scope_id != session_id:
            raise ValueError("access_control session scope must bind the exact session")
        if scope_type == "project" and scope_id.lower() != project:
            raise ValueError("access_control project scope must bind the exact project")
        if visibility == "restricted" or consent_status != "granted":
            raise ValueError("access_control resolved scope cannot be restricted")
    else:
        if visibility != "restricted" or consent_status != "restricted_unknown":
            raise ValueError("access_control unresolved scope must fail closed")

    return {
        "schema_version": COGNITIVE_ACCESS_SCHEMA_VERSION,
        "owner": {"principal_id": owner_principal_id, "agent": owner_agent},
        "scope": {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "project": project,
            "session_id": session_id,
            "resolution": resolution,
        },
        "purposes": list(purposes),
        "consent": {"status": consent_status, "provenance_refs": list(provenance_refs)},
        "sensitivity": sensitivity,
        "retention_policy": retention_policy,
        "redaction_policy": redaction_policy,
        "source_acl_lineage": list(lineage),
        "visibility": visibility,
        "declassification": normalized_declassification,
    }


def authorize_cognitive_access(
    envelope: Mapping[str, Any],
    *,
    principal: PrincipalEnvelope | None,
    narrowing: AccessNarrowing | None,
    purpose: str,
) -> CognitiveAccessDecision:
    """Authorize one candidate before its semantic payload is fetched."""

    if principal is None:
        return CognitiveAccessDecision(False, "principal_required")
    try:
        normalized = validate_cognitive_access_envelope(envelope)
    except ValueError:
        return CognitiveAccessDecision(False, "acl_unknown")
    requested_purpose = str(purpose or "").strip()
    if not requested_purpose:
        return CognitiveAccessDecision(False, "purpose_required")
    scope = normalized["scope"]
    if scope["resolution"] != "resolved":
        return CognitiveAccessDecision(False, "acl_scope_unresolved")
    if normalized["visibility"] == "restricted":
        return CognitiveAccessDecision(False, "acl_restricted")
    if normalized["consent"]["status"] != "granted":
        return CognitiveAccessDecision(False, "consent_unresolved")
    if requested_purpose not in normalized["purposes"]:
        return CognitiveAccessDecision(False, "purpose_not_permitted")

    owner_principal_id = str(normalized["owner"]["principal_id"])
    owner_agent = str(normalized["owner"]["agent"])
    request_agent = str(principal.agent or "").strip().lower()
    cross_agent_grants = {str(value).strip().lower() for value in principal.allowed_source_agents}
    if request_agent != owner_agent and owner_agent not in cross_agent_grants and "*" not in cross_agent_grants:
        return CognitiveAccessDecision(False, "owner_agent_mismatch")
    if normalized["visibility"] == "private" and principal.principal_id != owner_principal_id:
        return CognitiveAccessDecision(False, "owner_principal_mismatch")

    effective_narrowing = narrowing or AccessNarrowing()
    project = str(scope["project"])
    if project:
        granted_projects = {str(value).strip().lower() for value in principal.allowed_projects}
        if project not in granted_projects and "*" not in granted_projects:
            return CognitiveAccessDecision(False, "principal_project_grant_missing")
        if str(effective_narrowing.project or "").strip().lower() != project:
            return CognitiveAccessDecision(False, "project_scope_mismatch")
    session_id = str(scope["session_id"])
    if session_id and str(effective_narrowing.session_id or "").strip() != session_id:
        return CognitiveAccessDecision(False, "session_scope_mismatch")
    return CognitiveAccessDecision(True, "authorized")


def authorize_cognitive_write(
    envelope: Mapping[str, Any],
    *,
    principal: PrincipalEnvelope | None,
    scope_type: str,
    scope_id: str,
) -> CognitiveAccessDecision:
    """Authorize a semantic write from a server-resolved source ACL.

    A caller cannot nominate a new object's ACL.  It may only ask the state
    owner to derive one from an already-authorized source envelope.  Keeping
    this check next to object reads makes the ownership and purpose boundary
    explicit instead of trusting an application trace's metadata.
    """

    if principal is None:
        return CognitiveAccessDecision(False, "principal_required")
    if "memory_write" not in principal.capabilities and "*" not in principal.capabilities:
        return CognitiveAccessDecision(False, "principal_write_capability_missing")
    try:
        normalized = validate_cognitive_access_envelope(
            envelope,
            expected_scope_type=scope_type,
            expected_scope_id=scope_id,
        )
    except ValueError:
        return CognitiveAccessDecision(False, "source_acl_unknown")
    return authorize_cognitive_access(
        normalized,
        principal=principal,
        narrowing=AccessNarrowing(
            session_id=str(normalized["scope"]["session_id"]),
            project=str(normalized["scope"]["project"]),
        ),
        purpose="cognitive_state_write",
    )


def cognitive_access_matches_subject(
    envelope: Mapping[str, Any],
    *,
    scope_kind: str,
    scope_value: str,
) -> bool:
    """Match an ownership deletion subject using only a validated ACL header.

    This intentionally supports only scopes that are first-class fields of the
    envelope.  It must never inspect a semantic body, infer a source event
    from prose, or treat an unknown historical ACL as a match.
    """

    try:
        normalized = validate_cognitive_access_envelope(envelope)
    except (TypeError, ValueError):
        return False
    kind = str(scope_kind or "").strip().lower()
    value = str(scope_value or "").strip()
    if kind == "all":
        return value == "all"
    if kind == "agent":
        return str(normalized["owner"]["agent"]).lower() == value.lower()
    if kind == "session":
        return str(normalized["scope"]["session_id"]) == value
    if kind == "project":
        return str(normalized["scope"]["project"]).lower() == value.lower()
    return False


def derive_strictest_cognitive_access(
    sources: Sequence[Mapping[str, Any]],
    *,
    owner_principal_id: str,
    owner_agent: str,
    scope_type: str,
    scope_id: str,
    purposes: Sequence[str],
    retention_policy: str,
) -> dict[str, Any]:
    """Derive an ACL that is never broader than its source objects.

    Incompatible principals or scopes deliberately become a restricted unknown
    object.  A later audited declassification can create a new revision, but
    cannot silently make the derived object readable.
    """

    if not sources:
        raise ValueError("derived cognitive access requires at least one source")
    normalized_sources = [validate_cognitive_access_envelope(source) for source in sources]
    # A derived object has its own object identity (for example an Observation
    # becoming a ReflectionRecord), so its ``scope_type/scope_id`` are not
    # expected to equal a source object's ID.  The privacy boundary that must
    # remain invariant is the resolved project/session, ownership, visibility
    # and consent context.  Treating the source object's ID as an invariant
    # would force every useful derived object into ``restricted_unknown``;
    # omitting project/session would instead permit a cross-scope rebind.
    source_contexts = {
        (
            item["owner"]["agent"],
            (
                item["owner"]["principal_id"]
                if item["visibility"] == "private"
                else ""
            ),
            item["scope"]["project"],
            item["scope"]["session_id"],
            item["scope"]["resolution"],
            item["visibility"],
            item["consent"]["status"],
        )
        for item in normalized_sources
    }
    requested_purposes = tuple(sorted(set(str(value).strip() for value in purposes if str(value).strip())))
    if not requested_purposes:
        raise ValueError("derived cognitive access purposes are required")
    lineage = tuple(sorted({cognitive_access_hash(item) for item in normalized_sources}))
    source_lineage = tuple(
        sorted(
            {
                *lineage,
                *(value for item in normalized_sources for value in item["source_acl_lineage"]),
            }
        )
    )
    compatible = len(source_contexts) == 1
    first = normalized_sources[0]
    if compatible:
        source_scope = first["scope"]
        if (
            str(owner_agent).strip().lower() != str(first["owner"]["agent"])
            or (
                first["visibility"] == "private"
                and str(owner_principal_id) != str(first["owner"]["principal_id"])
            )
            or (
                str(scope_type) == "session"
                and str(scope_id) != str(source_scope["session_id"])
            )
            or (
                str(scope_type) == "project"
                and str(scope_id).strip().lower() != str(source_scope["project"])
            )
        ):
            compatible = False
        allowed_purposes = set(first["purposes"])
        for item in normalized_sources[1:]:
            allowed_purposes.intersection_update(item["purposes"])
        if not set(requested_purposes).issubset(allowed_purposes):
            compatible = False

    if compatible:
        return make_cognitive_access_envelope(
            owner_principal_id=owner_principal_id,
            owner_agent=owner_agent,
            scope_type=scope_type,
            scope_id=scope_id,
            session_id=str(source_scope["session_id"]),
            project=str(source_scope["project"]),
            purposes=requested_purposes,
            consent_provenance_refs=tuple(
                sorted(
                    {
                        ref
                        for item in normalized_sources
                        for ref in item["consent"]["provenance_refs"]
                    }
                )
            ),
            sensitivity=(
                "restricted"
                if any(item["sensitivity"] == "restricted" for item in normalized_sources)
                else "sensitive"
            ),
            retention_policy=retention_policy,
            source_acl_lineage=source_lineage,
            visibility=str(first["visibility"]),
            scope_resolution=str(source_scope["resolution"]),
            consent_status=str(first["consent"]["status"]),
        )

    return make_cognitive_access_envelope(
        owner_principal_id=owner_principal_id,
        owner_agent=owner_agent,
        scope_type=scope_type,
        scope_id=scope_id,
        purposes=requested_purposes,
        consent_provenance_refs=(),
        sensitivity="restricted",
        retention_policy=retention_policy,
        source_acl_lineage=source_lineage,
        visibility="restricted",
        scope_resolution="restricted_unknown",
        consent_status="restricted_unknown",
    )


__all__ = [
    "COGNITIVE_ACCESS_SCHEMA_VERSION",
    "CognitiveAccessDecision",
    "cognitive_access_matches_subject",
    "authorize_cognitive_access",
    "authorize_cognitive_write",
    "cognitive_access_hash",
    "derive_strictest_cognitive_access",
    "make_cognitive_access_envelope",
    "validate_cognitive_access_envelope",
]
