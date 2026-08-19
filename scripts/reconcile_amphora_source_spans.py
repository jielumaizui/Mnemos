#!/usr/bin/env python3
"""Supersede legacy Amphora tasks with exact immutable-Raw span generations.

Dry-run is read-only.  Apply requires stopped runtime writers, an exact reviewed
inventory hash, and a new backup directory.  It preserves every legacy task and
message file, creates a new task generation, records an append-only migration
receipt, closes only the obsolete runtime generation, and adds Raw retention
edges for the replacement task.  It never calls a model or writes a Wiki page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.kia.amphora_source_span_reconciliation import (  # noqa: E402
    build_plan,
)
from core.kia.amphora_provenance_support import (  # noqa: E402
    read_exact_regular_file_bytes,
    read_owned_message_asset_bytes,
)
from core.ops.durable_io import (  # noqa: E402
    DurableIOError,
    SecureImmutablePublishReceipt,
    ensure_private_directory,
    fsync_directory,
    fsync_regular_file,
    normalize_private_sqlite_copy,
    owned_sqlite_connection_pair,
    regular_file_sha256,
    secure_cleanup_created_tree,
    secure_create_directory,
    secure_publish_immutable_bytes,
    secure_publish_immutable_text,
    secure_regular_file_preimage,
    secure_remove_regular_file,
    validate_secure_created_file_receipts,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite  # noqa: E402
from core.pipeline_receipts import DistillationEnqueueReceipt  # noqa: E402


def _file_sha256(path: Path) -> str:
    return "sha256:" + regular_file_sha256(path)


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = connect_readonly_sqlite(path, timeout_seconds=30)
    connection.row_factory = sqlite3.Row
    return connection


def _backup_database(
    source: Path,
    target: Path,
    *,
    backup_root: Path | None = None,
    created_files: dict[str, dict[str, object]] | None = None,
) -> dict[str, str]:
    created = False
    created_preimage: dict[str, object] | None = None
    try:
        descriptor = os.open(
            target,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        created = True
        metadata = os.fstat(descriptor)
        created_preimage = {
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
        }
        os.close(descriptor)
        with owned_sqlite_connection_pair(
            lambda: _connect_read_only(source),
            lambda: sqlite3.connect(target),
        ) as (src, dst):
            src.backup(dst)
            integrity = str(dst.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError("backup integrity_check failed")
        normalize_private_sqlite_copy(target)
        target.chmod(0o600)
        fsync_regular_file(target)
        fsync_directory(target.parent)
        if backup_root is not None and created_files is not None:
            _record_created_backup_file(
                backup_root=backup_root,
                path=target,
                created_files=created_files,
            )
    except DurableIOError:
        if created and created_preimage is not None:
            secure_remove_regular_file(
                target.parent,
                target.name,
                expected_preimage=created_preimage,
            )
        raise RuntimeError("backup normalization failed") from None
    except BaseException:
        if created and created_preimage is not None:
            secure_remove_regular_file(
                target.parent,
                target.name,
                expected_preimage=created_preimage,
            )
        raise
    return {
        "source": str(source),
        "path": str(target),
        "sha256": _file_sha256(target),
        "integrity_check": integrity,
    }


def _remove_incomplete_backup_leaf(
    backup_root: Path,
    *,
    created_files: dict[str, dict[str, object]],
    created_directories: dict[str, dict[str, object]],
) -> None:
    secure_cleanup_created_tree(
        backup_root,
        created_files=created_files,
        created_directories=created_directories,
    )


def _record_created_backup_file(
    *,
    backup_root: Path,
    path: Path,
    created_files: dict[str, dict[str, object]],
    preimage: Mapping[str, object] | None = None,
) -> None:
    relative = path.relative_to(backup_root).as_posix()
    observed = (
        dict(preimage)
        if preimage is not None
        else secure_regular_file_preimage(backup_root, relative)
    )
    if observed is None:
        raise DurableIOError("backup created file receipt missing")
    created_files[relative] = observed


def _publish_owned_backup_bytes(
    *,
    backup_root: Path,
    root: Path,
    relative_path: str | Path,
    content: bytes,
    created_files: dict[str, dict[str, object]],
) -> SecureImmutablePublishReceipt:
    publication = secure_publish_immutable_bytes(
        root,
        relative_path,
        content,
        return_receipt=True,
    )
    if not isinstance(publication, SecureImmutablePublishReceipt):
        raise RuntimeError("backup publication receipt missing")
    if publication.created:
        _record_created_backup_file(
            backup_root=backup_root,
            path=publication.path,
            created_files=created_files,
            preimage=publication.preimage,
        )
    return publication


def _publish_owned_backup_text(
    *,
    backup_root: Path,
    root: Path,
    relative_path: str | Path,
    content: str,
    created_files: dict[str, dict[str, object]],
) -> SecureImmutablePublishReceipt:
    publication = secure_publish_immutable_text(
        root,
        relative_path,
        content,
        encoding="utf-8",
        return_receipt=True,
    )
    if not isinstance(publication, SecureImmutablePublishReceipt):
        raise RuntimeError("backup publication receipt missing")
    if publication.created:
        _record_created_backup_file(
            backup_root=backup_root,
            path=publication.path,
            created_files=created_files,
            preimage=publication.preimage,
        )
    return publication


def _run_private_backup_leaf(
    backup_root: Path,
    leaf: Path,
    creation_preimage: dict[str, object],
    builder: Any,
) -> dict[str, Any]:
    created_files: dict[str, dict[str, object]] = {}
    created_directories = {leaf.name: creation_preimage}
    try:
        return dict(builder(created_files, created_directories))
    except BaseException:
        _remove_incomplete_backup_leaf(
            backup_root,
            created_files=created_files,
            created_directories=created_directories,
        )
        raise


def _backup_reviewed_inventory_in_leaf(
    *,
    database_dir: Path,
    backup_root: Path,
    leaf: Path,
    plan: Mapping[str, Any],
    created_files: dict[str, dict[str, object]],
    created_directories: dict[str, dict[str, object]],
) -> dict[str, Any]:
    queue_backup_path = leaf / "distill_queue.db"
    queue_backup = _backup_database(
        database_dir / "distill_queue.db",
        queue_backup_path,
        backup_root=backup_root,
        created_files=created_files,
    )
    ledger_path = database_dir / "producer_consumer_ledger.db"
    ledger_backup = None
    if ledger_path.is_file():
        ledger_backup_path = leaf / "producer_consumer_ledger.db"
        ledger_backup = _backup_database(
            ledger_path,
            ledger_backup_path,
            backup_root=backup_root,
            created_files=created_files,
        )
    messages_dir = leaf / "legacy_messages"
    messages_relative = messages_dir.relative_to(backup_root).as_posix()
    created_directories[messages_relative] = secure_create_directory(
        backup_root,
        messages_relative,
    )
    message_entries: list[dict[str, Any]] = []
    for item in plan.get("objects", []):
        source = Path(str(item["messages_path"]))
        target = messages_dir / f"{item['legacy_task_id']}.json"
        source_bytes = read_owned_message_asset_bytes(
            database_path=database_dir / "distill_queue.db",
            messages_path=source,
            purpose="source span messages asset",
        )
        _publish_owned_backup_bytes(
            backup_root=backup_root,
            root=messages_dir,
            relative_path=target.name,
            content=source_bytes,
            created_files=created_files,
        )
        source_hash = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
        target_bytes = read_exact_regular_file_bytes(
            target,
            purpose="source span messages backup",
        )
        target_hash = "sha256:" + hashlib.sha256(target_bytes).hexdigest()
        if source_hash != target_hash or source_bytes != target_bytes:
            raise RuntimeError("legacy messages backup hash mismatch")
        message_entries.append(
            {
                "legacy_task_id": str(item["legacy_task_id"]),
                "source_path": str(source),
                "source_sha256": source_hash,
                "backup_path": str(target),
                "backup_sha256": target_hash,
                "size": len(target_bytes),
            }
        )
    messages_manifest_path = leaf / "messages_manifest.json"
    _publish_owned_backup_text(
        backup_root=backup_root,
        root=leaf,
        relative_path=messages_manifest_path.name,
        content=json.dumps(
            {
                "schema_version": "mnemos.amphora_source_span_messages_backup.v1",
                "inventory_hash": str(plan["inventory_hash"]),
                "count": len(message_entries),
                "entries": message_entries,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        created_files=created_files,
    )
    manifest = {
        "schema_version": "mnemos.amphora_source_span_backup.v1",
        "inventory_hash": str(plan["inventory_hash"]),
        "object_manifest_hash": str(plan["object_manifest_hash"]),
        "database": queue_backup,
        "producer_consumer_ledger": ledger_backup,
        "messages_manifest_path": str(messages_manifest_path),
        "messages_manifest_sha256": _file_sha256(messages_manifest_path),
        "messages_count": len(message_entries),
    }
    manifest_hash = _canonical_hash(manifest)
    manifest_path = leaf / "backup_manifest.json"
    _publish_owned_backup_text(
        backup_root=backup_root,
        root=leaf,
        relative_path=manifest_path.name,
        content=json.dumps(
            {**manifest, "manifest_hash": manifest_hash},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        created_files=created_files,
    )
    fsync_directory(leaf)
    validate_secure_created_file_receipts(
        backup_root,
        created_files,
    )
    return {
        **manifest,
        "manifest_path": str(manifest_path),
        "manifest_hash": manifest_hash,
        "manifest_file_hash": _file_sha256(manifest_path),
    }


def _backup_reviewed_inventory(
    *,
    database_dir: Path,
    backup_dir: Path,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        backup_root = ensure_private_directory(backup_dir)
        leaf_name = f"amphora-source-spans-{stamp}"
        creation_preimage = secure_create_directory(backup_root, leaf_name)
    except DurableIOError:
        raise RuntimeError("backup directory is unsafe") from None
    leaf = backup_root / leaf_name
    return _run_private_backup_leaf(
        backup_root,
        leaf,
        creation_preimage,
        lambda created_files, created_directories: _backup_reviewed_inventory_in_leaf(
            database_dir=database_dir,
            backup_root=backup_root,
            leaf=leaf,
            plan=plan,
            created_files=created_files,
            created_directories=created_directories,
        ),
    )


def _runtime_writers_are_inactive(database_dir: Path) -> bool:
    from core.migrations.model_call_ledger_reconcile.runtime import (
        runtime_writers_are_inactive,
    )

    return runtime_writers_are_inactive(database_dir)


def build_source_span_reconciliation_plan(config: Any) -> dict[str, Any]:
    """Expose the complete internal plan for tests and the apply path."""

    return build_plan(Path(config.database_dir))


def _backup_capture_raw_inventory_in_leaf(
    *,
    database_dir: Path,
    backup_root: Path,
    leaf: Path,
    plan: Mapping[str, Any],
    created_files: dict[str, dict[str, object]],
    created_directories: dict[str, dict[str, object]],
) -> dict[str, Any]:
    del created_directories
    raw_backup_path = leaf / "raw_events.db"
    raw_backup = _backup_database(
        database_dir / "raw_events.db",
        raw_backup_path,
        backup_root=backup_root,
        created_files=created_files,
    )
    capture_backup_path = leaf / "capture_queue.db"
    capture_backup = _backup_database(
        database_dir / "capture_queue.db",
        capture_backup_path,
        backup_root=backup_root,
        created_files=created_files,
    )
    queue_backup_path = leaf / "distill_queue.db"
    queue_backup = _backup_database(
        database_dir / "distill_queue.db",
        queue_backup_path,
        backup_root=backup_root,
        created_files=created_files,
    )
    manifest = {
        "schema_version": "mnemos.capture_raw_backfill_backup.v1",
        "capture_raw_backfill_manifest_hash": str(plan["capture_raw_backfill_manifest_hash"]),
        "capture_raw_backfill_events": int(plan["capture_raw_backfill_events"]),
        "raw_events": raw_backup,
        "capture_queue": capture_backup,
        "distill_queue": queue_backup,
    }
    manifest_hash = _canonical_hash(manifest)
    manifest_path = leaf / "backup_manifest.json"
    _publish_owned_backup_text(
        backup_root=backup_root,
        root=leaf,
        relative_path=manifest_path.name,
        content=json.dumps(
            {**manifest, "manifest_hash": manifest_hash},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        created_files=created_files,
    )
    fsync_directory(leaf)
    validate_secure_created_file_receipts(
        backup_root,
        created_files,
    )
    return {
        **manifest,
        "manifest_path": str(manifest_path),
        "manifest_hash": manifest_hash,
        "manifest_file_hash": _file_sha256(manifest_path),
    }


def _backup_capture_raw_inventory(
    *,
    database_dir: Path,
    backup_dir: Path,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        backup_root = ensure_private_directory(backup_dir)
        leaf_name = f"capture-raw-backfill-{stamp}"
        creation_preimage = secure_create_directory(backup_root, leaf_name)
    except DurableIOError:
        raise RuntimeError("backup directory is unsafe") from None
    leaf = backup_root / leaf_name
    return _run_private_backup_leaf(
        backup_root,
        leaf,
        creation_preimage,
        lambda created_files, created_directories: _backup_capture_raw_inventory_in_leaf(
            database_dir=database_dir,
            backup_root=backup_root,
            leaf=leaf,
            plan=plan,
            created_files=created_files,
            created_directories=created_directories,
        ),
    )


def _mapping_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"capture payload {field} is invalid")
    return [dict(item) for item in value]


def _capture_raw_arguments(spec: Mapping[str, Any]) -> dict[str, Any]:
    payload = spec.get("capture_payload")
    if not isinstance(payload, Mapping):
        raise ValueError("capture raw backfill payload is invalid")
    if _canonical_hash(payload) != str(spec.get("capture_payload_hash") or ""):
        raise ValueError("capture raw backfill payload hash drifted")
    user = payload.get("user_content")
    assistant = payload.get("assistant_content")
    if not isinstance(user, str) or not isinstance(assistant, str) or not (user or assistant):
        raise ValueError("capture raw backfill visible payload is invalid")
    metadata_value = payload.get("metadata")
    completeness_value = payload.get("completeness")
    if metadata_value is not None and not isinstance(metadata_value, Mapping):
        raise ValueError("capture payload metadata is invalid")
    if completeness_value is not None and not isinstance(completeness_value, Mapping):
        raise ValueError("capture payload completeness is invalid")
    source_files_value = payload.get("source_files")
    if source_files_value is None:
        source_files: list[str] = []
    elif isinstance(source_files_value, list) and all(
        isinstance(value, str) for value in source_files_value
    ):
        source_files = list(source_files_value)
    else:
        raise ValueError("capture payload source_files is invalid")
    payload_hash = str(spec["capture_payload_hash"])
    event_id = int(spec["capture_event_id"])
    metadata = dict(metadata_value or {})
    metadata["native_event_id"] = (
        f"mnemos-capture-event-v1:{event_id}:{payload_hash.split(':')[-1][:16]}"
    )
    metadata["legacy_capture_reconciliation"] = {
        "schema_version": "mnemos.capture_raw_backfill.v1",
        "capture_event_id": event_id,
        "handoff_receipt_id": str(spec["handoff_receipt_id"]),
        "capture_payload_hash": payload_hash,
        "capture_created_at": str(spec["capture_created_at"]),
    }
    cwd = payload.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            raise ValueError("capture payload cwd is invalid")
        metadata["legacy_capture_cwd"] = cwd
    completeness = dict(completeness_value or {})
    completeness.setdefault("visible_text", "host_provided")
    reasoning = payload.get("reasoning")
    if reasoning is None:
        reasoning = ""
    if not isinstance(reasoning, str):
        raise ValueError("capture payload reasoning is invalid")
    timestamp = payload.get("timestamp")
    model = payload.get("model")
    if timestamp is not None and not isinstance(timestamp, str):
        raise ValueError("capture payload timestamp is invalid")
    if model is not None and not isinstance(model, str):
        raise ValueError("capture payload model is invalid")
    full_content_hash = str(metadata.get("full_content_hash") or spec["content_hash"])
    return {
        "source_agent": str(spec["source_agent"]),
        "session_id": str(spec["session_id"]),
        "turn_number": int(spec["turn_number"]),
        "user_content": user,
        "assistant_content": assistant,
        "model_tag": str(model or spec["source_agent"]),
        "timestamp": str(timestamp or spec["capture_created_at"]),
        "metadata": metadata,
        "tool_calls": _mapping_list(payload.get("tool_calls"), field="tool_calls"),
        "tool_results": _mapping_list(payload.get("tool_results"), field="tool_results"),
        "reasoning": reasoning,
        "attachments": _mapping_list(payload.get("attachments"), field="attachments"),
        "raw_event_refs": _mapping_list(payload.get("raw_event_refs"), field="raw_event_refs"),
        "source_files": source_files,
        "source_path": source_files[0] if source_files else None,
        "completeness": completeness,
        "content_hash": str(spec["content_hash"]),
        "full_content_hash": full_content_hash,
        "origin": "capture_service",
    }


def _apply_capture_raw_backfills(
    *,
    database_dir: Path,
    specs: list[Mapping[str, Any]],
) -> dict[str, int]:
    from core.sync_framework.raw_event_store import RawEventStore

    normalized = [(spec, _capture_raw_arguments(spec)) for spec in specs]
    store = RawEventStore(db_path=database_dir / "raw_events.db")
    revisions = 0
    edges = 0
    try:
        for spec, arguments in normalized:
            revision_id = store.upsert_turn(**arguments)
            replay = store.get_turn(revision_id)
            if (
                replay is None
                or str(replay.get("source_agent") or "") != str(spec["source_agent"])
                or str(replay.get("session_id") or "") != str(spec["session_id"])
                or int(replay.get("turn_number", -1)) != int(spec["turn_number"])
                or str(replay.get("content_hash") or "") != str(spec["content_hash"])
                or str(replay.get("user_content") or "") != str(arguments["user_content"])
                or str(replay.get("assistant_content") or "") != str(arguments["assistant_content"])
            ):
                raise RuntimeError("capture Raw replay verification failed")
            store.record_provenance_edge(
                source_revision_id=revision_id,
                span_start=0,
                span_end=len(str(arguments["user_content"]))
                + len(str(arguments["assistant_content"])),
                consumer_type="legacy_capture_event",
                consumer_id=str(spec["capture_event_id"]),
            )
            revisions += 1
            edges += 1
    finally:
        store.close()
    return {
        "raw_revisions_created_or_reused": revisions,
        "raw_provenance_edges": edges,
    }


def reconcile_capture_raw_backfills(
    config: Any,
    *,
    apply: bool,
    backup_dir: Path | None = None,
    expected_manifest_hash: str = "",
) -> dict[str, Any]:
    reviewed = build_source_span_reconciliation_plan(config)
    result: dict[str, Any] = {
        "schema_version": "mnemos.capture_raw_backfill_reconciliation.v1",
        "mode": "apply" if apply else "dry_run",
        "ok": False,
        "status": "blocked",
        "capture_raw_backfill_events": int(reviewed.get("capture_raw_backfill_events") or 0),
        "capture_raw_backfill_tasks": int(reviewed.get("capture_raw_backfill_tasks") or 0),
        "capture_raw_backfill_manifest_hash": str(
            reviewed.get("capture_raw_backfill_manifest_hash") or ""
        ),
        "blocked_by_reason": dict(reviewed.get("blocked_by_reason") or {}),
        "backup": None,
        "applied": {
            "raw_revisions_created_or_reused": 0,
            "raw_provenance_edges": 0,
        },
    }
    if reviewed.get("error"):
        result["error"] = reviewed["error"]
        return result
    blocker_set = set(result["blocked_by_reason"])
    if blocker_set.difference({"capture_raw_revision_missing"}):
        result["error"] = "non_capture_raw_blockers_present"
        return result
    if not result["capture_raw_backfill_events"]:
        result["ok"] = not blocker_set
        result["status"] = "noop" if result["ok"] else "blocked"
        return result
    if not apply:
        result["status"] = "review_required"
        return result
    if backup_dir is None or not expected_manifest_hash:
        result["error"] = "backup_directory_and_expected_manifest_hash_required"
        return result
    if expected_manifest_hash != result["capture_raw_backfill_manifest_hash"]:
        result["error"] = "capture_raw_backfill_manifest_hash_mismatch"
        return result
    database_dir = Path(config.database_dir)
    if not _runtime_writers_are_inactive(database_dir):
        result["error"] = "daemon_not_inactive"
        return result
    backup = _backup_capture_raw_inventory(
        database_dir=database_dir,
        backup_dir=Path(backup_dir),
        plan=reviewed,
    )
    result["backup"] = backup
    fresh = build_source_span_reconciliation_plan(config)
    if str(fresh.get("capture_raw_backfill_manifest_hash") or "") != expected_manifest_hash:
        result["error"] = "reviewed_capture_raw_inventory_drifted_after_backup"
        return result
    applied = _apply_capture_raw_backfills(
        database_dir=database_dir,
        specs=list(fresh.get("raw_backfills") or []),
    )
    result["applied"] = applied
    post = build_source_span_reconciliation_plan(config)
    integrity = _integrity(database_dir / "raw_events.db")
    result["integrity_check"] = {"raw_events": integrity}
    result["post"] = {
        key: post.get(key)
        for key in (
            "ok",
            "missing_span_tasks",
            "candidate_tasks",
            "blocked_by_reason",
            "capture_raw_backfill_events",
            "capture_raw_backfill_manifest_hash",
            "inventory_hash",
        )
    }
    result["ok"] = bool(
        post.get("ok")
        and int(post.get("capture_raw_backfill_events") or 0) == 0
        and integrity == "ok"
        and int(applied["raw_revisions_created_or_reused"])
        == int(reviewed["capture_raw_backfill_events"])
    )
    result["status"] = "verified" if result["ok"] else "failed"
    return result


def _task_by_id(queue_path: Path, task_id: str) -> dict[str, Any] | None:
    with _connect_read_only(queue_path) as conn:
        row = conn.execute(
            "SELECT * FROM distillation_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    payload = dict(row)
    try:
        meta = json.loads(str(payload.get("meta") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    payload["meta"] = dict(meta) if isinstance(meta, Mapping) else {}
    return payload


def _record_runtime_and_raw_receipts(
    config: Any,
    migrations: list[Mapping[str, str]],
) -> dict[str, int]:
    from core.ops.cognitive_pipeline_receipts import (
        record_distillation_generation_superseded,
        record_sync_handoff,
    )
    from core.sync_framework.raw_event_store import RawEventStore

    database_dir = Path(config.database_dir)
    queue_path = database_dir / "distill_queue.db"
    superseded_receipts = 0
    produced_receipts = 0
    raw_edges = 0
    raw_store = RawEventStore(db_path=database_dir / "raw_events.db")
    try:
        for migration in migrations:
            legacy = _task_by_id(queue_path, str(migration["legacy_task_id"]))
            canonical = _task_by_id(queue_path, str(migration["canonical_task_id"]))
            if legacy is None or canonical is None:
                raise RuntimeError("migrated task receipt is missing")
            superseded = record_distillation_generation_superseded(
                config,
                legacy_task=legacy,
                replacement_task_id=str(canonical["task_id"]),
            )
            if superseded.get("matched"):
                superseded_receipts += 1
            receipt = DistillationEnqueueReceipt(
                receipt_id=str(canonical.get("receipt_id") or ""),
                task_id=str(canonical["task_id"]),
                source_agent=str(canonical["source_agent"]),
                session_id=str(canonical["session_id"]),
                input_revision=str(canonical["input_revision"]),
                status=str(canonical["status"]),
                created=False,
            )
            record_sync_handoff(
                config,
                str(canonical["session_id"]),
                dict(canonical["meta"]),
                receipt,
            )
            produced_receipts += 1
            refs = canonical["meta"].get("raw_event_refs")
            if not isinstance(refs, list):
                raise RuntimeError("canonical source span task lacks Raw refs")
            for ref in refs:
                if not isinstance(ref, Mapping):
                    raise RuntimeError("canonical source span Raw ref is invalid")
                raw_store.record_provenance_edge(
                    source_revision_id=str(ref.get("revision_id") or ""),
                    span_start=int(ref.get("span_start") or 0),
                    span_end=int(ref.get("span_end") or 0),
                    consumer_type="amphora_task",
                    consumer_id=str(canonical["task_id"]),
                )
                raw_edges += 1
    finally:
        raw_store.close()
    return {
        "superseded_runtime_receipts": superseded_receipts,
        "replacement_runtime_productions": produced_receipts,
        "raw_provenance_edges": raw_edges,
    }


def _integrity(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def reconcile_source_spans(
    config: Any,
    *,
    apply: bool,
    backup_dir: Path | None = None,
    expected_inventory_hash: str = "",
) -> dict[str, Any]:
    """Inspect or apply the reviewed historical source-span migration."""

    reviewed = build_source_span_reconciliation_plan(config)
    result = dict(reviewed)
    result.pop("objects", None)
    result.pop("blocked_objects", None)
    result["mode"] = "apply" if apply else "dry_run"
    result["backup"] = None
    result["applied"] = {
        "legacy_tasks_retired": 0,
        "canonical_tasks_created": 0,
        "canonical_tasks_reused": 0,
    }
    if reviewed.get("error") or not apply:
        return result
    if backup_dir is None or not expected_inventory_hash:
        result["ok"] = False
        result["error"] = "backup_directory_and_expected_inventory_hash_required"
        return result
    if expected_inventory_hash != str(reviewed.get("inventory_hash") or ""):
        result["ok"] = False
        result["error"] = "inventory_hash_mismatch"
        return result
    if not reviewed.get("ok"):
        return result
    if int(reviewed.get("candidate_tasks") or 0) == 0:
        result["status"] = "noop"
        return result
    database_dir = Path(config.database_dir)
    if not _runtime_writers_are_inactive(database_dir):
        result["ok"] = False
        result["error"] = "daemon_not_inactive"
        return result
    backup = _backup_reviewed_inventory(
        database_dir=database_dir,
        backup_dir=Path(backup_dir),
        plan=reviewed,
    )
    result["backup"] = backup
    fresh = build_source_span_reconciliation_plan(config)
    if not fresh.get("ok") or fresh.get("inventory_hash") != expected_inventory_hash:
        result["ok"] = False
        result["error"] = "reviewed_inventory_drifted_after_backup"
        return result
    from core.kia.amphora_source_span_migration import apply_source_span_migrations

    applied = apply_source_span_migrations(
        objects=list(fresh["objects"]),
        inventory_hash=expected_inventory_hash,
        backup_manifest_path=Path(str(backup["manifest_path"])),
        backup_manifest_hash=str(backup["manifest_hash"]),
        backup_manifest_file_hash=str(backup["manifest_file_hash"]),
    )
    result["applied"] = {
        key: int(applied[key])
        for key in (
            "legacy_tasks_retired",
            "canonical_tasks_created",
            "canonical_tasks_reused",
        )
    }
    derived = _record_runtime_and_raw_receipts(config, list(applied["migrations"]))
    result["applied"].update(derived)
    post = build_source_span_reconciliation_plan(config)
    integrity = {
        "distill_queue": _integrity(database_dir / "distill_queue.db"),
        "raw_events": _integrity(database_dir / "raw_events.db"),
    }
    ledger_path = database_dir / "producer_consumer_ledger.db"
    if ledger_path.is_file():
        integrity["producer_consumer_ledger"] = _integrity(ledger_path)
    result["integrity_check"] = integrity
    result["post"] = {
        key: post.get(key)
        for key in (
            "ok",
            "missing_span_tasks",
            "candidate_tasks",
            "verified_migrations",
            "blocked_by_reason",
            "inventory_hash",
        )
    }
    result["ok"] = bool(
        post.get("ok")
        and int(post.get("missing_span_tasks") or 0) == 0
        and int(post.get("verified_migrations") or 0) >= int(reviewed.get("candidate_tasks") or 0)
        and all(value == "ok" for value in integrity.values())
    )
    result["status"] = "verified" if result["ok"] else "failed"
    return result


def _render(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep CLI output metadata-only and free of task/session identities."""

    rendered = dict(result)
    rendered.pop("objects", None)
    rendered.pop("blocked_objects", None)
    rendered.pop("raw_backfills", None)
    backup = rendered.get("backup")
    if isinstance(backup, Mapping):
        rendered["backup"] = {
            key: backup.get(key)
            for key in (
                "database",
                "producer_consumer_ledger",
                "messages_count",
                "messages_manifest_path",
                "raw_events",
                "capture_queue",
                "distill_queue",
                "manifest_path",
                "manifest_hash",
            )
            if key in backup
        }
    return rendered


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    apply_group = parser.add_mutually_exclusive_group()
    apply_group.add_argument("--apply", action="store_true")
    apply_group.add_argument("--apply-capture-raw", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expected-inventory-hash", default="")
    parser.add_argument("--expected-capture-raw-manifest-hash", default="")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from core.config import get_config

    config = get_config()
    if args.apply_capture_raw:
        result = reconcile_capture_raw_backfills(
            config,
            apply=True,
            backup_dir=args.backup_dir,
            expected_manifest_hash=str(args.expected_capture_raw_manifest_hash),
        )
    else:
        result = reconcile_source_spans(
            config,
            apply=bool(args.apply),
            backup_dir=args.backup_dir,
            expected_inventory_hash=str(args.expected_inventory_hash),
        )
    print(json.dumps(_render(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
