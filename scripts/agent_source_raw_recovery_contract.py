"""Shared typed failure contract for Agent Native-to-Raw recovery."""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA_VERSION = "mnemos.agent_source_raw_reconciliation.v2"
PREVIOUS_SCHEMA_VERSION = "mnemos.agent_source_raw_reconciliation.v1"
PLAN_VERSION = "mnemos.agent_source_raw_reconciliation_plan.v3"
RAW_GENERATION_WORKER_MAX_RSS_BYTES = 8 * 1024 * 1024 * 1024
RAW_GENERATION_WORKER_MAX_REPORT_BYTES = 16 * 1024 * 1024
RAW_GENERATION_WORKER_MAX_SECONDS = 15 * 60
RAW_GENERATION_WORKER_ISOLATION_SCHEMA = (
    "mnemos.raw_generation_worker_isolation.v1"
)
RAW_GENERATION_WORKER_WRITE_GUARD = (
    "python_audit_global_exact_raw_cursor_coverage_targets"
)
RAW_GENERATION_PARENT_DEATH_GUARD = (
    "pipe_eof_split_process_group_kill_cleanup_v2"
)


def raw_generation_worker_isolation_contract() -> dict[str, int | str]:
    """Return the exact producer/verifier contract for one Raw generation."""

    return {
        "schema_version": RAW_GENERATION_WORKER_ISOLATION_SCHEMA,
        "write_guard": RAW_GENERATION_WORKER_WRITE_GUARD,
        "allowed_ephemeral_root_count": 1,
        "filesystem_sandbox": (
            "darwin_kernel_deny_writes_outside_allowed_roots_v1"
        ),
        "inherited_regular_file_descriptor_policy": (
            "close_all_preexisting_regular_file_descriptors_v1"
        ),
        "crash_cleanup": (
            "guardian_cleanup_plus_owner_registry_next_run_reap_v2"
        ),
        "parent_death_guard": RAW_GENERATION_PARENT_DEATH_GUARD,
        "max_rss_bytes": RAW_GENERATION_WORKER_MAX_RSS_BYTES,
        "max_report_bytes": RAW_GENERATION_WORKER_MAX_REPORT_BYTES,
        "max_seconds": RAW_GENERATION_WORKER_MAX_SECONDS,
    }


class AgentSourceRawReconciliationError(RuntimeError):
    """Fail-closed condition for the controlled Raw-only recovery."""

    def __init__(
        self,
        code: str,
        *,
        details: Mapping[str, Any] | None = None,
    ):
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


def reconciliation_error_from_typed_failure(
    failure: Any,
) -> AgentSourceRawReconciliationError:
    """Preserve a typed lower-layer code and content-free details."""

    code = getattr(failure, "code", None)
    details = getattr(failure, "details", None)
    if not isinstance(code, str) or not code:
        return AgentSourceRawReconciliationError(
            "typed_reconciliation_failure_invalid"
        )
    return AgentSourceRawReconciliationError(
        code,
        details=details if isinstance(details, Mapping) else None,
    )
