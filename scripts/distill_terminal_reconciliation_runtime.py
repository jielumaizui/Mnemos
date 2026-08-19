"""Transactional runtime for terminal receipt reconciliation.

This module owns the bounded apply transaction. The CLI module retains the
plan, validation, backup, and receipt primitives and re-exports this entry.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.pipeline_receipts import DistillationWriteReceipt


@dataclass(frozen=True)
class RuntimeDependencies:
    """Explicit call-local seams for one terminal reconciliation."""

    schema_version: str
    source_span_reason_prefix: str
    terminal_statuses: frozenset[str]
    atomic_write_json: Callable[..., Any]
    backup_database_set: Callable[..., Any]
    canonical_sha256: Callable[..., str]
    cognitive_event_ids: Callable[..., Any]
    commit_terminal_outbox: Callable[..., str]
    connect_read_only: Callable[..., Any]
    conservation_snapshot: Callable[..., dict[str, Any]]
    distill_cognitive_heads: Callable[..., Any]
    ensure_terminal_outbox_anchor_schema: Callable[..., Any]
    failed_terminal_outbox: Callable[..., dict[str, Any]]
    has_cognitive_event_ids: Callable[..., bool]
    migration_receipt_path: Callable[..., Path]
    outbox_matches_candidate: Callable[..., bool]
    prepare_terminal_outbox: Callable[..., str]
    produced_generations: Callable[..., Any]
    reconciler_code_identity: Callable[..., dict[str, Any]]
    rollback_terminal_reconciliation: Callable[..., Any]
    runtime_writers_are_inactive: Callable[[Path], bool]
    source_span_replacements: Callable[..., Any]
    source_span_runtime_corrections: Callable[..., Any]
    sqlite_family_preimages: Callable[..., Any]
    success_terminal_outbox: Callable[..., dict[str, Any]]
    task_rows: Callable[..., Any]
    validate_terminal_task: Callable[..., Any]
    write_final_migration_receipt: Callable[..., Any]
    inspect_terminal_cognitive_state: Callable[..., Any]
    inspect_terminal_runtime_state: Callable[..., Any]
    record_cognitive_data_consumed: Callable[..., Any]
    record_failed_terminal: Callable[..., Any]
    record_generation_superseded: Callable[..., Any]
    record_handoff: Callable[..., Any]
    record_terminal: Callable[..., Any]
    verify_failed_terminal: Callable[..., Any]
    verify_terminal: Callable[..., Any]


def reconcile_terminal_runtime_receipts(
    config: Any,
    *,
    dependencies: RuntimeDependencies,
    apply: bool,
    backup_dir: Path | None = None,
    expected_plan_sha256: str | None = None,
    legacy_naive_timezone: str | None = None,
) -> dict[str, Any]:
    """Inspect or reconcile only typed, durable terminal task evidence."""
    SCHEMA_VERSION = dependencies.schema_version
    _SOURCE_SPAN_REASON_PREFIX = dependencies.source_span_reason_prefix
    _TERMINAL_STATUSES = dependencies.terminal_statuses
    _atomic_write_json = dependencies.atomic_write_json
    _backup_database_set = dependencies.backup_database_set
    _canonical_sha256 = dependencies.canonical_sha256
    _cognitive_event_ids = dependencies.cognitive_event_ids
    _commit_terminal_outbox = dependencies.commit_terminal_outbox
    _connect_read_only = dependencies.connect_read_only
    _conservation_snapshot = dependencies.conservation_snapshot
    _distill_cognitive_heads = dependencies.distill_cognitive_heads
    _ensure_terminal_outbox_anchor_schema = dependencies.ensure_terminal_outbox_anchor_schema
    _failed_terminal_outbox = dependencies.failed_terminal_outbox
    _has_cognitive_event_ids = dependencies.has_cognitive_event_ids
    _migration_receipt_path = dependencies.migration_receipt_path
    _outbox_matches_candidate = dependencies.outbox_matches_candidate
    _prepare_terminal_outbox = dependencies.prepare_terminal_outbox
    _produced_generations = dependencies.produced_generations
    _reconciler_code_identity = dependencies.reconciler_code_identity
    _rollback_terminal_reconciliation = dependencies.rollback_terminal_reconciliation
    _runtime_writers_are_inactive = dependencies.runtime_writers_are_inactive
    _source_span_replacements = dependencies.source_span_replacements
    _source_span_runtime_corrections = dependencies.source_span_runtime_corrections
    _sqlite_family_preimages = dependencies.sqlite_family_preimages
    _success_terminal_outbox = dependencies.success_terminal_outbox
    _task_rows = dependencies.task_rows
    _validate_terminal_task = dependencies.validate_terminal_task
    _write_final_migration_receipt = dependencies.write_final_migration_receipt
    inspect_distillation_terminal_cognitive_state = dependencies.inspect_terminal_cognitive_state
    inspect_distillation_terminal_runtime_state = dependencies.inspect_terminal_runtime_state
    record_cognitive_data_consumed = dependencies.record_cognitive_data_consumed
    record_distillation_failed_terminal = dependencies.record_failed_terminal
    record_distillation_generation_superseded = dependencies.record_generation_superseded
    record_distillation_handoff = dependencies.record_handoff
    record_distillation_terminal = dependencies.record_terminal
    verify_distillation_failed_terminal = dependencies.verify_failed_terminal
    verify_distillation_terminal = dependencies.verify_terminal
    database_dir = Path(config.database_dir)
    ledger_path = database_dir / "producer_consumer_ledger.db"
    queue_path = database_dir / "distill_queue.db"
    wiki_projection_path = database_dir / "wiki_projection.db"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry_run",
        "ok": False,
        "terminal_tasks": 0,
        "lifecycle_proven_terminal_tasks": 0,
        "outside_scope_terminal_tasks": 0,
        "candidate_tasks": 0,
        "already_receipted": 0,
        "reconciled_tasks": 0,
        "receipts_recorded": 0,
        "handoff_candidate_tasks": 0,
        "handoff_tasks_reconciled": 0,
        "terminal_cognitive_candidate_tasks": 0,
        "terminal_cognitive_tasks_reconciled": 0,
        "cognitive_receipts_deferred": 0,
        "source_span_superseded_tasks": 0,
        "source_span_runtime_corrections_required": 0,
        "source_span_runtime_corrections_recorded": 0,
        "source_span_cognitive_corrections_required": 0,
        "source_span_cognitive_corrections_recorded": 0,
        "source_span_corrections_deferred": 0,
        "unproven_by_reason": {},
        "outstanding_statuses": {},
        "backup": None,
        "migration_receipt": None,
        "plan_sha256": "",
        "semantic_plan_sha256": "",
        "reviewed_plan": None,
        "plan_entries": 0,
        "preimages": {},
        "code_identity": {},
        "conservation": {},
        "terminal_outboxes_missing": 0,
        "failed_terminal_tasks": 0,
        "failed_terminal_outboxes_missing": 0,
        "terminal_outboxes_prepared": 0,
        "terminal_outboxes_committed": 0,
        "failed_terminal_outboxes_prepared": 0,
        "failed_terminal_outboxes_committed": 0,
        "manual_reconciliation_required": 0,
        "legacy_naive_timezone": str(legacy_naive_timezone or "").strip(),
        "rollback": None,
    }
    if not ledger_path.is_file() or not queue_path.is_file():
        result["error"] = "required_database_missing"
        return result
    if apply and backup_dir is None:
        result["error"] = "backup_directory_required"
        return result
    if apply and backup_dir is not None and backup_dir.exists() and any(backup_dir.iterdir()):
        result["error"] = "backup_directory_must_be_empty"
        return result
    if apply and not str(expected_plan_sha256 or "").strip():
        result["error"] = "reviewed_plan_sha256_required"
        return result
    if apply and not _runtime_writers_are_inactive(database_dir):
        result["error"] = "daemon_not_inactive"
        return result
    timezone_name = str(legacy_naive_timezone or "").strip()
    try:
        historical_timezone = ZoneInfo(timezone_name) if timezone_name else None
    except ZoneInfoNotFoundError:
        result["error"] = "legacy_naive_timezone_invalid"
        return result

    conservation_before: dict[str, Any] | None = None
    prepared_receipt: dict[str, Any] | None = None
    prepared_receipt_sha256 = ""
    receipt_path: Path | None = None
    try:
        produced_events, consumed_production_ids = _produced_generations(ledger_path)
        tasks = _task_rows(queue_path)
        event_task_references: Counter[str] = Counter()
        for task in tasks:
            event_task_references.update(_cognitive_event_ids(task.get("meta")))
        ambiguous_cognitive_events = {
            event_id for event_id, count in event_task_references.items() if count != 1
        }
        source_span_replacements = _source_span_replacements(queue_path)
        source_span_runtime_corrected = _source_span_runtime_corrections(ledger_path)
        cognitive_heads = _distill_cognitive_heads(ledger_path)
        terminal_tasks = [
            task for task in tasks if str(task.get("status") or "") in _TERMINAL_STATUSES
        ]
        with _connect_read_only(queue_path) as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) FROM distillation_tasks GROUP BY status"
            ).fetchall()
        result["outstanding_statuses"] = {
            str(status): int(count)
            for status, count in status_rows
            if str(status) not in _TERMINAL_STATUSES
        }
        terminal_reconciliations: list[
            tuple[
                dict[str, Any],
                DistillationWriteReceipt,
                str | None,
                bool,
                dict[str, Any],
            ]
        ] = []
        failed_terminal_reconciliations: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
        source_span_reconciliations: list[
            tuple[dict[str, Any], str, str | None, list[tuple[str, dict[str, Any]]]]
        ] = []
        reasons: Counter[str] = Counter()
        plan_entries: list[dict[str, Any]] = []
        for task in tasks:
            if _has_cognitive_event_ids(task):
                result["handoff_candidate_tasks"] += 1
        for task in terminal_tasks:
            task_key = (str(task["task_id"]), str(task["input_revision"]))
            production_event_id = produced_events.get(task_key)
            referenced_events = _cognitive_event_ids(task.get("meta"))
            if task.get("parse_error"):
                reasons["queue_terminal_json_invalid"] += 1
                result["manual_reconciliation_required"] += 1
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": str(task["status"]),
                        "production_event_id": production_event_id or "",
                        "disposition": "manual:queue_terminal_json_invalid",
                    }
                )
                continue
            replacement_task_id = source_span_replacements.get(str(task["task_id"]))
            if replacement_task_id and str(task.get("terminal_reason") or "") == (
                _SOURCE_SPAN_REASON_PREFIX + replacement_task_id
            ):
                result["source_span_superseded_tasks"] += 1
                cognitive_corrections: list[tuple[str, dict[str, Any]]] = []
                for event_id in _cognitive_event_ids(task.get("meta", {})):
                    current = cognitive_heads.get(event_id)
                    if current is None:
                        continue
                    metadata = current.get("metadata", {})
                    already_reopened = (
                        current.get("status") == "revoked"
                        and metadata.get("reopen_required") is True
                        and metadata.get("replacement_task_id") == replacement_task_id
                        and bool(current.get("supersedes_consumption_id"))
                        and current.get("supersedes_consumption_id")
                        == current.get("correction_of_consumption_id")
                    )
                    false_terminal = current.get(
                        "outcome"
                    ) == "distill_task_intentional_skip" and metadata.get("task_id") == str(
                        task["task_id"]
                    )
                    if false_terminal and not already_reopened:
                        cognitive_corrections.append((event_id, current))
                if (
                    production_event_id is not None
                    and production_event_id not in source_span_runtime_corrected
                ):
                    result["source_span_runtime_corrections_required"] += 1
                result["source_span_cognitive_corrections_required"] += len(cognitive_corrections)
                source_span_reconciliations.append(
                    (task, replacement_task_id, production_event_id, cognitive_corrections)
                )
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": str(task["status"]),
                        "production_event_id": production_event_id or "",
                        "disposition": "source_span_generation_superseded",
                        "replacement_task_id": replacement_task_id,
                    }
                )
                continue
            receipt, reason = _validate_terminal_task(
                task,
                wiki_projection_path=wiki_projection_path,
                legacy_naive_timezone=historical_timezone,
            )
            if receipt is None:
                if production_event_id:
                    reasons[reason] += 1
                    result["manual_reconciliation_required"] += 1
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": str(task["status"]),
                        "production_event_id": production_event_id or "",
                        "disposition": f"manual:{reason}",
                    }
                )
                continue
            if reason == "valid_lifecycle_removal":
                result["lifecycle_proven_terminal_tasks"] += 1
            if _has_cognitive_event_ids(task):
                result["terminal_cognitive_candidate_tasks"] += 1
            if any(event_id in ambiguous_cognitive_events for event_id in referenced_events):
                reasons["ambiguous_cognitive_event_task_mapping"] += 1
                result["manual_reconciliation_required"] += 1
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": str(task["status"]),
                        "production_event_id": production_event_id or "",
                        "disposition": ("manual:ambiguous_cognitive_event_task_mapping"),
                        "cognitive_event_ids": list(referenced_events),
                    }
                )
                continue
            if production_event_id:
                already_receipted = production_event_id in consumed_production_ids
                if already_receipted:
                    result["already_receipted"] += 1
                if not str(task.get("completed_at") or ""):
                    reasons["terminal_completed_at_missing"] += 1
                    result["manual_reconciliation_required"] += 1
                    plan_entries.append(
                        {
                            "task_id": str(task["task_id"]),
                            "input_revision": str(task["input_revision"]),
                            "status": str(task["status"]),
                            "production_event_id": production_event_id,
                            "disposition": "manual:terminal_completed_at_missing",
                        }
                    )
                    continue
                outbox = _success_terminal_outbox(task, receipt)
                runtime_terminal = inspect_distillation_terminal_runtime_state(
                    config,
                    task=task,
                    receipt=receipt,
                )
                if runtime_terminal.get("state") in {
                    "conflict",
                    "identity_missing",
                    "production_missing",
                    "runtime_ledger_missing",
                }:
                    terminal_reason = (
                        "terminal_receipt_conflict"
                        if runtime_terminal.get("state") == "conflict"
                        else str(runtime_terminal.get("state"))
                    )
                    reasons[terminal_reason] += 1
                    result["manual_reconciliation_required"] += 1
                    plan_entries.append(
                        {
                            "task_id": str(task["task_id"]),
                            "input_revision": str(task["input_revision"]),
                            "status": str(task["status"]),
                            "production_event_id": production_event_id,
                            "disposition": f"manual:{terminal_reason}",
                        }
                    )
                    continue
                cognitive_terminal = inspect_distillation_terminal_cognitive_state(
                    config,
                    task=task,
                    receipt=receipt,
                )
                if cognitive_terminal.get("state") in {
                    "conflict",
                    "runtime_ledger_missing",
                }:
                    reasons["cognitive_terminal_conflict"] += 1
                    result["manual_reconciliation_required"] += 1
                    plan_entries.append(
                        {
                            "task_id": str(task["task_id"]),
                            "input_revision": str(task["input_revision"]),
                            "status": str(task["status"]),
                            "production_event_id": production_event_id,
                            "disposition": ("manual:cognitive_terminal_conflict"),
                            "conflicting_cognitive_event_ids": list(
                                cognitive_terminal.get(
                                    "conflicting_event_ids",
                                    [],
                                )
                            ),
                        }
                    )
                    continue
                existing_outbox = task.get("meta", {}).get("terminal_receipt_outbox")
                if existing_outbox is None:
                    result["terminal_outboxes_missing"] += 1
                elif not _outbox_matches_candidate(
                    existing_outbox,
                    outbox,
                ):
                    reasons["terminal_outbox_conflict"] += 1
                    result["manual_reconciliation_required"] += 1
                    plan_entries.append(
                        {
                            "task_id": str(task["task_id"]),
                            "input_revision": str(task["input_revision"]),
                            "status": str(task["status"]),
                            "production_event_id": production_event_id,
                            "disposition": "manual:terminal_outbox_conflict",
                        }
                    )
                    continue
                result["candidate_tasks"] += 1
                terminal_reconciliations.append(
                    (
                        task,
                        receipt,
                        production_event_id,
                        already_receipted,
                        outbox,
                    )
                )
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "session_id": str(task["session_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": str(task["status"]),
                        "production_event_id": production_event_id,
                        "disposition": "typed_terminal_outbox_replay",
                        "receipt_sha256": outbox["receipt_sha256"],
                        "cognitive_event_ids": list(referenced_events),
                        "runtime_terminal_action": {
                            "missing": "append_new_terminal",
                            "exact": "reuse_exact_terminal",
                            "legacy_reconcilable": ("append_legacy_supersession"),
                        }[str(runtime_terminal["state"])],
                        "supersedes_receipt_ids": list(
                            runtime_terminal.get(
                                "supersedes_receipt_ids",
                                [],
                            )
                        ),
                        "supersession_reason": str(
                            runtime_terminal.get("supersession_reason") or ""
                        ),
                        "cognitive_terminal_action": {
                            "not_applicable": "not_applicable",
                            "missing": "append_new_terminal",
                            "exact": "reuse_exact_terminal",
                            "legacy_reconcilable": ("append_legacy_supersession"),
                        }[str(cognitive_terminal["state"])],
                        "supersedes_cognitive_consumption_ids": list(
                            cognitive_terminal.get(
                                "supersedes_consumption_ids",
                                [],
                            )
                        ),
                        "cognitive_supersession_reasons": list(
                            cognitive_terminal.get(
                                "supersession_reasons",
                                [],
                            )
                        ),
                    }
                )
            else:
                result["outside_scope_terminal_tasks"] += 1
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": str(task["status"]),
                        "production_event_id": "",
                        "disposition": "outside_runtime_scope",
                    }
                )

        failed_tasks = [task for task in tasks if str(task.get("status") or "") == "failed"]
        result["failed_terminal_tasks"] = len(failed_tasks)
        for task in failed_tasks:
            task_key = (str(task["task_id"]), str(task["input_revision"]))
            production_event_id = produced_events.get(task_key)
            referenced_events = _cognitive_event_ids(task.get("meta"))
            reason = ""
            if task.get("parse_error"):
                reason = "queue_terminal_json_invalid"
            elif not production_event_id:
                reason = "production_missing"
            elif not str(task.get("completed_at") or ""):
                reason = "failed_completed_at_missing"
            elif not str(task.get("terminal_reason") or ""):
                reason = "failed_reason_missing"
            elif int(task.get("max_retries") or 0) < 1:
                reason = "failed_retry_budget_invalid"
            elif int(task.get("retry_count") or 0) < int(task.get("max_retries") or 0):
                reason = "failed_retry_budget_not_exhausted"
            elif any(event_id in ambiguous_cognitive_events for event_id in referenced_events):
                reason = "ambiguous_cognitive_event_task_mapping"
            if reason:
                reasons[reason] += 1
                result["manual_reconciliation_required"] += 1
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": "failed",
                        "production_event_id": production_event_id or "",
                        "disposition": f"manual:{reason}",
                    }
                )
                continue
            outbox = _failed_terminal_outbox(task)
            existing_outbox = task.get("meta", {}).get("failed_terminal_receipt_outbox")
            if existing_outbox is None:
                result["failed_terminal_outboxes_missing"] += 1
            elif not _outbox_matches_candidate(existing_outbox, outbox):
                reasons["failed_terminal_outbox_conflict"] += 1
                result["manual_reconciliation_required"] += 1
                plan_entries.append(
                    {
                        "task_id": str(task["task_id"]),
                        "input_revision": str(task["input_revision"]),
                        "status": "failed",
                        "production_event_id": production_event_id,
                        "disposition": "manual:failed_terminal_outbox_conflict",
                    }
                )
                continue
            failed_terminal_reconciliations.append((task, str(outbox["reason"]), outbox))
            plan_entries.append(
                {
                    "task_id": str(task["task_id"]),
                    "session_id": str(task["session_id"]),
                    "input_revision": str(task["input_revision"]),
                    "status": "failed",
                    "production_event_id": production_event_id,
                    "disposition": "typed_failed_terminal_outbox_replay",
                    "payload_sha256": outbox["payload_sha256"],
                    "cognitive_event_ids": list(referenced_events),
                }
            )
        result["terminal_tasks"] = len(terminal_tasks)
        result["plan_entries"] = len(plan_entries)
        semantic_plan = {
            "schema_version": SCHEMA_VERSION,
            "database_dir": str(database_dir.resolve()),
            "legacy_naive_timezone": timezone_name,
            "entries": plan_entries,
        }
        result["reviewed_plan"] = semantic_plan
        result["semantic_plan_sha256"] = _canonical_sha256(semantic_plan)
        result["preimages"] = {
            "ledger": _sqlite_family_preimages(ledger_path),
            "queue": _sqlite_family_preimages(queue_path),
        }
        result["code_identity"] = _reconciler_code_identity()
        result["plan_sha256"] = _canonical_sha256(
            {
                "semantic_plan_sha256": result["semantic_plan_sha256"],
                "preimages": result["preimages"],
                "code_identity": result["code_identity"],
                "backup_scope": [
                    str(ledger_path.resolve()),
                    str(queue_path.resolve()),
                ],
            }
        )
        result["unproven_by_reason"] = dict(sorted(reasons.items()))
        if apply and str(expected_plan_sha256) != result["plan_sha256"]:
            result["error"] = "reviewed_plan_sha256_mismatch"
            return result
        if not apply:
            result["ok"] = True
            return result

        if backup_dir is None:
            raise RuntimeError("backup_directory_required")
        conservation_before = _conservation_snapshot(queue_path, ledger_path)
        result["backup"] = _backup_database_set(
            (
                ("ledger", ledger_path, "producer-consumer"),
                ("queue", queue_path, "distill-queue"),
            ),
            backup_dir,
        )
        receipt_path = _migration_receipt_path(
            backup_dir,
            str(expected_plan_sha256),
        )
        prepared_receipt = {
            "schema_version": "mnemos.distill_runtime_receipt_migration_receipt.v1",
            "status": "prepared",
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "reviewed_plan_sha256": str(expected_plan_sha256),
            "semantic_plan_sha256": result["semantic_plan_sha256"],
            "legacy_naive_timezone": timezone_name,
            "preimages": result["preimages"],
            "code_identity": result["code_identity"],
            "database_dir": str(database_dir.resolve()),
            "backup_dir": str(backup_dir.resolve()),
            "backup": result["backup"],
        }
        prepared_receipt_sha256 = _canonical_sha256(prepared_receipt)
        prepared_receipt["receipt_sha256"] = prepared_receipt_sha256
        _atomic_write_json(receipt_path, prepared_receipt)
        result["migration_receipt"] = {
            "path": str(receipt_path),
            "status": "prepared",
            "receipt_sha256": prepared_receipt_sha256,
        }
        result["terminal_outbox_anchor_schema"] = _ensure_terminal_outbox_anchor_schema(queue_path)
        for (
            task,
            replacement_task_id,
            production_event_id,
            cognitive_corrections,
        ) in source_span_reconciliations:
            if (
                production_event_id is not None
                and production_event_id not in source_span_runtime_corrected
            ):
                runtime = record_distillation_generation_superseded(
                    config,
                    legacy_task=task,
                    replacement_task_id=replacement_task_id,
                )
                if runtime.get("matched"):
                    result["source_span_runtime_corrections_recorded"] += 1
                elif production_event_id is not None:
                    result["source_span_corrections_deferred"] += 1
            for event_id, current in cognitive_corrections:
                current_id = str(current.get("consumption_id") or "")
                correction_id = record_cognitive_data_consumed(
                    event_id,
                    consumer_id="distill",
                    outcome="source_span_generation_superseded_requires_reprocessing",
                    status="revoked",
                    metadata={
                        "task_id": str(task["task_id"]),
                        "replacement_task_id": replacement_task_id,
                        "reopen_required": True,
                        "correction_reason": "legacy_generation_was_not_model_consumed",
                    },
                    supersedes_consumption_id=current_id,
                    correction_of_consumption_id=current_id,
                    config_or_path=config,
                )
                if correction_id is not None:
                    result["source_span_cognitive_corrections_recorded"] += 1
                else:
                    result["source_span_corrections_deferred"] += 1

        for (
            task,
            receipt,
            production_event_id,
            already_receipted,
            outbox,
        ) in terminal_reconciliations:
            prepared = _prepare_terminal_outbox(
                queue_path,
                task=task,
                outbox_key="terminal_receipt_outbox",
                candidate=outbox,
            )
            if prepared == "prepared":
                result["terminal_outboxes_prepared"] += 1
            handoff = record_distillation_handoff(
                config,
                task=task,
                allow_legacy_unbound_current=True,
            )
            if handoff.get("verified"):
                result["handoff_tasks_reconciled"] += 1
            result["cognitive_receipts_deferred"] += int(handoff.get("cognitive_deferred") or 0)
            evidence = record_distillation_terminal(
                config,
                task=task,
                receipt=receipt,
                allow_legacy_terminal_supersession=True,
            )
            if _has_cognitive_event_ids(task) and evidence.get("cognitive_terminal_verified"):
                result["terminal_cognitive_tasks_reconciled"] += 1
            result["cognitive_receipts_deferred"] += int(evidence.get("cognitive_deferred") or 0)
            if production_event_id:
                if not evidence.get("matched"):
                    reasons[str(evidence.get("reason") or "reconciliation_deferred")] += 1
                    continue
                result["reconciled_tasks"] += 1
                if not already_receipted:
                    result["receipts_recorded"] += 1
                verified = verify_distillation_terminal(
                    config,
                    task=task,
                    receipt=receipt,
                )
                if not verified.get("verified"):
                    reasons[str(verified.get("reason") or "terminal_verification_deferred")] += 1
                    continue
                committed = _commit_terminal_outbox(
                    queue_path,
                    task=task,
                    outbox_key="terminal_receipt_outbox",
                    candidate=outbox,
                    evidence=verified,
                )
                if committed == "committed":
                    result["terminal_outboxes_committed"] += 1

        for task, failure_reason, outbox in failed_terminal_reconciliations:
            prepared = _prepare_terminal_outbox(
                queue_path,
                task=task,
                outbox_key="failed_terminal_receipt_outbox",
                candidate=outbox,
            )
            if prepared == "prepared":
                result["failed_terminal_outboxes_prepared"] += 1
            handoff = record_distillation_handoff(
                config,
                task=task,
                allow_legacy_unbound_current=True,
            )
            if handoff.get("verified"):
                result["handoff_tasks_reconciled"] += 1
            result["cognitive_receipts_deferred"] += int(handoff.get("cognitive_deferred") or 0)
            evidence = record_distillation_failed_terminal(
                config,
                task=task,
                reason=failure_reason,
            )
            result["cognitive_receipts_deferred"] += int(evidence.get("cognitive_deferred") or 0)
            if not evidence.get("matched"):
                reasons[
                    str(evidence.get("reason") or "failed_terminal_reconciliation_deferred")
                ] += 1
                continue
            verified = verify_distillation_failed_terminal(
                config,
                task=task,
                expected_reason=failure_reason,
            )
            if not verified.get("verified"):
                reasons[str(verified.get("reason") or "failed_terminal_verification_deferred")] += 1
                continue
            committed = _commit_terminal_outbox(
                queue_path,
                task=task,
                outbox_key="failed_terminal_receipt_outbox",
                candidate=outbox,
                evidence=verified,
            )
            if committed == "committed":
                result["failed_terminal_outboxes_committed"] += 1
            result["reconciled_tasks"] += 1
        result["unproven_by_reason"] = dict(sorted(reasons.items()))
        with sqlite3.connect(ledger_path) as conn:
            ledger_integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        with sqlite3.connect(queue_path) as conn:
            queue_integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        result["integrity_check"] = {
            "ledger": ledger_integrity,
            "queue": queue_integrity,
        }
        conservation_after = _conservation_snapshot(queue_path, ledger_path)
        conserved_fields = (
            "queue_task_count",
            "queue_identity_status_sha256",
            "produced_event_count",
            "produced_event_sha256",
        )
        conservation_ok = all(
            conservation_before[field] == conservation_after[field] for field in conserved_fields
        )
        result["conservation"] = {
            "before": conservation_before,
            "after": conservation_after,
            "identity_and_status_conserved": conservation_ok,
        }
        result["ok"] = (
            ledger_integrity == "ok"
            and queue_integrity == "ok"
            and not result["unproven_by_reason"]
            and not result["cognitive_receipts_deferred"]
            and not result["source_span_corrections_deferred"]
            and conservation_ok
            and result.get("terminal_outbox_anchor_schema", {}).get("canonical") is True
        )
        if prepared_receipt is None or receipt_path is None:
            raise RuntimeError("prepared_migration_receipt_missing")
        if not result["ok"]:
            result["rollback"] = _rollback_terminal_reconciliation(
                backups=result["backup"],
                queue_path=queue_path,
                ledger_path=ledger_path,
                expected_conservation=conservation_before,
                attempted_conservation=result["conservation"],
            )
            if not result["rollback"]["verified"]:
                result["error"] = "terminal_reconciliation_rollback_failed"
            result["integrity_check"] = result["rollback"]["integrity_check"]
            _write_final_migration_receipt(
                receipt_path=receipt_path,
                status=("rolled_back" if result["rollback"]["verified"] else "rollback_failed"),
                prepared_receipt=prepared_receipt,
                prepared_receipt_sha256=prepared_receipt_sha256,
                result=result,
            )
            return result
        _write_final_migration_receipt(
            receipt_path=receipt_path,
            status="completed",
            prepared_receipt=prepared_receipt,
            prepared_receipt_sha256=prepared_receipt_sha256,
            result=result,
        )
        return result
    except (OSError, sqlite3.Error, ValueError, KeyError, RuntimeError) as exc:
        result["error"] = type(exc).__name__
        if (
            conservation_before is not None
            and prepared_receipt is not None
            and receipt_path is not None
            and isinstance(result.get("backup"), Mapping)
        ):
            try:
                result["rollback"] = _rollback_terminal_reconciliation(
                    backups=result["backup"],
                    queue_path=queue_path,
                    ledger_path=ledger_path,
                    expected_conservation=conservation_before,
                    attempted_conservation=result.get("conservation"),
                )
            except (OSError, sqlite3.Error, ValueError, KeyError, RuntimeError):
                result["rollback"] = {
                    "verified": False,
                    "error": "terminal_reconciliation_rollback_failed",
                }
            if result["rollback"]["verified"]:
                result["integrity_check"] = result["rollback"]["integrity_check"]
            _write_final_migration_receipt(
                receipt_path=receipt_path,
                status=("rolled_back" if result["rollback"]["verified"] else "rollback_failed"),
                prepared_receipt=prepared_receipt,
                prepared_receipt_sha256=prepared_receipt_sha256,
                result=result,
            )
        return result
