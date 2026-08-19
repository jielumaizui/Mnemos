"""Shared v4 cognition-episode fixtures for typed distillation tests.

These helpers intentionally build the model-owned projection first and then
run the production source-authority resolver.  Tests therefore cannot pass by
fabricating system-owned Raw span or authority metadata.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping


DEFAULT_KNOWN_EPISODE_FIELDS = ("situation", "facts", "scope")


def exact_source_message(
    *,
    role: str,
    content: str,
    revision_id: str,
    **metadata: Any,
) -> dict[str, Any]:
    """Build one message whose visible bytes are bound to an exact Raw span."""

    return {
        "role": role,
        "content": content,
        **metadata,
        "source_span": {
            "revision_id": revision_id,
            "content_hash": "sha256:"
            + hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "span_start": 0,
            "span_end": len(content),
            "role": role,
        },
    }


def model_exact_evidence(
    input_spec: Any,
    *,
    source_event_id: str | None = None,
    quote: str | None = None,
) -> dict[str, Any]:
    """Select one exact system catalog entry using only model-owned fields."""

    candidates = [
        entry
        for entry in input_spec.source_authority_catalog.entries
        if entry.span_status == "exact"
        and (source_event_id is None or entry.source_event_id == source_event_id)
    ]
    assert candidates, "fixture requires at least one exact Raw source span"
    entry = candidates[0]
    selected_quote = str(quote or entry._verifiable_text).strip()
    assert selected_quote and entry.matches_quote(selected_quote)
    return {
        "source_event_id": entry.source_event_id,
        "source_authority_id": entry.source_authority_id,
        "quote": selected_quote,
    }


def model_cognition_episode(
    evidence: Mapping[str, Any],
    *,
    claim_id: str,
    known_fields: Iterable[str] = DEFAULT_KNOWN_EPISODE_FIELDS,
    known_values: Mapping[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build a complete non-skip 19-field model cognition episode."""

    from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS

    known = set(known_fields)
    values = dict(known_values or {})
    return {
        field: [
            {
                "status": "known",
                "value": values.get(field, f"{field} 的可验证测试认知"),
                "evidence_refs": [deepcopy(dict(evidence))],
                "claim_ids": [claim_id],
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


def resolve_model_evidence(payload: Mapping[str, Any], input_spec: Any) -> dict[str, Any]:
    """Resolve every evidence selection through the production authority gate."""

    from core.evidence.source_authority import resolve_model_source_authority_selections

    resolution = resolve_model_source_authority_selections(
        payload,
        input_spec.source_authority_catalog,
    )
    assert resolution.issues == (), resolution.issues
    assert isinstance(resolution.payload, dict)
    return resolution.payload


def bind_admitted_cognition_episode(
    result: Any,
    database_dir: Path,
    *,
    source_event_ids: Iterable[str],
) -> Any:
    """Bind a lightweight sink test to a real admitted v4 cognition episode."""

    from core.cognitive.state_schema import initialize_cognitive_state_schema
    from core.hephaestus.distill_input_spec import DistillInputSpec
    from core.hephaestus.distillation_contract import (
        canonical_extraction_output_hash,
        canonicalize_extraction_output,
        validate_extraction_output,
    )

    event_ids = tuple(str(value) for value in source_event_ids if str(value))
    assert event_ids, "fixture requires at least one source event"
    source_messages = [
        exact_source_message(
            role="user",
            content=f"Canonical provenance evidence for {event_id}.",
            revision_id=event_id,
        )
        for event_id in event_ids
    ]
    visible_input = "\n".join(message["content"] for message in source_messages)
    source_agent = str(getattr(result, "source", "") or "codex")
    input_spec = DistillInputSpec.build(
        source_agent=source_agent,
        source_session_id=str(result.session_id),
        source_event_ids=event_ids,
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="chunked",
        source_messages=source_messages,
    )
    evidence = model_exact_evidence(input_spec, source_event_id=event_ids[0])
    claim_id = "provenance-fixture-claim"
    admitted_forms = {
        "问题-解决",
        "决策记录",
        "经验法则",
        "反模式",
        "方法论",
        "洞察关联",
    }
    for fragment in result.fragments:
        if fragment.form not in admitted_forms:
            fragment.form = "经验法则"
        fragment.frontmatter = dict(fragment.frontmatter or {})
        fragment.frontmatter.setdefault("摘要", "验证分块页面的精确原始来源映射。")
        fragment.frontmatter.setdefault("领域", "测试工程")
        if not fragment.claim_ids:
            fragment.claim_ids = [claim_id]
    structured = {
        "schema_version": "distill_output_v4",
        **input_spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "验证页面写入与原始来源映射的完整认知终态。",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {**dict(evidence), "reason": "输入要求验证精确来源映射。"}
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.8,
            "intent_status": "unverified",
            "behavior_summary": "用户要求验证分块页面的来源映射。",
        },
        "claims": [
            {
                "claim_id": claim_id,
                "claim_text": "每个分块页面必须保留其自身精确的原始来源跨度。",
                "claim_type": "technical_fact",
                "scope": {"domain": "provenance"},
                "evidence": [dict(evidence)],
                "relation_to_existing": {
                    "type": "new",
                    "target_pages": [],
                    "delta_text": "",
                    "reason": "测试 vault 没有既有页面。",
                },
                "recommended_action": "create_page",
                "confidence": 0.9,
            }
        ],
        "cognition_episode": model_cognition_episode(
            evidence,
            claim_id=claim_id,
        ),
    }
    root = canonicalize_extraction_output(
        {
            "judgment": str(result.judgment),
            "judgment_reason": "完整认知来源映射测试",
            "structured_output": structured,
        },
        result.fragments,
    )
    root = resolve_model_evidence(root, input_spec)
    validation = validate_extraction_output(root, input_spec)
    assert validation.valid, validation.error_text
    result.source = source_agent
    result.input_spec = input_spec
    result.structured_output = root["structured_output"]
    result.extraction_judgment = str(result.judgment)
    result.extraction_contract_valid = True
    result.extraction_output = root
    result.extraction_output_hash = canonical_extraction_output_hash(
        canonical_output=root
    )
    database_dir = Path(database_dir)
    initialize_cognitive_state_schema(
        database_dir / "producer_consumer_ledger.db"
    )
    return result


def commit_cognition_episode_result(result: Any, database_dir: Path) -> str:
    """Provision the canonical test store and commit an admitted result."""

    from core.cognitive.cognition_episode_persistence import commit_cognition_episode
    from core.cognitive.state_schema import initialize_cognitive_state_schema

    database_dir = Path(database_dir)
    initialize_cognitive_state_schema(database_dir / "producer_consumer_ledger.db")
    receipt = commit_cognition_episode(
        result,
        SimpleNamespace(database_dir=database_dir, get=lambda _key, default=None: default),
    )
    assert result.cognition_episode_revision_id == receipt.revision_id
    return receipt.revision_id
