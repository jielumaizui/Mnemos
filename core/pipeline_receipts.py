"""Typed durable receipts shared by cross-database pipeline handoffs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DistillationEnqueueReceipt:
    """Durable acknowledgement returned after Amphora owns one input revision."""

    receipt_id: str
    task_id: str
    source_agent: str
    session_id: str
    input_revision: str
    status: str
    created: bool
    schema_version: str = "mnemos.distillation_enqueue_receipt.v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DistillationWriteReceipt:
    """Outcome of converting a distillation result into durable artifacts."""

    status: str
    terminal_reason: str
    written_pages: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    expected_count: int = 0
    written_count: int = 0
    failed_count: int = 0
    required_consumer_receipts: tuple[str, ...] = ()
    schema_version: str = "mnemos.distillation_write_receipt.v1"

    def __post_init__(self) -> None:
        allowed = {
            "committed",
            "intentional_skip",
            "proposal_pending",
            "partial",
            "retryable_failed",
        }
        if self.status not in allowed:
            raise ValueError(f"invalid distillation write status: {self.status}")
        if not self.terminal_reason.strip():
            raise ValueError("distillation write receipt requires terminal_reason")
        if min(self.expected_count, self.written_count, self.failed_count) < 0:
            raise ValueError("distillation write receipt counts cannot be negative")
        if self.written_count != len(self.written_pages):
            raise ValueError("written_count must equal the number of written_pages")
        if self.status == "committed" and not self.written_pages:
            raise ValueError("committed distillation receipt requires a durable page")

    @property
    def terminal(self) -> bool:
        return self.status in {"committed", "intentional_skip"}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("written_pages", "proposal_ids", "required_consumer_receipts"):
            data[key] = list(data[key])
        data["terminal"] = self.terminal
        return data


def canonical_distillation_write_receipt_payload(
    receipt: DistillationWriteReceipt,
) -> dict[str, Any]:
    """Return the immutable payload used by terminal outbox/runtime proofs."""
    payload = receipt.to_dict()
    payload.pop("terminal", None)
    return payload


def distillation_write_receipt_sha256(
    receipt: DistillationWriteReceipt,
) -> str:
    canonical = json.dumps(
        canonical_distillation_write_receipt_payload(receipt),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_distillation_failed_terminal_payload(
    *,
    task_id: str,
    session_id: str,
    input_revision: str,
    reason: str,
    retry_count: int,
    max_retries: int,
    cognitive_event_ids: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    """Return the exact failed-terminal payload shared by queue and runtime."""
    return {
        "schema_version": "mnemos.distillation_failed_terminal.v1",
        "task_id": str(task_id),
        "session_id": str(session_id),
        "input_revision": str(input_revision),
        "reason": str(reason),
        "retry_count": int(retry_count),
        "max_retries": int(max_retries),
        "cognitive_event_ids": list(cognitive_event_ids),
    }


def distillation_failed_terminal_sha256(
    *,
    task_id: str,
    session_id: str,
    input_revision: str,
    reason: str,
    retry_count: int,
    max_retries: int,
    cognitive_event_ids: tuple[str, ...] | list[str],
) -> str:
    payload = canonical_distillation_failed_terminal_payload(
        task_id=task_id,
        session_id=session_id,
        input_revision=input_revision,
        reason=reason,
        retry_count=retry_count,
        max_retries=max_retries,
        cognitive_event_ids=cognitive_event_ids,
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionEndReceipt:
    """Durable acknowledgement that a session-end flush was scheduled."""

    receipt_id: str
    source_agent: str
    session_id: str
    status: str
    error: str = ""
    schema_version: str = "mnemos.session_end_receipt.v1"

    def __post_init__(self) -> None:
        if self.status not in {"handoff_pending", "retryable_failed", "committed"}:
            raise ValueError(f"invalid session-end receipt status: {self.status}")
        if not self.receipt_id or not self.source_agent or not self.session_id:
            raise ValueError("session-end receipt identity fields are required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
