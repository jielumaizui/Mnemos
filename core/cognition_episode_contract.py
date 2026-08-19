"""Shared contracts for model drafts and canonical cognition episodes."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

LEGACY_COGNITION_EPISODE_SCHEMA_VERSION = "mnemos.cognition_episode.v1"
COGNITION_EPISODE_SCHEMA_VERSION = "mnemos.cognition_episode.v2"
SUPPORTED_COGNITION_EPISODE_SCHEMA_VERSIONS = frozenset(
    {
        LEGACY_COGNITION_EPISODE_SCHEMA_VERSION,
        COGNITION_EPISODE_SCHEMA_VERSION,
    }
)
COGNITION_EXTRACTION_CONTEXT_VERSION = "mnemos.cognition_extraction_context.v1"
VISIBLE_INPUT_LOSS_CONTRACT = "lossless-visible-v1"
COGNITION_EPISODE_V2_FIELDS = (
    "claims",
    "claim_catalog_hash",
    "user_behavior_intent",
)
COGNITION_EPISODE_FIELDS = (
    "situation",
    "goal",
    "desired_state",
    "facts",
    "assumptions",
    "hypotheses",
    "causal_links",
    "alternatives",
    "tradeoffs",
    "decision",
    "rationale",
    "actions",
    "outcomes",
    "root_cause",
    "correction",
    "supersedes",
    "uncertainty",
    "invalidation_conditions",
    "scope",
)
COGNITION_EPISODE_STATUSES = frozenset({"known", "unknown", "not_applicable"})


def structured_output(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    nested = payload.get("structured_output")
    return nested if isinstance(nested, Mapping) else payload


def iter_cognition_episode_evidence(
    payload: Any,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield every model-selected episode evidence object with a stable path."""

    structured = structured_output(payload)
    if not isinstance(structured, Mapping):
        return
    episode = structured.get("cognition_episode")
    if not isinstance(episode, Mapping):
        return
    for field_name in COGNITION_EPISODE_FIELDS:
        entries = episode.get(field_name)
        if not isinstance(entries, list):
            continue
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                continue
            evidence_refs = entry.get("evidence_refs")
            if not isinstance(evidence_refs, list):
                continue
            for evidence_index, evidence in enumerate(evidence_refs):
                if isinstance(evidence, dict):
                    yield (
                        "structured_output.cognition_episode."
                        f"{field_name}[{entry_index}].evidence_refs[{evidence_index}]",
                        evidence,
                    )
