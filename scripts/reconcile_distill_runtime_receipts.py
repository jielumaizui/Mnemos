#!/usr/bin/env python3
"""Reconcile only provable Raw-to-distill terminal generations.

Dry-run emits a stable reviewed plan. Apply requires that exact plan hash,
SQLite backups of both queue and ledger, and inactive runtime writers. It may
backfill a pending queue-owned terminal outbox and then commit it only after
exact runtime and cognitive proof verifies. Raw data, task terminal status,
messages, and Wiki pages are never changed. Archived and unproven tasks remain
explicit manual-reconciliation items.

Source-span compatibility retirements are handled separately: the obsolete
runtime generation receives a typed skip, while any historical false cognitive
``distill`` terminal is append-only revoked so the replacement stays pending.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.cognitive_pipeline_receipts import (
    inspect_distillation_terminal_cognitive_state,
    inspect_distillation_terminal_runtime_state,
    record_distillation_failed_terminal,
    record_distillation_generation_superseded,
    record_distillation_handoff,
    record_distillation_terminal,
    verify_distillation_failed_terminal,
    verify_distillation_terminal,
)
from core.ops.runtime_flow_telemetry import record_cognitive_data_consumed
from core.migrations.model_call_ledger_reconcile.runtime import (
    runtime_writers_are_inactive as _shared_runtime_is_inactive,
)
from core.pipeline_receipts import (
    DistillationWriteReceipt,
    canonical_distillation_write_receipt_payload,
    distillation_failed_terminal_sha256,
    distillation_write_receipt_sha256,
)
from core.kia.amphora import (
    _reconcile_terminal_outbox_anchor_schema,
    _terminal_outbox_anchor_sha256,
)
from core.ops.durable_io import (
    DurableIOError,
    fsync_directory,
    fsync_regular_file,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    private_sqlite_sidecars,
    regular_file_sha256,
    validate_private_sqlite_copy,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite

SCHEMA_VERSION = "mnemos.distill_runtime_receipt_reconciliation.v4"
_TERMINAL_STATUSES = frozenset({"committed", "intentional_skip"})
_SOURCE_SPAN_REASON_PREFIX = "superseded_by_verified_source_span_migration:"


def _sha256(path: Path) -> str:
    return "sha256:" + regular_file_sha256(path)


def _connect_read_only(
    path: Path,
    *,
    immutable: bool = False,
) -> sqlite3.Connection:
    return connect_readonly_sqlite(path, immutable=immutable)


def _backup_database(
    source: Path,
    backup_dir: Path,
    *,
    label: str,
) -> dict[str, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"{label}-before-distill-receipts-{stamp}.db"
    created = False
    try:
        descriptor = os.open(
            target,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        os.close(descriptor)
        with owned_sqlite_connection_pair(
            lambda: _connect_read_only(source),
            lambda: sqlite3.connect(target),
        ) as (src, dst):
            src.backup(dst)
            integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError("backup integrity check failed")
        normalize_private_sqlite_copy(target)
        target.chmod(0o600)
        fsync_regular_file(target)
        fsync_directory(backup_dir)
    except DurableIOError:
        if created:
            for candidate in (*private_sqlite_sidecars(target), target):
                candidate.unlink(missing_ok=True)
        raise RuntimeError("backup normalization failed") from None
    except BaseException:
        if created:
            for candidate in (*private_sqlite_sidecars(target), target):
                candidate.unlink(missing_ok=True)
        raise
    return {"path": str(target), "sha256": _sha256(target), "integrity_check": "ok"}


def _remove_unbound_backup_set(
    backups: Mapping[str, Mapping[str, Any]],
    backup_dir: Path,
) -> None:
    resolved_dir = backup_dir.resolve(strict=True)
    for record in backups.values():
        target = Path(str(record.get("path") or "")).resolve(strict=False)
        if target.parent != resolved_dir:
            raise RuntimeError("partial_backup_scope_invalid")
        for candidate in (*private_sqlite_sidecars(target), target):
            candidate.unlink(missing_ok=True)
    fsync_directory(resolved_dir)


def _backup_database_set(
    sources: tuple[tuple[str, Path, str], ...],
    backup_dir: Path,
) -> dict[str, dict[str, str]]:
    backups: dict[str, dict[str, str]] = {}
    try:
        for key, source, label in sources:
            backups[key] = _backup_database(
                source,
                backup_dir,
                label=label,
            )
        return backups
    except BaseException:
        _remove_unbound_backup_set(backups, backup_dir)
        raise


def _restore_database_from_backup(
    backup: Mapping[str, Any],
    target: Path,
) -> None:
    """Restore one verified SQLite backup through a private atomic stage."""
    source_path = Path(str(backup.get("path") or ""))
    if (
        not source_path.is_file()
        or _sha256(source_path) != str(backup.get("sha256") or "")
    ):
        raise RuntimeError("terminal_reconciliation_rollback_backup_drift")
    try:
        validate_private_sqlite_copy(source_path)
    except DurableIOError:
        raise RuntimeError("terminal_reconciliation_rollback_backup_invalid") from None
    with _connect_read_only(source_path, immutable=True) as source:
        if str(source.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise RuntimeError("terminal_reconciliation_rollback_backup_invalid")
    stage = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
    stage_created = False
    try:
        descriptor = os.open(
            stage,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        stage_created = True
        os.close(descriptor)
        with owned_sqlite_connection_pair(
            lambda: _connect_read_only(source_path, immutable=True),
            lambda: sqlite3.connect(stage),
        ) as (source, destination):
            source.backup(destination)
        try:
            normalize_private_sqlite_copy(stage)
        except DurableIOError:
            raise RuntimeError(
                "terminal_reconciliation_rollback_stage_invalid"
            ) from None
        stage.chmod(0o600)
        with _connect_read_only(stage, immutable=True) as restored:
            if str(restored.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError(
                    "terminal_reconciliation_rollback_stage_invalid"
                )
        fsync_regular_file(stage)
        os.replace(stage, target)
        for sidecar in private_sqlite_sidecars(target):
            sidecar.unlink(missing_ok=True)
        target.chmod(0o600)
        fsync_regular_file(target)
        fsync_directory(target.parent)
    finally:
        if stage_created:
            for candidate in (*private_sqlite_sidecars(stage), stage):
                candidate.unlink(missing_ok=True)


def _rollback_terminal_reconciliation(
    *,
    backups: Mapping[str, Mapping[str, Any]],
    queue_path: Path,
    ledger_path: Path,
    expected_conservation: Mapping[str, Any],
    attempted_conservation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _restore_database_from_backup(backups["ledger"], ledger_path)
    _restore_database_from_backup(backups["queue"], queue_path)
    with sqlite3.connect(ledger_path) as conn:
        ledger_integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    with sqlite3.connect(queue_path) as conn:
        queue_integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    restored = _conservation_snapshot(queue_path, ledger_path)
    verified = (
        ledger_integrity == "ok"
        and queue_integrity == "ok"
        and dict(restored) == dict(expected_conservation)
    )
    return {
        "verified": verified,
        "integrity_check": {
            "ledger": ledger_integrity,
            "queue": queue_integrity,
        },
        "expected_conservation": dict(expected_conservation),
        "restored_conservation": restored,
        "attempted_conservation": dict(attempted_conservation or {}),
    }


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably publish one private migration-control record."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        temporary_created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        fsync_regular_file(path)
        fsync_directory(path.parent)
    finally:
        if temporary_created:
            temporary.unlink(missing_ok=True)


def _migration_receipt_path(backup_dir: Path, plan_sha256: str) -> Path:
    suffix = str(plan_sha256).removeprefix("sha256:")
    return backup_dir / f"distill-runtime-receipts-migration.{suffix}.json"


def _migration_outcome(result: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "reconciled_tasks",
        "receipts_recorded",
        "terminal_outboxes_prepared",
        "terminal_outboxes_committed",
        "failed_terminal_outboxes_prepared",
        "failed_terminal_outboxes_committed",
        "manual_reconciliation_required",
        "unproven_by_reason",
        "cognitive_receipts_deferred",
        "source_span_corrections_deferred",
    )
    return {
        "ok": bool(result.get("ok")),
        **{key: result.get(key) for key in keys},
    }


def _write_final_migration_receipt(
    *,
    receipt_path: Path,
    status: str,
    prepared_receipt: Mapping[str, Any],
    prepared_receipt_sha256: str,
    result: dict[str, Any],
) -> None:
    final_receipt = {
        "schema_version": "mnemos.distill_runtime_receipt_migration_receipt.v1",
        "status": status,
        f"{status}_at": datetime.now(timezone.utc).isoformat(),
        "prepared_receipt_sha256": prepared_receipt_sha256,
        "reviewed_plan_sha256": prepared_receipt["reviewed_plan_sha256"],
        "semantic_plan_sha256": prepared_receipt["semantic_plan_sha256"],
        "legacy_naive_timezone": prepared_receipt["legacy_naive_timezone"],
        "preimages": prepared_receipt["preimages"],
        "code_identity": prepared_receipt["code_identity"],
        "database_dir": prepared_receipt["database_dir"],
        "backup_dir": prepared_receipt["backup_dir"],
        "backup": prepared_receipt["backup"],
        "integrity_check": result.get("integrity_check", {}),
        "conservation": result.get("conservation", {}),
        "outcome": _migration_outcome(result),
    }
    if result.get("rollback") is not None:
        final_receipt["rollback"] = result["rollback"]
    if result.get("error"):
        final_receipt["error"] = str(result["error"])
    final_receipt_sha256 = _canonical_sha256(final_receipt)
    final_receipt["receipt_sha256"] = final_receipt_sha256
    _atomic_write_json(receipt_path, final_receipt)
    result["migration_receipt"] = {
        "path": str(receipt_path),
        "status": status,
        "receipt_sha256": final_receipt_sha256,
        "prepared_receipt_sha256": prepared_receipt_sha256,
    }


def _json_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _json_strings(value: Any) -> list[str]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if isinstance(item, str) and item]


def _strict_json_mapping(value: Any) -> dict[str, Any]:
    decoded = json.loads(str(value or "{}"))
    if not isinstance(decoded, dict):
        raise ValueError("queue_meta_must_be_json_object")
    return dict(decoded)


def _strict_json_strings(value: Any, *, field: str) -> list[str]:
    decoded = json.loads(str(value or "[]"))
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) for item in decoded
    ):
        raise ValueError(f"{field}_must_be_json_string_list")
    return list(decoded)


def _task_rows(queue_path: Path) -> list[dict[str, Any]]:
    with _connect_read_only(queue_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(distillation_tasks)")
        }
        required = {
            "task_id",
            "session_id",
            "input_revision",
            "status",
            "terminal_reason",
            "written_count",
            "written_paths",
            "meta",
            "completed_at",
        }
        if not required.issubset(columns):
            raise RuntimeError("distillation_tasks_schema_incomplete")

        rows = conn.execute(
            "SELECT * FROM distillation_tasks ORDER BY task_id"
        ).fetchall()
    tasks: list[dict[str, Any]] = []
    for row in rows:
        parse_error = ""
        try:
            written_paths = _strict_json_strings(
                row["written_paths"],
                field="written_paths",
            )
            meta = _strict_json_mapping(row["meta"])
            proposal_ids = _strict_json_strings(
                row["proposal_ids"] if "proposal_ids" in columns else "[]",
                field="proposal_ids",
            )
            required_receipts = _strict_json_strings(
                (
                    row["required_consumer_receipts"]
                    if "required_consumer_receipts" in columns
                    else "[]"
                ),
                field="required_consumer_receipts",
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            written_paths = []
            meta = {}
            proposal_ids = []
            required_receipts = []
            parse_error = str(exc) or type(exc).__name__
        tasks.append(
            {
                "task_id": str(row["task_id"] or ""),
                "session_id": str(row["session_id"] or ""),
                "input_revision": str(row["input_revision"] or ""),
                "status": str(row["status"] or ""),
                "terminal_reason": str(row["terminal_reason"] or ""),
                "written_count": int(row["written_count"] or 0),
                "written_paths": written_paths,
                "meta": meta,
                "completed_at": str(row["completed_at"] or ""),
                "proposal_ids": proposal_ids,
                "required_consumer_receipts": required_receipts,
                "retry_count": int(
                    row["retry_count"] or 0
                    if "retry_count" in columns
                    else 0
                ),
                "max_retries": int(
                    row["max_retries"] or 0
                    if "max_retries" in columns
                    else 0
                ),
                "progress_detail": str(
                    row["progress_detail"] or ""
                    if "progress_detail" in columns
                    else ""
                ),
                "parse_error": parse_error,
            }
        )
    return tasks


def _has_cognitive_event_ids(task: Mapping[str, Any]) -> bool:
    meta = task.get("meta")
    if not isinstance(meta, Mapping):
        return False
    values = meta.get("cognitive_sync_event_ids")
    return isinstance(values, list) and any(str(value) for value in values)


def _cognitive_event_ids(meta: Any) -> tuple[str, ...]:
    if not isinstance(meta, Mapping):
        return ()
    values = meta.get("cognitive_sync_event_ids")
    if not isinstance(values, list):
        return ()
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _produced_generations(ledger_path: Path) -> tuple[dict[tuple[str, str], str], set[str]]:
    with _connect_read_only(ledger_path) as conn:
        rows = conn.execute("""
            SELECT event_id, metadata
            FROM runtime_flow_events
            WHERE flow_id='raw_quality_to_distill_gate' AND direction='produced'
            """).fetchall()
        receipt_rows = conn.execute("""
            SELECT production_event_id
            FROM runtime_flow_receipts
            WHERE flow_id='raw_quality_to_distill_gate'
              AND consumer_id='core/hephaestus/distillation_engine.py'
              AND status IN ('consumed','dead_letter','skipped')
            """).fetchall()
    event_by_task_generation: dict[tuple[str, str], str] = {}
    for event_id, metadata in rows:
        payload = _json_mapping(metadata)
        task_id = str(payload.get("task_id") or "")
        revision = str(payload.get("input_revision") or "")
        if task_id and revision:
            event_by_task_generation[(task_id, revision)] = str(event_id)
    return event_by_task_generation, {str(row[0] or "") for row in receipt_rows if row[0]}


def _source_span_replacements(queue_path: Path) -> dict[str, str]:
    with _connect_read_only(queue_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='amphora_source_span_migrations'"
        ).fetchone()
        if exists is None:
            return {}
        rows = conn.execute(
            "SELECT legacy_task_id, canonical_task_id "
            "FROM amphora_source_span_migrations ORDER BY legacy_task_id"
        ).fetchall()
    return {
        str(legacy_task_id): str(canonical_task_id)
        for legacy_task_id, canonical_task_id in rows
        if legacy_task_id and canonical_task_id
    }


def _source_span_runtime_corrections(ledger_path: Path) -> set[str]:
    with _connect_read_only(ledger_path) as conn:
        rows = conn.execute(
            """
            SELECT receipt_id, production_event_id, consumer_id, status,
                   item_id, generation_id, metadata
            FROM runtime_flow_receipts
            WHERE flow_id='raw_quality_to_distill_gate'
            """
        ).fetchall()
    receipts: list[dict[str, Any]] = [
        {
            "receipt_id": str(receipt_id),
            "production_event_id": str(production_event_id),
            "consumer_id": str(consumer_id),
            "status": str(status),
            "item_id": str(item_id),
            "generation_id": str(generation_id),
            "metadata": _json_mapping(metadata),
        }
        for (
            receipt_id,
            production_event_id,
            consumer_id,
            status,
            item_id,
            generation_id,
            metadata,
        ) in rows
    ]
    corrected: set[str] = set()
    expected_transition = "verified_source_span_generation_superseded"
    expected_reason = "source_span_generation_replaced_with_exact_raw"
    for successor in receipts:
        metadata = successor["metadata"]
        if not (
            successor["consumer_id"] == "core/hephaestus/distillation_engine.py"
            and successor["status"] == "skipped"
            and metadata.get("transition") == expected_transition
            and metadata.get("supersession_reason") == expected_reason
        ):
            continue
        raw_superseded = metadata.get("supersedes_receipt_ids", [])
        if not isinstance(raw_superseded, list):
            continue
        superseded = {str(value) for value in raw_superseded if str(value)}
        invalid_predecessors = {
            receipt["receipt_id"]
            for receipt in receipts
            if receipt["receipt_id"] != successor["receipt_id"]
            and receipt["production_event_id"]
            == successor["production_event_id"]
            and receipt["item_id"] == successor["item_id"]
            and receipt["generation_id"] == successor["generation_id"]
            and receipt["metadata"].get("transition") == expected_transition
            and (
                receipt["consumer_id"]
                != "core/hephaestus/distillation_engine.py"
                or receipt["metadata"].get("supersession_reason")
                != expected_reason
            )
        }
        if invalid_predecessors.issubset(superseded):
            corrected.add(successor["production_event_id"])
    return corrected


def _distill_cognitive_heads(ledger_path: Path) -> dict[str, dict[str, Any]]:
    with _connect_read_only(ledger_path) as conn:
        rows = conn.execute(
            """
            SELECT h.event_id, c.consumption_id, c.status, c.outcome, c.metadata,
                   COALESCE(c.supersedes_consumption_id, ''),
                   COALESCE(c.correction_of_consumption_id, '')
            FROM cognitive_data_consumer_heads AS h
            JOIN cognitive_data_consumptions AS c
              ON c.consumption_id=h.consumption_id
            WHERE h.consumer_id='distill'
            """
        ).fetchall()
    return {
        str(event_id): {
            "consumption_id": str(consumption_id),
            "status": str(status),
            "outcome": str(outcome),
            "metadata": _json_mapping(metadata),
            "supersedes_consumption_id": str(supersedes_consumption_id),
            "correction_of_consumption_id": str(correction_of_consumption_id),
        }
        for (
            event_id,
            consumption_id,
            status,
            outcome,
            metadata,
            supersedes_consumption_id,
            correction_of_consumption_id,
        ) in rows
    }


def _parse_timestamp(
    value: Any,
    *,
    legacy_naive_timezone: ZoneInfo | None = None,
) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if legacy_naive_timezone is None:
            return None
        parsed = parsed.replace(tzinfo=legacy_naive_timezone)
    return parsed.astimezone(timezone.utc)


def _lifecycle_proves_later_removal(
    wiki_projection_path: Path,
    *,
    page_path: str,
    task_completed_at: str,
    legacy_naive_timezone: ZoneInfo | None,
) -> bool:
    """Prove that a committed page disappeared through its canonical lifecycle."""
    completed_at = _parse_timestamp(
        task_completed_at,
        legacy_naive_timezone=legacy_naive_timezone,
    )
    if completed_at is None or not wiki_projection_path.is_file():
        return False
    try:
        with _connect_read_only(wiki_projection_path) as conn:
            rows = conn.execute(
                """
                SELECT page_id, mutation_type, page_path, previous_path,
                       tombstone, created_at, sequence_no
                FROM wiki_mutations
                WHERE page_path=? OR previous_path=?
                ORDER BY sequence_no
                """,
                (page_path, page_path),
            ).fetchall()
    except sqlite3.Error:
        return False

    active_pages: dict[str, int] = {}
    for row in rows:
        page_id = str(row[0] or "")
        mutation_type = str(row[1] or "")
        current_path = str(row[2] or "")
        previous_path = str(row[3] or "")
        tombstone = bool(row[4])
        created_at = _parse_timestamp(row[5])
        sequence_no = int(row[6] or 0)
        if not page_id or created_at is None or sequence_no <= 0:
            continue
        if (
            mutation_type in {"create", "update"}
            and current_path == page_path
            and not tombstone
            and created_at <= completed_at
        ):
            active_pages[page_id] = max(active_pages.get(page_id, 0), sequence_no)
            continue
        active_sequence = active_pages.get(page_id)
        if active_sequence is None or sequence_no <= active_sequence or created_at < completed_at:
            continue
        deleted = mutation_type == "delete" and tombstone and current_path == page_path
        moved = (
            mutation_type == "move"
            and not tombstone
            and previous_path == page_path
            and current_path != page_path
        )
        if deleted or moved:
            return True
    return False


def _validate_terminal_task(
    task: Mapping[str, Any],
    *,
    wiki_projection_path: Path,
    legacy_naive_timezone: ZoneInfo | None,
) -> tuple[DistillationWriteReceipt | None, str]:
    status = str(task.get("status") or "")
    reason = str(task.get("terminal_reason") or "").strip()
    if status not in _TERMINAL_STATUSES:
        return None, "not_terminal"
    if not reason:
        return None, "terminal_reason_missing"
    if status == "intentional_skip":
        return DistillationWriteReceipt(status=status, terminal_reason=reason), "valid"

    paths = tuple(str(path) for path in task.get("written_paths", []) if path)
    written_count = int(task.get("written_count") or 0)
    if written_count <= 0 or len(paths) != written_count:
        return None, "committed_artifact_count_invalid"
    missing_paths = tuple(path for path in paths if not Path(path).is_file())
    if missing_paths and not all(
        _lifecycle_proves_later_removal(
            wiki_projection_path,
            page_path=path,
            task_completed_at=str(task.get("completed_at") or ""),
            legacy_naive_timezone=legacy_naive_timezone,
        )
        for path in missing_paths
    ):
        return None, "committed_artifact_missing"
    return (
        DistillationWriteReceipt(
            status=status,
            terminal_reason=reason,
            written_pages=paths,
            proposal_ids=tuple(task.get("proposal_ids", [])),
            expected_count=written_count,
            written_count=written_count,
            required_consumer_receipts=tuple(
                task.get("required_consumer_receipts", [])
            ),
        ),
        "valid_lifecycle_removal" if missing_paths else "valid",
    )


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sqlite_family_preimages(path: Path) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    for suffix in ("", "-wal", "-shm"):
        member = Path(str(path) + suffix)
        members.append(
            {
                "name": member.name,
                "exists": member.is_file(),
                "size": member.stat().st_size if member.is_file() else 0,
                "sha256": _sha256(member) if member.is_file() else "",
            }
        )
    return {"database": str(path.resolve()), "members": members}


def _reconciler_code_identity() -> dict[str, Any]:
    paths = (
        Path(__file__).resolve(),
        ROOT / "scripts/distill_terminal_reconciliation_runtime.py",
        ROOT / "core/migrations/model_call_ledger_reconcile/runtime.py",
        ROOT / "core/ops/cognitive_pipeline_receipts.py",
        ROOT / "core/ops/producer_consumer_ledger.py",
        ROOT / "core/ops/runtime_flow_telemetry.py",
        ROOT / "core/ops/durable_io.py",
        ROOT / "core/kia/amphora.py",
        ROOT / "core/kia/amphora_types.py",
        ROOT / "core/kia/amphora_terminal_contract.py",
        ROOT / "core/kia/amphora_terminal_operations.py",
        ROOT / "core/kia/amphora_provenance_reconciliation.py",
        ROOT / "core/kia/amphora_provenance_support.py",
        ROOT / "core/pipeline_receipts.py",
    )
    return {
        "python": sys.version,
        "files": [
            {"path": str(path), "sha256": _sha256(path)}
            for path in paths
        ],
    }


def _conservation_snapshot(
    queue_path: Path,
    ledger_path: Path,
) -> dict[str, Any]:
    with _connect_read_only(queue_path) as conn:
        queue_rows = conn.execute(
            "SELECT task_id, session_id, input_revision, status "
            "FROM distillation_tasks ORDER BY task_id"
        ).fetchall()
    with _connect_read_only(ledger_path) as conn:
        produced = conn.execute(
            "SELECT event_id, item_id, generation_id FROM runtime_flow_events "
            "WHERE flow_id='raw_quality_to_distill_gate' "
            "AND direction='produced' ORDER BY event_id"
        ).fetchall()
        receipt_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM runtime_flow_receipts"
            ).fetchone()[0]
        )
        cognitive_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM cognitive_data_consumptions"
            ).fetchone()[0]
        )
    return {
        "queue_task_count": len(queue_rows),
        "queue_identity_status_sha256": _canonical_sha256(queue_rows),
        "produced_event_count": len(produced),
        "produced_event_sha256": _canonical_sha256(produced),
        "runtime_receipt_count": receipt_count,
        "cognitive_consumption_count": cognitive_count,
    }


def _success_terminal_outbox(
    task: Mapping[str, Any],
    receipt: DistillationWriteReceipt,
) -> dict[str, Any]:
    event_ids = list(_cognitive_event_ids(task.get("meta")))
    created_at = str(task.get("completed_at") or "")
    payload = canonical_distillation_write_receipt_payload(receipt)
    return {
        "schema_version": "mnemos.amphora_terminal_receipt_outbox.v1",
        "task_id": str(task["task_id"]),
        "session_id": str(task["session_id"]),
        "input_revision": str(task["input_revision"]),
        "status": "pending",
        "created_at": created_at,
        "receipt": payload,
        "receipt_sha256": distillation_write_receipt_sha256(receipt),
        "cognitive_event_ids": event_ids,
        "cognitive_event_count": len(event_ids),
        "cognitive_event_ids_sha256": hashlib.sha256(
            json.dumps(
                event_ids,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _failed_terminal_outbox(task: Mapping[str, Any]) -> dict[str, Any]:
    event_ids = list(_cognitive_event_ids(task.get("meta")))
    reason = f"retry_exhausted:{str(task.get('terminal_reason') or '')}"
    retry_count = int(task.get("retry_count") or 0)
    max_retries = int(task.get("max_retries") or 0)
    outbox = {
        "schema_version": "mnemos.amphora_failed_terminal_receipt_outbox.v2",
        "task_id": str(task["task_id"]),
        "session_id": str(task["session_id"]),
        "input_revision": str(task["input_revision"]),
        "status": "pending",
        "reason": reason,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "created_at": str(task.get("completed_at") or ""),
        "cognitive_event_ids": event_ids,
        "cognitive_event_count": len(event_ids),
        "cognitive_event_ids_sha256": hashlib.sha256(
            json.dumps(
                event_ids,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    outbox["payload_sha256"] = distillation_failed_terminal_sha256(
        task_id=str(task["task_id"]),
        session_id=str(task["session_id"]),
        input_revision=str(task["input_revision"]),
        reason=reason,
        retry_count=retry_count,
        max_retries=max_retries,
        cognitive_event_ids=event_ids,
    )
    return outbox


def _outbox_matches_candidate(
    existing: Any,
    candidate: Mapping[str, Any],
) -> bool:
    if not isinstance(existing, Mapping):
        return False
    ignored = {
        "status",
        "committed_at",
        "runtime_receipt_id",
        "production_event_id",
        "generation_id",
    }
    return (
        existing.get("status") in {"pending", "committed"}
        and {
            key: value for key, value in existing.items() if key not in ignored
        }
        == {
            key: value for key, value in candidate.items() if key not in ignored
        }
    )


def _ensure_terminal_outbox_anchor_schema(
    queue_path: Path,
) -> dict[str, bool]:
    with sqlite3.connect(queue_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = _reconcile_terminal_outbox_anchor_schema(conn)
        conn.commit()
    return result


def _prepare_terminal_outbox(
    queue_path: Path,
    *,
    task: Mapping[str, Any],
    outbox_key: str,
    candidate: Mapping[str, Any],
) -> str:
    """CAS a proven historical terminal into a pending queue-owned outbox."""
    with sqlite3.connect(queue_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        columns = {
            str(info[1])
            for info in conn.execute("PRAGMA table_info(distillation_tasks)")
        }
        if "terminal_outbox_anchor_sha256" not in columns:
            raise RuntimeError(
                "canonical_terminal_outbox_anchor_upgrade_required"
            )
        row = conn.execute(
            "SELECT task_id, session_id, input_revision, status, meta, "
            "terminal_outbox_anchor_sha256 "
            "FROM distillation_tasks WHERE task_id=?",
            (str(task["task_id"]),),
        ).fetchone()
        if row is None:
            raise RuntimeError("terminal_reconciliation_task_missing")
        if (
            str(row["session_id"]) != str(task["session_id"])
            or str(row["input_revision"]) != str(task["input_revision"])
            or str(row["status"]) != str(task["status"])
        ):
            raise RuntimeError("terminal_reconciliation_task_identity_drift")
        original_meta = str(row["meta"] or "{}")
        try:
            meta = _strict_json_mapping(original_meta)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise RuntimeError(
                "terminal_reconciliation_task_meta_invalid"
            ) from None
        existing = meta.get(outbox_key)
        expected_anchor = _terminal_outbox_anchor_sha256(candidate)
        if existing is not None:
            if not _outbox_matches_candidate(existing, candidate):
                raise RuntimeError("terminal_reconciliation_outbox_conflict")
            if str(row["terminal_outbox_anchor_sha256"] or "") == expected_anchor:
                conn.commit()
                return "already_present"
        else:
            meta[outbox_key] = dict(candidate)
        encoded_meta = json.dumps(
            meta,
            ensure_ascii=False,
            sort_keys=True,
        )
        if "updated_at" in columns:
            updated = conn.execute(
                """
                UPDATE distillation_tasks
                SET meta=?, terminal_outbox_anchor_sha256=?, updated_at=?
                WHERE task_id=? AND meta=?
                """,
                (
                    encoded_meta,
                    expected_anchor,
                    str(candidate["created_at"]),
                    str(task["task_id"]),
                    original_meta,
                ),
            )
        else:
            updated = conn.execute(
                """
                UPDATE distillation_tasks
                SET meta=?, terminal_outbox_anchor_sha256=?
                WHERE task_id=? AND meta=?
                """,
                (
                    encoded_meta,
                    expected_anchor,
                    str(task["task_id"]),
                    original_meta,
                ),
            )
        if updated.rowcount != 1:
            raise RuntimeError("terminal_reconciliation_outbox_cas_failed")
        conn.commit()
        return "prepared"


def _commit_terminal_outbox(
    queue_path: Path,
    *,
    task: Mapping[str, Any],
    outbox_key: str,
    candidate: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> str:
    proof = {
        "runtime_receipt_id": str(evidence.get("runtime_receipt_id") or ""),
        "production_event_id": str(evidence.get("production_event_id") or ""),
        "generation_id": str(evidence.get("generation_id") or ""),
    }
    if not all(proof.values()):
        raise RuntimeError("terminal_reconciliation_proof_incomplete")
    with sqlite3.connect(queue_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT session_id, input_revision, status, meta, "
            "terminal_outbox_anchor_sha256 "
            "FROM distillation_tasks WHERE task_id=?",
            (str(task["task_id"]),),
        ).fetchone()
        if row is None:
            raise RuntimeError("terminal_reconciliation_task_missing")
        if (
            str(row[0]) != str(task["session_id"])
            or str(row[1]) != str(task["input_revision"])
            or str(row[2]) != str(task["status"])
        ):
            raise RuntimeError("terminal_reconciliation_task_identity_drift")
        original_meta = str(row[3] or "{}")
        try:
            meta = _strict_json_mapping(original_meta)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise RuntimeError(
                "terminal_reconciliation_task_meta_invalid"
            ) from None
        existing = meta.get(outbox_key)
        if (
            not isinstance(existing, Mapping)
            or not _outbox_matches_candidate(existing, candidate)
        ):
            raise RuntimeError("terminal_reconciliation_outbox_conflict")
        expected_anchor = _terminal_outbox_anchor_sha256(candidate)
        if str(row[4] or "") != expected_anchor:
            raise RuntimeError("terminal_reconciliation_anchor_mismatch")
        if existing.get("status") == "committed":
            if all(existing.get(key) == value for key, value in proof.items()):
                conn.commit()
                return "already_committed"
            raise RuntimeError("terminal_reconciliation_committed_proof_conflict")
        committed = dict(existing)
        committed.update(proof)
        committed["status"] = "committed"
        committed["committed_at"] = datetime.now(timezone.utc).isoformat()
        meta[outbox_key] = committed
        columns = {
            str(info[1])
            for info in conn.execute("PRAGMA table_info(distillation_tasks)")
        }
        encoded_meta = json.dumps(
            meta,
            ensure_ascii=False,
            sort_keys=True,
        )
        if "updated_at" in columns:
            updated = conn.execute(
                """
                UPDATE distillation_tasks
                SET meta=?, updated_at=?
                WHERE task_id=? AND meta=?
                """,
                (
                    encoded_meta,
                    committed["committed_at"],
                    str(task["task_id"]),
                    original_meta,
                ),
            )
        else:
            updated = conn.execute(
                """
                UPDATE distillation_tasks
                SET meta=?
                WHERE task_id=? AND meta=?
                """,
                (
                    encoded_meta,
                    str(task["task_id"]),
                    original_meta,
                ),
            )
        if updated.rowcount != 1:
            raise RuntimeError("terminal_reconciliation_outbox_cas_failed")
        conn.commit()
        return "committed"


def _runtime_writers_are_inactive(database_dir: Path) -> bool:
    return _shared_runtime_is_inactive(database_dir)


from scripts import distill_terminal_reconciliation_runtime as _terminal_runtime


def reconcile_terminal_runtime_receipts(*args, **kwargs):
    """Delegate while preserving the CLI owner's injectable runtime seams."""
    dependencies = _terminal_runtime.RuntimeDependencies(
        schema_version=SCHEMA_VERSION,
        source_span_reason_prefix=_SOURCE_SPAN_REASON_PREFIX,
        terminal_statuses=_TERMINAL_STATUSES,
        atomic_write_json=_atomic_write_json,
        backup_database_set=_backup_database_set,
        canonical_sha256=_canonical_sha256,
        cognitive_event_ids=_cognitive_event_ids,
        commit_terminal_outbox=_commit_terminal_outbox,
        connect_read_only=_connect_read_only,
        conservation_snapshot=_conservation_snapshot,
        distill_cognitive_heads=_distill_cognitive_heads,
        ensure_terminal_outbox_anchor_schema=(
            _ensure_terminal_outbox_anchor_schema
        ),
        failed_terminal_outbox=_failed_terminal_outbox,
        has_cognitive_event_ids=_has_cognitive_event_ids,
        migration_receipt_path=_migration_receipt_path,
        outbox_matches_candidate=_outbox_matches_candidate,
        prepare_terminal_outbox=_prepare_terminal_outbox,
        produced_generations=_produced_generations,
        reconciler_code_identity=_reconciler_code_identity,
        rollback_terminal_reconciliation=(
            _rollback_terminal_reconciliation
        ),
        runtime_writers_are_inactive=_runtime_writers_are_inactive,
        source_span_replacements=_source_span_replacements,
        source_span_runtime_corrections=(
            _source_span_runtime_corrections
        ),
        sqlite_family_preimages=_sqlite_family_preimages,
        success_terminal_outbox=_success_terminal_outbox,
        task_rows=_task_rows,
        validate_terminal_task=_validate_terminal_task,
        write_final_migration_receipt=_write_final_migration_receipt,
        inspect_terminal_cognitive_state=(
            inspect_distillation_terminal_cognitive_state
        ),
        inspect_terminal_runtime_state=(
            inspect_distillation_terminal_runtime_state
        ),
        record_cognitive_data_consumed=record_cognitive_data_consumed,
        record_failed_terminal=record_distillation_failed_terminal,
        record_generation_superseded=(
            record_distillation_generation_superseded
        ),
        record_handoff=record_distillation_handoff,
        record_terminal=record_distillation_terminal,
        verify_failed_terminal=verify_distillation_failed_terminal,
        verify_terminal=verify_distillation_terminal,
    )
    return _terminal_runtime.reconcile_terminal_runtime_receipts(
        *args,
        dependencies=dependencies,
        **kwargs,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--plan-sha256")
    parser.add_argument(
        "--legacy-naive-timezone",
        help=(
            "IANA timezone for legacy task timestamps without an offset; "
            "omitting it leaves those lifecycle claims unproven"
        ),
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from core.config import get_config

    result = reconcile_terminal_runtime_receipts(
        get_config(),
        apply=bool(args.apply),
        backup_dir=args.backup_dir,
        expected_plan_sha256=args.plan_sha256,
        legacy_naive_timezone=args.legacy_naive_timezone,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(rendered)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
