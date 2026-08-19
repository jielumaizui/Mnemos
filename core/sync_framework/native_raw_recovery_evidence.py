"""Content-free conservation evidence for Native-to-Raw recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Mapping

from core.ops.readiness_query_budget import connect_readonly_sqlite
from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger


class NativeRawRecoveryEvidenceError(RuntimeError):
    """Fail-closed evidence construction error."""


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _hash_sql_row(
    columns: list[str],
    row: tuple[Any, ...],
) -> str:
    normalized = [
        (
            {"blob_sha256": hashlib.sha256(value).hexdigest()}
            if isinstance(value, bytes)
            else value
        )
        for value in row
    ]
    return _canonical_hash({"columns": columns, "values": normalized})


def _json_value(value: Any) -> Any:
    return json.loads(str(value or "null"))


def _compressed_text(value: Any) -> str:
    if not isinstance(value, bytes):
        raise ValueError("compressed Raw field is not bytes")
    return zlib.decompress(value).decode("utf-8")


def _revision_projection_matches(
    *,
    columns: list[str],
    row: tuple[Any, ...],
    revision: tuple[Any, ...],
    event_id: str,
) -> bool:
    """Bind every stable current Raw projection field to its revision snapshot."""
    try:
        snapshot = json.loads(zlib.decompress(revision[3]).decode("utf-8"))
        if not isinstance(snapshot, Mapping):
            return False
        row_map = dict(zip(columns, row, strict=True))
        row_projection = {
            "event_id": row_map["event_id"],
            "source_agent": row_map["source_agent"],
            "session_id": row_map["session_id"],
            "turn_number": row_map["turn_number"],
            "model_tag": row_map["model_tag"],
            "conversation_at": row_map["conversation_at"],
            "captured_at": row_map["captured_at"],
            "origin": row_map["origin"],
            "source_path": row_map["source_path"],
            "source_files": _json_value(row_map["source_files_json"]),
            "content_hash": row_map["content_hash"],
            "full_content_hash": row_map["full_content_hash"],
            "completeness": _json_value(row_map["completeness_json"]),
            "tool_calls": _json_value(row_map["tool_calls_json"]),
            "tool_results": _json_value(row_map["tool_results_json"]),
            "attachments": _json_value(row_map["attachments_json"]),
            "raw_event_refs": _json_value(row_map["raw_event_refs_json"]),
            "reasoning": _compressed_text(row_map["reasoning_blob"]),
            "user_content": _compressed_text(row_map["user_content_blob"]),
            "assistant_content": _compressed_text(row_map["assistant_content_blob"]),
            "compression": row_map["compression"],
            "raw_bytes": row_map["raw_bytes"],
            "quality_rank": row_map["quality_rank"],
        }
        snapshot_projection = {key: snapshot.get(key) for key in row_projection}
        return bool(
            str(revision[0] or "") == event_id
            and str(revision[1] or "") == str(row_map["content_hash"] or "")
            and str(revision[2] or "") == str(row_map["full_content_hash"] or "")
            and row_projection == snapshot_projection
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        zlib.error,
    ):
        return False


def _effective_projection_matches(
    *,
    columns: list[str],
    row: tuple[Any, ...],
    revision: tuple[Any, ...],
    latest: Mapping[str, Any] | None,
) -> bool:
    """Bind effective status/metadata/time to the latest native observation."""
    try:
        snapshot = json.loads(zlib.decompress(revision[3]).decode("utf-8"))
        if not isinstance(snapshot, Mapping):
            return False
        row_map = dict(zip(columns, row, strict=True))
        supplied_metadata = snapshot.get("metadata")
        expected_metadata = (
            dict(supplied_metadata) if isinstance(supplied_metadata, Mapping) else {}
        )
        expected_status = str(snapshot.get("completeness_status") or "partial")
        expected_updated_at = str(snapshot.get("updated_at") or "")
        if latest is not None:
            contract_errors = latest.get("contract_errors")
            if not isinstance(contract_errors, list) or any(
                not isinstance(error, str) or not error for error in contract_errors
            ):
                return False
            contract_state = str(latest.get("contract_state") or "")
            if (
                contract_state not in {"conformant", "nonconforming"}
                or (contract_state == "conformant") != (not contract_errors)
                or not str(latest.get("observation_id") or "")
                or not str(latest.get("observed_revision_id") or "")
                or not str(latest.get("support_manifest_hash") or "")
                or not str(latest.get("observed_at") or "")
            ):
                return False
            expected_metadata.update(
                {
                    "support_current_revision_raw_contract_state": expected_metadata.get(
                        "support_raw_contract_state", ""
                    ),
                    "support_current_revision_raw_contract_errors": expected_metadata.get(
                        "support_raw_contract_errors", []
                    ),
                    "support_raw_contract_state": contract_state,
                    "support_raw_contract_errors": contract_errors,
                    "support_latest_native_contract_observation_id": str(
                        latest.get("observation_id") or ""
                    ),
                    "support_latest_native_contract_state": contract_state,
                    "support_latest_native_contract_errors": contract_errors,
                    "support_latest_native_contract_observed_at": str(
                        latest.get("observed_at") or ""
                    ),
                    "support_native_contract_certifying": contract_state == "conformant",
                }
            )
            if contract_state != "conformant":
                expected_status = "partial"
            expected_updated_at = str(latest.get("observed_at") or "")
        return bool(
            _json_value(row_map["metadata_json"]) == expected_metadata
            and str(row_map["completeness_status"] or "") == expected_status
            and str(row_map["updated_at"] or "") == expected_updated_at
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        zlib.error,
    ):
        return False


def raw_conservation_evidence(raw_db_path: Path) -> dict[str, Any]:
    """Capture content-free key/row/ACL evidence for first-apply conservation."""
    modes = {
        "raw_turns": (
            "revision_bound_projection",
            "SELECT * FROM raw_turns ORDER BY rowid",
        ),
        "raw_turn_revisions": (
            "append_rows",
            "SELECT * FROM raw_turn_revisions ORDER BY rowid",
        ),
        "raw_native_contract_observations": (
            "append_rows",
            "SELECT * FROM raw_native_contract_observations ORDER BY rowid",
        ),
        "raw_metrics": (
            "contract_bound_metrics",
            "SELECT * FROM raw_metrics ORDER BY rowid",
        ),
        "raw_provenance_edges": (
            "exact_rows",
            "SELECT * FROM raw_provenance_edges ORDER BY rowid",
        ),
        "raw_provenance_gaps": (
            "exact_rows",
            "SELECT * FROM raw_provenance_gaps ORDER BY rowid",
        ),
        "raw_access_log": (
            "exact_rows",
            "SELECT * FROM raw_access_log ORDER BY rowid",
        ),
        "raw_lifecycle_state": (
            "exact_rows",
            "SELECT * FROM raw_lifecycle_state ORDER BY rowid",
        ),
        "raw_event_identity_aliases": (
            "exact_rows",
            "SELECT * FROM raw_event_identity_aliases ORDER BY rowid",
        ),
        "raw_subject_deletion_receipts": (
            "exact_rows",
            "SELECT * FROM raw_subject_deletion_receipts ORDER BY rowid",
        ),
    }
    evidence: dict[str, Any] = {}
    try:
        with connect_readonly_sqlite(Path(raw_db_path)) as conn:
            present = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table, (mode, rows_query) in modes.items():
                if table not in present:
                    continue
                table_info = conn.execute(
                    'SELECT cid, name, type, "notnull", dflt_value, pk '
                    "FROM pragma_table_info(?)",
                    (table,),
                ).fetchall()
                columns = [str(row[1]) for row in table_info]
                primary_columns = [
                    str(row[1])
                    for row in sorted(
                        table_info,
                        key=lambda item: int(item[5] or 0),
                    )
                    if int(row[5] or 0) > 0
                ]
                key_indexes = [columns.index(column) for column in primary_columns]
                item: dict[str, Any] = {
                    "mode": mode,
                    "row_count": 0,
                    "key_hashes": [],
                }
                if mode in {"append_rows", "exact_rows"}:
                    item["row_hashes"] = []
                event_index = (
                    columns.index("event_id")
                    if table in {"raw_turns", "raw_metrics"}
                    else -1
                )
                revision_index = (
                    columns.index("current_revision_id")
                    if table == "raw_turns"
                    else -1
                )
                metadata_index = (
                    columns.index("metadata_json")
                    if table == "raw_turns"
                    else -1
                )
                raw_turn_stable_columns = (
                    [
                        column
                        for column in columns
                        if column
                        not in {
                            "completeness_status",
                            "metadata_json",
                            "updated_at",
                        }
                    ]
                    if table == "raw_turns"
                    else []
                )
                raw_turn_stable_indexes = [
                    columns.index(column) for column in raw_turn_stable_columns
                ]
                metric_mutable_columns = {
                    "confidence",
                    "survival_score",
                    "retention_state",
                    "next_survival_recalc_at",
                    "updated_at",
                }
                metric_stable_columns = (
                    [
                        column
                        for column in columns
                        if column not in metric_mutable_columns
                    ]
                    if table == "raw_metrics"
                    else []
                )
                metric_stable_indexes = [
                    columns.index(column) for column in metric_stable_columns
                ]
                acl_hashes: list[str] = []
                turn_bindings: list[dict[str, Any]] = []
                metric_bindings: list[dict[str, Any]] = []
                for row in conn.execute(rows_query):
                    item["row_count"] = int(item["row_count"]) + 1
                    key_row = (
                        tuple(row[index] for index in key_indexes)
                        if key_indexes
                        else row
                    )
                    item["key_hashes"].append(
                        _hash_sql_row(
                            primary_columns or columns,
                            key_row,
                        )
                    )
                    if mode in {"append_rows", "exact_rows"}:
                        item["row_hashes"].append(
                            _hash_sql_row(columns, row)
                        )
                    if table == "raw_turn_revisions":
                        logical_event_id = str(
                            row[columns.index("logical_event_id")] or ""
                        )
                        supersedes_revision_id = str(
                            row[columns.index("supersedes_revision_id")] or ""
                        )
                        if supersedes_revision_id:
                            superseded = conn.execute(
                                """
                                SELECT logical_event_id
                                FROM raw_turn_revisions
                                WHERE revision_id=?
                                """,
                                (supersedes_revision_id,),
                            ).fetchone()
                            if (
                                superseded is None
                                or str(superseded[0] or "") != logical_event_id
                            ):
                                raise ValueError(
                                    "Raw revision supersedes a foreign logical event"
                                )
                    if table == "raw_turns":
                        try:
                            metadata = json.loads(str(row[metadata_index] or "{}"))
                        except (TypeError, json.JSONDecodeError):
                            metadata = {}
                        acl = {
                            str(key): value
                            for key, value in (
                                metadata.items() if isinstance(metadata, Mapping) else ()
                            )
                            if any(
                                marker in str(key).lower()
                                for marker in (
                                    "acl",
                                    "access",
                                    "authorization",
                                    "visibility",
                                    "privacy",
                                )
                            )
                        }
                        acl_hashes.append(
                            _hash_sql_row(
                                ["event_identity_hash", "acl"],
                                (
                                    hashlib.sha256(
                                        str(row[event_index]).encode("utf-8")
                                    ).hexdigest(),
                                    acl,
                                ),
                            )
                        )
                        event_id = str(row[event_index] or "")
                        revision_id = str(row[revision_index] or "")
                        revision = conn.execute(
                            """
                            SELECT logical_event_id, content_hash, full_content_hash,
                                   snapshot_blob
                            FROM raw_turn_revisions WHERE revision_id=?
                            """,
                            (revision_id,),
                        ).fetchone()
                        projection_valid = bool(
                            revision is not None
                            and _revision_projection_matches(
                                columns=columns,
                                row=row,
                                revision=revision,
                                event_id=event_id,
                            )
                        )
                        latest = NativeRawContractLedger.latest(conn, event_id)
                        effective_projection_valid = bool(
                            revision is not None
                            and _effective_projection_matches(
                                columns=columns,
                                row=row,
                                revision=revision,
                                latest=latest,
                            )
                        )
                        turn_bindings.append(
                            {
                                "event_identity_hash": _canonical_hash({"event_id": event_id}),
                                "current_revision_hash": _canonical_hash(
                                    {"revision_id": revision_id}
                                ),
                                "stable_projection_hash": _canonical_hash(
                                    {
                                        "columns": raw_turn_stable_columns,
                                        "values": [
                                            (
                                                {"blob_sha256": hashlib.sha256(value).hexdigest()}
                                                if isinstance(value, bytes)
                                                else value
                                            )
                                            for value in (
                                                row[index]
                                                for index in raw_turn_stable_indexes
                                            )
                                        ],
                                    }
                                ),
                                "effective_projection_hash": _canonical_hash(
                                    {
                                        "completeness_status": row[
                                            columns.index("completeness_status")
                                        ],
                                        "metadata_json": row[metadata_index],
                                        "updated_at": row[columns.index("updated_at")],
                                    }
                                ),
                                "latest_observation_hash": _canonical_hash(
                                    {
                                        "observation_id": (
                                            str(latest.get("observation_id") or "")
                                            if latest
                                            else ""
                                        )
                                    }
                                ),
                                "current_revision_projection_valid": projection_valid,
                                "effective_projection_valid": effective_projection_valid,
                            }
                        )
                    if table == "raw_metrics":
                        event_id = str(row[event_index] or "")
                        current_revision = conn.execute(
                            """
                            SELECT current_revision_id
                            FROM raw_turns
                            WHERE event_id=?
                            """,
                            (event_id,),
                        ).fetchone()
                        if current_revision is None:
                            raise ValueError(
                                "Raw metric has no canonical logical event"
                            )
                        latest = NativeRawContractLedger.latest(conn, event_id)
                        metric_bindings.append(
                            {
                                "event_identity_hash": _canonical_hash({"event_id": event_id}),
                                "stable_projection_hash": _canonical_hash(
                                    {
                                        "columns": metric_stable_columns,
                                        "values": [
                                            row[index]
                                            for index in metric_stable_indexes
                                        ],
                                    }
                                ),
                                "retention_state": str(row[columns.index("retention_state")] or ""),
                                "governed_projection_hash": _canonical_hash(
                                    {
                                        "confidence": row[columns.index("confidence")],
                                        "survival_score": row[columns.index("survival_score")],
                                        "retention_state": row[columns.index("retention_state")],
                                        "next_survival_recalc_at": row[
                                            columns.index("next_survival_recalc_at")
                                        ],
                                        "updated_at": row[columns.index("updated_at")],
                                    }
                                ),
                                "latest_observation_hash": _canonical_hash(
                                    {
                                        "observation_id": (
                                            str(latest.get("observation_id") or "")
                                            if latest
                                            else ""
                                        )
                                    }
                                ),
                                "latest_contract_state": (
                                    str(latest.get("contract_state") or "")
                                    if latest
                                    else ""
                                ),
                            }
                        )
                item["key_hashes"].sort()
                if mode in {"append_rows", "exact_rows"}:
                    item["row_hashes"].sort()
                if table == "raw_turns":
                    item["acl_hashes"] = sorted(acl_hashes)
                    item["turn_bindings"] = sorted(
                        turn_bindings,
                        key=lambda value: value["event_identity_hash"],
                    )
                if table == "raw_metrics":
                    item["metric_bindings"] = sorted(
                        metric_bindings,
                        key=lambda value: value["event_identity_hash"],
                    )
                evidence[table] = item
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise NativeRawRecoveryEvidenceError("raw_conservation_evidence_failed") from None
    return evidence


def raw_conservation_findings(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return content-free reasons why Raw conservation does not hold."""
    findings: list[dict[str, Any]] = []
    for table, prior in before.items():
        current = after.get(table)
        if not isinstance(current, Mapping):
            findings.append(
                {
                    "table": table,
                    "rule": "required_table_present",
                    "missing_count": 1,
                }
            )
            continue
        prior_keys = set(prior.get("key_hashes") or [])
        current_keys = set(current.get("key_hashes") or [])
        missing_keys = prior_keys - current_keys
        if missing_keys:
            findings.append(
                {
                    "table": table,
                    "rule": "prior_keys_preserved",
                    "missing_count": len(missing_keys),
                }
            )
        mode = str(prior.get("mode") or "")
        if mode == "revision_bound_projection":
            prior_bindings = {
                item["event_identity_hash"]: item for item in prior.get("turn_bindings") or []
            }
            current_bindings = {
                item["event_identity_hash"]: item for item in current.get("turn_bindings") or []
            }
            binding_counts = {
                "binding_missing": 0,
                "current_revision_projection_invalid": 0,
                "effective_projection_invalid": 0,
                "stable_projection_changed_without_revision": 0,
                "effective_projection_changed_without_observation": 0,
            }
            for event_hash, prior_binding in prior_bindings.items():
                current_binding = current_bindings.get(event_hash)
                if not current_binding:
                    binding_counts["binding_missing"] += 1
                    continue
                if current_binding.get("current_revision_projection_valid") is not True:
                    binding_counts["current_revision_projection_invalid"] += 1
                if current_binding.get("effective_projection_valid") is not True:
                    binding_counts["effective_projection_invalid"] += 1
                if current_binding.get("current_revision_hash") == prior_binding.get(
                    "current_revision_hash"
                ):
                    if current_binding.get("stable_projection_hash") != prior_binding.get(
                        "stable_projection_hash"
                    ) and prior_binding.get(
                        "current_revision_projection_valid"
                    ) is True:
                        binding_counts[
                            "stable_projection_changed_without_revision"
                        ] += 1
                    if current_binding.get("effective_projection_hash") != prior_binding.get(
                        "effective_projection_hash"
                    ) and current_binding.get("latest_observation_hash") == prior_binding.get(
                        "latest_observation_hash"
                    ) and prior_binding.get("effective_projection_valid") is True:
                        binding_counts[
                            "effective_projection_changed_without_observation"
                        ] += 1
            findings.extend(
                {
                    "table": table,
                    "rule": rule,
                    "mismatch_count": count,
                }
                for rule, count in binding_counts.items()
                if count
            )
        if mode == "contract_bound_metrics":
            prior_bindings = {
                item["event_identity_hash"]: item for item in prior.get("metric_bindings") or []
            }
            current_bindings = {
                item["event_identity_hash"]: item for item in current.get("metric_bindings") or []
            }
            metric_counts = {
                "metric_binding_missing": 0,
                "stable_metric_projection_changed": 0,
                "governed_metric_changed_without_observation": 0,
                "invalid_retention_transition": 0,
            }
            for event_hash, prior_binding in prior_bindings.items():
                current_binding = current_bindings.get(event_hash)
                if not current_binding:
                    metric_counts["metric_binding_missing"] += 1
                    continue
                if current_binding.get("stable_projection_hash") != prior_binding.get(
                    "stable_projection_hash"
                ):
                    metric_counts["stable_metric_projection_changed"] += 1
                if current_binding.get("governed_projection_hash") != prior_binding.get(
                    "governed_projection_hash"
                ) and current_binding.get("latest_observation_hash") == prior_binding.get(
                    "latest_observation_hash"
                ):
                    metric_counts[
                        "governed_metric_changed_without_observation"
                    ] += 1
                if current_binding.get("retention_state") != prior_binding.get(
                    "retention_state"
                ) and not (
                    current_binding.get("retention_state") == "active"
                    and current_binding.get("latest_contract_state") == "conformant"
                    and current_binding.get("latest_observation_hash")
                    != prior_binding.get("latest_observation_hash")
                ):
                    metric_counts["invalid_retention_transition"] += 1
            findings.extend(
                {
                    "table": table,
                    "rule": rule,
                    "mismatch_count": count,
                }
                for rule, count in metric_counts.items()
                if count
            )
        prior_rows = set(prior.get("row_hashes") or [])
        current_rows = set(current.get("row_hashes") or [])
        if mode == "exact_rows" and prior_rows != current_rows:
            findings.append(
                {
                    "table": table,
                    "rule": "exact_rows_unchanged",
                    "missing_count": len(prior_rows - current_rows),
                    "added_count": len(current_rows - prior_rows),
                }
            )
        if mode == "append_rows":
            missing_rows = prior_rows - current_rows
            if missing_rows:
                findings.append(
                    {
                        "table": table,
                        "rule": "prior_rows_preserved",
                        "missing_count": len(missing_rows),
                    }
                )
        if table == "raw_turns":
            prior_acl = set(prior.get("acl_hashes") or [])
            current_acl = set(current.get("acl_hashes") or [])
            missing_acl = prior_acl - current_acl
            if missing_acl:
                findings.append(
                    {
                        "table": table,
                        "rule": "prior_acl_preserved",
                        "missing_count": len(missing_acl),
                    }
                )
    return findings


def compare_raw_conservation(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Require exact protected rows and append-only mutable Raw history."""
    return not raw_conservation_findings(before, after)


def conservation_summary(
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Collapse comparison sets into durable content-free receipt evidence."""
    return {
        table: {
            "mode": item.get("mode"),
            "row_count": item.get("row_count"),
            "key_set_hash": _canonical_hash({"values": item.get("key_hashes") or []}),
            "row_set_hash": _canonical_hash({"values": item.get("row_hashes") or []}),
            "acl_set_hash": _canonical_hash({"values": item.get("acl_hashes") or []}),
            "turn_binding_set_hash": _canonical_hash({"values": item.get("turn_bindings") or []}),
            "metric_binding_set_hash": _canonical_hash(
                {"values": item.get("metric_bindings") or []}
            ),
        }
        for table, item in sorted(evidence.items())
    }


__all__ = [
    "NativeRawRecoveryEvidenceError",
    "compare_raw_conservation",
    "conservation_summary",
    "raw_conservation_findings",
    "raw_conservation_evidence",
]
