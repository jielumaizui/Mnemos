"""Provider-bound scoring helpers kept outside the large scorer module."""

from __future__ import annotations

from typing import Any, TypeAlias

from core.telemetry.prompt_call_log import model_call_run_scope


SubjectScope: TypeAlias = tuple[str, str]


def require_adaptive_score_subject_scope(subject_scope: SubjectScope | None) -> SubjectScope:
    """Reject untyped or generic fallback attribution before a provider call.

    Adaptive scoring often receives visible user content.  It may only cross a
    billable boundary when the caller supplies the exact owning subject (or an
    explicitly named, genuinely system-owned source).  In particular, do not
    recreate the historic ``("source", "adaptive_scorer")`` catch-all: that
    identifier cannot be deleted with the user whose content was scored.
    """
    if (
        not isinstance(subject_scope, tuple)
        or len(subject_scope) != 2
        or not all(isinstance(part, str) for part in subject_scope)
    ):
        raise ValueError("adaptive score embedding requires a typed subject scope")
    scope_kind, scope_value = (part.strip() for part in subject_scope)
    if not scope_kind or not scope_value:
        raise ValueError("adaptive score embedding requires a non-empty subject scope")
    if (scope_kind, scope_value) == ("source", "adaptive_scorer"):
        raise ValueError("generic adaptive scorer attribution is forbidden")
    return scope_kind, scope_value


def embed_for_adaptive_score(
    client: Any,
    content: str,
    config: Any,
    *,
    subject_scope: SubjectScope | None,
) -> Any:
    """Embed one score candidate in an explicitly attributable run."""
    resolved_scope = require_adaptive_score_subject_scope(subject_scope)
    with model_call_run_scope(
        config,
        "adaptive_scorer_embedding",
        subject_scope=resolved_scope,
    ):
        return client.embed_single(content, subject_scope=resolved_scope)
