"""Typed, hash-indexed provenance shared by COG-043 storage owners.

The object ACL remains the source of truth.  This module only derives exact
selector hashes from that validated ACL; it never infers a subject from free
text, a payload, a target string, or an existing session-shaped field.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from core.cognitive.access_control import (
    cognitive_access_hash,
    validate_cognitive_access_envelope,
)


OBJECT_PROVENANCE_SCHEMA_VERSION = "mnemos.object_provenance.v1"
TRACKED_PROVENANCE_STATE = "tracked"
UNATTRIBUTED_PROVENANCE_STATE = "unattributed"
SUPPORTED_SUBJECT_SCOPES = frozenset(
    {
        "all",
        "agent",
        "session",
        "project",
        "path",
        "source",
        "time",
        "wiki_page",
        "persona_signal",
        "raw_event_id",
    }
)


class ObjectProvenanceError(ValueError):
    """A storage owner was not given a typed, exact provenance selector."""


def normalize_scope_selector(scope_kind: str, scope_value: str) -> tuple[str, str]:
    """Normalize one deletion selector without widening its meaning."""

    kind = str(scope_kind or "").strip().lower()
    value = str(scope_value or "").strip()
    if kind not in SUPPORTED_SUBJECT_SCOPES:
        raise ObjectProvenanceError(f"unsupported provenance scope: {scope_kind}")
    if not value:
        raise ObjectProvenanceError("provenance scope value is required")
    if kind == "all":
        if value != "all":
            raise ObjectProvenanceError("all scope must use value 'all'")
        return kind, value
    if kind in {"agent", "project"}:
        value = value.lower()
    return kind, value


def scope_selector_hash(scope_kind: str, scope_value: str) -> str:
    """Return the non-reversible, domain-independent selector identity."""

    kind, value = normalize_scope_selector(scope_kind, scope_value)
    material = f"{kind}\x1f{value}".encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ObjectProvenance:
    """A validated ACL plus exact, hash-only deletion selectors."""

    access_control: Mapping[str, Any]
    access_json: str
    access_hash: str
    selector_hashes: tuple[tuple[str, str], ...]
    state: str = TRACKED_PROVENANCE_STATE

    @classmethod
    def from_access_control(cls, value: Mapping[str, Any]) -> "ObjectProvenance":
        if not isinstance(value, Mapping):
            raise ObjectProvenanceError("object provenance requires a typed access envelope")
        try:
            access_control = validate_cognitive_access_envelope(value)
        except ValueError as exc:
            raise ObjectProvenanceError(str(exc)) from exc
        owner = access_control["owner"]
        scope = access_control["scope"]
        selectors: set[tuple[str, str]] = set()

        owner_agent = str(owner.get("agent") or "").strip()
        if owner_agent:
            selectors.add(("agent", scope_selector_hash("agent", owner_agent)))
        project = str(scope.get("project") or "").strip()
        if project:
            selectors.add(("project", scope_selector_hash("project", project)))
        session_id = str(scope.get("session_id") or "").strip()
        if session_id:
            selectors.add(("session", scope_selector_hash("session", session_id)))

        declared_kind = str(scope.get("scope_type") or "").strip().lower()
        declared_value = str(scope.get("scope_id") or "").strip()
        if declared_kind in SUPPORTED_SUBJECT_SCOPES - {"all"} and declared_value:
            selectors.add(
                (
                    declared_kind,
                    scope_selector_hash(declared_kind, declared_value),
                )
            )

        return cls(
            access_control=access_control,
            access_json=_canonical_json(access_control),
            access_hash=cognitive_access_hash(access_control),
            selector_hashes=tuple(sorted(selectors)),
        )


def provenance_state(value: Mapping[str, Any] | None) -> str:
    """Classify absent provenance explicitly; it is never an implicit grant."""

    return TRACKED_PROVENANCE_STATE if value is not None else UNATTRIBUTED_PROVENANCE_STATE
