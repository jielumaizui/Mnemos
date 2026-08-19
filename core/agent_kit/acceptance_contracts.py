# -*- coding: utf-8 -*-
"""Acceptance contracts for the Mnemos agent data life line.

This module is the executable source of truth for roadmap step 1:
agent samples, raw event fields, distilled knowledge fields, and downstream
linkage fields must stay aligned before raw ingestion and distillation audits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, cast

from core.agent_kit.protocol import TARGET_AGENT_NAMES, required_cognitive_capabilities
from core.utils import load_json_value


SCHEMA_VERSION = "mnemos_acceptance_contracts.v1"

SAMPLE_TYPES: tuple[dict[str, str], ...] = (
    {
        "id": "ordinary_qa",
        "name": "Ordinary question and answer",
        "purpose": "Validate visible user and assistant text without tools.",
    },
    {
        "id": "tool_call",
        "name": "Tool call and result",
        "purpose": "Validate tool_calls and tool_results preservation.",
    },
    {
        "id": "long_multiturn",
        "name": "Long multi-turn conversation",
        "purpose": "Validate turn order, session continuity, and long text handling.",
    },
    {
        "id": "file_attachment_context",
        "name": "File or attachment context",
        "purpose": "Validate attachments, media, or explicit loss declaration.",
    },
    {
        "id": "artifact_uri_context",
        "name": "Artifact URI context",
        "purpose": (
            "Validate normalized artifact_refs for tool results, attachments, "
            "screenshots, terminals, and test reports."
        ),
    },
    {
        "id": "reasoning_metadata",
        "name": "Reasoning metadata",
        "purpose": "Validate available thinking metadata without exposing private chains.",
    },
    {
        "id": "cross_directory_project",
        "name": "Cross-directory or project session",
        "purpose": "Validate canonical session id, aliases, working_dir, and dedupe.",
    },
    {
        "id": "interrupted_error",
        "name": "Interrupted or error session",
        "purpose": "Validate partial captures, loss reasons, and recoverability.",
    },
)


RAW_EVENT_FIELD_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "name": "source_agent",
        "source": "AgentSource.name or capture_turn.source_agent",
        "meaning": "Normalized source agent id such as codex or claude.",
        "requirement": "required",
        "missing_strategy": "reject record; downstream routing cannot recover this field",
    },
    {
        "name": "source_kind",
        "source": "SessionInfo.source_kind or source metadata",
        "meaning": "Native source format, for example jsonl, sqlite, trajectory, or mcp_turn.",
        "requirement": "required",
        "missing_strategy": "set to unknown and mark source_fidelity as partial",
    },
    {
        "name": "source_file",
        "source": "SessionInfo.source_path / Turn.source_files",
        "meaning": "Filesystem source file for file-backed sources.",
        "requirement": "conditional",
        "missing_strategy": "allowed only when source_db is present",
    },
    {
        "name": "source_db",
        "source": "SQLite-backed source metadata",
        "meaning": "Database path or logical DB source for sqlite-backed agents.",
        "requirement": "conditional",
        "missing_strategy": "allowed only when source_file is present",
    },
    {
        "name": "canonical_session_id",
        "source": "core.sync_framework.agent_source.canonicalize_session_info()",
        "meaning": "Stable session identity after path aliases and split files are merged.",
        "requirement": "required",
        "missing_strategy": "fallback to session_id and record dedupe_strategy=fallback_session_id",
    },
    {
        "name": "session_aliases",
        "source": "SessionInfo.session_aliases and source parser metadata",
        "meaning": "Known source-specific aliases and path variants for the same session.",
        "requirement": "optional",
        "missing_strategy": "empty list; dedupe remains based on canonical id and content hash",
    },
    {
        "name": "turn_id",
        "source": "native event id or derived source_agent:session:turn identifier",
        "meaning": "Stable turn/event id used as evidence reference.",
        "requirement": "required",
        "missing_strategy": "derive from source_agent, canonical_session_id, and turn_number",
    },
    {
        "name": "turn_number",
        "source": "Turn.turn_number or native message order",
        "meaning": "Zero- or one-based order as normalized by the source parser.",
        "requirement": "required",
        "missing_strategy": "reject record; ordering cannot be reconstructed reliably",
    },
    {
        "name": "role",
        "source": "native message role or paired Turn user/assistant side",
        "meaning": "Speaker role for raw event reconstruction.",
        "requirement": "required",
        "missing_strategy": "map paired Turn fields to user and assistant roles",
    },
    {
        "name": "content",
        "source": "native message content or Turn user_content/assistant_content",
        "meaning": "Raw visible content for this role/event.",
        "requirement": "required",
        "missing_strategy": "empty content is allowed only with explicit loss_reasons",
    },
    {
        "name": "visible_text",
        "source": "parser-extracted human-visible text",
        "meaning": "Text that can be read in raw vault projection and used for distillation.",
        "requirement": "required",
        "missing_strategy": (
            "mark completeness.visible_text=missing and do not distill automatically"
        ),
    },
    {
        "name": "tool_calls",
        "source": "native tool call blocks or Turn.tool_calls",
        "meaning": "Structured tool invocations with name, arguments, and ids when available.",
        "requirement": "required",
        "missing_strategy": "empty list plus completeness.tool_calls=unavailable",
    },
    {
        "name": "tool_results",
        "source": "native tool result blocks or Turn.tool_results",
        "meaning": "Structured tool outputs/results paired to calls when available.",
        "requirement": "required",
        "missing_strategy": "empty list plus completeness.tool_results=unavailable",
    },
    {
        "name": "reasoning_metadata",
        "source": "host-exposed thinking metadata or local artifact/summary references",
        "meaning": (
            "Reasoning evidence the host explicitly exposes; private chains are not required."
        ),
        "requirement": "required",
        "missing_strategy": (
            "empty string plus completeness.reasoning=unavailable and loss reason if expected"
        ),
    },
    {
        "name": "attachments",
        "source": "native attachment, media, file context, or raw_event_refs",
        "meaning": "Files, media, or context references visible to the agent.",
        "requirement": "required",
        "missing_strategy": "empty list plus completeness.attachments=unavailable",
    },
    {
        "name": "artifact_refs",
        "source": "CaptureService metadata.artifact_refs or source parser artifact references",
        "meaning": (
            "Hash-verifiable source references used to build a system-owned, "
            "path-free DistillInputSpec artifact catalog."
        ),
        "requirement": "conditional",
        "missing_strategy": (
            "empty list; if attachments/tool_results are present, "
            "mark completeness.artifact_refs=unavailable"
        ),
    },
    {
        "name": "created_at",
        "source": "native event timestamp or capture timestamp",
        "meaning": "Original event creation time when available.",
        "requirement": "required",
        "missing_strategy": "fallback to captured_at and mark timestamp_source=capture_time",
    },
    {
        "name": "updated_at",
        "source": "store write timestamp",
        "meaning": "Last normalized record update time.",
        "requirement": "required",
        "missing_strategy": "set at write time",
    },
    {
        "name": "working_dir",
        "source": "SessionInfo.working_dir or capture metadata",
        "meaning": "Project directory associated with the session.",
        "requirement": "conditional",
        "missing_strategy": "leave empty and avoid project-scoped assumptions",
    },
    {
        "name": "project",
        "source": "normalized working_dir/project metadata",
        "meaning": "Project key used by search and access-control filters.",
        "requirement": "conditional",
        "missing_strategy": "derive from working_dir basename or leave empty",
    },
    {
        "name": "content_hash",
        "source": "compute_raw_content_hash",
        "meaning": "Stable hash across text, tools, attachments, reasoning, and metadata.",
        "requirement": "required",
        "missing_strategy": "compute before storage; reject on failure",
    },
    {
        "name": "source_fidelity",
        "source": "AgentSource.completeness_capabilities and turn completeness",
        "meaning": "Capture fidelity enum: full, derived, experimental, or partial.",
        "requirement": "required",
        "missing_strategy": "set unknown/partial and block full-power status",
    },
    {
        "name": "compression",
        "source": "source capabilities plus RawEventStore compression column",
        "meaning": "Whether native source or Mnemos storage compressed the content.",
        "requirement": "required",
        "missing_strategy": "set unknown and keep raw bytes/count evidence",
    },
    {
        "name": "dedupe_strategy",
        "source": "source capabilities and SyncEngine dedupe logic",
        "meaning": "Canonical rule used to avoid duplicate ingestion.",
        "requirement": "required",
        "missing_strategy": "default to canonical_session_id+turn_number+content_hash",
    },
)


DISTILLED_KNOWLEDGE_FIELD_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "name": "title",
        "source": "KnowledgeExtractor fragment title",
        "meaning": "Human-readable knowledge page title.",
        "requirement": "required",
        "missing_strategy": "reject fragment and request correction",
    },
    {
        "name": "core_content",
        "source": "KnowledgeExtractor fragment core_content",
        "meaning": "Readable distilled content body with sufficient context.",
        "requirement": "required",
        "missing_strategy": "reject fragment and record distill_failed if correction fails",
    },
    {
        "name": "frontmatter",
        "source": "fragment frontmatter and wiki writer",
        "meaning": "YAML metadata envelope for the wiki page.",
        "requirement": "required",
        "missing_strategy": "reject fragment before vault write",
    },
    {
        "name": "摘要",
        "source": "frontmatter.摘要",
        "meaning": "Short Chinese summary used by search and human scanning.",
        "requirement": "required",
        "missing_strategy": "reject fragment and request correction",
    },
    {
        "name": "领域",
        "source": "frontmatter.领域",
        "meaning": "Knowledge domain used for routing and grouping.",
        "requirement": "required",
        "missing_strategy": "reject fragment and request correction",
    },
    {
        "name": "tags",
        "source": "fragment tags/keywords/frontmatter",
        "meaning": "Search and organization tags.",
        "requirement": "required",
        "missing_strategy": "empty list allowed only when entities/领域 are present",
    },
    {
        "name": "source_sessions",
        "source": "raw session ids and distillation context",
        "meaning": "Raw sessions that support the page.",
        "requirement": "required",
        "missing_strategy": "block write; distilled knowledge must be traceable",
    },
    {
        "name": "source_agent",
        "source": "raw event source_agent",
        "meaning": "Primary agent source for the distilled page.",
        "requirement": "required",
        "missing_strategy": "fallback to mixed only when multiple agents are explicit",
    },
    {
        "name": "cognition_extraction_context",
        "source": "DistillInputSpec v4 system-owned CognitionExtractionContext",
        "meaning": (
            "Immutable source agent/session/events, exact Raw spans, completeness/loss, "
            "catalog hashes, local-user ACL, purpose, and retention oracle."
        ),
        "requirement": "required before live extraction",
        "missing_strategy": "reject before model call, checkpoint admission, or formal write",
    },
    {
        "name": "cognition_episode",
        "source": "distill_output_v4 cognition_episode to mnemos.cognition_episode.v2",
        "meaning": (
            "Complete 19-field typed cognition chain plus immutable claims and behavior "
            "intent with exact evidence-bound known entries and explicit unknowns."
        ),
        "requirement": "required for every non-skip output",
        "missing_strategy": (
            "reject incomplete, forged, cross-chunk, assertive-unknown, or all-unknown "
            "grounding; commit canonical revision/event/outbox before downstream sinks"
        ),
    },
    {
        "name": "evidence_refs",
        "source": "distill_output_v4 claims.evidence and source_event_ids",
        "meaning": (
            "Raw event ids, short quotes, and optional system-resolved artifact_ref_id "
            "fields supporting claims."
        ),
        "requirement": "required",
        "missing_strategy": "block write unless distill_intent=skip",
    },
    {
        "name": "artifact_refs",
        "source": (
            "DistillInputSpec artifact catalog plus "
            "distill_output_v4 claims.evidence[].artifact_ref_id"
        ),
        "meaning": (
            "Model-selected opaque IDs resolved by the system to canonical content URI, "
            "type, SHA-256, MIME and local-user ACL."
        ),
        "requirement": "conditional",
        "missing_strategy": "allowed only when no multimodal/tool artifact was part of the supporting evidence",
    },
    {
        "name": "entities",
        "source": "fragment keywords/concepts and KG extraction",
        "meaning": "Entities available to KG, search, and reranker features.",
        "requirement": "required",
        "missing_strategy": "derive from keywords/concepts or leave empty with low confidence",
    },
    {
        "name": "relations",
        "source": "fragment relations and KG relation builder",
        "meaning": "Structured relationships to existing knowledge or entities.",
        "requirement": "required",
        "missing_strategy": "empty list allowed; hidden relation suggestion can fill later",
    },
    {
        "name": "confidence",
        "source": "distill_output_v4 claims.confidence and quality gates",
        "meaning": "Confidence score for claims and downstream ranking.",
        "requirement": "required",
        "missing_strategy": "default low confidence and route for review",
    },
    {
        "name": "embedding_status",
        "source": "embedding index build/update",
        "meaning": "Whether the page is pending, indexed, failed, or skipped for embeddings.",
        "requirement": "required",
        "missing_strategy": "set pending and enqueue embedding job",
    },
    {
        "name": "distill_status",
        "source": "distillation queue / sync_log / wiki writer",
        "meaning": "Distillation lifecycle status.",
        "requirement": "required",
        "missing_strategy": "set failed with error artifact rather than silent drop",
    },
)


DOWNSTREAM_LINK_FIELD_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "name": "kg_entity_refs",
        "source": "entities/frontmatter/KG event handler",
        "meaning": "Entity ids or names created from distilled knowledge.",
        "requirement": "conditional",
        "missing_strategy": "keep page searchable and mark KG update pending",
    },
    {
        "name": "kg_relation_refs",
        "source": "relations/distill_output_v4 relation_to_existing",
        "meaning": "Relation ids or typed relation payloads created for KG.",
        "requirement": "conditional",
        "missing_strategy": "empty list; relation suggestion can run later",
    },
    {
        "name": "embedding_ref",
        "source": "embedding index manager",
        "meaning": "Vector/index id for retrieval.",
        "requirement": "conditional",
        "missing_strategy": "set embedding_status=pending or failed",
    },
    {
        "name": "reranker_features",
        "source": "frontmatter, candidate_summary, persona_alignment, freshness",
        "meaning": "Fields consumed by context search and reranking.",
        "requirement": "conditional",
        "missing_strategy": "fallback to keyword/semantic score only",
    },
    {
        "name": "observation_refs",
        "source": "L3 observation store/events",
        "meaning": "Observation ids linked back to the page or raw evidence.",
        "requirement": "optional",
        "missing_strategy": "no observation bridge; page remains valid",
    },
    {
        "name": "persona_alignment",
        "source": "persona scoring/frontmatter",
        "meaning": "Personalization signal for context-aware search and behavior prompts.",
        "requirement": "optional",
        "missing_strategy": "persona score defaults to 0",
    },
)


def sample_type_ids() -> tuple[str, ...]:
    return tuple(sample["id"] for sample in SAMPLE_TYPES)


def field_names(contracts: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(str(item["name"]) for item in contracts)


def required_capability_names(agent: str) -> tuple[str, ...]:
    return required_cognitive_capabilities(agent)


def build_agent_acceptance_samples() -> dict[str, Any]:
    """Return the complete sample matrix for all target agents."""
    sample_ids = sample_type_ids()
    agents: dict[str, Any] = {}
    for agent in TARGET_AGENT_NAMES:
        agents[agent] = {
            "required_capabilities": list(required_capability_names(agent)),
            "samples": {
                sample_id: {
                    "fixture_ref": (
                        "tests/fixtures/agent_acceptance_samples/manifest.json"
                        f"#/agents/{agent}/samples/{sample_id}"
                    ),
                    "expected_raw_contract": "raw_event_contract.v1",
                    "expected_distilled_contract": "distilled_knowledge_contract.v1",
                }
                for sample_id in sample_ids
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "target_agents": list(TARGET_AGENT_NAMES),
        "sample_types": list(sample_ids),
        "agents": agents,
    }


def load_acceptance_manifest(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], load_json_value(path))


def validate_contracts() -> list[str]:
    """Validate static contract definitions and return human-readable errors."""
    errors: list[str] = []
    for label, contracts in (
        ("raw_event", RAW_EVENT_FIELD_CONTRACTS),
        ("distilled_knowledge", DISTILLED_KNOWLEDGE_FIELD_CONTRACTS),
        ("downstream_link", DOWNSTREAM_LINK_FIELD_CONTRACTS),
    ):
        seen: set[str] = set()
        for item in contracts:
            name = str(item.get("name", "")).strip()
            if not name:
                errors.append(f"{label}: field without name")
            if name in seen:
                errors.append(f"{label}: duplicate field {name}")
            seen.add(name)
            for required_key in ("source", "meaning", "requirement", "missing_strategy"):
                if not str(item.get(required_key, "")).strip():
                    errors.append(f"{label}.{name}: missing {required_key}")
    return errors


def validate_acceptance_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """Validate an acceptance sample manifest against the executable contract."""
    errors = validate_contracts()
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("manifest schema_version does not match acceptance contract")

    expected_agents = set(TARGET_AGENT_NAMES)
    actual_agents = set(manifest.get("target_agents") or [])
    if actual_agents != expected_agents:
        errors.append(
            f"target_agents mismatch: expected {sorted(expected_agents)}, "
            f"got {sorted(actual_agents)}"
        )

    expected_samples = set(sample_type_ids())
    actual_samples = set(manifest.get("sample_types") or [])
    if actual_samples != expected_samples:
        errors.append(
            f"sample_types mismatch: expected {sorted(expected_samples)}, "
            f"got {sorted(actual_samples)}"
        )

    agents = manifest.get("agents")
    if not isinstance(agents, Mapping):
        return errors + ["agents must be a mapping"]

    for agent in TARGET_AGENT_NAMES:
        agent_spec = agents.get(agent)
        if not isinstance(agent_spec, Mapping):
            errors.append(f"missing agent sample spec: {agent}")
            continue
        samples = agent_spec.get("samples")
        if not isinstance(samples, Mapping):
            errors.append(f"{agent}: samples must be a mapping")
            continue
        missing = expected_samples - set(samples)
        extra = set(samples) - expected_samples
        if missing:
            errors.append(f"{agent}: missing samples {sorted(missing)}")
        if extra:
            errors.append(f"{agent}: unknown samples {sorted(extra)}")
        for sample_id, sample_spec in samples.items():
            if not isinstance(sample_spec, Mapping):
                errors.append(f"{agent}.{sample_id}: sample spec must be a mapping")
                continue
            fixture_ref = str(sample_spec.get("fixture_ref", ""))
            if f"#/agents/{agent}/samples/{sample_id}" not in fixture_ref:
                errors.append(f"{agent}.{sample_id}: fixture_ref must point to its manifest entry")
            if sample_spec.get("expected_raw_contract") != "raw_event_contract.v1":
                errors.append(f"{agent}.{sample_id}: expected_raw_contract mismatch")
            if sample_spec.get("expected_distilled_contract") != "distilled_knowledge_contract.v1":
                errors.append(f"{agent}.{sample_id}: expected_distilled_contract mismatch")

    return errors


def validate_builtin_agent_capabilities() -> list[str]:
    """Validate passive source capabilities for target agents without requiring installation."""
    from core.sync_framework.registry import SourceRegistry

    errors: list[str] = []
    for agent in TARGET_AGENT_NAMES:
        source_class = SourceRegistry.get_builtin_source_class(agent)
        if source_class is None:
            errors.append(f"{agent}: no built-in AgentSource class")
            continue
        source = source_class()
        caps = dict(source.completeness_capabilities())
        if caps.get("source_fidelity") != "full":
            errors.append(
                f"{agent}: source_fidelity must be 'full', "
                f"got {caps.get('source_fidelity')!r}"
            )
        for capability in required_capability_names(agent):
            if capability == "source_fidelity":
                continue
            value = caps.get(capability)
            if value in (None, False, "", "unknown", "unavailable", "not_available"):
                errors.append(f"{agent}: required capability not declared: {capability}")
        for metadata_key in (
            "memory_scope",
            "host_memory_default",
            "host_memory_effect",
            "transcript_kind",
            "compression",
            "dedupe_strategy",
        ):
            if not str(caps.get(metadata_key, "")).strip() or caps.get(metadata_key) == "unknown":
                errors.append(f"{agent}: capability metadata missing or unknown: {metadata_key}")
    return errors
