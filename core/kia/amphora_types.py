"""Shared immutable types and constants for the Amphora queue."""

from dataclasses import dataclass
from enum import Enum


MINUTES_SECONDS = 1440
TIMEOUT_MINUTES = 30
PROVENANCE_MIGRATION_SCHEMA = "mnemos.amphora_provenance_migration.v2"
SYSTEM_OWNED_META_KEYS = frozenset(
    {
        "failed_terminal_receipt_outbox",
        "message_cleanup_outbox",
        "messages_revision",
        "terminal_receipt_outbox",
    }
)


class DistillProgress(Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    STRUCTURING = "structuring"
    VERIFYING = "verifying"
    WRITING = "writing"
    DONE = "done"


@dataclass(frozen=True)
class DistillationFailureTransition:
    """Durable result of one queue-owned failure transition."""

    task_id: str
    status: str
    retry_count: int
    max_retries: int
    terminal: bool
