"""Access and deletion contracts shared by the reflection store."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Sequence

from core.cognitive.access_control import (
    derive_strictest_cognitive_access,
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.db_utils import render_sql


REFLECTION_OBJECT_PURPOSES = (
    "cognitive_state_read",
    "cognitive_state_write",
    "reflection_read",
    "reflection_feedback",
    "reflection_prompt",
    "reflection_experience_read",
    "reflection_export",
)
REFLECTION_DELETION_SCHEMA_VERSION = "mnemos.reflection_deletion.v1"
REFLECTION_DELETION_TABLE = "reflection_deletion_receipts"


def _reflection_deletion_sql(template: str) -> str:
    return render_sql(
        template,
        identifiers={"reflection_deletion_table": REFLECTION_DELETION_TABLE},
    )


def _deletion_scope_hash(scope_kind: str, scope_value: str) -> str:
    material = f"{str(scope_kind).strip().lower()}:{str(scope_value).strip()}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _reflection_deletion_receipt_id(
    *,
    request_id: str,
    object_type: str,
    object_id: str,
    scope_hash: str,
) -> str:
    material = "|".join((request_id, object_type, object_id, scope_hash))
    return "reflection-delete-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _restricted_reflection_access(object_ref: str) -> Dict[str, Any]:
    """Return a durable deny-all envelope for unproven reflection data."""

    return make_cognitive_access_envelope(
        owner_principal_id="system:reflection-store",
        owner_agent="system",
        scope_type="reflection",
        scope_id=str(object_ref or "unknown"),
        purposes=REFLECTION_OBJECT_PURPOSES,
        consent_provenance_refs=(),
        sensitivity="restricted",
        retention_policy="reflection_retention",
        source_acl_lineage=(f"reflection-source:{object_ref or 'unknown'}",),
        visibility="restricted",
        scope_resolution="restricted_unknown",
        consent_status="restricted_unknown",
    )


def normalize_reflection_access(
    access_control: Mapping[str, Any] | None,
    *,
    object_ref: str,
) -> Dict[str, Any]:
    """Normalize a record ACL without making historical data readable."""

    if not access_control:
        return _restricted_reflection_access(object_ref)
    return validate_cognitive_access_envelope(access_control)


def _load_reflection_access(raw_value: Any, *, object_ref: str) -> Dict[str, Any]:
    """Parse persisted ACL JSON without making malformed historical rows readable."""

    try:
        decoded = json.loads(str(raw_value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return _restricted_reflection_access(object_ref)
    try:
        return normalize_reflection_access(decoded, object_ref=object_ref)
    except (TypeError, ValueError):
        return _restricted_reflection_access(object_ref)


def derive_reflection_access(
    sources: Sequence[Mapping[str, Any]],
    *,
    reflection_id: str,
    owner_principal_id: str,
    owner_agent: str,
) -> Dict[str, Any]:
    """Derive a ReflectionRecord ACL from every prompt-visible source."""

    if not sources:
        return _restricted_reflection_access(reflection_id)
    try:
        return derive_strictest_cognitive_access(
            sources,
            owner_principal_id=owner_principal_id,
            owner_agent=owner_agent,
            scope_type="reflection",
            scope_id=reflection_id,
            purposes=REFLECTION_OBJECT_PURPOSES,
            retention_policy="reflection_retention",
        )
    except (TypeError, ValueError, KeyError):
        return _restricted_reflection_access(reflection_id)
