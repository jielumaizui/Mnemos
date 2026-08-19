"""Typed business outcome returned by EventBus handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class HandlerOutcome:
    """Explicitly separate business acknowledgement from retry/dead outcomes."""

    disposition: str
    consumer: str = ""
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.disposition not in {"ack", "noop", "retry", "defer", "dead"}:
            raise ValueError(f"unsupported handler disposition: {self.disposition}")
        if self.disposition == "defer" and not any(
            str(item) for item in self.metadata.get("deferred_keys", [])
        ):
            raise ValueError("defer outcome requires at least one deferred_key")

    @classmethod
    def ack(cls, consumer: str = "", **metadata: Any) -> "HandlerOutcome":
        """Return a durable successful-consumption outcome."""

        return cls("ack", consumer=consumer, metadata=metadata)

    @classmethod
    def noop(
        cls, consumer: str = "", reason: str = "", **metadata: Any
    ) -> "HandlerOutcome":
        """Return a durable success for a consumer with no applicable mutation."""

        return cls("noop", consumer=consumer, reason=reason, metadata=metadata)

    @classmethod
    def retry(
        cls, consumer: str = "", reason: str = "", **metadata: Any
    ) -> "HandlerOutcome":
        """Request at-least-once redelivery without acknowledging the handler."""

        return cls("retry", consumer=consumer, reason=reason, metadata=metadata)

    @classmethod
    def defer(
        cls, consumer: str = "", reason: str = "", **metadata: Any
    ) -> "HandlerOutcome":
        """Wait for explicit external state without consuming retry budget."""

        return cls("defer", consumer=consumer, reason=reason, metadata=metadata)

    @classmethod
    def dead(
        cls, consumer: str = "", reason: str = "", **metadata: Any
    ) -> "HandlerOutcome":
        """Return a terminal business failure that must enter dead-letter storage."""

        return cls("dead", consumer=consumer, reason=reason, metadata=metadata)

    @classmethod
    def from_result(cls, result: Any, *, consumer: str = "") -> "HandlerOutcome":
        """Normalize supported handler return values into the typed outcome contract."""

        if isinstance(result, cls):
            return result if result.consumer else cls(
                result.disposition,
                consumer=consumer,
                reason=result.reason,
                metadata=result.metadata,
            )
        if result is None or result is True:
            return cls.ack(consumer)
        if result is False:
            return cls.retry(consumer, "handler returned false")
        if isinstance(result, str):
            status = result.strip().lower()
            if status in {"skipped", "noop", "no_op", "ignored", "not_applicable", "duplicate"}:
                return cls.noop(consumer, status)
            if status in {"retry", "retryable", "error", "failed", "failure"}:
                return cls.retry(consumer, status)
            if status in {"dead", "rejected", "invalid", "terminal_failed"}:
                return cls.dead(consumer, status)
            return cls.ack(consumer, value=result)
        if isinstance(result, dict):
            raw_metadata = dict(result)
            status = str(raw_metadata.get("status", "")).strip().lower()
            success = raw_metadata.get("success")
            duplicate = bool(raw_metadata.get("duplicate")) or status == "duplicate"
            projection_errors = int(raw_metadata.get("projection_errors") or 0)
            reason = str(raw_metadata.get("error") or raw_metadata.get("reason") or status)
            metadata = {
                key: value
                for key, value in raw_metadata.items()
                if key not in {"consumer", "reason", "disposition"}
            }
            if projection_errors > 0:
                return cls.retry(consumer, reason, **metadata)
            if success is False:
                return cls.retry(consumer, reason or "success=false", **metadata)
            if duplicate:
                return cls.noop(consumer, reason or "duplicate", **metadata)
            if status in {"ok", "success", "done", "accepted", "observed"}:
                return cls.ack(consumer, **metadata)
            if status in {"skipped", "noop", "no_op", "ignored", "not_applicable"}:
                return cls.noop(consumer, reason, **metadata)
            if status in {
                "retry", "retryable", "retryable_failed", "error", "failed",
                "page_not_found", "pending",
            }:
                return cls.retry(consumer, reason, **metadata)
            if status in {"defer", "deferred", "awaiting_decision"}:
                return cls.defer(consumer, reason, **metadata)
            if status in {"dead", "rejected", "invalid", "terminal_failed"}:
                return cls.dead(consumer, reason, **metadata)
            if success is True or not status:
                return cls.ack(consumer, **metadata)
            return cls.retry(consumer, f"unknown handler status: {status}", **metadata)
        if result:
            return cls.ack(consumer, value=result)
        return cls.retry(consumer, f"unsupported handler result: {type(result).__name__}")
