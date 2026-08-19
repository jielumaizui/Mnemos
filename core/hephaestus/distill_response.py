"""Typed response evidence for every distillation model call.

The parsed payload is convenient for consumers, but it is not sufficient
evidence when a schema or semantic contract rejects the model output.  This
module keeps the provider response and the parse/transport facts together so
the failure path cannot accidentally discard the bytes needed for diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from typing import Any, Mapping, Sequence


RESPONSE_EVIDENCE_SCHEMA = "mnemos.distill_backend_response.v1"


def _detached_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value or {}).items()}


@dataclass(frozen=True)
class DistillBackendResponse:
    """One provider response plus the evidence required to explain it."""

    raw_text: str
    parsed: Any | None
    usage: Mapping[str, Any]
    provider: str
    model: str
    request_id: str
    finish_reason: str
    parse_path: str
    attempt_history: tuple[Mapping[str, Any], ...]
    response_hash: str
    schema_version: str = RESPONSE_EVIDENCE_SCHEMA

    @classmethod
    def create(
        cls,
        *,
        raw_text: str,
        parsed: Any | None,
        usage: Mapping[str, Any] | None,
        provider: str,
        model: str,
        request_id: str = "",
        finish_reason: str = "",
        parse_path: str,
        attempt_history: Sequence[Mapping[str, Any]] = (),
    ) -> "DistillBackendResponse":
        raw = str(raw_text or "")
        return cls(
            raw_text=raw,
            parsed=parsed,
            usage=_detached_mapping(usage),
            provider=str(provider or ""),
            model=str(model or ""),
            request_id=str(request_id or ""),
            finish_reason=str(finish_reason or ""),
            parse_path=str(parse_path or "unknown"),
            attempt_history=tuple(_detached_mapping(item) for item in attempt_history),
            response_hash=cls.hash_raw_text(raw),
        )

    @classmethod
    def transport_empty(
        cls,
        *,
        usage: Mapping[str, Any] | None,
        provider: str = "",
        model: str = "",
        request_id: str = "",
        finish_reason: str = "",
        attempt_history: Sequence[Mapping[str, Any]] = (),
    ) -> "DistillBackendResponse":
        return cls.create(
            raw_text="",
            parsed=None,
            usage=usage,
            provider=provider,
            model=model,
            request_id=request_id,
            finish_reason=finish_reason,
            parse_path="transport_empty",
            attempt_history=attempt_history,
        )

    @staticmethod
    def hash_raw_text(raw_text: str) -> str:
        return hashlib.sha256(str(raw_text or "").encode("utf-8")).hexdigest()

    @property
    def successful(self) -> bool:
        return self.parsed is not None

    def require_mapping(self) -> Mapping[str, Any]:
        """Return the parsed JSON object or fail closed on protocol drift."""

        if not isinstance(self.parsed, Mapping):
            raise TypeError("distillation response parsed payload must be a mapping")
        return self.parsed

    def with_prior_attempts(
        self, attempts: Sequence[Mapping[str, Any]]
    ) -> "DistillBackendResponse":
        """Return the same response with earlier route attempts prepended."""

        if not attempts:
            return self
        return replace(
            self,
            attempt_history=tuple(_detached_mapping(item) for item in attempts)
            + self.attempt_history,
        )

    def to_failure_metadata(self) -> dict[str, Any]:
        """Return raw-free metadata suitable for a failure artifact."""

        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "finish_reason": self.finish_reason,
            "parse_path": self.parse_path,
            "attempt_history": [dict(item) for item in self.attempt_history],
            "usage": dict(self.usage),
            "response_hash": self.response_hash,
            "raw_available": bool(self.raw_text),
            "raw_length": len(self.raw_text),
            "transport_empty": not self.raw_text,
        }
