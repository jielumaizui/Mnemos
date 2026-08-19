"""Compatibility import seam for durable model-call accounting.

The implementation lives in :mod:`core.telemetry.model_call_ledger`.  This
module intentionally contains static re-exports only, so existing callers
continue to resolve the canonical objects without a second persistence path
or a compatibility runtime branch.
"""

from .model_call_ledger import (
    SCHEMA_VERSION,
    MeteredProviderUsage,
    ModelCallBudgetExceeded,
    ModelCallLedger,
    ModelCallLedgerError,
    ModelCallLedgerInvariantError,
    ModelCallRecord,
    ModelCallReservation,
    ModelCallSubjectFrozen,
    PromptCallLog,
    current_model_call_run,
    has_metered_provider_usage,
    metered_provider_usage,
    model_call_run_scope,
)
from .model_call_ledger.contracts import _SAFE_ERROR_CODES

__all__ = [
    "SCHEMA_VERSION",
    "ModelCallLedger",
    "ModelCallReservation",
    "ModelCallRecord",
    "ModelCallLedgerError",
    "ModelCallBudgetExceeded",
    "ModelCallLedgerInvariantError",
    "ModelCallSubjectFrozen",
    "MeteredProviderUsage",
    "metered_provider_usage",
    "has_metered_provider_usage",
    "current_model_call_run",
    "model_call_run_scope",
    "PromptCallLog",
    "_SAFE_ERROR_CODES",
]
