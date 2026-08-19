"""System-owned provenance context for cognition episode extraction.

The model may select opaque evidence references from this context.  It never
owns source identity, exact Raw spans, authority, ACL, purpose, retention, or
the visible-input loss contract sealed here before a prompt is rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from core.cognition_episode_contract import (
    COGNITION_EXTRACTION_CONTEXT_VERSION,
    VISIBLE_INPUT_LOSS_CONTRACT,
)
from core.cognitive.access_control import (
    cognitive_access_hash,
    make_cognitive_access_envelope,
)
from core.evidence.artifact_catalog import ArtifactCatalog
from core.evidence.source_authority import SourceAuthorityCatalog


LOCAL_COGNITION_ACL = "local_user"
COGNITION_PURPOSE = "canonical_cognition_episode"
COGNITION_RETENTION_POLICY = "inherit_source"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    raw = _canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _source_spans(catalog: SourceAuthorityCatalog) -> tuple[Mapping[str, Any], ...]:
    spans: list[Mapping[str, Any]] = []
    for entry in catalog.entries:
        if (
            entry.span_status != "exact"
            or not entry.source_revision_sha256
            or entry.span_start < 0
            or entry.span_end <= entry.span_start
        ):
            continue
        spans.append(
            MappingProxyType(
                {
                    "source_authority_id": entry.source_authority_id,
                    "revision_id": entry.source_event_id,
                    "role": entry.role,
                    "span_start": entry.span_start,
                    "span_end": entry.span_end,
                    "span_status": entry.span_status,
                    "content_sha256": entry.content_sha256,
                    "source_revision_sha256": entry.source_revision_sha256,
                }
            )
        )
    return tuple(
        sorted(
            spans,
            key=lambda item: (
                str(item["revision_id"]),
                int(item["span_start"]),
                int(item["span_end"]),
                str(item["source_authority_id"]),
            ),
        )
    )


@dataclass(frozen=True)
class CognitionExtractionContext:
    """Immutable system oracle bound into one ``DistillInputSpec``."""

    source_agent: str
    source_session_id: str
    source_event_ids: tuple[str, ...]
    raw_completeness: str
    source_spans: tuple[Mapping[str, Any], ...]
    artifact_catalog_hash: str
    source_authority_catalog_hash: str
    acl: str = LOCAL_COGNITION_ACL
    purpose: str = COGNITION_PURPOSE
    retention_policy: str = COGNITION_RETENTION_POLICY
    loss_contract: str = VISIBLE_INPUT_LOSS_CONTRACT
    schema_version: str = COGNITION_EXTRACTION_CONTEXT_VERSION

    @classmethod
    def build(
        cls,
        *,
        source_agent: str,
        source_session_id: str,
        source_event_ids: Sequence[str],
        raw_completeness: str,
        artifact_catalog: ArtifactCatalog,
        source_authority_catalog: SourceAuthorityCatalog,
    ) -> "CognitionExtractionContext":
        return cls(
            source_agent=str(source_agent),
            source_session_id=str(source_session_id),
            source_event_ids=tuple(str(value) for value in source_event_ids),
            raw_completeness=str(raw_completeness),
            source_spans=_source_spans(source_authority_catalog),
            artifact_catalog_hash=artifact_catalog.catalog_hash,
            source_authority_catalog_hash=source_authority_catalog.catalog_hash,
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_agent": self.source_agent,
            "source_session_id": self.source_session_id,
            "source_event_ids": list(self.source_event_ids),
            "raw_completeness": self.raw_completeness,
            "loss_contract": self.loss_contract,
            "source_spans": [dict(value) for value in self.source_spans],
            "artifact_catalog_hash": self.artifact_catalog_hash,
            "source_authority_catalog_hash": self.source_authority_catalog_hash,
            "acl": self.acl,
            "access_control": self.access_control,
            "purpose": self.purpose,
            "retention_policy": self.retention_policy,
        }

    @property
    def access_control(self) -> dict[str, Any]:
        """Return the system-bound private ACL for this source session.

        The model sees only opaque selection handles.  Ownership, scope,
        retention and provenance remain fixed here and are copied into the
        immutable cognitive revision at commit time.
        """

        return make_cognitive_access_envelope(
            owner_principal_id=f"source-agent:{self.source_agent}",
            owner_agent=self.source_agent,
            scope_type="session",
            scope_id=self.source_session_id,
            session_id=self.source_session_id,
            purposes=(
                "canonical_cognition_episode",
                "cognitive_state_read",
                "cognitive_graph_read",
                "evidence_graph_read",
                "preflight_inject",
            ),
            consent_provenance_refs=(self.source_authority_catalog_hash,),
            sensitivity="sensitive",
            retention_policy=self.retention_policy,
            source_acl_lineage=(self.source_authority_catalog_hash,),
            # The distillation context has a stable source-agent owner rather
            # than an expiring MCP capability principal.  Agent visibility is
            # therefore the narrowest usable scope; cross-agent access still
            # requires an explicit server grant in the retrieval seam.
            visibility="agent",
        )

    @property
    def context_hash(self) -> str:
        return _sha256(self.canonical_payload())

    def prompt_payload(self) -> dict[str, Any]:
        """Expose selection handles without giving the model identity ownership."""

        return {
            "schema_version": self.schema_version,
            "context_hash": self.context_hash,
            "source_span_refs": [
                str(value["source_authority_id"]) for value in self.source_spans
            ],
            "raw_completeness": self.raw_completeness,
            "loss_contract": self.loss_contract,
            "acl": self.acl,
            "access_control_hash": cognitive_access_hash(self.access_control),
            "purpose": self.purpose,
            "retention_policy": self.retention_policy,
        }
