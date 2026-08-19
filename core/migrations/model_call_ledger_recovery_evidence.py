"""Private evidence, sealing, and locking primitives for ledger recovery.

The reconciler owns the one-way privacy/database mutation.  This module owns
the *separate* recovery contract around a successful reconciliation: it seals
the verified preimage backups, binds them to the reviewed execution receipt and
the migration-ledger row, and performs an explicitly requested restore.  It
never reads or serializes prompt/response content; only opaque file hashes and
safe SQLite integrity outcomes are persisted.

The manifest is deliberately immutable.  Progress is append-only JSONL with a
hash chain, so a failed restore is visible as a partial operation rather than a
false green ``rolled_back`` result.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from core.runtime_paths import RuntimePaths


RECOVERY_SCHEMA_VERSION = "mnemos.model_call_ledger_recovery.v3"
RECOVERY_PROGRESS_SCHEMA_VERSION = "mnemos.model_call_ledger_recovery_progress.v1"
MODEL_CALL_LEDGER_MIGRATION_ID = "database.model_call_ledger.v1"

# These are fixed runtime owners.  Manifest entries contain only their stable
# IDs, never arbitrary paths supplied by a caller.
_TARGETS: tuple[tuple[str, str], ...] = (
    ("canonical_ledger", "model_call_ledger.db"),
    ("legacy_wiki_state", "wiki_state.db"),
    ("legacy_prompt_calls", "prompt_calls.db"),
    ("legacy_sync_log", "sync_log.db"),
)
_TARGET_FILENAMES = dict(_TARGETS)
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_RECOVERY_PREFIX = "model-call-ledger-recovery-v3-"


class ModelCallLedgerRecoveryError(RuntimeError):
    """A fail-closed recovery contract violation with a safe error code."""


_RECOVERABLE_ERRORS = (
    ModelCallLedgerRecoveryError,
    OSError,
    sqlite3.Error,
    ValueError,
    TypeError,
    ImportError,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash_payload(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _clean_error(exc: BaseException) -> str:
    if isinstance(exc, ModelCallLedgerRecoveryError):
        code = str(exc)
        if re.fullmatch(r"(?:recovery|legacy)_[a-z0-9_]{2,120}", code):
            return code
        if code == "model_call_ledger_migration_lock_unavailable":
            return code
        return "recovery_contract_failed"
    if isinstance(exc, sqlite3.Error):
        return "recovery_sqlite_error"
    if isinstance(exc, OSError):
        return "recovery_io_error"
    if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
        return "recovery_validation_error"
    return "recovery_contract_failed"


def _mnemos_dir(config: Any) -> Path:
    value = getattr(config, "mnemos_dir", None) or getattr(config, "data_dir", None)
    return Path(value) if value is not None else Path.home() / ".mnemos"


def _lstat_directory(path: Path, *, private: bool) -> os.stat_result:
    """Validate every existing component, not just the final directory.

    A final ``lstat`` alone misses ``runtime/link/db``: the final ``db`` can be
    a normal directory even though an intermediate component redirects a
    high-impact restore outside the configured owner tree.  Recovery is strict
    about every component, including the configured root itself.
    """
    supplied = Path(path).expanduser()
    if ".." in supplied.parts:
        raise ModelCallLedgerRecoveryError("recovery_directory_parent_escape")
    path = Path(os.path.abspath(str(supplied)))
    parts = path.parts
    cursor = Path(parts[0]) if parts else path
    try:
        root_metadata = cursor.lstat()
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_directory_uninspectable") from exc
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ModelCallLedgerRecoveryError("recovery_directory_not_safe")
    metadata = root_metadata
    for component in parts[1:]:
        cursor = cursor / component
        try:
            metadata = cursor.lstat()
        except FileNotFoundError as exc:
            raise ModelCallLedgerRecoveryError("recovery_directory_missing") from exc
        except OSError as exc:
            raise ModelCallLedgerRecoveryError("recovery_directory_uninspectable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ModelCallLedgerRecoveryError("recovery_directory_not_safe")
    # ``cursor`` is now exactly ``path`` and all its parents were checked.
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ModelCallLedgerRecoveryError("recovery_directory_missing") from exc
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_directory_uninspectable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ModelCallLedgerRecoveryError("recovery_directory_not_safe")
    if private:
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ModelCallLedgerRecoveryError("recovery_directory_permissions_invalid")
        if metadata.st_uid != os.getuid():
            raise ModelCallLedgerRecoveryError("recovery_directory_owner_invalid")
    return metadata


def _lstat_regular(path: Path, *, private: bool) -> os.stat_result:
    _lstat_directory(path.parent, private=False)
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ModelCallLedgerRecoveryError("recovery_file_missing") from exc
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_file_uninspectable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ModelCallLedgerRecoveryError("recovery_file_not_regular")
    if private:
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ModelCallLedgerRecoveryError("recovery_file_permissions_invalid")
        if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            raise ModelCallLedgerRecoveryError("recovery_file_owner_or_link_invalid")
    return metadata


def _no_follow_open(path: Path, flags: int, mode: int = 0o600) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    return os.open(str(path), flags | nofollow | cloexec, mode)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(
            str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_directory_fsync_open_failed") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_directory_fsync_failed") from exc
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise ModelCallLedgerRecoveryError("recovery_file_write_failed")
        offset += written


def _read_private_bytes(path: Path) -> bytes:
    before = _lstat_regular(path, private=True)
    try:
        descriptor = _no_follow_open(path, os.O_RDONLY)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_file_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ModelCallLedgerRecoveryError("recovery_file_replaced_during_read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise ModelCallLedgerRecoveryError("recovery_file_changed_during_read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _file_identity(path: Path, *, private: bool) -> dict[str, Any]:
    """Hash a regular file without exposing its content to output objects."""
    before = _lstat_regular(path, private=private)
    try:
        descriptor = _no_follow_open(path, os.O_RDONLY)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_file_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ModelCallLedgerRecoveryError("recovery_file_replaced_during_hash")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise ModelCallLedgerRecoveryError("recovery_file_changed_during_hash")
        return {
            "sha256": "sha256:" + digest.hexdigest(),
            "byte_size": int(after.st_size),
            "mode": int(stat.S_IMODE(after.st_mode)),
            "uid": int(after.st_uid),
            "device": int(after.st_dev),
            "inode": int(after.st_ino),
            "mtime_ns": int(after.st_mtime_ns),
            "ctime_ns": int(after.st_ctime_ns),
        }
    finally:
        os.close(descriptor)


def _sqlite_integrity(path: Path) -> str:
    _lstat_regular(path, private=False)
    try:
        uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=15) as conn:
            row = conn.execute("PRAGMA integrity_check").fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise ModelCallLedgerRecoveryError("recovery_sqlite_integrity_unavailable") from exc
    result = str(row[0] if row else "")
    if result != "ok":
        raise ModelCallLedgerRecoveryError("recovery_sqlite_integrity_invalid")
    return result


def _sidecar_identity(path: Path) -> list[dict[str, Any]]:
    identities: list[dict[str, Any]] = []
    for suffix in _SIDECAR_SUFFIXES:
        candidate = Path(str(path) + suffix)
        try:
            candidate.lstat()
        except FileNotFoundError:
            identities.append({"suffix": suffix, "state": "absent"})
            continue
        except OSError as exc:
            raise ModelCallLedgerRecoveryError("recovery_sidecar_uninspectable") from exc
        identity = _file_identity(candidate, private=False)
        identities.append(
            {
                "suffix": suffix,
                "state": "present",
                "sha256": identity["sha256"],
                "byte_size": identity["byte_size"],
            }
        )
    return identities


def _durable_sidecar_identity(sidecars: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return sidecar evidence whose bytes are durable across a read-only open.

    SQLite's ``-shm`` file is reconstructed/shared-memory coordination state;
    a read-only verification connection may legally update it without changing
    the database or WAL transaction state.  It is still captured for reverse
    compensation, but it cannot be an exact postimage invariant.  WAL and
    rollback-journal bytes remain bound exactly.
    """
    return [
        {
            "suffix": str(sidecar.get("suffix") or ""),
            "state": str(sidecar.get("state") or ""),
            **(
                {
                    "sha256": str(sidecar.get("sha256") or ""),
                    "byte_size": int(sidecar.get("byte_size") or 0),
                }
                if str(sidecar.get("state") or "") == "present"
                else {}
            ),
        }
        for sidecar in sidecars
        if str(sidecar.get("suffix") or "") != "-shm"
    ]


def _target_identity(path: Path) -> dict[str, Any]:
    try:
        path.lstat()
    except FileNotFoundError:
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = Path(str(path) + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ModelCallLedgerRecoveryError("recovery_sidecar_uninspectable") from exc
            # An absent main file is not a valid absent target if a journal,
            # WAL, or SHM remains.  In particular a journal can retain raw
            # retired pages; treat it as a fail-closed recovery boundary.
            raise ModelCallLedgerRecoveryError("recovery_orphan_sidecar_present")
        return {"state": "absent"}
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_target_uninspectable") from exc
    identity = _file_identity(path, private=False)
    integrity = _sqlite_integrity(path)
    sidecars = _sidecar_identity(path)
    state_hash = _hash_payload(
        {
            "main_sha256": identity["sha256"],
            "sidecars": _durable_sidecar_identity(sidecars),
            "integrity_check": integrity,
        }
    )
    return {
        "state": "present",
        "sha256": identity["sha256"],
        "byte_size": identity["byte_size"],
        "mode": identity["mode"],
        "sqlite_integrity": integrity,
        "sidecars": sidecars,
        "state_hash": state_hash,
    }


def _private_root_binding(root: Path) -> str:
    metadata = _lstat_directory(root, private=True)
    return _hash_payload(
        {
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "uid": int(metadata.st_uid),
            "mode": int(stat.S_IMODE(metadata.st_mode)),
        }
    )


def _safe_relative_child(root: Path, name: str, *, private: bool) -> Path:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ModelCallLedgerRecoveryError("recovery_artifact_name_invalid")
    _lstat_directory(root, private=private)
    candidate = root / name
    if candidate.parent != root:
        raise ModelCallLedgerRecoveryError("recovery_artifact_escape")
    return candidate


def _write_new_private(path: Path, payload: bytes) -> None:
    root = path.parent
    _lstat_directory(root, private=True)
    try:
        descriptor = _no_follow_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ModelCallLedgerRecoveryError("recovery_artifact_already_exists") from exc
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_artifact_create_failed") from exc
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_artifact_write_failed") from exc
    finally:
        os.close(descriptor)
    _lstat_regular(path, private=True)
    _fsync_directory(root)


def _append_progress(root: Path, journal_name: str, event: Mapping[str, Any]) -> str:
    journal = _safe_relative_child(root, journal_name, private=True)
    record = dict(event)
    record["schema_version"] = RECOVERY_PROGRESS_SCHEMA_VERSION
    record["event_hash"] = _hash_payload(record)
    payload = _canonical_json(record) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    try:
        descriptor = _no_follow_open(journal, flags, 0o600)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_journal_open_failed") from exc
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_journal_write_failed") from exc
    finally:
        os.close(descriptor)
    _lstat_regular(journal, private=True)
    _fsync_directory(root)
    return str(record["event_hash"])


def _read_progress(root: Path, manifest: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    journal_name = str(manifest.get("journal_file") or "")
    journal = _safe_relative_child(root, journal_name, private=True)
    raw = _read_private_bytes(journal)
    events: list[dict[str, Any]] = []
    previous = str(manifest.get("journal_anchor_hash") or "")
    if not previous:
        raise ModelCallLedgerRecoveryError("recovery_journal_anchor_missing")
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelCallLedgerRecoveryError("recovery_journal_invalid_json") from exc
        if not isinstance(value, dict):
            raise ModelCallLedgerRecoveryError("recovery_journal_record_invalid")
        observed_hash = str(value.pop("event_hash", ""))
        if str(value.get("schema_version") or "") != RECOVERY_PROGRESS_SCHEMA_VERSION:
            raise ModelCallLedgerRecoveryError("recovery_journal_schema_invalid")
        if str(value.get("prev_hash") or "") != previous:
            raise ModelCallLedgerRecoveryError("recovery_journal_chain_invalid")
        if observed_hash != _hash_payload(value):
            raise ModelCallLedgerRecoveryError("recovery_journal_hash_invalid")
        value["event_hash"] = observed_hash
        events.append(value)
        previous = observed_hash
    if not events:
        raise ModelCallLedgerRecoveryError("recovery_journal_empty")
    first = events[0]
    if (
        first.get("event") != "apply_prepared"
        or str(first.get("event_hash") or "")
        != str(manifest.get("prepared_chain_head") or "")
    ):
        raise ModelCallLedgerRecoveryError("recovery_journal_prepare_anchor_invalid")
    allowed_after = {
        "apply_prepared": {"apply_started", "apply_failed"},
        "apply_started": {"apply_committed", "apply_failed", "restore_started"},
        "apply_committed": {"restore_started"},
        "apply_failed": {"restore_started"},
        "restore_started": {"target_restore_intent", "restore_failed"},
        "target_restore_intent": {"target_restored", "restore_failed"},
        "target_restored": {"target_restore_intent", "restore_committed", "restore_failed"},
        "restore_committed": set(),
        # A failed restore whose durable receipt proves reverse compensation
        # completed may be retried after the planner re-verifies every
        # postimage.  A failed/unknown compensation remains terminal in the
        # planner even though the append-only grammar accepts no mutation by
        # itself.
        "restore_failed": {"restore_started"},
    }
    for prior, current in zip(events, events[1:]):
        prior_event = str(prior.get("event") or "")
        current_event = str(current.get("event") or "")
        if current_event not in allowed_after.get(prior_event, set()):
            raise ModelCallLedgerRecoveryError("recovery_journal_transition_invalid")
    return events, previous


def _target_paths(config: Any, target_ids: Iterable[str] | None = None) -> dict[str, Path]:
    paths = RuntimePaths.from_config(config)
    database_dir = Path(paths.database_dir).expanduser().absolute()
    try:
        _lstat_directory(database_dir, private=False)
    except ModelCallLedgerRecoveryError as exc:
        if str(exc) == "recovery_directory_missing":
            raise ModelCallLedgerRecoveryError("recovery_runtime_directory_missing") from exc
        raise ModelCallLedgerRecoveryError("recovery_runtime_directory_not_safe") from exc
    result: dict[str, Path] = {}
    selected_ids = tuple(target_ids) if target_ids is not None else tuple(_TARGET_FILENAMES)
    for target_id in selected_ids:
        filename = _TARGET_FILENAMES.get(target_id)
        if filename is None:
            raise ModelCallLedgerRecoveryError("recovery_target_id_invalid")
        target = database_dir / filename
        if target.parent != database_dir:
            raise ModelCallLedgerRecoveryError("recovery_target_path_invalid")
        result[target_id] = target
    return result


def _source_report_by_filename(result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    reports: dict[str, Mapping[str, Any]] = {}
    raw_sources = result.get("sources")
    if not isinstance(raw_sources, list):
        raise ModelCallLedgerRecoveryError("recovery_source_reports_missing")
    for report in raw_sources:
        if not isinstance(report, Mapping):
            raise ModelCallLedgerRecoveryError("recovery_source_reports_invalid")
        filename = Path(str(report.get("path") or "")).name
        if filename not in {filename for _, filename in _TARGETS}:
            raise ModelCallLedgerRecoveryError("recovery_source_report_owner_invalid")
        if filename in reports:
            raise ModelCallLedgerRecoveryError("recovery_source_report_duplicate")
        reports[filename] = report
    canonical = result.get("canonical_retired_storage")
    if not isinstance(canonical, Mapping):
        raise ModelCallLedgerRecoveryError("recovery_canonical_report_missing")
    reports["model_call_ledger.db"] = canonical
    return reports


def _semantic_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Use only safe plan fields and intentionally omit physical generation."""
    schema_objects = [
        {
            "type": str(value.get("type") or ""),
            "name": str(value.get("name") or ""),
            "table": str(value.get("table") or ""),
        }
        for value in report.get("other_user_schema_objects", [])
        if isinstance(value, Mapping)
    ]
    schema_objects.sort(key=lambda value: _canonical_json(value))
    return {
        "exists": bool(report.get("exists")),
        "integrity_check": str(report.get("integrity_check") or ""),
        "retired_tables": sorted(str(value) for value in report.get("retired_tables", [])),
        "other_user_tables": sorted(str(value) for value in report.get("other_user_tables", [])),
        "other_user_schema_objects": schema_objects,
        "rows_by_table": {
            str(key): int(value or 0)
            for key, value in dict(report.get("rows_by_table") or {}).items()
        },
        "safe_metadata_fingerprint": str(report.get("safe_metadata_fingerprint") or ""),
        "safe_to_delete_database": bool(report.get("safe_to_delete_database")),
        "error": str(report.get("error") or ""),
    }


def reconciliation_semantic_hash(plan: Mapping[str, Any]) -> str:
    """Stable, non-content identity of a reconciliation plan after restore.

    The reconciler's execution fingerprint intentionally includes physical
    source generation to catch TOCTOU before destructive cleanup.  A restored
    database naturally receives a new inode, so recovery needs a separate
    semantic proof that keeps that safety mechanism intact while still proving
    that the original migration obligation has returned.
    """
    reports = _source_report_by_filename(plan)
    safe_counts = {
        key: int(plan.get(key, 0) or 0)
        for key in (
            "canonical_retired_record_count",
            "canonical_retired_stats_row_count",
            "retired_stats_row_count",
            "legacy_source_row_count",
            "unique_legacy_call_count",
            "duplicate_legacy_row_count",
            "canonical_already_imported_count",
            "would_import_count",
            "attributable_legacy_call_count",
            "unattributable_legacy_call_count",
            "legacy_storage_path_count",
        )
    }
    return _hash_payload(
        {
            "canonical_state": str(plan.get("canonical_state") or ""),
            "canonical_privacy_counts": {
                str(key): int(value or 0)
                for key, value in dict(plan.get("canonical_privacy_counts") or {}).items()
            },
            "privacy_reconciliation_required": bool(
                plan.get("privacy_reconciliation_required")
            ),
            "sources": {
                filename: _semantic_report(report)
                for filename, report in sorted(reports.items())
            },
            "counts": safe_counts,
            "requires_explicit_unattributable_discard": bool(
                plan.get("requires_explicit_unattributable_discard")
            ),
            "requires_explicit_retired_stats_discard": bool(
                plan.get("requires_explicit_retired_stats_discard")
            ),
            "requires_explicit_unrecoverable_run_tombstone_history_discard": bool(
                plan.get("requires_explicit_unrecoverable_run_tombstone_history_discard")
            ),
        }
    )


def _backup_map(root: Path, result: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_backups = result.get("backup")
    if not isinstance(raw_backups, list):
        raise ModelCallLedgerRecoveryError("recovery_backup_receipts_missing")
    expected_names = {filename for _, filename in _TARGETS}
    mapped: dict[str, Mapping[str, Any]] = {}
    for receipt in raw_backups:
        if not isinstance(receipt, Mapping):
            raise ModelCallLedgerRecoveryError("recovery_backup_receipt_invalid")
        source_name = Path(str(receipt.get("source") or "")).name
        backup_name = Path(str(receipt.get("path") or "")).name
        if source_name not in expected_names or not backup_name:
            raise ModelCallLedgerRecoveryError("recovery_backup_owner_invalid")
        candidate = _safe_relative_child(root, backup_name, private=True)
        received = Path(str(receipt.get("path") or "")).expanduser()
        try:
            _lstat_directory(received.parent, private=True)
            if received.parent.resolve(strict=True) != root.resolve(strict=True):
                raise ModelCallLedgerRecoveryError("recovery_backup_outside_private_root")
        except FileNotFoundError as exc:
            raise ModelCallLedgerRecoveryError("recovery_backup_missing") from exc
        if source_name in mapped:
            raise ModelCallLedgerRecoveryError("recovery_backup_duplicate")
        # Validate now so the caller never treats an arbitrary receipt as a
        # restore capability.  The detailed binding is created later.
        _lstat_regular(candidate, private=True)
        mapped[source_name] = receipt
    return mapped


def _backup_binding(root: Path, target_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
    name = Path(str(receipt.get("path") or "")).name
    candidate = _safe_relative_child(root, name, private=True)
    identity = _file_identity(candidate, private=True)
    if _sqlite_integrity(candidate) != "ok":  # defensive, `_sqlite_integrity` raises first.
        raise ModelCallLedgerRecoveryError("recovery_backup_integrity_invalid")
    return {
        "backup_id": _hash_payload(
            {"target_id": target_id, "relative_name": name, "sha256": identity["sha256"]}
        ),
        "relative_name": name,
        "sha256": identity["sha256"],
        "byte_size": identity["byte_size"],
        "mode": identity["mode"],
        "uid": identity["uid"],
        "device": identity["device"],
        "inode": identity["inode"],
        "mtime_ns": identity["mtime_ns"],
        "ctime_ns": identity["ctime_ns"],
        "sqlite_integrity": "ok",
        "source_generation": str(receipt.get("source_generation") or ""),
        "backup_generation": str(receipt.get("backup_generation") or ""),
    }


def _verify_backup_binding(root: Path, binding: Mapping[str, Any]) -> Path:
    name = str(binding.get("relative_name") or "")
    path = _safe_relative_child(root, name, private=True)
    identity = _file_identity(path, private=True)
    for key in ("sha256", "byte_size", "mode", "uid", "device", "inode", "mtime_ns", "ctime_ns"):
        if identity.get(key) != binding.get(key):
            raise ModelCallLedgerRecoveryError("recovery_backup_binding_mismatch")
    if _sqlite_integrity(path) != "ok":
        raise ModelCallLedgerRecoveryError("recovery_backup_integrity_invalid")
    expected_id = _hash_payload(
        {
            "target_id": str(binding.get("target_id") or ""),
            "relative_name": name,
            "sha256": str(binding.get("sha256") or ""),
        }
    )
    # ``target_id`` is added by callers when verifying a manifest entry.  A
    # missing ID here is handled by that caller before this comparison.
    if binding.get("backup_id") and binding.get("backup_id") != expected_id:
        raise ModelCallLedgerRecoveryError("recovery_backup_id_mismatch")
    return path


def _manifest_without_hash(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(manifest)
    value.pop("manifest_sha256", None)
    return value


def _load_manifest(config: Any, recovery_manifest: Path) -> tuple[dict[str, Any], Path, str]:
    supplied = Path(recovery_manifest).expanduser()
    if ".." in supplied.parts:
        raise ModelCallLedgerRecoveryError("recovery_manifest_parent_escape")
    path = supplied.absolute()
    root = path.parent
    expected_parent = (
        _mnemos_dir(config).expanduser().absolute() / "backups" / "model-call-ledger"
    )
    try:
        path.relative_to(expected_parent)
    except ValueError as exc:
        raise ModelCallLedgerRecoveryError("recovery_manifest_outside_runtime_backup_root") from exc
    _lstat_directory(root, private=True)
    manifest_name = path.name
    if not manifest_name.startswith(_RECOVERY_PREFIX) or not manifest_name.endswith(".json"):
        raise ModelCallLedgerRecoveryError("legacy_recovery_manifest_not_automatically_restorable")
    manifest_path = _safe_relative_child(root, manifest_name, private=True)
    raw = _read_private_bytes(manifest_path)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelCallLedgerRecoveryError("recovery_manifest_invalid_json") from exc
    if not isinstance(manifest, dict):
        raise ModelCallLedgerRecoveryError("recovery_manifest_invalid")
    if str(manifest.get("schema_version") or "") != RECOVERY_SCHEMA_VERSION:
        raise ModelCallLedgerRecoveryError("legacy_recovery_manifest_not_automatically_restorable")
    manifest_hash = str(manifest.get("manifest_sha256") or "")
    if not manifest_hash or manifest_hash != _hash_payload(_manifest_without_hash(manifest)):
        raise ModelCallLedgerRecoveryError("recovery_manifest_seal_invalid")
    if str(manifest.get("backup_root_binding") or "") != _private_root_binding(root):
        raise ModelCallLedgerRecoveryError("recovery_backup_root_binding_mismatch")
    return manifest, root, manifest_hash


def _target_entries(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_entries = manifest.get("targets")
    raw_ids = manifest.get("target_ids")
    if not isinstance(raw_entries, list) or not isinstance(raw_ids, list):
        raise ModelCallLedgerRecoveryError("recovery_manifest_targets_invalid")
    expected_ids = [str(target_id) for target_id in raw_ids]
    fixed_order = [target_id for target_id, _ in _TARGETS]
    if (
        not expected_ids
        or expected_ids[0] != "canonical_ledger"
        or any(target_id not in _TARGET_FILENAMES for target_id in expected_ids)
        or expected_ids != [target_id for target_id in fixed_order if target_id in expected_ids]
    ):
        raise ModelCallLedgerRecoveryError("recovery_manifest_target_order_invalid")
    entries = [entry for entry in raw_entries if isinstance(entry, Mapping)]
    if len(entries) != len(expected_ids) or [str(entry.get("target_id") or "") for entry in entries] != expected_ids:
        raise ModelCallLedgerRecoveryError("recovery_manifest_target_order_invalid")
    return entries


def _verify_preimage_bindings(manifest: Mapping[str, Any], root: Path) -> None:
    if str(manifest.get("migration_id") or "") != MODEL_CALL_LEDGER_MIGRATION_ID:
        raise ModelCallLedgerRecoveryError("recovery_manifest_migration_invalid")
    if not str(manifest.get("expected_plan_hash") or ""):
        raise ModelCallLedgerRecoveryError("recovery_manifest_plan_hash_missing")
    if str(manifest.get("expected_plan_hash")) != str(manifest.get("reconcile_plan_hash")):
        raise ModelCallLedgerRecoveryError("recovery_manifest_plan_hash_mismatch")
    for entry in _target_entries(manifest):
        preimage = entry.get("preimage")
        if not isinstance(preimage, Mapping):
            raise ModelCallLedgerRecoveryError("recovery_manifest_target_state_invalid")
        if str(preimage.get("state") or "") not in {"present", "absent"}:
            raise ModelCallLedgerRecoveryError("recovery_manifest_preimage_invalid")
        if preimage.get("state") == "present":
            binding = preimage.get("backup")
            if not isinstance(binding, Mapping):
                raise ModelCallLedgerRecoveryError("recovery_manifest_backup_missing")
            bound = dict(binding)
            bound["target_id"] = str(entry.get("target_id") or "")
            _verify_backup_binding(root, bound)


def _verify_manifest_bindings(
    manifest: Mapping[str, Any],
    root: Path,
    manifest_hash: str,
    ledger_binding: Mapping[str, Any] | None,
    *,
    terminal_event: str,
    chain_head: str,
) -> None:
    _verify_preimage_bindings(manifest, root)
    if ledger_binding is None:
        raise ModelCallLedgerRecoveryError("recovery_registry_binding_required")
    verification = ledger_binding.get("verification")
    if not isinstance(verification, Mapping):
        raise ModelCallLedgerRecoveryError("recovery_registry_verification_missing")
    attempt_id = str(manifest.get("registry_ledger_id") or "")
    binding_attempt_id = str(
        verification.get("recovery_attempt_ledger_id")
        or ledger_binding.get("ledger_id")
        or ""
    )
    if (
        not attempt_id
        or binding_attempt_id != attempt_id
        or str(ledger_binding.get("migration_id") or "") != MODEL_CALL_LEDGER_MIGRATION_ID
        or str(ledger_binding.get("plan_hash") or "")
        != str(manifest.get("expected_plan_hash") or "")
        or str(verification.get("recovery_manifest_sha256") or "") != manifest_hash
    ):
        raise ModelCallLedgerRecoveryError("recovery_registry_binding_mismatch")
    status = str(ledger_binding.get("status") or "")
    if status == "applied":
        if (
            terminal_event != "apply_committed"
            or str(verification.get("recovery_chain_head") or "") != chain_head
        ):
            raise ModelCallLedgerRecoveryError("recovery_registry_seal_mismatch")
        return
    if status not in {"applying", "failed"}:
        raise ModelCallLedgerRecoveryError("recovery_registry_binding_mismatch")
    if str(verification.get("recovery_prepare_chain_head") or "") != str(
        manifest.get("prepared_chain_head") or ""
    ):
        raise ModelCallLedgerRecoveryError("recovery_registry_prepare_mismatch")


def _postimages_from_event(
    manifest: Mapping[str, Any], event: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    raw_postimages = event.get("postimages")
    target_ids = [str(entry["target_id"]) for entry in _target_entries(manifest)]
    if not isinstance(raw_postimages, list) or len(raw_postimages) != len(target_ids):
        raise ModelCallLedgerRecoveryError("recovery_postimage_records_invalid")
    result: dict[str, Mapping[str, Any]] = {}
    for expected_id, item in zip(target_ids, raw_postimages):
        if not isinstance(item, Mapping) or str(item.get("target_id") or "") != expected_id:
            raise ModelCallLedgerRecoveryError("recovery_postimage_target_order_invalid")
        state = item.get("postimage")
        if not isinstance(state, Mapping) or str(state.get("state") or "") not in {
            "present",
            "absent",
        }:
            raise ModelCallLedgerRecoveryError("recovery_postimage_invalid")
        result[expected_id] = state
    if str(event.get("postimage_hash") or "") != _hash_payload(raw_postimages):
        raise ModelCallLedgerRecoveryError("recovery_postimage_hash_invalid")
    return result


def _state_matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    if str(expected.get("state") or "") != str(actual.get("state") or ""):
        return False
    if expected.get("state") == "absent":
        return True
    # A target is accepted only if its main database plus durable WAL/journal
    # byte state is the sealed postimage.  SQLite's SHM coordination file is
    # deliberately excluded: read-only verification may rebuild it without a
    # data mutation.  This still catches a later ledger call in WAL rather
    # than the main database file.
    return bool(
        str(expected.get("state_hash") or "")
        and str(expected.get("state_hash") or "") == str(actual.get("state_hash") or "")
        and str(expected.get("sqlite_integrity") or "") == "ok"
    )


def _matches_preimage(entry: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    preimage = entry.get("preimage")
    if not isinstance(preimage, Mapping):
        return False
    if str(preimage.get("state") or "") == "absent":
        return str(actual.get("state") or "") == "absent"
    binding = preimage.get("backup")
    return (
        isinstance(binding, Mapping)
        and str(actual.get("state") or "") == "present"
        and str(actual.get("sha256") or "") == str(binding.get("sha256") or "")
        and str(actual.get("sqlite_integrity") or "") == "ok"
        and all(item.get("state") == "absent" for item in actual.get("sidecars", []))
    )


def _runtime_writers_are_inactive(database_dir: Path) -> bool:
    # Keep the dependency at this boundary so tests and callers can explicitly
    # prove the daemon gate without importing daemon machinery during planning.
    from core.migrations.model_call_ledger_reconcile.runtime import runtime_writers_are_inactive

    return bool(runtime_writers_are_inactive(database_dir))


@dataclass(frozen=True)
class _RecoveryLock:
    descriptor: int
    path: Path

    def close(self) -> None:
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self.descriptor)


def _acquire_exclusive_lock(lock_path: Path, *, private_parent: bool, error_prefix: str) -> _RecoveryLock:
    _lstat_directory(lock_path.parent, private=private_parent)
    try:
        descriptor = _no_follow_open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError(f"{error_prefix}_open_failed") from exc
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ModelCallLedgerRecoveryError(f"{error_prefix}_invalid")
        if metadata.st_nlink != 1:
            raise ModelCallLedgerRecoveryError(f"{error_prefix}_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ModelCallLedgerRecoveryError(f"{error_prefix}_unavailable") from exc
        return _RecoveryLock(descriptor=descriptor, path=lock_path)
    except (OSError, ModelCallLedgerRecoveryError):
        os.close(descriptor)
        raise


def acquire_model_call_ledger_migration_lock(config: Any) -> _RecoveryLock:
    """One runtime-root writer lock shared by reconcile and v3 restore."""
    database_dir = Path(RuntimePaths.from_config(config).database_dir).expanduser().absolute()
    _lstat_directory(database_dir, private=False)
    return _acquire_exclusive_lock(
        database_dir / ".model-call-ledger-migration.lock",
        private_parent=False,
        error_prefix="model_call_ledger_migration_lock",
    )


@contextlib.contextmanager
def _exclusive_recovery_lock(root: Path) -> Iterator[_RecoveryLock]:
    lock_path = _safe_relative_child(root, ".model-call-ledger-recovery.lock", private=True)
    lock = _acquire_exclusive_lock(
        lock_path, private_parent=True, error_prefix="recovery_lock"
    )
    try:
        yield lock
    finally:
        lock.close()


def _create_private_directory(root: Path, name: str) -> Path:
    candidate = _safe_relative_child(root, name, private=True)
    try:
        candidate.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ModelCallLedgerRecoveryError("recovery_reverse_backup_collision") from exc
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_reverse_backup_create_failed") from exc
    _lstat_directory(candidate, private=True)
    _fsync_directory(root)
    return candidate
