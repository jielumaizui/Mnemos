"""Apply coordinator for the registered model-call-ledger reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from core.runtime_paths import RuntimePaths
from core.telemetry.model_call_ledger import ModelCallLedger, ModelCallLedgerInvariantError
from core.telemetry.model_call_ledger.migration import LedgerReconciliation

from . import runtime
from .backup import create_sqlite_backups
from .cleanup import cleanup_canonical_retired_storage, cleanup_source_database
from .contracts import (
    SOURCE_FILENAMES,
    ModelCallLedgerReconcileError,
    json_hash as _json_hash,
    safe_reconcile_error,
)
from .inventory import (
    _require_regular_sqlite_file,
    _retired_storage_fingerprint,
    _source_inventory,
)
from .planner import build_reconciliation_plan


def _safe_reconcile_error(exc: BaseException) -> str:
    return safe_reconcile_error(exc, invariant_error=ModelCallLedgerInvariantError)


def _external_source_snapshot_fingerprint(plan: dict[str, Any]) -> str:
    """Return the immutable external-source snapshot component of a plan."""
    return "sha256:" + _json_hash(
        [_source_inventory(report) for report in plan.get("sources", [])]
    )


def _assert_sources_still_match_after_canonical_write(
    config: Any,
    expected_plan: dict[str, Any],
) -> None:
    """Reject source drift without comparing canonical fields we just changed."""
    observed, _pending = build_reconciliation_plan(config)
    if not observed.get("ok"):
        raise ModelCallLedgerReconcileError("source_drift_after_canonical_reconciliation")
    if _external_source_snapshot_fingerprint(observed) != _external_source_snapshot_fingerprint(
        expected_plan
    ):
        raise ModelCallLedgerReconcileError("source_drift_after_canonical_reconciliation")
    expected_canonical_retired = dict(expected_plan.get("canonical_retired_storage", {}))
    if expected_canonical_retired.get("retired_tables") and _retired_storage_fingerprint(
        dict(observed.get("canonical_retired_storage", {}))
    ) != _retired_storage_fingerprint(expected_canonical_retired):
        raise ModelCallLedgerReconcileError("canonical_retired_source_drift_after_reconciliation")


def _verified_backup_for_source(
    backups: Iterable[dict[str, Any]],
    private_backup_identities: dict[Path, str],
    source: Path,
) -> tuple[Path, str]:
    """Return the in-memory identity bound to one verified private backup."""
    source_path = Path(source).expanduser().resolve()
    for backup in backups:
        if Path(str(backup.get("source", ""))).expanduser().resolve() != source_path:
            continue
        backup_path = Path(str(backup.get("path", ""))).expanduser().resolve()
        backup_identity = private_backup_identities.get(backup_path)
        if backup_identity:
            return backup_path, backup_identity
    raise ModelCallLedgerReconcileError("verified_backup_missing_for_source")


def _run_recovery_lifecycle(
    lifecycle: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None,
    phase: str,
    plan: dict[str, Any],
) -> Mapping[str, Any] | None:
    """Require a durable v3 recovery receipt before/after destructive work."""
    if lifecycle is None:
        return None
    try:
        result = lifecycle(phase, plan)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ModelCallLedgerReconcileError(f"sealed_recovery_{phase}_failed") from exc
    if not isinstance(result, Mapping) or not bool(result.get("ok")):
        raise ModelCallLedgerReconcileError(f"sealed_recovery_{phase}_failed")
    plan["sealed_recovery_manifest"] = str(result.get("recovery_manifest") or "")
    plan["sealed_recovery_status"] = str(result.get("status") or "")
    return result


def reconcile_model_call_ledger(
    config: Any,
    *,
    apply: bool,
    backup_dir: Path | None = None,
    expected_plan_hash: str | None = None,
    discard_unattributable_legacy: bool = False,
    discard_unrecoverable_run_tombstone_history: bool = False,
    migration_capability: object | None = None,
) -> dict[str, Any]:
    """Inspect or explicitly reconcile every retired model-call storage owner."""
    plan, pending = build_reconciliation_plan(config)
    plan["mode"] = "apply" if apply else "dry_run"
    plan["backup"] = []
    plan["imported_count"] = 0
    plan["cleanup"] = []
    plan["canonical_cleanup"] = {}
    plan["privacy_reconciliation"] = {}
    plan["discarded_unattributable_source_count"] = 0
    plan["discarded_unrecoverable_run_tombstone_history"] = 0
    if not apply or not plan.get("ok"):
        return plan
    if plan.get("status") == "clean":
        plan.update(
            status="noop",
            ok=True,
            change_count=0,
            backup=[],
            cleanup=[],
            canonical_cleanup={},
        )
        return plan
    if not expected_plan_hash:
        plan.update(ok=False, status="blocked", error="expected_plan_hash_required")
        return plan
    if str(expected_plan_hash) != str(plan.get("plan_fingerprint") or ""):
        plan.update(
            ok=False,
            status="blocked",
            error="expected_plan_hash_mismatch",
            reviewed_plan_hash_present=True,
            reviewed_plan_hash_matches=False,
        )
        return plan
    plan["reviewed_plan_hash_present"] = True
    plan["reviewed_plan_hash_matches"] = True
    if plan.get("requires_explicit_unattributable_discard") and not discard_unattributable_legacy:
        plan.update(
            ok=False,
            status="blocked",
            error="unattributable_legacy_requires_explicit_discard",
        )
        return plan
    if plan.get("requires_explicit_retired_stats_discard") and not discard_unattributable_legacy:
        plan.update(
            ok=False,
            status="blocked",
            error="retired_prompt_stats_requires_explicit_discard",
        )
        return plan
    if (
        plan.get("requires_explicit_unrecoverable_run_tombstone_history_discard")
        and not discard_unrecoverable_run_tombstone_history
    ):
        plan.update(
            ok=False,
            status="blocked",
            error="unrecoverable_run_tombstone_history_requires_explicit_discard",
        )
        return plan
    if backup_dir is None:
        plan.update(ok=False, status="blocked", error="backup_directory_required")
        return plan
    try:
        from core.migrations.registry import _consume_model_call_ledger_apply_capability

        _attempt_ledger_id, recovery_lifecycle = _consume_model_call_ledger_apply_capability(
            migration_capability,
            expected_plan_hash=str(expected_plan_hash),
        )
    except (ImportError, TypeError, ValueError):
        plan.update(
            ok=False,
            status="blocked",
            error="registered_migration_capability_required",
        )
        return plan

    paths = RuntimePaths.from_config(config)
    if not runtime.runtime_writers_are_inactive(paths.database_dir):
        plan.update(ok=False, status="blocked", error="daemon_not_inactive")
        return plan
    backup_root = Path(backup_dir).expanduser()
    recovery_prepared = False
    recovery_started = False
    recovery_finished = False
    migration_lock = None
    try:
        from core.migrations.model_call_ledger_recovery import (
            acquire_model_call_ledger_migration_lock,
        )

        migration_lock = acquire_model_call_ledger_migration_lock(config)
        if not runtime.runtime_writers_are_inactive(paths.database_dir):
            plan.update(ok=False, status="blocked", error="daemon_not_inactive")
            return plan
        current_plan, current_pending = build_reconciliation_plan(config)
        if (
            not current_plan.get("ok")
            or current_plan["plan_fingerprint"] != plan["plan_fingerprint"]
        ):
            plan.update(ok=False, status="blocked", error="source_drift_before_lock")
            return plan
        source_paths = [paths.database_dir / filename for filename in SOURCE_FILENAMES]
        source_reports_by_name = {
            Path(str(report["path"])).name: report
            for report in current_plan.get("sources", [])
        }
        mutation_source_paths = [
            source
            for source in source_paths
            if source_reports_by_name.get(source.name, {}).get("retired_tables")
        ]
        canonical_source = paths.model_call_ledger_db.expanduser()
        _require_regular_sqlite_file(canonical_source, allow_missing=True)
        pre_backup_proof = LedgerReconciliation.prepare_backup(
            canonical_source
        )
        private_backup_identities: dict[Path, str] = {}
        backup_result = create_sqlite_backups(
            [paths.model_call_ledger_db, *mutation_source_paths],
            backup_root,
            prepared_canonical_backup=pre_backup_proof,
            return_canonical_backup_receipt=True,
            private_backup_identities=private_backup_identities,
        )
        plan["backup"], canonical_backup_receipt = backup_result
        current_plan, current_pending = build_reconciliation_plan(config)
        if not current_plan.get("ok") or current_plan["plan_fingerprint"] != plan["plan_fingerprint"]:
            plan.update(ok=False, status="blocked", error="source_drift_after_backup")
            return plan
        plan.update(status="in_progress", ok=False)
        _run_recovery_lifecycle(recovery_lifecycle, "prepare", plan)
        recovery_prepared = True
        _run_recovery_lifecycle(recovery_lifecycle, "started", plan)
        recovery_started = True

        # This is the only backup-gated schema upgrade path.  The internal
        # ledger session owns the opaque authorization and always revokes it
        # when this coordinator leaves the session.
        session = LedgerReconciliation.open_after_verified_backup(
            pre_backup_proof,
            canonical_backup_receipt,
            config=config,
        )
        try:
            plan["privacy_reconciliation"] = session.reconcile_privacy_schema(
                discard_unattributable_legacy=discard_unattributable_legacy,
                discard_unrecoverable_run_tombstone_history=(
                    discard_unrecoverable_run_tombstone_history
                ),
            )
            _assert_sources_still_match_after_canonical_write(config, current_plan)
            for record in current_pending:
                if record.subject_scope is None:
                    raise ModelCallLedgerReconcileError("unattributable_source_record_reached_import")
                _assert_sources_still_match_after_canonical_write(config, current_plan)
                if session.import_historical_observation(record):
                    plan["imported_count"] += 1
            _assert_sources_still_match_after_canonical_write(config, current_plan)
            canonical_report = dict(current_plan.get("canonical_retired_storage", {}))
            if canonical_report.get("retired_tables"):
                canonical_backup_path, canonical_backup_identity = _verified_backup_for_source(
                    plan["backup"], private_backup_identities, canonical_source
                )
                plan["canonical_cleanup"] = cleanup_canonical_retired_storage(
                    session,
                    expected_report=canonical_report,
                    verified_backup_path=canonical_backup_path,
                    verified_backup_identity=canonical_backup_identity,
                )
        finally:
            session.close()
        plan["discarded_unattributable_source_count"] = int(
            current_plan.get("unattributable_legacy_call_count", 0) or 0
        )
        plan["discarded_unrecoverable_run_tombstone_history"] = int(
            plan["privacy_reconciliation"].get(
                "unrecoverable_run_tombstone_history_discarded", 0
            )
            or 0
        )
        expected_reports = {
            Path(str(report["path"])).name: report for report in current_plan.get("sources", [])
        }
        for source in source_paths:
            source_report = expected_reports.get(source.name)
            if not source_report or not source_report.get("retired_tables"):
                plan["cleanup"].append(
                    {
                        "path": str(source),
                        "dropped_tables": [],
                        "database_removed": False,
                        "status": "skipped_no_retired_storage",
                    }
                )
                continue
            backup_path: Path | None = None
            backup_identity: str | None = None
            if source_report and source_report.get("retired_tables"):
                backup_path, backup_identity = _verified_backup_for_source(
                    plan["backup"], private_backup_identities, source
                )
            plan["cleanup"].append(
                cleanup_source_database(
                    source,
                    expected_report=source_report,
                    verified_backup_path=backup_path,
                    verified_backup_identity=backup_identity,
                )
            )
        inspection = ModelCallLedger.inspect(config)
        plan["post_apply"] = inspection
        required_zero_metrics = (
            "model_call_storage_path_count",
            "health_ledger_path_mismatch",
            "billable_calls_without_ledger",
            "billable_request_without_reservation",
            "settled_cost_without_provider_usage",
            "sensitive_prompt_preview",
            "subject_attribution_schema_missing",
            "entry_subject_attribution_schema_missing",
            "privacy_dispatch_schema_missing",
            "metered_usage_receipt_schema_missing",
            "runtime_schema_gap_count",
            "unattributed_model_call_run_count",
            "unattributed_billable_entry_count",
            "unrecoverable_run_tombstone_history_disposition",
        )
        plan["status"] = "applied" if (
            inspection.get("status") == "ok"
            and int(inspection.get("model_call_storage_path_count", 0) or 0) == 1
            and all(
                int(inspection.get(metric, 0) or 0) == 0
                for metric in required_zero_metrics
                if metric != "model_call_storage_path_count"
            )
        ) else "blocked"
        plan["ok"] = plan["status"] == "applied"
        if not plan["ok"]:
            plan["error"] = "post_apply_ledger_invariants_failed"
        if plan["ok"]:
            _run_recovery_lifecycle(recovery_lifecycle, "commit", plan)
        elif recovery_started:
            _run_recovery_lifecycle(recovery_lifecycle, "failed", plan)
        recovery_finished = True
        return plan
    except (
        ModelCallLedgerReconcileError,
        ModelCallLedgerInvariantError,
        OSError,
        sqlite3.Error,
        RuntimeError,
        ValueError,
    ) as exc:
        plan.update(ok=False, status="blocked", error=_safe_reconcile_error(exc))
        if recovery_prepared and not recovery_finished:
            try:
                _run_recovery_lifecycle(recovery_lifecycle, "failed", plan)
            except ModelCallLedgerReconcileError:
                plan["sealed_recovery_failure_record_error"] = True
        return plan
    finally:
        if migration_lock is not None:
            migration_lock.close()
