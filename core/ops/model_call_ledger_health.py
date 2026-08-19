"""Read-only health projection for the provider-bound model-call ledger."""

from __future__ import annotations

from typing import Any

from core.telemetry.prompt_call_log import ModelCallLedger


_BLOCKING_FIELDS = (
    "billable_calls_without_ledger",
    "billable_request_without_reservation",
    "settled_cost_without_provider_usage",
    "sensitive_prompt_preview",
    "health_ledger_path_mismatch",
    "unverified_provider_usage",
    "subject_attribution_schema_missing",
    "entry_subject_attribution_schema_missing",
    "privacy_dispatch_schema_missing",
    "metered_usage_receipt_schema_missing",
    "unattributed_model_call_run_count",
    "unattributed_billable_entry_count",
    "unrecoverable_run_tombstone_history_disposition",
    "runtime_schema_gap_count",
    "invalid_daily_cost_cap",
    "reservation_cost_overrun",
)


def build_model_call_ledger_health(config: Any) -> dict[str, Any]:
    """Return public-safe ledger health without an absolute filesystem path."""
    report = ModelCallLedger.inspect(config)
    ledger_status = str(report.pop("status", "error") or "error")
    ok = ledger_status in {"ok", "uninitialized"} and not any(
        int(report.get(key, 0) or 0) > 0 for key in _BLOCKING_FIELDS
    )
    return {
        "status": "ok" if ok else "degraded",
        "ledger_status": ledger_status,
        **report,
        "repair_actions": (
            []
            if ok
            else [
                "Run `python3 mnemos_cli.py migrate plan --json`, review the current plan, "
                "then use the registered wrapped migration apply path with its fresh expected plan hash.",
                "Do not run the reconciliation script with `--apply` directly; the registry is the only mutation path.",
                "Do not delete retired prompt storage manually; reconciliation preserves deduplication evidence.",
            ]
        ),
    }
