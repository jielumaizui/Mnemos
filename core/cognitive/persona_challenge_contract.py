"""Immutable command contract shared by DecisionTrace and Persona consumers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.cognitive.state_contract import LocalConsumerCommand, sha256_json

PERSONA_CHALLENGE_CONSUMER = "persona_decision_challenge"
PERSONA_CHALLENGE_COMMAND = "evaluate_persona_decision_challenge"
PERSONA_CHALLENGE_SCHEMA_VERSION = "mnemos.persona_decision_challenge_command.v1"


def build_persona_challenge_command(
    *,
    decision_revision_id: str,
    decision_id: str,
    decision_hash: str,
    candidates: Sequence[Mapping[str, Any]],
    persona_revision: Mapping[str, str],
    principal: Mapping[str, str],
    scope: Mapping[str, str],
    created_at: str = "",
) -> LocalConsumerCommand:
    """Bind a challenge command to one sealed multi-option decision."""

    rows = tuple(dict(candidate) for candidate in candidates)
    if len(rows) < 2:
        raise ValueError("Persona challenge command requires at least two decision options")
    options = []
    for candidate in rows:
        option_id = str(candidate.get("candidate_id") or "").strip()
        if not option_id:
            raise ValueError("Persona challenge option lacks candidate_id")
        options.append(
            {
                "option_id": option_id,
                "option_key": str(candidate.get("key") or ""),
                "option_hash": sha256_json(candidate),
            }
        )
    if len({item["option_id"] for item in options}) != len(options):
        raise ValueError("Persona challenge option ids must be unique")
    normalized_decision_revision = str(decision_revision_id or "").strip()
    normalized_decision_id = str(decision_id or "").strip()
    normalized_persona_revision = str(persona_revision.get("revision_id") or "").strip()
    persona_hash = str(persona_revision.get("content_hash") or "").strip()
    principal_id = str(principal.get("principal_id") or "").strip()
    principal_agent = str(principal.get("agent") or "").strip()
    scope_type = str(scope.get("type") or "").strip()
    scope_id = str(scope.get("id") or "").strip()
    if not all(
        (
            normalized_decision_revision,
            normalized_decision_id,
            normalized_persona_revision,
            principal_id,
            principal_agent,
            scope_type,
            scope_id,
        )
    ):
        raise ValueError("Persona challenge command lacks decision, Persona, principal, or scope")
    if not _is_sha256(decision_hash) or not _is_sha256(persona_hash):
        raise ValueError("Persona challenge command requires exact content hashes")
    return LocalConsumerCommand.create(
        revision_id=normalized_decision_revision,
        consumer_id=PERSONA_CHALLENGE_CONSUMER,
        command_type=PERSONA_CHALLENGE_COMMAND,
        payload={
            "schema_version": PERSONA_CHALLENGE_SCHEMA_VERSION,
            "decision_trace": {
                "decision_id": normalized_decision_id,
                "revision_id": normalized_decision_revision,
                "content_hash": str(decision_hash),
            },
            "options": sorted(options, key=lambda item: item["option_id"]),
            "persona_revision": {
                "revision_id": normalized_persona_revision,
                "content_hash": persona_hash,
            },
            "principal": {
                "principal_id": principal_id,
                "agent": principal_agent,
            },
            "scope": {"type": scope_type, "id": scope_id},
        },
        created_at=created_at,
    )


def _is_sha256(value: str) -> bool:
    normalized = str(value or "")
    return (
        normalized.startswith("sha256:")
        and len(normalized) == 71
        and all(char in "0123456789abcdef" for char in normalized[7:].lower())
    )
