"""Public exact-plan transaction for Agent Native-to-Raw migration."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
)


@dataclass(frozen=True)
class RuntimeDependencies:
    """Explicit transaction seams supplied by the CLI owner for one call."""

    archive_terminal_migration_lineage: Callable[..., list[str]]
    backups_from_records: Callable[..., Any]
    compare_raw_conservation: Callable[..., bool]
    conservation_summary: Callable[..., dict[str, Any]]
    default_runtime_writers_are_inactive: Callable[[Path], bool]
    ensure_private_backup_dir: Callable[[Path], Any]
    execute_recovery: Callable[..., dict[str, Any]]
    file_sha256: Callable[[Path], str]
    mark_reconciliation_receipt_rolled_back: Callable[..., None]
    migration_receipt_path: Callable[[Path, str], Path]
    post_apply_raw_gap: Callable[..., dict[str, Any]]
    raw_conservation_findings: Callable[..., list[dict[str, Any]]]
    read_private_backup_bytes: Callable[[Path, Path], bytes]
    recover_prepared_raw_receipt: Callable[..., str | None]
    restore_drill_ok: Callable[..., bool]
    restore_recovery_state: Callable[..., None]
    safe_raw_conservation: Callable[[Path], dict[str, Any]]
    target_state: Callable[[Any, Path], dict[str, Any]]
    unlink_targets_durably: Callable[..., None]
    verify_completed_raw_receipt: Callable[..., dict[str, Any] | None]
    write_receipt: Callable[[Path, Mapping[str, Any]], None]
    coverage_state_path: Callable[[Path], Path]
    offline_migration_lock: Callable[..., AbstractContextManager[Any]]


def reconcile_active_source_raw_capture(
    *,
    dependencies: RuntimeDependencies,
    config: Any,
    raw_db_path: Path,
    backup_dir: Path,
    sources: Iterable[Any],
    apply: bool,
    cycles: int = 2,
    batch_sessions: int = 100,
    batch_turns: int = 100,
    reset_derived_state: bool = True,
    require_all_active_sources: bool = True,
    runtime_writers_are_inactive: Callable[[], bool] | None = None,
    expected_plan_hash: str = "",
) -> dict[str, Any]:
    """Plan, execute, or verify one exact frozen Native-to-Raw migration."""
    archive_terminal_migration_lineage = (
        dependencies.archive_terminal_migration_lineage
    )
    backups_from_records = dependencies.backups_from_records
    compare_raw_conservation = dependencies.compare_raw_conservation
    conservation_summary = dependencies.conservation_summary
    default_runtime_writers_are_inactive = (
        dependencies.default_runtime_writers_are_inactive
    )
    ensure_private_backup_dir = dependencies.ensure_private_backup_dir
    execute_recovery = dependencies.execute_recovery
    file_sha256 = dependencies.file_sha256
    mark_reconciliation_receipt_rolled_back = (
        dependencies.mark_reconciliation_receipt_rolled_back
    )
    migration_receipt_path = dependencies.migration_receipt_path
    post_apply_raw_gap = dependencies.post_apply_raw_gap
    raw_conservation_findings = dependencies.raw_conservation_findings
    read_private_backup_bytes = dependencies.read_private_backup_bytes
    recover_prepared_raw_receipt = dependencies.recover_prepared_raw_receipt
    restore_drill_ok = dependencies.restore_drill_ok
    restore_recovery_state = dependencies.restore_recovery_state
    safe_raw_conservation = dependencies.safe_raw_conservation
    target_state = dependencies.target_state
    unlink_targets_durably = dependencies.unlink_targets_durably
    verify_completed_raw_receipt = dependencies.verify_completed_raw_receipt
    write_receipt = dependencies.write_receipt
    coverage_state_path = dependencies.coverage_state_path
    migration_lock = dependencies.offline_migration_lock
    source_list = list(sources)
    if expected_plan_hash and not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        expected_plan_hash,
    ):
        raise AgentSourceRawReconciliationError("expected_plan_hash_invalid")
    if not apply:
        return execute_recovery(
            config=config,
            raw_db_path=raw_db_path,
            backup_dir=backup_dir,
            sources=source_list,
            apply=False,
            cycles=cycles,
            batch_sessions=batch_sessions,
            batch_turns=batch_turns,
            reset_derived_state=reset_derived_state,
            require_all_active_sources=require_all_active_sources,
            runtime_writers_are_inactive=runtime_writers_are_inactive,
            expected_plan_hash=expected_plan_hash,
        )
    if not expected_plan_hash:
        raise AgentSourceRawReconciliationError("expected_plan_hash_required")
    database_dir = Path(config.database_dir)
    is_inactive = runtime_writers_are_inactive or (
        lambda: default_runtime_writers_are_inactive(database_dir)
    )
    if not is_inactive():
        raise AgentSourceRawReconciliationError("daemon_not_inactive")
    resolved_backup = Path(backup_dir).expanduser().resolve(strict=False)
    resolved_database_dir = database_dir.expanduser().resolve(strict=False)
    if resolved_backup == resolved_database_dir or resolved_backup in resolved_database_dir.parents:
        raise AgentSourceRawReconciliationError("backup_scope_overlaps_database")
    try:
        with migration_lock(
            database_dir,
            daemon_check=lambda _database_dir: bool(is_inactive()),
        ):
            ensure_private_backup_dir(resolved_backup)
            receipt_path = migration_receipt_path(
                resolved_backup,
                expected_plan_hash,
            )
            receipt_status = recover_prepared_raw_receipt(
                config=config,
                raw_db_path=raw_db_path,
                backup_dir=resolved_backup,
                expected_plan_hash=expected_plan_hash,
            )
            prior_terminal_receipts: list[str] = []
            if receipt_status == "completed":
                repeated = verify_completed_raw_receipt(
                    config=config,
                    raw_db_path=raw_db_path,
                    backup_dir=resolved_backup,
                    sources=source_list,
                    expected_plan_hash=expected_plan_hash,
                )
                if repeated is not None:
                    return repeated
            elif receipt_status == "recovered_rollback":
                prior_terminal_receipts = archive_terminal_migration_lineage(
                    receipt_path=receipt_path,
                    backup_dir=resolved_backup,
                    plan_hash=expected_plan_hash,
                )
            before = target_state(config, raw_db_path)
            before_conservation = safe_raw_conservation(raw_db_path)
            before_conservation_summary = conservation_summary(
                before_conservation
            )
            prepared_intent_written = False
            reviewed_plan: dict[str, Any] | None = None

            def persist_reviewed_plan(value: Mapping[str, Any]) -> None:
                nonlocal reviewed_plan
                if (
                    reviewed_plan is not None
                    or value.get("plan_hash") != expected_plan_hash
                    or value.get("apply_eligible") is not True
                ):
                    raise AgentSourceRawReconciliationError(
                        "migration_reviewed_plan_binding_mismatch"
                    )
                reviewed_plan = dict(value)

            def require_reviewed_plan() -> dict[str, Any]:
                if reviewed_plan is None:
                    raise AgentSourceRawReconciliationError(
                        "migration_reviewed_plan_missing"
                    )
                return reviewed_plan

            def persist_prepared_intent(inner: Mapping[str, Any]) -> None:
                nonlocal prepared_intent_written
                plan = require_reviewed_plan()
                inner_receipt_filename = str(inner["receipt_filename"])
                inner_receipt_path = resolved_backup / inner_receipt_filename
                try:
                    write_receipt(
                        receipt_path,
                        {
                            "schema_version": "mnemos.agent_source_raw_migration_receipt.v1",
                            "status": "prepared",
                            "plan_hash": expected_plan_hash,
                            "reviewed_plan": plan["canonical_plan"],
                            "raw_db": str(Path(raw_db_path).resolve()),
                            "backup_dir": str(resolved_backup),
                            "native_inventory_hash": plan["native_artifact_inventory"][
                                "inventory_hash"
                            ],
                            "require_all_active_sources": require_all_active_sources,
                            "before_state": before,
                            "before_conservation": before_conservation_summary,
                            "backups": inner["backups"],
                            "inner_receipt_filename": inner_receipt_filename,
                            "inner_prepared_receipt_sha256": file_sha256(
                                inner_receipt_path
                            ),
                            "prior_terminal_receipts": prior_terminal_receipts,
                        },
                    )
                    prepared_intent_written = True
                except BaseException:
                    unlink_targets_durably(
                        (receipt_path,),
                        error_code="unbound_migration_receipt_cleanup_failed",
                    )
                    raise

            try:
                applied = execute_recovery(
                    config=config,
                    raw_db_path=raw_db_path,
                    backup_dir=resolved_backup,
                    sources=source_list,
                    apply=True,
                    cycles=cycles,
                    batch_sessions=batch_sessions,
                    batch_turns=batch_turns,
                    reset_derived_state=reset_derived_state,
                    require_all_active_sources=require_all_active_sources,
                    runtime_writers_are_inactive=lambda: True,
                    expected_plan_hash=expected_plan_hash,
                    reviewed_plan_sink=persist_reviewed_plan,
                    prepared_intent_sink=persist_prepared_intent,
                )
                plan = require_reviewed_plan()
                prepared_receipt = json.loads(
                    read_private_backup_bytes(
                        receipt_path,
                        resolved_backup,
                    ).decode("utf-8")
                )
                inner_receipt_path = resolved_backup / str(applied.get("receipt_filename") or "")
                inner_receipt = json.loads(
                    read_private_backup_bytes(
                        inner_receipt_path,
                        resolved_backup,
                    ).decode("utf-8")
                )
                write_receipt(
                    receipt_path,
                    {
                        **prepared_receipt,
                        "inner_receipt_status": inner_receipt.get("status"),
                        "inner_receipt_sha256": file_sha256(inner_receipt_path),
                    },
                )
            except AgentSourceRawReconciliationError as exc:
                if (
                    target_state(config, raw_db_path) != before
                    or conservation_summary(safe_raw_conservation(raw_db_path))
                    != before_conservation_summary
                ):
                    raise AgentSourceRawReconciliationError("rollback_failed") from None
                if prepared_intent_written:
                    try:
                        prepared_receipt = json.loads(
                            read_private_backup_bytes(
                                receipt_path,
                                resolved_backup,
                            ).decode("utf-8")
                        )
                        if prepared_receipt.get("status") != "prepared":
                            raise AgentSourceRawReconciliationError(
                                "migration_receipt_binding_mismatch"
                            )
                        mark_reconciliation_receipt_rolled_back(
                            backup_dir=resolved_backup,
                            applied={
                                "receipt_filename": prepared_receipt["inner_receipt_filename"]
                            },
                        )
                        rolled_back_inner_path = (
                            resolved_backup / prepared_receipt["inner_receipt_filename"]
                        )
                        write_receipt(
                            receipt_path,
                            {
                                **prepared_receipt,
                                "status": "recovered_rollback",
                                "rollback_ok": True,
                                "recovered_after_inner_failure": True,
                                "inner_error_code": exc.code,
                                "inner_receipt_status": ("rolled_back_by_migration_certification"),
                                "inner_receipt_sha256": file_sha256(
                                    rolled_back_inner_path
                                ),
                            },
                        )
                    except (
                        OSError,
                        UnicodeError,
                        json.JSONDecodeError,
                    ):
                        raise AgentSourceRawReconciliationError(
                            "migration_evidence_write_failed"
                        ) from None
                raise
            certification_completed = False
            try:
                after_conservation = safe_raw_conservation(raw_db_path)
                structural_comparator_ok = bool(
                    applied.get("ok")
                    and applied.get("after_challenger", {}).get("ok")
                    and applied.get("raw_only_boundary_ok")
                    and int(applied.get("unexpected_mutation_count") or 0) == 0
                    and all(
                        bool(item.get("ok")) for item in applied.get("source_capture", {}).values()
                    )
                    and applied.get("session_identity_reconciliation", {}).get("ok") is True
                )
                conservation_ok = compare_raw_conservation(
                    before_conservation,
                    after_conservation,
                )
                conservation_findings = raw_conservation_findings(
                    before_conservation,
                    after_conservation,
                )
                if conservation_ok is False and not conservation_findings:
                    conservation_findings = [
                        {
                            "table": "__comparator__",
                            "rule": "boolean_finding_disagreement",
                            "mismatch_count": 1,
                        }
                    ]
                structural_findings = [
                    rule
                    for rule, passed in (
                        ("inner_result_ok", bool(applied.get("ok"))),
                        (
                            "after_challenger_ok",
                            bool(applied.get("after_challenger", {}).get("ok")),
                        ),
                        (
                            "raw_only_boundary_ok",
                            bool(applied.get("raw_only_boundary_ok")),
                        ),
                        (
                            "unexpected_mutation_count_zero",
                            int(applied.get("unexpected_mutation_count") or 0) == 0,
                        ),
                        (
                            "all_source_capture_ok",
                            all(
                                bool(item.get("ok"))
                                for item in applied.get("source_capture", {}).values()
                            ),
                        ),
                        (
                            "session_identity_reconciliation_ok",
                            applied.get("session_identity_reconciliation", {}).get("ok") is True,
                        ),
                    )
                    if not passed
                ]
                first_apply_comparator = {
                    "ok": bool(structural_comparator_ok and conservation_ok),
                    "structural_ok": structural_comparator_ok,
                    "structural_findings": structural_findings,
                    "conservation_ok": conservation_ok,
                    "conservation_findings": conservation_findings,
                    "before": conservation_summary(before_conservation),
                    "after": conservation_summary(after_conservation),
                }
                if not structural_comparator_ok or not conservation_ok:
                    prepared_receipt = json.loads(
                        read_private_backup_bytes(
                            receipt_path,
                            resolved_backup,
                        ).decode("utf-8")
                    )
                    write_receipt(
                        receipt_path,
                        {
                            **prepared_receipt,
                            "status": "certification_failed",
                            "certification_error_code": ("first_apply_conservation_failed"),
                            "first_apply_comparator": first_apply_comparator,
                        },
                    )
                    raise AgentSourceRawReconciliationError("first_apply_conservation_failed")
                if not restore_drill_ok(
                    before=before,
                    backups=applied["backups"],
                    backup_dir=resolved_backup,
                ):
                    raise AgentSourceRawReconciliationError("backup_restore_drill_failed")
                post_gap = post_apply_raw_gap(
                    config=config,
                    raw_db_path=raw_db_path,
                    sources=source_list,
                    expected_inventory_hash=plan["native_artifact_inventory"]["inventory_hash"],
                    require_all_active_sources=require_all_active_sources,
                    session_identity_reconciliation=plan["session_identity_reconciliation"],
                )
                if not post_gap["ok"]:
                    raise AgentSourceRawReconciliationError("post_apply_gap_nonzero")
                post = target_state(config, raw_db_path)
                comparator = first_apply_comparator
                inner_receipt_filename = str(applied.get("receipt_filename") or "")
                inner_receipt_path = resolved_backup / inner_receipt_filename
                write_receipt(
                    receipt_path,
                    {
                        "schema_version": "mnemos.agent_source_raw_migration_receipt.v1",
                        "status": "completed",
                        "plan_hash": expected_plan_hash,
                        "reviewed_plan": plan["canonical_plan"],
                        "raw_db": str(Path(raw_db_path).resolve()),
                        "backup_dir": str(resolved_backup),
                        "native_inventory_hash": plan["native_artifact_inventory"][
                            "inventory_hash"
                        ],
                        "require_all_active_sources": (require_all_active_sources),
                        "before_state": before,
                        "before_conservation": before_conservation_summary,
                        "post_state": post,
                        "backups": applied["backups"],
                        "first_apply_comparator": comparator,
                        "post_apply_gap": post_gap,
                        "restore_drill_ok": True,
                        "session_identity_reconciliation": applied[
                            "session_identity_reconciliation"
                        ],
                        "current_projection_reconciliation": applied[
                            "current_projection_reconciliation"
                        ],
                        "inner_receipt_filename": (inner_receipt_filename),
                        "inner_prepared_receipt_sha256": applied["prepared_receipt_sha256"],
                        "inner_receipt_sha256": file_sha256(inner_receipt_path),
                        "prior_terminal_receipts": (prior_terminal_receipts),
                        "required_gap": post_gap["required_gap"],
                    },
                )
                second = verify_completed_raw_receipt(
                    config=config,
                    raw_db_path=raw_db_path,
                    backup_dir=resolved_backup,
                    sources=source_list,
                    expected_plan_hash=expected_plan_hash,
                )
                if second is None:
                    raise AgentSourceRawReconciliationError("second_apply_receipt_missing")
                certification_completed = True
            except AgentSourceRawReconciliationError:
                raise
            except (OSError, sqlite3.Error, ValueError, TypeError):
                raise AgentSourceRawReconciliationError("migration_evidence_write_failed") from None
            finally:
                if not certification_completed:
                    try:
                        restore_recovery_state(
                            backups=backups_from_records(
                                applied["backups"],
                                resolved_backup,
                            ),
                            raw_db_path=Path(raw_db_path),
                            cursor_path=database_dir / "agent_sync_cursors.db",
                            coverage_path=coverage_state_path(database_dir),
                        )
                        if target_state(config, raw_db_path) != before:
                            raise AgentSourceRawReconciliationError("rollback_state_mismatch")
                        if (
                            conservation_summary(
                                safe_raw_conservation(raw_db_path)
                            )
                            != before_conservation_summary
                        ):
                            raise AgentSourceRawReconciliationError("rollback_state_mismatch")
                        mark_reconciliation_receipt_rolled_back(
                            backup_dir=resolved_backup,
                            applied=applied,
                        )
                        rolled_back_inner_path = resolved_backup / str(
                            applied.get("receipt_filename") or ""
                        )
                        prepared_receipt = json.loads(
                            read_private_backup_bytes(
                                receipt_path,
                                resolved_backup,
                            ).decode("utf-8")
                        )
                        write_receipt(
                            receipt_path,
                            {
                                **prepared_receipt,
                                "status": "recovered_rollback",
                                "rollback_ok": True,
                                "recovered_after_certification_failure": True,
                                "inner_receipt_status": ("rolled_back_by_migration_certification"),
                                "inner_receipt_sha256": file_sha256(
                                    rolled_back_inner_path
                                ),
                            },
                        )
                    except (
                        AgentSourceRawReconciliationError,
                        OSError,
                        UnicodeError,
                        json.JSONDecodeError,
                    ):
                        raise AgentSourceRawReconciliationError("rollback_failed") from None
            applied.update(
                {
                    "first_apply": {
                        "comparator_ok": True,
                        "conservation_ok": True,
                    },
                    "restore_drill_ok": True,
                    "second_apply_changed": False,
                    "post_apply_gap": post_gap,
                    "required_gap": post_gap["required_gap"],
                    "receipt_filename": receipt_path.name,
                }
            )
            return applied
    except AgentSourceRawReconciliationError:
        raise
    except KeyboardInterrupt:
        raise AgentSourceRawReconciliationError("reconciliation_interrupted") from None
    except RuntimeError:
        raise AgentSourceRawReconciliationError("writer_lock_unavailable") from None
