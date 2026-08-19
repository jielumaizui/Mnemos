"""Independent structural validator for canonical user reaction revisions."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from core.cognitive.access_control import validate_cognitive_access_envelope


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REACTION_ID = re.compile(r"reaction-[0-9a-f]{32}\Z")
_SOURCE_CHANNELS = frozenset(
    {
        "predictive_push",
        "context_search",
        "reflection",
        "retrospective",
        "dialog_decision_push",
        "dialog_reminder",
        "delivery_feedback",
        "outcome_compatibility",
    }
)
_EXPLICIT_KINDS = frozenset(
    {
        "accept",
        "ignore",
        "dismiss",
        "inaccurate",
        "outdated",
        "accurate",
        "useful",
        "insightful",
        "irrelevant",
    }
)
_FACTS = {
    "accept": "accepted",
    "ignore": "ignored",
    "dismiss": "dismissed",
    "inaccurate": "inaccurate",
    "outdated": "outdated",
    "accurate": "accurate",
    "useful": "useful",
    "insightful": "insightful",
    "irrelevant": "irrelevant",
    "opened": "opened",
    "clicked": "clicked",
    "read": "read",
    "dwell_observed": "dwell_seconds",
    "repeated_query": "repeated_query",
    "no_click": "no_click",
    "silence_window_closed": "silence_window_closed",
}
_FIELDS = frozenset(
    {
        "schema_version",
        "reaction_id",
        "revision_state",
        "reaction_input_hash",
        "source_event_ref",
        "observed_at",
        "recorded_at",
        "supersedes_event_id",
        "correction_of_event_id",
        "principal_ref",
        "scope",
        "source_channel",
        "authority_class",
        "subject_ref",
        "decision_ref",
        "prediction_ref",
        "action_ref",
        "delivery_ref",
        "display_ref",
        "search_ref",
        "interaction",
        "evidence",
        "observation_window",
        "exposure",
        "competing_causes",
        "source_completeness",
        "attribution",
        "downstream",
        "correction",
        "access_control",
    }
)


def independent_reaction_payload_valid(
    payload: Mapping[str, Any],
    *,
    feedback_targets: Sequence[str],
    canonical_revisions_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
    """Validate all causal fields without importing feedback writer helpers."""

    try:
        if set(payload) != _FIELDS:
            return False
        if (
            payload.get("schema_version") != "mnemos.user_reaction_event.v1"
            or not _REACTION_ID.fullmatch(str(payload.get("reaction_id") or ""))
            or payload.get("revision_state") not in {"recorded", "corrected"}
        ):
            return False
        observed_at = _timestamp(payload.get("observed_at"))
        recorded_at = _timestamp(payload.get("recorded_at"))
        if recorded_at < observed_at:
            return False
        if not _source_ref_valid(payload.get("source_event_ref")):
            return False
        subject = payload.get("subject_ref")
        if not _required_mapping(subject, {"type", "id"}):
            return False
        principal = payload.get("principal_ref")
        if (
            not isinstance(principal, Mapping)
            or set(principal) != {"principal_id", "agent", "authorization_ref"}
            or any(
                not str(principal[field]).strip()
                for field in ("principal_id", "agent", "authorization_ref")
            )
        ):
            return False
        scope = payload.get("scope")
        if (
            not isinstance(scope, Mapping)
            or set(scope) != {"type", "id", "project", "session_id"}
            or not str(scope["type"]).strip()
            or not str(scope["id"]).strip()
            or payload.get("source_channel") not in _SOURCE_CHANNELS
        ):
            return False
        access_control = payload.get("access_control")
        if not isinstance(access_control, Mapping):
            return False
        access = validate_cognitive_access_envelope(
            access_control,
            expected_scope_type=str(scope["type"]),
            expected_scope_id=str(scope["id"]),
        )
        if (
            str(access["owner"]["principal_id"]) != str(principal["principal_id"])
            or str(access["owner"]["agent"]).lower()
            != str(principal["agent"]).lower()
            or str(access["scope"]["project"]) != str(scope["project"]).lower()
            or str(access["scope"]["session_id"]) != str(scope["session_id"])
        ):
            return False
        if not all(
            _entity_ref_valid(payload.get(field))
            for field in ("decision_ref", "prediction_ref", "action_ref")
        ):
            return False
        if not _causal_entity_refs_resolve(
            payload,
            canonical_revisions_by_id or {},
        ):
            return False
        if not _typed_ref_valid(
            payload.get("delivery_ref"),
            identity_fields=("event_id",),
            hash_field="event_payload_hash",
        ) or not _typed_ref_valid(
            payload.get("display_ref"),
            identity_fields=("display_id",),
            hash_field="content_hash",
        ) or not _typed_ref_valid(
            payload.get("search_ref"),
            identity_fields=("session_id", "result_id", "exposure_id"),
        ):
            return False
        evidence = payload.get("evidence")
        if not isinstance(evidence, Mapping) or set(evidence) != {
            "refs",
            "content_hashes",
        }:
            return False
        refs = evidence["refs"]
        hashes = evidence["content_hashes"]
        if (
            not isinstance(refs, (list, tuple))
            or not refs
            or not isinstance(hashes, (list, tuple))
            or len(refs) != len(hashes)
            or any(not str(value).strip() for value in refs)
            or any(not _SHA256.fullmatch(str(value)) for value in hashes)
        ):
            return False
        interaction = payload.get("interaction")
        if not isinstance(interaction, Mapping) or set(interaction) != {
            "kind",
            "observed_facts",
        }:
            return False
        kind = str(interaction["kind"])
        facts = interaction["observed_facts"]
        if kind not in _FACTS or not isinstance(facts, (list, tuple)) or len(facts) != 1:
            return False
        fact = facts[0]
        if not isinstance(fact, Mapping) or set(fact) != {"name", "value"}:
            return False
        value = fact["value"]
        valid_value = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and float(value) >= 0.0
            if _FACTS[kind] == "dwell_seconds"
            else value is True
        )
        if fact["name"] != _FACTS[kind] or not valid_value:
            return False
        expected_authority = "explicit_user" if kind in _EXPLICIT_KINDS else "tool_observation"
        if payload.get("authority_class") != expected_authority:
            return False
        if not _causal_context_valid(payload, scope, observed_at):
            return False
        if not _attribution_valid(payload.get("attribution"), kind):
            return False
        if not _correction_valid(payload, kind):
            return False
        if not _downstream_valid(payload.get("downstream"), kind, feedback_targets):
            return False
        identity = {
            str(key): value
            for key, value in payload.items()
            if key
            not in {
                "reaction_id",
                "revision_state",
                "reaction_input_hash",
                "recorded_at",
                "attribution",
                "downstream",
            }
        }
        return payload.get("reaction_input_hash") == _sha256_json(identity)
    except (KeyError, TypeError, ValueError):
        return False


def _causal_context_valid(
    payload: Mapping[str, Any],
    scope: Mapping[str, Any],
    observed_at: datetime,
) -> bool:
    window = payload.get("observation_window")
    if not isinstance(window, Mapping) or set(window) != {
        "starts_at",
        "ends_at",
        "status",
    }:
        return False
    start = _timestamp(window["starts_at"])
    end = _timestamp(window["ends_at"])
    if window["status"] != "closed" or start > observed_at or observed_at > end:
        return False
    exposure = payload.get("exposure")
    if (
        not isinstance(exposure, Mapping)
        or set(exposure)
        != {"session_id", "exposure_id", "interface_id", "was_visible"}
        or str(exposure["session_id"]) != str(scope["session_id"])
        or (
            str(scope["type"]) == "session"
            and not str(exposure["session_id"]).strip()
        )
        or not str(exposure["exposure_id"]).strip()
        or not str(exposure["interface_id"]).strip()
        or not isinstance(exposure["was_visible"], bool)
    ):
        return False
    search_ref = payload["search_ref"]
    if search_ref["state"] == "available" and (
        str(search_ref["exposure_id"]) != str(exposure["exposure_id"])
    ):
        return False
    causes = payload.get("competing_causes")
    if not isinstance(causes, (list, tuple)):
        return False
    if any(
        not isinstance(item, Mapping)
        or set(item) != {"cause", "evidence_ref", "content_hash"}
        or not str(item["cause"]).strip()
        or not str(item["evidence_ref"]).strip()
        or not _SHA256.fullmatch(str(item["content_hash"]))
        for item in causes
    ):
        return False
    completeness = payload.get("source_completeness")
    if not isinstance(completeness, Mapping) or set(completeness) != {
        "state",
        "missing_refs",
    }:
        return False
    missing = completeness["missing_refs"]
    return bool(
        completeness["state"] in {"complete", "incomplete"}
        and isinstance(missing, (list, tuple))
        and not any(not str(value).strip() for value in missing)
        and ((completeness["state"] == "complete") == (not missing))
    )


def _attribution_valid(value: Any, kind: str) -> bool:
    expected_class = (
        "explicit_correction"
        if kind in {"inaccurate", "outdated"}
        else "explicit_preference"
        if kind in _EXPLICIT_KINDS
        else "weak_behavior"
    )
    expected_disposition = (
        "correction_pending" if expected_class == "explicit_correction" else "record_only"
    )
    return bool(
        isinstance(value, Mapping)
        and set(value)
        == {"method", "version", "code_hash", "spec_hash", "disposition", "evidence_class"}
        and value["method"] == "conservative_observation"
        and value["version"] == "v1"
        and _SHA256.fullmatch(str(value["code_hash"]))
        and _SHA256.fullmatch(str(value["spec_hash"]))
        and value["disposition"] == expected_disposition
        and value["evidence_class"] == expected_class
    )


def _correction_valid(payload: Mapping[str, Any], kind: str) -> bool:
    correction = payload.get("correction")
    if not isinstance(correction, Mapping) or set(correction) != {
        "state",
        "target_ref",
        "reason",
    }:
        return False
    if kind in {"inaccurate", "outdated"}:
        return bool(
            correction["state"] == "requested"
            and str(correction["target_ref"]).strip()
            and str(correction["reason"]).strip()
            and bool(str(payload.get("supersedes_event_id") or "").strip())
            == bool(str(payload.get("correction_of_event_id") or "").strip())
        )
    return bool(
        correction == {"state": "none", "target_ref": "", "reason": ""}
        and not payload.get("correction_of_event_id")
    )


def _downstream_valid(value: Any, kind: str, feedback_targets: Sequence[str]) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "registry_version",
        "required_targets",
        "eligible_targets",
        "exclusions",
    }:
        return False
    targets = tuple(feedback_targets)
    eligible = tuple(str(item) for item in value["eligible_targets"])
    expected_eligible = targets if kind in {"inaccurate", "outdated"} else ()
    exclusions = value["exclusions"]
    if (
        value["registry_version"] != "mnemos.feedback_target_registry.v1"
        or tuple(value["required_targets"]) != targets
        or eligible != expected_eligible
        or not isinstance(exclusions, (list, tuple))
    ):
        return False
    excluded = []
    for item in exclusions:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"target_id", "reason"}
            or not str(item["reason"]).strip()
        ):
            return False
        excluded.append(str(item["target_id"]))
    return tuple(excluded) == tuple(item for item in targets if item not in eligible)


def _source_ref_valid(value: Any) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value) == {"event_id", "source_revision_id", "content_hash"}
        and str(value["event_id"]).strip()
        and str(value["source_revision_id"]).strip()
        and _SHA256.fullmatch(str(value["content_hash"]))
    )


def _entity_ref_valid(value: Any) -> bool:
    return _typed_ref_valid(
        value,
        identity_fields=("id", "revision_id"),
        hash_field="content_hash",
    )


def _causal_entity_refs_resolve(
    payload: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
) -> bool:
    decision_ref = payload["decision_ref"]
    prediction_ref = payload["prediction_ref"]
    decision = _resolved_revision(decision_ref, revisions, "decision_trace")
    prediction = _resolved_revision(
        prediction_ref, revisions, "prediction_record"
    )
    if decision_ref["state"] == "available" and decision is None:
        return False
    if prediction_ref["state"] == "available" and prediction is None:
        return False
    if prediction is not None and not _prediction_access_matches(prediction, payload):
        return False
    action_ref = payload["action_ref"]
    if action_ref["state"] == "available":
        if decision is None or action_ref["revision_id"] != decision["revision_id"]:
            return False
        action_specs = [
            dict(item)
            for item in decision["payload"].get("action_specs") or ()
            if isinstance(item, Mapping) and item.get("action_id") == action_ref["id"]
        ]
        if len(action_specs) != 1 or _sha256_json(action_specs[0]) != action_ref["content_hash"]:
            return False
    if prediction is not None and decision is not None:
        prediction_decision = prediction["payload"].get("decision_ref")
        if not isinstance(prediction_decision, Mapping) or (
            prediction_decision.get("decision_id") != decision["object_id"]
            or prediction_decision.get("revision_id") != decision["revision_id"]
            or prediction_decision.get("revision_hash") != decision["payload_hash"]
        ):
            return False
    if prediction is not None and action_ref["state"] == "available":
        prediction_action = prediction["payload"].get("action_ref")
        if (
            not isinstance(prediction_action, Mapping)
            or prediction_action.get("action_id") != action_ref["id"]
        ):
            return False
    return True


def _resolved_revision(
    ref: Mapping[str, Any],
    revisions: Mapping[str, Mapping[str, Any]],
    object_type: str,
) -> Mapping[str, Any] | None:
    if ref["state"] != "available":
        return None
    revision = revisions.get(str(ref["revision_id"]))
    if revision is None:
        return None
    if (
        revision.get("object_type") != object_type
        or revision.get("object_id") != ref["id"]
        or revision.get("payload_hash") != ref["content_hash"]
        or not isinstance(revision.get("payload"), Mapping)
    ):
        return None
    return revision


def _prediction_access_matches(
    prediction: Mapping[str, Any], payload: Mapping[str, Any]
) -> bool:
    access = prediction["payload"].get("access_control")
    if not isinstance(access, Mapping):
        return False
    owner = access.get("owner")
    scope = access.get("scope")
    reaction_owner = payload["principal_ref"]
    reaction_scope = payload["scope"]
    return bool(
        isinstance(owner, Mapping)
        and isinstance(scope, Mapping)
        and owner.get("principal_id") == reaction_owner["principal_id"]
        and str(owner.get("agent") or "").lower()
        == str(reaction_owner["agent"]).lower()
        and str(scope.get("project") or "").lower()
        == str(reaction_scope["project"]).lower()
        and str(scope.get("session_id") or "") == str(reaction_scope["session_id"])
    )


def _typed_ref_valid(
    value: Any,
    *,
    identity_fields: tuple[str, ...],
    hash_field: str = "",
) -> bool:
    fields = {"state", *identity_fields, "unavailable_reason"}
    if hash_field:
        fields.add(hash_field)
    if not isinstance(value, Mapping) or set(value) != fields:
        return False
    identities = tuple(str(value[field]) for field in identity_fields)
    content_hash = str(value[hash_field]) if hash_field else ""
    if value["state"] == "available":
        return bool(
            all(item.strip() for item in identities)
            and (not hash_field or _SHA256.fullmatch(content_hash))
            and not value["unavailable_reason"]
        )
    if value["state"] == "unavailable":
        return bool(
            not any(identities)
            and not content_hash
            and str(value["unavailable_reason"]).strip()
        )
    return False


def _required_mapping(value: Any, fields: set[str]) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value) == fields
        and all(str(value[field]).strip() for field in fields)
    )


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
