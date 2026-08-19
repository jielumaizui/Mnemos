"""Append-only historical provenance reconciliation for Amphora."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Dict, List

from core.kia.amphora_provenance_support import (
    AmphoraProvenanceContext,
    _visible_message_projection,
    read_owned_message_asset_bytes,
)
from core.kia.amphora_types import (
    PROVENANCE_MIGRATION_SCHEMA,
    SYSTEM_OWNED_META_KEYS,
)
from core.ops.durable_io import (
    SecureImmutablePublishReceipt,
    secure_remove_regular_file,
    validate_secure_immutable_publish_receipt,
)
from core.pipeline_receipts import DistillationEnqueueReceipt


@dataclass(frozen=True)
class _ProvenanceReconciliationRuntime:
    db_lock: threading.Lock
    backup_historical_object: Callable[..., tuple[Path, str]]
    connect: Callable[[], sqlite3.Connection]
    historical_inventory: Callable[..., dict[str, Any]]
    init_db: Callable[[], None]
    messages_dir: Callable[[], Path]
    messages_revision: Callable[[List[Dict]], str]
    normalize_messages: Callable[[object], List[Dict]]
    normalize_priority: Callable[[int | None, Dict | None], int]
    now: Callable[[], str]
    provenance_context: Callable[[], AmphoraProvenanceContext]
    task_id: Callable[[str, str, str], str]
    write_messages: Callable[..., Any]
    build_historical_inventory: Callable[[], dict[str, Any]]


_RUNTIME: _ProvenanceReconciliationRuntime | None = None


def bind_provenance_reconciliation_runtime(
    *,
    _DB_LOCK: threading.Lock,
    _backup_historical_provenance_object_support: Callable[..., tuple[Path, str]],
    _connect: Callable[[], sqlite3.Connection],
    _historical_provenance_inventory_support: Callable[..., dict[str, Any]],
    _init_db: Callable[[], None],
    _messages_dir: Callable[[], Path],
    _messages_revision: Callable[[List[Dict]], str],
    _normalize_messages: Callable[[object], List[Dict]],
    _normalize_priority: Callable[[int | None, Dict | None], int],
    _now: Callable[[], str],
    _provenance_context: Callable[[], AmphoraProvenanceContext],
    _task_id: Callable[[str, str, str], str],
    _write_messages: Callable[..., Any],
    build_historical_provenance_inventory: Callable[[], dict[str, Any]],
) -> None:
    """Bind queue-owned state without importing ``amphora`` cyclically."""
    global _RUNTIME
    _RUNTIME = _ProvenanceReconciliationRuntime(
        db_lock=_DB_LOCK,
        backup_historical_object=_backup_historical_provenance_object_support,
        connect=_connect,
        historical_inventory=_historical_provenance_inventory_support,
        init_db=_init_db,
        messages_dir=_messages_dir,
        messages_revision=_messages_revision,
        normalize_messages=_normalize_messages,
        normalize_priority=_normalize_priority,
        now=_now,
        provenance_context=_provenance_context,
        task_id=_task_id,
        write_messages=_write_messages,
        build_historical_inventory=build_historical_provenance_inventory,
    )


def _runtime() -> _ProvenanceReconciliationRuntime:
    if _RUNTIME is None:
        raise RuntimeError("amphora_provenance_runtime_unbound")
    return _RUNTIME


def reconcile_historical_task_provenance(
    *,
    session_id: str,
    messages: List[Dict],
    meta: Dict,
    reviewed_task_id: str,
    expected_old_input_revision: str,
    expected_object_hash: str,
    expected_inventory_hash: str,
    backup_dir: Path,
) -> DistillationEnqueueReceipt:
    """Create a canonical task from one exact reviewed historical object.

    The historical row and its messages asset remain unchanged. Apply requires an
    exact reviewed inventory/object identity and a verified SQLite/file backup;
    completion is recorded in an append-only migration receipt.
    """

    runtime = _runtime()
    runtime.init_db()
    normalized_messages = runtime.normalize_messages(messages)
    present_reserved = sorted(SYSTEM_OWNED_META_KEYS.intersection(meta))
    if present_reserved:
        raise ValueError(f"{present_reserved[0]}_is_reserved")
    source_agent = str(meta.get("source") or "")
    input_revision = str(meta.get("input_revision") or "")
    handoff_receipt_id = str(meta.get("handoff_receipt_id") or "")
    if not source_agent or not input_revision or not handoff_receipt_id:
        raise ValueError(
            "legacy provenance reconciliation requires source, input_revision, "
            "and handoff_receipt_id"
        )

    reviewed_task_id = str(reviewed_task_id or "").strip()
    expected_old_input_revision = str(expected_old_input_revision or "")
    expected_object_hash = str(expected_object_hash or "").strip()
    expected_inventory_hash = str(expected_inventory_hash or "").strip()
    if not all((reviewed_task_id, expected_object_hash, expected_inventory_hash)):
        raise ValueError("legacy provenance migration requires exact reviewed identity")
    inventory = runtime.build_historical_inventory()
    if inventory["inventory_hash"] != expected_inventory_hash:
        raise ValueError("legacy provenance inventory hash drifted")
    reviewed = [item for item in inventory["objects"] if item["primary_key"] == reviewed_task_id]
    if len(reviewed) != 1:
        raise ValueError("reviewed legacy task is absent from the exact inventory")
    reviewed_object = reviewed[0]
    if (
        reviewed_object["old_input_revision"] != expected_old_input_revision
        or reviewed_object["object_hash"] != expected_object_hash
    ):
        raise ValueError("reviewed legacy task identity mismatch")
    try:
        legacy_message_bytes = read_owned_message_asset_bytes(
            database_path=runtime.provenance_context().db_path(),
            messages_path=str(reviewed_object["messages_asset"]["path"]),
            purpose="reviewed legacy messages asset",
        )
        legacy_messages = json.loads(legacy_message_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("reviewed legacy messages asset is invalid") from exc
    if _visible_message_projection(legacy_messages) != _visible_message_projection(
        normalized_messages
    ):
        raise ValueError("legacy and canonical visible messages differ")
    if runtime.messages_revision(normalized_messages) != input_revision:
        raise ValueError("canonical Amphora input revision does not match messages")
    if reviewed_object["covered"]:
        with runtime.connect() as conn:
            row = conn.execute(
                "SELECT canonical_task_id FROM amphora_provenance_migrations "
                "WHERE legacy_task_id=?",
                (reviewed_task_id,),
            ).fetchone()
            canonical = (
                conn.execute(
                    "SELECT * FROM distillation_tasks WHERE task_id=?",
                    (str(row[0]),),
                ).fetchone()
                if row
                else None
            )
        if canonical is None:
            raise RuntimeError("Amphora provenance migration receipt is orphaned")
        return DistillationEnqueueReceipt(
            receipt_id=str(canonical["receipt_id"]),
            task_id=str(canonical["task_id"]),
            source_agent=str(canonical["source_agent"]),
            session_id=str(canonical["session_id"]),
            input_revision=str(canonical["input_revision"]),
            status=str(canonical["status"]),
            created=False,
        )
    canonical_task_id = runtime.task_id(session_id, source_agent, input_revision)
    migration_id = (
        "amphora-migration-"
        + hashlib.sha256(
            (
                reviewed_task_id
                + "\0"
                + expected_object_hash
                + "\0"
                + canonical_task_id
                + "\0"
                + input_revision
            ).encode("utf-8")
        ).hexdigest()[:32]
    )
    with runtime.db_lock:
        provenance_context = runtime.provenance_context()
        manifest_path, manifest_hash = runtime.backup_historical_object(
            context=provenance_context,
            backup_dir=Path(backup_dir),
            inventory=inventory,
            reviewed_object=reviewed_object,
        )
        with runtime.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fresh = runtime.historical_inventory(
                conn,
                context=provenance_context,
            )
            fresh_objects = [
                item for item in fresh["objects"] if item["primary_key"] == reviewed_task_id
            ]
            if (
                fresh["inventory_hash"] != expected_inventory_hash
                or len(fresh_objects) != 1
                or fresh_objects[0]["object_hash"] != expected_object_hash
                or fresh_objects[0]["old_input_revision"] != expected_old_input_revision
            ):
                raise ValueError("legacy provenance object drifted after backup")
            try:
                fresh_legacy_message_bytes = read_owned_message_asset_bytes(
                    database_path=provenance_context.db_path(),
                    messages_path=str(fresh_objects[0]["messages_asset"]["path"]),
                    purpose="legacy provenance messages asset",
                )
                fresh_legacy_messages = json.loads(fresh_legacy_message_bytes.decode("utf-8"))
            except (
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                raise ValueError("legacy provenance messages drifted after backup") from exc
            if _visible_message_projection(fresh_legacy_messages) != _visible_message_projection(
                normalized_messages
            ):
                raise ValueError("legacy and canonical visible messages differ")
            exact = conn.execute(
                "SELECT task_id FROM distillation_tasks WHERE source_agent=? "
                "AND session_id=? AND input_revision=?",
                (source_agent, session_id, input_revision),
            ).fetchone()
            if exact is not None:
                raise RuntimeError("canonical Amphora identity exists without a migration receipt")
            messages_path = runtime.messages_dir() / f"{canonical_task_id}.json"
            if messages_path.exists():
                raise RuntimeError("canonical Amphora messages asset already exists")
            messages_publication: SecureImmutablePublishReceipt | None = None
            transaction_committed = False
            try:
                publication = runtime.write_messages(
                    canonical_task_id,
                    normalized_messages,
                    return_receipt=True,
                )
                if not isinstance(
                    publication,
                    SecureImmutablePublishReceipt,
                ):
                    raise RuntimeError("canonical Amphora messages publication receipt missing")
                messages_path = publication.path
                messages_publication = publication
                reconciled_meta = dict(meta)
                reconciled_meta["messages_revision"] = input_revision
                reconciled_meta["provenance_migration"] = {
                    "schema_version": PROVENANCE_MIGRATION_SCHEMA,
                    "migration_id": migration_id,
                    "legacy_task_id": reviewed_task_id,
                    "legacy_object_hash": expected_object_hash,
                    "inventory_hash": expected_inventory_hash,
                    "backup_manifest_hash": manifest_hash,
                }
                now = runtime.now()
                conn.execute(
                    """
                    INSERT INTO distillation_tasks(
                        task_id, session_id, source_agent, input_revision, generation,
                        receipt_id, handoff_receipt_id, status, priority, retry_count,
                        max_retries, messages_path, meta, progress_step, progress_detail,
                        progress, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, 3, ?, ?, 'pending', ?, 0.0, ?, ?)
                    """,
                    (
                        canonical_task_id,
                        session_id,
                        source_agent,
                        input_revision,
                        int(reviewed_object["row"].get("generation") or 0) + 1,
                        f"amphora-{canonical_task_id}",
                        handoff_receipt_id,
                        runtime.normalize_priority(None, meta),
                        str(messages_path),
                        json.dumps(reconciled_meta, ensure_ascii=False),
                        "canonical task created by reviewed object-level provenance migration",
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO amphora_provenance_migrations(
                        migration_id, schema_version, legacy_task_id,
                        legacy_input_revision, legacy_object_hash, inventory_hash,
                        backup_manifest_hash, backup_manifest_path, canonical_task_id,
                        canonical_input_revision, handoff_receipt_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        migration_id,
                        PROVENANCE_MIGRATION_SCHEMA,
                        reviewed_task_id,
                        expected_old_input_revision,
                        expected_object_hash,
                        expected_inventory_hash,
                        manifest_hash,
                        str(manifest_path),
                        canonical_task_id,
                        input_revision,
                        handoff_receipt_id,
                        now,
                    ),
                )
                if messages_publication is None:
                    raise RuntimeError("canonical Amphora messages publication receipt missing")
                validate_secure_immutable_publish_receipt(messages_publication)
                conn.commit()
                transaction_committed = True
            finally:
                if (
                    messages_publication is not None
                    and messages_publication.created
                    and not transaction_committed
                ):
                    secure_remove_regular_file(
                        messages_publication.path.parent,
                        messages_publication.path.name,
                        expected_preimage=messages_publication.preimage,
                    )
            return DistillationEnqueueReceipt(
                receipt_id=f"amphora-{canonical_task_id}",
                task_id=canonical_task_id,
                source_agent=source_agent,
                session_id=session_id,
                input_revision=input_revision,
                status="pending",
                created=True,
            )
