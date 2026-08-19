"""Pure normalization helpers for the canonical prediction ledger."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.access_policy import PrincipalEnvelope
from core.cognitive.access_control import (
    derive_strictest_cognitive_access,
    validate_cognitive_access_envelope,
)
from core.cognitive.state_contract import canonical_json, sha256_json
from core.evidence.artifact_catalog import require_sha256_file


def file_sha256(path: Path) -> str:
    """Return the canonical prefixed SHA-256 for a required artifact."""

    return "sha256:" + require_sha256_file(path)


def normalized_route_payload(route_facts: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize system-owned predictive route facts."""

    required = {
        "event_id", "source", "subject", "channel", "target", "decision",
        "reason", "requested_level", "delivered_level", "profile",
        "cooldown_key", "trust_decision_id", "trust_score", "task_fit_score",
        "interruption_cost", "evidence_refs", "created_at", "request_binding",
        "metadata", "source_access_control",
    }
    missing = sorted(required - set(route_facts))
    if missing:
        raise ValueError("predictive route facts missing fields: " + ",".join(missing))
    route = {key: route_facts[key] for key in required}
    route["scope_type"] = str(route_facts.get("scope_type") or "topic")
    route["scope_id"] = str(
        route_facts.get("scope_id")
        or route_facts.get("cooldown_key")
        or route_facts["subject"]
    )
    if not isinstance(route["request_binding"], Mapping) or set(
        route["request_binding"]
    ) != {"target_ref", "input_hash"}:
        raise ValueError("route request binding is invalid")
    route["request_binding"] = {
        "target_ref": required_text(
            route["request_binding"].get("target_ref"),
            "route request target_ref",
        ),
        "input_hash": required_text(
            route["request_binding"].get("input_hash"),
            "route request input_hash",
        ),
    }
    if not route["request_binding"]["input_hash"].startswith("sha256:"):
        raise ValueError("route request input hash is invalid")
    if not isinstance(route["metadata"], Mapping):
        raise ValueError("route metadata must be an object")
    route["metadata"] = dict(route["metadata"])
    route["source_access_control"] = validate_cognitive_access_envelope(
        route["source_access_control"]
    )
    for field_name in (
        "event_id", "source", "subject", "channel", "decision", "reason",
        "requested_level", "delivered_level", "profile", "cooldown_key",
        "trust_decision_id", "created_at", "scope_type", "scope_id",
    ):
        route[field_name] = required_text(route[field_name], f"route {field_name}")
    route["target"] = str(route["target"] or "")
    if not isinstance(route["evidence_refs"], Sequence) or isinstance(
        route["evidence_refs"], (str, bytes)
    ):
        raise ValueError("route evidence_refs must be a sequence")
    route["evidence_refs"] = sorted(
        set(str(value) for value in route["evidence_refs"] if str(value))
    )
    for field_name in ("trust_score", "task_fit_score", "interruption_cost"):
        route[field_name] = float(route[field_name])
    timestamp(route["created_at"])
    if route["decision"] not in {"deliver", "suppress"}:
        raise ValueError("route decision is invalid")
    parsed = json.loads(canonical_json(route))
    if not isinstance(parsed, dict):
        raise RuntimeError("canonical route payload did not remain an object")
    return parsed


def terminal_matches(payload: Mapping[str, Any], proposed: Mapping[str, Any]) -> bool:
    """Return whether a terminal payload is an exact semantic replay."""

    outcome = proposed.get("outcome")
    expected_outcome = {
        "revision_id": outcome.revision_id if outcome is not None else "",
        "payload_hash": outcome.payload_hash if outcome is not None else "",
    }
    return bool(
        payload["terminal"]["state"] == proposed["state"]
        and payload["terminal"]["reason"] == proposed["reason"]
        and payload["outcome_ref"] == expected_outcome
        and payload["exposure"] == {
            "status": proposed["exposure_status"],
            "evidence_refs": list(proposed["exposure_refs"]),
        }
        and payload["attribution"] == {
            "method": proposed["attribution_method"],
            "competing_causes": list(proposed["competing_causes"]),
        }
        and payload["error"] == {
            "kind": "categorical_miss" if proposed["error"] is not None else "none",
            "value": proposed["error"],
        }
    )


def prediction_access_control(
    *,
    source_access_control: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive PredictionRecord access from the exact admitted source ACL."""

    source = validate_cognitive_access_envelope(source_access_control)
    derived = derive_strictest_cognitive_access(
        (source,),
        owner_principal_id=str(source["owner"]["principal_id"]),
        owner_agent=str(source["owner"]["agent"]),
        scope_type=str(source["scope"]["scope_type"]),
        scope_id=str(source["scope"]["scope_id"]),
        purposes=(
            "cognitive_state_read",
            "cognitive_state_write",
            "prediction_read",
        ),
        retention_policy="prediction_ledger",
    )
    if derived["scope"]["resolution"] != "resolved":
        raise PermissionError("prediction source ACL cannot authorize a resolved object")
    return derived


def system_principal() -> PrincipalEnvelope:
    """Return the daemon principal used for system-owned prediction writes."""

    return PrincipalEnvelope(
        principal_id="system:prediction-ledger",
        agent="mnemos",
        host_kind="daemon",
        capability_id="prediction-ledger-lifecycle",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
    )


def window_hours(config: Any | None, default: int) -> int:
    """Resolve and validate the global predictive evaluation window."""

    raw: Any = default
    if config is not None:
        try:
            raw = config.get("prediction.predictive_delivery_window_hours", default)
        except (AttributeError, TypeError):
            raw = default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("prediction window hours must be an integer") from exc
    if value <= 0 or value > 24 * 365:
        raise ValueError("prediction window hours is outside the supported range")
    return value


def route_disposition(decision: str, delivered_level: str) -> str:
    """Map a trust decision and delivery level to the sealed disposition."""

    if decision == "suppress":
        return "suppress"
    if decision != "deliver":
        raise ValueError("predictive route decision is invalid")
    return "silent" if delivered_level == "silent" else "deliver"


def score_band(trust_score: float, task_fit_score: float) -> str:
    """Classify route scores without presenting them as probabilities."""

    if trust_score >= 0.75 and task_fit_score >= 0.70:
        return "high"
    if trust_score >= 0.50 and task_fit_score >= 0.40:
        return "medium"
    return "low"


def prediction_id(value: Any) -> str:
    """Validate a canonical PredictionRecord object identifier."""

    normalized = required_text(value, "prediction_id")
    if not normalized.startswith("prediction-") or len(normalized) != 43:
        raise ValueError("prediction_id is invalid")
    return normalized


def required_text(value: Any, field_name: str) -> str:
    """Return required non-empty text or raise a field-specific error."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def timestamp(value: datetime | str) -> datetime:
    """Parse an aware timestamp and normalize it to UTC."""

    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def now() -> str:
    """Return the current UTC timestamp in canonical ISO form."""

    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    """Return a canonical SHA-256 digest for immutable prediction inputs."""

    return sha256_json(value).split(":", 1)[1]
