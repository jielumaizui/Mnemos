"""Transactional executor for reviewed Amphora source-span generations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.kia import amphora
from core.kia.amphora_provenance_support import read_exact_regular_file_bytes
from core.kia.amphora_types import SYSTEM_OWNED_META_KEYS
from core.ops.durable_io import (
    SecureImmutablePublishReceipt,
    secure_remove_regular_file,
    validate_secure_immutable_publish_receipt,
)


def apply_source_span_migrations(
    *,
    objects: Sequence[Mapping[str, Any]],
    inventory_hash: str,
    backup_manifest_path: Path,
    backup_manifest_hash: str,
    backup_manifest_file_hash: str,
) -> dict[str, Any]:
    """Atomically supersede reviewed span-less tasks with exact-Raw generations.

    The historical row and message asset remain in place.  Every new task is bound
    to a reviewed immutable Raw preimage, while the old generation receives a
    typed ``intentional_skip`` terminal state so the runtime ledger can close
    it without pretending that a Wiki page was written.
    """

    from core.kia.amphora_source_span_reconciliation import (
        MIGRATION_REASON_PREFIX,
        MIGRATION_SCHEMA_VERSION,
        historical_object_hash,
    )

    reviewed = list(objects)
    if not inventory_hash or not reviewed:
        raise ValueError("source span migration requires a reviewed inventory")
    manifest_path = Path(backup_manifest_path).expanduser().absolute()
    if not backup_manifest_hash or not backup_manifest_file_hash:
        raise ValueError("source span migration requires a verified backup manifest")
    try:
        manifest_bytes = read_exact_regular_file_bytes(
            manifest_path,
            purpose="source span migration backup manifest",
        )
    except ValueError as exc:
        raise ValueError("source span migration requires a verified backup manifest") from exc
    actual_manifest_file_hash = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_file_hash != backup_manifest_file_hash:
        raise ValueError("source span migration backup manifest hash mismatch")

    amphora._init_db()
    message_publication_receipts: list[SecureImmutablePublishReceipt] = []
    created_tasks = 0
    reused_tasks = 0
    retired_tasks = 0
    applied: list[dict[str, str]] = []
    transaction_committed = False
    with amphora._DB_LOCK:
        with amphora._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in reviewed:
                    legacy_task_id = str(item.get("legacy_task_id") or "")
                    canonical_task_id = str(item.get("canonical_task_id") or "")
                    canonical_revision = str(item.get("canonical_input_revision") or "")
                    migration_id = str(item.get("migration_id") or "")
                    if not all(
                        (
                            legacy_task_id,
                            canonical_task_id,
                            canonical_revision,
                            migration_id,
                        )
                    ):
                        raise ValueError("source span migration identity is incomplete")
                    legacy = conn.execute(
                        "SELECT * FROM distillation_tasks WHERE task_id=?",
                        (legacy_task_id,),
                    ).fetchone()
                    if legacy is None:
                        raise ValueError("reviewed legacy task is missing")
                    legacy_payload = dict(legacy)
                    legacy_path = Path(str(legacy_payload.get("messages_path") or ""))
                    if historical_object_hash(
                        legacy_payload,
                        legacy_path,
                        database_path=amphora._db_path(),
                    ) != str(item.get("legacy_object_hash") or ""):
                        raise ValueError("reviewed legacy task preimage drifted")
                    if str(legacy_payload.get("input_revision") or "") != str(
                        item.get("legacy_input_revision") or ""
                    ):
                        raise ValueError("reviewed legacy input revision drifted")
                    if str(legacy_payload.get("status") or "") not in {
                        "pending",
                        "retryable_failed",
                        "partial",
                        "failed",
                    }:
                        raise ValueError("reviewed legacy task is no longer migratable")
                    canonical_messages = amphora._normalize_messages(item.get("canonical_messages"))
                    if (
                        not canonical_messages
                        or amphora._messages_revision(canonical_messages) != canonical_revision
                    ):
                        raise ValueError("canonical source span messages revision mismatch")
                    for message in canonical_messages:
                        span = message.get("source_span") if isinstance(message, dict) else None
                        if not isinstance(span, dict):
                            raise ValueError("canonical source span message is unbound")
                        try:
                            start = int(span["span_start"])
                            end = int(span["span_end"])
                        except (KeyError, TypeError, ValueError) as exc:
                            raise ValueError("canonical source span message is invalid") from exc
                        if (
                            not str(span.get("revision_id") or "")
                            or not str(span.get("content_hash") or "")
                            or start < 0
                            or end <= start
                            or end - start != len(str(message.get("content") or ""))
                            or str(span.get("role") or "") != str(message.get("role") or "")
                        ):
                            raise ValueError("canonical source span message is invalid")
                    expected_task_id = amphora._task_id(
                        str(item.get("session_id") or ""),
                        str(item.get("source_agent") or ""),
                        canonical_revision,
                    )
                    if canonical_task_id != expected_task_id:
                        raise ValueError("canonical source span task identity mismatch")
                    existing = conn.execute(
                        "SELECT * FROM distillation_tasks WHERE task_id=?",
                        (canonical_task_id,),
                    ).fetchone()
                    if existing is None:
                        messages_path = amphora._messages_dir() / f"{canonical_task_id}.json"
                        if amphora._physical_message_path_kind(messages_path) != "missing":
                            raise RuntimeError(
                                "canonical source span messages exist without a task receipt"
                            )
                        publication = amphora._write_messages(
                            canonical_task_id,
                            canonical_messages,
                            return_receipt=True,
                        )
                        if not isinstance(
                            publication,
                            SecureImmutablePublishReceipt,
                        ):
                            raise RuntimeError(
                                "canonical source span messages publication " "receipt missing"
                            )
                        messages_path = publication.path
                        message_publication_receipts.append(publication)
                        meta = {
                            key: value
                            for key, value in dict(item.get("canonical_meta") or {}).items()
                            if key not in SYSTEM_OWNED_META_KEYS
                        }
                        migration_meta = dict(meta.get("source_span_migration") or {})
                        migration_meta.update(
                            {
                                "schema_version": MIGRATION_SCHEMA_VERSION,
                                "migration_id": migration_id,
                                "legacy_task_id": legacy_task_id,
                                "legacy_object_hash": str(item.get("legacy_object_hash") or ""),
                                "raw_preimage_hash": str(item.get("raw_preimage_hash") or ""),
                                "inventory_hash": inventory_hash,
                                "backup_manifest_hash": backup_manifest_hash,
                            }
                        )
                        meta["source_span_migration"] = migration_meta
                        meta["input_revision"] = canonical_revision
                        meta["messages_revision"] = canonical_revision
                        generation = int(
                            conn.execute(
                                "SELECT COALESCE(MAX(generation), 0) + 1 "
                                "FROM distillation_tasks WHERE source_agent=? AND session_id=?",
                                (
                                    str(item.get("source_agent") or ""),
                                    str(item.get("session_id") or ""),
                                ),
                            ).fetchone()[0]
                            or 1
                        )
                        now = amphora._now()
                        conn.execute(
                            """
                            INSERT INTO distillation_tasks(
                                task_id, session_id, source_agent, input_revision,
                                generation, receipt_id, handoff_receipt_id, status,
                                priority, retry_count, max_retries, messages_path,
                                meta, progress_step, progress_detail, progress,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?, ?, ?, ?, 0.0, ?, ?)
                            """,
                            (
                                canonical_task_id,
                                str(item.get("session_id") or ""),
                                str(item.get("source_agent") or ""),
                                canonical_revision,
                                generation,
                                f"amphora-{canonical_task_id}",
                                str(meta.get("handoff_receipt_id") or ""),
                                int(legacy_payload.get("priority") or 0),
                                int(legacy_payload.get("max_retries") or 3),
                                str(messages_path),
                                json.dumps(meta, ensure_ascii=False),
                                amphora.DistillProgress.PENDING.value,
                                "created by verified immutable-Raw source span migration",
                                now,
                                now,
                            ),
                        )
                        created_tasks += 1
                    else:
                        existing_payload = dict(existing)
                        existing_messages = amphora._read_messages(
                            str(existing_payload.get("messages_path") or ""),
                            required=True,
                        )
                        if (
                            str(existing_payload.get("source_agent") or "")
                            != str(item.get("source_agent") or "")
                            or str(existing_payload.get("session_id") or "")
                            != str(item.get("session_id") or "")
                            or str(existing_payload.get("input_revision") or "")
                            != canonical_revision
                            or existing_messages != canonical_messages
                        ):
                            raise RuntimeError("canonical source span task collision")
                        reused_tasks += 1
                    reason = MIGRATION_REASON_PREFIX + canonical_task_id
                    now = amphora._now()
                    updated = conn.execute(
                        """
                        UPDATE distillation_tasks
                        SET status='intentional_skip', completed_at=?, updated_at=?,
                            terminal_reason=?, progress_step=?, progress_detail=?,
                            progress=1.0, error='', next_retry_at=NULL
                        WHERE task_id=?
                          AND status IN ('pending', 'retryable_failed', 'partial', 'failed')
                        """,
                        (
                            now,
                            now,
                            reason,
                            amphora.DistillProgress.DONE.value,
                            reason,
                            legacy_task_id,
                        ),
                    )
                    if int(updated.rowcount or 0) != 1:
                        raise RuntimeError("legacy source span task retirement failed")
                    retired_tasks += 1
                    conn.execute(
                        """
                        INSERT INTO amphora_source_span_migrations(
                            migration_id, schema_version, legacy_task_id,
                            legacy_input_revision, legacy_object_hash,
                            raw_preimage_hash, inventory_hash, canonical_task_id,
                            canonical_input_revision, backup_manifest_path,
                            backup_manifest_hash, backup_manifest_file_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            migration_id,
                            MIGRATION_SCHEMA_VERSION,
                            legacy_task_id,
                            str(item.get("legacy_input_revision") or ""),
                            str(item.get("legacy_object_hash") or ""),
                            str(item.get("raw_preimage_hash") or ""),
                            inventory_hash,
                            canonical_task_id,
                            canonical_revision,
                            str(manifest_path),
                            backup_manifest_hash,
                            backup_manifest_file_hash,
                            now,
                        ),
                    )
                    applied.append(
                        {
                            "legacy_task_id": legacy_task_id,
                            "canonical_task_id": canonical_task_id,
                            "canonical_input_revision": canonical_revision,
                            "terminal_reason": reason,
                        }
                    )
                for publication in message_publication_receipts:
                    validate_secure_immutable_publish_receipt(publication)
                conn.commit()
                transaction_committed = True
            finally:
                if not transaction_committed:
                    conn.rollback()
                for publication in message_publication_receipts:
                    if not transaction_committed and publication.created:
                        secure_remove_regular_file(
                            publication.path.parent,
                            publication.path.name,
                            expected_preimage=publication.preimage,
                        )
    return {
        "legacy_tasks_retired": retired_tasks,
        "canonical_tasks_created": created_tasks,
        "canonical_tasks_reused": reused_tasks,
        "migrations": applied,
    }
