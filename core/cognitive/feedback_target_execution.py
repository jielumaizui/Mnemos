"""Fail-closed execution and recovery for canonical feedback targets."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from core.cognitive.feedback_contract import FEEDBACK_TARGETS
from core.cognitive.feedback_models import FeedbackTargetAdapter, FeedbackTargetEffect


TARGET_ADAPTER_ERRORS = (
    KeyError,
    OSError,
    PermissionError,
    RuntimeError,
    TypeError,
    ValueError,
    sqlite3.DatabaseError,
)


def invoke_target_adapter(
    *,
    adapter: FeedbackTargetAdapter,
    operation: str,
    payload: Mapping[str, Any],
) -> FeedbackTargetEffect:
    """Run an adapter and recover only an exact target-domain receipt."""

    try:
        if operation == "apply":
            return adapter.apply(dict(payload))
        if operation == "neutralize":
            return adapter.neutralize(dict(payload))
        raise ValueError("unsupported feedback target operation")
    except TARGET_ADAPTER_ERRORS:
        recovered = recover_target_effect(adapter, payload)
        if recovered is None:
            raise
        return recovered


def recover_target_effect(
    adapter: FeedbackTargetAdapter,
    payload: Mapping[str, Any],
) -> FeedbackTargetEffect | None:
    """Best-effort recovery; absence never proves unchanged target state."""

    recover = getattr(adapter, "recover_command_effect", None)
    if not callable(recover):
        return None
    try:
        effect = recover(dict(payload))
    except TARGET_ADAPTER_ERRORS:
        return None
    return effect if isinstance(effect, FeedbackTargetEffect) else None


def inspect_existing_domain_effect(
    state: Any,
    command_id: str,
) -> FeedbackTargetEffect | None:
    """Check the fixed registry before claiming a structural no-effect state."""

    command = state.command(command_id)
    if command is None:
        return None
    target_id = str(command.get("consumer_id") or "")
    payload = command.get("payload")
    if target_id not in FEEDBACK_TARGETS or not isinstance(payload, Mapping):
        return None
    if not str(payload.get("command_key") or ""):
        return None
    from core.cognitive.feedback_target_registry import (
        build_registered_feedback_proposal_owner,
    )

    owner = build_registered_feedback_proposal_owner(
        state.db_path.parent,
        target_id,
    )
    effect = owner.inspect_command_effect(payload)
    return effect if isinstance(effect, FeedbackTargetEffect) else None
