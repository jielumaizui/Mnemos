"""Bounded runtime implementation for Raw projection fidelity auditing."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import zlib

from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.ops.durable_io import read_native_bytes
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger
from core.sync_framework.raw_event_identity_aliases import alias_table_exists
from core.sync_framework.raw_subject_deletion import (
    subject_deletion_table_exists,
    subject_deletion_visibility_predicate,
)
from scripts import audit_raw_projection_fidelity as fidelity_host
from scripts.audit_raw_projection_fidelity import (
    PROJECTION_JOURNAL_NAME,
    SCHEMA_VERSION,
    VISIBLE_FIELDS,
    _canonical_json_hash,
    _content_free_structural_evidence,
    _failure_report,
    _file_sha256,
    _is_regular_file_without_symlink_escape,
    _observed_projection_metadata,
    _observed_projection_runtime_ref,
    _observed_revision_evidence,
    _projection_canonical_aggregate_matches,
    _projection_metric_aggregate_evidence,
    _public_revision_evidence,
    _read_only_connection,
    _revision_lineage,
    _safe_managed_projection_paths,
    _sha256_text,
    _sqlite_inventory,
    _strict_json_loads,
    _validated_projection_metadata,
    _validated_projection_metric_ref,
    _validated_projection_ref,
    _validated_projection_runtime_ref,
    _validated_revision_metadata,
    _verified_projection_journal,
    structured_field_text,
)


def _canonical_revision_evidence(
    db_path: Path, *, include_eligible_delete: bool
) -> dict[str, dict[str, Any]]:
    """Return only expected visible-field digests, never a corpus body cache.

    A production Raw history can be multiple gigabytes.  The audit still has
    to inspect every snapshot, but retaining decoded payloads would make the
    verifier itself an out-of-memory failure mode.  Snapshot decoding remains
    one revision at a time; only the four deterministic visible-field hashes
    survive in the comparison map.
    """
    try:
        db_kind = inspect_path_kind(db_path)
    except DurableIOError:
        raise ValueError("raw_events.db is unavailable") from None
    if db_kind == "missing":
        raise ValueError("raw_events.db is missing")
    if db_kind != "file":
        raise ValueError("raw_events.db is not a regular file")
    query = """
        SELECT
            t.event_id,
            t.current_revision_id,
            r.logical_event_id,
            r.revision_number,
            r.supersedes_revision_id,
            r.content_hash,
            r.full_content_hash,
            r.snapshot_blob,
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
        FROM raw_turns AS t
        LEFT JOIN raw_turn_revisions AS r ON r.revision_id=t.current_revision_id
        LEFT JOIN raw_turn_revisions AS predecessor
            ON predecessor.revision_id=r.supersedes_revision_id
        LEFT JOIN raw_metrics AS m ON m.event_id=t.event_id
        WHERE 1=1
    """
    if not include_eligible_delete:
        query += " AND COALESCE(m.retention_state, 'active') != 'eligible_delete'"
    turns: dict[str, dict[str, Any]] = {}
    try:
        with _read_only_connection(db_path) as conn:
            if not subject_deletion_table_exists(conn):
                raise ValueError("raw subject deletion schema is missing")
            if alias_table_exists(conn):
                query += """
                    AND NOT EXISTS (
                        SELECT 1
                        FROM raw_event_identity_aliases a
                        WHERE a.alias_event_id=t.event_id
                    )
                """
            query += NativeRawContractLedger.current_event_visibility_predicate("t.event_id")
            query += subject_deletion_visibility_predicate("t.event_id")
            # Iteration is deliberate: fetchall() would retain every compressed
            # snapshot in memory before any body can be released.
            for (
                owner_logical_event_id,
                revision_id,
                logical_event_id,
                revision_number,
                supersedes_revision_id,
                content_hash,
                full_content_hash,
                blob,
                predecessor_logical_event_id,
                predecessor_revision_number,
                source_agent,
                session_id,
                turn_number,
                conversation_at,
                captured_at,
                completeness_status,
                search_count,
                result_count,
                hit_count,
                view_count,
                reference_count,
                freshness_score,
                confidence,
                survival_score,
            ) in conn.execute(query):
                metadata = _validated_revision_metadata(
                    revision_id=revision_id,
                    logical_event_id=logical_event_id,
                    revision_number=revision_number,
                    supersedes_revision_id=supersedes_revision_id,
                    content_hash=content_hash,
                    full_content_hash=full_content_hash,
                )
                if owner_logical_event_id != metadata["logical_event_id"]:
                    raise ValueError(
                        f"raw revision {revision_id} has a cross-linked current owner"
                    )
                if str(revision_id) in turns:
                    raise ValueError(
                        f"raw revision {revision_id} has duplicate current owners"
                    )
                if metadata["revision_number"] > 0 and (
                    predecessor_logical_event_id != metadata["logical_event_id"]
                    or type(predecessor_revision_number) is not int
                    or predecessor_revision_number
                    != metadata["revision_number"] - 1
                ):
                    raise ValueError(
                        f"raw revision {revision_id} has an invalid direct predecessor"
                    )
                try:
                    payload = _strict_json_loads(
                        zlib.decompress(blob).decode("utf-8")
                    )
                except (
                    TypeError,
                    ValueError,
                    zlib.error,
                    UnicodeDecodeError,
                    RecursionError,
                ) as exc:
                    raise ValueError(
                        f"raw revision {revision_id} snapshot is unreadable"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ValueError(f"raw revision {revision_id} snapshot is malformed")
                try:
                    projection_metadata = _validated_projection_metadata(payload)
                    projection_ref = _validated_projection_ref(
                        source_agent=source_agent,
                        session_id=session_id,
                        turn_number=turn_number,
                        conversation_at=conversation_at,
                        captured_at=captured_at,
                        completeness_status=completeness_status,
                    )
                    projection_runtime_ref = _validated_projection_runtime_ref(
                        search_count=search_count,
                        result_count=result_count,
                        hit_count=hit_count,
                        reference_count=reference_count,
                        survival_score=survival_score,
                    )
                    projection_metric_ref = _validated_projection_metric_ref(
                        search_count=search_count,
                        result_count=result_count,
                        hit_count=hit_count,
                        view_count=view_count,
                        reference_count=reference_count,
                        freshness_score=freshness_score,
                        confidence=confidence,
                        survival_score=survival_score,
                    )
                    expected_values = {
                        "user_content": str(payload.get("user_content") or ""),
                        "assistant_content": str(
                            payload.get("assistant_content") or ""
                        ),
                        "reasoning": str(payload.get("reasoning") or ""),
                        "structured": structured_field_text(payload),
                    }
                except (RecursionError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"raw revision {revision_id} snapshot is unreadable"
                    ) from exc
                turns[str(revision_id)] = {
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
                    "visible_field_hashes": {
                        field: _sha256_text(value)
                        for field, value in expected_values.items()
                    },
                }
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"raw_events.db is unreadable: {exc.__class__.__name__}") from exc
    return turns


def audit_raw_projection_fidelity(
    *,
    raw_dir: Path,
    db_path: Path,
    canonical_db_identity: Path | None = None,
    include_eligible_delete: bool = False,
    include_gap_evidence: bool = False,
) -> dict[str, Any]:
    """Return counts and samples for canonical Raw vs visible Markdown fidelity."""
    errors: list[str] = []
    try:
        identity_path = canonical_db_identity or db_path
        canonical_db_identity = (
            identity_path.parent.resolve(strict=True) / identity_path.name
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _failure_report(
            raw_dir=raw_dir,
            db_path=db_path,
            error=f"canonical DB identity is unresolvable: {exc.__class__.__name__}",
        )
    try:
        db_inventory_before = _sqlite_inventory(db_path)
        canonical_evidence = _canonical_revision_evidence(
            db_path,
            include_eligible_delete=include_eligible_delete,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _failure_report(raw_dir=raw_dir, db_path=db_path, error=str(exc))
    canonical = {
        revision_id: dict(record["visible_field_hashes"])
        for revision_id, record in canonical_evidence.items()
    }
    observed: dict[str, dict[str, Any]] = {}
    duplicate_events: set[str] = set()
    truncated_marker_files = 0
    try:
        journal, journal_paths, journal_errors = _verified_projection_journal(raw_dir)
        fallback_paths = _safe_managed_projection_paths(raw_dir)
        projection_paths = journal_paths or fallback_paths
        projection_inventory_before = fidelity_host._projection_inventory(
            raw_dir,
            projection_paths,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _failure_report(raw_dir=raw_dir, db_path=db_path, error=str(exc))
    errors.extend(journal_errors)
    if journal_paths and set(fallback_paths) != set(journal_paths):
        errors.append("projection_journal_managed_path_set_mismatch")
    journal_files = journal.get("files")
    journal_file_records = journal_files if isinstance(journal_files, dict) else {}
    publisher_generation_hash = str(journal.get("generation_hash") or "")
    try:
        publisher_journal_hash = (
            hashlib.sha256(
                read_native_bytes(raw_dir / PROJECTION_JOURNAL_NAME)
            ).hexdigest()
            if _is_regular_file_without_symlink_escape(
                raw_dir, PROJECTION_JOURNAL_NAME
            )
            else ""
        )
    except OSError:
        publisher_journal_hash = ""
    consumed_part_paths: set[str] = set()
    pending_part_paths: list[str] = []
    for relative_path in projection_paths:
        path = raw_dir / relative_path
        if not _is_regular_file_without_symlink_escape(raw_dir, relative_path):
            errors.append(f"projection_journal_path_unsafe:{relative_path}")
            continue
        try:
            raw_journal_metadata = journal_file_records.get(relative_path)
            parse_journal_metadata = (
                raw_journal_metadata
                if isinstance(raw_journal_metadata, dict)
                else None
            )
            file_kind = fidelity_host._classify_projection_file(path)
            if file_kind == "part":
                # Paged projection parts are verified through their index page.
                pending_part_paths.append(relative_path)
                continue
            if file_kind == "index":
                parsed, parse_errors, declared_parts = (
                    fidelity_host._parse_paged_projection(
                        path,
                        raw_dir=raw_dir,
                        journal_files=(journal_file_records if journal else None),
                        journal_metadata=parse_journal_metadata,
                        canonical_db_identity=canonical_db_identity,
                        expected_relative_path=relative_path,
                    )
                )
                for declared_part in declared_parts:
                    if declared_part in consumed_part_paths:
                        errors.append(
                            f"projection_part_duplicate_reference:{declared_part}"
                        )
                    consumed_part_paths.add(declared_part)
                has_truncation_marker = False
            else:
                parsed, parse_errors, has_truncation_marker = (
                    fidelity_host._parse_projection_file(
                        path,
                        journal_metadata=parse_journal_metadata,
                        canonical_db_identity=canonical_db_identity,
                    )
                )
        except (OSError, RecursionError, RuntimeError, ValueError) as exc:
            errors.append(
                f"{relative_path}:projection_parse_failed:{exc.__class__.__name__}"
            )
            continue
        errors.extend(parse_errors)
        truncated_marker_files += int(has_truncation_marker)
        journal_metadata = journal_file_records.get(relative_path)
        if isinstance(journal_metadata, dict):
            parsed_revision_ids = list(parsed)
            parsed_logical_event_ids = [
                str(record.get("marker", {}).get("logical_event_id") or "")
                for record in parsed.values()
            ]
            raw_journal_revision_ids = journal_metadata.get("revision_ids")
            raw_journal_logical_event_ids = journal_metadata.get("logical_event_ids")
            journal_revision_ids = (
                raw_journal_revision_ids
                if isinstance(raw_journal_revision_ids, list)
                else []
            )
            journal_logical_event_ids = (
                raw_journal_logical_event_ids
                if isinstance(raw_journal_logical_event_ids, list)
                else []
            )
            try:
                projected_file_hash = _file_sha256(path)
            except OSError:
                projected_file_hash = ""
                errors.append(f"projection_journal_file_unreadable:{relative_path}")
            if projected_file_hash != str(journal_metadata.get("content_hash") or ""):
                errors.append(
                    f"projection_journal_content_hash_mismatch:{relative_path}"
                )
            if set(parsed_revision_ids) != set(journal_revision_ids):
                errors.append(
                    f"projection_journal_revision_ids_mismatch:{relative_path}"
                )
            parsed_revision_logical_map = dict(
                zip(parsed_revision_ids, parsed_logical_event_ids)
            )
            journal_revision_logical_map = dict(
                zip(journal_revision_ids, journal_logical_event_ids)
            )
            if parsed_revision_logical_map != journal_revision_logical_map:
                errors.append(
                    f"projection_journal_logical_event_ids_mismatch:{relative_path}"
                )
        for event_id, record in parsed.items():
            record["_projection_path"] = relative_path
            if event_id in observed:
                duplicate_events.add(event_id)
            else:
                observed[event_id] = record

    for pending_part in pending_part_paths:
        if pending_part not in consumed_part_paths:
            errors.append(f"projection_part_orphan:{pending_part}")

    expected_ids = set(canonical)
    observed_ids = set(observed)
    missing = sorted(expected_ids - observed_ids)
    unexpected = sorted(observed_ids - expected_ids)
    try:
        unexpected_lineage = _revision_lineage(db_path, set(unexpected))
    except ValueError as exc:
        errors.append(str(exc))
        unexpected_lineage = {}
    aggregate_evidence = {**canonical_evidence, **unexpected_lineage}
    records_by_path: dict[str, list[dict[str, Any]]] = {}
    for record in observed.values():
        relative_path = str(record.get("_projection_path") or "")
        records_by_path.setdefault(relative_path, []).append(record)
    projection_metric_aggregate_mismatch_evidence: list[dict[str, str]] = []
    for relative_path, records in sorted(records_by_path.items()):
        raw_preamble = records[0].get("preamble")
        preamble = raw_preamble if isinstance(raw_preamble, dict) else {}
        if not _projection_canonical_aggregate_matches(
            relative_path=relative_path,
            preamble=preamble,
            records=records,
            evidence=aggregate_evidence,
        ):
            errors.append(
                f"{relative_path}:projection_preamble_canonical_aggregate_mismatch"
            )
        metric_mismatch = _projection_metric_aggregate_evidence(
            relative_path=relative_path,
            preamble=preamble,
            records=records,
            evidence=aggregate_evidence,
        )
        if metric_mismatch is not None:
            projection_metric_aggregate_mismatch_evidence.append(
                metric_mismatch
            )
    structural_errors = list(errors)
    structural_error_evidence = _content_free_structural_evidence(
        structural_errors
    )
    errors.extend(
        "projection_metric_aggregate_mismatch:"
        + record["relative_path"]
        for record in projection_metric_aggregate_mismatch_evidence
    )
    mismatches: list[str] = []
    logical_event_id_mismatches: list[str] = []
    projection_metadata_mismatches: list[str] = []
    projection_reference_mismatches: list[str] = []
    fields_checked = 0
    for event_id in sorted(expected_ids & observed_ids):
        expected_hashes = canonical[event_id]
        marker = observed[event_id]["marker"]
        observed_logical_event_id = (
            str(marker.get("logical_event_id") or "")
            if isinstance(marker, dict)
            else ""
        )
        if (
            observed_logical_event_id
            != canonical_evidence[event_id]["logical_event_id"]
        ):
            logical_event_id_mismatches.append(event_id)
        if (
            _observed_projection_metadata(observed[event_id])
            != canonical_evidence[event_id]["_projection_metadata"]
        ):
            projection_metadata_mismatches.append(event_id)
        if (
            _observed_projection_runtime_ref(observed[event_id])
            != canonical_evidence[event_id]["_projection_runtime_ref"]
        ):
            projection_reference_mismatches.append(event_id)
        marker_hashes = marker.get("field_hashes") if isinstance(marker, dict) else None
        fields = observed[event_id]["fields"]
        for field in VISIBLE_FIELDS:
            expected_hash = expected_hashes[field]
            field_record = fields.get(field)
            if not isinstance(field_record, dict):
                mismatches.append(f"{event_id}:{field}:missing")
                continue
            actual_hash = str(field_record.get("content_hash") or "")
            raw_field_marker = field_record.get("marker")
            field_marker: dict[str, Any] = (
                dict(raw_field_marker)
                if isinstance(raw_field_marker, dict)
                else {}
            )
            marker_hash = marker_hashes.get(field) if isinstance(marker_hashes, dict) else ""
            if (
                actual_hash != expected_hash
                or field_marker.get("sha256") != expected_hash
                or marker_hash != expected_hash
            ):
                mismatches.append(f"{event_id}:{field}:hash_mismatch")
                continue
            fields_checked += 1
    truncated_events = sum(1 for issue in mismatches if ":truncated" in issue)
    projection_reference_mismatch_evidence = [
        {
            "revision_id": revision_id,
            "logical_event_id": canonical_evidence[revision_id][
                "logical_event_id"
            ],
            "expected_projection_reference_hash": canonical_evidence[
                revision_id
            ]["projection_reference_hash"],
            "observed_projection_reference_hash": _canonical_json_hash(
                _observed_projection_runtime_ref(observed[revision_id])
            ),
        }
        for revision_id in projection_reference_mismatches
    ]
    errors.extend(f"missing_event:{event_id}" for event_id in missing)
    errors.extend(f"unexpected_event:{event_id}" for event_id in unexpected)
    errors.extend(f"duplicate_event:{event_id}" for event_id in sorted(duplicate_events))
    errors.extend(f"field:{issue}" for issue in mismatches)
    errors.extend(
        f"logical_event_id_mismatch:{event_id}"
        for event_id in logical_event_id_mismatches
    )
    errors.extend(
        f"projection_metadata_mismatch:{event_id}"
        for event_id in projection_metadata_mismatches
    )
    errors.extend(
        f"projection_reference_mismatch:{event_id}"
        for event_id in projection_reference_mismatches
    )
    if truncated_marker_files:
        errors.append("truncation_marker_present")
    missing_evidence = [
        _public_revision_evidence(canonical_evidence[revision_id])
        for revision_id in missing
    ]
    try:
        db_inventory_after = _sqlite_inventory(db_path)
        _journal_after, journal_paths_after, _journal_errors_after = (
            _verified_projection_journal(raw_dir)
        )
        fallback_paths_after = _safe_managed_projection_paths(raw_dir)
        projection_paths_after = journal_paths_after or fallback_paths_after
        projection_inventory_after = fidelity_host._projection_inventory(
            raw_dir,
            projection_paths_after,
        )
        evidence_epoch_stable = (
            db_inventory_after == db_inventory_before
            and projection_paths_after == projection_paths
            and projection_inventory_after == projection_inventory_before
        )
    except (OSError, RuntimeError, ValueError):
        evidence_epoch_stable = False
    if not evidence_epoch_stable:
        errors.append("raw_projection_evidence_epoch_changed_during_audit")
    unexpected_evidence: list[dict[str, Any]] = []
    for revision_id in unexpected:
        record = _observed_revision_evidence(revision_id, observed[revision_id])
        record["observed_logical_event_id"] = record.pop("logical_event_id")
        record["observed_visible_field_hashes"] = record.pop("visible_field_hashes")
        record["observed_projection_metadata_hash"] = record.pop(
            "projection_metadata_hash"
        )
        record["observed_projection_reference_hash"] = record.pop(
            "projection_reference_hash"
        )
        lineage = unexpected_lineage.get(revision_id)
        if lineage is not None:
            record.update(_public_revision_evidence(lineage))
        unexpected_evidence.append(record)
    observed_evidence = [
        _observed_revision_evidence(revision_id, observed[revision_id])
        for revision_id in sorted(observed)
    ]
    missing_new_count = sum(
        int(record["revision_number"] == 0) for record in missing_evidence
    )
    missing_replacement_count = sum(
        int(record["revision_number"] > 0) for record in missing_evidence
    )
    unexpected_superseded_field_mismatch_count = sum(
        int(
            bool(record.get("canonical_visible_field_hashes"))
            and record.get("observed_visible_field_hashes")
            != record.get("canonical_visible_field_hashes")
        )
        for record in unexpected_evidence
    )
    missing_ids = set(missing)
    unexpected_logical_event_id_mismatch_count = sum(
        int(
            bool(record.get("logical_event_id"))
            and record.get("observed_logical_event_id") != record.get("logical_event_id")
        )
        for record in unexpected_evidence
    )
    unexpected_projection_metadata_mismatch_count = sum(
        int(
            bool(record.get("projection_metadata_hash"))
            and record.get("observed_projection_metadata_hash")
            != record.get("projection_metadata_hash")
        )
        for record in unexpected_evidence
    )
    unexpected_projection_reference_mismatch_evidence = [
        {
            "revision_id": str(record["revision_id"]),
            "logical_event_id": str(record.get("logical_event_id") or ""),
            "expected_projection_reference_hash": str(
                record.get("projection_reference_hash") or ""
            ),
            "observed_projection_reference_hash": str(
                record.get("observed_projection_reference_hash") or ""
            ),
        }
        for record in unexpected_evidence
        if record.get("projection_reference_hash")
        and record.get("observed_projection_reference_hash")
        != record.get("projection_reference_hash")
    ]
    if unexpected_projection_reference_mismatch_evidence:
        projection_reference_mismatches.extend(
            item["revision_id"]
            for item in unexpected_projection_reference_mismatch_evidence
        )
        projection_reference_mismatch_evidence.extend(
            unexpected_projection_reference_mismatch_evidence
        )
    paired_superseded_revision_count = sum(
        int(
            record.get("current_revision_id") in missing_ids
            and canonical_evidence.get(
                str(record.get("current_revision_id") or ""), {}
            ).get("logical_event_id")
            == record.get("logical_event_id")
            and canonical_evidence.get(
                str(record.get("current_revision_id") or ""), {}
            ).get("supersedes_revision_id")
            == record.get("revision_id")
            and canonical_evidence.get(
                str(record.get("current_revision_id") or ""), {}
            ).get("revision_number")
            == int(record.get("revision_number") or 0) + 1
        )
        for record in unexpected_evidence
    )
    unpaired_superseded_revision_count = sum(
        int(
            bool(record.get("current_revision_id"))
            and record.get("current_revision_id") != record["revision_id"]
            and not (
                record.get("current_revision_id") in missing_ids
                and canonical_evidence.get(
                    str(record.get("current_revision_id") or ""), {}
                ).get("logical_event_id")
                == record.get("logical_event_id")
                and canonical_evidence.get(
                    str(record.get("current_revision_id") or ""), {}
                ).get("supersedes_revision_id")
                == record.get("revision_id")
                and canonical_evidence.get(
                    str(record.get("current_revision_id") or ""), {}
                ).get("revision_number")
                == int(record.get("revision_number") or 0) + 1
            )
        )
        for record in unexpected_evidence
    )
    unexpected_superseded_count = sum(
        int(
            bool(record.get("current_revision_id"))
            and record.get("current_revision_id") != record["revision_id"]
            and record.get("current_revision_id") in missing_ids
            and canonical_evidence.get(
                str(record.get("current_revision_id") or ""), {}
            ).get("logical_event_id")
            == record.get("logical_event_id")
            and canonical_evidence.get(
                str(record.get("current_revision_id") or ""), {}
            ).get("supersedes_revision_id")
            == record.get("revision_id")
            and canonical_evidence.get(
                str(record.get("current_revision_id") or ""), {}
            ).get("revision_number")
            == int(record.get("revision_number") or 0) + 1
            and record.get("observed_logical_event_id")
            == record.get("logical_event_id")
            and record.get("observed_projection_metadata_hash")
            == record.get("projection_metadata_hash")
            and record.get("observed_projection_reference_hash")
            == record.get("projection_reference_hash")
            and record.get("observed_visible_field_hashes")
            == record.get("canonical_visible_field_hashes")
        )
        for record in unexpected_evidence
    )
    unknown_unexpected_count = sum(
        int(revision_id not in unexpected_lineage) for revision_id in unexpected
    )
    if unexpected_superseded_field_mismatch_count:
        errors.append("unexpected_superseded_revision_field_hash_mismatch")
    if unexpected_logical_event_id_mismatch_count:
        errors.append("unexpected_revision_logical_event_id_mismatch")
    if unexpected_projection_metadata_mismatch_count:
        errors.append("unexpected_revision_projection_metadata_mismatch")
    if unexpected_projection_reference_mismatch_evidence:
        errors.append("unexpected_revision_projection_reference_mismatch")
    if unpaired_superseded_revision_count:
        errors.append("unexpected_superseded_revision_unpaired_with_missing_current")
    if not evidence_epoch_stable:
        classification = "evidence_epoch_changed"
    elif not errors:
        classification = "in_sync"
    elif (
        missing
        and not structural_errors
        and not duplicate_events
        and not mismatches
        and not projection_reference_mismatches
        and not projection_metric_aggregate_mismatch_evidence
        and not truncated_marker_files
        and unknown_unexpected_count == 0
        and unexpected_superseded_count == len(unexpected)
        and missing_new_count + missing_replacement_count == len(missing)
    ):
        classification = "projection_generation_stale"
    elif (
        structural_errors
        or duplicate_events
        or mismatches
        or logical_event_id_mismatches
        or projection_metadata_mismatches
        or projection_reference_mismatches
        or projection_metric_aggregate_mismatch_evidence
        or truncated_marker_files
        or unexpected_superseded_field_mismatch_count
        or unexpected_logical_event_id_mismatch_count
        or unexpected_projection_metadata_mismatch_count
        or unpaired_superseded_revision_count
    ):
        classification = "projection_content_or_structure_invalid"
    else:
        classification = "projection_gap_unclassified"
    gap_generation = {
        "classification": classification,
        "expected_revision_set_hash": _canonical_json_hash(sorted(expected_ids)),
        "observed_revision_set_hash": _canonical_json_hash(sorted(observed_ids)),
        "expected_revision_evidence_hash": _canonical_json_hash(
            [
                _public_revision_evidence(canonical_evidence[revision_id])
                for revision_id in sorted(canonical_evidence)
            ]
        ),
        "observed_revision_evidence_hash": _canonical_json_hash(observed_evidence),
        "missing_revision_evidence_hash": _canonical_json_hash(missing_evidence),
        "unexpected_revision_evidence_hash": _canonical_json_hash(unexpected_evidence),
        "missing_new_logical_event_count": missing_new_count,
        "missing_replacement_revision_count": missing_replacement_count,
        "unexpected_superseded_revision_count": unexpected_superseded_count,
        "paired_superseded_revision_count": paired_superseded_revision_count,
        "unexpected_superseded_field_mismatch_count": (
            unexpected_superseded_field_mismatch_count
        ),
        "unpaired_superseded_revision_count": unpaired_superseded_revision_count,
        "unknown_unexpected_revision_count": unknown_unexpected_count,
        "logical_event_id_mismatch_count": (
            len(logical_event_id_mismatches)
            + unexpected_logical_event_id_mismatch_count
        ),
        "projection_metadata_mismatch_count": (
            len(projection_metadata_mismatches)
            + unexpected_projection_metadata_mismatch_count
        ),
        "projection_reference_mismatch_count": len(
            projection_reference_mismatches
        ),
        "projection_reference_mismatch_evidence_hash": _canonical_json_hash(
            projection_reference_mismatch_evidence
        ),
        "projection_metric_aggregate_mismatch_count": len(
            projection_metric_aggregate_mismatch_evidence
        ),
        "projection_metric_aggregate_mismatch_evidence_hash": (
            _canonical_json_hash(
                projection_metric_aggregate_mismatch_evidence
            )
        ),
        "structural_error_count": len(structural_error_evidence),
        "structural_error_evidence_hash": _canonical_json_hash(
            structural_error_evidence
        ),
        "publisher_generation_hash": publisher_generation_hash,
        "publisher_journal_hash": publisher_journal_hash,
        "canonical_db_identity_hash": _sha256_text(
            str(canonical_db_identity)
        ),
        "evidence_epoch_stable": evidence_epoch_stable,
    }
    gap_generation["gap_hash"] = _canonical_json_hash(gap_generation)
    report = {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "raw_dir": str(raw_dir),
        "db_path": str(db_path),
        "expected_event_ids": len(expected_ids),
        "observed_event_ids": len(observed_ids),
        "missing_event_ids": len(missing),
        "duplicate_event_ids": len(duplicate_events),
        "unexpected_event_ids": len(unexpected),
        "truncated_events": truncated_events,
        "truncated_marker_files": truncated_marker_files,
        "field_hash_mismatch_count": (
            len(mismatches) + unexpected_superseded_field_mismatch_count
        ),
        "logical_event_id_mismatch_count": (
            len(logical_event_id_mismatches)
            + unexpected_logical_event_id_mismatch_count
        ),
        "projection_metadata_mismatch_count": (
            len(projection_metadata_mismatches)
            + unexpected_projection_metadata_mismatch_count
        ),
        "projection_reference_mismatch_count": len(
            projection_reference_mismatches
        ),
        "projection_metric_aggregate_mismatch_count": len(
            projection_metric_aggregate_mismatch_evidence
        ),
        "structural_error_count": len(structural_error_evidence),
        "visible_fields_checked": fields_checked,
        "error_count": len(errors),
        "errors": errors[:100],
        "missing_event_id_samples": missing[:20],
        "unexpected_event_id_samples": unexpected[:20],
        "gap_generation": gap_generation,
    }
    if include_gap_evidence:
        report["missing_revision_evidence"] = missing_evidence
        report["unexpected_revision_evidence"] = unexpected_evidence
        report["projection_reference_mismatch_evidence"] = (
            projection_reference_mismatch_evidence
        )
        report["projection_metric_aggregate_mismatch_evidence"] = (
            projection_metric_aggregate_mismatch_evidence
        )
        report["structural_error_evidence"] = structural_error_evidence
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the Raw projection fidelity audit command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument(
        "--canonical-db-identity",
        type=Path,
        help=(
            "Canonical DB path encoded in the publisher preamble; required when "
            "--db-path is a checkpointed evidence-epoch copy"
        ),
    )
    parser.add_argument("--include-eligible-delete", action="store_true")
    parser.add_argument(
        "--include-gap-evidence",
        action="store_true",
        help=(
            "Include every missing/unexpected revision ID, lineage field, and digest; "
            "never includes Raw body bytes"
        ),
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = audit_raw_projection_fidelity(
        raw_dir=args.raw_dir,
        db_path=args.db_path,
        canonical_db_identity=args.canonical_db_identity,
        include_eligible_delete=args.include_eligible_delete,
        include_gap_evidence=args.include_gap_evidence,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(report)
    return 0 if not args.strict or report["ok"] else 1
