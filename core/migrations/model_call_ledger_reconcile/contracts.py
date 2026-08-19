"""Shared, non-content contracts for model-call-ledger reconciliation.

The reconciler only carries allowlisted operational metadata.  Raw prompt,
response, credential, and caller-error values never belong in a plan, backup
receipt, or migration result.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "mnemos.model_call_ledger_reconcile.v3"
SOURCE_FILENAMES = ("wiki_state.db", "prompt_calls.db", "sync_log.db")
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
RETIRED_TABLES = frozenset({"prompt_calls", "prompt_call_log", "prompt_call_stats"})
RECORD_TABLES = frozenset({"prompt_calls", "prompt_call_log"})
SAFE_COLUMNS = (
    "task_type",
    "operation",
    "session_id",
    "provider",
    "model",
    "prompt_hash",
    "input_digest",
    "prompt_tokens",
    "input_tokens",
    "completion_tokens",
    "output_tokens",
    "latency_ms",
    "success",
    "created_at",
)
HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{2,120}$")


class ModelCallLedgerReconcileError(RuntimeError):
    """Raised when reconciliation cannot prove a safe local transition."""


@dataclass(frozen=True)
class HistoricalCall:
    """One metadata-only historical record selected from a retired owner."""

    source_db: str
    source_generation: str
    source_table: str
    source_rowid: int
    operation: str
    provider: str
    model: str
    input_digest: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    success: bool
    created_at: str
    fingerprint: str
    # This remains process-local.  It is never put into a plan or backup
    # receipt, and currently remains None for legacy sources without a
    # separately verified provenance record.
    subject_scope: tuple[str, str] | None


def utcnow() -> str:
    """Return a canonical UTC timestamp for safe migration receipts."""
    return datetime.now(timezone.utc).isoformat()


def json_hash(value: Any) -> str:
    """Hash structured, allowlisted metadata deterministically."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def safe_reconcile_error(exc: BaseException, *, invariant_error: type[BaseException] | None = None) -> str:
    """Project failures to typed categories without persisting exception text."""
    if isinstance(exc, ModelCallLedgerReconcileError) or (
        invariant_error is not None and isinstance(exc, invariant_error)
    ):
        code = str(exc)
        if SAFE_ERROR_CODE.fullmatch(code):
            return code
    if isinstance(exc, sqlite3.Error):
        return "reconciliation_sqlite_error"
    if isinstance(exc, OSError):
        return "reconciliation_os_error"
    if isinstance(exc, ValueError):
        return "reconciliation_value_error"
    if isinstance(exc, RuntimeError) and str(exc) == "model_call_ledger_migration_lock_unavailable":
        return "model_call_ledger_migration_lock_unavailable"
    return "reconciliation_failed"


def safe_record_metadata_identity(record: HistoricalCall) -> str:
    """Hash selected migration metadata without exposing filesystem details."""
    return "sha256:" + json_hash(
        {
            "source_db": record.source_db,
            "source_table": record.source_table,
            "source_rowid": record.source_rowid,
            "operation": record.operation,
            "provider": record.provider,
            "model": record.model,
            "input_digest": record.input_digest,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "latency_ms": record.latency_ms,
            "success": record.success,
            "created_at": record.created_at,
        }
    )


def sidecar_paths(path: Path) -> tuple[Path, ...]:
    """Return all SQLite sidecar owners that travel with a main database."""
    return tuple(Path(str(path) + suffix) for suffix in SQLITE_SIDECAR_SUFFIXES)
