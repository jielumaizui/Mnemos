#!/usr/bin/env python3
"""Audit the canonical distillation output union and release enforcement.

This is intentionally a static/local release gate.  It checks the one schema
that is rendered into the extract prompt and executed by the runtime validator,
then rejects a configured release profile that disables either the structured
contract gate or its action router.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.cognition_episode_contract import COGNITION_EPISODE_FIELDS  # noqa: E402
from core.evidence.artifact_capture import build_capture_artifact_refs  # noqa: E402
from core.evidence.artifact_catalog import resolve_model_artifact_selections  # noqa: E402
from core.evidence.source_authority import (  # noqa: E402
    resolve_model_source_authority_selections,
)
from core.hephaestus.distill_entrypoint_audit import (  # noqa: E402
    audit_distill_entrypoint,
)
from core.hephaestus.distill_input_spec import DistillInputSpec  # noqa: E402
from core.hephaestus.distill_output_version import (  # noqa: E402
    DISTILL_OUTPUT_CONTRACT_VERSION,
)
from core.hephaestus.distillation_contract import (  # noqa: E402
    SCHEMA_VERSION,
    _canonical_output_schema,
    canonical_model_output_projection,
    canonical_output_validator,
    validate_extraction_output,
)
from core.hephaestus.prompt_builder import TemplateRegistry  # noqa: E402


REPORT_SCHEMA_VERSION = "mnemos.distill_output_contract_audit.v2"
RELEASE_ENFORCEMENT_KEYS = (
    "distill.structured_output_contract.enforce",
    "distill.action_router.enabled",
)
GOLDEN_CORPUS_PATH = (
    ROOT / "tests" / "fixtures" / "cognition_episode_golden" / "manifest.json"
)


def _minimal_typed_skip() -> tuple[dict[str, Any], DistillInputSpec]:
    visible_input = "contract audit synthetic no-value input"
    spec = DistillInputSpec.build(
        source_agent="contract-audit",
        source_session_id="contract-audit-session",
        source_event_ids=["audit-event-1"],
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        source_messages=[
            {
                "role": "user",
                "content": visible_input,
                "source_span": {
                    "revision_id": "audit-event-1",
                    "content_hash": "sha256:" + "1" * 64,
                    "span_start": 0,
                    "span_end": len(visible_input),
                    "role": "user",
                },
            }
        ],
    )
    contract = {
        "schema_version": SCHEMA_VERSION,
        **spec.prompt_contract(),
        "distill_intent": "skip",
        "candidate_summary": "Synthetic no-value input.",
        "skip_reason": "The synthetic input contains no reusable conclusion.",
        "no_value_evidence": [
            {
                "source_event_id": spec.source_event_ids[0],
                "reason": "The only synthetic event contains no durable knowledge.",
            }
        ],
        "claims": [],
    }
    return (
        {
            "judgment": "skip",
            "judgment_reason": "No durable knowledge is present.",
            "fragments": [],
            "structured_output": contract,
        },
        spec,
    )


def _knowledge_for_spec(spec: DistillInputSpec) -> dict[str, Any]:
    quote = "Please retain this reusable conclusion."
    authority_matches = spec.source_authority_catalog.matching_entries(
        source_event_id=spec.source_event_ids[0],
        quote=quote,
    )
    if len(authority_matches) != 1:
        raise ValueError("synthetic authority fixture is ambiguous")
    authority_id = authority_matches[0].source_authority_id
    episode = {
        field: [
            {
                "status": "known",
                "value": f"Synthetic {field} for the contract audit.",
                "evidence_refs": [
                    {
                        "source_event_id": spec.source_event_ids[0],
                        "source_authority_id": authority_id,
                        "quote": quote,
                    }
                ],
                "claim_ids": ["audit-claim-1"],
            }
            if field in {"situation", "facts", "scope"}
            else {
                "status": "unknown",
                "reason": f"No reliable synthetic {field} evidence was supplied.",
                "evidence_refs": [],
                "claim_ids": [],
            }
        ]
        for field in COGNITION_EPISODE_FIELDS
    }
    contract = {
        "schema_version": SCHEMA_VERSION,
        **spec.prompt_contract(),
        "distill_intent": "create",
        "candidate_summary": "Synthetic reusable conclusion for the contract audit.",
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [
                {
                    "source_event_id": spec.source_event_ids[0],
                    "quote": quote,
                }
            ],
            "intent_verification_events": [],
            "intent_confidence": 0.8,
            "intent_status": "unverified",
            "behavior_summary": "The synthetic user requests a reusable conclusion.",
        },
        "claims": [
            {
                "claim_id": "audit-claim-1",
                "claim_text": "The contract audit must reject a non-skip response without evidence.",
                "claim_type": "technical_fact",
                "scope": {"domain": "distillation"},
                "evidence": [
                    {
                        "source_event_id": spec.source_event_ids[0],
                        "source_authority_id": authority_id,
                        "quote": quote,
                    }
                ],
                "relation_to_existing": {"type": "new"},
                "recommended_action": "create_page",
                "confidence": 0.9,
            }
        ],
        "cognition_episode": episode,
    }
    payload = {
        "judgment": "knowledge",
        "judgment_reason": "The synthetic input contains a reusable conclusion.",
        "fragments": [
            {
                "form": "经验法则",
                "title": "Synthetic contract audit conclusion",
                "frontmatter": {"摘要": "Synthetic reusable audit conclusion.", "领域": "测试"},
                "core_content": "# Synthetic contract audit conclusion\n\n"
                + "This is deliberately long enough for the canonical extraction contract. " * 2,
                "claim_ids": ["audit-claim-1"],
            }
        ],
        "structured_output": contract,
    }
    resolution = resolve_model_source_authority_selections(
        payload,
        spec.source_authority_catalog,
    )
    if resolution.issues:
        raise ValueError("synthetic authority fixture failed")
    if not isinstance(resolution.payload, Mapping):
        raise ValueError("synthetic authority fixture resolved to a non-object")
    return dict(resolution.payload)


def _minimal_typed_knowledge() -> tuple[dict[str, Any], DistillInputSpec]:
    """Build a complete, admissible non-skip response for the runtime probe."""
    visible_input = "Please retain this reusable conclusion."
    spec = DistillInputSpec.build(
        source_agent="contract-audit",
        source_session_id="contract-audit-knowledge-session",
        source_event_ids=["audit-event-knowledge-1"],
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        source_messages=[
            {
                "role": "user",
                "content": visible_input,
                "source_span": {
                    "revision_id": "audit-event-knowledge-1",
                    "content_hash": "sha256:" + "2" * 64,
                    "span_start": 0,
                    "span_end": len(visible_input),
                    "role": "user",
                },
            }
        ],
    )
    return _knowledge_for_spec(spec), spec


def _golden_case_payload(
    case: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], DistillInputSpec, dict[str, Any]]:
    """Build model/resolved payloads plus an independent Raw-span oracle."""

    case_id = str(case["case_id"])
    source_text = str(case["source_text"])
    event_id = f"golden-raw-{case_id}"
    revision_hash = "sha256:" + hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    oracle: dict[str, Any] = {
        "source_agent": "golden-contract-audit",
        "source_session_id": f"golden-{case_id}",
        "source_event_id": event_id,
        "raw_completeness": "full",
        "acl": "local_user",
        "retention_policy": "inherit_source",
        "span_start": 0,
        "span_end": len(source_text),
        "content_hash": revision_hash,
    }
    spec = DistillInputSpec.build(
        source_agent=oracle["source_agent"],
        source_session_id=oracle["source_session_id"],
        source_event_ids=[event_id],
        raw_completeness=oracle["raw_completeness"],
        visible_input=source_text,
        input_mode="standard",
        source_messages=[
            {
                "role": "user",
                "content": source_text,
                "source_span": {
                    "revision_id": event_id,
                    "content_hash": revision_hash,
                    "span_start": 0,
                    "span_end": len(source_text),
                    "role": "user",
                },
            }
        ],
    )
    base = {
        "schema_version": SCHEMA_VERSION,
        **spec.prompt_contract(),
    }
    if case.get("judgment") == "skip":
        structured = {
            **base,
            "distill_intent": "skip",
            "candidate_summary": "该对话没有形成可复用认知结论。",
            "skip_reason": str(case["skip_reason"]),
            "no_value_evidence": [
                {
                    "source_event_id": event_id,
                    "reason": str(case["skip_reason"]),
                }
            ],
            "claims": [],
        }
        skip_payload = {
            "judgment": "skip",
            "judgment_reason": str(case["skip_reason"]),
            "fragments": [],
            "structured_output": structured,
        }
        return skip_payload, skip_payload, spec, oracle

    authority = next(
        entry
        for entry in spec.source_authority_catalog.entries
        if entry.span_status == "exact"
    )
    evidence = {
        "source_event_id": event_id,
        "source_authority_id": authority.source_authority_id,
        "quote": source_text,
    }
    claim_id = f"golden-claim-{case_id}"
    known_fields = dict(case.get("known_fields") or {})
    not_applicable = set(case.get("not_applicable_fields") or ())
    episode: dict[str, list[dict[str, Any]]] = {}
    for field_name in COGNITION_EPISODE_FIELDS:
        if field_name in known_fields:
            entry: dict[str, Any] = {
                "status": "known",
                "value": str(known_fields[field_name]),
                "evidence_refs": [dict(evidence)],
                "claim_ids": [claim_id],
            }
        elif field_name in not_applicable:
            entry = {
                "status": "not_applicable",
                "reason": f"{case_id} 场景不适用 {field_name}。",
                "evidence_refs": [],
                "claim_ids": [],
            }
        else:
            entry = {
                "status": "unknown",
                "reason": f"该输入没有提供 {field_name} 的可靠证据。",
                "evidence_refs": [],
                "claim_ids": [],
            }
        episode[field_name] = [entry]
    claim_type = str(case["claim_type"])
    claim: dict[str, Any] = {
        "claim_id": claim_id,
        "claim_text": str(case["claim_text"]),
        "claim_type": claim_type,
        "scope": {"domain": "cognition episode golden corpus"},
        "evidence": [dict(evidence)],
        "relation_to_existing": {"type": "new"},
        "recommended_action": "create_page",
        "confidence": 0.9,
    }
    if claim_type in {"decision", "preference"}:
        claim["cognitive_actions"] = ["create_observation"]
    structured = {
        **base,
        "distill_intent": "create",
        "candidate_summary": str(case["claim_text"]),
        "user_behavior_intent": {
            "content_source": "native_dialogue",
            "user_intent_signal": "seeking_judgment",
            "intent_hypothesis": "seeking_judgment",
            "intent_evidence": [dict(evidence)],
            "intent_verification_events": [],
            "intent_confidence": 0.8,
            "intent_status": "unverified",
            "behavior_summary": f"Golden corpus case {case_id} contains reusable cognition.",
        },
        "claims": [claim],
        "cognition_episode": episode,
    }
    knowledge_payload = {
        "judgment": "knowledge",
        "judgment_reason": f"Golden corpus case {case_id} is reusable.",
        "fragments": [
            {
                "form": "决策记录",
                "title": f"Cognition episode golden case {case_id}",
                "frontmatter": {
                    "摘要": f"Golden corpus {case_id} cognition episode.",
                    "领域": "认知合同",
                },
                "core_content": f"# {case_id}\n\n" + source_text * 3,
                "claim_ids": [claim_id],
            }
        ],
        "structured_output": structured,
    }
    resolution = resolve_model_source_authority_selections(
        knowledge_payload,
        spec.source_authority_catalog,
    )
    if resolution.issues:
        raise ValueError(f"golden case {case_id} source resolution failed")
    if not isinstance(resolution.payload, Mapping):
        raise ValueError(f"golden case {case_id} resolved to a non-object")
    return knowledge_payload, dict(resolution.payload), spec, oracle


def audit_cognition_episode_golden_corpus() -> tuple[dict[str, Any], list[str]]:
    """Validate the COG-010 corpus and its fail-closed negative probes."""

    errors: list[str] = []
    try:
        corpus = json.loads(GOLDEN_CORPUS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"ok": False, "corpus_path": str(GOLDEN_CORPUS_PATH)}, [
            f"cognition episode golden corpus unavailable: {exc}"
        ]
    cases = corpus.get("cases") if isinstance(corpus, dict) else None
    if not isinstance(cases, list):
        return {"ok": False, "corpus_path": str(GOLDEN_CORPUS_PATH)}, [
            "cognition episode golden corpus cases must be a list"
        ]
    expected_case_ids = {
        "decision",
        "failure_correction",
        "preference_boundary",
        "no_conclusion",
    }
    observed_case_ids = {
        str(case.get("case_id") or "") for case in cases if isinstance(case, dict)
    }
    if observed_case_ids != expected_case_ids:
        errors.append("cognition episode golden corpus case denominator drift")

    eligible_count = 0
    skip_count = 0
    field_total = 0
    field_valid = 0
    exact_span_mismatch_count = 0
    context_mismatch_count = 0
    typed_nonassertive_violation_count = 0
    first_model_payload: dict[str, Any] | None = None
    first_spec: DistillInputSpec | None = None
    for raw_case in cases:
        if not isinstance(raw_case, dict):
            errors.append("cognition episode golden corpus contains a non-object case")
            continue
        case_id = str(raw_case.get("case_id") or "unknown")
        try:
            model_payload, payload, spec, oracle = _golden_case_payload(raw_case)
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            errors.append(f"golden case {case_id} could not be built: {exc}")
            continue
        runtime = validate_extraction_output(payload, spec)
        schema_errors = list(
            canonical_output_validator().iter_errors(
                canonical_model_output_projection(payload)
            )
        )
        if not runtime.valid:
            errors.append(f"golden case {case_id} runtime rejection: {runtime.error_text}")
        if schema_errors:
            errors.append(f"golden case {case_id} schema rejection")
        if raw_case.get("judgment") == "skip":
            skip_count += 1
            if not runtime.is_skip or "cognition_episode" in payload["structured_output"]:
                errors.append(f"golden skip case {case_id} is not a strict typed skip")
            continue

        eligible_count += 1
        first_model_payload = first_model_payload or model_payload
        first_spec = first_spec or spec
        structured = payload["structured_output"]
        context = spec.cognition_context
        if (
            structured.get("cognition_context_hash") != context.context_hash
            or context.source_agent != oracle["source_agent"]
            or context.source_session_id != oracle["source_session_id"]
            or context.raw_completeness != oracle["raw_completeness"]
            or context.acl != oracle["acl"]
            or context.retention_policy != oracle["retention_policy"]
        ):
            context_mismatch_count += 1
        spans = [dict(value) for value in context.source_spans]
        if len(spans) != 1 or any(
            (
                span.get("revision_id") != oracle["source_event_id"]
                or span.get("span_status") != "exact"
                or span.get("span_start") != oracle["span_start"]
                or span.get("span_end") != oracle["span_end"]
                or span.get("source_revision_sha256") != oracle["content_hash"]
            )
            for span in spans
        ):
            context_mismatch_count += 1

        episode = structured.get("cognition_episode")
        if not isinstance(episode, dict):
            errors.append(f"golden case {case_id} has no cognition episode")
            continue
        for field_name in COGNITION_EPISODE_FIELDS:
            field_total += 1
            entries = episode.get(field_name)
            if not isinstance(entries, list) or not entries:
                continue
            entry = entries[0]
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status == "known":
                evidence_refs = entry.get("evidence_refs")
                exact = bool(
                    isinstance(evidence_refs, list)
                    and evidence_refs
                    and evidence_refs[0].get("authority_span_status") == "exact"
                    and evidence_refs[0].get("authority_source_revision_sha256")
                    == oracle["content_hash"]
                    and evidence_refs[0].get("source_event_id")
                    == oracle["source_event_id"]
                )
                if not exact:
                    exact_span_mismatch_count += 1
                    continue
            elif status in {"unknown", "not_applicable"}:
                if (
                    entry.get("value")
                    or entry.get("evidence_refs")
                    or entry.get("claim_ids")
                    or not str(entry.get("reason") or "").strip()
                ):
                    typed_nonassertive_violation_count += 1
                    continue
            else:
                continue
            field_valid += 1

    forged_ref_rejected = False
    cross_chunk_ref_rejected = False
    all_unknown_rejected = False
    if first_model_payload is not None and first_spec is not None:
        forged = json.loads(json.dumps(first_model_payload))
        forged_entry = forged["structured_output"]["cognition_episode"]["facts"][0]
        forged_entry["evidence_refs"][0]["source_authority_id"] = (
            "source-authority:" + "f" * 32
        )
        forged_resolution = resolve_model_source_authority_selections(
            forged,
            first_spec.source_authority_catalog,
        )
        forged_ref_rejected = bool(forged_resolution.issues)

        cross_chunk = json.loads(json.dumps(first_model_payload))
        cross_evidence = cross_chunk["structured_output"]["cognition_episode"]["facts"][0][
            "evidence_refs"
        ][0]
        cross_evidence["source_event_id"] = "outside-chunk-revision"
        cross_resolution = resolve_model_source_authority_selections(
            cross_chunk,
            first_spec.source_authority_catalog,
        )
        cross_chunk_ref_rejected = bool(cross_resolution.issues)

        resolved = resolve_model_source_authority_selections(
            first_model_payload,
            first_spec.source_authority_catalog,
        ).payload
        all_unknown = json.loads(json.dumps(resolved))
        for required_field in ("situation", "facts", "scope"):
            all_unknown["structured_output"]["cognition_episode"][required_field] = [
                {
                    "status": "unknown",
                    "reason": "Synthetic all-unknown bypass attempt.",
                    "evidence_refs": [],
                    "claim_ids": [],
                }
            ]
        all_unknown_rejected = not validate_extraction_output(
            all_unknown,
            first_spec,
        ).valid
    if not forged_ref_rejected:
        errors.append("golden corpus forged source authority ref was not rejected")
    if not cross_chunk_ref_rejected:
        errors.append("golden corpus cross-chunk source ref was not rejected")
    if not all_unknown_rejected:
        errors.append("golden corpus all-unknown non-skip bypass was not rejected")
    if exact_span_mismatch_count:
        errors.append("golden corpus exact Raw span mismatch")
    if context_mismatch_count:
        errors.append("golden corpus extraction context differs from Raw oracle")
    if typed_nonassertive_violation_count:
        errors.append("golden corpus unknown/not_applicable entry asserts unsupported content")
    required_field_rate = field_valid / field_total if field_total else 0.0
    if required_field_rate != 1.0:
        errors.append("golden corpus cognition required field rate is below 100%")
    report = {
        "schema_version": str(corpus.get("schema_version") or ""),
        "corpus_path": str(GOLDEN_CORPUS_PATH.relative_to(ROOT)),
        "case_count": len(cases),
        "eligible_non_skip_count": eligible_count,
        "typed_skip_count": skip_count,
        "required_field_total": field_total,
        "required_field_valid": field_valid,
        "required_field_rate": required_field_rate,
        "exact_span_mismatch_count": exact_span_mismatch_count,
        "context_mismatch_count": context_mismatch_count,
        "typed_nonassertive_violation_count": typed_nonassertive_violation_count,
        "forged_ref_rejected": forged_ref_rejected,
        "cross_chunk_ref_rejected": cross_chunk_ref_rejected,
        "all_unknown_rejected": all_unknown_rejected,
        "ok": not errors,
    }
    return report, errors


def _nested_property(
    schema: dict[str, Any], *path: str
) -> dict[str, Any] | None:
    """Return a schema property without silently accepting a malformed shape."""
    current: Any = schema
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get("properties", {}).get(part)
    return current if isinstance(current, dict) else None


def _require_array_bound(
    errors: list[str],
    schema: dict[str, Any] | None,
    *,
    key: str,
    expected: int,
    label: str,
) -> None:
    if not isinstance(schema, dict) or schema.get(key) != expected:
        errors.append(f"canonical extraction schema must require {label} {key}={expected}")


def _require_schema_and_runtime_rejection(
    errors: list[str],
    *,
    payload: dict[str, Any],
    spec: DistillInputSpec,
    label: str,
) -> None:
    """Reject drift where a policy is only checked by Python or only by schema."""
    schema_errors = list(
        canonical_output_validator().iter_errors(canonical_model_output_projection(payload))
    )
    runtime = validate_extraction_output(payload, spec)
    if not schema_errors:
        errors.append(f"canonical extraction schema accepts invalid {label}")
    if runtime.valid:
        errors.append(f"runtime accepts invalid {label}")


def validate_release_enforcement(config: Any) -> list[str]:
    """Return release-blocking errors for disabled output enforcement."""
    getter = getattr(config, "get", None)
    if not callable(getter):
        return ["release config does not expose get(key, default)"]
    errors: list[str] = []
    for key in RELEASE_ENFORCEMENT_KEYS:
        if getter(key, None) is not True:
            errors.append(f"release profile requires {key}=true")
    return errors


def audit_artifact_identity() -> tuple[dict[str, Any], list[str]]:
    """Exercise a non-empty catalog and report the COG-029 acceptance metric."""
    errors: list[str] = []
    source_event_id = "audit-event-knowledge-1"
    refs = build_capture_artifact_refs(
        source_agent="contract-audit",
        session_id="contract-audit-knowledge-session",
        turn_number=1,
        source_event_id=source_event_id,
        tool_results=(
            {
                "tool_name": "contract_audit",
                "result": "synthetic contract audit tool result",
            },
        ),
    )
    digest = str(refs[0]["sha256"])
    visible_input = "Please retain this reusable conclusion."
    spec = DistillInputSpec.build(
        source_agent="contract-audit",
        source_session_id="contract-audit-knowledge-session",
        source_event_ids=[source_event_id],
        raw_completeness="full",
        visible_input=visible_input,
        input_mode="standard",
        artifact_refs=refs,
        source_messages=[
            {
                "role": "user",
                "content": visible_input,
                "source_span": {
                    "revision_id": source_event_id,
                    "content_hash": "sha256:" + "3" * 64,
                    "span_start": 0,
                    "span_end": len(visible_input),
                    "role": "user",
                },
            }
        ],
    )
    payload = _knowledge_for_spec(spec)
    model_evidence = payload["structured_output"]["claims"][0]["evidence"][0]
    entries = spec.artifact_catalog.entries
    if len(entries) != 1:
        errors.append("artifact identity audit requires one admitted catalog entry")
        ref_id = "artifact-ref:" + "0" * 32
    else:
        ref_id = entries[0].artifact_ref_id
    model_evidence["artifact_ref_id"] = ref_id

    resolution = resolve_model_artifact_selections(payload, spec.artifact_catalog)
    runtime = validate_extraction_output(resolution.payload, spec)
    resolved_evidence = resolution.payload["structured_output"]["claims"][0]["evidence"][0]
    expected = entries[0].resolved_evidence_payload() if entries else {}
    mismatched_fields = sorted(
        field for field, value in expected.items() if resolved_evidence.get(field) != value
    )
    artifact_ref_mismatch = len(resolution.issues) + len(mismatched_fields)
    if artifact_ref_mismatch:
        errors.append(
            "artifact identity resolution mismatch: "
            + ", ".join(issue.code for issue in resolution.issues)
            + (", " if resolution.issues and mismatched_fields else "")
            + ", ".join(mismatched_fields)
        )
    if not runtime.valid:
        errors.append(f"runtime rejects system-resolved artifact evidence: {runtime.error_text}")

    forged = json.loads(json.dumps(payload))
    forged["structured_output"]["claims"][0]["evidence"][0][
        "artifact_ref_id"
    ] = "artifact-ref:" + "f" * 32
    forged_resolution = resolve_model_artifact_selections(
        forged,
        spec.artifact_catalog,
    )
    forged_ref_rejected = any(
        issue.code == "artifact_ref_unknown" for issue in forged_resolution.issues
    )
    if not forged_ref_rejected:
        errors.append("valid-shaped forged artifact_ref_id was not rejected")

    hash_verifiable_count = sum(
        entry.sha256 == f"sha256:{digest}" for entry in entries
    )
    if hash_verifiable_count != len(entries):
        errors.append("not every artifact catalog entry has a verifiable full SHA-256")
    if any(field.startswith("artifact_") and field != "artifact_ref_id" for field in model_evidence):
        errors.append("model evidence contains canonical artifact identity fields")

    return {
        "scope": "static_contract_and_synthetic_validation",
        "catalog_denominator": len(entries),
        "hash_verifiable_count": hash_verifiable_count,
        "artifact_ref_mismatch": artifact_ref_mismatch,
        "model_outputs_canonical_uri": "artifact_uri" in model_evidence,
        "forged_ref_rejected": forged_ref_rejected,
        "ok": not errors,
    }, errors


def validate_contract_sync() -> list[str]:
    """Verify prompt, schema and runtime acceptance use the same v4 union."""
    errors: list[str] = []
    try:
        schema = _canonical_output_schema()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return [f"canonical extraction schema unavailable: {exc}"]

    if SCHEMA_VERSION != DISTILL_OUTPUT_CONTRACT_VERSION:
        errors.append("runtime schema version differs from the single output contract version")
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        errors.append("canonical extraction schema must declare draft 2020-12")
    variants = schema.get("oneOf")
    variant_items = variants if isinstance(variants, list) else []
    names = {
        str(item.get("title") or "")
        for item in variant_items
        if isinstance(item, dict)
    }
    if names != {"SkipOutput", "KnowledgeOrSkillOutput"}:
        errors.append("canonical extraction schema must expose exactly skip and knowledge/skill branches")

    variant_by_name = {
        str(item.get("title")): item
        for item in variant_items
        if isinstance(item, dict)
    }
    skip_variant = variant_by_name.get("SkipOutput")
    non_skip_variant = variant_by_name.get("KnowledgeOrSkillOutput")
    _require_array_bound(
        errors,
        _nested_property(skip_variant, "fragments") if isinstance(skip_variant, dict) else None,
        key="maxItems",
        expected=0,
        label="skip fragments",
    )
    _require_array_bound(
        errors,
        _nested_property(non_skip_variant, "fragments")
        if isinstance(non_skip_variant, dict)
        else None,
        key="minItems",
        expected=1,
        label="non-skip fragments",
    )
    skip_intent = (
        _nested_property(skip_variant, "structured_output", "distill_intent")
        if isinstance(skip_variant, dict)
        else None
    )
    if not isinstance(skip_intent, dict) or skip_intent.get("const") != "skip":
        errors.append("canonical extraction schema must bind the skip branch to distill_intent=skip")
    non_skip_intent = (
        _nested_property(non_skip_variant, "structured_output", "distill_intent")
        if isinstance(non_skip_variant, dict)
        else None
    )
    if not isinstance(non_skip_intent, dict) or set(non_skip_intent.get("enum", [])) != {
        "create",
        "update",
        "merge",
        "dispute",
        "reinforce",
    }:
        errors.append("canonical extraction schema must bind non-skip branches to knowledge intents")

    output_schema = schema.get("properties", {}).get("structured_output", {})
    if not isinstance(output_schema, dict):
        errors.append("canonical extraction schema missing structured_output")
    else:
        version = output_schema.get("properties", {}).get("schema_version", {})
        if not isinstance(version, dict) or version.get("const") != SCHEMA_VERSION:
            errors.append("canonical extraction schema version drift")

        conditional = next(
            (
                clause
                for clause in output_schema.get("allOf", [])
                if isinstance(clause, dict)
                and _nested_property(clause.get("if", {}), "distill_intent") == {"const": "skip"}
            ),
            None,
        )
        if not isinstance(conditional, dict):
            errors.append("canonical extraction schema must include the skip conditional")
        else:
            then_branch = conditional.get("then")
            else_branch = conditional.get("else")
            if not isinstance(then_branch, dict) or not {
                "skip_reason",
                "no_value_evidence",
                "claims",
            }.issubset(set(then_branch.get("required", []))):
                errors.append("canonical extraction schema must require skip evidence and empty claims")
            else:
                _require_array_bound(
                    errors,
                    _nested_property(then_branch, "claims"),
                    key="maxItems",
                    expected=0,
                    label="skip claims",
                )
            if not isinstance(else_branch, dict) or not {
                "user_behavior_intent",
                "cognition_episode",
                "claims",
            }.issubset(set(else_branch.get("required", []))):
                errors.append(
                    "canonical extraction schema must require behavior intent, "
                    "cognition episode and claims for non-skip"
                )
            else:
                _require_array_bound(
                    errors,
                    _nested_property(else_branch, "claims"),
                    key="minItems",
                    expected=1,
                    label="non-skip claims",
                )

        evidence_properties = (
            output_schema.get("properties", {})
            .get("claims", {})
            .get("items", {})
            .get("properties", {})
            .get("evidence", {})
            .get("items", {})
            .get("properties", {})
        )
        system_owned_artifact_fields = {
            "artifact_uri",
            "artifact_type",
            "artifact_summary",
            "artifact_sha256",
            "artifact_mime_type",
            "artifact_acl",
        }
        if "artifact_ref_id" not in evidence_properties:
            errors.append("canonical extraction schema must expose artifact_ref_id selection")
        if system_owned_artifact_fields.intersection(evidence_properties):
            errors.append("model extraction schema exposes system-owned artifact identity fields")

    template = (ROOT / "prompts" / "distill" / "extract" / "base.md").read_text(
        encoding="utf-8"
    )
    required_prompt_fragments = (
        "{output_schema}",
        "严格 skip",
        "no_value_evidence",
        "{artifact_catalog_json}",
        "{source_authority_catalog_json}",
        "禁止输出 `artifact_uri`",
        "source_authority_id",
        "cognition_context_hash",
        "cognition_episode",
        DISTILL_OUTPUT_CONTRACT_VERSION,
    )
    for fragment in required_prompt_fragments:
        if fragment not in template:
            errors.append(f"extract prompt missing canonical contract fragment: {fragment}")
    if "`new` 不要求 `delta_text`" not in template:
        errors.append("extract prompt must state that relation type new does not require delta_text")
    if "非 100% 重复时必须写清楚新增/变化的最小差异" in template:
        errors.append("extract prompt relation example incorrectly requires delta_text for new")

    rendered = TemplateRegistry(ROOT / "prompts" / "distill").render_schema("extract")
    required_rendered_constraints = (
        "**分支：SkipOutput**",
        "**分支：KnowledgeOrSkillOutput**",
        "**fragments** (`array`; 最多项数：0)",
        "**fragments** (`array`; 最少项数：1)",
        "**当 `distill_intent` 固定为 `skip` 时**",
        "**满足时必填字段**：`skip_reason`、`no_value_evidence`、`claims`",
        "**claims** (`array`; 最多项数：0)",
        "**否则（非 skip）必填字段**：`user_behavior_intent`、`cognition_episode`、`claims`",
        "**claims** (`array`; 最少项数：1)",
        "**当 `relation_to_existing.type` 为以下之一：`contradicts`、`supersedes` 时**",
        "**recommended_action** (`string`; 固定为 `route_to_dispute`) (必填)",
        "`recommended_action` 不得为 `skip`",
        "匹配模式：`^artifact-ref:[0-9a-f]{32}$`",
        "匹配模式：`^source-authority:[0-9a-f]{32}$`",
        DISTILL_OUTPUT_CONTRACT_VERSION,
    )
    for fragment in required_rendered_constraints:
        if fragment not in rendered:
            errors.append(f"prompt schema rendering drift: missing {fragment}")

    legal_skip, spec = _minimal_typed_skip()
    legal = validate_extraction_output(legal_skip, spec)
    if not legal.valid or not legal.is_skip:
        errors.append(f"runtime rejects canonical legal skip: {legal.error_text}")

    legal_knowledge, knowledge_spec = _minimal_typed_knowledge()
    knowledge = validate_extraction_output(legal_knowledge, knowledge_spec)
    if not knowledge.valid or knowledge.is_skip:
        errors.append(f"runtime rejects canonical legal non-skip: {knowledge.error_text}")
    if list(
        canonical_output_validator().iter_errors(
            canonical_model_output_projection(legal_knowledge)
        )
    ):
        errors.append("canonical extraction schema rejects the runtime legal non-skip")

    artifact_without_companions = json.loads(json.dumps(legal_knowledge))
    artifact_without_companions["structured_output"]["claims"][0]["evidence"][0][
        "artifact_ref_id"
    ] = "forged-artifact-reference"
    _require_schema_and_runtime_rejection(
        errors,
        payload=artifact_without_companions,
        spec=knowledge_spec,
        label="artifact reference outside the system catalog",
    )

    cautious_external_behavior = json.loads(json.dumps(legal_knowledge))
    behavior = cautious_external_behavior["structured_output"]["user_behavior_intent"]
    behavior.update(
        {
            "content_source": "external_file",
            "user_intent_signal": "curate_or_decision_material",
            "intent_hypothesis": "seeking_summary",
            "intent_confidence": 0.6,
        }
    )
    if list(
        canonical_output_validator().iter_errors(
            canonical_model_output_projection(cautious_external_behavior)
        )
    ):
        errors.append("canonical schema still forces external-file intent elevation")
    if not validate_extraction_output(cautious_external_behavior, knowledge_spec).valid:
        errors.append("runtime still forces external-file intent elevation")

    invalid_unknown_behavior = json.loads(json.dumps(legal_knowledge))
    behavior = invalid_unknown_behavior["structured_output"]["user_behavior_intent"]
    behavior.update(
        {
            "user_intent_signal": "unknown",
            "intent_hypothesis": "unknown",
            "intent_status": "unverified",
            "intent_confidence": 0.8,
        }
    )
    _require_schema_and_runtime_rejection(
        errors,
        payload=invalid_unknown_behavior,
        spec=knowledge_spec,
        label="overconfident unknown behavior intent",
    )

    invalid_claim_action = json.loads(json.dumps(legal_knowledge))
    claim = invalid_claim_action["structured_output"]["claims"][0]
    claim.update(
        {
            "claim_type": "decision",
            "relation_to_existing": {"type": "contradicts"},
            "recommended_action": "create_page",
        }
    )
    _require_schema_and_runtime_rejection(
        errors,
        payload=invalid_claim_action,
        spec=knowledge_spec,
        label="conflicting high-value claim without route and action",
    )

    invalid_skip = json.loads(json.dumps(legal_skip))
    invalid_skip["structured_output"]["claims"] = [
        {"claim_id": "forbidden-in-skip"}
    ]
    invalid = validate_extraction_output(invalid_skip, spec)
    if invalid.valid:
        errors.append("runtime accepts a skip branch with knowledge claims")
    return errors


def audit(config: Any | None = None) -> dict[str, Any]:
    config = get_config() if config is None else config
    contract_errors = validate_contract_sync()
    artifact_identity, artifact_identity_errors = audit_artifact_identity()
    cognition_episode, cognition_episode_errors = (
        audit_cognition_episode_golden_corpus()
    )
    enforcement_errors = validate_release_enforcement(config)
    entrypoint = audit_distill_entrypoint(ROOT)
    errors = [
        *contract_errors,
        *artifact_identity_errors,
        *cognition_episode_errors,
        *enforcement_errors,
        *entrypoint.errors,
    ]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not errors,
        "contract_drift_count": len(contract_errors),
        "artifact_identity": artifact_identity,
        "cognition_episode_golden_corpus": cognition_episode,
        "release_enforcement_errors": enforcement_errors,
        "entrypoint_ownership": entrypoint.to_dict(),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="accepted for release-gate parity")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)
    report = audit()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["ok"]:
        print("Distill output contract audit passed")
    else:
        print("Distill output contract audit failed:")
        for error in report["errors"]:
            print(f"- {error}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
