"""Canonical JSON, hashes, and scalar validation primitives."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

from core.cognitive.state_contract_schema import _SHA256_PATTERN


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    """Return the one JSON identity representation used by state persistence."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def sha256_json(value: Any) -> str:
    raw = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _required_sha256(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} must be an exact SHA-256 identity")
    return normalized


def _finite_float(value: Any, field_name: str) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be a finite number")
    return normalized


def _positive_finite(value: Any, field_name: str) -> float:
    normalized = _finite_float(value, field_name)
    if normalized <= 0.0:
        raise ValueError(f"{field_name} must be a positive finite number")
    return normalized


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_source_span_id(value: Any) -> str:
    span_id = _required_text(value, "source_span_id")
    if not span_id.startswith("raw-span:"):
        raise ValueError("calibration_record source span identity is invalid")
    identity, span_start, span_end = span_id.rsplit(":", 2)
    try:
        start = int(span_start)
        end = int(span_end)
    except ValueError as exc:
        raise ValueError("calibration_record source span bounds are invalid") from exc
    if not identity.removeprefix("raw-span:") or start < 0 or end <= start:
        raise ValueError("calibration_record source span bounds are invalid")
    return span_id


def _string_tuple(value: Sequence[Any] | None, field_name: str) -> tuple[str, ...]:
    if value is None or isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be a sequence")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result):
        raise ValueError(f"{field_name} contains a blank item")
    return result


def _exact_mapping(value: Any, fields: set[str], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{field_name} is invalid")
    return value


def _validate_decision_execution_specs(payload: Mapping[str, Any]) -> None:
    model = payload["model_spec"]
    prompt = payload["prompt_spec"]
    if not isinstance(model, Mapping) or not isinstance(prompt, Mapping):
        raise ValueError("decision_trace model/prompt spec is invalid")
    for field_name in ("provider", "model", "route", "version"):
        _required_text(model.get(field_name), f"decision_trace model_spec.{field_name}")
    _required_sha256(model.get("config_hash"), "decision_trace model config_hash")
    _required_text(prompt.get("prompt_id"), "decision_trace prompt_spec.prompt_id")
    _required_sha256(prompt.get("prompt_hash"), "decision_trace prompt_hash")
    _required_sha256(prompt.get("schema_hash"), "decision_trace schema_hash")
    tool_names: set[str] = set()
    for tool in payload["tool_specs"]:
        if not isinstance(tool, Mapping):
            raise ValueError("decision_trace tool spec is invalid")
        name = _required_text(tool.get("name"), "decision_trace tool name")
        if name in tool_names:
            raise ValueError("decision_trace tool names must be unique")
        tool_names.add(name)
        _required_text(tool.get("version"), "decision_trace tool version")
        _required_sha256(tool.get("code_hash"), "decision_trace tool code_hash")
    window = payload["evaluation_window"]
    if not isinstance(window, Mapping):
        raise ValueError("decision_trace evaluation window is invalid")
    if _parse_timestamp(window.get("ends_at"), "decision_trace ends_at") <= (
        _parse_timestamp(window.get("starts_at"), "decision_trace starts_at")
    ):
        raise ValueError("decision_trace evaluation window is invalid")
    approval = payload["approval"]
    if not isinstance(approval, Mapping) or approval.get("decision") != payload["decision_state"]:
        raise ValueError("decision_trace approval state mismatch")


def _contains_prohibited_reasoning(value: Any) -> bool:
    prohibited = {
        "chain_of_thought",
        "scratchpad",
        "private_reasoning",
        "hidden_reasoning",
        "reasoning_trace",
    }
    if isinstance(value, Mapping):
        return any(
            str(key).lower() in prohibited or _contains_prohibited_reasoning(child)
            for key, child in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_prohibited_reasoning(child) for child in value)
    return False


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)
