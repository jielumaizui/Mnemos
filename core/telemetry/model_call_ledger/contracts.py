"""Durable, privacy-safe accounting for every billable model request.

Historically this module was a best-effort ``PromptCallLog`` written after a
distillation call had finished.  That made the budget non-authoritative,
stored a reversible preview, and allowed provider boundaries to bypass it.
``ModelCallLedger`` is its in-place replacement: it reserves before dispatch,
settles from provider usage after a response, and persists no prompt or
response text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


SCHEMA_VERSION = "mnemos.model_call_ledger.v2"
_TERMINAL_STATES = frozenset(
    {
        "settled",
        "released",
        "usage_unverified",
        "incurred_unknown",
        "incurred_overrun",
        "legacy_observed",
    }
)
_CHARGEABLE_STATES = frozenset(
    {"reserved", "usage_unverified", "incurred_unknown", "incurred_overrun", "settled"}
)
_SAFE_ERROR_CODES = frozenset(
    {
        "",
        "error_redacted",
        "legacy_failure",
        "pre_dispatch_failure",
        "provider_usage_unknown",
        "provider_actual_exceeds_reservation",
        "subject_frozen_before_dispatch",
        "provider_usage_missing",
        "provider_exception_after_dispatch",
        "provider_exception_before_dispatch",
        "stale_dispatched_reservation_recovered",
        "model_call_budget_after_dispatch",
        "model_call_budget_before_dispatch",
        "model_call_subject_frozen_after_dispatch",
        "model_call_subject_frozen_before_dispatch",
        "reflection_provider_usage_missing",
        "reflection_provider_exception",
        "reflection_pre_dispatch_exception",
        "embedding_provider_usage_missing",
        "embedding_provider_exception",
        "embedding_pre_dispatch_exception",
        "embedding_ledger_error_after_dispatch",
        "embedding_ledger_error_before_dispatch",
        "rerank_provider_usage_missing",
        "rerank_provider_exception",
        "rerank_pre_dispatch_exception",
        "rerank_ledger_error_after_dispatch",
        "rerank_ledger_error_before_dispatch",
        "multimodal_provider_usage_missing",
        "multimodal_provider_exception",
        "multimodal_pre_dispatch_exception",
        "multimodal_response_exception",
        "freshness_provider_usage_missing",
        "freshness_provider_exception",
        "freshness_pre_dispatch_exception",
        "intent_router_provider_usage_missing",
        "intent_router_provider_exception",
        "intent_router_pre_dispatch_exception",
        "merge_provider_usage_missing",
        "merge_budget_after_dispatch",
        "merge_budget_before_dispatch",
        "merge_subject_frozen_after_dispatch",
        "merge_subject_frozen_before_dispatch",
        "merge_provider_exception",
        "merge_provider_pre_dispatch_exception",
        "verify_llm_smoke_exception",
        "verify_llm_smoke_pre_dispatch_exception",
        "verify_llm_smoke_usage_missing",
        "verify_embedding_smoke_exception",
        "verify_embedding_smoke_pre_dispatch_exception",
        "verify_embedding_smoke_usage_missing",
        "verify_rerank_smoke_exception",
        "verify_rerank_smoke_pre_dispatch_exception",
        "verify_rerank_smoke_usage_missing",
    }
)
# These values appear in human-facing diagnostics and ``recent()``.  They are
# therefore not free-form telemetry fields: accepting a caller supplied label
# would recreate a prompt/identifier persistence path even if ``input_text``
# itself is hashed.  New provider boundaries must add a reviewed literal here.
_LEDGER_OPERATIONS = frozenset(
    {
        "distill",
        "distill_extract",
        "distill_correct",
        "distill_judge",
        "distill_merge",
        "embedding",
        "rerank",
        "multimodal_extract",
        "freshness_redistill",
        "intent_router",
        "reflection_insight",
        "verify_llm_smoke",
        "verify_embedding_smoke",
        "verify_rerank_smoke",
        # ``legacy`` is reconciliation-only and ``test`` keeps the public
        # ledger testable without giving production callers a wildcard.
        "legacy",
        "test",
    }
)
_RUNTIME_LEDGER_OPERATIONS = _LEDGER_OPERATIONS - {"legacy", "test"}
_LEDGER_CACHE_STATUSES = frozenset({"miss", "hit", "revalidated", "bypass", "legacy"})
_RUNTIME_LEDGER_CACHE_STATUSES = _LEDGER_CACHE_STATUSES - {"legacy"}
_PROVIDER_LABEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MODEL_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_OPAQUE_METADATA_REFERENCE_KINDS = frozenset(
    {"provider_usage", "request", "metered_receipt", "provider_label", "model_label"}
)
_OPAQUE_METADATA_REFERENCE_VERSION = "v2"
_LEGACY_PRICE_VERSIONS = frozenset({"legacy", "legacy-observation-unbillable-v1"})
_STALE_DISPATCH_GRACE_SECONDS = 300
_RETIRED_PROMPT_STORAGE_TABLES = frozenset(
    {"prompt_calls", "prompt_call_log", "prompt_call_stats"}
)
_SUBJECT_SCOPE_KINDS = frozenset(
    {
        "agent",
        "session",
        "project",
        "path",
        "source",
        "time",
        "wiki_page",
        "persona_signal",
        "raw_event_id",
    }
)
_RUNTIME_REQUIRED_TABLES = frozenset(
    {
        "model_call_runs",
        "model_call_entries",
        "model_call_run_subjects",
        "model_call_entry_subjects",
        "model_call_frozen_subjects",
        "model_call_daily_spend_tombstones",
        "model_call_run_spend_tombstones",
    }
)
_RUNTIME_OPTIONAL_TABLES = frozenset({"model_call_ledger_reconciliation_dispositions"})
_UNRECOVERABLE_TOMBSTONE_DISPOSITION_KEY = "legacy_cascading_run_tombstone_history_v1"
_UNRECOVERABLE_TOMBSTONE_DISPOSITION = "explicit_unrecoverable_history_discard"
_RUNTIME_REQUIRED_COLUMNS = {
    "model_call_runs": frozenset({"run_id", "cost_budget", "created_at", "schema_version"}),
    "model_call_entries": frozenset(
        {
            "entry_id",
            "run_id",
            "operation",
            "provider",
            "model",
            "input_digest",
            "reserved_input_tokens",
            "reserved_output_tokens",
            "reserved_cost",
            "actual_input_tokens",
            "actual_output_tokens",
            "actual_total_tokens",
            "actual_cost",
            "refund_cost",
            "latency_ms",
            "provider_usage_id",
            "metered_usage_receipt",
            "request_id",
            "price_version",
            "input_price",
            "output_price",
            "cache_status",
            "retry_attempt",
            "request_dispatched",
            "lifecycle_state",
            "error_code",
            "legacy_fingerprint",
            "created_at",
            "dispatched_at",
            "settled_at",
        }
    ),
    "model_call_run_subjects": frozenset(
        {"run_id", "scope_kind", "subject_hash", "created_at"}
    ),
    "model_call_entry_subjects": frozenset(
        {"entry_id", "scope_kind", "subject_hash", "created_at"}
    ),
    "model_call_frozen_subjects": frozenset({"scope_kind", "subject_hash", "frozen_at"}),
    "model_call_daily_spend_tombstones": frozenset(
        {"spend_day", "effective_cost", "deleted_entry_count", "updated_at"}
    ),
    "model_call_run_spend_tombstones": frozenset(
        {"run_id_digest", "effective_cost", "deleted_entry_count", "updated_at"}
    ),
    "model_call_ledger_reconciliation_dispositions": frozenset(
        {"disposition_key", "disposition", "known_row_count", "recorded_at"}
    ),
}
_RUNTIME_REQUIRED_COLUMN_CONTRACTS: dict[
    str,
    dict[str, tuple[str, bool, str | None]],
] = {
    "model_call_runs": {
        "run_id": ("TEXT", False, None),
        "cost_budget": ("REAL", False, None),
        "created_at": ("TEXT", True, None),
        "schema_version": ("TEXT", True, None),
    },
    "model_call_entries": {
        "entry_id": ("TEXT", False, None),
        "run_id": ("TEXT", True, None),
        "operation": ("TEXT", True, None),
        "provider": ("TEXT", True, None),
        "model": ("TEXT", True, None),
        "input_digest": ("TEXT", True, None),
        "reserved_input_tokens": ("INTEGER", True, None),
        "reserved_output_tokens": ("INTEGER", True, None),
        "reserved_cost": ("REAL", True, None),
        "actual_input_tokens": ("INTEGER", False, None),
        "actual_output_tokens": ("INTEGER", False, None),
        "actual_total_tokens": ("INTEGER", False, None),
        "actual_cost": ("REAL", False, None),
        "refund_cost": ("REAL", True, "0"),
        "latency_ms": ("INTEGER", True, "0"),
        "provider_usage_id": ("TEXT", True, "''"),
        "metered_usage_receipt": ("TEXT", True, "''"),
        "request_id": ("TEXT", True, "''"),
        "price_version": ("TEXT", True, None),
        "input_price": ("REAL", True, None),
        "output_price": ("REAL", True, None),
        "cache_status": ("TEXT", True, "'miss'"),
        "retry_attempt": ("INTEGER", True, "0"),
        "lifecycle_state": ("TEXT", True, None),
        "request_dispatched": ("INTEGER", True, "0"),
        "error_code": ("TEXT", True, "''"),
        "legacy_fingerprint": ("TEXT", False, None),
        "created_at": ("TEXT", True, None),
        "dispatched_at": ("TEXT", False, None),
        "settled_at": ("TEXT", False, None),
    },
    "model_call_run_subjects": {
        "run_id": ("TEXT", False, None),
        "scope_kind": ("TEXT", True, None),
        "subject_hash": ("TEXT", True, None),
        "created_at": ("TEXT", True, None),
    },
    "model_call_entry_subjects": {
        "entry_id": ("TEXT", True, None),
        "scope_kind": ("TEXT", True, None),
        "subject_hash": ("TEXT", True, None),
        "created_at": ("TEXT", True, None),
    },
    "model_call_frozen_subjects": {
        "scope_kind": ("TEXT", True, None),
        "subject_hash": ("TEXT", True, None),
        "frozen_at": ("TEXT", True, None),
    },
    "model_call_daily_spend_tombstones": {
        "spend_day": ("TEXT", False, None),
        "effective_cost": ("REAL", True, None),
        "deleted_entry_count": ("INTEGER", True, None),
        "updated_at": ("TEXT", True, None),
    },
    "model_call_run_spend_tombstones": {
        "run_id_digest": ("TEXT", False, None),
        "effective_cost": ("REAL", True, None),
        "deleted_entry_count": ("INTEGER", True, None),
        "updated_at": ("TEXT", True, None),
    },
    "model_call_ledger_reconciliation_dispositions": {
        "disposition_key": ("TEXT", True, None),
        "disposition": ("TEXT", True, None),
        "known_row_count": ("INTEGER", True, None),
        "recorded_at": ("TEXT", True, None),
    },
}
_RUNTIME_REQUIRED_PRIMARY_KEYS = {
    "model_call_runs": ("run_id",),
    "model_call_entries": ("entry_id",),
    "model_call_run_subjects": ("run_id",),
    "model_call_entry_subjects": ("entry_id", "scope_kind", "subject_hash"),
    "model_call_frozen_subjects": ("scope_kind", "subject_hash"),
    "model_call_daily_spend_tombstones": ("spend_day",),
    "model_call_run_spend_tombstones": ("run_id_digest",),
    "model_call_ledger_reconciliation_dispositions": ("disposition_key",),
}
_RUNTIME_REQUIRED_UNIQUE_COLUMNS = {
    "model_call_entries": frozenset({("legacy_fingerprint",)}),
}
_RUNTIME_REQUIRED_NO_FOREIGN_KEY_TABLES = frozenset({"model_call_run_spend_tombstones"})
_FORBIDDEN_ENTRY_COLUMNS = frozenset(
    {"prompt", "prompt_summary", "prompt_preview", "response", "response_preview"}
)
_RUNTIME_REQUIRED_INDEX_COLUMNS = {
    "idx_model_call_entries_run": ("run_id", "created_at"),
    "idx_model_call_entries_created": ("created_at",),
    "idx_model_call_entries_state": ("lifecycle_state", "created_at"),
    "idx_model_call_run_subjects_scope": ("scope_kind", "subject_hash"),
    "idx_model_call_entry_subjects_scope": ("scope_kind", "subject_hash", "entry_id"),
}
_RUNTIME_REQUIRED_INDEX_TABLES = {
    "idx_model_call_entries_run": "model_call_entries",
    "idx_model_call_entries_created": "model_call_entries",
    "idx_model_call_entries_state": "model_call_entries",
    "idx_model_call_run_subjects_scope": "model_call_run_subjects",
    "idx_model_call_entry_subjects_scope": "model_call_entry_subjects",
}
_RUNTIME_REQUIRED_FOREIGN_KEYS = {
    "model_call_entries": ("run_id", "model_call_runs", "run_id", "NO ACTION"),
    "model_call_run_subjects": ("run_id", "model_call_runs", "run_id", "CASCADE"),
    "model_call_entry_subjects": ("entry_id", "model_call_entries", "entry_id", "CASCADE"),
}


class ModelCallLedgerError(RuntimeError):
    """Base error for a model-cost ledger invariant."""


class ModelCallBudgetExceeded(ModelCallLedgerError):
    """Raised before a provider request would exceed its durable cap."""


class ModelCallLedgerInvariantError(ModelCallLedgerError):
    """Raised when a reservation cannot transition safely."""


class ModelCallSubjectFrozen(ModelCallLedgerError):
    """Raised before a frozen data subject can reach a provider boundary."""


@dataclass(frozen=True)
class ModelCallRecord:
    """Read-only public shape for a durable model-call entry."""

    entry_id: str
    run_id: str
    operation: str
    provider: str
    model: str
    lifecycle_state: str
    reserved_cost: float
    actual_cost: float | None
    refund_cost: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int
    provider_usage_id: str
    request_id: str
    price_version: str
    cache_status: str
    retry_attempt: int
    created_at: str
