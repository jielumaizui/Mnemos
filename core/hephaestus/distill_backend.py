"""Backend abstraction for distillation LLM calls.

P1-C keeps behavior unchanged while moving concrete HTTP caller construction out
of extractors and judges. Real local AgentBackend integration is intentionally
not implemented here.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Protocol, Sequence

from core.hephaestus.distillation_llm import HttpApiHostAgentCaller
from core.hephaestus.distill_response import DistillBackendResponse

_BACKEND_ERRORS = (RuntimeError, OSError, ValueError, TypeError, KeyError, TimeoutError)


class DistillBackend(Protocol):
    """Minimal call interface used by Hephaestus distillation stages."""

    def call(
        self,
        prompt: str,
        expect_json: bool = True,
        max_retries: int | None = None,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> DistillBackendResponse:
        ...

    def checkpoint_identity(self) -> Mapping[str, Any]:
        """Return credential-free fields that determine generated output."""
        ...


class LLMBackend:
    """Adapter around the existing HTTP-compatible LLM caller."""

    def __init__(self, caller: HttpApiHostAgentCaller | None = None):
        self._caller = caller or HttpApiHostAgentCaller()

    @property
    def caller(self) -> HttpApiHostAgentCaller:
        return self._caller

    def call(
        self,
        prompt: str,
        expect_json: bool = True,
        max_retries: int | None = None,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> DistillBackendResponse:
        kwargs: Dict[str, Any] = {
            "max_retries": max_retries,
            "response_max_tokens": response_max_tokens,
            "response_retry_max_tokens": response_retry_max_tokens,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        call = self._caller.call_with_evidence
        try:
            params = inspect.signature(call).parameters
            accepts_kwargs = any(
                param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
            )
            if not accepts_kwargs:
                kwargs = {key: value for key, value in kwargs.items() if key in params}
        except (TypeError, ValueError):
            pass
        response = call(prompt, expect_json=expect_json, **kwargs)
        if not isinstance(response, DistillBackendResponse):
            raise TypeError("distillation caller must return DistillBackendResponse")
        return response

    def checkpoint_identity(self) -> Mapping[str, Any]:
        """Delegate canonical output identity to the concrete caller."""
        identity = getattr(self._caller, "checkpoint_identity", None)
        if not callable(identity):
            raise TypeError("distillation caller must expose checkpoint_identity()")
        return {
            "backend": f"{type(self).__module__}.{type(self).__qualname__}",
            "caller": identity(),
        }


@dataclass(frozen=True)
class BackendCallMetric:
    backend: str
    ok: bool
    elapsed_ms: float
    error_type: str = ""


class BackendChain:
    """Single-node chain baseline for future backend routing.

    P1-C intentionally forbids fallback and agent backends. The chain exists so
    metrics and the eventual routing seam have a stable shape without changing
    production behavior.
    """

    def __init__(self, backends: Sequence[DistillBackend]):
        if len(backends) != 1:
            raise ValueError("P1-C BackendChain supports exactly one backend")
        self._backend = backends[0]
        self.metrics: List[BackendCallMetric] = []

    @property
    def caller(self) -> Any:
        return getattr(self._backend, "caller", None)

    def call(
        self,
        prompt: str,
        expect_json: bool = True,
        max_retries: int | None = None,
        response_max_tokens: int | None = None,
        response_retry_max_tokens: int | None = None,
    ) -> DistillBackendResponse:
        started = time.monotonic()
        backend_name = type(self._backend).__name__
        try:
            result = self._backend.call(
                prompt,
                expect_json=expect_json,
                max_retries=max_retries,
                response_max_tokens=response_max_tokens,
                response_retry_max_tokens=response_retry_max_tokens,
            )
            if not isinstance(result, DistillBackendResponse):
                raise TypeError("distillation backend must return DistillBackendResponse")
        except _BACKEND_ERRORS as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            self.metrics.append(
                BackendCallMetric(
                    backend=backend_name,
                    ok=False,
                    elapsed_ms=elapsed_ms,
                    error_type=type(exc).__name__,
                )
            )
            raise
        elapsed_ms = (time.monotonic() - started) * 1000
        self.metrics.append(
            BackendCallMetric(backend=backend_name, ok=True, elapsed_ms=elapsed_ms)
        )
        return result

    def checkpoint_identity(self) -> Mapping[str, Any]:
        """Expose the ordered chain identity without runtime metrics."""
        identities = []
        for backend in (self._backend,):
            identity = getattr(backend, "checkpoint_identity", None)
            if not callable(identity):
                raise TypeError("distillation backend must expose checkpoint_identity()")
            identities.append(identity())
        return {"strategy": "single", "backends": identities}


def create_default_llm_backend() -> LLMBackend:
    """Create a fresh backend instance for one distillation stage."""

    return LLMBackend()


def create_default_backend_chain() -> BackendChain:
    """Create the current single-node production chain."""

    return BackendChain([create_default_llm_backend()])
