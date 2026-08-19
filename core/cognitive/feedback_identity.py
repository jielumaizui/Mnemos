"""Stable private identities shared by canonical feedback chains."""

from __future__ import annotations

from typing import Any, cast, Mapping

from core.cognitive.feedback_contract import FEEDBACK_SOURCE_CHANNELS
from core.cognitive.state_contract import sha256_json


SUPPORTED_FEEDBACK_SOURCE_CHANNELS = FEEDBACK_SOURCE_CHANNELS


def attribution_principal_ref(
    access_control: Mapping[str, Any],
) -> dict[str, str]:
    """Return the immutable private owner binding used by attribution identity."""

    owner = dict(access_control["owner"])
    return {
        "principal_id": str(owner["principal_id"]),
        "agent": str(owner["agent"]).lower(),
    }


def feedback_attribution_id(
    *,
    subject_ref: Mapping[str, Any],
    scope_type: str,
    scope_id: str,
    principal_ref: Mapping[str, Any],
) -> str:
    """Derive one attribution chain per subject, scope, and private owner."""

    identity = {
        "subject_ref": dict(subject_ref),
        "scope_type": str(scope_type),
        "scope_id": str(scope_id),
        "principal_ref": {
            "principal_id": str(principal_ref["principal_id"]),
            "agent": str(principal_ref["agent"]).lower(),
        },
    }
    digest = cast(str, sha256_json(identity))
    return "feedback-attribution-" + digest.split(":", 1)[1][:32]
