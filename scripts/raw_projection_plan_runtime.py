"""Immutable plan, index comparator, and apply boundary for Raw projection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core.app.raw_search import (
    missing_raw_index_schema_contract,
    raw_index_content_state,
    raw_index_projection_snapshot,
)
from core.config import get_config
from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.readiness_query_budget import connect_readonly_sqlite
from scripts.raw_projection_contract import (
    PROJECTION_CONTRACT,
)
from scripts.raw_projection_secure_io import (
    _acquire_projection_transaction_lock,
    _release_projection_transaction_lock,
    _secure_read_file,
)
from scripts.raw_projection_transaction_runtime import (
    _artifact_descriptors,
    _json_text,
    _load_projection_transaction,
    _projection_journal,
    _read_file_hash,
    _remove_projection_transaction,
    _rollback_projection_transaction,
    _transaction_path,
    _write_projection_transaction_state,
)

ProjectionArtifact = Any
ProjectionChunk = Any
ReadOnlyProjectionSource = Any


def _projection_host() -> Any:
    from scripts import project_raw_vault

    return project_raw_vault


def _existing_markdown_files(raw_dir: Path) -> List[Path]:
    return list(_projection_host()._existing_markdown_files(raw_dir))


def _existing_vault_file_count(raw_dir: Path) -> int:
    return int(_projection_host()._existing_vault_file_count(raw_dir))


def _fetch_refs(*args: Any, **kwargs: Any) -> list[Any]:
    return list(_projection_host()._fetch_refs(*args, **kwargs))


def _journal_path(raw_dir: Path) -> Path:
    return Path(_projection_host()._journal_path(raw_dir))


def _managed_projection_paths(raw_dir: Path) -> set[str]:
    return set(_projection_host()._managed_projection_paths(raw_dir))


def _sha256_text(value: str) -> str:
    return str(_projection_host()._sha256_text(value))


def _sqlite_epoch_signature(db_path: Path) -> Dict[str, Any]:
    return dict(_projection_host()._sqlite_epoch_signature(db_path))


def build_projection_chunks(*args: Any, **kwargs: Any) -> list[Any]:
    """Delegate deterministic chunk construction to the projection owner."""
    return list(_projection_host().build_projection_chunks(*args, **kwargs))


def managed_projection_paths(raw_dir: Path) -> List[str]:
    """Return the exact managed paths below one Raw projection directory."""
    return list(_projection_host().managed_projection_paths(raw_dir))


def _expected_index_state(
    artifact: ProjectionArtifact,
    raw_dir: Path,
) -> Dict[str, Any]:
    declared_state = getattr(artifact, "index_state", None)
    if isinstance(declared_state, dict):
        return dict(declared_state)
    return raw_index_content_state(
        raw_dir,
        str(artifact.relative_path),
        str(artifact.text),
    )


def _raw_index_write_set(
    raw_dir: Path,
    desired_index_state_hashes: Dict[str, str],
    *,
    index_db_path: Path,
    previously_managed_paths: Iterable[str] = (),
) -> Tuple[
    List[str],
    List[str],
    str,
    Dict[str, str],
    Dict[str, int],
    str,
    str,
    List[str],
]:
    """Independently derive the exact index repair set from durable state."""
    desired_paths = set(desired_index_state_hashes)
    empty_orphan_counts = {"raw_fts": 0, "raw_tags": 0}
    empty_preimage: Dict[str, List[Dict[str, Any]]] = {
        "raw_index": [],
        "raw_fts": [],
        "raw_tags": [],
    }
    index_kind = inspect_path_kind(index_db_path)
    if index_kind == "missing":
        schema = missing_raw_index_schema_contract()
        return (
            sorted(desired_paths),
            [],
            _sha256_text(
                json.dumps(
                    empty_preimage,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
            {relative_path: "" for relative_path in sorted(desired_paths)},
            empty_orphan_counts,
            str(schema["state"]),
            str(schema["signature_hash"]),
            (
                [
                    "index:idx_raw_date",
                    "index:idx_raw_mtime",
                    "index:idx_raw_session",
                    "index:idx_raw_tags_file",
                    "index:idx_raw_tags_tag",
                    "table:raw_fts",
                    "table:raw_index",
                    "table:raw_tags",
                ]
                if desired_paths
                else []
            ),
        )
    if index_kind != "file":
        raise DurableIOError("raw_projection_index_not_regular")
    wal_path = Path(f"{index_db_path}-wal")
    snapshot: tempfile.TemporaryDirectory[str] | None = None
    inspection_path = index_db_path
    immutable = True
    wal_kind = inspect_path_kind(wal_path)
    if wal_kind not in {"missing", "file"}:
        raise DurableIOError("raw_projection_index_wal_not_regular")
    if wal_kind == "file" and wal_path.stat().st_size:
        before = _sqlite_epoch_signature(index_db_path)
        snapshot = tempfile.TemporaryDirectory(prefix="mnemos-raw-index-plan-")
        inspection_path = Path(snapshot.name) / index_db_path.name
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{index_db_path}{suffix}")
            source_kind = inspect_path_kind(source)
            if source_kind == "file":
                shutil.copyfile(source, Path(f"{inspection_path}{suffix}"))
            elif source_kind != "missing":
                raise DurableIOError("raw_projection_index_sidecar_not_regular")
        after = _sqlite_epoch_signature(index_db_path)
        if after != before:
            snapshot.cleanup()
            raise RuntimeError("Raw index evidence epoch changed during snapshot")
        for suffix in ("", "-wal", "-shm"):
            source = Path(f"{index_db_path}{suffix}")
            copied = Path(f"{inspection_path}{suffix}")
            source_kind = inspect_path_kind(source)
            if source_kind == "file" and _read_file_hash(source) != _read_file_hash(copied):
                snapshot.cleanup()
                raise RuntimeError("Raw index evidence snapshot copy is inconsistent")
            if source_kind not in {"missing", "file"}:
                snapshot.cleanup()
                raise DurableIOError("raw_projection_index_sidecar_not_regular")
        immutable = False
    index_snapshot: Dict[str, Any] = {}
    try:
        connection = connect_readonly_sqlite(
            inspection_path,
            immutable=immutable,
        )
        index_snapshot = raw_index_projection_snapshot(connection)
    except (RuntimeError, sqlite3.Error) as exc:
        raise RuntimeError(f"Raw index write-set inspection failed: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
        if snapshot is not None:
            snapshot.cleanup()

    indexed_states = dict(index_snapshot["indexed_states"])
    indexed_managed_paths = set(index_snapshot["indexed_managed_paths"])
    orphan_counts = dict(index_snapshot["orphan_counts"])
    schema = dict(index_snapshot["schema"])
    missing_objects = list(schema["missing_objects"])
    changed_paths = sorted(
        relative_path
        for relative_path, expected_hash in desired_index_state_hashes.items()
        if _sha256_text(
            json.dumps(
                indexed_states.get(relative_path),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        != expected_hash
    )
    owned_paths = set(previously_managed_paths) | indexed_managed_paths
    deleted_paths = sorted((owned_paths - desired_paths) & set(indexed_states))
    index_preimage_hash = str(index_snapshot["preimage_hash"])
    index_state_preimage_hashes = {
        relative_path: (
            _sha256_text(
                json.dumps(
                    indexed_states[relative_path],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if relative_path in indexed_states
            else ""
        )
        for relative_path in sorted(desired_paths | owned_paths)
    }
    return (
        changed_paths,
        deleted_paths,
        index_preimage_hash,
        index_state_preimage_hashes,
        orphan_counts,
        str(schema["state"]),
        str(schema["signature_hash"]),
        missing_objects,
    )


def build_projection_plan(
    raw_dir: Path,
    store: Any,
    chunks: List[ProjectionChunk],
    *,
    db_path: Path,
    max_turn_chars: int,
    max_file_bytes: int = 0,
    backup_dir: Path | None = None,
) -> Dict[str, Any]:
    """Return a content-bound, side-effect-free file and index write set."""
    source_revisions = sorted(
        (
            ref.logical_event_id or ref.event_id,
            ref.event_id,
        )
        for chunk in chunks
        for ref in chunk.refs
    )
    include_eligible_delete = any(
        ref.retention_state == "eligible_delete" for chunk in chunks for ref in chunk.refs
    )
    canonical_source_revisions = sorted(
        (
            ref.logical_event_id or ref.event_id,
            ref.event_id,
        )
        for ref in _fetch_refs(
            store,
            include_eligible_delete=include_eligible_delete,
        )
    )
    if source_revisions != canonical_source_revisions:
        raise RuntimeError("Raw projection plan must bind the complete canonical Raw denominator")
    artifacts, _chunks_by_path = _artifact_descriptors(
        raw_dir,
        store,
        chunks,
        db_path=db_path,
        max_turn_chars=max_turn_chars,
        max_file_bytes=max_file_bytes,
    )
    previously_managed = _managed_projection_paths(raw_dir)
    desired_file_hashes = {
        relative_path: artifact.sha256 for relative_path, artifact in sorted(artifacts.items())
    }
    file_preimage_hashes = {
        relative_path: _read_file_hash(raw_dir / relative_path)
        for relative_path in sorted(previously_managed | set(artifacts))
    }
    changed_paths = sorted(
        relative_path
        for relative_path, expected_hash in desired_file_hashes.items()
        if file_preimage_hashes.get(relative_path) != expected_hash
    )
    stale_paths = sorted(previously_managed - set(artifacts))
    journal = _projection_journal(artifacts)
    journal_text = _json_text(journal)
    current_journal_hash = _read_file_hash(_journal_path(raw_dir))
    desired_journal_hash = _sha256_text(journal_text)
    journal_write = current_journal_hash != desired_journal_hash
    desired_index_state_hashes = {
        relative_path: _sha256_text(
            json.dumps(
                _expected_index_state(artifact, raw_dir),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        for relative_path, artifact in artifacts.items()
    }
    (
        index_changed_paths,
        index_deleted_paths,
        index_preimage_hash,
        index_state_preimage_hashes,
        index_orphan_row_counts,
        index_schema_state,
        index_schema_signature_hash,
        index_schema_missing_objects,
    ) = _raw_index_write_set(
        raw_dir,
        desired_index_state_hashes,
        index_db_path=raw_dir / ".raw_index.db",
        previously_managed_paths=previously_managed,
    )
    write_set_empty = (
        not any(
            (
                changed_paths,
                stale_paths,
                index_changed_paths,
                index_deleted_paths,
                [name for name, count in index_orphan_row_counts.items() if count],
                index_schema_missing_objects,
            )
        )
        and not journal_write
    )
    payload: Dict[str, Any] = {
        "schema_version": "mnemos.raw_projection_plan.v2",
        "projection_contract": PROJECTION_CONTRACT,
        "raw_dir": str(raw_dir.resolve()),
        "canonical_db": str(db_path.resolve()),
        "backup_dir": str(backup_dir.resolve()) if backup_dir is not None else "",
        "generation_hash": journal["generation_hash"],
        "source_epoch_hash": str(getattr(store, "epoch_hash", "")),
        "source_revision_set_hash": _sha256_text(
            json.dumps(
                source_revisions,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ),
        "desired_file_hashes": desired_file_hashes,
        "file_preimage_hashes": file_preimage_hashes,
        "current_journal_hash": current_journal_hash,
        "desired_journal_hash": desired_journal_hash,
        "index_preimage_hash": index_preimage_hash,
        "index_state_preimage_hashes": index_state_preimage_hashes,
        "index_orphan_row_counts": index_orphan_row_counts,
        "index_schema_state": index_schema_state,
        "index_schema_signature_hash": index_schema_signature_hash,
        "index_schema_missing_objects": index_schema_missing_objects,
        "desired_index_state_hashes": desired_index_state_hashes,
        "desired_index_generation_hash": _sha256_text(
            json.dumps(
                desired_index_state_hashes,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        "previously_managed_paths": sorted(previously_managed),
        "changed_paths": changed_paths,
        "unchanged_paths": sorted(set(artifacts) - set(changed_paths)),
        "stale_paths": stale_paths,
        "index_changed_paths": index_changed_paths,
        "index_deleted_paths": index_deleted_paths,
        "journal_write": journal_write,
        "manifest_write": bool(backup_dir is not None and not write_set_empty),
        "write_set_empty": write_set_empty,
    }
    payload["plan_hash"] = _sha256_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return payload


def _validated_projection_plan(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Raw projection plan is missing")
    plan = dict(payload)
    claimed_hash = plan.pop("plan_hash", None)
    expected_hash = _sha256_text(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    expected_fields = {
        "schema_version",
        "projection_contract",
        "raw_dir",
        "canonical_db",
        "backup_dir",
        "generation_hash",
        "source_epoch_hash",
        "source_revision_set_hash",
        "desired_file_hashes",
        "file_preimage_hashes",
        "current_journal_hash",
        "desired_journal_hash",
        "index_preimage_hash",
        "index_state_preimage_hashes",
        "index_orphan_row_counts",
        "index_schema_state",
        "index_schema_signature_hash",
        "index_schema_missing_objects",
        "desired_index_state_hashes",
        "desired_index_generation_hash",
        "previously_managed_paths",
        "changed_paths",
        "unchanged_paths",
        "stale_paths",
        "index_changed_paths",
        "index_deleted_paths",
        "journal_write",
        "manifest_write",
        "write_set_empty",
        "plan_hash",
    }
    path_lists = (
        "previously_managed_paths",
        "changed_paths",
        "unchanged_paths",
        "stale_paths",
        "index_changed_paths",
        "index_deleted_paths",
    )
    digest_fields = (
        "generation_hash",
        "source_revision_set_hash",
        "desired_journal_hash",
        "index_preimage_hash",
        "index_schema_signature_hash",
        "desired_index_generation_hash",
    )

    def is_digest(value: Any, *, allow_empty: bool = False) -> bool:
        return bool(
            isinstance(value, str)
            and ((allow_empty and value == "") or re.fullmatch(r"[0-9a-f]{64}", value))
        )

    def is_safe_relative_path(value: Any) -> bool:
        if not isinstance(value, str) or not value or "\\" in value:
            return False
        path = Path(value)
        return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value

    desired_file_hashes = payload.get("desired_file_hashes")
    file_preimages = payload.get("file_preimage_hashes")
    index_preimages = payload.get("index_state_preimage_hashes")
    index_orphan_row_counts = payload.get("index_orphan_row_counts")
    index_schema_state = payload.get("index_schema_state")
    index_schema_missing_objects = payload.get("index_schema_missing_objects")
    index_states = payload.get("desired_index_state_hashes")
    all_path_values = [
        item
        for field in path_lists
        if isinstance(payload.get(field), list)
        for item in payload[field]
    ]
    validated_desired_file_hashes = (
        desired_file_hashes if isinstance(desired_file_hashes, dict) else {}
    )
    desired_paths = set(validated_desired_file_hashes)
    changed_paths = set(payload.get("changed_paths") or [])
    unchanged_paths = set(payload.get("unchanged_paths") or [])
    stale_paths = set(payload.get("stale_paths") or [])
    index_changed_paths = set(payload.get("index_changed_paths") or [])
    index_deleted_paths = set(payload.get("index_deleted_paths") or [])
    previous_paths = set(payload.get("previously_managed_paths") or [])
    derived_changed_paths = {
        path
        for path in desired_paths
        if isinstance(file_preimages, dict)
        and validated_desired_file_hashes[path] != file_preimages.get(path, "")
    }
    derived_unchanged_paths = desired_paths - derived_changed_paths
    derived_stale_paths = previous_paths - desired_paths
    derived_index_changed_paths = {
        path
        for path in desired_paths
        if isinstance(index_preimages, dict)
        and isinstance(index_states, dict)
        and index_states.get(path) != index_preimages.get(path, "")
    }
    derived_index_deleted_paths = {
        path
        for path, preimage_hash in (
            index_preimages.items() if isinstance(index_preimages, dict) else ()
        )
        if path not in desired_paths and bool(preimage_hash)
    }
    derived_journal_write = payload.get("current_journal_hash") != payload.get(
        "desired_journal_hash"
    )
    derived_write_set_empty = (
        not changed_paths
        and not stale_paths
        and not index_changed_paths
        and not index_deleted_paths
        and not any(
            count
            for count in (
                index_orphan_row_counts.values()
                if isinstance(index_orphan_row_counts, dict)
                else ()
            )
        )
        and not index_schema_missing_objects
        and derived_journal_write is False
    )
    all_index_schema_objects = {
        "index:idx_raw_date",
        "index:idx_raw_mtime",
        "index:idx_raw_session",
        "index:idx_raw_tags_file",
        "index:idx_raw_tags_tag",
        "table:raw_fts",
        "table:raw_index",
        "table:raw_tags",
    }
    expected_missing_schema_objects = (
        sorted(all_index_schema_objects)
        if index_schema_state == "absent"
        or (index_schema_state == "missing_database" and bool(desired_paths))
        else []
    )
    if (
        set(payload) != expected_fields
        or payload.get("schema_version") != "mnemos.raw_projection_plan.v2"
        or payload.get("projection_contract") != PROJECTION_CONTRACT
        or not isinstance(claimed_hash, str)
        or claimed_hash != expected_hash
        or any(not is_digest(payload.get(field)) for field in digest_fields)
        or not is_digest(payload.get("source_epoch_hash"), allow_empty=True)
        or not is_digest(payload.get("current_journal_hash"), allow_empty=True)
        or not isinstance(desired_file_hashes, dict)
        or not isinstance(file_preimages, dict)
        or not isinstance(index_preimages, dict)
        or not isinstance(index_orphan_row_counts, dict)
        or set(index_orphan_row_counts) != {"raw_fts", "raw_tags"}
        or any(type(count) is not int or count < 0 for count in index_orphan_row_counts.values())
        or index_schema_state not in {"missing_database", "absent", "partial", "canonical"}
        or not isinstance(index_schema_missing_objects, list)
        or index_schema_missing_objects != sorted(set(index_schema_missing_objects))
        or any(item not in all_index_schema_objects for item in index_schema_missing_objects)
        or (index_schema_state == "canonical" and bool(index_schema_missing_objects))
        or (
            index_schema_state == "partial"
            and (
                not index_schema_missing_objects
                or set(index_schema_missing_objects) == all_index_schema_objects
            )
        )
        or (
            index_schema_state in {"absent", "missing_database"}
            and index_schema_missing_objects != expected_missing_schema_objects
        )
        or not isinstance(index_states, dict)
        or any(
            not is_safe_relative_path(path) or not is_digest(digest)
            for path, digest in desired_file_hashes.items()
        )
        or any(
            not is_safe_relative_path(path) or not is_digest(digest, allow_empty=True)
            for path, digest in file_preimages.items()
        )
        or any(
            not is_safe_relative_path(path) or not is_digest(digest)
            for path, digest in index_states.items()
        )
        or any(
            not is_safe_relative_path(path) or not is_digest(digest, allow_empty=True)
            for path, digest in index_preimages.items()
        )
        or any(not is_safe_relative_path(item) for item in all_path_values)
        or not all(
            isinstance(payload.get(field), str)
            and (not payload[field] or Path(payload[field]).is_absolute())
            for field in ("raw_dir", "canonical_db", "backup_dir")
        )
        or any(
            not isinstance(payload.get(field), list)
            or payload[field] != sorted(set(payload[field]))
            or not all(isinstance(item, str) and item for item in payload[field])
            for field in path_lists
        )
        or any(
            type(payload.get(field)) is not bool
            for field in ("journal_write", "manifest_write", "write_set_empty")
        )
        or payload.get("desired_index_generation_hash")
        != _sha256_text(
            json.dumps(
                index_states,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        or set(index_states) != desired_paths
        or not desired_paths <= set(index_preimages)
        or payload.get("journal_write") is not derived_journal_write
        or bool(payload.get("manifest_write"))
        != bool(payload.get("backup_dir") and not payload.get("write_set_empty"))
        or payload.get("write_set_empty") is not derived_write_set_empty
        or changed_paths != derived_changed_paths
        or unchanged_paths != derived_unchanged_paths
        or stale_paths != derived_stale_paths
        or index_changed_paths != derived_index_changed_paths
        or index_deleted_paths != derived_index_deleted_paths
        or bool(changed_paths & unchanged_paths)
        or changed_paths | unchanged_paths != desired_paths
        or set(file_preimages) != previous_paths | desired_paths
        or not stale_paths <= previous_paths
        or not index_changed_paths <= desired_paths
        or bool(index_changed_paths & index_deleted_paths)
    ):
        raise RuntimeError("Raw projection plan is malformed or tampered")
    return dict(payload)


def validate_projection_plan(payload: Any) -> Dict[str, Any]:
    """Validate the complete typed plan before a daemon may trust it."""
    return _validated_projection_plan(payload)


def update_raw_index_changes(
    raw_dir: Path,
    *,
    changed_paths: Iterable[str],
    deleted_paths: Iterable[str],
    cleanup_orphans: bool = False,
    index_db_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
    expected_preimage_hash: str | None = None,
    expected_schema_state: str | None = None,
    expected_schema_signature_hash: str | None = None,
    expected_orphan_counts: Dict[str, int] | None = None,
    expected_post_state_hashes: Dict[str, str] | None = None,
) -> Dict[str, int]:
    """Update only publisher-owned changed/deleted chunks in a vault-local index.

    ``db_path`` is retained as a compatibility alias for callers that already
    supplied an explicit Raw-index database.  A projection must never fall
    back to the global configured index: temporary and custom Raw vaults need
    an index owned by that vault, otherwise their relative paths can overwrite
    production index records.
    """
    if index_db_path is not None and db_path is not None:
        raise ValueError("use only one of index_db_path or db_path")
    resolved_index_path = index_db_path or db_path or (raw_dir / ".raw_index.db")
    index = _projection_host().RawIndex(
        raw_dir=raw_dir,
        db_path=resolved_index_path,
        raw_event_store=False,
        initialize_schema=False,
    )
    try:
        result = index.apply_projection_write_set(
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
            cleanup_orphans=cleanup_orphans,
            expected_preimage_hash=expected_preimage_hash,
            expected_schema_state=expected_schema_state,
            expected_schema_signature_hash=expected_schema_signature_hash,
            expected_orphan_counts=expected_orphan_counts,
            expected_post_state_hashes=expected_post_state_hashes,
        )
        if not cleanup_orphans:
            result.pop("orphan_fts_removed", None)
            result.pop("orphan_tags_removed", None)
        if not isinstance(result, dict):
            raise RuntimeError("raw index apply result is malformed")
        normalized: Dict[str, int] = {}
        for key, value in result.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise RuntimeError("raw index apply result is malformed")
            normalized[str(key)] = value
        return normalized
    finally:
        index.close()


def rebuild_raw_index(raw_dir: Path, db_path: Optional[Path] = None) -> Dict[str, int]:
    """Repair helper: index publisher-owned projections, never the whole vault."""
    return update_raw_index_changes(
        raw_dir,
        changed_paths=managed_projection_paths(raw_dir),
        deleted_paths=(),
        db_path=db_path,
    )


def plan_projection(
    args: argparse.Namespace,
) -> Tuple[ReadOnlyProjectionSource, List[ProjectionChunk], Dict[str, Any]]:
    """Construct an immutable, content-free Raw projection plan."""
    cfg = get_config()
    db_path = (
        Path(args.db_path).expanduser() if args.db_path else cfg.database_dir / "raw_events.db"
    )
    db_kind = inspect_path_kind(db_path)
    if db_kind == "missing":
        raise ValueError(f"canonical raw database is missing: {db_path}")
    if db_kind != "file":
        raise DurableIOError("canonical_raw_database_not_regular")
    raw_dir = Path(args.raw_dir).expanduser() if args.raw_dir else cfg.obsidian_vault_path
    if _load_projection_transaction(raw_dir):
        raise RuntimeError(
            "Raw projection has an interrupted transaction; apply recovery is required"
        )
    canonical_db_value = str(getattr(args, "canonical_db_identity", "") or "")
    canonical_db_path = Path(canonical_db_value).expanduser() if canonical_db_value else db_path
    max_files = int(args.max_files)
    if max_files != 0:
        raise ValueError(
            "canonical Raw projection requires --max-files=0; "
            "use a separately named Raw Preview for compact output"
        )
    if int(args.max_turn_chars) != 0:
        raise ValueError(
            "canonical Raw projection requires --max-turn-chars=0; "
            "use a separately named Raw Preview for compact output"
        )
    max_file_bytes = int(getattr(args, "max_file_bytes", 0) or 0)
    if max_file_bytes < 0:
        raise ValueError("canonical Raw projection requires --max-file-bytes>=0")
    max_chunks = None
    store = _projection_host().ReadOnlyProjectionSource(
        db_path,
        include_eligible_delete=bool(args.include_eligible_delete),
    )
    refs = _fetch_refs(store, include_eligible_delete=args.include_eligible_delete)
    chunks = build_projection_chunks(
        refs,
        chunk_turns=int(args.chunk_turns),
        max_chunks=max_chunks,
    )
    candidate_sources: Dict[str, int] = {}
    for ref in refs:
        candidate_sources[ref.source_agent] = candidate_sources.get(ref.source_agent, 0) + 1
    projected_sources: Dict[str, int] = {}
    for chunk in chunks:
        projected_sources[chunk.source_agent] = projected_sources.get(chunk.source_agent, 0) + 1
    backup_dir_value = str(getattr(args, "backup_dir", "") or "")
    backup_root = Path(backup_dir_value).expanduser() if backup_dir_value else None
    projection_plan = build_projection_plan(
        raw_dir,
        store,
        chunks,
        db_path=canonical_db_path,
        max_turn_chars=int(args.max_turn_chars),
        max_file_bytes=max_file_bytes,
        backup_dir=backup_root,
    )
    stats = {
        "raw_dir": str(raw_dir),
        "db_path": str(db_path),
        "canonical_db_identity": str(canonical_db_path),
        "existing_files": _existing_vault_file_count(raw_dir),
        "existing_md_files": len(_existing_markdown_files(raw_dir)),
        "existing_managed_files": len(managed_projection_paths(raw_dir)),
        "candidate_turns": len(refs),
        "candidate_sources": dict(sorted(candidate_sources.items())),
        "projected_chunks": len(chunks),
        "projected_files": len(chunks),
        "projected_sources": dict(sorted(projected_sources.items())),
        "max_files": max_files,
        "limit_mode": "retention",
        "chunk_turns": int(args.chunk_turns),
        "max_file_bytes": max_file_bytes,
        "projection_plan": projection_plan,
    }
    return store, chunks, stats


def apply_projection(
    args: argparse.Namespace,
    store: Any,
    chunks: List[ProjectionChunk],
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Apply one reviewed projection plan under its recovery contract."""
    raw_dir = Path(stats["raw_dir"])
    db_path = Path(stats.get("canonical_db_identity") or stats["db_path"])
    max_file_bytes = int(getattr(args, "max_file_bytes", 0) or 0)
    backup_dir_value = str(getattr(args, "backup_dir", "") or "")
    backup_root = Path(backup_dir_value).expanduser() if backup_dir_value else None
    assert_epoch_current = getattr(store, "assert_epoch_current", None)
    pre_effect_epoch_guard = assert_epoch_current if callable(assert_epoch_current) else None
    post_effect_epoch_guard = assert_epoch_current if callable(assert_epoch_current) else None
    if pre_effect_epoch_guard is not None:
        pre_effect_epoch_guard()
    current_plan = _projection_host().build_projection_plan(
        raw_dir,
        store,
        chunks,
        db_path=db_path,
        max_turn_chars=int(args.max_turn_chars),
        max_file_bytes=max_file_bytes,
        backup_dir=backup_root,
    )
    if pre_effect_epoch_guard is not None:
        pre_effect_epoch_guard()
    expected_plan = _validated_projection_plan(stats.get("projection_plan"))
    bound_backup_root = Path(expected_plan["backup_dir"]) if expected_plan["backup_dir"] else None
    expected_plan_hash = str(getattr(args, "expected_plan_hash", "") or "")
    if expected_plan_hash and expected_plan_hash != expected_plan["plan_hash"]:
        raise RuntimeError("Raw projection reviewed plan hash does not match")
    if expected_plan != current_plan:
        raise RuntimeError("Raw projection plan preconditions changed before apply")
    index_db_path = raw_dir / ".raw_index.db"
    (
        index_changed_paths,
        index_deleted_paths,
        index_preimage_hash,
        index_state_preimage_hashes,
        index_orphan_row_counts,
        index_schema_state,
        index_schema_signature_hash,
        index_schema_missing_objects,
    ) = _raw_index_write_set(
        raw_dir,
        dict(expected_plan["desired_index_state_hashes"]),
        index_db_path=index_db_path,
        previously_managed_paths=expected_plan["previously_managed_paths"],
    )
    if index_preimage_hash != expected_plan["index_preimage_hash"]:
        raise RuntimeError("Raw projection index preimage changed before apply")
    if index_state_preimage_hashes != expected_plan["index_state_preimage_hashes"]:
        raise RuntimeError("Raw projection index state changed before apply")
    if index_orphan_row_counts != expected_plan["index_orphan_row_counts"]:
        raise RuntimeError("Raw projection index orphan denominator changed before apply")
    if (
        index_schema_state != expected_plan["index_schema_state"]
        or index_schema_signature_hash != expected_plan["index_schema_signature_hash"]
        or index_schema_missing_objects != expected_plan["index_schema_missing_objects"]
    ):
        raise RuntimeError("Raw projection index schema changed before apply")
    if index_changed_paths != list(
        expected_plan["index_changed_paths"]
    ) or index_deleted_paths != list(expected_plan["index_deleted_paths"]):
        raise RuntimeError("Raw projection index write set diverged from the frozen plan")
    if pre_effect_epoch_guard is not None:
        pre_effect_epoch_guard()
    planned_manifest_path = ""
    owns_projection_transaction = False
    planned_receipt_payload = {
        "schema_version": "mnemos.raw_projection_change_set.v1",
        "status": "planned",
        "plan_hash": expected_plan["plan_hash"],
        "generation_hash": expected_plan["generation_hash"],
        "backup_dir": expected_plan["backup_dir"],
        "changed_paths": expected_plan["changed_paths"],
        "stale_paths": expected_plan["stale_paths"],
        "index_changed_paths": expected_plan["index_changed_paths"],
        "index_deleted_paths": expected_plan["index_deleted_paths"],
    }

    def bind_planned_receipt_before_publish() -> None:
        nonlocal owns_projection_transaction, planned_manifest_path
        owns_projection_transaction = True
        if pre_effect_epoch_guard is not None:
            pre_effect_epoch_guard()
        if not bool(expected_plan["manifest_write"]):
            return
        if bound_backup_root is None:
            raise RuntimeError("Raw projection plan requires a metadata backup target")
        planned_manifest_path = _projection_host()._write_change_manifest(
            bound_backup_root,
            planned_receipt_payload,
            receipt_kind="plan",
        )

    def resolve_failed_apply() -> None:
        pending_transaction = _load_projection_transaction(
            raw_dir,
            allow_cleanup=True,
        )
        if pending_transaction and (
            pending_transaction.get("plan_hash") != expected_plan["plan_hash"]
            or pending_transaction.get("generation_hash") != expected_plan["generation_hash"]
            or str(pending_transaction.get("backup_dir") or "")
            != str(expected_plan.get("backup_dir") or "")
        ):
            return
        transaction_status = (
            pending_transaction.get("_transaction_status") if pending_transaction else ""
        )
        planned_receipt_committed = False
        if bound_backup_root is not None and bool(expected_plan["manifest_write"]):
            plan_receipt_name = f"raw-projection-plan-{expected_plan['plan_hash']}.json"
            try:
                plan_receipt_bytes, _plan_receipt_hash = _secure_read_file(
                    bound_backup_root,
                    plan_receipt_name,
                )
                planned_receipt_committed = plan_receipt_bytes == _json_text(
                    planned_receipt_payload
                ).encode("utf-8")
                if plan_receipt_bytes is not None and not planned_receipt_committed:
                    raise RuntimeError("Raw projection planned receipt identity collision")
            except (OSError, ValueError):
                planned_receipt_committed = False
        if (
            planned_receipt_committed
            and bound_backup_root is not None
            and transaction_status not in {"publishing", "published"}
        ):
            if pending_transaction and transaction_status == "prepared":
                _write_projection_transaction_state(
                    _transaction_path(raw_dir),
                    status="aborting",
                    plan_hash=pending_transaction["plan_hash"],
                    generation_hash=pending_transaction["generation_hash"],
                    manifest_hash=pending_transaction["manifest_hash"],
                )
                pending_transaction["_transaction_status"] = "aborting"
                transaction_status = "aborting"
            try:
                _projection_host()._write_change_manifest(
                    bound_backup_root,
                    {
                        "schema_version": "mnemos.raw_projection_change_set.v1",
                        "status": "aborted_before_publish",
                        "plan_hash": expected_plan["plan_hash"],
                        "generation_hash": expected_plan["generation_hash"],
                        "changed_paths": expected_plan["changed_paths"],
                        "stale_paths": expected_plan["stale_paths"],
                        "index_changed_paths": expected_plan["index_changed_paths"],
                        "index_deleted_paths": expected_plan["index_deleted_paths"],
                    },
                    receipt_kind="abort",
                )
            except (OSError, RuntimeError, TypeError, ValueError) as receipt_error:
                raise RuntimeError(
                    "Raw projection abort receipt could not pair the planned receipt"
                ) from receipt_error
        if pending_transaction:
            if transaction_status == "aborting":
                _remove_projection_transaction(_transaction_path(raw_dir))
            else:
                _rollback_projection_transaction(raw_dir, pending_transaction)

    write_stats: Dict[str, Any] = {}
    transaction_lock_fd = -1
    try:
        write_stats = _projection_host().write_projection(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=int(args.max_turn_chars),
            max_file_bytes=max_file_bytes,
            backup_dir=None,
            transaction_backup_dir=bound_backup_root,
            projection_plan=expected_plan,
            before_prepare=pre_effect_epoch_guard,
            before_publish=bind_planned_receipt_before_publish,
            retain_transaction=not bool(expected_plan["write_set_empty"]),
        )
        transaction_lock_fd = int(write_stats.pop("_transaction_lock_fd", -1))
        if post_effect_epoch_guard is not None:
            post_effect_epoch_guard()
        if (
            list(write_stats["changed_paths"]) != list(expected_plan["changed_paths"])
            or bool(write_stats["journal_written"]) is not bool(expected_plan["journal_write"])
            or not set(write_stats["deleted_stale_paths"]) <= set(expected_plan["stale_paths"])
        ):
            raise RuntimeError("Raw projection filesystem effect diverged from the frozen plan")
        index_repair_required = bool(
            index_changed_paths
            or index_deleted_paths
            or any(index_orphan_row_counts.values())
            or index_schema_missing_objects
        )
        if index_repair_required:
            index_stats = _projection_host().update_raw_index_changes(
                raw_dir,
                changed_paths=index_changed_paths,
                deleted_paths=index_deleted_paths,
                cleanup_orphans=bool(
                    any(index_orphan_row_counts.values()) or index_schema_missing_objects
                ),
                index_db_path=index_db_path,
                expected_preimage_hash=index_preimage_hash,
                expected_schema_state=index_schema_state,
                expected_schema_signature_hash=index_schema_signature_hash,
                expected_orphan_counts=index_orphan_row_counts,
                expected_post_state_hashes={
                    **dict(expected_plan["desired_index_state_hashes"]),
                    **{relative_path: "" for relative_path in index_deleted_paths},
                },
            )
            if int(index_stats.get("failed", 0)):
                raise RuntimeError("Raw projection index update did not commit every planned path")
            if int(index_stats.get("orphan_fts_removed", 0)) != int(
                index_orphan_row_counts["raw_fts"]
            ) or int(index_stats.get("orphan_tags_removed", 0)) != int(
                index_orphan_row_counts["raw_tags"]
            ):
                raise RuntimeError(
                    "Raw projection index orphan cleanup diverged from the frozen plan"
                )
        else:
            index_stats = {"indexed": 0, "removed": 0, "failed": 0}
        if post_effect_epoch_guard is not None:
            post_effect_epoch_guard()
        post_apply_plan = _projection_host().build_projection_plan(
            raw_dir,
            store,
            chunks,
            db_path=db_path,
            max_turn_chars=int(args.max_turn_chars),
            max_file_bytes=max_file_bytes,
            backup_dir=bound_backup_root,
        )
        if post_effect_epoch_guard is not None:
            post_effect_epoch_guard()
        if not bool(post_apply_plan["write_set_empty"]):
            raise RuntimeError("Raw projection apply left a non-empty replay write set")
    except BaseException:
        try:
            if owns_projection_transaction:
                if transaction_lock_fd < 0:
                    transaction_kind = inspect_path_kind(_transaction_path(raw_dir))
                    if transaction_kind != "missing":
                        transaction_lock_fd = _acquire_projection_transaction_lock(raw_dir)
                if transaction_lock_fd >= 0:
                    resolve_failed_apply()
        finally:
            _release_projection_transaction_lock(transaction_lock_fd)
            transaction_lock_fd = -1
        raise
    manifest_path = ""
    try:
        if bool(expected_plan["manifest_write"]):
            manifest_path = _projection_host()._write_change_manifest(
                bound_backup_root,
                {
                    "schema_version": "mnemos.raw_projection_change_set.v1",
                    "status": "committed",
                    "plan_hash": expected_plan["plan_hash"],
                    "generation_hash": expected_plan["generation_hash"],
                    "changed_paths": expected_plan["changed_paths"],
                    "stale_paths": expected_plan["stale_paths"],
                    "index_changed_paths": expected_plan["index_changed_paths"],
                    "index_deleted_paths": expected_plan["index_deleted_paths"],
                    "bytes_written": write_stats["bytes_written"],
                },
                receipt_kind="commit",
            )
        if transaction_lock_fd >= 0:
            pending_transaction = _load_projection_transaction(
                raw_dir,
                allow_cleanup=True,
            )
            if pending_transaction:
                _remove_projection_transaction(_transaction_path(raw_dir))
    finally:
        _release_projection_transaction_lock(transaction_lock_fd)
        transaction_lock_fd = -1
    return {
        **stats,
        **write_stats,
        "backup_dir": (str(bound_backup_root) if bound_backup_root is not None else ""),
        "backup_manifest_path": manifest_path,
        "backup_plan_manifest_path": planned_manifest_path,
        "applied_plan_hash": expected_plan["plan_hash"],
        "index_changed_paths": index_changed_paths,
        "index_deleted_paths": index_deleted_paths,
        "raw_index": index_stats,
        "post_apply_plan_hash": post_apply_plan["plan_hash"],
        "post_apply_zero_delta": True,
    }
