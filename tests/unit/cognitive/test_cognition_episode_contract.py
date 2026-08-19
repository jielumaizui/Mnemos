from __future__ import annotations

import hashlib
from copy import deepcopy

EPISODE_FIELDS = (
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


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _input_spec():
    from core.hephaestus.distill_input_spec import DistillInputSpec

    content = (
        "当前 Redis 连接池频繁耗尽。用户目标是先确认根因。"
        "已知连接上限过低且缺少超时监控。"
        "用户决定先增加连接上限，并立即补充超时监控。"
        "该结论仅适用于高并发 Redis 服务。"
    )
    message = {
        "role": "user",
        "content": content,
        "source_span": {
            "revision_id": "rawrev-1",
            "logical_event_id": "raw-event-1",
            "turn_number": 1,
            "content_hash": _sha256(content),
            "span_start": 0,
            "span_end": len(content),
            "role": "user",
        },
    }
    return DistillInputSpec.build(
        source_agent="codex",
        source_session_id="session-1",
        source_event_ids=("rawrev-1",),
        raw_completeness="full",
        visible_input=content,
        input_mode="standard",
        source_messages=(message,),
    )


def _evidence(spec, quote: str) -> dict[str, str]:
    return {
        "source_event_id": "rawrev-1",
        "source_authority_id": spec.source_authority_catalog.entries[0].source_authority_id,
        "quote": quote,
    }


def _episode(spec) -> dict:
    known = {
        "situation": "当前 Redis 连接池频繁耗尽。",
        "goal": "用户目标是先确认根因。",
        "desired_state": "连接池不再因容量和超时配置耗尽。",
        "facts": "已知连接上限过低且缺少超时监控。",
        "decision": "用户决定先增加连接上限。",
        "rationale": "现有证据直接指向容量和超时配置。",
        "actions": "立即补充超时监控。",
        "scope": "该结论仅适用于高并发 Redis 服务。",
    }
    result = {}
    exact_quotes = {
        "situation": "当前 Redis 连接池频繁耗尽。",
        "goal": "用户目标是先确认根因。",
        "facts": "已知连接上限过低且缺少超时监控。",
        "decision": "用户决定先增加连接上限",
        "actions": "立即补充超时监控。",
        "scope": "该结论仅适用于高并发 Redis 服务。",
    }
    for field in EPISODE_FIELDS:
        if field in known:
            quote = exact_quotes.get(
                field,
                "已知连接上限过低且缺少超时监控。",
            )
            result[field] = [
                {
                    "status": "known",
                    "value": known[field],
                    "evidence_refs": [_evidence(spec, quote)],
                    "claim_ids": ["claim-1"],
                }
            ]
        else:
            result[field] = [
                {
                    "status": "unknown",
                    "reason": f"输入没有提供 {field} 的可靠证据。",
                    "evidence_refs": [],
                    "claim_ids": [],
                }
            ]
    return result


def _root(spec, *, include_episode: bool) -> dict:
    structured = {
        "schema_version": "distill_output_v4",
        **spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "Redis 连接池耗尽的根因和处置决策。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "用户希望确认连接池耗尽根因。",
            "intent_evidence": [
                {
                    "source_event_id": "rawrev-1",
                    "quote": "用户目标是先确认根因。",
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.9,
            "intent_status": "verified",
            "behavior_summary": "用户要求确认根因并记录决定。",
        },
        "claims": [
            {
                "claim_id": "claim-1",
                "claim_text": "连接上限过低且缺少超时监控会导致连接池耗尽。",
                "claim_type": "technical_fact",
                "scope": {
                    "domain": "backend",
                    "applies_to": ["高并发 Redis 服务"],
                    "not_applies_to": [],
                },
                "evidence": [_evidence(spec, "已知连接上限过低且缺少超时监控。")],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "新的故障证据。",
                },
                "recommended_action": "create_page",
                "confidence": 0.9,
            }
        ],
    }
    if include_episode:
        structured["cognition_episode"] = _episode(spec)
    return {
        "judgment": "knowledge",
        "judgment_reason": "存在可复用的事实和决策。",
        "structured_output": structured,
        "fragments": [
            {
                "form": "问题-解决",
                "title": "Redis 连接池耗尽根因与处置决策记录",
                "frontmatter": {"摘要": "连接池耗尽根因和处置决策。", "领域": "后端"},
                "core_content": "## Redis 连接池耗尽\n\n"
                + "连接上限过低且缺少超时监控，应提高上限并补充监控。" * 5,
                "claim_ids": ["claim-1"],
            }
        ],
    }


def _resolve(root, spec):
    from core.evidence.artifact_catalog import resolve_model_artifact_selections
    from core.evidence.source_authority import resolve_model_source_authority_selections

    artifacts = resolve_model_artifact_selections(root, spec.artifact_catalog)
    authorities = resolve_model_source_authority_selections(
        artifacts.payload,
        spec.source_authority_catalog,
    )
    assert artifacts.issues == ()
    assert authorities.issues == ()
    return authorities.payload


def test_non_skip_contract_requires_and_accepts_complete_cognition_episode():
    from core.hephaestus.distillation_contract import validate_extraction_output

    spec = _input_spec()
    missing = validate_extraction_output(_resolve(_root(spec, include_episode=False), spec), spec)
    complete = validate_extraction_output(_resolve(_root(spec, include_episode=True), spec), spec)

    assert missing.valid is False
    assert "cognition_episode" in missing.error_text
    assert complete.valid is True, complete.error_text


def test_episode_rejects_forged_cross_chunk_and_assertive_unknown_entries():
    from core.evidence.source_authority import resolve_model_source_authority_selections
    from core.hephaestus.distillation_contract import validate_extraction_output

    spec = _input_spec()
    model_root = _root(spec, include_episode=True)

    forged = deepcopy(model_root)
    forged["structured_output"]["cognition_episode"]["facts"][0]["evidence_refs"][0][
        "source_authority_id"
    ] = ("source-authority:" + "f" * 32)
    forged_resolution = resolve_model_source_authority_selections(
        forged,
        spec.source_authority_catalog,
    )
    assert forged_resolution.issues

    cross_chunk = deepcopy(model_root)
    cross_chunk["structured_output"]["cognition_episode"]["facts"][0]["evidence_refs"][0][
        "source_event_id"
    ] = "rawrev-outside-current-chunk"
    cross_chunk_resolution = resolve_model_source_authority_selections(
        cross_chunk,
        spec.source_authority_catalog,
    )
    assert cross_chunk_resolution.issues

    assertive_unknown = _resolve(deepcopy(model_root), spec)
    assertive_unknown["structured_output"]["cognition_episode"]["hypotheses"][0][
        "value"
    ] = "没有证据的假设不得伪装成 unknown。"
    validation = validate_extraction_output(assertive_unknown, spec)
    assert validation.valid is False
    assert "unknown/not_applicable" in validation.error_text


def test_non_skip_episode_rejects_all_unknown_required_grounding_fields():
    from core.hephaestus.distillation_contract import validate_extraction_output

    spec = _input_spec()
    root = _resolve(_root(spec, include_episode=True), spec)
    for field_name in ("situation", "facts", "scope"):
        root["structured_output"]["cognition_episode"][field_name] = [
            {
                "status": "unknown",
                "reason": "尝试用 unknown 绕过非 skip 的最低事实约束。",
                "evidence_refs": [],
                "claim_ids": [],
            }
        ]

    validation = validate_extraction_output(root, spec)

    assert validation.valid is False
    assert "must contain at least one exact, evidence-bound known entry" in (validation.error_text)


def test_non_skip_episode_rejects_duplicate_typed_entries_before_persistence():
    from core.hephaestus.distillation_contract import validate_extraction_output

    spec = _input_spec()
    root = _resolve(_root(spec, include_episode=True), spec)
    duplicate = deepcopy(root["structured_output"]["cognition_episode"]["facts"][0])
    root["structured_output"]["cognition_episode"]["facts"].append(duplicate)

    validation = validate_extraction_output(root, spec)

    assert validation.valid is False
    assert "duplicate typed entry" in validation.error_text


def test_canonical_episode_commit_is_atomic_typed_and_idempotent(tmp_path):
    from types import SimpleNamespace

    from core.cognitive.cognition_episode_persistence import commit_cognition_episode
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import DistillationResult

    spec = _input_spec()
    root = _resolve(_root(spec, include_episode=True), spec)
    admission = validate_extraction_output(root, spec)
    assert admission.valid, admission.error_text
    root_hash = canonical_extraction_output_hash(canonical_output=root)
    result = DistillationResult(
        session_id="session-1",
        judgment="knowledge",
        structured_output=root["structured_output"],
        input_spec=spec,
        extraction_judgment="knowledge",
        extraction_contract_valid=True,
        extraction_output=root,
        extraction_output_hash=root_hash,
        source="codex",
    )
    config = SimpleNamespace(database_dir=tmp_path)
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")

    first = commit_cognition_episode(result, config)
    second = commit_cognition_episode(result, config)

    assert first.status == "committed"
    assert second.status == "existing"
    assert second.revision_id == first.revision_id
    assert second.event_id == first.event_id
    assert len(first.outbox_ids) == 3
    assert first.consumer_ids == ("wiki", "knowledge_graph", "cognitive_graph")

    revision = CognitiveStateStore(config).current_revision(
        "cognition_episode",
        first.object_id,
    )
    assert revision is not None
    assert revision.revision_id == first.revision_id
    payload = dict(revision.payload)
    assert payload["schema_version"] == "mnemos.cognition_episode.v2"
    assert payload["cognition_context_hash"] == spec.cognition_context.context_hash
    assert payload["input_spec_hash"] == spec.input_spec_hash
    assert payload["extraction_output_hash"] == root_hash
    assert payload["source_agent"] == "codex"
    assert payload["source_session_id"] == "session-1"
    assert payload["source_event_ids"] == ["rawrev-1"]
    assert payload["raw_completeness"] == "full"
    assert payload["loss_contract"] == "lossless-visible-v1"
    assert payload["artifact_catalog_hash"] == spec.artifact_catalog.catalog_hash
    assert payload["source_authority_catalog_hash"] == spec.source_authority_catalog.catalog_hash
    assert payload["acl"] == "local_user"
    assert payload["retention_policy"] == "inherit_source"
    assert payload["source_spans"][0]["revision_id"] == "rawrev-1"
    assert payload["source_spans"][0]["span_status"] == "exact"
    assert payload["facts"][0]["entry_id"].startswith("cogentry-")
    assert payload["facts"][0]["evidence_refs"][0]["authority_span_status"] == "exact"
    assert payload["claims"] == root["structured_output"]["claims"]
    assert payload["claims"][0]["claim_text"] == ("连接上限过低且缺少超时监控会导致连接池耗尽。")
    assert payload["claims"][0]["claim_type"] == "technical_fact"
    assert payload["claims"][0]["scope"]["domain"] == "backend"
    assert payload["claims"][0]["relation_to_existing"]["type"] == "new"
    assert payload["claims"][0]["recommended_action"] == "create_page"
    assert payload["claims"][0]["confidence"] == 0.9
    assert payload["claim_catalog_hash"].startswith("sha256:")
    assert payload["user_behavior_intent"] == root["structured_output"]["user_behavior_intent"]
    assert revision.evidence_refs


def _write_result(spec, root):
    from core.hephaestus.distillation_contract import canonical_extraction_output_hash
    from core.hephaestus.distillation_models import DistillationResult, KnowledgeFragment

    raw_fragment = root["fragments"][0]
    fragment = KnowledgeFragment(
        form=raw_fragment["form"],
        title=raw_fragment["title"],
        frontmatter=dict(raw_fragment["frontmatter"]),
        background="Redis 连接池故障上下文。",
        core_content=raw_fragment["core_content"],
        boundaries={},
        anti_patterns=[],
        related_concepts=[],
        claim_ids=list(raw_fragment["claim_ids"]),
    )
    root_hash = canonical_extraction_output_hash(canonical_output=root)
    return DistillationResult(
        session_id="session-1",
        judgment="knowledge",
        fragments=[fragment],
        structured_output=root["structured_output"],
        input_spec=spec,
        extraction_judgment="knowledge",
        extraction_contract_valid=True,
        extraction_output=root,
        extraction_output_hash=root_hash,
        source="codex",
    )


def _write_engine(tmp_path, route):
    from types import SimpleNamespace

    return SimpleNamespace(
        wiki_base=tmp_path,
        _validate_structured_output_contract=lambda result, cfg: True,
        _prepare_fragments=lambda fragments, cfg: fragments,
        _filter_accepted_fragments=lambda result, fragments, cfg: fragments,
        _route_structured_actions=route,
        _link_cross_agent=lambda fragments: None,
        _write_metrics_back=lambda fragments: None,
        _emit_distill_events=lambda result, fragments, written: None,
    )


def test_write_boundary_commits_episode_before_action_or_wiki_route(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.cognitive.state_store import CognitiveStateStore
    from core.hephaestus.distillation_write_receipt import persist_with_receipt

    spec = _input_spec()
    root = _resolve(_root(spec, include_episode=True), spec)
    result = _write_result(spec, root)
    config = SimpleNamespace(database_dir=tmp_path, get=lambda key, default=None: default)
    initialize_cognitive_state_schema(tmp_path / "producer_consumer_ledger.db")
    route_observations = []

    def route(current_result, fragments, cfg):
        revision = CognitiveStateStore(config).revision(
            current_result.cognition_episode_revision_id
        )
        route_observations.append(revision.revision_id if revision else "")
        return [], []

    monkeypatch.setattr(
        "core.hephaestus.raw_provenance.record_page_provenance",
        lambda *args, **kwargs: (),
    )
    persist_with_receipt(_write_engine(tmp_path, route), result, config)

    assert route_observations == [result.cognition_episode_revision_id]
    assert result.cognition_episode_receipt.status == "committed"
    assert any(layer.name == "cognition_episode_commit" for layer in result.layer_results)


def test_write_boundary_blocks_all_routes_when_cognition_store_is_unavailable(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from core.hephaestus.distillation_write_receipt import persist_with_receipt

    spec = _input_spec()
    root = _resolve(_root(spec, include_episode=True), spec)
    result = _write_result(spec, root)
    config = SimpleNamespace(database_dir=tmp_path, get=lambda key, default=None: default)
    route = MagicMock(return_value=([], []))

    receipt = persist_with_receipt(_write_engine(tmp_path, route), result, config)

    assert receipt.status == "retryable_failed"
    assert receipt.terminal_reason == "cognition_episode_commit_failed"
    assert route.call_count == 0
    assert result.cognition_episode_revision_id == ""
    layer = next(
        layer for layer in result.layer_results if layer.name == "cognition_episode_commit"
    )
    assert layer.passed is False
