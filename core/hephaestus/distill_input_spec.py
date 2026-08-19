"""Immutable provenance for one distillation extraction input.

The model is allowed to classify the visible input, but it is never allowed to
invent the identity of that input.  This small value object is built by the
engine before a prompt is rendered and is used both in the prompt and when the
model response is admitted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.cognitive.cognition_extraction_context import CognitionExtractionContext
from core.evidence.artifact_catalog import ArtifactCatalog
from core.evidence.source_authority import SourceAuthorityCatalog
from core.hephaestus.distill_output_version import DISTILL_OUTPUT_CONTRACT_VERSION


SCHEMA_VERSION = "mnemos.distill_input_spec.v4"
OUTPUT_CONTRACT_VERSION = DISTILL_OUTPUT_CONTRACT_VERSION


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class DistillInputSpec:
    """The immutable, non-content identity an extraction response must echo."""

    source_agent: str
    source_session_id: str
    source_event_ids: tuple[str, ...]
    raw_completeness: str
    visible_input_sha256: str
    gate_decision_id: str
    input_mode: str
    artifact_catalog: ArtifactCatalog = field(default_factory=ArtifactCatalog)
    source_authority_catalog: SourceAuthorityCatalog = field(
        default_factory=SourceAuthorityCatalog
    )
    cognition_context: CognitionExtractionContext = field(init=False)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cognition_context",
            CognitionExtractionContext.build(
                source_agent=self.source_agent,
                source_session_id=self.source_session_id,
                source_event_ids=self.source_event_ids,
                raw_completeness=self.raw_completeness,
                artifact_catalog=self.artifact_catalog,
                source_authority_catalog=self.source_authority_catalog,
            ),
        )

    @classmethod
    def build(
        cls,
        *,
        source_agent: str,
        source_session_id: str,
        source_event_ids: Iterable[str],
        raw_completeness: str,
        visible_input: str,
        input_mode: str,
        artifact_refs: Iterable[dict[str, Any]] | None = None,
        source_messages: Iterable[dict[str, Any]] | None = None,
        source_authority_context: dict[str, Any] | None = None,
    ) -> "DistillInputSpec":
        session_id = str(source_session_id or "unknown").strip() or "unknown"
        agent = str(source_agent or "unknown").strip() or "unknown"
        event_ids = tuple(
            dict.fromkeys(
                str(event_id).strip()
                for event_id in source_event_ids
                if str(event_id).strip()
            )
        ) or (f"session:{session_id}",)
        input_hash = _sha256(str(visible_input))
        artifact_catalog = ArtifactCatalog.from_refs(
            artifact_refs,
            allowed_source_event_ids=event_ids,
        )
        authority_messages = list(source_messages or ())
        if not authority_messages:
            # A detached caller did not supply role-local Raw messages.  Keep
            # the visible bytes searchable, but never guess explicit-user
            # authority from a preformatted session string.
            authority_messages = [
                {
                    "role": "unknown",
                    "content": str(visible_input),
                    "source_authority": "quoted_content",
                }
            ]
        source_authority_catalog = SourceAuthorityCatalog.from_messages(
            authority_messages,
            allowed_source_event_ids=event_ids,
            artifact_catalog=artifact_catalog,
            context=source_authority_context,
        )
        # The decision id is deterministic and contains no visible user text.
        gate_id = (
            f"extract:{session_id}:{input_hash.removeprefix('sha256:')[:16]}:"
            f"{artifact_catalog.catalog_hash.removeprefix('sha256:')[:12]}:"
            f"{source_authority_catalog.catalog_hash.removeprefix('sha256:')[:12]}"
        )
        return cls(
            source_agent=agent,
            source_session_id=session_id,
            source_event_ids=event_ids,
            raw_completeness=str(raw_completeness or "unknown"),
            visible_input_sha256=input_hash,
            gate_decision_id=gate_id,
            input_mode=str(input_mode or "standard"),
            artifact_catalog=artifact_catalog,
            source_authority_catalog=source_authority_catalog,
        )

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_agent": self.source_agent,
            "source_session_id": self.source_session_id,
            "source_event_ids": list(self.source_event_ids),
            "raw_completeness": self.raw_completeness,
            "visible_input_sha256": self.visible_input_sha256,
            "gate_decision_id": self.gate_decision_id,
            "input_mode": self.input_mode,
            "artifact_catalog": self.artifact_catalog.canonical_payload(),
            "source_authority_catalog": self.source_authority_catalog.canonical_payload(),
            "cognition_context": self.cognition_context.canonical_payload(),
        }

    @property
    def input_spec_hash(self) -> str:
        return _sha256(_canonical_json(self.canonical_payload()))

    def prompt_contract(self) -> dict[str, Any]:
        """Return only safe, model-visible immutable output fields."""
        return {
            "input_spec_hash": self.input_spec_hash,
            "gate_decision_id": self.gate_decision_id,
            "source_agent": self.source_agent,
            "source_session_id": self.source_session_id,
            "source_event_ids": list(self.source_event_ids),
            "raw_completeness": self.raw_completeness,
            "cognition_context_hash": self.cognition_context.context_hash,
            "cognition_context": self.cognition_context.prompt_payload(),
            "artifact_catalog": self.artifact_catalog.prompt_payload(),
            "source_authority_catalog": self.source_authority_catalog.prompt_payload(),
        }


@dataclass(frozen=True)
class ExtractionRequest:
    """In-memory extraction input bound to an immutable provenance spec."""

    session_text: str
    analysis_type: str
    input_spec: DistillInputSpec

    def __post_init__(self) -> None:
        visible_hash = _sha256(self.session_text)
        if visible_hash != self.input_spec.visible_input_sha256:
            raise ValueError("extraction request text does not match input spec")
        if not self.input_spec.source_agent or not self.input_spec.source_session_id:
            raise ValueError("extraction input spec requires source identity")


@dataclass(frozen=True)
class PreparedExtractionPrompt:
    """A prompt that can only be used with the request it was rendered for."""

    text: str
    prompt_hash: str
    input_spec_hash: str
    output_contract_version: str
    analysis_type: str

    @classmethod
    def build(cls, text: str, request: ExtractionRequest) -> "PreparedExtractionPrompt":
        return cls(
            text=str(text),
            prompt_hash=_sha256(str(text)),
            input_spec_hash=request.input_spec.input_spec_hash,
            output_contract_version=OUTPUT_CONTRACT_VERSION,
            analysis_type=request.analysis_type,
        )

    def assert_matches(self, request: ExtractionRequest) -> None:
        if self.input_spec_hash != request.input_spec.input_spec_hash:
            raise ValueError("prepared prompt input spec mismatch")
        if self.output_contract_version != OUTPUT_CONTRACT_VERSION:
            raise ValueError("prepared prompt output contract mismatch")
        if self.analysis_type != request.analysis_type:
            raise ValueError("prepared prompt analysis type mismatch")
        if self.prompt_hash != _sha256(self.text):
            raise ValueError("prepared prompt hash mismatch")
