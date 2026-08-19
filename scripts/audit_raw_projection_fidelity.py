#!/usr/bin/env python3
"""Reverse-parse lossless Raw Markdown and compare every visible field to Raw.

The audit intentionally opens ``raw_events.db`` read-only and never constructs
``RawEventStore``: a fidelity check must not create, migrate, or repair the
canonical evidence it is checking.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import zlib
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ops.durable_io import (
    DurableIOError,
    physical_scope_signature,
    regular_file_sha256,
)
from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.ops.durable_io import read_native_bytes
from scripts.project_raw_vault import (
    EVENT_MARKER_PREFIX,
    FIELD_CONT_MARKER_PREFIX,
    FIELD_MARKER_END,
    FIELD_MARKER_PREFIX,
    PROJECTION_CONTRACT,
    PROJECTION_INDEX_MNEMOS_TYPE,
    PROJECTION_JOURNAL_NAME,
    PROJECTION_PART_PATH_PATTERN,
    VISIBLE_FIELDS,
    _decode_utf8_prefix,
    _frontmatter,
    _safe_slug,
    _sha256_text,
    render_projection_index_body,
    structured_field_text,
)
from scripts.raw_projection_contract import (
    parse_projection_event_header,
    projection_timestamp_path_segment,
    validate_projection_metadata,
)
SCHEMA_VERSION = "mnemos.raw_projection_fidelity.v3"
_EVENT_PREFIX = EVENT_MARKER_PREFIX.encode("utf-8")
_FIELD_PREFIX = FIELD_MARKER_PREFIX.encode("utf-8")
_FIELD_END = FIELD_MARKER_END.encode("utf-8")
_FIELD_CONT_PREFIX = FIELD_CONT_MARKER_PREFIX.encode("utf-8")
_FIELD_HEADINGS = (
    ("user_content", b"User"),
    ("assistant_content", b"Assistant"),
    ("reasoning", b"Reasoning"),
    ("structured", b"Structured"),
)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    """Open a checkpointed DB without creating or touching WAL sidecars.

    SQLite ``mode=ro`` may still create or update ``-shm`` while reading a WAL
    database.  A fidelity audit must not mutate the formal evidence it checks,
    and ``immutable=1`` would silently ignore non-empty WAL pages.  Therefore a
    live WAL generation is rejected until an evidence-epoch owner supplies a
    checkpointed immutable snapshot.
    """
    resolved = path.parent.resolve(strict=True) / path.name
    wal_path = resolved.with_name(resolved.name + "-wal")
    try:
        wal_scope = physical_scope_signature(
            (wal_path,),
            hash_max_bytes=0,
        )
        entries = wal_scope.get("entries")
        if not isinstance(entries, list) or len(entries) != 1:
            raise DurableIOError("raw_projection_wal_signature_invalid")
        wal_entry = entries[0]
        if not isinstance(wal_entry, dict):
            raise DurableIOError("raw_projection_wal_signature_invalid")
        if wal_entry.get("present") is False:
            wal_size = 0
        elif wal_entry.get("kind") != "file":
            raise DurableIOError("raw_projection_wal_not_regular")
        else:
            wal_size = int(wal_entry.get("size") or 0)
    except (DurableIOError, OSError, TypeError, ValueError) as exc:
        raise ValueError("raw_events.db WAL state is unreadable") from exc
    if wal_size:
        raise ValueError(
            "raw_events.db has a non-empty WAL; audit a checkpointed immutable "
            "evidence-epoch snapshot instead of touching production sidecars"
        )
    return connect_readonly_sqlite(resolved, immutable=True)


def _path_signature(path: Path) -> dict[str, Any]:
    try:
        scope = physical_scope_signature((Path(path).absolute(),))
        entries = scope.get("entries")
        if not isinstance(entries, list) or len(entries) != 1:
            raise DurableIOError("raw_projection_path_signature_invalid")
        entry = entries[0]
        if not isinstance(entry, dict):
            raise DurableIOError("raw_projection_path_signature_invalid")
    except (DurableIOError, OSError, TypeError) as exc:
        return {"exists": False, "error": exc.__class__.__name__}
    if entry.get("present") is False:
        return {"exists": False}
    return {
        "exists": True,
        **{
            key: value
            for key, value in entry.items()
            if key not in {"path", "present"}
        },
    }


def _file_sha256(path: Path) -> str:
    return regular_file_sha256(path)


def _is_regular_file_without_symlink_escape(root: Path, relative_path: str) -> bool:
    """Validate a publisher-owned file lexically without following symlinks."""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return False
    try:
        root_stat = root.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        return False
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(current_stat.st_mode):
            return False
        is_last = index == len(relative.parts) - 1
        if is_last:
            return stat.S_ISREG(current_stat.st_mode)
        if not stat.S_ISDIR(current_stat.st_mode):
            return False
    return False


def _safe_managed_projection_paths(raw_dir: Path) -> list[str]:
    """Discover pre-journal managed Markdown without reading through symlinks."""
    try:
        root_stat = raw_dir.lstat()
    except OSError as exc:
        raise ValueError("Raw projection root is unreadable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("Raw projection root must be a non-symlink directory")
    managed: list[str] = []
    for directory, directory_names, filenames in os.walk(raw_dir, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = [
            name
            for name in directory_names
            if not (directory_path / name).is_symlink()
        ]
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            path = directory_path / filename
            relative_path = path.relative_to(raw_dir).as_posix()
            if not _is_regular_file_without_symlink_escape(raw_dir, relative_path):
                continue
            try:
                prefix = _decode_utf8_prefix(read_native_bytes(path)[:4096])
            except UnicodeError as exc:
                raise ValueError(
                    "managed Raw projection candidate is not valid UTF-8"
                ) from exc
            except OSError as exc:
                raise ValueError(
                    "managed Raw projection candidate is unreadable"
                ) from exc
            if re.search(
                r'^mnemos_type:\s+["\']?raw_retention_projection(?:_index)?["\']?\s*$',
                prefix,
                flags=re.MULTILINE,
            ):
                managed.append(relative_path)
    return sorted(managed)


def _sqlite_inventory(path: Path) -> dict[str, dict[str, Any]]:
    resolved = path.parent.resolve(strict=True) / path.name
    return {
        suffix or "db": _path_signature(
            resolved if not suffix else resolved.with_name(resolved.name + suffix)
        )
        for suffix in ("", "-wal", "-shm")
    }


def _projection_inventory(
    raw_dir: Path,
    relative_paths: list[str],
) -> dict[str, dict[str, Any]]:
    journal = raw_dir / PROJECTION_JOURNAL_NAME
    return {
        ".mnemos_raw_projection_journal.json": _path_signature(journal),
        **{
            relative_path: _path_signature(raw_dir / relative_path)
            for relative_path in relative_paths
        },
    }


def _failure_report(*, raw_dir: Path, db_path: Path, error: str) -> dict[str, Any]:
    gap_generation = {
        "classification": "audit_input_unreadable",
        "expected_revision_set_hash": "",
        "observed_revision_set_hash": "",
        "expected_revision_evidence_hash": "",
        "observed_revision_evidence_hash": "",
        "missing_revision_evidence_hash": "",
        "unexpected_revision_evidence_hash": "",
        "missing_new_logical_event_count": 0,
        "missing_replacement_revision_count": 0,
        "unexpected_superseded_revision_count": 0,
        "paired_superseded_revision_count": 0,
        "unexpected_superseded_field_mismatch_count": 0,
        "unpaired_superseded_revision_count": 0,
        "unknown_unexpected_revision_count": 0,
        "logical_event_id_mismatch_count": 0,
        "projection_metadata_mismatch_count": 0,
        "projection_reference_mismatch_count": 0,
        "projection_reference_mismatch_evidence_hash": _canonical_json_hash([]),
        "projection_metric_aggregate_mismatch_count": 0,
        "projection_metric_aggregate_mismatch_evidence_hash": _canonical_json_hash(
            []
        ),
        "structural_error_count": 0,
        "structural_error_evidence_hash": _canonical_json_hash([]),
        "publisher_generation_hash": "",
        "publisher_journal_hash": "",
        "canonical_db_identity_hash": "",
        "evidence_epoch_stable": False,
    }
    gap_generation["gap_hash"] = _canonical_json_hash(gap_generation)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "raw_dir": str(raw_dir),
        "db_path": str(db_path),
        "expected_event_ids": 0,
        "observed_event_ids": 0,
        "missing_event_ids": 0,
        "duplicate_event_ids": 0,
        "unexpected_event_ids": 0,
        "truncated_events": 0,
        "truncated_marker_files": 0,
        "field_hash_mismatch_count": 0,
        "logical_event_id_mismatch_count": 0,
        "projection_metadata_mismatch_count": 0,
        "projection_reference_mismatch_count": 0,
        "projection_metric_aggregate_mismatch_count": 0,
        "structural_error_count": 0,
        "visible_fields_checked": 0,
        "error_count": 1,
        "error": error,
        "errors": [error],
        "missing_event_id_samples": [],
        "unexpected_event_id_samples": [],
        "gap_generation": gap_generation,
    }


def _canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _content_free_structural_evidence(
    errors: list[str],
) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for error in sorted(errors):
        code_match = re.search(r"(?:raw_)?projection_[a-z0-9_]+", error)
        evidence.append(
            {
                "error_code": (
                    code_match.group(0)
                    if code_match is not None
                    else "projection_structure_error"
                ),
                "error_sha256": _sha256_text(error),
            }
        )
    return evidence


def _strict_json_loads(value: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=reject_duplicate_keys)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses duplicate mapping keys."""


def _construct_unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _is_hex_digest(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    return len(text) == 64 and all(
        character in "0123456789abcdef" for character in text
    )


def _is_revision_content_digest(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    return len(text) in {16, 64} and all(
        character in "0123456789abcdef" for character in text
    )


def _is_revision_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    return (
        text.startswith("rawrev-")
        and len(text) == 47
        and all(character in "0123456789abcdef" for character in text[7:])
    )


def _is_nonnegative_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(number) and number >= 0


def _is_logical_event_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    return len(text) == 32 and all(
        character in "0123456789abcdef" for character in text
    )


def _validated_revision_metadata(
    *,
    revision_id: object,
    logical_event_id: object,
    revision_number: object,
    supersedes_revision_id: object,
    content_hash: object,
    full_content_hash: object,
    current_revision_id: object | None = None,
) -> dict[str, Any]:
    """Return typed content-free lineage or reject untrusted DB metadata."""
    supersedes = str(supersedes_revision_id or "")
    current = str(current_revision_id or "")
    if not _is_revision_id(revision_id):
        raise ValueError("raw revision metadata has an invalid revision_id")
    if not _is_logical_event_id(logical_event_id):
        raise ValueError("raw revision metadata has an invalid logical_event_id")
    if type(revision_number) is not int or revision_number < 0:
        raise ValueError("raw revision metadata has an invalid revision_number")
    if revision_number == 0 and supersedes:
        raise ValueError("raw revision zero unexpectedly supersedes another revision")
    if revision_number > 0 and not _is_revision_id(supersedes):
        raise ValueError("raw replacement revision lacks a valid predecessor")
    if not _is_revision_content_digest(content_hash):
        raise ValueError("raw revision metadata has an invalid content digest")
    if full_content_hash and not _is_revision_content_digest(full_content_hash):
        raise ValueError("raw revision metadata has an invalid full content digest")
    if current and not _is_revision_id(current):
        raise ValueError("raw revision metadata has an invalid current revision")
    return {
        "revision_id": str(revision_id),
        "logical_event_id": str(logical_event_id),
        "revision_number": revision_number,
        "supersedes_revision_id": supersedes,
        "content_hash": str(content_hash),
        "full_content_hash": str(full_content_hash or ""),
        **({"current_revision_id": current} if current_revision_id is not None else {}),
    }


def _validated_projection_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return validate_projection_metadata(payload)


def _validated_projection_ref(
    *,
    source_agent: object,
    session_id: object,
    turn_number: object,
    conversation_at: object,
    captured_at: object,
    completeness_status: object,
) -> dict[str, Any]:
    payload = {
        "source_agent": source_agent,
        "session_id": session_id,
        "turn_number": turn_number,
        "conversation_at": conversation_at,
        "captured_at": captured_at,
        "completeness_status": completeness_status,
    }
    metadata = _validated_projection_metadata(payload)
    return {
        "source_agent": metadata["source_agent"],
        "session_id": metadata["session_id"],
        "turn_number": metadata["turn_number"],
        "timestamp": metadata["conversation_at"] or metadata["captured_at"],
        "completeness_status": metadata["completeness_status"],
    }


def _validated_projection_runtime_ref(
    *,
    search_count: object,
    result_count: object,
    hit_count: object,
    reference_count: object,
    survival_score: object,
) -> dict[str, int | float]:
    count_values = {
        "search_count": search_count,
        "result_count": result_count,
        "hit_count": hit_count,
        "reference_count": reference_count,
    }
    counts: dict[str, int] = {}
    for name, value in count_values.items():
        if type(value) is not int or value < 0:
            raise ValueError(
                "raw revision has invalid projection reference counts"
            )
        counts[name] = value
    if (
        isinstance(survival_score, bool)
        or not isinstance(survival_score, (int, float))
        or not math.isfinite(float(survival_score))
        or float(survival_score) < 0
    ):
        raise ValueError("raw revision has invalid projection survival score")
    return {
        **counts,
        "survival_score": float(f"{float(survival_score):.2f}"),
    }


def _validated_projection_metric_ref(
    *,
    search_count: object,
    result_count: object,
    hit_count: object,
    view_count: object,
    reference_count: object,
    freshness_score: object,
    confidence: object,
    survival_score: object,
) -> dict[str, int | float]:
    runtime = _validated_projection_runtime_ref(
        search_count=search_count,
        result_count=result_count,
        hit_count=hit_count,
        reference_count=reference_count,
        survival_score=survival_score,
    )
    if type(view_count) is not int or view_count < 0:
        raise ValueError("raw revision has invalid projection view count")
    validated_scores: dict[str, float] = {}
    for name, value in {
        "freshness_score": freshness_score,
        "confidence": confidence,
    }.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(
                f"raw revision has invalid projection {name.replace('_', ' ')}"
            )
        validated_scores[name] = float(value)
    return {
        **runtime,
        "view_count": view_count,
        "freshness_score": float(
            f"{validated_scores['freshness_score']:.4f}"
        ),
        "confidence": float(f"{validated_scores['confidence']:.4f}"),
    }


def _public_revision_evidence(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key
        not in {
            "_projection_metadata",
            "_projection_ref",
            "_projection_runtime_ref",
            "_projection_metric_ref",
        }
    }


def _verified_projection_journal(
    raw_dir: Path,
) -> tuple[dict[str, Any], list[str], list[str]]:
    journal_path = raw_dir / PROJECTION_JOURNAL_NAME
    errors: list[str] = []
    if not _is_regular_file_without_symlink_escape(
        raw_dir, PROJECTION_JOURNAL_NAME
    ):
        try:
            journal_path.lstat()
        except FileNotFoundError:
            return {}, [], ["projection_journal_missing"]
        except OSError:
            return {}, [], ["projection_journal_unreadable"]
        return {}, [], ["projection_journal_path_unsafe"]
    try:
        raw = read_native_bytes(journal_path)
        payload = _strict_json_loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return {}, [], ["projection_journal_missing"]
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
        RecursionError,
    ):
        return {}, [], ["projection_journal_unreadable"]
    if not isinstance(payload, dict):
        return {}, [], ["projection_journal_malformed"]
    files = payload.get("files")
    if (
        set(payload)
        != {"schema_version", "projection_contract", "generation_hash", "files"}
        or
        payload.get("schema_version") != "mnemos.raw_projection.v2"
        or payload.get("projection_contract") != PROJECTION_CONTRACT
        or not isinstance(files, dict)
    ):
        return payload, [], ["projection_journal_contract_mismatch"]
    expected_generation_hash = _canonical_json_hash(files)
    if payload.get("generation_hash") != expected_generation_hash:
        errors.append("projection_journal_generation_hash_mismatch")
    relative_paths: list[str] = []
    for relative_path, metadata in sorted(files.items()):
        relative = Path(str(relative_path))
        if (
            not isinstance(relative_path, str)
            or relative.is_absolute()
            or ".." in relative.parts
            or not isinstance(metadata, dict)
        ):
            errors.append("projection_journal_file_record_malformed")
            continue
        if set(metadata) != {
            "content_hash",
            "logical_event_ids",
            "revision_ids",
            "revision_set_hash",
        }:
            errors.append(f"projection_journal_metadata_mismatch:{relative_path}")
        if not _is_regular_file_without_symlink_escape(raw_dir, relative_path):
            errors.append(f"projection_journal_path_unsafe:{relative_path}")
            continue
        revision_ids = metadata.get("revision_ids")
        logical_event_ids = metadata.get("logical_event_ids")
        if (
            not _is_hex_digest(metadata.get("content_hash"))
            or not isinstance(revision_ids, list)
            or not all(
                isinstance(item, str)
                and item.startswith("rawrev-")
                and len(item) == 47
                and all(character in "0123456789abcdef" for character in item[7:])
                for item in revision_ids
            )
            or len(set(revision_ids)) != len(revision_ids)
            or not isinstance(logical_event_ids, list)
            or len(logical_event_ids) != len(revision_ids)
            or not all(
                isinstance(item, str)
                and len(item) == 32
                and all(character in "0123456789abcdef" for character in item)
                for item in logical_event_ids
            )
            or metadata.get("revision_set_hash") != _canonical_json_hash(revision_ids)
        ):
            errors.append(f"projection_journal_metadata_mismatch:{relative_path}")
        relative_paths.append(relative_path)
    return payload, relative_paths, errors


def _canonical_revision_evidence(
    db_path: Path, *, include_eligible_delete: bool
) -> dict[str, dict[str, Any]]:
    from scripts.raw_projection_fidelity_runtime import (
        _canonical_revision_evidence as runtime_revision_evidence,
    )

    return runtime_revision_evidence(
        db_path,
        include_eligible_delete=include_eligible_delete,
    )


def _canonical_turns(
    db_path: Path, *, include_eligible_delete: bool
) -> dict[str, dict[str, str]]:
    """Compatibility view used by callers that only need visible-field hashes."""
    evidence = _canonical_revision_evidence(
        db_path,
        include_eligible_delete=include_eligible_delete,
    )
    return {
        revision_id: dict(record["visible_field_hashes"])
        for revision_id, record in evidence.items()
    }


def _revision_lineage(
    db_path: Path,
    revision_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Read content-free lineage for observed revisions absent from current Raw."""
    if not revision_ids:
        return {}
    result: dict[str, dict[str, Any]] = {}
    ordered = sorted(revision_ids)
    try:
        with _read_only_connection(db_path) as conn:
            for start in range(0, len(ordered), 500):
                batch = ordered[start : start + 500]
                query = """
                    SELECT
                        r.revision_id,
                        r.logical_event_id,
                        r.revision_number,
                        r.supersedes_revision_id,
                        r.content_hash,
                        r.full_content_hash,
                        r.snapshot_blob,
                        t.current_revision_id,
                        predecessor.logical_event_id,
                        predecessor.revision_number,
                        t.source_agent,
                        t.session_id,
                        t.turn_number,
                        t.conversation_at,
                        t.captured_at,
                        t.completeness_status,
                        COALESCE(m.search_count, 0),
                        COALESCE(m.result_count, 0),
                        COALESCE(m.hit_count, 0),
                        COALESCE(m.view_count, 0),
                        COALESCE(m.reference_count, 0),
                        COALESCE(m.freshness_score, 0.0),
                        COALESCE(m.confidence, 0.0),
                        COALESCE(m.survival_score, 0.0)
                    FROM raw_turn_revisions AS r
                    LEFT JOIN raw_turns AS t ON t.event_id=r.logical_event_id
                    LEFT JOIN raw_metrics AS m ON m.event_id=r.logical_event_id
                    LEFT JOIN raw_turn_revisions AS predecessor
                        ON predecessor.revision_id=r.supersedes_revision_id
                    WHERE r.revision_id IN (
                        SELECT value FROM json_each(?)
                    )
                """
                for row in conn.execute(
                    query,
                    (json.dumps(batch, ensure_ascii=False),),
                ):
                    metadata = _validated_revision_metadata(
                        revision_id=row[0],
                        logical_event_id=row[1],
                        revision_number=row[2],
                        supersedes_revision_id=row[3],
                        content_hash=row[4],
                        full_content_hash=row[5],
                        current_revision_id=row[7],
                    )
                    if metadata["revision_number"] > 0 and (
                        row[8] != metadata["logical_event_id"]
                        or type(row[9]) is not int
                        or row[9] != metadata["revision_number"] - 1
                    ):
                        raise ValueError(
                            f"raw revision {row[0]} has an invalid direct predecessor"
                        )
                    try:
                        payload = _strict_json_loads(
                            zlib.decompress(row[6]).decode("utf-8")
                        )
                    except (
                        TypeError,
                        ValueError,
                        zlib.error,
                        UnicodeDecodeError,
                        RecursionError,
                    ) as exc:
                        raise ValueError(
                            f"raw revision {row[0]} snapshot is unreadable"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ValueError(f"raw revision {row[0]} snapshot is malformed")
                    try:
                        projection_metadata = _validated_projection_metadata(payload)
                        projection_ref = _validated_projection_ref(
                            source_agent=payload.get("source_agent"),
                            session_id=payload.get("session_id"),
                            turn_number=payload.get("turn_number"),
                            conversation_at=payload.get("conversation_at"),
                            captured_at=payload.get("captured_at"),
                            completeness_status=payload.get(
                                "completeness_status"
                            ),
                        )
                        projection_runtime_ref = _validated_projection_runtime_ref(
                            search_count=row[16],
                            result_count=row[17],
                            hit_count=row[18],
                            reference_count=row[20],
                            survival_score=row[23],
                        )
                        projection_metric_ref = _validated_projection_metric_ref(
                            search_count=row[16],
                            result_count=row[17],
                            hit_count=row[18],
                            view_count=row[19],
                            reference_count=row[20],
                            freshness_score=row[21],
                            confidence=row[22],
                            survival_score=row[23],
                        )
                        visible_values = {
                            "user_content": str(payload.get("user_content") or ""),
                            "assistant_content": str(
                                payload.get("assistant_content") or ""
                            ),
                            "reasoning": str(payload.get("reasoning") or ""),
                            "structured": structured_field_text(payload),
                        }
                    except (RecursionError, TypeError, ValueError) as exc:
                        raise ValueError(
                            f"raw revision {row[0]} snapshot is unreadable"
                        ) from exc
                    result[str(row[0])] = {
                        **metadata,
                        "_projection_metadata": projection_metadata,
                        "_projection_ref": projection_ref,
                        "_projection_runtime_ref": projection_runtime_ref,
                        "_projection_metric_ref": projection_metric_ref,
                        "projection_metadata_hash": _canonical_json_hash(
                            projection_metadata
                        ),
                        "projection_reference_hash": _canonical_json_hash(
                            projection_runtime_ref
                        ),
                        "projection_metric_reference_hash": _canonical_json_hash(
                            projection_metric_ref
                        ),
                        "canonical_visible_field_hashes": {
                            field: _sha256_text(value)
                            for field, value in visible_values.items()
                        },
                    }
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(
            f"raw_events.db revision lineage is unreadable: {exc.__class__.__name__}"
        ) from exc
    return result


def _observed_revision_evidence(
    revision_id: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    marker = record.get("marker")
    fields = record.get("fields")
    marker_record = marker if isinstance(marker, dict) else {}
    field_records = fields if isinstance(fields, dict) else {}
    visible_hashes: dict[str, str] = {}
    for field in VISIBLE_FIELDS:
        raw_field_record = field_records.get(field)
        field_record = raw_field_record if isinstance(raw_field_record, dict) else {}
        visible_hashes[field] = str(field_record.get("content_hash") or "")
    return {
        "revision_id": revision_id,
        "logical_event_id": str(marker_record.get("logical_event_id") or ""),
        "projection_metadata_hash": _canonical_json_hash(
            _observed_projection_metadata(record)
        ),
        "projection_reference_hash": _canonical_json_hash(
            _observed_projection_runtime_ref(record)
        ),
        "visible_field_hashes": visible_hashes,
    }


def _observed_projection_metadata(record: dict[str, Any]) -> dict[str, Any]:
    raw_header = record.get("header")
    raw_preamble = record.get("preamble")
    header = raw_header if isinstance(raw_header, dict) else {}
    preamble = raw_preamble if isinstance(raw_preamble, dict) else {}
    return {
        "source_agent": str(preamble.get("source") or ""),
        "session_id": str(preamble.get("session_id") or ""),
        "turn_number": header.get("turn_number"),
        "captured_at": str(header.get("captured_at") or ""),
        "conversation_at": str(header.get("conversation_at") or ""),
        "completeness_status": str(header.get("completeness_status") or ""),
    }


def _observed_projection_runtime_ref(record: dict[str, Any]) -> dict[str, Any]:
    raw_header = record.get("header")
    header = raw_header if isinstance(raw_header, dict) else {}
    return {
        "search_count": header.get("search_count"),
        "result_count": header.get("result_count"),
        "hit_count": header.get("hit_count"),
        "reference_count": header.get("reference_count"),
        "survival_score": header.get("survival_score"),
    }


def _decode_marker(payload: bytes) -> dict[str, Any] | None:
    if len(payload) > 8 * 1024:
        return None
    try:
        value = _strict_json_loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _marker_matches(raw: bytes, prefix: bytes, start: int) -> tuple[dict[str, Any] | None, int]:
    line_end = raw.find(b"\n", start)
    if line_end < 0:
        return None, -1
    line = raw[start:line_end]
    if len(line) > 8 * 1024:
        return None, -1
    if not line.startswith(prefix) or not line.endswith(b" -->"):
        return None, -1
    return _decode_marker(line[len(prefix) : -4]), line_end + 1


def _structural_header_metadata(raw: bytes, marker_start: int) -> dict[str, Any]:
    header_start = raw.rfind(b"\n## Turn ", 0, marker_start)
    if header_start < 0:
        return {}
    header = raw[header_start + 1 : marker_start]
    if len(header) > 2048:
        return {}
    try:
        return parse_projection_event_header(header)
    except ValueError:
        return {}


def _valid_event_marker(marker: dict[str, Any], expected_event_id: str) -> bool:
    if set(marker) != {"event_id", "logical_event_id", "field_hashes"}:
        return False
    if str(marker.get("event_id") or "") != expected_event_id:
        return False
    logical_event_id = str(marker.get("logical_event_id") or "")
    if len(logical_event_id) != 32 or any(
        character not in "0123456789abcdef" for character in logical_event_id
    ):
        return False
    field_hashes = marker.get("field_hashes")
    return (
        isinstance(field_hashes, dict)
        and set(field_hashes) == set(VISIBLE_FIELDS)
        and all(_is_hex_digest(field_hashes.get(field)) for field in VISIBLE_FIELDS)
    )


def _next_structural_event(
    raw: bytes,
    *,
    cursor: int,
    path: Path,
    errors: list[str],
) -> tuple[int, dict[str, Any], dict[str, Any], int] | None:
    """Find an event marker only when its generated V2 preamble is intact.

    Raw user/tool text may legitimately quote an event-marker literal.  Search
    candidates are therefore ignored unless their immediately preceding V2
    turn header is also present; a malformed structural candidate remains an
    audit error and will additionally leave its expected event missing.
    """
    marker_start = raw.find(_EVENT_PREFIX, cursor)
    while marker_start >= 0:
        header_metadata = _structural_header_metadata(raw, marker_start)
        expected_event_id = str(header_metadata.get("event_id") or "")
        if expected_event_id:
            marker, after_event = _marker_matches(raw, _EVENT_PREFIX, marker_start)
            if marker is None or not _valid_event_marker(marker, expected_event_id):
                errors.append(f"{path}:malformed_event_marker")
            else:
                return marker_start, header_metadata, marker, after_event
        marker_start = raw.find(_EVENT_PREFIX, marker_start + len(_EVENT_PREFIX))
    return None


def _parse_event_fields(
    raw: bytes,
    *,
    cursor: int,
    event_id: str,
    path: Path,
    errors: list[str],
) -> tuple[dict[str, dict[str, Any]], int] | None:
    fields: dict[str, dict[str, Any]] = {}
    for field, heading in _FIELD_HEADINGS:
        heading_prefix = b"\n### " + heading + b"\n\n"
        if not raw.startswith(heading_prefix, cursor):
            errors.append(f"{path}:field_heading_mismatch:{event_id}:{field}")
            return None
        marker_start = cursor + len(heading_prefix)
        marker, content_start = _marker_matches(raw, _FIELD_PREFIX, marker_start)
        if marker is None:
            errors.append(f"{path}:malformed_field_marker:{event_id}:{field}")
            return None
        if (
            set(marker) != {"event_id", "field", "sha256", "bytes"}
            or str(marker.get("event_id") or "") != event_id
            or marker.get("field") != field
            or not _is_hex_digest(marker.get("sha256"))
        ):
            errors.append(f"{path}:field_identity_mismatch:{event_id}:{field}")
            return None
        byte_count = marker.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            errors.append(f"{path}:field_length_malformed:{event_id}:{field}")
            return None
        if field == "structured":
            if raw[content_start : content_start + len(b"```json\n")] != b"```json\n":
                errors.append(f"{path}:structured_fence_missing:{event_id}")
                return None
            content_start += len(b"```json\n")
            suffix = b"\n```\n" + _FIELD_END + b"\n"
        else:
            suffix = b"\n" + _FIELD_END + b"\n"
        content_end = content_start + byte_count
        if content_end > len(raw) or raw[content_end : content_end + len(suffix)] != suffix:
            errors.append(f"{path}:field_boundary_mismatch:{event_id}:{field}")
            return None
        fields[field] = {
            "marker": marker,
            "content_hash": hashlib.sha256(raw[content_start:content_end]).hexdigest(),
        }
        cursor = content_end + len(suffix)
    return fields, cursor


_PROJECTION_FRONTMATTER_KEY_ORDER = (
    "mnemos_type",
    "projection_version",
    "projection_contract",
    "canonical_db",
    "source",
    "session_id",
    "turn_start",
    "turn_end",
    "event_ids",
    "logical_event_ids",
    "conversation_start_at",
    "conversation_end_at",
    "completeness_statuses",
    "search_count",
    "result_count",
    "hit_count",
    "view_count",
    "reference_count",
    "freshness_score",
    "confidence",
    "survival_score",
    "retention_state",
    "tags",
)
_PROJECTION_FRONTMATTER_KEYS = set(_PROJECTION_FRONTMATTER_KEY_ORDER)
_PROJECTION_PREAMBLE_NOTICE = (
    "> Lossless Raw projection. Canonical raw content is stored in "
    "`raw_events.db`; every visible field below is byte-hashed.\n\n"
)


def _valid_projection_metadata_values(payload: dict[str, Any]) -> bool:
    """Validate the shared chunk metadata values of a projection frontmatter."""
    source = payload.get("source")
    session_id = payload.get("session_id")
    event_ids = payload.get("event_ids")
    logical_event_ids = payload.get("logical_event_ids")
    completeness_statuses = payload.get("completeness_statuses")
    tags = payload.get("tags")
    count_fields = (
        "turn_start",
        "turn_end",
        "search_count",
        "result_count",
        "hit_count",
        "view_count",
        "reference_count",
    )
    score_fields = ("freshness_score", "confidence", "survival_score")
    timestamps = (
        payload.get("conversation_start_at"),
        payload.get("conversation_end_at"),
    )
    return bool(
        payload.get("projection_version") == 2
        and payload.get("projection_contract") == PROJECTION_CONTRACT
        and isinstance(source, str)
        and re.fullmatch(r"[\w:.$/-]{1,64}", source) is not None
        and isinstance(session_id, str)
        and re.fullmatch(r"[\w:.$/-]{1,256}", session_id) is not None
        and isinstance(event_ids, list)
        and len(event_ids) > 0
        and len(set(event_ids)) == len(event_ids)
        and all(_is_revision_id(item) for item in event_ids)
        and isinstance(logical_event_ids, list)
        and len(logical_event_ids) == len(event_ids)
        and len(set(logical_event_ids)) == len(logical_event_ids)
        and all(_is_logical_event_id(item) for item in logical_event_ids)
        and all(
            type(payload.get(field)) is int and payload[field] >= 0
            for field in count_fields
        )
        and payload["turn_start"] <= payload["turn_end"]
        and all(
            _is_nonnegative_finite_number(payload.get(field))
            for field in score_fields
        )
        and all(
            isinstance(value, str) and len(value) <= 64
            for value in timestamps
        )
        and isinstance(completeness_statuses, list)
        and all(
            isinstance(value, str)
            and re.fullmatch(r"[\w-]{0,64}", value) is not None
            for value in completeness_statuses
        )
        and payload.get("retention_state") == "active"
        and tags
        == [
            "raw-retention-projection",
            f"source={source}",
            "canonical=raw_events",
        ]
    )


def _valid_projection_preamble(
    prefix: bytes,
    *,
    path: Path,
    journal_metadata: dict[str, Any] | None,
    canonical_db_identity: Path | None,
    errors: list[str],
) -> dict[str, Any] | None:
    if len(prefix) > 32 * 1024:
        errors.append(f"{path}:projection_preamble_exceeds_byte_budget")
        return None
    try:
        text = prefix.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path}:projection_preamble_not_utf8")
        return None
    if not text.startswith("---\n"):
        errors.append(f"{path}:projection_frontmatter_missing")
        return None
    closing = text.find("\n---\n", 4)
    if closing < 0:
        errors.append(f"{path}:projection_frontmatter_unterminated")
        return None
    body = text[4:closing]
    raw_keys = re.findall(r"^([a-z_]+):", body, flags=re.MULTILINE)
    if len(raw_keys) != len(set(raw_keys)) or set(raw_keys) != _PROJECTION_FRONTMATTER_KEYS:
        errors.append(f"{path}:projection_frontmatter_schema_mismatch")
        return None
    loader = _UniqueKeySafeLoader(body)
    try:
        payload = loader.get_single_data()
    except (yaml.YAMLError, RecursionError):
        errors.append(f"{path}:projection_frontmatter_malformed")
        return None
    finally:
        loader.dispose()
    if (
        not isinstance(payload, dict)
        or tuple(payload) != _PROJECTION_FRONTMATTER_KEY_ORDER
        or set(payload) != _PROJECTION_FRONTMATTER_KEYS
    ):
        errors.append(f"{path}:projection_frontmatter_malformed")
        return None
    canonical_frontmatter = _frontmatter(payload)
    if text[: closing + len("\n---\n")] != canonical_frontmatter:
        errors.append(f"{path}:projection_frontmatter_not_canonical")
        return None
    source = payload.get("source")
    session_id = payload.get("session_id")
    canonical_db = payload.get("canonical_db")
    event_ids = payload.get("event_ids")
    logical_event_ids = payload.get("logical_event_ids")
    if not isinstance(canonical_db, str):
        errors.append(f"{path}:projection_preamble_contract_mismatch")
        return None
    try:
        encoded_path = Path(canonical_db)
        encoded_canonical_db_identity = (
            encoded_path.parent.resolve(strict=True) / encoded_path.name
        )
    except (OSError, RuntimeError, ValueError):
        errors.append(f"{path}:projection_canonical_db_identity_unresolvable")
        return None
    valid = (
        payload.get("mnemos_type") == "raw_retention_projection"
        and len(canonical_db) <= 1024
        and "\n" not in canonical_db
        and Path(canonical_db).is_absolute()
        and Path(canonical_db).name == "raw_events.db"
        and (
            canonical_db_identity is None
            or encoded_canonical_db_identity == canonical_db_identity
        )
        and _valid_projection_metadata_values(payload)
    )
    expected_suffix = f"# {source} / {session_id}\n\n{_PROJECTION_PREAMBLE_NOTICE}"
    if not valid or text[closing + len("\n---\n") :] != expected_suffix:
        errors.append(f"{path}:projection_preamble_contract_mismatch")
        return None
    if not isinstance(event_ids, list) or not isinstance(logical_event_ids, list):
        errors.append(f"{path}:projection_preamble_contract_mismatch")
        return None
    if journal_metadata is not None:
        journal_revision_ids = journal_metadata.get("revision_ids")
        journal_logical_event_ids = journal_metadata.get("logical_event_ids")
        journal_map = (
            dict(zip(journal_revision_ids, journal_logical_event_ids))
            if isinstance(journal_revision_ids, list)
            and isinstance(journal_logical_event_ids, list)
            else {}
        )
        if dict(zip(event_ids, logical_event_ids)) != journal_map:
            errors.append(f"{path}:projection_preamble_journal_mismatch")
            return None
    return payload


def _projection_internal_aggregate_matches(
    *,
    path: Path,
    preamble: dict[str, Any],
    events: dict[str, dict[str, Any]],
) -> bool:
    """Bind publisher sequence and path turn range to the parsed preamble."""
    headers = [
        record.get("header")
        for record in events.values()
        if isinstance(record.get("header"), dict)
    ]
    if len(headers) != len(events) or not headers:
        return False
    event_ids = list(events)
    logical_event_ids = [
        str(record.get("marker", {}).get("logical_event_id") or "")
        for record in events.values()
    ]
    suffix_match = re.search(
        r"_t(?P<start>[0-9]{4,})-(?P<end>[0-9]{4,})\.md$",
        path.name,
    )
    return bool(
        suffix_match is not None
        and int(suffix_match.group("start")) == preamble.get("turn_start")
        and int(suffix_match.group("end")) == preamble.get("turn_end")
        and preamble.get("event_ids") == event_ids
        and preamble.get("logical_event_ids") == logical_event_ids
    )


def _projection_canonical_aggregate_matches(
    *,
    relative_path: str,
    preamble: dict[str, Any],
    records: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> bool:
    bound_records: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for record in records:
        marker = record.get("marker")
        revision_id = (
            str(marker.get("event_id") or "")
            if isinstance(marker, dict)
            else ""
        )
        raw_ref = evidence.get(revision_id, {}).get("_projection_ref")
        if not isinstance(raw_ref, dict):
            return False
        bound_records.append((revision_id, raw_ref, evidence[revision_id]))
    if not bound_records:
        return False
    refs = [item[1] for item in bound_records]
    if (
        len({str(ref.get("source_agent") or "") for ref in refs}) != 1
        or len({str(ref.get("session_id") or "") for ref in refs}) != 1
    ):
        return False
    turns = [int(ref["turn_number"]) for ref in refs]
    timestamps = [str(ref.get("timestamp") or "") for ref in refs]
    timestamps = [value for value in timestamps if value]
    completeness_statuses = sorted(
        {str(ref.get("completeness_status") or "") for ref in refs}
    )
    canonical_sequence = sorted(
        bound_records,
        key=lambda item: (int(item[1]["turn_number"]), item[0]),
    )
    expected_event_ids = [item[0] for item in canonical_sequence]
    expected_logical_event_ids = [
        str(item[2].get("logical_event_id") or "")
        for item in canonical_sequence
    ]
    parsed_event_ids = [
        str(record.get("marker", {}).get("event_id") or "")
        for record in records
    ]
    latest_timestamp = max(timestamps, default="")
    date = projection_timestamp_path_segment(latest_timestamp)
    source = _safe_slug(str(refs[0]["source_agent"]), 24)
    session = _safe_slug(str(refs[0]["session_id"]), 36)
    first_logical_event_id = expected_logical_event_ids[0]
    chunk_id = _safe_slug(first_logical_event_id[:10] or "unknown", 12)
    expected_relative_path = (
        f"{source}/{date}/{source}_{session}_{chunk_id}"
        f"_t{min(turns):04d}-{max(turns):04d}.md"
    )
    return bool(
        relative_path == expected_relative_path
        and parsed_event_ids == expected_event_ids
        and preamble.get("event_ids") == expected_event_ids
        and preamble.get("logical_event_ids") == expected_logical_event_ids
        and preamble.get("source") == refs[0]["source_agent"]
        and preamble.get("session_id") == refs[0]["session_id"]
        and preamble.get("turn_start") == min(turns)
        and preamble.get("turn_end") == max(turns)
        and preamble.get("conversation_start_at")
        == (min(timestamps) if timestamps else "")
        and preamble.get("conversation_end_at")
        == (max(timestamps) if timestamps else "")
        and preamble.get("completeness_statuses") == completeness_statuses
    )


def _projection_metric_aggregate_evidence(
    *,
    relative_path: str,
    preamble: dict[str, Any],
    records: list[dict[str, Any]],
    evidence: dict[str, dict[str, Any]],
) -> dict[str, str] | None:
    metric_refs: list[dict[str, Any]] = []
    for record in records:
        marker = record.get("marker")
        revision_id = (
            str(marker.get("event_id") or "")
            if isinstance(marker, dict)
            else ""
        )
        raw_ref = evidence.get(revision_id, {}).get("_projection_metric_ref")
        if not isinstance(raw_ref, dict):
            # Superseded historical revisions do not have versioned metric
            # authority. They are already unexpected generation evidence and
            # must be replayed; do not invent current metrics for their past.
            return None
        metric_refs.append(raw_ref)
    if not metric_refs:
        return None
    expected = {
        "search_count": sum(int(ref["search_count"]) for ref in metric_refs),
        "result_count": sum(int(ref["result_count"]) for ref in metric_refs),
        "hit_count": sum(int(ref["hit_count"]) for ref in metric_refs),
        "view_count": sum(int(ref["view_count"]) for ref in metric_refs),
        "reference_count": sum(
            int(ref["reference_count"]) for ref in metric_refs
        ),
        "freshness_score": round(
            max(float(ref["freshness_score"]) for ref in metric_refs), 4
        ),
        "confidence": round(
            max(float(ref["confidence"]) for ref in metric_refs), 4
        ),
        "survival_score": round(
            max(float(ref["survival_score"]) for ref in metric_refs), 2
        ),
    }
    observed = {
        field: preamble.get(field)
        for field in (
            "search_count",
            "result_count",
            "hit_count",
            "view_count",
            "reference_count",
            "freshness_score",
            "confidence",
            "survival_score",
        )
    }
    if observed == expected:
        return None
    return {
        "relative_path": relative_path,
        "expected_projection_metric_aggregate_hash": _canonical_json_hash(
            expected
        ),
        "observed_projection_metric_aggregate_hash": _canonical_json_hash(
            observed
        ),
    }


_INDEX_FRONTMATTER_KEY_ORDER = _PROJECTION_FRONTMATTER_KEY_ORDER + (
    "part_count",
    "parts",
)
_PART_FRONTMATTER_KEY_ORDER = (
    "mnemos_type",
    "projection_version",
    "projection_contract",
    "canonical_db",
    "source",
    "session_id",
    "chunk_file",
    "part_index",
    "part_count",
    "turn_start",
    "turn_end",
    "retention_state",
    "tags",
)


def _parse_canonical_frontmatter(
    text: str,
    *,
    path: Path,
    key_order: tuple[str, ...],
    errors: list[str],
) -> tuple[dict[str, Any], str] | None:
    """Parse one canonical frontmatter block; return ``(payload, body)``."""
    if not text.startswith("---\n"):
        errors.append(f"{path}:projection_frontmatter_missing")
        return None
    closing = text.find("\n---\n", 4)
    if closing < 0:
        errors.append(f"{path}:projection_frontmatter_unterminated")
        return None
    body = text[4:closing]
    raw_keys = re.findall(r"^([a-z_]+):", body, flags=re.MULTILINE)
    if len(raw_keys) != len(set(raw_keys)) or set(raw_keys) != set(key_order):
        errors.append(f"{path}:projection_frontmatter_schema_mismatch")
        return None
    loader = _UniqueKeySafeLoader(body)
    try:
        payload = loader.get_single_data()
    except (yaml.YAMLError, RecursionError):
        errors.append(f"{path}:projection_frontmatter_malformed")
        return None
    finally:
        loader.dispose()
    if (
        not isinstance(payload, dict)
        or tuple(payload) != key_order
        or set(payload) != set(key_order)
    ):
        errors.append(f"{path}:projection_frontmatter_malformed")
        return None
    canonical_frontmatter = _frontmatter(payload)
    if text[: closing + len("\n---\n")] != canonical_frontmatter:
        errors.append(f"{path}:projection_frontmatter_not_canonical")
        return None
    return payload, text[closing + len("\n---\n") :]


def _valid_index_page(
    raw: bytes,
    *,
    path: Path,
    canonical_db_identity: Path | None,
    errors: list[str],
) -> dict[str, Any] | None:
    """Validate a paged projection index page and return its frontmatter."""
    if len(raw) > 4 * 1024 * 1024:
        errors.append(f"{path}:projection_index_exceeds_byte_budget")
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path}:projection_index_not_utf8")
        return None
    parsed = _parse_canonical_frontmatter(
        text,
        path=path,
        key_order=_INDEX_FRONTMATTER_KEY_ORDER,
        errors=errors,
    )
    if parsed is None:
        return None
    payload, remainder = parsed
    canonical_db = payload.get("canonical_db")
    if not isinstance(canonical_db, str):
        errors.append(f"{path}:projection_index_contract_mismatch")
        return None
    try:
        encoded_path = Path(canonical_db)
        encoded_canonical_db_identity = (
            encoded_path.parent.resolve(strict=True) / encoded_path.name
        )
    except (OSError, RuntimeError, ValueError):
        errors.append(f"{path}:projection_canonical_db_identity_unresolvable")
        return None
    part_count = payload.get("part_count")
    parts = payload.get("parts")
    base_name = path.name
    base_stem = base_name[: -len(".md")] if base_name.endswith(".md") else base_name
    expected_part_names = (
        [f"{base_stem}.part-{index:03d}.md" for index in range(1, part_count + 1)]
        if type(part_count) is int and part_count >= 1
        else []
    )
    valid_parts = bool(
        type(part_count) is int
        and part_count >= 1
        and isinstance(parts, list)
        and len(parts) == part_count
        and all(
            isinstance(entry, dict)
            and set(entry) == {"path", "bytes", "sha256"}
            and isinstance(entry.get("path"), str)
            and not Path(entry["path"]).is_absolute()
            and ".." not in Path(entry["path"]).parts
            and "\\" not in entry["path"]
            and Path(entry["path"]).as_posix() == entry["path"]
            and type(entry.get("bytes")) is int
            and entry["bytes"] >= 0
            and _is_hex_digest(entry.get("sha256"))
            for entry in parts
        )
        and [Path(entry["path"]).name for entry in parts] == expected_part_names
    )
    valid = (
        payload.get("mnemos_type") == PROJECTION_INDEX_MNEMOS_TYPE
        and len(canonical_db) <= 1024
        and "\n" not in canonical_db
        and Path(canonical_db).is_absolute()
        and Path(canonical_db).name == "raw_events.db"
        and (
            canonical_db_identity is None
            or encoded_canonical_db_identity == canonical_db_identity
        )
        and _valid_projection_metadata_values(payload)
        and valid_parts
    )
    expected_body = (
        render_projection_index_body(
            source_agent=str(payload.get("source") or ""),
            session_id=str(payload.get("session_id") or ""),
            base_stem=base_stem,
            part_count=part_count,
        )
        if type(part_count) is int and part_count >= 1
        else ""
    )
    if not valid or remainder != expected_body:
        errors.append(f"{path}:projection_index_contract_mismatch")
        return None
    return payload


def _valid_part_page(
    raw: bytes,
    *,
    path: Path,
    canonical_db_identity: Path | None,
    errors: list[str],
) -> tuple[dict[str, Any], bytes] | None:
    """Validate one paged projection part; return ``(frontmatter, content)``."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{path}:projection_part_not_utf8")
        return None
    parsed = _parse_canonical_frontmatter(
        text,
        path=path,
        key_order=_PART_FRONTMATTER_KEY_ORDER,
        errors=errors,
    )
    if parsed is None:
        return None
    payload, remainder = parsed
    source = payload.get("source")
    session_id = payload.get("session_id")
    canonical_db = payload.get("canonical_db")
    chunk_file = payload.get("chunk_file")
    part_index = payload.get("part_index")
    part_count = payload.get("part_count")
    turn_start = payload.get("turn_start")
    turn_end = payload.get("turn_end")
    tags = payload.get("tags")
    if not isinstance(canonical_db, str):
        errors.append(f"{path}:projection_part_contract_mismatch")
        return None
    try:
        encoded_path = Path(canonical_db)
        encoded_canonical_db_identity = (
            encoded_path.parent.resolve(strict=True) / encoded_path.name
        )
    except (OSError, RuntimeError, ValueError):
        errors.append(f"{path}:projection_canonical_db_identity_unresolvable")
        return None
    valid = (
        payload.get("mnemos_type") == "raw_retention_projection"
        and payload.get("projection_version") == 2
        and payload.get("projection_contract") == PROJECTION_CONTRACT
        and len(canonical_db) <= 1024
        and "\n" not in canonical_db
        and Path(canonical_db).is_absolute()
        and Path(canonical_db).name == "raw_events.db"
        and (
            canonical_db_identity is None
            or encoded_canonical_db_identity == canonical_db_identity
        )
        and isinstance(source, str)
        and re.fullmatch(r"[\w:.$/-]{1,64}", source) is not None
        and isinstance(session_id, str)
        and re.fullmatch(r"[\w:.$/-]{1,256}", session_id) is not None
        and isinstance(chunk_file, str)
        and not Path(chunk_file).is_absolute()
        and ".." not in Path(chunk_file).parts
        and "\\" not in chunk_file
        and chunk_file.endswith(".md")
        and PROJECTION_PART_PATH_PATTERN.search(chunk_file) is None
        and type(part_index) is int
        and part_index >= 1
        and type(part_count) is int
        and part_count >= part_index
        and type(turn_start) is int
        and type(turn_end) is int
        and 0 <= turn_start <= turn_end
        and payload.get("retention_state") == "active"
        and tags
        == [
            "raw-retention-projection",
            f"source={source}",
            "canonical=raw_events",
        ]
    )
    expected_prefix = (
        f"# {source} / {session_id} (part {part_index}/{part_count})\n\n"
        + _PROJECTION_PREAMBLE_NOTICE
    )
    if not valid or not remainder.startswith(expected_prefix):
        errors.append(f"{path}:projection_part_contract_mismatch")
        return None
    chunk_stem = Path(str(chunk_file)).name[: -len(".md")]
    expected_name = f"{chunk_stem}.part-{part_index:03d}.md"
    if path.name != expected_name:
        errors.append(f"{path}:projection_part_name_mismatch")
        return None
    content = remainder[len(expected_prefix) :]
    return payload, content.encode("utf-8")


def _classify_projection_file(path: Path) -> str:
    """Classify one managed projection file as ``single``/``index``/``part``."""
    try:
        prefix = _decode_utf8_prefix(read_native_bytes(path)[:4096])
    except UnicodeError as exc:
        raise ValueError(
            "managed Raw projection file is not valid UTF-8"
        ) from exc
    except OSError as exc:
        raise ValueError(
            "managed Raw projection file is unreadable"
        ) from exc
    if re.search(
        r'^mnemos_type:\s+["\']?raw_retention_projection_index["\']?\s*$',
        prefix,
        flags=re.MULTILINE,
    ):
        return "index"
    if PROJECTION_PART_PATH_PATTERN.search(path.name):
        return "part"
    return "single"


def _strip_field_continuation(
    content: bytes,
    *,
    part_path: Path,
    errors: list[str],
) -> bytes | None:
    """Remove and validate the continuation marker opening a field slice part."""
    line_end = content.find(b"\n")
    if line_end < 0:
        errors.append(f"{part_path}:malformed_field_continuation_marker")
        return None
    line = content[:line_end]
    if len(line) > 8 * 1024 or not line.endswith(b" -->"):
        errors.append(f"{part_path}:malformed_field_continuation_marker")
        return None
    marker = _decode_marker(line[len(_FIELD_CONT_PREFIX) : -4])
    if (
        not isinstance(marker, dict)
        or set(marker) != {"event_id", "field"}
        or not isinstance(marker.get("event_id"), str)
        or not marker["event_id"]
        or len(marker["event_id"]) > 128
        or marker.get("field") not in VISIBLE_FIELDS
    ):
        errors.append(f"{part_path}:malformed_field_continuation_marker")
        return None
    return content[line_end + 1 :]


def _parse_paged_projection(
    path: Path,
    *,
    raw_dir: Path | None = None,
    journal_files: dict[str, Any] | None = None,
    journal_metadata: dict[str, Any] | None = None,
    canonical_db_identity: Path | None = None,
    expected_relative_path: str | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], list[str]]:
    """Parse an index page and its ordered parts into the chunk event stream.

    Returns ``(events, errors, part_relative_paths)``.  Part bodies are
    concatenated in declared order after stripping each part's preamble and
    rejoining continuation-marked field slices; the reassembled document then
    goes through the same structural and field-hash checks as a single file.
    """
    events: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    raw = read_native_bytes(path)
    payload = _valid_index_page(
        raw,
        path=path,
        canonical_db_identity=canonical_db_identity,
        errors=errors,
    )
    if payload is None:
        return events, errors, []
    part_entries = payload["parts"]
    part_paths = [str(entry["path"]) for entry in part_entries]
    contents: list[bytes] = []
    for position, entry in enumerate(part_entries, start=1):
        relative_part = str(entry["path"])
        if expected_relative_path is not None and (
            Path(relative_part).parent.as_posix()
            != Path(expected_relative_path).parent.as_posix()
        ):
            errors.append(f"{path}:projection_part_path_mismatch:{relative_part}")
            return events, errors, part_paths
        if raw_dir is not None:
            if not _is_regular_file_without_symlink_escape(raw_dir, relative_part):
                errors.append(f"{path}:projection_part_path_unsafe:{relative_part}")
                return events, errors, part_paths
            part_path = raw_dir / relative_part
        else:
            part_path = path.parent / Path(relative_part).name
        try:
            part_raw = read_native_bytes(part_path)
        except OSError:
            errors.append(f"{path}:projection_part_unreadable:{relative_part}")
            return events, errors, part_paths
        part_hash = hashlib.sha256(part_raw).hexdigest()
        if len(part_raw) != entry["bytes"] or part_hash != entry["sha256"]:
            errors.append(f"{path}:projection_part_hash_mismatch:{relative_part}")
            return events, errors, part_paths
        if journal_files is not None:
            journal_record = journal_files.get(relative_part)
            if not isinstance(journal_record, dict):
                errors.append(
                    f"{path}:projection_part_journal_record_missing:{relative_part}"
                )
                return events, errors, part_paths
            if str(journal_record.get("content_hash") or "") != part_hash:
                errors.append(f"projection_journal_content_hash_mismatch:{relative_part}")
                return events, errors, part_paths
        parsed_part = _valid_part_page(
            part_raw,
            path=part_path,
            canonical_db_identity=canonical_db_identity,
            errors=errors,
        )
        if parsed_part is None:
            return events, errors, part_paths
        part_payload, content = parsed_part
        if (
            part_payload.get("part_index") != position
            or part_payload.get("part_count") != payload.get("part_count")
            or part_payload.get("source") != payload.get("source")
            or part_payload.get("session_id") != payload.get("session_id")
            or (
                expected_relative_path is not None
                and part_payload.get("chunk_file") != expected_relative_path
            )
        ):
            errors.append(f"{part_path}:projection_part_index_binding_mismatch")
            return events, errors, part_paths
        if content.startswith(_FIELD_CONT_PREFIX):
            if position == 1:
                errors.append(f"{part_path}:unexpected_field_continuation_marker")
                return events, errors, part_paths
            stripped = _strip_field_continuation(
                content,
                part_path=part_path,
                errors=errors,
            )
            if stripped is None:
                return events, errors, part_paths
            content = stripped
        contents.append(content)
    virtual_fields = {
        key: ("raw_retention_projection" if key == "mnemos_type" else payload[key])
        for key in _PROJECTION_FRONTMATTER_KEY_ORDER
    }
    virtual_raw = (
        _frontmatter(virtual_fields)
        + f"# {payload['source']} / {payload['session_id']}\n\n"
        + _PROJECTION_PREAMBLE_NOTICE
    ).encode("utf-8") + b"".join(contents)
    # The reassembled body ends with the final field suffix; the structural
    # parser expects the single-file layout, which rstrips that trailing blank.
    virtual_raw = virtual_raw.rstrip() + b"\n"
    events, parse_errors, _truncated = _parse_projection_bytes(
        virtual_raw,
        path=path,
        journal_metadata=journal_metadata,
        canonical_db_identity=canonical_db_identity,
    )
    errors.extend(parse_errors)
    return events, errors, part_paths


def _parse_projection_file(
    path: Path,
    *,
    journal_metadata: dict[str, Any] | None = None,
    canonical_db_identity: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    raw = read_native_bytes(path)
    file_kind = _classify_projection_file(path)
    if file_kind == "index":
        events, errors, _part_paths = _parse_paged_projection(
            path,
            journal_metadata=journal_metadata,
            canonical_db_identity=canonical_db_identity,
        )
        return events, errors, False
    if file_kind == "part":
        return {}, [f"{path}:projection_part_requires_index_page"], False
    return _parse_projection_bytes(
        raw,
        path=path,
        journal_metadata=journal_metadata,
        canonical_db_identity=canonical_db_identity,
    )


def _parse_projection_bytes(
    raw: bytes,
    *,
    path: Path,
    journal_metadata: dict[str, Any] | None = None,
    canonical_db_identity: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    events: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    cursor = 0
    first_event = True
    preamble_payload: dict[str, Any] = {}
    while True:
        structural_event = _next_structural_event(
            raw, cursor=cursor, path=path, errors=errors
        )
        if structural_event is None:
            if first_event:
                errors.append(f"{path}:projection_contains_no_structural_event")
            elif cursor != len(raw):
                errors.append(f"{path}:projection_has_unconsumed_trailing_bytes")
            break
        event_start, header_metadata, event, after_event = structural_event
        header_start = raw.rfind(b"\n## Turn ", 0, event_start)
        structural_start = header_start + 1
        if first_event:
            parsed_preamble = _valid_projection_preamble(
                raw[:structural_start],
                path=path,
                journal_metadata=journal_metadata,
                canonical_db_identity=canonical_db_identity,
                errors=errors,
            )
            preamble_payload = parsed_preamble or {}
            first_event = False
        elif raw[cursor:structural_start] != b"\n":
            errors.append(f"{path}:projection_has_unconsumed_inter_event_bytes")
        event_id = str(event.get("event_id") or "")
        if event_id in events:
            errors.append(f"{path}:duplicate_event_id:{event_id}")
        parsed_fields = _parse_event_fields(
            raw,
            cursor=after_event,
            event_id=event_id,
            path=path,
            errors=errors,
        )
        if parsed_fields is None:
            cursor = after_event
            continue
        fields, cursor = parsed_fields
        if event_id not in events:
            events[event_id] = {
                "header": header_metadata,
                "marker": event,
                "fields": fields,
                "preamble": preamble_payload,
            }
    if events and not _projection_internal_aggregate_matches(
        path=path,
        preamble=preamble_payload,
        events=events,
    ):
        errors.append(f"{path}:projection_preamble_aggregate_mismatch")
    # V1 truncation had no V2 event/field receipts, so it is detected by
    # missing canonical events.  A literal marker may be legitimate evidence
    # inside a V2 field and must not independently fail this audit.
    return events, errors, False


def audit_raw_projection_fidelity(
    *,
    raw_dir: Path,
    db_path: Path,
    canonical_db_identity: Path | None = None,
    include_eligible_delete: bool = False,
    include_gap_evidence: bool = False,
) -> dict[str, Any]:
    from scripts.raw_projection_fidelity_runtime import (
        audit_raw_projection_fidelity as runtime_audit,
    )

    return runtime_audit(
        raw_dir=raw_dir,
        db_path=db_path,
        canonical_db_identity=canonical_db_identity,
        include_eligible_delete=include_eligible_delete,
        include_gap_evidence=include_gap_evidence,
    )


def main(argv: list[str] | None = None) -> int:
    from scripts.raw_projection_fidelity_runtime import main as runtime_main

    return runtime_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
