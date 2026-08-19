"""Shallow, final runtime identity for the canonical feedback owner."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


_ACTIVE_FEEDBACK_FAILURE_CONTEXT: ContextVar[
    tuple[int, int, str, object] | None
] = ContextVar("active_feedback_failure_context", default=None)
_CANONICAL_FEEDBACK_OWNER_TYPE: type[Any] | None = None


class CanonicalFeedbackOwner:
    """Allow exactly one concrete owner class and reject proxy subclasses."""

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        global _CANONICAL_FEEDBACK_OWNER_TYPE
        if (
            cls.__module__ != "core.cognitive.feedback_attribution"
            or cls.__name__ != "FeedbackAttributionStore"
            or _CANONICAL_FEEDBACK_OWNER_TYPE is not None
        ):
            raise TypeError("canonical feedback owner cannot be subclassed")
        _CANONICAL_FEEDBACK_OWNER_TYPE = cls


def is_canonical_feedback_owner(owner: Any) -> bool:
    """Match the one concrete class object registered at module import."""

    return bool(
        _CANONICAL_FEEDBACK_OWNER_TYPE is not None
        and type(owner) is _CANONICAL_FEEDBACK_OWNER_TYPE
    )


def feedback_failure_context_matches_state(
    state: Any,
    command_id: str,
) -> bool:
    """Validate the state half of the state-created owner capability."""

    active = _ACTIVE_FEEDBACK_FAILURE_CONTEXT.get()
    return bool(
        active is not None
        and active[1] == id(state)
        and active[2] == str(command_id or "")
        and state._feedback_terminal_capability_matches(active[0], active[3])
    )
