"""Canonical domain contracts for gaps, blindspots, and preferences.

The three concepts intentionally share no persistence identity.  A missing
knowledge asset is a property of a knowledge scope, a cognitive blindspot is a
scoped hypothesis about a decision, and an interaction preference is a
revisable adaptation signal.  Callers must not promote one into another merely
because their display text mentions the same topic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from core.evidence.source_authority import SourceAuthorityCatalog


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tuple(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


@dataclass(frozen=True)
class CognitiveAuthorityEvidence:
    """System-resolved high-authority evidence for one cognitive mutation.

    Callers cannot construct this contract from a bare string.  The builder
    resolves an opaque source-authority id against the immutable catalog and
    proves that the selected quote belongs to the exact authorized span.
    """

    source_authority_id: str
    source_event_id: str
    catalog_hash: str
    authority: str
    purpose: str
    role: str
    span_start: int
    span_end: int
    content_sha256: str
    quote_sha256: str

    @classmethod
    def from_catalog(
        cls,
        catalog: SourceAuthorityCatalog,
        *,
        source_authority_id: str,
        quote: str,
    ) -> "CognitiveAuthorityEvidence":
        if not isinstance(catalog, SourceAuthorityCatalog):
            raise TypeError("typed SourceAuthorityCatalog is required")
        catalog.require_admissible()
        entry = catalog.get(source_authority_id)
        quote_value = str(quote or "").strip()
        if entry is None or not entry.allows_cognitive_update:
            raise ValueError("source authority cannot authorize a cognitive update")
        if not quote_value or not entry.matches_quote(quote_value):
            raise ValueError("cognitive evidence quote is outside the selected source span")
        return cls(
            source_authority_id=entry.source_authority_id,
            source_event_id=entry.source_event_id,
            catalog_hash=catalog.catalog_hash,
            authority=entry.authority.value,
            purpose=entry.purpose,
            role=entry.role,
            span_start=entry.span_start,
            span_end=entry.span_end,
            content_sha256=entry.content_sha256,
            quote_sha256=_canonical_hash(quote_value),
        )

    def verify(self, catalog: SourceAuthorityCatalog) -> None:
        if not isinstance(catalog, SourceAuthorityCatalog):
            raise TypeError("typed SourceAuthorityCatalog is required")
        catalog.require_admissible()
        entry = catalog.get(self.source_authority_id)
        if (
            entry is None
            or not entry.allows_cognitive_update
            or catalog.catalog_hash != self.catalog_hash
            or entry.source_event_id != self.source_event_id
            or entry.authority.value != self.authority
            or entry.purpose != self.purpose
            or entry.role != self.role
            or entry.span_start != self.span_start
            or entry.span_end != self.span_end
            or entry.content_sha256 != self.content_sha256
        ):
            raise ValueError("cognitive authority evidence no longer matches its catalog")

    @property
    def evidence_ref(self) -> str:
        return "cognitive-authority:" + _canonical_hash(self.canonical_payload()).split(":", 1)[1]

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "source_authority_id": self.source_authority_id,
            "source_event_id": self.source_event_id,
            "catalog_hash": self.catalog_hash,
            "authority": self.authority,
            "purpose": self.purpose,
            "role": self.role,
            "span_start": self.span_start,
            "span_end": self.span_end,
            "content_sha256": self.content_sha256,
            "quote_sha256": self.quote_sha256,
        }


@dataclass(frozen=True)
class AssetScope:
    """The exact boundary in which one user-model asset is meaningful."""

    scope_type: str
    scope_id: str
    purpose: str
    principal_id: str = ""

    def __post_init__(self) -> None:
        if self.scope_type not in {"vault", "project", "session", "user"}:
            raise ValueError("unsupported user-model asset scope_type")
        if not self.scope_id.strip():
            raise ValueError("user-model asset scope_id is required")
        if not self.purpose.strip():
            raise ValueError("user-model asset purpose is required")

    @property
    def key(self) -> str:
        return _canonical_hash(
            {
                "scope_type": self.scope_type,
                "scope_id": self.scope_id,
                "purpose": self.purpose,
                "principal_id": self.principal_id,
            }
        )


@dataclass(frozen=True)
class KnowledgeCoverageGap:
    """A missing, stale, or insufficiently connected knowledge asset."""

    asset_id: str
    revision_id: str
    topic: str
    dimension: str
    description: str
    evidence_refs: tuple[str, ...]
    scope: AssetScope
    confidence: float
    detected_at: str
    expires_at: str
    status: str = "detected"
    supersedes_revision_id: str = ""
    resolution_condition: str = "verified_knowledge_coverage_recheck"
    resolution_evidence_refs: tuple[str, ...] = ()
    consumers: tuple[str, ...] = ("knowledge_retrieval", "verification_queue")
    asset_type: str = field(default="knowledge_coverage_gap", init=False)

    @classmethod
    def create(
        cls,
        *,
        topic: str,
        dimension: str,
        description: str,
        evidence_refs: Iterable[str],
        scope: AssetScope,
        confidence: float,
        expires_at: str,
        detected_at: str = "",
        revision_number: int = 1,
        supersedes_revision_id: str = "",
    ) -> "KnowledgeCoverageGap":
        topic_value = topic.strip()
        evidence = _tuple(evidence_refs)
        if not topic_value or not description.strip() or not evidence:
            raise ValueError("knowledge coverage gap requires topic, description, and evidence")
        if dimension not in {
            "missing_topic",
            "missing_form",
            "domain_sparsity",
            "temporal_staleness",
            "relation_sparsity",
            "unsolved_trail",
            "unrecorded_trail",
        }:
            raise ValueError("unsupported knowledge coverage gap dimension")
        if not expires_at.strip():
            raise ValueError("knowledge coverage gap expires_at is required")
        asset_id = (
            "kcg_"
            + _canonical_hash(
                {
                    "asset_type": "knowledge_coverage_gap",
                    "scope": scope.key,
                    "dimension": dimension,
                    "topic": topic_value.casefold(),
                }
            ).split(":", 1)[1][:24]
        )
        revision_id = f"{asset_id}:r{int(revision_number)}"
        return cls(
            asset_id=asset_id,
            revision_id=revision_id,
            topic=topic_value,
            dimension=dimension,
            description=description.strip(),
            evidence_refs=evidence,
            scope=scope,
            confidence=min(max(float(confidence), 0.0), 1.0),
            detected_at=detected_at or _now(),
            expires_at=expires_at,
            supersedes_revision_id=supersedes_revision_id,
        )


@dataclass(frozen=True)
class KnowledgeCoverageResolutionEvidence:
    """Independent proof that one exact knowledge-gap revision is covered."""

    receipt_id: str
    asset_id: str
    gap_revision_id: str
    scope_key: str
    verifier_id: str
    verification_method: str
    content_hash: str
    verified_at: str
    outcome: str = "covered"

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> "KnowledgeCoverageResolutionEvidence":
        values = {
            key: str(payload.get(key) or "").strip()
            for key in (
                "receipt_id",
                "asset_id",
                "gap_revision_id",
                "scope_key",
                "verifier_id",
                "verification_method",
                "content_hash",
                "verified_at",
                "outcome",
            )
        }
        if not values["outcome"]:
            values["outcome"] = "covered"
        required = tuple(value for key, value in values.items() if key != "outcome")
        if not all(required):
            raise ValueError("knowledge coverage resolution evidence is incomplete")
        if values["outcome"] != "covered":
            raise ValueError("knowledge coverage resolution outcome must be covered")
        if values["verifier_id"] in {
            "wiki_projection",
            "distillation_renderer",
            "blindspot_discovery",
        }:
            raise ValueError("knowledge coverage verifier must be independent")
        content_hash = values["content_hash"]
        digest = content_hash.split(":", 1)[1] if content_hash.startswith("sha256:") else ""
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("knowledge coverage content_hash must be an exact SHA-256")
        try:
            datetime.fromisoformat(values["verified_at"])
        except ValueError as exc:
            raise ValueError("knowledge coverage verified_at must be an ISO timestamp") from exc
        return cls(**values)

    @property
    def evidence_ref(self) -> str:
        return (
            "knowledge-coverage-resolution:"
            + _canonical_hash(
                {
                    "receipt_id": self.receipt_id,
                    "asset_id": self.asset_id,
                    "gap_revision_id": self.gap_revision_id,
                    "scope_key": self.scope_key,
                    "verifier_id": self.verifier_id,
                    "verification_method": self.verification_method,
                    "content_hash": self.content_hash,
                    "verified_at": self.verified_at,
                    "outcome": self.outcome,
                }
            ).split(":", 1)[1]
        )


@dataclass
class UserCognitiveBlindspot:
    """A scoped, evidence-backed hypothesis about a decision limitation."""

    type: str
    description: str
    evidence: list[str]
    confidence: float = 0.0
    first_detected: str = ""
    last_challenged: str = ""
    challenge_count: int = 0
    user_reaction: str = ""
    status: str = "suspected"
    user_goal_ref: str = ""
    impact: str = ""
    scope_type: str = "session"
    scope_id: str = ""
    purpose: str = "decision_support"
    principal_id: str = ""
    expires_at: str = ""
    invalidation_condition: str = ""
    revision_id: str = ""
    supersedes_revision_id: str = ""
    consumers: tuple[str, ...] = ("decision_support", "persona_challenge")
    asset_id: str = ""
    authority_evidence_refs: tuple[str, ...] = ()
    admission_command_id: str = ""
    admission_command_hash: str = ""
    admission_idempotency_key: str = ""
    decision_context: dict[str, str] = field(default_factory=dict)
    challenge_outcome: str = ""
    challenge_delivery_id: str = ""
    challenge_presentation_receipt_hash: str = ""
    challenge_feedback_event_id: str = ""
    challenge_reaction_revision_id: str = ""

    @property
    def asset_type(self) -> str:
        return "user_cognitive_blindspot"

    @property
    def blindspot_type(self) -> str:
        return self.type

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        """Immutable evidence projection used by the canonical contract."""

        return _tuple(self.evidence)

    @property
    def scope(self) -> AssetScope:
        return AssetScope(
            scope_type=self.scope_type,
            scope_id=self.scope_id,
            purpose=self.purpose,
            principal_id=self.principal_id,
        )

    @classmethod
    def create(
        cls,
        *,
        blindspot_type: str,
        description: str,
        evidence_refs: Iterable[str],
        user_goal_ref: str,
        impact: str,
        scope: AssetScope,
        confidence: float,
        expires_at: str,
        invalidation_condition: str,
        first_detected: str = "",
        revision_number: int = 1,
        supersedes_revision_id: str = "",
        authority_evidence_refs: Iterable[str] = (),
        admission_command_id: str = "",
        admission_command_hash: str = "",
        admission_idempotency_key: str = "",
        decision_context: Mapping[str, str] | None = None,
    ) -> "UserCognitiveBlindspot":
        evidence = _tuple(evidence_refs)
        if (
            not all(
                value.strip()
                for value in (
                    blindspot_type,
                    description,
                    user_goal_ref,
                    impact,
                    expires_at,
                    invalidation_condition,
                )
            )
            or not evidence
        ):
            raise ValueError(
                "user cognitive blindspot requires goal, impact, scope, evidence, and expiry"
            )
        command_key = admission_idempotency_key.strip()
        asset_id = (
            "ucb_"
            + _canonical_hash(
                {
                    "asset_type": "user_cognitive_blindspot",
                    "scope": scope.key,
                    "admission_idempotency_key": command_key,
                    "blindspot_type": blindspot_type if not command_key else "",
                    "user_goal_ref": user_goal_ref if not command_key else "",
                }
            ).split(":", 1)[1][:24]
        )
        return cls(
            type=blindspot_type,
            description=description,
            evidence=list(evidence),
            confidence=min(max(float(confidence), 0.0), 1.0),
            first_detected=first_detected or _now(),
            user_goal_ref=user_goal_ref,
            impact=impact,
            scope_type=scope.scope_type,
            scope_id=scope.scope_id,
            purpose=scope.purpose,
            principal_id=scope.principal_id,
            expires_at=expires_at,
            invalidation_condition=invalidation_condition,
            revision_id=f"{asset_id}:r{int(revision_number)}",
            supersedes_revision_id=supersedes_revision_id,
            asset_id=asset_id,
            authority_evidence_refs=_tuple(authority_evidence_refs),
            admission_command_id=admission_command_id.strip(),
            admission_command_hash=admission_command_hash.strip(),
            admission_idempotency_key=command_key,
            decision_context={
                str(key): str(value)
                for key, value in dict(decision_context or {}).items()
                if str(key).strip() and str(value).strip()
            },
        )


@dataclass(frozen=True)
class InteractionPreference:
    """A scoped and revisable preference for how an interaction is conducted."""

    asset_id: str
    revision_id: str
    dimension: str
    value: str
    evidence_refs: tuple[str, ...]
    scope: AssetScope
    confidence: float
    observed_at: str
    expires_at: str
    invalidation_condition: str
    status: str = "active"
    supersedes_revision_id: str = ""
    invalidation_evidence_refs: tuple[str, ...] = ()
    authority_evidence_refs: tuple[str, ...] = ()
    consumers: tuple[str, ...] = ("context_search",)
    asset_type: str = field(default="interaction_preference", init=False)

    @classmethod
    def create(
        cls,
        *,
        dimension: str,
        value: str,
        evidence_refs: Iterable[str],
        scope: AssetScope,
        confidence: float,
        expires_at: str,
        invalidation_condition: str,
        observed_at: str = "",
        revision_number: int = 1,
        supersedes_revision_id: str = "",
        authority_evidence_refs: Iterable[str] = (),
    ) -> "InteractionPreference":
        evidence = _tuple(evidence_refs)
        if (
            not all(item.strip() for item in (dimension, value, expires_at, invalidation_condition))
            or not evidence
        ):
            raise ValueError(
                "interaction preference requires value, evidence, scope, expiry, and invalidation"
            )
        asset_id = (
            "ipr_"
            + _canonical_hash(
                {
                    "asset_type": "interaction_preference",
                    "scope": scope.key,
                    "dimension": dimension,
                }
            ).split(":", 1)[1][:24]
        )
        return cls(
            asset_id=asset_id,
            revision_id=f"{asset_id}:r{int(revision_number)}",
            dimension=dimension,
            value=value,
            evidence_refs=evidence,
            scope=scope,
            confidence=min(max(float(confidence), 0.0), 1.0),
            observed_at=observed_at or _now(),
            expires_at=expires_at,
            invalidation_condition=invalidation_condition,
            supersedes_revision_id=supersedes_revision_id,
            authority_evidence_refs=_tuple(authority_evidence_refs),
        )


def cognitive_evidence_payloads(
    evidence: Iterable[CognitiveAuthorityEvidence],
    catalog: SourceAuthorityCatalog,
) -> tuple[dict[str, Any], ...]:
    """Verify and serialize high-authority evidence without storing quote bytes."""

    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, CognitiveAuthorityEvidence):
            raise TypeError("typed CognitiveAuthorityEvidence is required")
        item.verify(catalog)
        if item.evidence_ref in seen:
            continue
        seen.add(item.evidence_ref)
        payloads.append({**item.canonical_payload(), "evidence_ref": item.evidence_ref})
    if not payloads:
        raise ValueError("at least one cognitive authority evidence item is required")
    return tuple(payloads)


__all__ = [
    "AssetScope",
    "CognitiveAuthorityEvidence",
    "InteractionPreference",
    "KnowledgeCoverageGap",
    "KnowledgeCoverageResolutionEvidence",
    "UserCognitiveBlindspot",
    "cognitive_evidence_payloads",
]
