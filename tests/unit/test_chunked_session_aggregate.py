"""COG-012 regression coverage for deterministic chunk-session aggregation.

The fixtures deliberately use the real typed extractor/checkpoint boundary.  A
plain list-returning fake here would let a last-chunk assignment regression pass
without exercising the contract that formal writes actually rely on.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any


def _fragment(
    index: int,
    *,
    relation_target: str,
):
    from core.hephaestus.distillation_models import KnowledgeFragment

    label = ("决策", "动作", "结果")[index]
    return KnowledgeFragment(
        form="决策记录",
        title=f"COG012 第{index + 1}块{label}的完整认知记录",
        frontmatter={
            "领域": "分块蒸馏",
            "摘要": f"第{index + 1}块保留独立的{label}、关系和来源证据。",
        },
        background="跨 chunk 聚合必须保留每个已准入局部根的认知信息。",
        core_content=(
            f"# 第{index + 1}块{label}\n\n"
            "这一段用于验证分块认知聚合：决策、动作、结果、代码空行和关系"
            "必须按输入顺序保留，不能被末块的合法 skip 或行级去重覆盖。\n\n"
            "```python\nresult = preserve_chunk_evidence()\n\nresult.commit()\n```\n\n"
            "上述内容刻意包含空行和可重复的步骤，以约束合并器不得压缩可见字节。"
            * 3
        ),
        boundaries={"applies": "COG-012 chunk aggregate", "not_applies": "single root"},
        anti_patterns=[],
        related_concepts=[],
        relations=[
            {
                "type": ("related_to", "contradicts", "supercedes")[index],
                "target": f"[[{relation_target}]]",
                "context": f"第{index + 1}块的结构化关系证据必须完整保留。",
            }
        ],
    )


def _fragment_payload(fragment) -> dict[str, Any]:
    return {
        "form": fragment.form,
        "title": fragment.title,
        "frontmatter": dict(fragment.frontmatter),
        "background": fragment.background,
        "core_content": fragment.core_content,
        "boundaries": dict(fragment.boundaries),
        "anti_patterns": list(fragment.anti_patterns),
        "related_concepts": list(fragment.related_concepts),
        "claim_ids": list(fragment.claim_ids),
        "relations": [dict(item) for item in fragment.relations],
    }


def _claim(
    input_spec,
    *,
    index: int,
    claim_id: str,
    claim_text: str,
    recommended_action: str = "create_page",
) -> dict[str, Any]:
    event_id = input_spec.source_event_ids[0]
    claim_type = ("decision", "procedure", "procedure")[index]
    evidence = {
        "source_event_id": event_id,
        "quote": f"CHUNK-{index}: COG-012 认知链路原始证据",
    }
    return {
        "claim_id": claim_id,
        "claim_text": claim_text,
        "claim_type": claim_type,
        "scope": {
            "domain": "COG-012",
            "applies_to": ["chunked session aggregate"],
            "not_applies_to": ["single extraction"],
        },
        "evidence": [evidence, dict(evidence)] if index == 0 else [evidence],
        "relation_to_existing": {
            "type": "new" if recommended_action == "create_page" else "extends",
            "target_pages": (
                [] if recommended_action == "create_page" else ["03-Tech/cog012.md"]
            ),
            "delta_text": f"第{index + 1}块需要追加到既有页面。",
            "reason": "相同 COG-012 主题的增量证据。",
        },
        "recommended_action": recommended_action,
        "cognitive_actions": [
            ("create_observation", "propose_methodology", "record_reinforcement")[
                index
            ]
        ],
        "confidence": 0.81 + index * 0.05,
    }


def _knowledge_outcome(
    request,
    *,
    index: int,
    claim_id: str,
    claim_text: str,
    hypothesis: str,
    recommended_action: str = "create_page",
):
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome
    from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS
    from core.evidence.source_authority import resolve_model_source_authority_selections

    input_spec = request.input_spec
    event_id = input_spec.source_event_ids[0]
    fragment = _fragment(index, relation_target="episode-shared")
    fragment.claim_ids = [claim_id]
    authority = next(
        entry
        for entry in input_spec.source_authority_catalog.entries
        if entry.source_event_id == event_id and entry.span_status == "exact"
    )
    exact_evidence = {
        "source_event_id": event_id,
        "source_authority_id": authority.source_authority_id,
        "quote": f"CHUNK-{index}: COG-012 认知链路原始证据",
    }
    known_values = {
        "situation": f"第{index + 1}块独立情境",
        "goal": f"第{index + 1}块独立目标",
        "facts": f"第{index + 1}块独立事实",
        "decision": f"第{index + 1}块独立决策",
        "actions": f"第{index + 1}块独立动作",
        "outcomes": f"第{index + 1}块独立结果",
        "scope": f"第{index + 1}块独立适用范围",
    }
    cognition_episode = {
        field: [
            {
                "status": "known",
                "value": known_values[field],
                "evidence_refs": [dict(exact_evidence)],
                "claim_ids": [claim_id],
            }
            if field in known_values
            else {
                "status": "unknown",
                "reason": f"第{index + 1}块没有提供 {field} 的可靠证据。",
                "evidence_refs": [],
                "claim_ids": [],
            }
        ]
        for field in COGNITION_EPISODE_FIELDS
    }
    structured = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": "create" if recommended_action == "create_page" else "update",
        "candidate_summary": f"第{index + 1}块的 COG-012 决策、动作和结果。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": hypothesis,
            "intent_evidence": [
                {
                    "source_event_id": event_id,
                    "quote": f"CHUNK-{index}: COG-012 认知链路原始证据",
                    "reason": "测试必须保留互相独立的意图假设。",
                }
            ],
            "intent_verification_events": (
                [
                    {
                        "source_event_id": event_id,
                        "status": "unverified",
                        "quote": f"CHUNK-{index}: COG-012 认知链路原始证据",
                    },
                    {
                        "source_event_id": event_id,
                        "status": "unverified",
                        "quote": f"CHUNK-{index}: COG-012 认知链路原始证据",
                    },
                ]
                if index == 0
                else []
            ),
            "intent_confidence": 0.8,
            "intent_status": "unverified",
            "behavior_summary": f"第{index + 1}块要求保留认知链路。",
        },
        "claims": [
            _claim(
                input_spec,
                index=index,
                claim_id=claim_id,
                claim_text=claim_text,
                recommended_action=recommended_action,
            )
        ],
        "cognition_episode": cognition_episode,
    }
    payload = {
        "judgment": "knowledge",
        "judgment_reason": f"第{index + 1}块含有独立的可复用认知证据。",
        "fragments": [_fragment_payload(fragment)],
        "structured_output": structured,
    }
    resolution = resolve_model_source_authority_selections(
        payload,
        input_spec.source_authority_catalog,
    )
    assert resolution.issues == ()
    payload = resolution.payload
    structured = payload["structured_output"]
    admission = validate_extraction_output(payload, input_spec)
    assert admission.valid, admission.error_text
    canonical_output = canonicalize_extraction_output(payload, (fragment,))
    return ExtractionOutcome(
        judgment="knowledge",
        fragments=(fragment,),
        structured_output=structured,
        canonical_output=canonical_output,
        admission=admission,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=canonical_output
        ),
    )


def _skip_outcome(request):
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )
    from core.hephaestus.distillation_models import ExtractionOutcome

    input_spec = request.input_spec
    event_id = input_spec.source_event_ids[0]
    structured = {
        "schema_version": "distill_output_v4",
        "input_spec_hash": input_spec.input_spec_hash,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "source_event_ids": list(input_spec.source_event_ids),
        "raw_completeness": input_spec.raw_completeness,
        "distill_intent": "skip",
        "candidate_summary": "末块只是对前文修复结论的重复确认。",
        "skip_reason": "没有新增长期可复用的决策、动作或结果。",
        "no_value_evidence": [
            {"source_event_id": event_id, "reason": "末块重复确认前文结论。"}
        ],
        "claims": [],
    }
    payload = {
        "judgment": "skip",
        "judgment_reason": "末块是合法 local skip，不能覆盖先前知识根。",
        "fragments": [],
        "structured_output": structured,
    }
    admission = validate_extraction_output(payload, input_spec)
    assert admission.valid, admission.error_text
    canonical_output = canonicalize_extraction_output(payload, ())
    return ExtractionOutcome(
        judgment="skip",
        fragments=(),
        structured_output=structured,
        canonical_output=canonical_output,
        admission=admission,
        canonical_output_hash=canonical_extraction_output_hash(
            canonical_output=canonical_output
        ),
    )


class _AggregateBackend:
    def checkpoint_identity(self):
        return {"provider": "test", "model": "cog012-aggregate"}

    def call(self, *_args, **_kwargs):
        raise AssertionError("typed fixture must not use a live backend")


class _NoMerge:
    """Keep distinct fragments so this file isolates session aggregation."""

    def checkpoint_identity(self):
        return {"strategy": "test-no-fragment-merge"}

    def merge(self, fragments):
        return list(fragments)


class _AggregateExtractor:
    def __init__(
        self,
        *,
        colliding_claims: bool = False,
        recommended_action: str = "create_page",
    ):
        self.backend = _AggregateBackend()
        self.calls: list[str] = []
        self.colliding_claims = colliding_claims
        self.recommended_action = recommended_action

    def prepare_prompt(self, request):
        from core.hephaestus.distill_input_spec import PreparedExtractionPrompt

        return PreparedExtractionPrompt.build(
            f"cog012-aggregate|{request.analysis_type}|{request.session_text}", request
        )

    def extract(self, request, *, prepared=None):
        assert prepared is not None
        prepared.assert_matches(request)
        self.calls.append(request.session_text)
        if "CHUNK-0" in request.session_text:
            return _knowledge_outcome(
                request,
                index=0,
                claim_id="claim-decision",
                claim_text="用户决定先修复分块聚合，再验证输入、关系和动作都没有丢失。",
                hypothesis="用户正在确认分块认知修复的决策。",
                recommended_action=self.recommended_action,
            )
        if "CHUNK-1" in request.session_text:
            return _knowledge_outcome(
                request,
                index=1,
                claim_id=("claim-decision" if self.colliding_claims else "claim-action-outcome"),
                claim_text=(
                    "同一外部 claim id 但不同的动作语义：执行冷启动、命中缓存和重启校验。"
                    if self.colliding_claims
                    else "修复动作完成后，冷启动、全命中和重启必须得到相同聚合根。"
                ),
                hypothesis="用户正在验证修复动作和可重复结果。",
                recommended_action=self.recommended_action,
            )
        if "CHUNK-2" in request.session_text:
            return _knowledge_outcome(
                request,
                index=2,
                claim_id="claim-outcome",
                claim_text="最终结果必须同时保留三个局部知识根和末块合法 skip。",
                hypothesis="用户正在确认修复结果与认知守恒。",
                recommended_action=self.recommended_action,
            )
        if "CHUNK-3" in request.session_text:
            return _skip_outcome(request)
        raise AssertionError(f"unexpected test chunk: {request.session_text!r}")


class _AggregateConfig:
    def __init__(self, root: Path):
        from core.config import get_config

        self.database_dir = root / "db"
        self.wiki_dir = root / "wiki"
        self._base = get_config()
        self._values = {
            "distill.chunk_checkpoint_enabled": True,
            "distill.chunk_checkpoint_db_path": str(self.database_dir / "chunks.db"),
        }

    def get(self, key, default=None):
        return self._values.get(key, self._base.get(key, default))

    def __getattr__(self, name):
        return getattr(self._base, name)


def _chunks() -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    for index, role in enumerate(("user", "assistant", "user", "assistant")):
        content = f"CHUNK-{index}: COG-012 认知链路原始证据"
        chunks.append(
            [
                {
                    "role": role,
                    "content": content,
                    "turn": index + 1,
                    "source_span": {
                        "revision_id": f"raw-revision-{index}",
                        "logical_event_id": f"logical-{index}",
                        "turn_number": index + 1,
                        "content_hash": f"sha256:chunk-{index}",
                        "role": role,
                        "span_start": 0,
                        "span_end": len(content),
                    },
                }
            ]
        )
    return chunks


def _engine(
    root: Path,
    *,
    colliding_claims: bool = False,
    recommended_action: str = "create_page",
):
    from core.hephaestus.distillation_engine import DistillationEngine

    config = _AggregateConfig(root)
    engine = DistillationEngine(
        wiki_base=str(root / "wiki"),
        backend_factory=_AggregateBackend,
        receipt_config=config,
    )
    extractor = _AggregateExtractor(
        colliding_claims=colliding_claims,
        recommended_action=recommended_action,
    )
    engine._extractor = extractor
    engine._fragment_merger = _NoMerge()
    engine._chunk_messages = lambda *_args, **_kwargs: _chunks()
    engine._cross_linker = type("_CrossLinker", (), {"link": staticmethod(lambda fragments: fragments)})()
    return engine, config, extractor


def _raw_event_refs(chunks):
    return [
        {
            key: value
            for key, value in chunk[0]["source_span"].items()
            if key != "role"
        }
        for chunk in chunks
    ]


def _run_chunked(engine, config):
    from core.hephaestus.distillation_engine import DistillationResult

    chunks = _chunks()
    result = DistillationResult(
        session_id="cog012-aggregate-session",
        source="codex",
        raw_event_refs=_raw_event_refs(chunks),
    )
    fragments, infos = engine._extract_chunked(
        result,
        [message for chunk in chunks for message in chunk],
        {"cfg": config, "chunk_size": 400},
    )
    return result, fragments, infos


def _aggregate_hash(result) -> str:
    assert result.chunk_aggregate is not None
    assert result.chunk_aggregate.aggregate_root_hash == result.extraction_output_hash
    return result.chunk_aggregate.aggregate_root_hash


def test_four_chunk_aggregate_is_lossless_and_identical_for_cold_hit_and_restart(
    tmp_path, monkeypatch
):
    """Three distinct cognition roots plus a local skip must produce one stable root."""
    cold_engine, cold_config, cold_extractor = _engine(tmp_path)
    cold, cold_fragments, cold_infos = _run_chunked(cold_engine, cold_config)

    assert cold_fragments is not None
    assert len(cold_extractor.calls) == 4
    assert [info["cache_hit"] for info in cold_infos] == [False, False, False, False]
    assert cold.extraction_judgment == "knowledge"
    assert cold.input_spec.input_mode == "chunked_aggregate_v1"
    cold_claim_ids = [claim["claim_id"] for claim in cold.structured_output["claims"]]
    assert len(cold_claim_ids) == 3
    assert len(set(cold_claim_ids)) == 3
    assert all(claim_id.startswith("claim-") for claim_id in cold_claim_ids)
    assert [fragment.relations[0]["target"] for fragment in cold_fragments] == [
        "[[episode-shared]]",
        "[[episode-shared]]",
        "[[episode-shared]]",
    ]
    assert [fragment.relations[0]["type"] for fragment in cold_fragments] == [
        "related_to",
        "contradicts",
        "supercedes",
    ]
    assert all(
        chunk.contract_verdict == "admitted" for chunk in cold.chunk_extraction_results
    )
    assert [chunk.canonical_output["judgment"] for chunk in cold.chunk_extraction_results] == [
        "knowledge",
        "knowledge",
        "knowledge",
        "skip",
    ]
    assert [
        chunk.episode_fragment["user_behavior_intent"]["intent_hypothesis"]
        for chunk in cold.chunk_extraction_results[:3]
    ] == [
        "用户正在确认分块认知修复的决策。",
        "用户正在验证修复动作和可重复结果。",
        "用户正在确认修复结果与认知守恒。",
    ]
    assert [span["revision_id"] for span in cold.chunk_extraction_results[0].source_span_map] == [
        "raw-revision-0"
    ]
    assert cold.chunk_aggregate.episode["competing_hypotheses"]
    assert cold.chunk_aggregate.episode["claim_conservation"] == {
        "input_count": 3,
        "output_count": 3,
        "lost_claims": 0,
        "duplicate_claims": 0,
        "claim_id_collisions": [],
    }
    assert cold.chunk_aggregate.episode["lost_relations"] == 0
    assert cold.chunk_aggregate.episode["relation_conflicts"] == [
        {
            "target": "[[episode-shared]]",
            "relations": cold.chunk_aggregate.episode["relations"],
        }
    ]
    assert cold.structured_output["chunk_aggregation"]["input_ids"] == [
        "raw-revision-0",
        "raw-revision-1",
        "raw-revision-2",
        "raw-revision-3",
    ]
    assert cold.structured_output["chunk_aggregation"]["output_ids"] == [
        "raw-revision-0",
        "raw-revision-1",
        "raw-revision-2",
        "raw-revision-3",
    ]
    expected_episode_values = {
        "goal": [f"第{index + 1}块独立目标" for index in range(3)],
        "decision": [f"第{index + 1}块独立决策" for index in range(3)],
        "actions": [f"第{index + 1}块独立动作" for index in range(3)],
        "outcomes": [f"第{index + 1}块独立结果" for index in range(3)],
    }
    for field, expected_values in expected_episode_values.items():
        entries = cold.structured_output["cognition_episode"][field]
        assert len(entries) == 3
        assert [item["value"] for item in entries] == expected_values
    repeated = cold.structured_output["claims"][0]["evidence"]
    assert len(repeated) == 2
    assert repeated[0]["quote"] == repeated[1]["quote"]
    assert [item["aggregate_origin"]["local_ordinal"] for item in repeated] == [0, 1]
    verification_events = cold.structured_output["user_behavior_intent"][
        "intent_verification_events"
    ]
    assert len(verification_events) == 2
    assert verification_events[0]["quote"] == verification_events[1]["quote"]
    assert [item["aggregate_origin"]["local_ordinal"] for item in verification_events] == [
        0,
        1,
    ]
    cold_hash = _aggregate_hash(cold)

    hit, hit_fragments, hit_infos = _run_chunked(cold_engine, cold_config)
    assert hit_fragments is not None
    assert cold_extractor.calls and len(cold_extractor.calls) == 4
    assert [info["cache_hit"] for info in hit_infos] == [True, True, True, True]
    assert [chunk.cache_hit for chunk in hit.chunk_extraction_results] == [
        True,
        True,
        True,
        True,
    ]
    assert _aggregate_hash(hit) == cold_hash
    assert [claim["claim_id"] for claim in hit.structured_output["claims"]] == cold_claim_ids

    restarted_engine, restarted_config, restarted_extractor = _engine(tmp_path)
    restarted, restarted_fragments, restarted_infos = _run_chunked(
        restarted_engine, restarted_config
    )
    assert restarted_fragments is not None
    assert restarted_extractor.calls == []
    assert [info["cache_hit"] for info in restarted_infos] == [True, True, True, True]
    assert _aggregate_hash(restarted) == cold_hash
    assert [claim["claim_id"] for claim in restarted.structured_output["claims"]] == cold_claim_ids
    assert [fragment.relations for fragment in restarted_fragments] == [
        fragment.relations for fragment in cold_fragments
    ]

    from datetime import datetime

    import core.hephaestus.distillation_wiki_page as wiki_page

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 15, 12, 0, 0)
            return value if tz is None else value.replace(tzinfo=tz)

    monkeypatch.setattr(wiki_page, "datetime", _FrozenDateTime)

    def render_pages(result, fragments):
        return [
            wiki_page.generate_wiki_page(
                fragment,
                result.session_id,
                source=result.source,
                session_coverage=result.session_coverage,
                distill_input_mode=result.distill_input_mode,
                structured_output=result.structured_output,
                wiki_dir=tmp_path,
            )
            for fragment in fragments
        ]

    cold_pages = render_pages(cold, cold_fragments)
    assert cold_pages == render_pages(hit, hit_fragments)
    assert cold_pages == render_pages(restarted, restarted_fragments)
    assert len(cold_pages) == 3
    assert all("result = preserve_chunk_evidence()\n\nresult.commit()" in page for page in cold_pages)


def test_chunked_extraction_rejects_missing_source_span_before_model_call(tmp_path):
    """Formal chunking may not make an LLM call without exact Raw evidence."""
    from core.hephaestus.distillation_engine import DistillationResult

    engine, config, extractor = _engine(tmp_path)
    chunks = _chunks()
    del chunks[1][0]["source_span"]
    engine._chunk_messages = lambda *_args, **_kwargs: chunks
    result = DistillationResult(
        session_id="cog012-missing-source-span",
        source="codex",
        raw_event_refs=[{"revision_id": "raw-aggregate-session"}],
    )

    fragments, infos = engine._extract_chunked(
        result,
        [message for chunk in chunks for message in chunk],
        {"cfg": config, "chunk_size": 400},
    )

    assert fragments is None
    assert infos == []
    assert extractor.calls == []
    assert result.error == "chunk_source_span_invalid"
    assert result.extraction_contract_valid is False


def test_chunked_extraction_rejects_forged_span_length_before_model_call(tmp_path):
    from core.hephaestus.distillation_engine import DistillationResult

    engine, config, extractor = _engine(tmp_path)
    chunks = _chunks()
    raw_event_refs = _raw_event_refs(chunks)
    chunks[1][0]["source_span"]["span_end"] -= 1
    engine._chunk_messages = lambda *_args, **_kwargs: chunks
    result = DistillationResult(
        session_id="cog012-forged-source-length",
        source="codex",
        raw_event_refs=raw_event_refs,
    )

    fragments, infos = engine._extract_chunked(
        result,
        [message for chunk in chunks for message in chunk],
        {"cfg": config, "chunk_size": 400},
    )

    assert fragments is None
    assert infos == []
    assert extractor.calls == []
    assert result.error == "chunk_source_span_invalid"


def test_chunked_extraction_rejects_catalog_hash_mismatch_before_model_call(tmp_path):
    from core.hephaestus.distillation_engine import DistillationResult

    engine, config, extractor = _engine(tmp_path)
    chunks = _chunks()
    raw_event_refs = _raw_event_refs(chunks)
    chunks[2][0]["source_span"]["content_hash"] = "sha256:forged"
    engine._chunk_messages = lambda *_args, **_kwargs: chunks
    result = DistillationResult(
        session_id="cog012-forged-source-hash",
        source="codex",
        raw_event_refs=raw_event_refs,
    )

    fragments, infos = engine._extract_chunked(
        result,
        [message for chunk in chunks for message in chunk],
        {"cfg": config, "chunk_size": 400},
    )

    assert fragments is None
    assert infos == []
    assert extractor.calls == []
    assert result.error == "chunk_source_span_invalid"


def test_same_source_claim_id_with_distinct_semantics_is_derived_without_loss(tmp_path):
    """An extractor collision must become two stable derived claim IDs, never overwrite."""
    engine, config, _extractor = _engine(tmp_path, colliding_claims=True)
    result, fragments, _infos = _run_chunked(engine, config)

    assert fragments is not None
    claims = result.structured_output["claims"]
    assert len(claims) == 3
    assert len({claim["claim_id"] for claim in claims}) == 3
    assert {claim["claim_text"] for claim in claims} == {
        "用户决定先修复分块聚合，再验证输入、关系和动作都没有丢失。",
        "同一外部 claim id 但不同的动作语义：执行冷启动、命中缓存和重启校验。",
        "最终结果必须同时保留三个局部知识根和末块合法 skip。",
    }
    collisions = result.chunk_aggregate.episode["claim_conservation"][
        "claim_id_collisions"
    ]
    assert collisions
    assert collisions[0]["original_claim_id"] == "claim-decision"
    colliding_claim_ids = {
        claim["claim_id"]
        for claim in claims
        if claim["claim_text"] != "最终结果必须同时保留三个局部知识根和末块合法 skip。"
    }
    assert set(collisions[0]["aggregate_claim_ids"]) == colliding_claim_ids


def test_tampered_checkpoint_root_misses_and_reextracts_only_that_chunk(tmp_path):
    """A stored canonical root is evidence, not a trusted cache blob."""
    cold_engine, cold_config, cold_extractor = _engine(tmp_path)
    cold, _fragments, _infos = _run_chunked(cold_engine, cold_config)
    cold_hash = _aggregate_hash(cold)
    assert len(cold_extractor.calls) == 4

    db_path = cold_config.database_dir / "chunks.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT structured_output_json FROM distill_chunk_results "
            "WHERE session_id = ? AND chunk_index = 1",
            ("cog012-aggregate-session",),
        ).fetchone()
        assert row is not None
        envelope = json.loads(row[0])
        envelope["canonical_output"]["structured_output"]["claims"][0][
            "claim_text"
        ] = "tampered checkpoint root must never be routed"
        conn.execute(
            "UPDATE distill_chunk_results SET structured_output_json = ? "
            "WHERE session_id = ? AND chunk_index = 1",
            (json.dumps(envelope, ensure_ascii=False), "cog012-aggregate-session"),
        )

    retry_engine, retry_config, retry_extractor = _engine(tmp_path)
    retried, fragments, infos = _run_chunked(retry_engine, retry_config)
    assert fragments is not None
    assert len(retry_extractor.calls) == 1
    assert [info["cache_hit"] for info in infos] == [True, False, True, True]
    assert infos[1]["miss_reason"] in {
        "corrupt_root_output_binding",
        "corrupt_output_admission",
        "checkpoint_output_contract_invalid",
    }
    assert _aggregate_hash(retried) == cold_hash


def test_router_rejects_forged_chunk_aggregate_or_capability_without_writes(tmp_path):
    """Forging either aggregate evidence or its capability must not reach create_pages."""
    from core.hephaestus.distill_action_router import (
        DistillActionRouter,
        DistillActionRouterOptions,
    )

    engine, config, _extractor = _engine(tmp_path)
    result, fragments, _infos = _run_chunked(engine, config)
    assert fragments is not None
    engine._run_cross_linking(result, fragments)
    assert result.fragment_route_capability is not None
    assert result.chunk_aggregate is not None

    router_db = tmp_path / "router-db"
    router_wiki = tmp_path / "router-wiki"
    router_db.mkdir()
    router_wiki.mkdir()
    router = DistillActionRouter(
        DistillActionRouterOptions(
            database_dir=router_db,
            wiki_dir=router_wiki,
        ),
    )

    def assert_zero_writes(forged):
        calls: list[list[Any]] = []

        def create_pages(accepted):
            calls.append(list(accepted))
            return [], []

        routed = router.route(forged, forged.fragments, create_pages)
        assert calls == []
        assert routed.written == []
        assert routed.errors

    aggregate_forged = copy.copy(result)
    aggregate_forged.chunk_aggregate = replace(
        result.chunk_aggregate,
        aggregate_root_hash="sha256:forged-chunk-aggregate",
    )
    assert_zero_writes(aggregate_forged)

    from core.hephaestus.distillation_contract import canonical_extraction_output_hash

    forged_root = copy.deepcopy(result.chunk_aggregate.aggregate_root)
    forged_root["judgment_reason"] = "self-consistent forged aggregate"
    forged_hash = canonical_extraction_output_hash(canonical_output=forged_root)
    recomputation_forged = copy.copy(result)
    recomputation_forged.chunk_aggregate = replace(
        result.chunk_aggregate,
        aggregate_root=forged_root,
        aggregate_root_hash=forged_hash,
    )
    recomputation_forged.extraction_output = forged_root
    recomputation_forged.extraction_output_hash = forged_hash
    recomputation_forged.fragment_route_capability = replace(
        result.fragment_route_capability,
        extraction_output_hash=forged_hash,
    )
    assert_zero_writes(recomputation_forged)

    capability_forged = copy.copy(result)
    capability_forged.fragment_route_capability = replace(
        result.fragment_route_capability,
        chunk_root_hashes=("sha256:forged-chunk-root",),
    )
    assert_zero_writes(capability_forged)


def test_chunked_update_shadow_proposals_preserve_exact_claim_evidence_without_effect(
    tmp_path, monkeypatch
):
    from core.cognitive.cognition_episode_persistence import commit_cognition_episode
    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.hephaestus.distill_action_router import (
        DistillActionRouter,
        DistillActionRouterOptions,
    )
    from core.hephaestus.raw_provenance import preflight_chunked_write_provenance

    engine, config, _extractor = _engine(
        tmp_path,
        recommended_action="update_page",
    )
    result, fragments, _infos = _run_chunked(engine, config)
    assert fragments is not None
    engine._run_cross_linking(result, fragments)
    initialize_cognitive_state_schema(config.database_dir / "producer_consumer_ledger.db")
    commit_cognition_episode(result, config)
    monkeypatch.setattr(
        "core.hephaestus.distill_action_router.publish_wiki_page_updated",
        lambda *_args, **_kwargs: None,
    )
    router = DistillActionRouter(
            DistillActionRouterOptions(
                database_dir=tmp_path / "route-db",
                wiki_dir=tmp_path / "route-wiki",
                cognitive_state_database_dir=config.database_dir,
            )
    )

    routed = router.route(
        result,
        fragments,
        lambda _accepted: (_ for _ in ()).throw(
            AssertionError("update routing must not call create_pages")
        ),
    )

    assert routed.errors == []
    assert routed.written == []
    assert routed.page_raw_event_refs == []
    actions = router.list_actions_for_session(result.session_id)
    assert len(actions) == 3
    assert {row["result_status"] for row in actions} == {"proposed"}
    assert {row["target_kind"] for row in actions} == {
        "shadow",
        "authority_pending_hypothesis",
    }
    shadow_texts = []
    for row in actions:
        shadow_path = router.wiki_dir / row["target_page"]
        assert shadow_path.exists()
        shadow_texts.append(shadow_path.read_text(encoding="utf-8"))
        assert router.list_cognitive_actions(row["action_id"]) == []
    for index in range(3):
        assert any(f"raw-revision-{index}" in text for text in shadow_texts)

    forged_claim = copy.deepcopy(result.structured_output["claims"][0])
    forged_claim.pop("aggregate_origin")
    try:
        preflight_chunked_write_provenance(result, [forged_claim], fragments)
    except ValueError as exc:
        assert "lacks aggregate origin" in str(exc)
    else:
        raise AssertionError("missing claim provenance must fail before a routed write")
