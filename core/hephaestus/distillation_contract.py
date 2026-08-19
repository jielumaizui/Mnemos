# -*- coding: utf-8 -*-
"""Validation for structured distillation output before vault writes."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from numbers import Real
from pathlib import Path
from typing import Any, Mapping, Sequence, TypeGuard

from core.cognitive.sources import ContentSource, UserIntent
from core.evidence.artifact_catalog import (
    LOCAL_USER_ACL,
    SYSTEM_ARTIFACT_FIELDS,
    ArtifactCatalog,
    model_artifact_projection,
    normalize_sha256,
)
from core.evidence.artifact_uri import (
    ALLOWED_ARTIFACT_TYPES,
    artifact_uri_error,
    parse_artifact_uri,
)
from core.evidence.source_authority import (
    SYSTEM_SOURCE_AUTHORITY_FIELDS,
    SourceAuthorityCatalog,
    model_source_authority_projection,
)
from core.hephaestus.behavior_intent import INTENT_STATUS_VALUES
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.cognition_episode_validation import (
    validate_cognition_episode_draft,
)
from core.hephaestus.distill_output_version import DISTILL_OUTPUT_CONTRACT_VERSION

SCHEMA_VERSION = DISTILL_OUTPUT_CONTRACT_VERSION

RAW_COMPLETENESS_VALUES = {"full", "compressed", "truncated", "partial", "unknown"}
DISTILL_INTENTS = {"create", "update", "merge", "dispute", "reinforce", "skip"}
CLAIM_TYPES = {
    "technical_fact",
    "preference",
    "procedure",
    "decision",
    "constraint",
    "pattern",
    "anti_pattern",
    "entity",
    "relationship",
    "open_question",
    "meta",
}
RELATION_TYPES = {
    "new",
    "same",
    "extends",
    "refines",
    "specializes",
    "example",
    "related",
    "contradicts",
    "supersedes",
}
RECOMMENDED_ACTIONS = {
    "create_page",
    "merge_into_page",
    "update_page",
    "route_to_dispute",
    "record_reinforcement",
    "skip",
}
COGNITIVE_ACTIONS = {
    "create_observation",
    "create_reflection_seed",
    "propose_policy_patch",
    "propose_methodology",
    "propose_pitfall_pattern",
    "update_relation",
    "record_reinforcement",
}
COGNITIVE_ACTION_REQUIRED_CLAIM_TYPES = {
    "preference",
    "procedure",
    "decision",
    "constraint",
    "pattern",
    "anti_pattern",
    "relationship",
    "meta",
}
CONTENT_SOURCE_VALUES = {source.value for source in ContentSource}
USER_INTENT_SIGNAL_VALUES = {intent.value for intent in UserIntent}
INTENT_VERIFICATION_STATUS_VALUES = {"verified", "refuted", "revised", "unverified"}

DELTA_RELATIONS = {"extends", "refines", "specializes", "example", "related"}
CONFLICT_RELATIONS = {"contradicts", "supersedes"}


@dataclass(frozen=True)
class ContractValidationIssue:
    code: str
    path: str
    message: str


@dataclass
class ContractValidationResult:
    """Result of validating a distillation output contract."""

    issues: list[ContractValidationIssue] = field(default_factory=list)
    relation_types: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    output_judgment: str = ""

    @property
    def valid(self) -> bool:
        return not self.issues

    @property
    def errors(self) -> list[ContractValidationIssue]:
        return list(self.issues)

    @property
    def error_text(self) -> str:
        return "; ".join(f"{issue.path}: {issue.message}" for issue in self.issues)

    @property
    def requires_dispute(self) -> bool:
        return any(rel in CONFLICT_RELATIONS for rel in self.relation_types)

    @property
    def reinforcement_only(self) -> bool:
        return bool(self.actions) and all(
            action == "record_reinforcement" for action in self.actions
        )

    @property
    def is_skip(self) -> bool:
        return self.valid and self.output_judgment == "skip"


def validate_distill_output_contract(
    payload: Mapping[str, Any] | None,
    *,
    input_spec: DistillInputSpec | None = None,
) -> ContractValidationResult:
    """Validate the inner structured contract used by the vault gatekeeper.

    ``input_spec`` is optional only for consumers that inspect a detached
    stored payload.  The extraction and checkpoint paths always provide it,
    which makes source identity a comparison rather than an LLM assertion.
    """
    result = ContractValidationResult()
    if not _is_mapping(payload):
        result.issues.append(
            ContractValidationIssue(
                "invalid_payload",
                "$",
                "structured distillation output must be a mapping",
            )
        )
        return result

    _require_equal(result, payload, "schema_version", SCHEMA_VERSION)
    _require_non_empty_str(result, payload, "input_spec_hash")
    _require_non_empty_str(result, payload, "gate_decision_id")
    _require_non_empty_str(result, payload, "source_agent")
    _require_non_empty_str(result, payload, "source_session_id")
    source_event_ids = _require_non_empty_str_list(result, payload, "source_event_ids")
    _require_enum(result, payload, "raw_completeness", RAW_COMPLETENESS_VALUES)
    distill_intent = _require_enum(result, payload, "distill_intent", DISTILL_INTENTS)
    result.output_judgment = distill_intent
    _require_non_empty_str(result, payload, "candidate_summary")
    _require_non_empty_str(result, payload, "cognition_context_hash")
    _validate_input_spec_binding(result, payload, input_spec)
    artifact_catalog = input_spec.artifact_catalog if input_spec is not None else None
    source_authority_catalog = (
        input_spec.source_authority_catalog if input_spec is not None else None
    )

    if distill_intent == "skip":
        _require_non_empty_str(result, payload, "skip_reason")
        _validate_no_value_evidence(result, payload.get("no_value_evidence"), set(source_event_ids))
        claims = payload.get("claims")
        if not _is_sequence_allow_empty(claims) or list(claims):
            result.issues.append(
                ContractValidationIssue(
                    "invalid_skip_claims",
                    "claims",
                    "skip distill output must declare an empty claims list",
                )
            )
        behavior = payload.get("user_behavior_intent")
        if behavior not in (None, ""):
            _validate_user_behavior_intent(
                result,
                behavior,
                set(source_event_ids),
                distill_intent,
                source_authority_catalog,
            )
        return result

    _validate_user_behavior_intent(
        result,
        payload.get("user_behavior_intent"),
        set(source_event_ids),
        distill_intent,
        source_authority_catalog,
    )

    claims = payload.get("claims")
    if not _is_sequence(claims):
        result.issues.append(
            ContractValidationIssue("missing_claims", "claims", "claims must be a non-empty list")
        )
        return result

    claim_ids = {
        str(claim.get("claim_id") or "")
        for claim in claims
        if _is_mapping(claim) and _non_empty_str(claim.get("claim_id"))
    }
    episode_issues = validate_cognition_episode_draft(
        payload.get("cognition_episode"),
        claim_ids=claim_ids,
        source_authority_catalog=source_authority_catalog,
        evidence_validator=lambda evidence, path: _validate_evidence(
            result,
            evidence,
            path,
            set(source_event_ids),
            artifact_catalog,
            source_authority_catalog,
            field_name="evidence_refs",
        ),
    )
    result.issues.extend(
        ContractValidationIssue(issue.code, issue.path, issue.message)
        for issue in episode_issues
    )

    for index, claim in enumerate(claims):
        _validate_claim(
            result,
            claim,
            index,
            set(source_event_ids),
            artifact_catalog,
            source_authority_catalog,
        )

    return result


def _validate_no_value_evidence(
    result: ContractValidationResult,
    value: Any,
    source_event_ids: set[str],
) -> None:
    path = "no_value_evidence"
    if not _is_sequence(value):
        result.issues.append(
            ContractValidationIssue(
                "missing_no_value_evidence",
                path,
                "skip output must include a non-empty no_value_evidence list",
            )
        )
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not _is_mapping(item):
            result.issues.append(
                ContractValidationIssue(
                    "invalid_no_value_evidence",
                    item_path,
                    "no-value evidence item must be a mapping",
                )
            )
            continue
        source_id = item.get("source_event_id")
        if not _non_empty_str(source_id) or str(source_id) not in source_event_ids:
            result.issues.append(
                ContractValidationIssue(
                    "invalid_no_value_evidence_source",
                    f"{item_path}.source_event_id",
                    "no-value evidence source_event_id must be listed in source_event_ids",
                )
            )
        _require_non_empty_str(result, item, "reason", item_path)


def validate_extraction_output(
    payload: Mapping[str, Any] | None,
    input_spec: DistillInputSpec,
) -> ContractValidationResult:
    """Validate the full model response before correction or checkpointing.

    The response is a discriminated union.  A legal skip is not treated as a
    malformed empty knowledge response; every non-skip branch has to contain
    at least one fragment and a non-skip structured intent.
    """
    result = ContractValidationResult()
    if not _is_mapping(payload):
        result.issues.append(
            ContractValidationIssue(
                "invalid_extraction_payload",
                "$",
                "distillation extraction output must be a mapping",
            )
        )
        return result

    _validate_against_canonical_schema(result, payload)

    judgment = _require_enum(result, payload, "judgment", {"knowledge", "skill", "skip"})
    result.output_judgment = judgment
    _require_non_empty_str(result, payload, "judgment_reason")
    fragments = payload.get("fragments")
    if not _is_sequence_allow_empty(fragments):
        result.issues.append(
            ContractValidationIssue("invalid_fragments", "fragments", "fragments must be a list")
        )
        fragments = []

    structured = payload.get("structured_output")
    structured_validation = validate_distill_output_contract(
        structured if _is_mapping(structured) else None,
        input_spec=input_spec,
    )
    result.issues.extend(structured_validation.issues)
    result.relation_types.extend(structured_validation.relation_types)
    result.actions.extend(structured_validation.actions)

    if judgment == "skip":
        if list(fragments):
            result.issues.append(
                ContractValidationIssue(
                    "skip_with_fragments",
                    "fragments",
                    "skip output must use an empty fragments list",
                )
            )
        if _is_mapping(structured) and structured.get("distill_intent") != "skip":
            result.issues.append(
                ContractValidationIssue(
                    "skip_intent_mismatch",
                    "structured_output.distill_intent",
                    "skip output must use distill_intent=skip",
                )
            )
        return result

    if not _is_sequence(fragments):
        result.issues.append(
            ContractValidationIssue(
                "missing_fragments",
                "fragments",
                "knowledge or skill output must contain at least one fragment",
            )
        )
    else:
        for index, fragment in enumerate(fragments):
            _validate_fragment_shape(result, fragment, index)
        _validate_claim_fragment_mapping(result, structured, fragments)
    if _is_mapping(structured) and structured.get("distill_intent") == "skip":
        result.issues.append(
            ContractValidationIssue(
                "non_skip_intent_mismatch",
                "structured_output.distill_intent",
                "knowledge or skill output must not use distill_intent=skip",
            )
        )
    return result


@lru_cache(maxsize=1)
def _canonical_output_schema() -> Mapping[str, Any]:
    # Import lazily: PromptBuilder owns bundled prompt/schema asset IO, while
    # this module owns the runtime validation semantics for that exact asset.
    from core.hephaestus.prompt_builder import TemplateRegistry

    path = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "distill"
        / "_output_schemas"
        / "extract.json"
    )
    return TemplateRegistry.load_json_schema(path)


def canonical_output_schema_text() -> str:
    """Render the exact runtime-owned schema for correction prompts."""
    return json.dumps(
        _canonical_output_schema(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _validate_against_canonical_schema(
    result: ContractValidationResult,
    payload: Mapping[str, Any],
) -> None:
    """Run the exact prompt schema at runtime; no prompt-only schema drift."""
    try:
        validator = canonical_output_validator()
    except ImportError:
        result.issues.append(
            ContractValidationIssue(
                "schema_validator_unavailable",
                "$",
                "canonical output schema validator is unavailable",
            )
        )
        return
    try:
        errors = sorted(
            validator.iter_errors(
                canonical_model_output_projection(payload)
            ),
            key=lambda error: list(error.absolute_path),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        result.issues.append(
            ContractValidationIssue(
                "canonical_schema_unavailable",
                "$",
                "canonical output schema could not be loaded",
            )
        )
        return
    for error in errors:
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        result.issues.append(
            ContractValidationIssue(
                "schema_validation_failed",
                path,
                error.message,
            )
        )


def canonical_model_output_projection(payload: Any) -> Any:
    """Return the exact model-owned view consumed by the JSON Schema."""

    return model_source_authority_projection(model_artifact_projection(payload))


@lru_cache(maxsize=1)
def canonical_output_validator() -> Any:
    """Return the schema validator, including schema-owned artifact URI format.

    The URI grammar is a semantic format instead of a prompt-only convention:
    the JSON Schema declares it and this validator executes it.  This keeps
    the runtime's strict URI parsing aligned with the exact schema rendered to
    the model.
    """
    from jsonschema import Draft202012Validator, FormatChecker

    format_checker = FormatChecker()

    @format_checker.checks("mnemos-artifact-uri")
    def _is_mnemos_artifact_uri(value: Any) -> bool:
        return artifact_uri_error(value) == ""

    return Draft202012Validator(
        _canonical_output_schema(),
        format_checker=format_checker,
    )


def validate_checkpoint_extraction_output(
    *,
    canonical_output: Mapping[str, Any] | None,
    input_spec: DistillInputSpec,
) -> ContractValidationResult:
    """Re-run the canonical union validator over the persisted root payload.

    Checkpoints must preserve and replay the actual root response, including
    ``judgment_reason`` and optional root fields.  Fabricating a replacement
    root during cache lookup would make the stored data pass a different
    contract than the response that was originally admitted.
    """
    return validate_extraction_output(canonical_output, input_spec)


def validate_admitted_extraction_root(
    *,
    input_spec: DistillInputSpec,
    structured_output: Mapping[str, Any] | None,
    extraction_contract_valid: bool | None,
    extraction_output: Mapping[str, Any] | None,
    extraction_output_hash: str,
    extraction_judgment: str,
) -> ContractValidationResult:
    """Validate the immutable root proof required before any formal action.

    The Engine and direct ``DistillActionRouter.route()`` callers must use the
    same proof: a root output admitted against the current input spec, its
    canonical hash, and the exact judgment/structured output now being acted
    upon.  This prevents a caller from submitting only a valid inner payload
    directly to the router after bypassing extraction admission.
    """
    result = ContractValidationResult()
    if extraction_contract_valid is not True:
        result.issues.append(
            ContractValidationIssue(
                "missing_extraction_admission_proof",
                "extraction_contract_valid",
                "extraction admission proof is missing",
            )
        )

    if not _is_mapping(extraction_output):
        result.issues.append(
            ContractValidationIssue(
                "missing_canonical_extraction_root",
                "extraction_output",
                "canonical extraction root is missing",
            )
        )
        return result

    canonical_output = dict(extraction_output)
    root_validation = validate_checkpoint_extraction_output(
        canonical_output=canonical_output,
        input_spec=input_spec,
    )
    result.output_judgment = root_validation.output_judgment
    result.relation_types.extend(root_validation.relation_types)
    result.actions.extend(root_validation.actions)
    for issue in root_validation.issues:
        result.issues.append(
            ContractValidationIssue(
                issue.code,
                f"root.{issue.path}",
                issue.message,
            )
        )

    if extraction_output_hash != canonical_extraction_output_hash(
        canonical_output=canonical_output,
    ):
        result.issues.append(
            ContractValidationIssue(
                "canonical_extraction_root_hash_mismatch",
                "extraction_output_hash",
                "canonical extraction root hash mismatch",
            )
        )
    if canonical_output.get("judgment") != extraction_judgment:
        result.issues.append(
            ContractValidationIssue(
                "extraction_judgment_mismatch",
                "extraction_judgment",
                "extraction judgment does not match root",
            )
        )
    if canonical_output.get("structured_output") != structured_output:
        result.issues.append(
            ContractValidationIssue(
                "structured_output_root_mismatch",
                "structured_output",
                "structured output does not match root",
            )
        )
    return result


def canonicalize_extraction_output(
    output: Mapping[str, Any] | None,
    fragments: Sequence[Any],
) -> dict[str, Any]:
    """Keep the schema-defined root output while normalizing admitted fragments.

    Unknown model keys are deliberately not retained in a checkpoint.  Every
    field declared by the canonical root schema is retained, and fragments are
    rebuilt from the parsed objects that actually passed the admission path.
    """
    declared_root_fields = (
        "judgment",
        "judgment_reason",
        "skill_suggestion",
        "cognitive_decision_asset",
        "analysis_type",
        "data_profile",
        "anomalies",
        "structured_output",
    )
    source = output if _is_mapping(output) else {}
    # This root is the immutable admission evidence that later write routing
    # rechecks.  Do not retain references into mutable ``KnowledgeFragment``
    # instances or the working structured payload: normalization/quality
    # annotations after extraction must not silently alter an admitted root
    # while leaving its stored hash unchanged.
    payload = {
        key: deepcopy(source[key])
        for key in declared_root_fields
        if key in source
    }
    payload["fragments"] = [
        deepcopy(canonical_fragment_payload(fragment)) for fragment in fragments
    ]
    return payload


def canonical_extraction_output_hash(
    *,
    canonical_output: Mapping[str, Any] | None,
) -> str:
    """Hash the exact root payload admitted for replay or checkpoint reuse."""
    payload = dict(canonical_output or {})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_input_spec_binding(
    result: ContractValidationResult,
    payload: Mapping[str, Any],
    input_spec: DistillInputSpec | None,
) -> None:
    if input_spec is None:
        return
    expected = {
        "input_spec_hash": input_spec.input_spec_hash,
        "gate_decision_id": input_spec.gate_decision_id,
        "source_agent": input_spec.source_agent,
        "source_session_id": input_spec.source_session_id,
        "raw_completeness": input_spec.raw_completeness,
        "cognition_context_hash": input_spec.cognition_context.context_hash,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            result.issues.append(
                ContractValidationIssue(
                    "immutable_input_mismatch",
                    key,
                    f"{key} must match the immutable distillation input spec",
                )
            )
    actual_event_ids = payload.get("source_event_ids")
    if not _is_sequence_allow_empty(actual_event_ids) or tuple(actual_event_ids) != input_spec.source_event_ids:
        result.issues.append(
            ContractValidationIssue(
                "immutable_input_mismatch",
                "source_event_ids",
                "source_event_ids must match the immutable distillation input spec",
            )
        )


def _validate_fragment_shape(
    result: ContractValidationResult,
    fragment: Any,
    index: int,
) -> None:
    path = f"fragments[{index}]"
    if not _is_mapping(fragment):
        result.issues.append(
            ContractValidationIssue("invalid_fragment", path, "fragment must be a mapping")
        )
        return
    for key in ("form", "title", "frontmatter", "core_content"):
        value = fragment.get(key)
        if key == "frontmatter":
            valid = _is_mapping(value)
        else:
            valid = _non_empty_str(value)
        if not valid:
            result.issues.append(
                ContractValidationIssue(
                    "invalid_fragment",
                    f"{path}.{key}",
                    f"fragment {key} is required",
                )
            )


def _validate_claim_fragment_mapping(
    result: ContractValidationResult,
    structured: Any,
    fragments: Sequence[Any],
) -> None:
    """Require a total claim-to-fragment relation in the admitted root."""

    if not _is_mapping(structured):
        return
    claims = structured.get("claims")
    if not _is_sequence(claims):
        return
    claim_ids = {
        str(claim.get("claim_id") or "")
        for claim in claims
        if _is_mapping(claim) and _non_empty_str(claim.get("claim_id"))
    }
    referenced: set[str] = set()
    for index, fragment in enumerate(fragments):
        if not _is_mapping(fragment):
            continue
        raw_ids = fragment.get("claim_ids")
        path = f"fragments[{index}].claim_ids"
        if not _is_sequence(raw_ids):
            result.issues.append(
                ContractValidationIssue(
                    "missing_fragment_claim_mapping",
                    path,
                    "every non-skip fragment must reference at least one admitted claim_id",
                )
            )
            continue
        normalized = [str(value) for value in raw_ids]
        if len(normalized) != len(set(normalized)):
            result.issues.append(
                ContractValidationIssue(
                    "duplicate_fragment_claim_mapping",
                    path,
                    "fragment claim_ids must be duplicate-free",
                )
            )
        unknown = sorted(set(normalized) - claim_ids)
        if unknown:
            result.issues.append(
                ContractValidationIssue(
                    "unknown_fragment_claim_mapping",
                    path,
                    "fragment references unknown claim_id(s): " + ", ".join(unknown),
                )
            )
        referenced.update(set(normalized) & claim_ids)
    missing = sorted(claim_ids - referenced)
    if missing:
        result.issues.append(
            ContractValidationIssue(
                "claim_without_fragment_mapping",
                "structured_output.claims",
                "admitted claim(s) have no fragment mapping: " + ", ".join(missing),
            )
        )


def canonical_fragment_payload(fragment: Any) -> dict[str, Any]:
    fields = (
        "form",
        "title",
        "frontmatter",
        "background",
        "core_content",
        "boundaries",
        "anti_patterns",
        "related_concepts",
        "claim_ids",
        "relations",
    )
    if _is_mapping(fragment):
        return {field: fragment[field] for field in fields if field in fragment}
    return {
        "form": getattr(fragment, "form", ""),
        "title": getattr(fragment, "title", ""),
        "frontmatter": getattr(fragment, "frontmatter", {}),
        "background": getattr(fragment, "background", ""),
        "core_content": getattr(fragment, "core_content", ""),
        "boundaries": getattr(fragment, "boundaries", {}),
        "anti_patterns": getattr(fragment, "anti_patterns", []),
        "related_concepts": getattr(fragment, "related_concepts", []),
        "claim_ids": getattr(fragment, "claim_ids", []),
        "relations": getattr(fragment, "relations", []),
    }


def _validate_user_behavior_intent(
    result: ContractValidationResult,
    value: Any,
    source_event_ids: set[str],
    distill_intent: str,
    source_authority_catalog: SourceAuthorityCatalog | None,
) -> None:
    path = "user_behavior_intent"
    if value in (None, "") and distill_intent == "skip":
        return
    if not _is_mapping(value):
        result.issues.append(
            ContractValidationIssue(
                "missing_user_behavior_intent",
                path,
                "non-skip distill output must include user_behavior_intent",
            )
        )
        return

    _require_enum(result, value, "content_source", CONTENT_SOURCE_VALUES, path)
    _require_enum(result, value, "user_intent_signal", USER_INTENT_SIGNAL_VALUES, path)
    intent_hypothesis = _require_non_empty_str(result, value, "intent_hypothesis", path)
    intent_status = _require_enum(result, value, "intent_status", INTENT_STATUS_VALUES, path)
    _require_non_empty_str(result, value, "behavior_summary", path)
    confidence = _validate_confidence_value(
        result,
        value.get("intent_confidence"),
        f"{path}.intent_confidence",
        code="invalid_intent_confidence",
    )
    _validate_behavior_intent_evidence(
        result,
        value.get("intent_evidence"),
        f"{path}.intent_evidence",
        source_event_ids,
        required=True,
        source_authority_catalog=source_authority_catalog,
    )
    _validate_behavior_intent_evidence(
        result,
        value.get("intent_verification_events"),
        f"{path}.intent_verification_events",
        source_event_ids,
        required=False,
        require_status=True,
        source_authority_catalog=source_authority_catalog,
    )

    if intent_hypothesis == "unknown" and intent_status not in {"unverified", "unknown"}:
        result.issues.append(
            ContractValidationIssue(
                "unknown_intent_marked_verified",
                f"{path}.intent_status",
                "unknown intent_hypothesis must stay unverified or unknown",
            )
        )
    if intent_hypothesis == "unknown" and confidence > 0.3:
        result.issues.append(
            ContractValidationIssue(
                "unknown_intent_high_confidence",
                f"{path}.intent_confidence",
                "unknown intent_hypothesis must use intent_confidence at most 0.3",
            )
        )


def _validate_behavior_intent_evidence(
    result: ContractValidationResult,
    value: Any,
    path: str,
    source_event_ids: set[str],
    *,
    required: bool,
    require_status: bool = False,
    source_authority_catalog: SourceAuthorityCatalog | None = None,
) -> None:
    if required and not _is_sequence(value):
        result.issues.append(
            ContractValidationIssue(
                "missing_intent_evidence",
                path,
                "intent_evidence must be a non-empty list",
            )
        )
        return
    if not required and value in (None, ""):
        result.issues.append(
            ContractValidationIssue(
                "missing_intent_verification_events",
                path,
                "intent_verification_events must be a list, empty when unverified",
            )
        )
        return
    if not _is_sequence_allow_empty(value):
        result.issues.append(
            ContractValidationIssue("invalid_intent_events", path, "intent events must be a list")
        )
        return

    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not _is_mapping(item):
            result.issues.append(
                ContractValidationIssue(
                    "invalid_intent_event",
                    item_path,
                    "intent event must be a mapping",
                )
            )
            continue
        source_id = item.get("source_event_id")
        if not _non_empty_str(source_id):
            result.issues.append(
                ContractValidationIssue(
                    "missing_intent_event_source",
                    f"{item_path}.source_event_id",
                    "source_event_id is required",
                )
            )
        elif source_event_ids and source_id not in source_event_ids:
            result.issues.append(
                ContractValidationIssue(
                    "unknown_intent_event_source",
                    f"{item_path}.source_event_id",
                    "source_event_id must be listed in source_event_ids",
                )
            )
        _require_non_empty_str(result, item, "quote", item_path)
        _validate_source_authority_binding(
            result,
            item,
            item_path,
            source_id,
            source_authority_catalog,
        )
        if require_status:
            _require_enum(
                result,
                item,
                "status",
                INTENT_VERIFICATION_STATUS_VALUES,
                item_path,
            )


def _validate_source_authority_binding(
    result: ContractValidationResult,
    item: Mapping[str, Any],
    item_path: str,
    source_id: Any,
    source_authority_catalog: SourceAuthorityCatalog | None,
) -> None:
    authority_ref_id = item.get("source_authority_id")
    supplied_authority_fields = SYSTEM_SOURCE_AUTHORITY_FIELDS.intersection(item)
    if "source_authority_id" not in item:
        if supplied_authority_fields:
            result.issues.append(
                ContractValidationIssue(
                    "source_authority_identity_without_ref",
                    item_path,
                    "system source authority fields require source_authority_id",
                )
            )
        return
    if not _non_empty_str(authority_ref_id):
        result.issues.append(
            ContractValidationIssue(
                "invalid_source_authority_id",
                f"{item_path}.source_authority_id",
                "source_authority_id must be a non-empty string",
            )
        )
        return
    missing_authority_fields = sorted(SYSTEM_SOURCE_AUTHORITY_FIELDS.difference(item))
    if missing_authority_fields:
        result.issues.append(
            ContractValidationIssue(
                "unresolved_source_authority",
                f"{item_path}.source_authority_id",
                "source_authority_id must be resolved by the system before admission: "
                + ", ".join(missing_authority_fields),
            )
        )
        return
    if source_authority_catalog is None:
        return
    authority_entry = source_authority_catalog.get(authority_ref_id)
    if authority_entry is None:
        result.issues.append(
            ContractValidationIssue(
                "source_authority_unknown",
                f"{item_path}.source_authority_id",
                "source_authority_id is not present in the immutable input catalog",
            )
        )
        return
    if str(source_id or "") != authority_entry.source_event_id:
        result.issues.append(
            ContractValidationIssue(
                "source_authority_source_mismatch",
                f"{item_path}.source_event_id",
                "source_authority_id is not authorized for this source_event_id",
            )
        )
    if not authority_entry.matches_quote(item.get("quote")):
        result.issues.append(
            ContractValidationIssue(
                "source_authority_quote_mismatch",
                f"{item_path}.quote",
                "quote must occur in the exact system-bound authority span",
            )
        )
    expected_authority = authority_entry.resolved_evidence_payload()
    for field_name, expected_value in expected_authority.items():
        if item.get(field_name) != expected_value:
            result.issues.append(
                ContractValidationIssue(
                    "source_authority_catalog_mismatch",
                    f"{item_path}.{field_name}",
                    f"{field_name} must match the immutable source authority catalog",
                )
            )


def _validate_claim(
    result: ContractValidationResult,
    claim: Any,
    index: int,
    source_event_ids: set[str],
    artifact_catalog: ArtifactCatalog | None,
    source_authority_catalog: SourceAuthorityCatalog | None,
) -> None:
    path = f"claims[{index}]"
    if not _is_mapping(claim):
        result.issues.append(
            ContractValidationIssue("invalid_claim", path, "claim must be a mapping")
        )
        return

    _require_non_empty_str(result, claim, "claim_id", path)
    _require_non_empty_str(result, claim, "claim_text", path)
    claim_type = _require_enum(result, claim, "claim_type", CLAIM_TYPES, path)

    scope = claim.get("scope")
    if not _is_mapping(scope):
        result.issues.append(
            ContractValidationIssue("invalid_scope", f"{path}.scope", "scope must be a mapping")
        )
    elif not _non_empty_str(scope.get("domain")):
        result.issues.append(
            ContractValidationIssue(
                "missing_scope_domain",
                f"{path}.scope.domain",
                "domain is required",
            )
        )

    _validate_evidence(
        result,
        claim.get("evidence"),
        path,
        source_event_ids,
        artifact_catalog,
        source_authority_catalog,
    )

    relation = claim.get("relation_to_existing")
    relation_type = ""
    if not _is_mapping(relation):
        result.issues.append(
            ContractValidationIssue(
                "invalid_relation",
                f"{path}.relation_to_existing",
                "relation_to_existing must be a mapping",
            )
        )
    else:
        relation_type = _validate_relation(result, relation, path)
        if relation_type:
            result.relation_types.append(relation_type)

    action = _require_enum(result, claim, "recommended_action", RECOMMENDED_ACTIONS, path)
    if action:
        result.actions.append(action)
    _validate_cognitive_actions(result, claim, path, claim_type, action)

    _validate_confidence_value(result, claim.get("confidence"), f"{path}.confidence")

    if relation_type in CONFLICT_RELATIONS and action != "route_to_dispute":
        result.issues.append(
            ContractValidationIssue(
                "conflict_without_dispute_route",
                f"{path}.recommended_action",
                "conflicting claims must use route_to_dispute",
            )
        )
    if relation_type == "same" and action != "record_reinforcement":
        result.issues.append(
            ContractValidationIssue(
                "duplicate_without_reinforcement",
                f"{path}.recommended_action",
                "100% duplicate claims must use record_reinforcement",
            )
        )


def _validate_evidence(
    result: ContractValidationResult,
    evidence: Any,
    path: str,
    source_event_ids: set[str],
    artifact_catalog: ArtifactCatalog | None,
    source_authority_catalog: SourceAuthorityCatalog | None,
    *,
    field_name: str = "evidence",
) -> None:
    evidence_path = f"{path}.{field_name}"
    if not _is_sequence(evidence):
        result.issues.append(
            ContractValidationIssue(
                "missing_evidence",
                evidence_path,
                "evidence must be a non-empty list",
            )
        )
        return

    for index, item in enumerate(evidence):
        item_path = f"{evidence_path}[{index}]"
        if not _is_mapping(item):
            result.issues.append(
                ContractValidationIssue(
                    "invalid_evidence",
                    item_path,
                    "evidence item must be a mapping",
                )
            )
            continue
        source_id = item.get("source_event_id")
        if not _non_empty_str(source_id):
            result.issues.append(
                ContractValidationIssue(
                    "missing_evidence_source",
                    f"{item_path}.source_event_id",
                    "source_event_id is required",
                )
            )
        elif source_event_ids and source_id not in source_event_ids:
            result.issues.append(
                ContractValidationIssue(
                    "unknown_evidence_source",
                    f"{item_path}.source_event_id",
                    "source_event_id must be listed in source_event_ids",
                )
            )
        _require_non_empty_str(result, item, "quote", item_path)
        _validate_source_authority_binding(
            result,
            item,
            item_path,
            source_id,
            source_authority_catalog,
        )
        artifact_ref_id = item.get("artifact_ref_id")
        supplied_system_fields = SYSTEM_ARTIFACT_FIELDS.intersection(item)
        if "artifact_ref_id" not in item:
            if supplied_system_fields:
                result.issues.append(
                    ContractValidationIssue(
                        "artifact_identity_without_ref",
                        item_path,
                        "system artifact identity requires artifact_ref_id",
                    )
                )
            continue
        if not _non_empty_str(artifact_ref_id):
            result.issues.append(
                ContractValidationIssue(
                    "invalid_artifact_ref_id",
                    f"{item_path}.artifact_ref_id",
                    "artifact_ref_id must be a non-empty string",
                )
            )
            continue
        missing_system_fields = sorted(SYSTEM_ARTIFACT_FIELDS.difference(item))
        if missing_system_fields:
            result.issues.append(
                ContractValidationIssue(
                    "unresolved_artifact_ref",
                    f"{item_path}.artifact_ref_id",
                    "artifact_ref_id must be resolved by the system before admission: "
                    + ", ".join(missing_system_fields),
                )
            )
            continue

        artifact_uri = item.get("artifact_uri")
        error = artifact_uri_error(artifact_uri)
        if error:
            result.issues.append(
                ContractValidationIssue(
                    "invalid_artifact_uri",
                    f"{item_path}.artifact_uri",
                    error,
                )
            )
        artifact_type = _require_enum(
            result,
            item,
            "artifact_type",
            set(ALLOWED_ARTIFACT_TYPES),
            item_path,
        )
        _require_non_empty_str(result, item, "artifact_summary", item_path)
        digest = normalize_sha256(item.get("artifact_sha256"))
        if not digest:
            result.issues.append(
                ContractValidationIssue(
                    "invalid_artifact_sha256",
                    f"{item_path}.artifact_sha256",
                    "artifact_sha256 must be a complete SHA-256 digest",
                )
            )
        if item.get("artifact_acl") != LOCAL_USER_ACL:
            result.issues.append(
                ContractValidationIssue(
                    "artifact_ref_unauthorized",
                    f"{item_path}.artifact_acl",
                    "artifact ACL must be local_user",
                )
            )
        if not error and artifact_type:
            identity = parse_artifact_uri(artifact_uri)
            if identity.artifact_type != artifact_type:
                result.issues.append(
                    ContractValidationIssue(
                        "artifact_type_mismatch",
                        f"{item_path}.artifact_type",
                        "artifact_type must match the canonical artifact URI",
                    )
                )
            if identity.identity_kind != "content" or identity.sha256 != digest:
                result.issues.append(
                    ContractValidationIssue(
                        "artifact_hash_mismatch",
                        f"{item_path}.artifact_sha256",
                        "artifact URI and SHA-256 must identify the same content",
                    )
                )

        if artifact_catalog is None:
            continue
        entry = artifact_catalog.get(str(artifact_ref_id))
        if entry is None:
            result.issues.append(
                ContractValidationIssue(
                    "artifact_ref_unknown",
                    f"{item_path}.artifact_ref_id",
                    "artifact_ref_id is not present in the immutable input catalog",
                )
            )
            continue
        if str(source_id or "") not in entry.source_event_ids:
            result.issues.append(
                ContractValidationIssue(
                    "artifact_ref_source_mismatch",
                    f"{item_path}.source_event_id",
                    "artifact_ref_id is not authorized for this source_event_id",
                )
            )
        expected = entry.resolved_evidence_payload()
        for field_name, expected_value in expected.items():
            if item.get(field_name) != expected_value:
                result.issues.append(
                    ContractValidationIssue(
                        "artifact_catalog_mismatch",
                        f"{item_path}.{field_name}",
                        f"{field_name} must match the immutable artifact catalog",
                    )
                )


def _validate_cognitive_actions(
    result: ContractValidationResult,
    claim: Mapping[str, Any],
    claim_path: str,
    claim_type: str,
    recommended_action: str,
) -> list[str]:
    actions_path = f"{claim_path}.cognitive_actions"
    raw_actions = claim.get("cognitive_actions")
    requires_action = (
        claim_type in COGNITIVE_ACTION_REQUIRED_CLAIM_TYPES
        and recommended_action != "skip"
    )
    if raw_actions in (None, ""):
        if requires_action:
            result.issues.append(
                ContractValidationIssue(
                    "missing_cognitive_actions",
                    actions_path,
                    "high-value claims must declare at least one cognitive action",
                )
            )
        return []
    if not _is_sequence(raw_actions):
        result.issues.append(
            ContractValidationIssue(
                "invalid_cognitive_actions",
                actions_path,
                "cognitive_actions must be a non-empty list",
            )
        )
        return []

    actions: list[str] = []
    for index, item in enumerate(raw_actions):
        if not _non_empty_str(item) or str(item) not in COGNITIVE_ACTIONS:
            result.issues.append(
                ContractValidationIssue(
                    "invalid_cognitive_action",
                    f"{actions_path}[{index}]",
                    f"cognitive action {item!r} must be one of: "
                    f"{', '.join(sorted(COGNITIVE_ACTIONS))}",
                )
            )
            continue
        actions.append(str(item))
    if requires_action and not actions:
        result.issues.append(
            ContractValidationIssue(
                "missing_cognitive_actions",
                actions_path,
                "high-value claims must declare at least one cognitive action",
            )
        )
    return actions


def _validate_relation(
    result: ContractValidationResult,
    relation: Mapping[str, Any],
    claim_path: str,
) -> str:
    path = f"{claim_path}.relation_to_existing"
    relation_type = _require_enum(result, relation, "type", RELATION_TYPES, path) or ""
    target_pages = relation.get("target_pages")

    if relation_type == "new":
        return relation_type

    if not _is_sequence(target_pages):
        result.issues.append(
            ContractValidationIssue(
                "missing_relation_targets",
                f"{path}.target_pages",
                "target_pages must be a non-empty list for non-new relations",
            )
        )

    if relation_type in DELTA_RELATIONS:
        _require_non_empty_str(result, relation, "delta_text", path)
    elif relation_type in CONFLICT_RELATIONS:
        _require_non_empty_str(result, relation, "delta_text", path)
        _require_non_empty_str(result, relation, "reason", path)
    elif relation_type == "same":
        reason = relation.get("reason", "")
        if "100%" not in str(reason) and "完全重复" not in str(reason):
            result.issues.append(
                ContractValidationIssue(
                    "same_without_exact_reason",
                    f"{path}.reason",
                    "same requires an explicit 100% duplicate reason",
                )
            )

    return relation_type


def _require_equal(
    result: ContractValidationResult,
    payload: Mapping[str, Any],
    key: str,
    expected: str,
    prefix: str = "",
) -> None:
    path = f"{prefix}.{key}" if prefix else key
    if payload.get(key) != expected:
        result.issues.append(
            ContractValidationIssue(
                f"invalid_{key}",
                path,
                f"{key} must be {expected}",
            )
        )


def _require_non_empty_str(
    result: ContractValidationResult,
    payload: Mapping[str, Any],
    key: str,
    prefix: str = "",
) -> str:
    path = f"{prefix}.{key}" if prefix else key
    value = payload.get(key)
    if not _non_empty_str(value):
        result.issues.append(
            ContractValidationIssue(f"missing_{key}", path, f"{key} is required")
        )
        return ""
    return str(value)


def _require_non_empty_str_list(
    result: ContractValidationResult,
    payload: Mapping[str, Any],
    key: str,
    prefix: str = "",
) -> list[str]:
    path = f"{prefix}.{key}" if prefix else key
    value = payload.get(key)
    if not _is_sequence(value) or not all(_non_empty_str(item) for item in value):
        result.issues.append(
            ContractValidationIssue(
                f"missing_{key}",
                path,
                f"{key} must be a non-empty list of strings",
            )
        )
        return []
    return [str(item) for item in value]


def _require_enum(
    result: ContractValidationResult,
    payload: Mapping[str, Any],
    key: str,
    allowed: set[str],
    prefix: str = "",
) -> str:
    path = f"{prefix}.{key}" if prefix else key
    value = payload.get(key)
    if not _non_empty_str(value) or str(value) not in allowed:
        result.issues.append(
            ContractValidationIssue(
                f"invalid_{key}",
                path,
                f"{key} must be one of: {', '.join(sorted(allowed))}",
            )
        )
        return ""
    return str(value)


def _validate_confidence_value(
    result: ContractValidationResult,
    value: Any,
    path: str,
    *,
    code: str = "invalid_confidence",
) -> float:
    if isinstance(value, Real) and not isinstance(value, bool):
        confidence_value = float(value)
    else:
        confidence_value = -1.0
    if not 0.0 <= confidence_value <= 1.0:
        result.issues.append(
            ContractValidationIssue(
                code,
                path,
                "confidence must be a number between 0 and 1",
            )
        )
    return confidence_value


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_mapping(value: Any) -> TypeGuard[Mapping[str, Any]]:
    return isinstance(value, Mapping)


def _is_sequence(value: Any) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and bool(value)


def _is_sequence_allow_empty(value: Any) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
