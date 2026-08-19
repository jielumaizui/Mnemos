#!/usr/bin/env python3
"""Bind an applied Wiki ACL batch to lifecycle mutations and durable events."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.access_policy import ACL_METADATA_KEYS  # noqa: E402
from core.cognitive.sources import SourceReader  # noqa: E402
from core.config import get_config  # noqa: E402
from core.event_bus_contract import _resolve_event_db_dir  # noqa: E402
from core.frontmatter import parse_frontmatter  # noqa: E402
from core.db_utils import render_sql  # noqa: E402
from core.migrations.model_call_ledger_reconcile.runtime import (  # noqa: E402
    runtime_writers_are_inactive,
)
from core.mnemos_bus import EventBus  # noqa: E402
from core.ops.offline_migration_lock import offline_migration_lock  # noqa: E402
from core.wiki_projection_lifecycle import (  # noqa: E402
    DEFAULT_REQUIRED_CONSUMERS,
    WikiMutationReceipt,
    WikiProjectionLedger,
)
from core.wiki_projection_publisher import publish_wiki_mutation  # noqa: E402


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes()) if path.is_file() else ""


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _normalize_acl_backup(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=True)
    nested = candidate / "wiki"
    return nested if nested.is_dir() else candidate


def _relative_markdown(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(root.rglob("*.md"))
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    }


def _ledger_rows(ledger_path: Path, wiki_dir: Path) -> dict[str, sqlite3.Row]:
    rows: dict[str, sqlite3.Row] = {}
    with _connect_read_only(ledger_path) as connection:
        for row in connection.execute(
            "SELECT page_id,current_path,current_revision,content_sha256,lifecycle_state "
            "FROM wiki_pages WHERE lifecycle_state='active'"
        ):
            path = Path(str(row["current_path"])).resolve(strict=False)
            try:
                relative = path.relative_to(wiki_dir).as_posix()
            except ValueError:
                continue
            rows[relative] = row
    return rows


def _plan_details(
    *,
    wiki_dir: Path,
    ledger_path: Path,
    acl_backup_dir: Path,
    predecessor_backup_dirs: Iterable[Path] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    wiki_dir = wiki_dir.expanduser().resolve(strict=True)
    acl_root = _normalize_acl_backup(acl_backup_dir)
    predecessor_roots = [_normalize_acl_backup(path) for path in predecessor_backup_dirs]
    current_files = _relative_markdown(wiki_dir)
    backup_files = _relative_markdown(acl_root)
    ledger = _ledger_rows(ledger_path, wiki_dir)
    details: list[dict[str, Any]] = []
    classifications: Counter[str] = Counter()
    changed_keys: Counter[str] = Counter()
    unresolved: list[str] = []
    # ACL reconciliation backs up only the files in the reviewed mutation
    # batch.  Treat that exact backup manifest as the denominator; scanning the
    # full current Wiki here would turn every unchanged page into a false gap.
    for relative in sorted(backup_files):
        current = current_files.get(relative)
        backup = backup_files.get(relative)
        row = ledger.get(relative)
        if current is None or backup is None or row is None:
            unresolved.append(relative)
            continue
        current_bytes = current.read_bytes()
        backup_bytes = backup.read_bytes()
        current_fm, current_body = parse_frontmatter(current_bytes.decode("utf-8"))
        backup_fm, backup_body = parse_frontmatter(backup_bytes.decode("utf-8"))
        current_fm = current_fm or {}
        backup_fm = backup_fm or {}
        keys = sorted(
            key
            for key in set(current_fm) | set(backup_fm)
            if current_fm.get(key) != backup_fm.get(key)
        )
        changed_keys.update(keys)
        invalid_keys = sorted(set(keys) - ACL_METADATA_KEYS)
        body_equal = current_body == backup_body
        ledger_hash = str(row["content_sha256"])
        backup_hash = hashlib.sha256(backup_bytes).hexdigest()
        current_hash = hashlib.sha256(current_bytes).hexdigest()
        if current_hash == ledger_hash:
            classifications["already_current"] += 1
            if invalid_keys or not body_equal:
                unresolved.append(relative)
            continue
        classification = "acl_backup_matches_ledger"
        if ledger_hash != backup_hash:
            classification = "preexisting_drift_unmatched"
            for predecessor_root in predecessor_roots:
                predecessor = predecessor_root / relative
                if (
                    predecessor.is_file()
                    and hashlib.sha256(predecessor.read_bytes()).hexdigest() == ledger_hash
                ):
                    classification = "predecessor_backup_matches_ledger"
                    break
        system_projection = Path(relative).parts[0] in SourceReader.SYSTEM_DIRS
        if (
            invalid_keys
            or not body_equal
            or (classification == "preexisting_drift_unmatched" and not system_projection)
        ):
            unresolved.append(relative)
        classifications[classification] += 1
        details.append(
            {
                "relative_path": relative,
                "page_id": str(row["page_id"]),
                "ledger_revision": str(row["current_revision"]),
                "ledger_sha256": ledger_hash,
                "acl_backup_sha256": backup_hash,
                "current_sha256": current_hash,
                "semantic_body_sha256": _sha256_bytes(current_body.encode("utf-8")),
                "changed_keys": keys,
                "classification": classification,
                "system_generated_projection": system_projection,
            }
        )
    payload: dict[str, Any] = {
        "schema_version": "mnemos.wiki_acl_projection_reconciliation.v1",
        "wiki_page_count": len(current_files),
        "acl_backup_page_count": len(backup_files),
        "ledger_active_page_count": len(ledger),
        "needs_mutation_count": len(details),
        "classification_counts": dict(sorted(classifications.items())),
        "changed_key_counts": dict(sorted(changed_keys.items())),
        "unresolved_count": len(set(unresolved)),
        "details_manifest_sha256": _sha256_json(details),
        "acl_backup_manifest_sha256": _sha256_json(
            [(relative, _sha256_path(path)) for relative, path in sorted(backup_files.items())]
        ),
        "predecessor_backup_manifest_sha256": _sha256_json(
            [
                (
                    str(index),
                    _sha256_json(
                        [
                            (relative, _sha256_path(path))
                            for relative, path in sorted(_relative_markdown(root).items())
                        ]
                    ),
                )
                for index, root in enumerate(predecessor_roots)
            ]
        ),
    }
    payload["plan_hash"] = _sha256_json(payload)
    return payload, details


def _sqlite_snapshot(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "integrity": "missing", "tables": {}}
    with _connect_read_only(path) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts: dict[str, int] = {}
        for table in tables:
            query = render_sql(
                "SELECT COUNT(*) FROM {table}",
                identifiers={"table": table},
            )
            counts[table] = int(
                connection.execute(query).fetchone()[0]
            )
    return {"exists": True, "integrity": integrity, "tables": counts}


def _backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_file():
        return {"existed": False, "snapshot": _sqlite_snapshot(source)}
    with (
        _connect_read_only(source) as source_connection,
        sqlite3.connect(str(destination), timeout=60) as backup_connection,
    ):
        source_connection.backup(backup_connection)
    snapshot = _sqlite_snapshot(destination)
    if snapshot["integrity"] != "ok":
        raise RuntimeError(f"SQLite backup integrity failed: {source.name}")
    return {
        "existed": True,
        "backup_file": destination.name,
        "backup_sha256": _sha256_path(destination),
        "snapshot": snapshot,
    }


def _restore_sqlite(target: Path, backup_dir: Path, record: dict[str, Any]) -> None:
    if not bool(record["existed"]):
        target.unlink(missing_ok=True)
        Path(f"{target}-wal").unlink(missing_ok=True)
        Path(f"{target}-shm").unlink(missing_ok=True)
        return
    backup = backup_dir / str(record["backup_file"])
    if not backup.is_file():
        raise RuntimeError(f"SQLite rollback backup is missing: {backup.name}")
    if _sha256_path(backup) != str(record.get("backup_sha256") or ""):
        raise RuntimeError(f"SQLite rollback backup hash mismatch: {backup.name}")
    if _sqlite_snapshot(backup) != record.get("snapshot"):
        raise RuntimeError(f"SQLite rollback backup snapshot mismatch: {backup.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.restore-",
        suffix=".db",
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        with (
            _connect_read_only(backup) as source_connection,
            sqlite3.connect(str(temporary_path), timeout=60) as target_connection,
        ):
            source_connection.backup(target_connection)
            target_connection.commit()
        if _sqlite_snapshot(temporary_path) != record["snapshot"]:
            raise RuntimeError(f"SQLite rollback temporary snapshot mismatch: {target.name}")
        Path(f"{target}-wal").unlink(missing_ok=True)
        Path(f"{target}-shm").unlink(missing_ok=True)
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    if _sqlite_snapshot(target) != record["snapshot"]:
        raise RuntimeError(f"SQLite rollback snapshot mismatch: {target.name}")


def recover_wiki_projection_databases(
    *,
    config: Any,
    backup_dir: Path,
    manifest_name: str,
) -> dict[str, Any]:
    """Roll back one projection batch under the full writer exclusion lock."""

    with offline_migration_lock(
        Path(config.database_dir),
        daemon_check=runtime_writers_are_inactive,
    ):
        return _recover_wiki_projection_databases_unlocked(
            config=config,
            backup_dir=backup_dir,
            manifest_name=manifest_name,
        )


def _recover_wiki_projection_databases_unlocked(
    *,
    config: Any,
    backup_dir: Path,
    manifest_name: str,
) -> dict[str, Any]:
    """Roll back a prepared projection batch after process death.

    A top-level Markdown reconciliation manifest is the authority for invoking
    this helper; a fully committed top-level operation is not recoverable here.
    """

    backup_dir = Path(backup_dir).expanduser().resolve(strict=True)
    if Path(manifest_name).name != manifest_name:
        raise ValueError("projection recovery manifest must be a basename")
    manifest_path = backup_dir / manifest_name
    if not manifest_path.is_file():
        entries = {path.name for path in backup_dir.iterdir()}
        allowed_preparation_artifacts = {"wiki_projection.db", "events.db"}
        allowed_preparation_artifacts.update(
            name
            for name in entries
            if name.startswith(f".{manifest_name}.") and name.endswith(".tmp")
        )
        if entries <= allowed_preparation_artifacts:
            return {
                "found": bool(entries),
                "status": ("backup_preparing_no_database_mutation" if entries else "absent"),
            }
        raise RuntimeError("projection recovery backup lacks its prepared manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="strict"))
    allowed_manifests = {
        "wiki-projection-batch-manifest.json": ("mnemos.wiki_projection_exact_batch_manifest.v1"),
        "wiki-acl-projection-reconciliation-manifest.json": (
            "mnemos.wiki_acl_projection_reconciliation_manifest.v1"
        ),
    }
    expected_schema = allowed_manifests.get(manifest_name)
    if expected_schema is None or manifest.get("schema_version") != expected_schema:
        raise RuntimeError("projection recovery manifest schema is unsupported")
    status = str(manifest.get("status") or "")
    if status == "backup_preparing":
        manifest.update({"status": "recovered_before_database_mutation"})
        _atomic_json(manifest_path, manifest)
        return {"found": True, "status": "recovered_before_database_mutation"}
    if status in {"rolled_back", "recovered_rollback"}:
        return {"found": True, "status": status}
    if status not in {"prepared", "committed", "rollback_failed"}:
        raise RuntimeError(f"projection recovery status is unsupported: {status}")
    backup = manifest.get("backup")
    if not isinstance(backup, dict) or set(backup) != {"wiki_projection.db", "events.db"}:
        raise RuntimeError("projection recovery manifest has an invalid backup set")
    database_dir = Path(config.database_dir).expanduser().resolve(strict=True)
    ledger_path = database_dir / "wiki_projection.db"
    events_path = _resolve_event_db_dir(config) / "events.db"
    _restore_sqlite(ledger_path, backup_dir, dict(backup["wiki_projection.db"]))
    _restore_sqlite(events_path, backup_dir, dict(backup["events.db"]))
    manifest.update(
        {
            "status": "recovered_rollback",
            "recovery": {
                "wiki_projection.db": _sqlite_snapshot(ledger_path),
                "events.db": _sqlite_snapshot(events_path),
            },
        }
    )
    _atomic_json(manifest_path, manifest)
    return {"found": True, "status": "recovered_rollback"}


def _gap_mutation_count(ledger_path: Path) -> int:
    values = ",".join("(?)" for _ in DEFAULT_REQUIRED_CONSUMERS)
    sql = f"""
        WITH required(consumer) AS (VALUES {values})
        SELECT COUNT(DISTINCT mutation.mutation_id)
        FROM wiki_mutations AS mutation
        CROSS JOIN required
        LEFT JOIN projection_receipts AS receipt
          ON receipt.mutation_id=mutation.mutation_id
         AND receipt.consumer=required.consumer
         AND receipt.outcome IN ('ack','noop')
        WHERE receipt.mutation_id IS NULL
    """  # nosec B608
    with _connect_read_only(ledger_path) as connection:
        return int(connection.execute(sql, DEFAULT_REQUIRED_CONSUMERS).fetchone()[0])


def _validate_published_events(
    *,
    events_path: Path,
    mutations: list[dict[str, Any]],
    source: str = "cog015_acl_projection_reconciliation",
) -> dict[str, int]:
    expected = {str(item["mutation_id"]): item for item in mutations}
    actual: dict[str, sqlite3.Row] = {}
    trace_ids = sorted(expected)
    with _connect_read_only(events_path) as connection:
        for offset in range(0, len(trace_ids), 500):
            chunk = trace_ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in connection.execute(
                "SELECT trace_id,event_type,source,payload_json,status FROM events "
                f"WHERE trace_id IN ({placeholders})",  # nosec B608
                chunk,
            ):
                actual[str(row["trace_id"])] = row
    mismatch = 0
    for trace_id, mutation in expected.items():
        row = actual.get(trace_id)
        if row is None:
            mismatch += 1
            continue
        payload = json.loads(str(row["payload_json"]))
        if (
            row["event_type"] != "wiki_page_updated"
            or row["source"] != source
            or row["status"] != "pending"
            or payload.get("mutation_id") != mutation["mutation_id"]
            or payload.get("page_revision") != mutation["page_revision"]
        ):
            mismatch += 1
    return {
        "expected": len(expected),
        "found": len(actual),
        "mismatch": mismatch,
    }


@dataclass(frozen=True)
class ExactWikiProjectionUpdate:
    """One already-materialized Wiki update bound to its ledger preimage."""

    path: Path
    before_sha256: str
    after_sha256: str
    page_id: str = ""
    before_revision: str = ""


def _plain_sha256(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("Wiki projection update requires an exact SHA-256 digest")
    return normalized


def commit_exact_wiki_updates(
    *,
    config: Any,
    updates: Iterable[ExactWikiProjectionUpdate],
    backup_dir: Path,
    source: str,
    manifest_name: str = "wiki-projection-batch-manifest.json",
) -> dict[str, Any]:
    """Commit one exact projection batch under the full writer exclusion lock."""

    with offline_migration_lock(
        Path(config.database_dir),
        daemon_check=runtime_writers_are_inactive,
    ):
        return _commit_exact_wiki_updates_unlocked(
            config=config,
            updates=updates,
            backup_dir=backup_dir,
            source=source,
            manifest_name=manifest_name,
        )


def _commit_exact_wiki_updates_unlocked(
    *,
    config: Any,
    updates: Iterable[ExactWikiProjectionUpdate],
    backup_dir: Path,
    source: str,
    manifest_name: str = "wiki-projection-batch-manifest.json",
) -> dict[str, Any]:
    """Atomically bind an exact materialized Wiki batch to lifecycle/events.

    The caller owns the Markdown preimage backup and restores those files when
    this function raises.  This function independently restores both SQLite
    stores before propagating an error, so no failed batch can leave only one
    truth surface advanced.
    """

    normalized_source = str(source).strip()
    if not normalized_source or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789_:-" for char in normalized_source
    ):
        raise ValueError("Wiki projection source must be a stable lowercase identifier")
    if Path(manifest_name).name != manifest_name or not manifest_name.endswith(".json"):
        raise ValueError("Wiki projection manifest_name must be a JSON basename")

    wiki_dir = Path(config.wiki_dir).expanduser().resolve(strict=True)
    database_dir = Path(config.database_dir).expanduser().resolve(strict=True)
    ledger_path = database_dir / "wiki_projection.db"
    events_path = _resolve_event_db_dir(config) / "events.db"
    prepared: list[ExactWikiProjectionUpdate] = []
    seen_paths: set[Path] = set()
    for candidate in updates:
        path = Path(candidate.path).expanduser().resolve(strict=True)
        if not path.is_relative_to(wiki_dir):
            raise ValueError(f"Wiki projection update escaped the configured Wiki: {path}")
        if path in seen_paths:
            raise ValueError(f"duplicate Wiki projection update path: {path}")
        seen_paths.add(path)
        before_sha256 = _plain_sha256(candidate.before_sha256)
        after_sha256 = _plain_sha256(candidate.after_sha256)
        if before_sha256 == after_sha256:
            raise ValueError(f"Wiki projection update is a no-op: {path}")
        prepared.append(
            ExactWikiProjectionUpdate(
                path=path,
                before_sha256=before_sha256,
                after_sha256=after_sha256,
                page_id=str(candidate.page_id),
                before_revision=str(candidate.before_revision),
            )
        )
    prepared.sort(key=lambda item: item.path.parts)
    if not prepared:
        raise ValueError("Wiki projection commit requires at least one exact update")

    ledger_rows = _ledger_rows(ledger_path, wiki_dir)
    exact_plan: list[dict[str, str]] = []
    for item in prepared:
        relative = item.path.relative_to(wiki_dir).as_posix()
        row = ledger_rows.get(relative)
        if row is None:
            raise RuntimeError(f"Wiki lifecycle preimage is missing: {relative}")
        actual_page_id = str(row["page_id"])
        actual_revision = str(row["current_revision"])
        actual_before = _plain_sha256(str(row["content_sha256"]))
        actual_after = hashlib.sha256(item.path.read_bytes()).hexdigest()
        if item.page_id and item.page_id != actual_page_id:
            raise RuntimeError(f"Wiki lifecycle page identity drifted: {relative}")
        if item.before_revision and item.before_revision != actual_revision:
            raise RuntimeError(f"Wiki lifecycle revision drifted: {relative}")
        if item.before_sha256 != actual_before:
            raise RuntimeError(f"Wiki lifecycle content preimage drifted: {relative}")
        if item.after_sha256 != actual_after:
            raise RuntimeError(f"Wiki materialized content drifted: {relative}")
        exact_plan.append(
            {
                "relative_path": relative,
                "page_id": actual_page_id,
                "before_revision": actual_revision,
                "before_sha256": actual_before,
                "after_sha256": actual_after,
            }
        )

    backup_dir = Path(backup_dir).expanduser()
    if backup_dir.exists() and (not backup_dir.is_dir() or any(backup_dir.iterdir())):
        raise ValueError("backup directory must not exist or must be empty")
    backup_resolved = backup_dir.resolve(strict=False)
    if (
        backup_resolved == wiki_dir
        or backup_resolved.is_relative_to(wiki_dir)
        or backup_resolved == database_dir
        or backup_resolved.is_relative_to(database_dir)
    ):
        raise ValueError("projection backup directory must be outside Wiki and database roots")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    gap_before = _gap_mutation_count(ledger_path)
    plan_hash = _sha256_json(exact_plan)
    manifest_path = backup_dir / manifest_name
    manifest: dict[str, Any] = {
        "schema_version": "mnemos.wiki_projection_exact_batch_manifest.v1",
        "status": "backup_preparing",
        "source": normalized_source,
        "plan_hash": plan_hash,
        "updates": exact_plan,
        "backup": {},
        "historical_gap_mutations_before": gap_before,
    }
    _atomic_json(manifest_path, manifest)
    backup = {
        "wiki_projection.db": _backup_sqlite(
            ledger_path,
            backup_dir / "wiki_projection.db",
        ),
        "events.db": _backup_sqlite(events_path, backup_dir / "events.db"),
    }
    manifest.update({"status": "prepared", "backup": backup})
    _atomic_json(manifest_path, manifest)

    bus: EventBus | None = None
    diagnostics: dict[str, Any] = {}
    try:
        ledger = WikiProjectionLedger(ledger_path)
        receipts: list[dict[str, Any]] = []
        bus = EventBus(
            config=config,
            run_startup_maintenance=False,
            recover_pending=False,
            enqueue_published_events=False,
        )
        for item, expected in zip(prepared, exact_plan, strict=True):
            receipt = ledger.record_mutation(item.path, mutation_type="update")
            if (
                receipt.page_id != expected["page_id"]
                or receipt.parent_revision != expected["before_revision"]
                or _plain_sha256(receipt.content_sha256) != expected["after_sha256"]
            ):
                raise RuntimeError(f"Wiki lifecycle mutation diverged from exact plan: {item.path}")
            publish_wiki_mutation(
                receipt,
                ledger=ledger,
                source=normalized_source,
                event_bus=bus,
            )
            receipts.append(receipt.to_dict())
        bus.close()
        bus = None

        event_validation = _validate_published_events(
            events_path=events_path,
            mutations=receipts,
            source=normalized_source,
        )
        after_rows = _ledger_rows(ledger_path, wiki_dir)
        lifecycle_mismatch_count = 0
        for expected in exact_plan:
            row = after_rows.get(expected["relative_path"])
            if (
                row is None
                or str(row["page_id"]) != expected["page_id"]
                or _plain_sha256(str(row["content_sha256"])) != expected["after_sha256"]
                or str(row["lifecycle_state"]) != "active"
            ):
                lifecycle_mismatch_count += 1
        gap_after = _gap_mutation_count(ledger_path)
        diagnostics = {
            "update_count": len(exact_plan),
            "recorded_mutations": len(receipts),
            "published_events": event_validation["found"],
            "event_validation": event_validation,
            "lifecycle_mismatch_count": lifecycle_mismatch_count,
            "historical_gap_mutations_after": gap_after,
            "new_pending_required_consumer_receipts": (
                len(receipts) * len(DEFAULT_REQUIRED_CONSUMERS)
            ),
        }
        if (
            event_validation["mismatch"]
            or event_validation["found"] != len(exact_plan)
            or lifecycle_mismatch_count
        ):
            raise RuntimeError("exact Wiki lifecycle/event batch did not converge")
        manifest.update({"status": "committed", "diagnostics": diagnostics})
        _atomic_json(manifest_path, manifest)
        return {
            "ok": True,
            "plan_hash": plan_hash,
            "manifest": str(manifest_path),
            "diagnostics": diagnostics,
        }
    except BaseException as exc:
        if bus is not None:
            bus.close()
        _restore_sqlite(ledger_path, backup_dir, backup["wiki_projection.db"])
        _restore_sqlite(events_path, backup_dir, backup["events.db"])
        manifest.update(
            {
                "status": "rolled_back",
                "failure": f"{type(exc).__name__}: {exc}",
                "diagnostics": diagnostics,
                "restored": {
                    "wiki_projection.db": _sqlite_snapshot(ledger_path),
                    "events.db": _sqlite_snapshot(events_path),
                },
            }
        )
        _atomic_json(manifest_path, manifest)
        raise


def reconcile(
    *,
    apply: bool,
    acl_backup_dir: Path,
    predecessor_backup_dirs: Iterable[Path] = (),
    backup_dir: Path | None = None,
    reviewed_plan_hash: str = "",
    config: Any | None = None,
) -> dict[str, Any]:
    config = config or get_config()
    if not apply:
        return _reconcile_unlocked(
            apply=False,
            acl_backup_dir=acl_backup_dir,
            predecessor_backup_dirs=predecessor_backup_dirs,
            backup_dir=backup_dir,
            reviewed_plan_hash=reviewed_plan_hash,
            config=config,
        )
    with offline_migration_lock(
        Path(config.database_dir),
        daemon_check=runtime_writers_are_inactive,
    ):
        return _reconcile_unlocked(
            apply=True,
            acl_backup_dir=acl_backup_dir,
            predecessor_backup_dirs=predecessor_backup_dirs,
            backup_dir=backup_dir,
            reviewed_plan_hash=reviewed_plan_hash,
            config=config,
        )


def _reconcile_unlocked(
    *,
    apply: bool,
    acl_backup_dir: Path,
    predecessor_backup_dirs: Iterable[Path] = (),
    backup_dir: Path | None = None,
    reviewed_plan_hash: str = "",
    config: Any | None = None,
) -> dict[str, Any]:
    assert config is not None
    wiki_dir = Path(config.wiki_dir).expanduser().resolve(strict=True)
    database_dir = Path(config.database_dir).expanduser()
    ledger_path = database_dir / "wiki_projection.db"
    events_path = _resolve_event_db_dir(config) / "events.db"
    plan, details = _plan_details(
        wiki_dir=wiki_dir,
        ledger_path=ledger_path,
        acl_backup_dir=acl_backup_dir,
        predecessor_backup_dirs=predecessor_backup_dirs,
    )
    report: dict[str, Any] = {
        "schema_version": "mnemos.wiki_acl_projection_reconciliation_run.v1",
        "mode": "apply" if apply else "dry_run",
        "plan": plan,
        "applied": False,
        "ok": plan["unresolved_count"] == 0,
    }
    if not apply:
        return report
    if backup_dir is None:
        raise ValueError("apply requires an explicit backup directory")
    if reviewed_plan_hash != plan["plan_hash"]:
        raise ValueError("reviewed plan hash does not match current projection plan")
    if plan["unresolved_count"]:
        raise RuntimeError("projection plan contains unresolved Wiki pages")
    if backup_dir.exists() and (not backup_dir.is_dir() or any(backup_dir.iterdir())):
        raise ValueError("backup directory must not exist or must be empty")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    gap_before = _gap_mutation_count(ledger_path)
    manifest_path = backup_dir / "wiki-acl-projection-reconciliation-manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": "mnemos.wiki_acl_projection_reconciliation_manifest.v1",
        "status": "backup_preparing",
        "plan": plan,
        "backup": {},
        "historical_gap_mutations_before": gap_before,
    }
    _atomic_json(manifest_path, manifest)
    backup = {
        "wiki_projection.db": _backup_sqlite(
            ledger_path,
            backup_dir / "wiki_projection.db",
        ),
        "events.db": _backup_sqlite(events_path, backup_dir / "events.db"),
    }
    manifest.update({"status": "prepared", "backup": backup})
    _atomic_json(manifest_path, manifest)
    bus: EventBus | None = None
    diagnostics: dict[str, Any] = {}
    try:
        ledger = WikiProjectionLedger(ledger_path)
        scan = ledger.reconcile_vault(wiki_dir)
        expected_paths = {str(wiki_dir / item["relative_path"]) for item in details}
        actual_paths = {str(item["page_path"]) for item in scan["mutations"]}
        if (
            int(scan["recorded_mutations"]) != int(plan["needs_mutation_count"])
            or actual_paths != expected_paths
            or scan["counts"]
            != {
                "create": 0,
                "update": int(plan["needs_mutation_count"]),
                "move": 0,
                "delete": 0,
            }
        ):
            raise RuntimeError("lifecycle scan diverged from the reviewed ACL projection plan")
        bus = EventBus(
            config=config,
            run_startup_maintenance=False,
            recover_pending=False,
            enqueue_published_events=False,
        )
        for mutation in scan["mutations"]:
            receipt = WikiMutationReceipt(**mutation)
            publish_wiki_mutation(
                receipt,
                ledger=ledger,
                source="cog015_acl_projection_reconciliation",
                event_bus=bus,
            )
        bus.close()
        bus = None
        event_validation = _validate_published_events(
            events_path=events_path,
            mutations=scan["mutations"],
        )
        after_plan, _after_details = _plan_details(
            wiki_dir=wiki_dir,
            ledger_path=ledger_path,
            acl_backup_dir=acl_backup_dir,
            predecessor_backup_dirs=predecessor_backup_dirs,
        )
        gap_after = _gap_mutation_count(ledger_path)
        diagnostics = {
            "scan": {key: value for key, value in scan.items() if key != "mutations"},
            "event_validation": event_validation,
            "after_plan": after_plan,
            "historical_gap_mutations_after": gap_after,
            "new_pending_required_consumer_receipts": (
                int(scan["recorded_mutations"]) * len(DEFAULT_REQUIRED_CONSUMERS)
            ),
        }
        if (
            event_validation["mismatch"]
            or event_validation["found"] != event_validation["expected"]
            or after_plan["needs_mutation_count"] != 0
        ):
            raise RuntimeError("lifecycle/event reconciliation did not converge")
        manifest.update({"status": "committed", "diagnostics": diagnostics})
        _atomic_json(manifest_path, manifest)
        report.update(
            {
                "applied": True,
                "ok": True,
                "backup_dir": str(backup_dir),
                "manifest": str(manifest_path),
                "diagnostics": diagnostics,
            }
        )
        return report
    except BaseException as exc:
        if bus is not None:
            bus.close()
        _restore_sqlite(ledger_path, backup_dir, backup["wiki_projection.db"])
        _restore_sqlite(events_path, backup_dir, backup["events.db"])
        manifest.update(
            {
                "status": "rolled_back",
                "failure": f"{type(exc).__name__}: {exc}",
                "diagnostics": diagnostics,
                "restored": {
                    "wiki_projection.db": _sqlite_snapshot(ledger_path),
                    "events.db": _sqlite_snapshot(events_path),
                },
            }
        )
        _atomic_json(manifest_path, manifest)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--acl-backup-dir", type=Path, required=True)
    parser.add_argument("--predecessor-backup-dir", type=Path, action="append", default=[])
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--reviewed-plan-hash", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = reconcile(
            apply=args.apply,
            acl_backup_dir=args.acl_backup_dir,
            predecessor_backup_dirs=args.predecessor_backup_dir,
            backup_dir=args.backup_dir,
            reviewed_plan_hash=args.reviewed_plan_hash,
        )
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        result = {
            "schema_version": "mnemos.wiki_acl_projection_reconciliation_run.v1",
            "mode": "apply" if args.apply else "dry_run",
            "applied": False,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(result["error"], file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(
            "Wiki ACL projection reconciliation: "
            f"mode={result['mode']} ok={result['ok']} "
            f"plan={result['plan']['plan_hash']}"
        )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
