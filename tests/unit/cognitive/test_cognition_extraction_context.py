from __future__ import annotations

import hashlib
import inspect


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_distill_input_spec_seals_exact_cognition_context_before_model_call():
    from core.hephaestus.distill_input_spec import DistillInputSpec

    content = "用户决定先修复认知事件合同，并保留失败证据。"
    start = 17
    message = {
        "role": "user",
        "content": content,
        "source_span": {
            "revision_id": "rawrev-1",
            "logical_event_id": "raw-event-1",
            "turn_number": 3,
            "content_hash": _sha256(content),
            "span_start": start,
            "span_end": start + len(content),
            "role": "user",
        },
    }

    spec = DistillInputSpec.build(
        source_agent="codex",
        source_session_id="session-1",
        source_event_ids=("rawrev-1",),
        raw_completeness="full",
        visible_input=content,
        input_mode="standard",
        source_messages=(message,),
    )

    assert spec.schema_version == "mnemos.distill_input_spec.v4"
    context = spec.cognition_context
    assert context.schema_version == "mnemos.cognition_extraction_context.v1"
    assert context.source_agent == "codex"
    assert context.source_session_id == "session-1"
    assert context.source_event_ids == ("rawrev-1",)
    assert context.raw_completeness == "full"
    assert context.loss_contract == "lossless-visible-v1"
    assert context.acl == "local_user"
    assert context.purpose == "canonical_cognition_episode"
    assert context.retention_policy == "inherit_source"
    assert context.source_spans == (
        {
            "source_authority_id": spec.source_authority_catalog.entries[0].source_authority_id,
            "revision_id": "rawrev-1",
            "role": "user",
            "span_start": start,
            "span_end": start + len(content),
            "span_status": "exact",
            "content_sha256": _sha256(content),
            "source_revision_sha256": _sha256(content),
        },
    )
    prompt_contract = spec.prompt_contract()
    assert prompt_contract["cognition_context_hash"] == context.context_hash
    assert prompt_contract["cognition_context"]["source_span_refs"] == [
        spec.source_authority_catalog.entries[0].source_authority_id
    ]
    assert spec.canonical_payload()["cognition_context"] == context.canonical_payload()


def test_cognition_context_is_not_a_caller_supplied_input_field():
    from core.hephaestus.distill_input_spec import DistillInputSpec

    parameters = inspect.signature(DistillInputSpec).parameters

    assert "cognition_context" not in parameters


def test_cognition_context_does_not_label_session_bound_text_as_exact_raw_span():
    from core.hephaestus.distill_input_spec import DistillInputSpec

    content = "调用方没有提供可独立验证的 Raw revision span。"
    spec = DistillInputSpec.build(
        source_agent="codex",
        source_session_id="detached-session",
        source_event_ids=("rawrev-detached",),
        raw_completeness="unknown",
        visible_input=content,
        input_mode="standard",
    )

    assert spec.source_authority_catalog.entries[0].span_status == "session_bound"
    assert spec.cognition_context.source_spans == ()
    assert spec.cognition_context.prompt_payload()["source_span_refs"] == []
