"""Composition helpers for one shared distillation model-call run."""

from __future__ import annotations

from typing import Any, Iterable


def start_distillation_model_call_run(
    backends: Any,
    fragment_merger: Any,
    config: Any,
    run_context: str,
    *,
    subject_scope: tuple[str, str] | None = None,
    subject_scopes: Iterable[tuple[str, str]] | None = None,
) -> None:
    """Reset the shared budget and bind every LLM-stage provider to its run."""
    entry_subject_scopes = tuple(subject_scopes or ())
    backends.reset_session_cost_budget(
        config.get("distill.llm_cost_budget_per_session"),
        run_context=run_context,
        subject_scope=subject_scope,
        subject_scopes=entry_subject_scopes or None,
    )
    bind = getattr(fragment_merger, "bind_model_call_run", None)
    if callable(bind) and backends.model_call_ledger is not None:
        bind(
            backends.model_call_ledger,
            backends.model_call_run_id,
            subject_scopes=entry_subject_scopes or None,
        )
