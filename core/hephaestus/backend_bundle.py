"""Backend construction helpers for Hephaestus composition roots."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from core.hephaestus.distill_backend import (
    DistillBackend,
    LLMBackend,
    create_default_llm_backend,
)
from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
from core.telemetry.prompt_call_log import ModelCallLedger


def backend_from_caller(caller: HttpApiHostAgentCaller | None = None) -> DistillBackend:
    return LLMBackend(caller) if caller is not None else create_default_llm_backend()


@dataclass
class DistillBackendBundle:
    judge: DistillBackend
    extractor: DistillBackend
    skill: DistillBackend
    model_call_ledger: ModelCallLedger | None = field(default=None, init=False, repr=False)
    model_call_run_id: str = field(default="", init=False)

    @classmethod
    def build(
        cls,
        *,
        caller: HttpApiHostAgentCaller | None = None,
        backend_factory: Callable[[], DistillBackend] | None = None,
    ) -> "DistillBackendBundle":
        factory = backend_factory
        if factory is None:
            factory = (lambda: LLMBackend(caller)) if caller is not None else create_default_llm_backend
        return cls(judge=factory(), extractor=factory(), skill=factory())

    @property
    def _all(self) -> tuple[DistillBackend, DistillBackend, DistillBackend]:
        return (self.judge, self.extractor, self.skill)

    def _callers(self) -> tuple[HttpApiHostAgentCaller, ...]:
        callers: list[HttpApiHostAgentCaller] = []
        for backend in self._all:
            caller = getattr(backend, "caller", None)
            if caller is not None and hasattr(caller, "reset_session_cost_budget") and caller not in callers:
                callers.append(caller)
        return tuple(callers)

    def reset_session_cost_budget(
        self,
        budget: Any,
        *,
        run_context: str = "",
        subject_scope: tuple[str, str] | None = None,
        subject_scopes: Iterable[tuple[str, str]] | None = None,
    ) -> None:
        """Create one durable run shared by judge/extract/correction/merge callers."""
        callers = self._callers()
        if not callers:
            return
        normalized_budget = float(budget) if budget is not None else None
        entry_subject_scopes = tuple(subject_scopes or ())
        # A composition root that is invoked outside the session pipeline has
        # no user asset to name.  It still must use an explicit, fixed source
        # rather than creating an anonymous provider run.
        root_scope = subject_scope or ("source", "distillation_backend_bundle")
        production_callers = tuple(
            caller for caller in callers if isinstance(caller, HttpApiHostAgentCaller)
        )
        try:
            config_getter = getattr(callers[0], "_get_config")
            ledger = ModelCallLedger.for_config(config_getter())
            # Run ids are visible accounting metadata.  They must never carry
            # a raw session/revision value merely to make a run recognizable.
            run_context_hash = hashlib.sha256(
                str(run_context or "session").encode("utf-8")
            ).hexdigest()[:24]
            run_id = f"distill:{run_context_hash}:{uuid.uuid4().hex}"
            run_id = ledger.start_run(
                run_id,
                cost_budget=normalized_budget,
                subject_scope=root_scope,
            )
            self.model_call_ledger = ledger
            self.model_call_run_id = run_id
            for caller in callers:
                caller.reset_session_cost_budget(
                    normalized_budget,
                    run_id=run_id,
                    ledger=ledger,
                    subject_scopes=entry_subject_scopes or None,
                )
        except (AttributeError, OSError, ValueError, TypeError):
            # Isolated fake callers intentionally retain their in-memory
            # accounting.  A real provider caller must never fall back: that
            # would let a billable request bypass durable attribution.
            if production_callers:
                raise
            self.model_call_ledger = None
            self.model_call_run_id = ""
            for caller in callers:
                caller.reset_session_cost_budget(normalized_budget)

    @property
    def budget_exceeded(self) -> bool:
        if self.model_call_ledger is not None and self.model_call_run_id:
            summary = self.model_call_ledger.run_summary(self.model_call_run_id)
            budget = summary.get("cost_budget")
            if budget is not None:
                return float(summary.get("effective_cost", 0.0) or 0.0) >= float(budget)
        return any(
            bool(getattr(caller, "budget_exceeded", False))
            for caller in (getattr(backend, "caller", None) for backend in self._all)
            if caller is not None
        )

    @property
    def session_cost(self) -> float:
        if self.model_call_ledger is not None and self.model_call_run_id:
            summary = self.model_call_ledger.run_summary(self.model_call_run_id)
            return float(summary.get("effective_cost", 0.0) or 0.0)
        return sum(
            float(getattr(caller, "session_cost", 0.0) or 0.0)
            for caller in (getattr(backend, "caller", None) for backend in self._all)
            if caller is not None
        )
