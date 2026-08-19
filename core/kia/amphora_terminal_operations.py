"""Terminal state transitions and outbox operations for Amphora."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import sqlite3
import threading
from typing import Callable

from core.kia.amphora_terminal_contract import (
    _cognitive_event_ids_sha256,
    _distillation_write_receipt_from_payload,
    _failed_terminal_outbox_matches_row,
    _identifier_filter,
    _normalized_cognitive_event_ids,
    _require_canonical_task_database_config,
    _retry_time,
    _task_with_frozen_terminal_denominator,
    _terminal_outbox_anchor_matches_row,
    _terminal_outbox_anchor_sha256,
    _terminal_receipt_matches_row,
    _terminal_receipt_payload,
    _terminal_receipt_payload_sha256,
    _validated_failed_terminal_outbox,
    _validated_terminal_receipt_outbox,
)
from core.kia.amphora_types import (
    DistillProgress,
    DistillationFailureTransition,
    TIMEOUT_MINUTES,
)
from core.pipeline_receipts import (
    DistillationWriteReceipt,
    distillation_failed_terminal_sha256,
)


@dataclass(frozen=True)
class _TerminalOperationsRuntime:
    db_lock: threading.Lock
    connect: Callable[[], sqlite3.Connection]
    init_db: Callable[[], None]
    now: Callable[[], str]
    row_to_dict: Callable[[sqlite3.Row], dict]


_RUNTIME: _TerminalOperationsRuntime | None = None


def bind_terminal_operations_runtime(
    *,
    db_lock: threading.Lock,
    connect: Callable[[], sqlite3.Connection],
    init_db: Callable[[], None],
    now: Callable[[], str],
    row_to_dict: Callable[[sqlite3.Row], dict],
) -> None:
    """Bind queue-owned state without importing the queue module cyclically."""
    global _RUNTIME
    _RUNTIME = _TerminalOperationsRuntime(
        db_lock=db_lock,
        connect=connect,
        init_db=init_db,
        now=now,
        row_to_dict=row_to_dict,
    )


def _runtime() -> _TerminalOperationsRuntime:
    if _RUNTIME is None:
        raise RuntimeError("amphora_terminal_operations_runtime_unbound")
    return _RUNTIME


def mark_terminal(
    identifier: str,
    receipt: DistillationWriteReceipt,
    *,
    expected_started_at: str | None = None,
) -> bool:
    """Persist a typed write receipt; only committed/intentional_skip are terminal."""
    runtime = _runtime()
    runtime.init_db()
    with runtime.db_lock:
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            column, value = _identifier_filter(conn, identifier)
            now = runtime.now()
            terminal = receipt.terminal
            row = conn.execute(
                f"""
                SELECT task_id, session_id, input_revision, status, started_at,
                       retry_count, max_retries, meta
                FROM distillation_tasks
                WHERE {column}=?
                """,  # nosec B608
                (value,),
            ).fetchone()
            if not row:
                conn.commit()
                return False
            retry_count = int(row["retry_count"] or 0)
            next_retry_at = None
            status = receipt.status
            completed_at = now if terminal else None
            if status in {"retryable_failed", "partial"}:
                retry_count += 1
                if retry_count >= int(row["max_retries"] or 3):
                    status = "failed"
                    completed_at = now
                else:
                    next_retry_at = _retry_time(retry_count)
            try:
                meta = json.loads(row["meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                conn.rollback()
                raise RuntimeError("amphora_task_meta_invalid") from None
            if not isinstance(meta, dict):
                conn.rollback()
                raise RuntimeError("amphora_task_meta_invalid")
            terminal_outbox_anchor_sha256 = ""
            if status == "failed":
                existing_outbox = _validated_failed_terminal_outbox(
                    meta.get("failed_terminal_receipt_outbox"),
                    task_id=str(row["task_id"]),
                )
                if existing_outbox is None:
                    cognitive_event_ids = _normalized_cognitive_event_ids(meta)
                    failed_outbox = {
                        "schema_version": (
                            "mnemos.amphora_failed_terminal_receipt_outbox.v2"
                        ),
                        "task_id": str(row["task_id"]),
                        "session_id": str(row["session_id"]),
                        "input_revision": str(row["input_revision"]),
                        "status": "pending",
                        "reason": (
                            "retry_exhausted:"
                            f"{receipt.terminal_reason}"
                        ),
                        "retry_count": retry_count,
                        "max_retries": int(row["max_retries"] or 3),
                        "created_at": now,
                        "cognitive_event_ids": cognitive_event_ids,
                        "cognitive_event_count": len(cognitive_event_ids),
                        "cognitive_event_ids_sha256": (
                            _cognitive_event_ids_sha256(
                                cognitive_event_ids
                            )
                        ),
                    }
                    failed_outbox["payload_sha256"] = (
                        distillation_failed_terminal_sha256(
                            task_id=str(row["task_id"]),
                            session_id=str(row["session_id"]),
                            input_revision=str(row["input_revision"]),
                            reason=str(failed_outbox["reason"]),
                            retry_count=retry_count,
                            max_retries=int(row["max_retries"] or 3),
                            cognitive_event_ids=cognitive_event_ids,
                        )
                    )
                    meta["failed_terminal_receipt_outbox"] = failed_outbox
                terminal_outbox_anchor_sha256 = (
                    _terminal_outbox_anchor_sha256(
                        meta["failed_terminal_receipt_outbox"]
                    )
                )
            elif status in {"committed", "intentional_skip"}:
                existing_outbox = _validated_terminal_receipt_outbox(
                    meta.get("terminal_receipt_outbox"),
                    task_id=str(row["task_id"]),
                )
                if existing_outbox is None:
                    cognitive_event_ids = _normalized_cognitive_event_ids(meta)
                    receipt_payload = _terminal_receipt_payload(receipt)
                    meta["terminal_receipt_outbox"] = {
                        "schema_version": (
                            "mnemos.amphora_terminal_receipt_outbox.v1"
                        ),
                        "task_id": str(row["task_id"]),
                        "session_id": str(row["session_id"]),
                        "input_revision": str(row["input_revision"]),
                        "status": "pending",
                        "created_at": now,
                        "receipt": receipt_payload,
                        "receipt_sha256": (
                            _terminal_receipt_payload_sha256(receipt_payload)
                        ),
                        "cognitive_event_ids": cognitive_event_ids,
                        "cognitive_event_count": len(cognitive_event_ids),
                        "cognitive_event_ids_sha256": (
                            _cognitive_event_ids_sha256(
                                cognitive_event_ids
                            )
                        ),
                    }
                terminal_outbox_anchor_sha256 = (
                    _terminal_outbox_anchor_sha256(
                        meta["terminal_receipt_outbox"]
                    )
                )
            claim_clause = ""
            claim_params: tuple[object, ...] = ()
            if expected_started_at is not None:
                claim_clause = " AND started_at=?"
                claim_params = (str(expected_started_at),)
            cur = conn.execute(
                f"""
                UPDATE distillation_tasks
                SET status=?, completed_at=?, updated_at=?, output_path=?,
                    terminal_reason=?, written_count=?, written_paths=?, proposal_ids=?,
                    required_consumer_receipts=?, progress_step=?, progress_detail=?,
                    progress=?, error=?, retry_count=?, next_retry_at=?, meta=?,
                    terminal_outbox_anchor_sha256=?
                WHERE {column}=?
                  AND status IN ('processing', 'proposal_pending')
                  {claim_clause}
                """,  # nosec B608
                (
                    status,
                    completed_at,
                    now,
                    receipt.written_pages[0] if receipt.written_pages else None,
                    receipt.terminal_reason,
                    receipt.written_count,
                    json.dumps(receipt.written_pages, ensure_ascii=False),
                    json.dumps(receipt.proposal_ids, ensure_ascii=False),
                    json.dumps(receipt.required_consumer_receipts, ensure_ascii=False),
                    DistillProgress.DONE.value if terminal else status,
                    receipt.terminal_reason,
                    1.0 if terminal else min(0.99, receipt.written_count / max(1, receipt.expected_count)),
                    "" if terminal else receipt.terminal_reason,
                    retry_count,
                    next_retry_at,
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    terminal_outbox_anchor_sha256,
                    value,
                    *claim_params,
                ),
            )
            conn.commit()
            return cur.rowcount > 0


def mark_done(identifier: str, output_path: str | None = None) -> bool:
    """Compatibility wrapper for a real output path; empty success is forbidden."""
    if not output_path:
        raise ValueError("mark_done requires a durable output_path; use mark_intentional_skip")
    receipt = DistillationWriteReceipt(
        status="committed",
        terminal_reason="legacy_output_path_committed",
        written_pages=(output_path,),
        expected_count=1,
        written_count=1,
    )
    return mark_terminal(identifier, receipt)


def mark_intentional_skip(identifier: str, reason: str) -> bool:
    """Close a task without an artifact only when a non-empty reason is explicit."""
    if not str(reason or "").strip():
        raise ValueError("intentional skip requires a terminal reason")
    return mark_terminal(
        identifier,
        DistillationWriteReceipt(
            status="intentional_skip",
            terminal_reason=str(reason).strip(),
        ),
    )


def reset_timeouts(
    timeout_minutes: int = TIMEOUT_MINUTES,
    *,
    excluded_claims: tuple[tuple[str, str], ...] = (),
) -> int:
    """
    将超时卡住的 processing 任务重置为 pending。

    这是消费端健康检查的轻量降级：Worker 崩溃或无响应时，任务不会永久停在
    processing。重置后仍遵守 retry_count/max_retries 和优先级排序。
    """
    runtime = _runtime()
    runtime.init_db()
    normalized_exclusions = tuple(
        (str(task_id), str(started_at))
        for task_id, started_at in excluded_claims
        if str(task_id) and str(started_at)
    )
    exclusion_sql = "".join(
        " AND NOT (task_id = ? AND started_at = ?)"
        for _claim in normalized_exclusions
    )
    exclusion_params = tuple(
        value
        for claim in normalized_exclusions
        for value in claim
    )
    cutoff = (datetime.now() - timedelta(minutes=timeout_minutes)).isoformat()
    with runtime.db_lock:
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = runtime.now()
            cur = conn.execute(
                f"""
                UPDATE distillation_tasks
                SET status = 'pending',
                    started_at = NULL,
                    updated_at = ?,
                    progress_step = ?,
                    progress = 0.0,
                    error = ?,
                    progress_detail = ?,
                    next_retry_at = NULL
                WHERE status = 'processing'
                  AND (
                        (
                            (updated_at IS NOT NULL OR started_at IS NOT NULL)
                            AND MAX(COALESCE(updated_at, ''), COALESCE(started_at, '')) < ?
                        )
                        OR (
                            updated_at IS NULL
                            AND started_at IS NULL
                            AND created_at < ?
                        )
                  )
                  {exclusion_sql}
            """,  # nosec B608: exclusion clauses contain bind markers only
                (
                    now,
                    DistillProgress.PENDING.value,
                    f"processing timeout after {timeout_minutes} minutes",
                    "reset by timeout watchdog",
                    cutoff,
                    cutoff,
                    *exclusion_params,
                ),
            )
            conn.commit()
            return cur.rowcount


def mark_failed_with_transition(
    identifier: str,
    error: str,
    *,
    expected_started_at: str | None = None,
) -> DistillationFailureTransition | None:
    """
    Atomically persist one task failure and return the committed transition.

    The transition is the sole authority for retry exhaustion.  Callers must
    not infer terminal state from a process-wide retry constant or from the
    stale task DTO that was claimed before this transaction.
    """
    runtime = _runtime()
    runtime.init_db()
    with runtime.db_lock:
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            column, value = _identifier_filter(conn, identifier)
            row = conn.execute(
                f"""
                SELECT task_id, session_id, input_revision, status, started_at,
                       retry_count, max_retries, meta
                FROM distillation_tasks
                WHERE {column}=?
                """,  # nosec B608
                (value,),
            ).fetchone()
            if not row:
                conn.commit()
                return None

            max_retries = max(1, int(row["max_retries"] or 0))
            retry_count = min(int(row["retry_count"] or 0) + 1, max_retries)
            now = runtime.now()
            if retry_count < max_retries:
                status = "pending"
                next_retry_at = _retry_time(retry_count)
                completed_at = None
                error_msg = f"{error} (retry {retry_count}/{max_retries})"
            else:
                status = "failed"
                next_retry_at = None
                completed_at = now
                error_msg = f"{error} (final fail after {retry_count} retries)"
            try:
                meta = json.loads(row["meta"] or "{}")
            except (json.JSONDecodeError, TypeError):
                raise RuntimeError("amphora_task_meta_invalid") from None
            if not isinstance(meta, dict):
                raise RuntimeError("amphora_task_meta_invalid")
            terminal_outbox_anchor_sha256 = ""
            if status == "failed":
                existing_outbox = _validated_failed_terminal_outbox(
                    meta.get("failed_terminal_receipt_outbox"),
                    task_id=str(row["task_id"]),
                )
                if existing_outbox is None:
                    cognitive_event_ids = _normalized_cognitive_event_ids(meta)
                    failed_outbox = {
                        "schema_version": (
                            "mnemos.amphora_failed_terminal_receipt_outbox.v2"
                        ),
                        "task_id": str(row["task_id"]),
                        "session_id": str(row["session_id"]),
                        "input_revision": str(row["input_revision"]),
                        "status": "pending",
                        "reason": f"retry_exhausted:{error}",
                        "retry_count": retry_count,
                        "max_retries": max_retries,
                        "created_at": now,
                        "cognitive_event_ids": cognitive_event_ids,
                        "cognitive_event_count": len(cognitive_event_ids),
                        "cognitive_event_ids_sha256": (
                            _cognitive_event_ids_sha256(cognitive_event_ids)
                        ),
                    }
                    failed_outbox["payload_sha256"] = (
                        distillation_failed_terminal_sha256(
                            task_id=str(row["task_id"]),
                            session_id=str(row["session_id"]),
                            input_revision=str(row["input_revision"]),
                            reason=str(failed_outbox["reason"]),
                            retry_count=retry_count,
                            max_retries=max_retries,
                            cognitive_event_ids=cognitive_event_ids,
                        )
                    )
                    meta["failed_terminal_receipt_outbox"] = failed_outbox
                terminal_outbox_anchor_sha256 = (
                    _terminal_outbox_anchor_sha256(
                        meta["failed_terminal_receipt_outbox"]
                    )
                )

            update_sql = """
                UPDATE distillation_tasks
                SET status = ?,
                    error = ?,
                    terminal_reason = ?,
                    completed_at = ?,
                    updated_at = ?,
                    retry_count = ?,
                    next_retry_at = ?,
                    progress_detail = ?,
                    meta = ?,
                    terminal_outbox_anchor_sha256 = ?
                WHERE task_id = ? AND status='processing'
            """
            claim_params: tuple[object, ...] = ()
            if expected_started_at is not None:
                update_sql = """
                    UPDATE distillation_tasks
                    SET status = ?,
                        error = ?,
                        terminal_reason = ?,
                        completed_at = ?,
                        updated_at = ?,
                        retry_count = ?,
                        next_retry_at = ?,
                        progress_detail = ?,
                        meta = ?,
                        terminal_outbox_anchor_sha256 = ?
                    WHERE task_id = ?
                      AND status='processing'
                      AND started_at=?
                """
                claim_params = (str(expected_started_at),)
            updated = conn.execute(
                update_sql,
                (
                    status,
                    error_msg,
                    error,
                    completed_at,
                    now,
                    retry_count,
                    next_retry_at,
                    error,
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    terminal_outbox_anchor_sha256,
                    row["task_id"],
                    *claim_params,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            return DistillationFailureTransition(
                task_id=str(row["task_id"]),
                status=status,
                retry_count=retry_count,
                max_retries=max_retries,
                terminal=status == "failed",
            )


def mark_failed(identifier: str, error: str) -> bool:
    """Compatibility boolean wrapper around the typed failure transition."""
    return mark_failed_with_transition(identifier, error) is not None


def list_terminal_receipt_outbox(
    *,
    identifier: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return committed/skip tasks whose runtime terminal still needs signing."""
    runtime = _runtime()
    runtime.init_db()
    where = "status IN ('committed', 'intentional_skip')"
    with runtime.connect() as conn:
        params: list[object] = []
        if identifier:
            column, value = _identifier_filter(conn, identifier)
            where += f" AND {column} = ?"  # nosec B608
            params.append(value)
        rows = conn.execute(
            f"""
            SELECT * FROM distillation_tasks
            WHERE {where}
            ORDER BY COALESCE(completed_at, created_at) ASC
            """,  # nosec B608
            tuple(params),
        )
        pending: list[dict] = []
        selected_limit = max(1, int(limit))
        for row in rows:
            frozen_task: dict | None = None
            frozen_receipt: DistillationWriteReceipt | None = None
            try:
                task = runtime.row_to_dict(row)
                outbox = _validated_terminal_receipt_outbox(
                    task.get("meta", {}).get("terminal_receipt_outbox"),
                    task_id=str(task.get("task_id") or ""),
                )
                if outbox is not None and outbox.get("status") == "pending":
                    if not _terminal_outbox_anchor_matches_row(row, outbox):
                        raise RuntimeError(
                            "amphora_terminal_outbox_anchor_mismatch"
                        )
                    frozen_task = _task_with_frozen_terminal_denominator(
                        row,
                        outbox,
                    )
                    frozen_receipt = (
                        _distillation_write_receipt_from_payload(
                            outbox.get("receipt")
                        )
                    )
                    if not _terminal_receipt_matches_row(
                        row,
                        frozen_receipt,
                    ):
                        raise RuntimeError(
                            "amphora_terminal_outbox_receipt_drift"
                        )
            except (
                json.JSONDecodeError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                quarantine = (
                    "terminal_outbox_quarantined:"
                    f"{type(exc).__name__}:{exc}"
                )
                conn.execute(
                    """
                    UPDATE distillation_tasks
                    SET progress_detail=?, updated_at=?
                    WHERE task_id=?
                      AND status IN ('committed', 'intentional_skip')
                      AND COALESCE(progress_detail, '') != ?
                    """,
                    (
                        quarantine,
                        runtime.now(),
                        str(row["task_id"]),
                        quarantine,
                    ),
                )
                continue
            if frozen_task is not None:
                if outbox is None:
                    raise RuntimeError("amphora_terminal_outbox_missing")
                task = frozen_task
                if str(task.get("progress_detail") or "").startswith(
                    "terminal_outbox_quarantined:"
                ):
                    conn.execute(
                        """
                        UPDATE distillation_tasks
                        SET progress_detail='', updated_at=?
                        WHERE task_id=?
                          AND status IN ('committed', 'intentional_skip')
                          AND progress_detail LIKE 'terminal_outbox_quarantined:%'
                        """,
                        (runtime.now(), str(task["task_id"])),
                    )
                    task["progress_detail"] = ""
                pending.append(
                    {
                        "task": task,
                        "outbox": dict(outbox),
                        "receipt": frozen_receipt,
                    }
                )
                if len(pending) >= selected_limit:
                    break
        conn.commit()
    return pending


def mark_terminal_receipt_outbox_committed(
    task_id: str,
    *,
    expected_created_at: str,
    runtime_receipt_id: str,
    production_event_id: str,
    generation_id: str,
    config: object,
) -> bool:
    """CAS one pending committed/skip outbox after independent ledger proof."""
    runtime = _runtime()
    runtime.init_db()
    _require_canonical_task_database_config(config)
    with runtime.db_lock:
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            if not row or str(row["status"]) not in {
                "committed",
                "intentional_skip",
            }:
                conn.commit()
                return False
            original_meta = str(row["meta"] or "{}")
            try:
                meta = json.loads(original_meta)
            except (json.JSONDecodeError, TypeError):
                conn.rollback()
                raise RuntimeError("amphora_task_meta_invalid") from None
            if not isinstance(meta, dict):
                conn.rollback()
                raise RuntimeError("amphora_task_meta_invalid")
            outbox = _validated_terminal_receipt_outbox(
                meta.get("terminal_receipt_outbox"),
                task_id=str(task_id),
            )
            if not (
                outbox is not None
                and outbox.get("status") == "pending"
                and outbox.get("created_at") == str(expected_created_at)
            ):
                conn.commit()
                return False
            if not _terminal_outbox_anchor_matches_row(row, outbox):
                conn.rollback()
                raise RuntimeError("terminal_receipt_anchor_mismatch")
            receipt = _distillation_write_receipt_from_payload(
                outbox.get("receipt")
            )
            if not _terminal_receipt_matches_row(row, receipt):
                conn.rollback()
                raise RuntimeError("terminal_receipt_task_payload_mismatch")
            from core.ops.cognitive_pipeline_receipts import (
                verify_distillation_terminal,
            )

            evidence = verify_distillation_terminal(
                config,
                task=_task_with_frozen_terminal_denominator(row, outbox),
                receipt=receipt,
            )
            requested_proof = {
                "runtime_receipt_id": str(runtime_receipt_id),
                "production_event_id": str(production_event_id),
                "generation_id": str(generation_id),
            }
            verified_proof = {
                "runtime_receipt_id": evidence.get("runtime_receipt_id"),
                "production_event_id": evidence.get("production_event_id"),
                "generation_id": evidence.get("generation_id"),
            }
            if not evidence.get("verified") or requested_proof != verified_proof:
                conn.rollback()
                raise RuntimeError("terminal_receipt_proof_verification_failed")
            committed = dict(outbox)
            committed["status"] = "committed"
            committed["committed_at"] = runtime.now()
            committed.update(requested_proof)
            if not all(
                str(committed[field] or "").strip()
                for field in (
                    "runtime_receipt_id",
                    "production_event_id",
                    "generation_id",
                )
            ):
                conn.rollback()
                raise RuntimeError("terminal_receipt_proof_incomplete")
            meta["terminal_receipt_outbox"] = committed
            updated = conn.execute(
                """
                UPDATE distillation_tasks
                SET meta=?, updated_at=?
                WHERE task_id=?
                  AND status IN ('committed', 'intentional_skip')
                  AND meta=?
                """,
                (
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    committed["committed_at"],
                    str(task_id),
                    original_meta,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True


def list_failed_terminal_receipt_outbox(
    *,
    identifier: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return failed tasks whose durable terminal receipt still needs signing."""
    runtime = _runtime()
    runtime.init_db()
    where = "status = 'failed'"
    with runtime.connect() as conn:
        params: list[object] = []
        if identifier:
            column, value = _identifier_filter(conn, identifier)
            where += f" AND {column} = ?"  # nosec B608
            params.append(value)
        rows = conn.execute(
            f"""
            SELECT * FROM distillation_tasks
            WHERE {where}
            ORDER BY COALESCE(completed_at, created_at) ASC
            """,  # nosec B608
            tuple(params),
        )
        pending: list[dict] = []
        selected_limit = max(1, int(limit))
        for row in rows:
            frozen_task: dict | None = None
            try:
                task = runtime.row_to_dict(row)
                outbox = _validated_failed_terminal_outbox(
                    task.get("meta", {}).get(
                        "failed_terminal_receipt_outbox"
                    ),
                    task_id=str(task.get("task_id") or ""),
                )
                if outbox is not None and outbox.get("status") == "pending":
                    if not _terminal_outbox_anchor_matches_row(row, outbox):
                        raise RuntimeError(
                            "amphora_failed_terminal_outbox_anchor_mismatch"
                        )
                    frozen_task = _task_with_frozen_terminal_denominator(
                        row,
                        outbox,
                    )
                    if not _failed_terminal_outbox_matches_row(row, outbox):
                        raise RuntimeError(
                            "amphora_failed_terminal_outbox_payload_drift"
                        )
            except (
                json.JSONDecodeError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                quarantine = (
                    "failed_terminal_outbox_quarantined:"
                    f"{type(exc).__name__}:{exc}"
                )
                conn.execute(
                    """
                    UPDATE distillation_tasks
                    SET progress_detail=?, updated_at=?
                    WHERE task_id=? AND status='failed'
                      AND COALESCE(progress_detail, '') != ?
                    """,
                    (
                        quarantine,
                        runtime.now(),
                        str(row["task_id"]),
                        quarantine,
                    ),
                )
                continue
            if frozen_task is not None:
                if outbox is None:
                    raise RuntimeError("amphora_failed_terminal_outbox_missing")
                task = frozen_task
                if str(task.get("progress_detail") or "").startswith(
                    "failed_terminal_outbox_quarantined:"
                ):
                    conn.execute(
                        """
                        UPDATE distillation_tasks
                        SET progress_detail='', updated_at=?
                        WHERE task_id=? AND status='failed'
                          AND progress_detail LIKE
                              'failed_terminal_outbox_quarantined:%'
                        """,
                        (runtime.now(), str(task["task_id"])),
                    )
                    task["progress_detail"] = ""
                pending.append({"task": task, "outbox": dict(outbox)})
                if len(pending) >= selected_limit:
                    break
        conn.commit()
    return pending


def mark_failed_terminal_receipt_outbox_committed(
    task_id: str,
    *,
    expected_created_at: str,
    runtime_receipt_id: str,
    production_event_id: str,
    generation_id: str,
    config: object,
) -> bool:
    """CAS one pending failed-terminal outbox entry to committed."""
    runtime = _runtime()
    runtime.init_db()
    _require_canonical_task_database_config(config)
    with runtime.db_lock:
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM distillation_tasks WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            if not row or str(row["status"]) != "failed":
                conn.commit()
                return False
            original_meta = str(row["meta"] or "{}")
            try:
                meta = json.loads(original_meta)
            except (json.JSONDecodeError, TypeError):
                conn.rollback()
                raise RuntimeError("amphora_task_meta_invalid") from None
            if not isinstance(meta, dict):
                conn.rollback()
                raise RuntimeError("amphora_task_meta_invalid")
            outbox = _validated_failed_terminal_outbox(
                meta.get("failed_terminal_receipt_outbox"),
                task_id=str(task_id),
            )
            if not (
                outbox is not None
                and outbox.get("status") == "pending"
                and outbox.get("created_at") == str(expected_created_at)
            ):
                conn.commit()
                return False
            if not _terminal_outbox_anchor_matches_row(row, outbox):
                conn.rollback()
                raise RuntimeError("failed_terminal_receipt_anchor_mismatch")
            if not _failed_terminal_outbox_matches_row(row, outbox):
                conn.rollback()
                raise RuntimeError(
                    "failed_terminal_receipt_task_payload_mismatch"
                )
            from core.ops.cognitive_pipeline_receipts import (
                verify_distillation_failed_terminal,
            )

            evidence = verify_distillation_failed_terminal(
                config,
                task=_task_with_frozen_terminal_denominator(row, outbox),
                expected_reason=str(outbox.get("reason") or ""),
            )
            requested_proof = {
                "runtime_receipt_id": str(runtime_receipt_id),
                "production_event_id": str(production_event_id),
                "generation_id": str(generation_id),
            }
            verified_proof = {
                "runtime_receipt_id": evidence.get("runtime_receipt_id"),
                "production_event_id": evidence.get("production_event_id"),
                "generation_id": evidence.get("generation_id"),
            }
            if not evidence.get("verified") or requested_proof != verified_proof:
                conn.rollback()
                raise RuntimeError(
                    "failed_terminal_receipt_proof_verification_failed"
                )
            committed = dict(outbox)
            committed["status"] = "committed"
            committed["committed_at"] = runtime.now()
            committed["runtime_receipt_id"] = str(runtime_receipt_id)
            committed["production_event_id"] = str(production_event_id)
            committed["generation_id"] = str(generation_id)
            if not all(
                str(committed[field] or "").strip()
                for field in (
                    "runtime_receipt_id",
                    "production_event_id",
                    "generation_id",
                )
            ):
                conn.rollback()
                raise RuntimeError("failed_terminal_receipt_proof_incomplete")
            meta["failed_terminal_receipt_outbox"] = committed
            updated = conn.execute(
                """
                UPDATE distillation_tasks
                SET meta=?, updated_at=?
                WHERE task_id=? AND status='failed' AND meta=?
                """,
                (
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    committed["committed_at"],
                    str(task_id),
                    original_meta,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True


def _failed_task_ids(
    conn: sqlite3.Connection,
    identifier: str | None = None,
    limit: int | None = None,
) -> list[str]:
    if identifier:
        column, value = _identifier_filter(conn, identifier)
        rows = conn.execute(
            f"""
            SELECT task_id FROM distillation_tasks
            WHERE {column} = ? AND status = 'failed'
            """,  # nosec B608
            (value,),
        ).fetchall()
        return [row["task_id"] for row in rows]

    query = """
        SELECT task_id FROM distillation_tasks
        WHERE status = 'failed'
        ORDER BY COALESCE(completed_at, created_at) ASC
    """
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (max(1, int(limit)),)
    rows = conn.execute(query, params).fetchall()
    return [row["task_id"] for row in rows]


def retry_failed(
    identifier: str | None = None,
    *,
    limit: int | None = None,
    reason: str = "manual retry",
) -> int:
    """Reject in-place reopening of a terminal task generation."""
    runtime = _runtime()
    runtime.init_db()
    with runtime.db_lock:
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_ids = _failed_task_ids(conn, identifier=identifier, limit=limit)
            if not task_ids:
                conn.commit()
                return 0
            for task_id in task_ids:
                row = conn.execute(
                    "SELECT meta FROM distillation_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                try:
                    meta = json.loads((row["meta"] if row else None) or "{}")
                except (json.JSONDecodeError, TypeError):
                    conn.rollback()
                    raise RuntimeError("amphora_task_meta_invalid") from None
                if not isinstance(meta, dict):
                    conn.rollback()
                    raise RuntimeError("amphora_task_meta_invalid")
                outbox = _validated_failed_terminal_outbox(
                    meta.get("failed_terminal_receipt_outbox"),
                    task_id=str(task_id),
                )
                if outbox is None:
                    conn.rollback()
                    raise RuntimeError(
                        "failed_terminal_receipt_required_before_retry"
                    )
                conn.rollback()
                raise RuntimeError(
                    "failed_terminal_generation_requires_new_input_revision"
                )
    raise RuntimeError("failed_terminal_retry_state_unreachable")


def archive_failed(
    identifier: str | None = None,
    *,
    limit: int | None = None,
    reason: str = "manual archive",
    config: object | None = None,
) -> int:
    """Archive failed tasks with an explicit reason so health can exclude them."""
    runtime = _runtime()
    runtime.init_db()
    if config is not None:
        _require_canonical_task_database_config(config)
    with runtime.db_lock:
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_ids = _failed_task_ids(conn, identifier=identifier, limit=limit)
            if not task_ids:
                conn.commit()
                return 0
            now = runtime.now()
            for task_id in task_ids:
                row = conn.execute(
                    "SELECT * FROM distillation_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                try:
                    meta = json.loads((row["meta"] if row else None) or "{}")
                except (json.JSONDecodeError, TypeError):
                    conn.rollback()
                    raise RuntimeError("amphora_task_meta_invalid") from None
                if not isinstance(meta, dict):
                    conn.rollback()
                    raise RuntimeError("amphora_task_meta_invalid")
                try:
                    outbox = _validated_failed_terminal_outbox(
                        meta.get("failed_terminal_receipt_outbox"),
                        task_id=str(task_id),
                    )
                    if outbox is not None and (
                        not _terminal_outbox_anchor_matches_row(row, outbox)
                        or not _failed_terminal_outbox_matches_row(
                            row,
                            outbox,
                        )
                    ):
                        raise RuntimeError(
                            "failed_terminal_archive_payload_mismatch"
                        )
                except RuntimeError:
                    conn.rollback()
                    raise RuntimeError(
                        "failed_terminal_archive_receipt_verification_failed"
                    ) from None
                if outbox is None:
                    conn.rollback()
                    raise RuntimeError(
                        "failed_terminal_receipt_required_before_archive"
                    )
                if outbox.get("status") == "pending":
                    conn.rollback()
                    raise RuntimeError(
                        "failed_terminal_receipt_must_commit_before_archive"
                    )
                if config is None:
                    conn.rollback()
                    raise RuntimeError(
                        "failed_terminal_archive_requires_runtime_ledger"
                    )
                from core.ops.cognitive_pipeline_receipts import (
                    verify_distillation_failed_terminal,
                )

                evidence = verify_distillation_failed_terminal(
                    config,
                    task=_task_with_frozen_terminal_denominator(row, outbox),
                    expected_reason=str(outbox.get("reason") or ""),
                )
                expected_proof = {
                    "runtime_receipt_id": outbox.get("runtime_receipt_id"),
                    "production_event_id": outbox.get("production_event_id"),
                    "generation_id": outbox.get("generation_id"),
                }
                actual_proof = {
                    "runtime_receipt_id": evidence.get("runtime_receipt_id"),
                    "production_event_id": evidence.get("production_event_id"),
                    "generation_id": evidence.get("generation_id"),
                }
                if not evidence.get("verified") or actual_proof != expected_proof:
                    conn.rollback()
                    raise RuntimeError(
                        "failed_terminal_archive_receipt_verification_failed"
                    )
                conn.execute(
                    """
                    UPDATE distillation_tasks
                    SET status = 'archived',
                        completed_at = ?,
                        updated_at = ?,
                        next_retry_at = NULL,
                        progress_detail = ?,
                        error = COALESCE(error, '') || ?
                    WHERE task_id = ?
                    """,
                    (
                        now,
                        now,
                        reason,
                        f"\narchived at {now}: {reason}",
                        task_id,
                    ),
                )
            conn.commit()
            return len(task_ids)


def update_progress(
    identifier: str,
    step: str,
    detail: str = "",
) -> bool:
    """Update the typed progress step and detail for one task."""
    runtime = _runtime()
    runtime.init_db()
    with runtime.connect() as conn:
        column, value = _identifier_filter(conn, identifier)
        now = runtime.now()
        cur = conn.execute(
            f"""
            UPDATE distillation_tasks
            SET progress_step = ?, progress_detail = ?, updated_at = ?
            WHERE {column} = ?
        """,  # nosec B608
            (str(step), detail, now, value),
        )
        return cur.rowcount > 0
