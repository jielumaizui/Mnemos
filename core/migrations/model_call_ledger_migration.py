"""Private execution for the registered model-call-ledger migration.

This module deliberately has no public CLI entrypoint.  The generic registry
constructs the narrow hook bridge, keeps sole authority to issue the one-use
reconcile capability, and dispatches here only for the registered
`database.model_call_ledger.v1` migration.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class ModelCallLedgerPlanDetails:
    """Safe COG-018 plan facts consumed by the generic registry."""

    status: str
    operations: tuple[str, ...]
    affected_paths: tuple[str, ...]
    execution_plan_hash: str


@dataclass(frozen=True)
class ModelCallLedgerRegistryHooks:
    """Narrow generic-registry bridge; no caller supplies migration authority."""

    make_transient_record: Callable[..., Any]
    make_record: Callable[..., Any]
    ledger_from_config: Callable[..., Any]
    issue_apply_capability: Callable[..., object]
    revoke_apply_capability: Callable[[object], None]
    json_hash: Callable[[Mapping[str, Any]], str]
    mnemos_dir: Callable[[Any], Path]
    resolve_recovery_ref: Callable[..., str]
    safe_error: Callable[..., str]
    safe_exception: Callable[..., str]


def _reconcile_api() -> Any:
    """Load production reconciliation only through its core facade."""
    from core.migrations import model_call_ledger_reconcile

    if not all(
        callable(getattr(model_call_ledger_reconcile, name, None))
        for name in ("build_reconciliation_plan", "reconcile_model_call_ledger")
    ):
        raise ImportError("model_call_ledger_reconcile_facade_unavailable")
    return model_call_ledger_reconcile


def inspect_registered_model_call_ledger_plan(config: Any) -> ModelCallLedgerPlanDetails:
    """Inspect the COG-018 reconciliation plan without exposing source errors."""
    try:
        reconciliation, _ = _reconcile_api().build_reconciliation_plan(config)
    except (ImportError, OSError, ValueError, sqlite3.Error):
        return ModelCallLedgerPlanDetails(
            status="blocked",
            operations=("inspect canonical ledger failed",),
            affected_paths=(
                "core/migrations/model_call_ledger_reconcile",
                "database:model_call_ledger.db",
            ),
            execution_plan_hash="",
        )

    execution_plan_hash = str(reconciliation.get("plan_fingerprint") or "")
    selected_source_paths = [
        "database:model_call_ledger.db",
        *[
            f"database:{Path(str(report.get('path') or '')).name}"
            for report in reconciliation.get("sources", [])
            if isinstance(report, Mapping)
            and report.get("retired_tables")
            and Path(str(report.get("path") or "")).name
            in {"wiki_state.db", "prompt_calls.db", "sync_log.db"}
        ],
    ]
    affected_paths = (
        "core/migrations/model_call_ledger_reconcile",
        *selected_source_paths,
    )
    reconciliation_status = str(reconciliation.get("status") or "blocked")
    if not reconciliation.get("ok") or reconciliation_status == "blocked":
        return ModelCallLedgerPlanDetails(
            status="blocked",
            operations=("resolve canonical ledger/source integrity before apply",),
            affected_paths=affected_paths,
            execution_plan_hash=execution_plan_hash,
        )
    if reconciliation_status == "clean":
        return ModelCallLedgerPlanDetails(
            status="verified",
            operations=("canonical model-call ledger has no retired storage owners",),
            affected_paths=affected_paths,
            execution_plan_hash=execution_plan_hash,
        )
    if reconciliation.get("requires_explicit_unattributable_discard"):
        canonical_unattributable = int(
            dict(reconciliation.get("canonical_privacy_counts", {})).get(
                "canonical_unattributable_legacy_count", 0
            )
            or 0
        )
        return ModelCallLedgerPlanDetails(
            status="blocked",
            operations=(
                "explicitly discard unattributable legacy observations after verified backup "
                f"(source={int(reconciliation.get('unattributable_legacy_call_count', 0) or 0)}, "
                f"canonical={canonical_unattributable})",
            ),
            affected_paths=affected_paths,
            execution_plan_hash=execution_plan_hash,
        )
    if reconciliation.get("requires_explicit_unrecoverable_run_tombstone_history_discard"):
        return ModelCallLedgerPlanDetails(
            status="blocked",
            operations=(
                "explicitly acknowledge unrecoverable history from the retired cascading "
                "run-tombstone schema after verified backup; this remains release-ineligible",
            ),
            affected_paths=affected_paths,
            execution_plan_hash=execution_plan_hash,
        )
    return ModelCallLedgerPlanDetails(
        status="planned",
        operations=(
            "back up, install hash-only subject attribution if required, "
            "then deduplicate and reconcile retired prompt-call stores",
        ),
        affected_paths=affected_paths,
        execution_plan_hash=execution_plan_hash,
    )


def _record_unsafe_model_call_ledger_outcome(
    hooks: ModelCallLedgerRegistryHooks,
    config: Any,
    spec: Any,
    *,
    ledger_id: str,
    actor: str,
    plan_hash: str,
    verification: Mapping[str, Any],
    backup_dir: Path,
    source_recovery_manifest: str = "",
    recovery_attempt_ledger_id: str = "",
    error: str,
    failure_stage: str,
) -> Any:
    evidence = dict(verification)
    evidence["unsafe_mutation"] = {
        "failure_stage": failure_stage,
        "automatic_recovery": False,
        "manual_recovery_required": True,
        "protected_backup_present": True,
    }
    if recovery_attempt_ledger_id:
        evidence["recovery_attempt_ledger_id"] = recovery_attempt_ledger_id
    record = hooks.make_record(
        ledger_id=ledger_id,
        migration_id=spec.migration_id,
        status="failed",
        plan_hash=plan_hash,
        from_version=spec.from_version,
        to_version=spec.to_version,
        backup_ref=str(backup_dir),
        actor=actor,
        verification=evidence,
        rollback_ref=source_recovery_manifest,
        error=error,
    )
    try:
        hooks.ledger_from_config(config).record(record)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return hooks.make_transient_record(
            spec,
            status="failed",
            plan_hash=plan_hash,
            actor=actor,
            verification=evidence,
            backup_ref=str(backup_dir),
            rollback_ref=source_recovery_manifest,
            error=hooks.safe_exception(
                exc, operation="migration_ledger_record"
            ),
        )
    return record


def apply_registered_model_call_ledger(
    hooks: ModelCallLedgerRegistryHooks,
    config: Any,
    spec: Any,
    *,
    actor: str,
    execute_wrapped: bool,
    expected_plan_hash: str | None,
    discard_unattributable_legacy: bool,
    discard_unrecoverable_run_tombstone_history: bool,
) -> Any:
    try:
        reconciliation_plan, _ = _reconcile_api().build_reconciliation_plan(config)
    except (ImportError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=hooks.json_hash(
                {
                    "migration_id": spec.migration_id,
                    "inspection_error": hooks.safe_exception(
                        exc, operation="model_call_ledger_plan_inspection"
                    ),
                }
            ),
            actor=actor,
            verification={"execution_plan_hash": "", "reviewed_plan_hash": ""},
            error="model_call_ledger_plan_inspection_failed",
        )

    execution_plan_hash = str(reconciliation_plan.get("plan_fingerprint") or "")
    reviewed_plan_hash_matches = bool(
        expected_plan_hash and str(expected_plan_hash) == execution_plan_hash
    )
    verification: dict[str, Any] = {
        "execution_plan_hash": execution_plan_hash,
        # ``expected_plan_hash`` is caller input.  Persist and display only
        # the independently computed plan hash after an exact match; a caller
        # must never be able to place arbitrary text in a migration record.
        "reviewed_plan_hash": (
            execution_plan_hash if reviewed_plan_hash_matches else ""
        ),
        "reviewed_plan_hash_present": bool(expected_plan_hash),
        "reviewed_plan_hash_matches": reviewed_plan_hash_matches,
        "operations": [
            "verify reviewed reconciler plan hash",
            "seal verified preimage recovery bundle after successful reconciliation",
        ],
        "discard_unattributable_legacy": bool(discard_unattributable_legacy),
        "discard_unrecoverable_run_tombstone_history": bool(
            discard_unrecoverable_run_tombstone_history
        ),
    }
    if not reconciliation_plan.get("ok") or reconciliation_plan.get("status") == "blocked":
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error=hooks.safe_error(
                reconciliation_plan.get("error"),
                fallback="model_call_ledger_plan_blocked",
            ),
        )
    if reconciliation_plan.get("status") == "clean":
        verification["reconciliation_status"] = "clean"
        return hooks.make_transient_record(
            spec,
            status="noop",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
        )
    if not execute_wrapped:
        verification["wrapper_command"] = list(spec.wrapper_command)
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error="wrapped_migration_requires_execute_wrapped",
        )
    if not expected_plan_hash:
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error="expected_plan_hash_required",
        )
    if str(expected_plan_hash) != execution_plan_hash:
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error="expected_plan_hash_mismatch",
        )
    if (
        reconciliation_plan.get("requires_explicit_unattributable_discard")
        and not discard_unattributable_legacy
    ):
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error="unattributable_legacy_requires_explicit_discard",
        )
    if (
        reconciliation_plan.get("requires_explicit_retired_stats_discard")
        and not discard_unattributable_legacy
    ):
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error="retired_prompt_stats_requires_explicit_discard",
        )
    if (
        reconciliation_plan.get(
            "requires_explicit_unrecoverable_run_tombstone_history_discard"
        )
        and not discard_unrecoverable_run_tombstone_history
    ):
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error="unrecoverable_run_tombstone_history_requires_explicit_discard",
        )

    from core.migrations.model_call_ledger_recovery import (
        commit_sealed_recovery_bundle,
        fail_sealed_recovery_apply,
        prepare_sealed_recovery_bundle,
        safe_reconciliation_summary,
        start_sealed_recovery_apply,
    )
    backup_dir = (
        hooks.mnemos_dir(config)
        / "backups"
        / "model-call-ledger"
        / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    attempt_ledger_id = f"mig-attempt-{uuid.uuid4().hex[:16]}"
    completed_ledger_id = f"mig-{uuid.uuid4().hex[:16]}"
    failed_ledger_id = f"mig-failed-{uuid.uuid4().hex[:16]}"
    bundle: dict[str, Any] = {}

    def recovery_lifecycle(phase: str, source_plan: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal bundle
        if phase == "prepare":
            prepared = prepare_sealed_recovery_bundle(
                config,
                reconciliation_plan=source_plan,
                backup_dir=backup_dir,
                registry_ledger_id=attempt_ledger_id,
                expected_plan_hash=execution_plan_hash,
            )
            if not prepared.get("ok"):
                return prepared
            verification.update(
                {
                    "recovery_attempt_ledger_id": attempt_ledger_id,
                    "recovery_id": str(prepared["recovery_id"]),
                    "recovery_manifest_sha256": str(prepared["manifest_sha256"]),
                    "recovery_prepare_chain_head": str(
                        prepared["recovery_prepare_chain_head"]
                    ),
                }
            )
            attempt_verification = {
                "execution_plan_hash": execution_plan_hash,
                "reviewed_plan_hash": execution_plan_hash,
                "reviewed_plan_hash_present": True,
                "reviewed_plan_hash_matches": True,
                "recovery_attempt_ledger_id": attempt_ledger_id,
                "recovery_id": str(prepared["recovery_id"]),
                "recovery_manifest_sha256": str(prepared["manifest_sha256"]),
                "recovery_prepare_chain_head": str(prepared["recovery_prepare_chain_head"]),
                "recovery_state": "prepared",
            }
            try:
                hooks.ledger_from_config(config).record(
                    hooks.make_record(
                        ledger_id=attempt_ledger_id,
                        migration_id=spec.migration_id,
                        status="applying",
                        plan_hash=execution_plan_hash,
                        from_version=spec.from_version,
                        to_version=spec.to_version,
                        backup_ref=str(prepared["backup_root"]),
                        actor=actor,
                        verification=attempt_verification,
                        rollback_ref=str(prepared["recovery_manifest"]),
                    )
                )
            except (OSError, sqlite3.Error, ValueError):
                return {
                    "schema_version": str(prepared.get("schema_version") or ""),
                    "status": "blocked",
                    "ok": False,
                    "error": "migration_ledger_attempt_record_failed",
                }
            bundle = dict(prepared)
            return prepared
        if not bundle:
            return {"status": "blocked", "ok": False, "error": "recovery_prepare_required"}
        if phase == "started":
            update = start_sealed_recovery_apply(config, bundle=bundle)
        elif phase == "commit":
            update = commit_sealed_recovery_bundle(
                config, reconciliation_result=source_plan, bundle=bundle
            )
        elif phase == "failed":
            update = fail_sealed_recovery_apply(
                config, bundle=bundle, phase="source_reconciliation"
            )
        else:
            return {"status": "blocked", "ok": False, "error": "recovery_phase_invalid"}
        if update.get("ok"):
            bundle.update(update)
        return update

    capability = hooks.issue_apply_capability(
        attempt_ledger_id=attempt_ledger_id,
        expected_plan_hash=execution_plan_hash,
        lifecycle=recovery_lifecycle,
    )
    try:
        result = _reconcile_api().reconcile_model_call_ledger(
            config,
            apply=True,
            backup_dir=backup_dir,
            expected_plan_hash=execution_plan_hash,
            discard_unattributable_legacy=discard_unattributable_legacy,
            discard_unrecoverable_run_tombstone_history=(
                discard_unrecoverable_run_tombstone_history
            ),
            migration_capability=capability,
        )
    except (ImportError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return hooks.make_transient_record(
            spec,
            status="blocked",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error=hooks.safe_exception(
                exc, operation="model_call_ledger_reconciliation"
            ),
        )
    finally:
        # A source race can make the reconciler return before consuming the
        # capability.  Never retain that abandoned authority in process.
        hooks.revoke_apply_capability(capability)
    verification["reconciliation"] = safe_reconciliation_summary(result)
    result_status = str(result.get("status") or "")
    if result_status == "noop" and result.get("ok"):
        return hooks.make_transient_record(
            spec,
            status="noop",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
        )
    if result_status != "applied" or not result.get("ok"):
        sealed_manifest = str(result.get("sealed_recovery_manifest") or "")
        if sealed_manifest:
            return _record_unsafe_model_call_ledger_outcome(
                hooks,
                config,
                spec,
                ledger_id=failed_ledger_id,
                actor=actor,
                plan_hash=execution_plan_hash,
                verification=verification,
                backup_dir=backup_dir,
                source_recovery_manifest=sealed_manifest,
                recovery_attempt_ledger_id=attempt_ledger_id if bundle else "",
                error=hooks.safe_error(
                    result.get("error"),
                    fallback="model_call_ledger_reconciliation_failed",
                ),
                failure_stage=(
                    "recovery_bundle_commit"
                    if str(result.get("sealed_recovery_status") or "") == "started"
                    else "source_reconciliation"
                ),
            )
        return hooks.make_transient_record(
            spec,
            status="blocked" if result_status == "blocked" else "failed",
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            error=hooks.safe_error(
                result.get("error"),
                fallback="model_call_ledger_reconciliation_failed",
            ),
        )

    if not bundle.get("ok") or bundle.get("status") != "sealed":
        verification["recovery_bundle"] = {
            "schema_version": str(bundle.get("schema_version") or ""),
            "status": str(bundle.get("status") or "blocked"),
        }
        return _record_unsafe_model_call_ledger_outcome(
            hooks,
            config,
            spec,
            ledger_id=failed_ledger_id,
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            backup_dir=backup_dir,
            source_recovery_manifest=str(result.get("sealed_recovery_manifest") or ""),
            recovery_attempt_ledger_id=attempt_ledger_id if bundle else "",
            error=hooks.safe_error(
                bundle.get("error"), fallback="recovery_bundle_seal_failed"
            ),
            failure_stage="recovery_bundle_seal",
        )
    verification.update(
        {
            "recovery_attempt_ledger_id": attempt_ledger_id,
            "recovery_id": str(bundle["recovery_id"]),
            "recovery_manifest_sha256": str(bundle["manifest_sha256"]),
            "recovery_chain_head": str(bundle["recovery_chain_head"]),
            "recovery_backup_id": str(bundle["backup_id"]),
            "preimage_semantic_hash": str(bundle["preimage_semantic_hash"]),
            "recovery_verification": dict(bundle.get("verification") or {}),
        }
    )
    record = hooks.make_record(
        ledger_id=completed_ledger_id,
        migration_id=spec.migration_id,
        status="applied",
        plan_hash=execution_plan_hash,
        from_version=spec.from_version,
        to_version=spec.to_version,
        backup_ref=str(bundle["backup_root"]),
        actor=actor,
        verification=verification,
        rollback_ref=str(bundle["recovery_manifest"]),
    )
    try:
        hooks.ledger_from_config(config).record(record)
    except (OSError, sqlite3.Error, ValueError) as exc:
        return _record_unsafe_model_call_ledger_outcome(
            hooks,
            config,
            spec,
            ledger_id=failed_ledger_id,
            plan_hash=execution_plan_hash,
            actor=actor,
            verification=verification,
            backup_dir=backup_dir,
            source_recovery_manifest=str(bundle["recovery_manifest"]),
            recovery_attempt_ledger_id=attempt_ledger_id,
            error=hooks.safe_exception(
                exc, operation="migration_ledger_record"
            ),
            failure_stage="migration_ledger_commit",
        )
    return record


def rollback_registered_model_call_ledger(
    hooks: ModelCallLedgerRegistryHooks,
    config: Any,
    spec: Any,
    *,
    actor: str,
    recovery_manifest: str | Path | None,
    apply: bool,
    execute_wrapped: bool,
) -> Any:
    manifest_ref = hooks.resolve_recovery_ref(config, recovery_manifest)
    if not recovery_manifest:
        manifest_ref = ""
    fallback_hash = hooks.json_hash(
        {
            "migration_id": spec.migration_id,
            "rollback_ref_present": bool(manifest_ref),
        }
    )
    if not manifest_ref:
        return hooks.make_record(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status="blocked",
            plan_hash=fallback_hash,
            from_version=spec.to_version,
            to_version=spec.from_version,
            backup_ref="",
            actor=actor,
            verification={"recovery_manifest_supplied": False},
            error="recovery_manifest_required",
        )
    read_only_ledger = hooks.ledger_from_config(config, initialize=False)
    try:
        binding = read_only_ledger.find_recovery_by_rollback_ref(
            spec.migration_id, manifest_ref
        )
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return hooks.make_record(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status="blocked",
            plan_hash=fallback_hash,
            from_version=spec.to_version,
            to_version=spec.from_version,
            backup_ref="",
            actor=actor,
            verification={"recovery_manifest_supplied": True},
            error="migration_ledger_recovery_binding_invalid",
        )
    if binding is None:
        return hooks.make_record(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status="blocked",
            plan_hash=fallback_hash,
            from_version=spec.to_version,
            to_version=spec.from_version,
            backup_ref="",
            actor=actor,
            verification={"recovery_manifest_supplied": True},
            error="recovery_manifest_registry_binding_not_found",
        )
    from core.migrations.model_call_ledger_recovery import restore_model_call_ledger

    if apply and not execute_wrapped:
        return hooks.make_record(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status="blocked",
            plan_hash=str(binding.get("plan_hash") or fallback_hash),
            from_version=spec.to_version,
            to_version=spec.from_version,
            backup_ref=str(binding.get("backup_ref") or ""),
            actor=actor,
            verification={
                "source_ledger_id": str(binding.get("ledger_id") or ""),
                "recovery_manifest_supplied": True,
            },
            rollback_ref=manifest_ref,
            error="wrapped_migration_requires_execute_wrapped",
        )
    result = restore_model_call_ledger(
        config,
        recovery_manifest=Path(manifest_ref),
        ledger_binding=binding,
        apply=apply,
    )
    status = str(result.get("status") or "blocked")
    verification = {
        "source_ledger_id": str(binding.get("ledger_id") or ""),
        "recovery_manifest_sha256": str(result.get("manifest_sha256") or ""),
        "recovery_chain_head": str(result.get("chain_head") or ""),
        "reverse_backup_id": str(result.get("reverse_backup_id") or ""),
        "recovery_id": str(result.get("recovery_id") or ""),
    }
    if status == "planned" and result.get("ok"):
        return hooks.make_record(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status="planned",
            plan_hash=str(result.get("expected_plan_hash") or binding.get("plan_hash") or fallback_hash),
            from_version=spec.to_version,
            to_version=spec.from_version,
            backup_ref=str(binding.get("backup_ref") or ""),
            actor=actor,
            verification=verification,
            rollback_ref=manifest_ref,
        )
    if status != "restored" or not result.get("ok"):
        return hooks.make_record(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status="blocked" if status == "blocked" else "failed",
            plan_hash=str(result.get("expected_plan_hash") or binding.get("plan_hash") or fallback_hash),
            from_version=spec.to_version,
            to_version=spec.from_version,
            backup_ref=str(binding.get("backup_ref") or ""),
            actor=actor,
            verification=verification,
            rollback_ref=manifest_ref,
            error=hooks.safe_error(
                result.get("error"), fallback="model_call_ledger_restore_failed"
            ),
        )
    record = hooks.make_record(
        ledger_id=f"mig-{uuid.uuid4().hex[:16]}",
        migration_id=spec.migration_id,
        status="rolled_back",
        plan_hash=str(result.get("expected_plan_hash") or binding.get("plan_hash") or fallback_hash),
        from_version=spec.to_version,
        to_version=spec.from_version,
        backup_ref=str(binding.get("backup_ref") or ""),
        actor=actor,
        verification=verification,
        rollback_ref=manifest_ref,
    )
    try:
        hooks.ledger_from_config(config).record(record)
    except (OSError, sqlite3.Error, ValueError) as exc:
        # Runtime state has already been restored; do not mislabel that as
        # a durable rollback receipt when the append-only record failed.
        return hooks.make_record(
            ledger_id=f"transient-{uuid.uuid4().hex[:16]}",
            migration_id=spec.migration_id,
            status="blocked",
            plan_hash=record.plan_hash,
            from_version=record.from_version,
            to_version=record.to_version,
            backup_ref=record.backup_ref,
            actor=actor,
            verification=verification,
            rollback_ref=manifest_ref,
            error=hooks.safe_exception(
                exc, operation="migration_ledger_rollback_record"
            ),
        )
    return record
