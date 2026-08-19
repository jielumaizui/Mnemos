# -*- coding: utf-8 -*-
from __future__ import annotations

from copy import deepcopy
import hashlib


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _detached_evidence(
    quote="连接池上限过低且缺少超时监控",
    *,
    source_event_id="raw-1",
):
    content_hash = _sha256(quote)
    return {
        "source_event_id": source_event_id,
        "source_authority_id": "source-authority:" + "1" * 32,
        "quote": quote,
        "source_authority": "explicit_user",
        "authority_purpose": "authoritative_instruction_or_user_statement",
        "authority_allows_cognitive_update": True,
        "authority_content_sha256": content_hash,
        "authority_role": "user",
        "authority_span_start": 0,
        "authority_span_end": len(quote),
        "authority_span_status": "exact",
        "authority_source_revision_sha256": content_hash,
        "authority_artifact_ref_id": "",
    }


def _cognition_episode(evidence):
    from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS

    known = {"situation", "facts", "scope"}
    return {
        field: [
            {
                "status": "known",
                "value": f"{field} 的可验证认知内容",
                "evidence_refs": [deepcopy(evidence)],
                "claim_ids": ["claim-1"],
            }
            if field in known
            else {
                "status": "unknown",
                "reason": f"输入没有提供 {field} 的可靠证据。",
                "evidence_refs": [],
                "claim_ids": [],
            }
        ]
        for field in COGNITION_EPISODE_FIELDS
    }


def _base_contract(**overrides):
    payload = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": "sha256:test-input-spec",
        "cognition_context_hash": "sha256:" + "0" * 64,
        "gate_decision_id": "gate-session-1",
        "source_agent": "codex",
        "source_session_id": "session-1",
        "source_event_ids": ["raw-1", "raw-2"],
        "raw_completeness": "full",
        "distill_intent": "create",
        "candidate_summary": "Redis 连接池耗尽的排查经验。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    "source_event_id": "raw-1",
                    "quote": "帮我判断 Redis 连接池耗尽的根因。",
                    "reason": "用户明确要求判断根因。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.72,
            "intent_status": "unverified",
            "behavior_summary": "用户想让系统判断 Redis 连接池耗尽的原因。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "Redis 连接池耗尽通常和连接上限过低、超时配置缺失有关。",
                "claim_type": "technical_fact",
                "scope": {
                    "domain": "backend",
                    "applies_to": ["高并发 Redis 服务"],
                    "not_applies_to": ["单机低并发脚本"],
                },
                "evidence": [_detached_evidence()],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "当前仓库没有同等知识。",
                },
                "recommended_action": "create_page",
                "confidence": 0.86,
            }
        ],
        "cognition_episode": _cognition_episode(_detached_evidence()),
    }
    payload.update(overrides)
    return payload


def _input_spec(
    *,
    source_agent="codex",
    session_id="session-1",
    text="test input",
    artifact_refs=(),
):
    from core.hephaestus.distill_input_spec import DistillInputSpec

    source_content = (
        "帮我判断 Redis 连接池耗尽的根因。\n"
        "连接池上限过低且缺少超时监控"
    )

    return DistillInputSpec.build(
        source_agent=source_agent,
        source_session_id=session_id,
        source_event_ids=["raw-1", "raw-2"],
        raw_completeness="full",
        visible_input=text,
        input_mode="standard",
        artifact_refs=artifact_refs,
        source_messages=(
            {
                "role": "user",
                "content": source_content,
                "source_span": {
                    "revision_id": "raw-1",
                    "content_hash": _sha256(source_content),
                    "span_start": 0,
                    "span_end": len(source_content),
                    "role": "user",
                },
            },
        ),
    )


def _bound_contract(*, text="test input", artifact_refs=(), **overrides):
    spec = _input_spec(text=text, artifact_refs=artifact_refs)
    payload = _base_contract(
        input_spec_hash=spec.input_spec_hash,
        gate_decision_id=spec.gate_decision_id,
        source_agent=spec.source_agent,
        source_session_id=spec.source_session_id,
        source_event_ids=list(spec.source_event_ids),
        raw_completeness=spec.raw_completeness,
        cognition_context_hash=spec.cognition_context.context_hash,
    )
    authority = spec.source_authority_catalog.entries[0]
    evidence = {
        "source_event_id": authority.source_event_id,
        "source_authority_id": authority.source_authority_id,
        "quote": "连接池上限过低且缺少超时监控",
        **authority.resolved_evidence_payload(),
    }
    payload["claims"][0]["evidence"] = [deepcopy(evidence)]
    payload["cognition_episode"] = _cognition_episode(evidence)
    payload.update(overrides)
    return payload, spec


def _typed_skip(*, text="test input", **overrides):
    structured, spec = _bound_contract(
        text=text,
        distill_intent="skip",
        candidate_summary="输入没有可长期复用的知识。",
        skip_reason="内容只包含寒暄，没有可复用的结论。",
        no_value_evidence=[
            {"source_event_id": "raw-1", "reason": "该事件只有寒暄和一次性问候。"}
        ],
        claims=[],
    )
    structured.pop("user_behavior_intent")
    structured.pop("cognition_episode")
    structured.update(overrides)
    return {
        "judgment": "skip",
        "judgment_reason": "输入不具备长期认知价值。",
        "structured_output": structured,
        "fragments": [],
    }, spec


def _typed_knowledge(*, text="test input", artifact_refs=(), **overrides):
    """Complete non-skip root payload for schema/runtime parity probes."""
    structured, spec = _bound_contract(text=text, artifact_refs=artifact_refs)
    structured.update(overrides)
    return {
        "judgment": "knowledge",
        "judgment_reason": "输入包含可长期复用的知识。",
        "structured_output": structured,
        "fragments": [
            {
                "form": "经验法则",
                "title": "Redis 连接池排查经验完整结论",
                "frontmatter": {"摘要": "Redis 连接池排查的可复用经验。", "领域": "后端"},
                "core_content": "## Redis 连接池排查结论\n\n"
                + "先收集连接上限、超时和泄漏证据，再根据监控结果调整配置。" * 5,
                "claim_ids": ["claim-1"],
            }
        ],
    }, spec


def test_distill_output_contract_accepts_valid_create_payload():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    result = validate_distill_output_contract(_base_contract())

    assert result.valid is True
    assert result.errors == []


def test_extraction_contract_accepts_minimal_typed_skip_without_correction_fields():
    from core.hephaestus.distillation_contract import validate_extraction_output

    payload, spec = _typed_skip()
    result = validate_extraction_output(payload, spec)

    assert result.valid is True
    assert result.is_skip is True
    assert "missing_claims" not in result.error_text


def test_extraction_contract_rejects_incomplete_or_mixed_skip():
    from core.hephaestus.distillation_contract import validate_extraction_output

    payload, spec = _typed_skip()
    payload["structured_output"].pop("no_value_evidence")
    payload["structured_output"]["claims"] = [_base_contract()["claims"][0]]

    result = validate_extraction_output(payload, spec)

    assert result.valid is False
    assert "no_value_evidence" in result.error_text
    assert "empty claims list" in result.error_text


def test_extraction_contract_rejects_non_skip_empty_fragments_and_identity_drift():
    from core.hephaestus.distillation_contract import validate_extraction_output

    structured, spec = _bound_contract()
    structured["source_agent"] = "forged-agent"
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "看似知识，但没有片段。",
        "structured_output": structured,
        "fragments": [],
    }

    result = validate_extraction_output(payload, spec)

    assert result.valid is False
    assert "immutable distillation input spec" in result.error_text
    assert "must contain at least one fragment" in result.error_text


def test_canonical_extraction_root_is_an_immutable_snapshot():
    """Later enrichment must not mutate a root whose admitted hash is retained."""
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )

    payload, spec = _typed_knowledge()
    root = canonicalize_extraction_output(payload, payload["fragments"])
    admitted_hash = canonical_extraction_output_hash(canonical_output=root)

    payload["structured_output"]["claims"][0]["claim_text"] = "后续工作副本发生变化"
    payload["fragments"][0]["frontmatter"]["摘要"] = "后续质量标注"

    assert root["structured_output"]["claims"][0]["claim_text"] != "后续工作副本发生变化"
    assert root["fragments"][0]["frontmatter"]["摘要"] != "后续质量标注"
    assert canonical_extraction_output_hash(canonical_output=root) == admitted_hash
    assert validate_extraction_output(root, spec).valid is True


def _artifact_source_ref(*, artifact_type="tool_result"):
    from core.evidence.artifact_capture import build_capture_artifact_refs

    assert artifact_type == "tool_result"
    return build_capture_artifact_refs(
        source_agent="codex",
        session_id="session-1",
        turn_number=2,
        source_event_id="raw-1",
        tool_results=(
            {"tool_name": "pytest", "result": "pytest failure output"},
        ),
    )[0]


def _resolved_artifact_knowledge():
    from core.evidence.artifact_catalog import resolve_model_artifact_selections

    payload, spec = _typed_knowledge(artifact_refs=(_artifact_source_ref(),))
    payload["structured_output"]["claims"][0]["evidence"][0][
        "artifact_ref_id"
    ] = spec.artifact_catalog.entries[0].artifact_ref_id
    resolution = resolve_model_artifact_selections(payload, spec.artifact_catalog)
    assert resolution.valid is True
    return resolution.payload, spec


def test_model_schema_accepts_ref_selection_and_runtime_requires_resolution():
    from core.hephaestus.distillation_contract import (
        canonical_model_output_projection,
        canonical_output_validator,
        validate_extraction_output,
    )

    payload, spec = _typed_knowledge(artifact_refs=(_artifact_source_ref(),))
    payload["structured_output"]["claims"][0]["evidence"][0][
        "artifact_ref_id"
    ] = spec.artifact_catalog.entries[0].artifact_ref_id

    schema_errors = list(
        canonical_output_validator().iter_errors(canonical_model_output_projection(payload))
    )
    runtime = validate_extraction_output(payload, spec)

    assert schema_errors == []
    assert runtime.valid is False
    assert "resolved by the system" in runtime.error_text


def test_canonical_schema_and_runtime_share_behavior_conditionals():
    from core.hephaestus.distillation_contract import (
        canonical_model_output_projection,
        canonical_output_validator,
        validate_extraction_output,
    )

    external_payload, spec = _typed_knowledge()
    external_behavior = external_payload["structured_output"]["user_behavior_intent"]
    external_behavior.update(
        {
            "content_source": "external_file",
            "user_intent_signal": "curate_or_decision_material",
            "intent_hypothesis": "seeking_summary",
            "intent_confidence": 0.6,
        }
    )
    unknown_payload = deepcopy(external_payload)
    unknown_behavior = unknown_payload["structured_output"]["user_behavior_intent"]
    unknown_behavior.update(
        {
            "content_source": "native_dialogue",
            "user_intent_signal": "unknown",
            "intent_hypothesis": "unknown",
            "intent_status": "unverified",
            "intent_confidence": 0.8,
        }
    )

    assert list(
        canonical_output_validator().iter_errors(
            canonical_model_output_projection(external_payload)
        )
    ) == []
    assert validate_extraction_output(external_payload, spec).valid is True
    assert list(
        canonical_output_validator().iter_errors(
            canonical_model_output_projection(unknown_payload)
        )
    )
    assert validate_extraction_output(unknown_payload, spec).valid is False


def test_runtime_revalidates_resolved_behavior_intent_authority_fields():
    from core.cognition_episode_contract import iter_cognition_episode_evidence
    from core.evidence.source_authority import (
        resolve_model_source_authority_selections,
    )
    from core.hephaestus.distill_input_spec import DistillInputSpec
    from core.hephaestus.distillation_contract import (
        canonical_model_output_projection,
        validate_extraction_output,
    )

    payload, _ = _typed_knowledge()
    payload = canonical_model_output_projection(payload)
    structured = payload["structured_output"]
    intent_quote = structured["user_behavior_intent"]["intent_evidence"][0]["quote"]
    claim_quote = structured["claims"][0]["evidence"][0]["quote"]
    visible_input = f"{intent_quote}\n{claim_quote}"
    spec = DistillInputSpec.build(
        source_agent="codex",
        source_session_id="session-1",
        source_event_ids=("raw-1",),
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        source_messages=[
            {
                "role": "user",
                "content": visible_input,
                "source_span": {
                    "revision_id": "raw-1",
                    "content_hash": _sha256(visible_input),
                    "span_start": 0,
                    "span_end": len(visible_input),
                    "role": "user",
                },
            }
        ],
    )
    structured.update(
        {
            "input_spec_hash": spec.input_spec_hash,
            "gate_decision_id": spec.gate_decision_id,
            "source_agent": spec.source_agent,
            "source_session_id": spec.source_session_id,
            "source_event_ids": list(spec.source_event_ids),
            "raw_completeness": spec.raw_completeness,
            "cognition_context_hash": spec.cognition_context.context_hash,
        }
    )
    for evidence in structured["claims"][0]["evidence"]:
        evidence.pop("source_authority_id", None)
    for _, evidence in iter_cognition_episode_evidence(payload):
        evidence.pop("source_authority_id", None)
    resolution = resolve_model_source_authority_selections(
        payload,
        spec.source_authority_catalog,
    )
    assert resolution.issues == ()
    assert validate_extraction_output(resolution.payload, spec).valid is True

    tampered = deepcopy(resolution.payload)
    tampered["structured_output"]["user_behavior_intent"]["intent_evidence"][0][
        "source_authority"
    ] = "external_content"
    result = validate_extraction_output(tampered, spec)

    assert result.valid is False
    assert "must match the immutable source authority catalog" in result.error_text


def test_canonical_schema_and_runtime_share_claim_relation_and_action_conditions():
    from core.hephaestus.distillation_contract import (
        canonical_output_validator,
        validate_extraction_output,
    )

    payload, spec = _typed_knowledge()
    claim = payload["structured_output"]["claims"][0]
    claim.update(
        {
            "claim_type": "decision",
            "relation_to_existing": {"type": "contradicts"},
            "recommended_action": "create_page",
        }
    )

    schema_errors = list(canonical_output_validator().iter_errors(payload))
    runtime = validate_extraction_output(payload, spec)

    assert schema_errors
    assert runtime.valid is False
    assert "target_pages" in runtime.error_text
    assert "cognitive_actions" in runtime.error_text
    assert "route_to_dispute" in runtime.error_text


def test_release_contract_audit_passes_and_rejects_disabled_enforcement():
    from scripts import audit_distill_output_contract as audit
    from core.hephaestus.distill_output_version import DISTILL_OUTPUT_CONTRACT_VERSION

    class _Config:
        def __init__(self, values):
            self.values = values

        def get(self, key, default=None):
            return self.values.get(key, default)

    enabled = _Config(
        {
            "distill.structured_output_contract.enforce": True,
            "distill.action_router.enabled": True,
        }
    )
    enabled_report = audit.audit(enabled)
    assert enabled_report["ok"] is True
    assert enabled_report["artifact_identity"] == {
        "scope": "static_contract_and_synthetic_validation",
        "catalog_denominator": 1,
        "hash_verifiable_count": 1,
        "artifact_ref_mismatch": 0,
        "model_outputs_canonical_uri": False,
        "forged_ref_rejected": True,
        "ok": True,
    }
    assert enabled_report["cognition_episode_golden_corpus"] == {
        "schema_version": "mnemos.cognition_episode_golden_corpus.v1",
        "corpus_path": "tests/fixtures/cognition_episode_golden/manifest.json",
        "case_count": 4,
        "eligible_non_skip_count": 3,
        "typed_skip_count": 1,
        "required_field_total": 57,
        "required_field_valid": 57,
        "required_field_rate": 1.0,
        "exact_span_mismatch_count": 0,
        "context_mismatch_count": 0,
        "typed_nonassertive_violation_count": 0,
        "forged_ref_rejected": True,
        "cross_chunk_ref_rejected": True,
        "all_unknown_rejected": True,
        "ok": True,
    }
    assert audit.SCHEMA_VERSION == DISTILL_OUTPUT_CONTRACT_VERSION

    disabled = _Config(
        {
            "distill.structured_output_contract.enforce": False,
            "distill.action_router.enabled": False,
        }
    )
    report = audit.audit(disabled)
    assert report["ok"] is False
    assert report["contract_drift_count"] == 0
    assert report["release_enforcement_errors"] == [
        "release profile requires distill.structured_output_contract.enforce=true",
        "release profile requires distill.action_router.enabled=true",
    ]


def test_release_contract_audit_rejects_semantic_prompt_render_drift(monkeypatch):
    """A field list alone is not enough: skip/knowledge bounds must be rendered."""
    from scripts import audit_distill_output_contract as audit

    original_render_schema = audit.TemplateRegistry.render_schema

    def _missing_skip_claim_bound(self, schema_name):
        return original_render_schema(self, schema_name).replace(
            "**claims** (`array`; 最多项数：0)",
            "",
            1,
        )

    monkeypatch.setattr(
        audit.TemplateRegistry,
        "render_schema",
        _missing_skip_claim_bound,
    )

    errors = audit.validate_contract_sync()

    assert "prompt schema rendering drift: missing **claims** (`array`; 最多项数：0)" in errors


def test_distill_output_contract_accepts_system_resolved_artifact_evidence():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    root, spec = _resolved_artifact_knowledge()
    payload = root["structured_output"]

    result = validate_distill_output_contract(payload, input_spec=spec)

    assert result.valid is True


def test_model_artifact_identity_fields_are_rejected_before_runtime_validation():
    from core.evidence.artifact_catalog import resolve_model_artifact_selections

    payload, spec = _typed_knowledge(artifact_refs=(_artifact_source_ref(),))
    payload["structured_output"]["claims"][0]["evidence"][0].update(
        {
            "artifact_ref_id": spec.artifact_catalog.entries[0].artifact_ref_id,
            "artifact_uri": "file:///tmp/test-report.txt",
            "artifact_type": "tool_result",
            "artifact_summary": "pytest failure output",
        }
    )

    result = resolve_model_artifact_selections(payload, spec.artifact_catalog)

    assert result.valid is False
    assert result.issues[0].code == "model_owned_artifact_identity"


def test_distill_output_contract_rejects_tampered_system_artifact_summary():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    root, spec = _resolved_artifact_knowledge()
    payload = root["structured_output"]
    payload["claims"][0]["evidence"][0]["artifact_summary"] = "tampered"

    result = validate_distill_output_contract(payload, input_spec=spec)

    assert result.valid is False
    assert "artifact_summary must match the immutable artifact catalog" in result.error_text


def test_distill_output_contract_rejects_resolved_artifact_uri_type_mismatch():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    root, spec = _resolved_artifact_knowledge()
    payload = root["structured_output"]
    payload["claims"][0]["evidence"][0]["artifact_type"] = "screenshot"

    result = validate_distill_output_contract(payload, input_spec=spec)

    assert result.valid is False
    assert "artifact_type must match the canonical artifact URI" in result.error_text


def test_distill_output_contract_rejects_missing_gate_and_sources():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    payload = _base_contract(gate_decision_id="", source_event_ids=[])
    result = validate_distill_output_contract(payload)

    assert result.valid is False
    assert "gate_decision_id" in result.error_text
    assert "source_event_ids" in result.error_text


def test_distill_output_contract_routes_conflict_to_dispute():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    payload = _base_contract()
    payload["distill_intent"] = "dispute"
    payload["claims"][0]["relation_to_existing"] = {
        "type": "contradicts",
        "target_pages": ["03-Tech/redis-连接池.md"],
        "delta_text": "新证据认为连接泄漏比上限过低更关键。",
        "reason": "和既有结论的因果权重冲突。",
    }
    payload["claims"][0]["recommended_action"] = "route_to_dispute"

    result = validate_distill_output_contract(payload)

    assert result.valid is True
    assert result.requires_dispute is True


def test_distill_output_contract_rejects_conflict_without_dispute_route():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    payload = _base_contract()
    payload["claims"][0]["relation_to_existing"] = {
        "type": "contradicts",
        "target_pages": ["03-Tech/redis-连接池.md"],
        "delta_text": "新证据认为连接泄漏比上限过低更关键。",
        "reason": "和既有结论的因果权重冲突。",
    }
    payload["claims"][0]["recommended_action"] = "create_page"

    result = validate_distill_output_contract(payload)

    assert result.valid is False
    assert "route_to_dispute" in result.error_text


def test_distill_output_contract_allows_only_exact_duplicate_reinforcement():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    payload = _base_contract()
    payload["claims"][0]["relation_to_existing"] = {
        "type": "same",
        "target_pages": ["03-Tech/redis-连接池.md"],
        "delta_text": "",
        "reason": "100% duplicate after canonical comparison.",
    }
    payload["claims"][0]["recommended_action"] = "record_reinforcement"

    result = validate_distill_output_contract(payload)

    assert result.valid is True
    assert result.reinforcement_only is True


def test_distill_output_contract_rejects_non_exact_duplicate_without_delta():
    from core.hephaestus.distillation_contract import validate_distill_output_contract

    payload = _base_contract()
    payload["claims"][0]["relation_to_existing"] = {
        "type": "extends",
        "target_pages": ["03-Tech/redis-连接池.md"],
        "delta_text": "",
        "reason": "只有一句新补充，但没有输出 delta。",
    }
    payload["claims"][0]["recommended_action"] = "merge_into_page"

    result = validate_distill_output_contract(payload)

    assert result.valid is False
    assert "delta_text" in result.error_text
