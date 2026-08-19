# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from core.agent_kit.acceptance_contracts import (
    DISTILLED_KNOWLEDGE_FIELD_CONTRACTS,
    DOWNSTREAM_LINK_FIELD_CONTRACTS,
    RAW_EVENT_FIELD_CONTRACTS,
    build_agent_acceptance_samples,
    field_names,
    load_acceptance_manifest,
    validate_acceptance_manifest,
    validate_builtin_agent_capabilities,
    validate_contracts,
)
from core.agent_kit.protocol import TARGET_AGENT_NAMES, required_cognitive_capabilities


MANIFEST = Path("tests/fixtures/agent_acceptance_samples/manifest.json")


def test_acceptance_contract_definitions_have_required_metadata():
    assert validate_contracts() == []


def test_raw_event_contract_covers_roadmap_fields():
    fields = set(field_names(RAW_EVENT_FIELD_CONTRACTS))

    assert {
        "source_agent",
        "source_kind",
        "source_file",
        "source_db",
        "canonical_session_id",
        "session_aliases",
        "turn_id",
        "turn_number",
        "role",
        "content",
        "visible_text",
        "tool_calls",
        "tool_results",
        "reasoning_metadata",
        "attachments",
        "artifact_refs",
        "created_at",
        "updated_at",
        "working_dir",
        "project",
        "content_hash",
        "source_fidelity",
        "compression",
        "dedupe_strategy",
    } <= fields


def test_distilled_knowledge_contract_covers_roadmap_fields():
    fields = set(field_names(DISTILLED_KNOWLEDGE_FIELD_CONTRACTS))

    assert {
        "title",
        "core_content",
        "frontmatter",
        "摘要",
        "领域",
        "tags",
        "source_sessions",
        "source_agent",
        "cognition_extraction_context",
        "cognition_episode",
        "evidence_refs",
        "artifact_refs",
        "entities",
        "relations",
        "confidence",
        "embedding_status",
        "distill_status",
    } <= fields


def test_downstream_contract_covers_kg_embedding_reranker_observation_persona():
    fields = set(field_names(DOWNSTREAM_LINK_FIELD_CONTRACTS))

    assert {
        "kg_entity_refs",
        "kg_relation_refs",
        "embedding_ref",
        "reranker_features",
        "observation_refs",
        "persona_alignment",
    } <= fields


def test_agent_acceptance_manifest_matches_generated_contract():
    manifest = load_acceptance_manifest(MANIFEST)

    assert validate_acceptance_manifest(manifest) == []
    assert manifest == build_agent_acceptance_samples()


def test_each_agent_has_all_samples_and_expected_capabilities():
    manifest = load_acceptance_manifest(MANIFEST)

    assert tuple(manifest["target_agents"]) == TARGET_AGENT_NAMES
    for agent in TARGET_AGENT_NAMES:
        spec = manifest["agents"][agent]
        assert tuple(spec["required_capabilities"]) == required_cognitive_capabilities(agent)
        assert set(spec["samples"]) == set(manifest["sample_types"])


def test_target_builtin_sources_match_acceptance_capability_contract():
    assert validate_builtin_agent_capabilities() == []
