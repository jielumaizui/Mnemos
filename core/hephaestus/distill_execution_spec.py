"""Canonical execution identity for resumable chunked distillation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from core.document_import import file_sha256
from core.hephaestus.distill_input_spec import (
    OUTPUT_CONTRACT_VERSION,
    ExtractionRequest,
    PreparedExtractionPrompt,
)

SCHEMA_VERSION = "mnemos.distill_execution_spec.v2"

# These values can change extraction, correction, fragment validation, merge,
# or final admission semantics. Paths and operational scheduling values are
# intentionally excluded so unrelated deployment changes keep cache hits.
EXECUTION_CONFIG_KEYS = (
    "distill.token_budget_total",
    "distill.token_budget_output_reserve",
    "distill.token_budget_system_pct",
    "distill.token_budget_context_pct",
    "distill.token_budget_content_pct",
    "distill.chunk_std_factor",
    "distill.chunk_total_factor",
    "distill.chunk_size_factor",
    "distill.effective_max_tokens",
    "distill.per_message_token_limit",
    "distill.incremental_batch_turns",
    "distill.llm_cost_budget_per_session",
    "distill.content_formatter_max_tokens",
    "distill.response_tokens",
    "distill.dynamic_response_tokens_enabled",
    "distill.response_tokens_default",
    "distill.response_tokens_medium",
    "distill.response_tokens_long",
    "distill.response_tokens_retry_max",
    "distill.response_tokens_short_input_threshold",
    "distill.response_tokens_medium_input_threshold",
    "distill.response_tokens_merge_fragment_threshold",
    "distill.extract_correction_retries",
    "distill.fragment_boundary_chars",
    "distill.min_session_fragment_pass_ratio",
    "distill.auto_expression_formatting",
    "distill.fragment_merge_threshold",
    "distill.enable_llm_fragment_merge",
    "distill.structured_output_contract.enforce",
    "distill.action_router.enabled",
    "scoring.domain_scorers_enabled",
    "quality_gate.enabled",
    "quality_gate.base_threshold",
    "quality_gate.review_margin",
    "quality_gate.cognitive_value.enabled",
    "quality_gate.cognitive_value.base_threshold",
    "quality_gate.cognitive_value.review_margin",
)

_SECRET_FIELDS = {
    "api_key",
    "api_key_source",
    "api_key_env",
    "token",
    "secret",
    "password",
    "credential",
    "bearer",
}


def _sha256(value: bytes | str) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _file_hash(path: Path) -> str:
    return "sha256:" + file_sha256(path)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                str(key): _freeze(child)
                for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _safe_identity(value: Any) -> Any:
    """Remove credentials while retaining output-affecting backend identity."""
    if isinstance(value, Mapping):
        return {
            str(key): _safe_identity(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key).lower() not in _SECRET_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_safe_identity(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return f"{type(value).__module__}.{type(value).__qualname__}"


def _component_identity(component: Any) -> dict[str, Any]:
    if component is None:
        raise TypeError("distillation component must expose checkpoint_identity()")
    hook = getattr(component, "checkpoint_identity", None)
    if not callable(hook):
        raise TypeError(
            f"{type(component).__module__}.{type(component).__qualname__} "
            "must expose checkpoint_identity()"
        )
    return {
        "component": f"{type(component).__module__}.{type(component).__qualname__}",
        "identity": _safe_identity(hook()),
    }


def _model_ids(identity: Mapping[str, Any]) -> tuple[str, ...]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            provider = str(value.get("provider", "") or "").strip()
            model = str(value.get("model", "") or "").strip()
            if model:
                found.add(f"{provider}/{model}" if provider else model)
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(identity)
    return tuple(sorted(found))


@lru_cache(maxsize=1)
def _output_schema_hash() -> str:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "prompts"
        / "distill"
        / "_output_schemas"
        / "extract.json"
    )
    return _file_hash(schema_path)


@lru_cache(maxsize=1)
def _extractor_contract_hash() -> str:
    base = Path(__file__).resolve().parent
    paths = (
        base / "distill_execution_spec.py",
        base / "distill_backend.py",
        base / "distillation_engine.py",
        base / "chunked_extraction.py",
        base / "chunk_aggregate.py",
        base / "distillation_extractor.py",
        base / "distillation_llm.py",
        base / "distillation_quality.py",
        base / "distillation_contract.py",
        base / "distill_input_spec.py",
        base / "distillation_models.py",
        base / "distillation_json.py",
        base / "prompt_builder.py",
        base / "response_budget.py",
        base / "tokenizer.py",
        base.parent / "evidence" / "artifact_catalog.py",
        base.parent / "evidence" / "source_authority.py",
        base.parent / "evidence" / "artifact_uri.py",
        base.parent / "kia" / "assertion_extractor.py",
    )
    payload = "\0".join(f"{path.name}:{_file_hash(path)}" for path in paths)
    return _sha256(payload)


def _merge_contract_hash(component: Any) -> str:
    # Per-chunk checkpoint reuse is only valid when the deterministic
    # session-level aggregator is unchanged too.  The aggregate derives the
    # final routable root from every local output, so treating it as outside
    # the merge contract would let an old chunk generation feed new semantics.
    from core.hephaestus.chunk_aggregate import ChunkEpisodeMerger

    payload = {
        "source_hash": _fragment_merger_source_hash(),
        "identity": _component_identity(component),
        "chunk_aggregate": ChunkEpisodeMerger().checkpoint_identity(),
    }
    return _sha256(_canonical_json(payload))


@lru_cache(maxsize=1)
def _fragment_merger_source_hash() -> str:
    path = Path(__file__).resolve().with_name("fragment_merger.py")
    return _file_hash(path)


@dataclass(frozen=True)
class DistillExecutionSpec:
    """Immutable, serializable identity of output-affecting distillation inputs."""

    input_contract_version: str
    input_spec_hash: str
    output_admission_contract_version: str
    prompt_version: str
    prompt_hash: str
    output_schema_hash: str
    extractor_contract_hash: str
    backend_hash: str
    merge_contract_hash: str
    model_ids: tuple[str, ...]
    config_values: Mapping[str, Any]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_values", _freeze(self.config_values))
        object.__setattr__(self, "model_ids", tuple(sorted(self.model_ids)))

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_contract_version": self.input_contract_version,
            "input_spec_hash": self.input_spec_hash,
            "output_admission_contract_version": self.output_admission_contract_version,
            "prompt_version": self.prompt_version,
            "prompt_hash": self.prompt_hash,
            "output_schema_hash": self.output_schema_hash,
            "extractor_contract_hash": self.extractor_contract_hash,
            "backend_hash": self.backend_hash,
            "merge_contract_hash": self.merge_contract_hash,
            "model_ids": list(self.model_ids),
            "config_values": _plain(self.config_values),
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.canonical_payload())

    @property
    def execution_spec_hash(self) -> str:
        return _sha256(self.canonical_json())

    def diff_fields(self, other: "DistillExecutionSpec") -> tuple[str, ...]:
        current = self.canonical_payload()
        previous = other.canonical_payload()
        changed: list[str] = []
        for key in sorted(set(current) | set(previous)):
            if current.get(key) == previous.get(key):
                continue
            if key == "config_values":
                left = current.get(key, {})
                right = previous.get(key, {})
                changed.extend(
                    f"config_values.{item}"
                    for item in sorted(set(left) | set(right))
                    if left.get(item) != right.get(item)
                )
            else:
                changed.append(key)
        return tuple(changed)

    @classmethod
    def from_json(cls, raw: str) -> "DistillExecutionSpec":
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported distill execution spec")
        return cls(
            input_contract_version=str(data["input_contract_version"]),
            input_spec_hash=str(data["input_spec_hash"]),
            output_admission_contract_version=str(
                data["output_admission_contract_version"]
            ),
            prompt_version=str(data["prompt_version"]),
            prompt_hash=str(data["prompt_hash"]),
            output_schema_hash=str(data["output_schema_hash"]),
            extractor_contract_hash=str(data["extractor_contract_hash"]),
            backend_hash=str(data["backend_hash"]),
            merge_contract_hash=str(data["merge_contract_hash"]),
            model_ids=tuple(str(item) for item in data.get("model_ids", [])),
            config_values=data.get("config_values", {}),
            schema_version=str(data["schema_version"]),
        )


def build_distill_execution_spec(
    *,
    prompt: str,
    cfg: Any,
    extractor_backend: Any,
    merge_component: Any,
    input_contract_version: str,
    input_spec_hash: str,
    output_admission_contract_version: str,
    prompt_version: str,
) -> DistillExecutionSpec:
    """Build the canonical execution spec without persisting credentials."""
    backend_identity = _component_identity(extractor_backend)
    config_values = {key: cfg.get(key) for key in EXECUTION_CONFIG_KEYS}
    return DistillExecutionSpec(
        input_contract_version=input_contract_version,
        input_spec_hash=input_spec_hash,
        output_admission_contract_version=output_admission_contract_version,
        prompt_version=prompt_version,
        prompt_hash=_sha256(prompt),
        output_schema_hash=_output_schema_hash(),
        extractor_contract_hash=_extractor_contract_hash(),
        backend_hash=_sha256(_canonical_json(backend_identity)),
        merge_contract_hash=_merge_contract_hash(merge_component),
        model_ids=_model_ids(backend_identity),
        config_values=config_values,
    )


def prepare_chunk_execution_spec(
    *,
    extractor: Any,
    merge_component: Any,
    cfg: Any,
    request: ExtractionRequest,
    input_contract_version: str,
    prompt_version: str,
) -> tuple[PreparedExtractionPrompt, DistillExecutionSpec]:
    """Render once, then bind the exact chunk prompt to its execution spec."""
    try:
        prepared = extractor.prepare_prompt(request)
    except AttributeError as exc:
        raise TypeError("extractor must expose prepare_prompt(request)") from exc
    if not isinstance(prepared, PreparedExtractionPrompt):
        raise TypeError("extractor prepare_prompt must return PreparedExtractionPrompt")
    prepared.assert_matches(request)
    spec = build_distill_execution_spec(
        prompt=prepared.text,
        cfg=cfg,
        extractor_backend=extractor.backend,
        merge_component=merge_component,
        input_contract_version=input_contract_version,
        input_spec_hash=request.input_spec.input_spec_hash,
        output_admission_contract_version=OUTPUT_CONTRACT_VERSION,
        prompt_version=prompt_version,
    )
    return prepared, spec
