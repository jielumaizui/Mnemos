"""Read-only reconciliation planning over fixed local SQLite owners."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.runtime_paths import RuntimePaths
from core.telemetry.model_call_ledger.migration import LedgerReconciliation

from .contracts import (
    RECORD_TABLES as _RECORD_TABLES,
    SCHEMA_VERSION,
    SOURCE_FILENAMES as _SOURCE_FILENAMES,
    HistoricalCall,
    ModelCallLedgerReconcileError,
)
from .inventory import (
    _plan_fingerprint,
    _require_runtime_owner_path,
    _source_snapshot,
)


def _canonical_fingerprints(path: Path) -> tuple[str, set[str], dict[str, int]]:
    """Inspect canonical ledger state through its dedicated migration seam."""
    return LedgerReconciliation.inspect_canonical(path)


def build_reconciliation_plan(config: Any) -> tuple[dict[str, Any], list[HistoricalCall]]:
    """Build a write-free plan and the exact safe metadata records to import."""
    paths = RuntimePaths.from_config(config)
    canonical_path = paths.model_call_ledger_db
    source_reports: list[dict[str, Any]] = []
    records: list[HistoricalCall] = []
    for filename in _SOURCE_FILENAMES:
        source_path = paths.database_dir / filename
        _require_runtime_owner_path(source_path, paths.database_dir)
        report, source_records = _source_snapshot(source_path)
        source_reports.append(report)
        records.extend(source_records)

    # A retired owner can also be embedded in the canonical database after an
    # interrupted migration.  It is not a normal external source (the file
    # must remain), but it has the same metadata-only import/discard and
    # verified-backup cleanup obligations as split stores.
    _require_runtime_owner_path(canonical_path, paths.database_dir)
    canonical_retired_report, canonical_retired_records = _source_snapshot(canonical_path)
    records.extend(canonical_retired_records)

    canonical_state, existing, canonical_counts = _canonical_fingerprints(canonical_path)
    # The source triple, rather than the value fingerprint, is the only
    # deduplication identity.  Do not collapse different physical rows merely
    # because their non-content operational metadata matches; conversely, an
    # accidental second observation of one physical row must fail closed even
    # if its mutable metadata changed between reads.
    unique: dict[tuple[str, str, str, int], HistoricalCall] = {}
    for record in records:
        source_identity = (
            record.source_db,
            record.source_generation,
            record.source_table,
            record.source_rowid,
        )
        if source_identity in unique:
            raise ModelCallLedgerReconcileError("duplicate_source_record_identity")
        unique[source_identity] = record
    candidate_records = sorted(unique.values(), key=lambda record: record.fingerprint)
    attributable_candidates = [record for record in candidate_records if record.subject_scope is not None]
    unattributable_candidates = [record for record in candidate_records if record.subject_scope is None]
    needs_import = [
        record for record in attributable_candidates if record.fingerprint not in existing
    ]
    canonical_retired_tables = list(canonical_retired_report.get("retired_tables", []))
    canonical_retired_record_count = sum(
        int(canonical_retired_report.get("rows_by_table", {}).get(table, 0) or 0)
        for table in _RECORD_TABLES
    )
    canonical_retired_stats_row_count = int(
        canonical_retired_report.get("rows_by_table", {}).get("prompt_call_stats", 0) or 0
    )
    retired_stats_row_count = canonical_retired_stats_row_count + sum(
        int(report.get("rows_by_table", {}).get("prompt_call_stats", 0) or 0)
        for report in source_reports
    )
    privacy_reconciliation_required = canonical_state not in {"ready", "missing"}
    requires_explicit_unattributable_discard = bool(
        unattributable_candidates
        or int(canonical_counts["canonical_unattributable_legacy_count"] or 0)
    )
    requires_explicit_retired_stats_discard = bool(retired_stats_row_count)
    requires_explicit_unrecoverable_run_tombstone_history_discard = bool(
        canonical_counts.get("canonical_unrecoverable_run_tombstone_history", 0)
    )
    has_orphan_sidecar = any(
        str(report.get("error") or "") == "reconciliation_orphan_sidecar_present"
        for report in [*source_reports, canonical_retired_report]
    )
    has_unreadable_source = any(report.get("error") for report in source_reports)
    invalid_integrity = any(
        report["exists"] and report.get("integrity_check") != "ok" for report in source_reports
    )
    unsupported_source_schema_objects = any(
        report.get("retired_tables") and report.get("other_user_schema_objects")
        for report in source_reports
    )
    canonical_schema_objects = bool(
        canonical_retired_tables and canonical_retired_report.get("other_user_schema_objects")
    )
    error = ""
    if has_orphan_sidecar:
        error = "reconciliation_orphan_sidecar_present"
    elif canonical_state == "invalid":
        error = "canonical_ledger_schema_invalid"
    elif canonical_retired_report.get("error"):
        error = "canonical_retired_storage_unreadable"
    elif (
        canonical_retired_report.get("exists")
        and canonical_retired_report.get("integrity_check") != "ok"
    ):
        error = "canonical_retired_storage_integrity_invalid"
    elif canonical_schema_objects:
        error = "canonical_retired_storage_schema_objects_unsupported"
    elif has_unreadable_source:
        error = "legacy_source_unreadable"
    elif invalid_integrity:
        error = "legacy_source_integrity_invalid"
    elif unsupported_source_schema_objects:
        error = "legacy_source_schema_objects_unsupported"
    plan = {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "canonical_path": str(canonical_path),
        "canonical_state": canonical_state,
        "canonical_privacy_counts": canonical_counts,
        "privacy_reconciliation_required": privacy_reconciliation_required,
        "sources": source_reports,
        "canonical_retired_storage": canonical_retired_report,
        "canonical_retired_record_count": canonical_retired_record_count,
        "canonical_retired_stats_row_count": canonical_retired_stats_row_count,
        "retired_stats_row_count": retired_stats_row_count,
        "legacy_source_row_count": len(records),
        "unique_legacy_call_count": len(candidate_records),
        "duplicate_legacy_row_count": len(records) - len(candidate_records),
        "canonical_already_imported_count": len(attributable_candidates) - len(needs_import),
        "would_import_count": len(needs_import),
        "attributable_legacy_call_count": len(attributable_candidates),
        "unattributable_legacy_call_count": len(unattributable_candidates),
        "requires_explicit_unattributable_discard": requires_explicit_unattributable_discard,
        "requires_explicit_retired_stats_discard": requires_explicit_retired_stats_discard,
        "requires_explicit_unrecoverable_run_tombstone_history_discard": (
            requires_explicit_unrecoverable_run_tombstone_history_discard
        ),
        "legacy_storage_path_count": sum(
            1 for report in source_reports if report.get("retired_tables")
        ) + int(bool(canonical_retired_tables)),
        "plan_fingerprint": _plan_fingerprint(
            candidate_records,
            canonical_state,
            [*source_reports, canonical_retired_report],
        ),
        "status": "blocked" if error else ("clean" if not records and not any(
            report.get("retired_tables") for report in source_reports
        ) and not canonical_retired_tables and not privacy_reconciliation_required else "reconciliation_required"),
        "ok": not error,
    }
    if error:
        plan["error"] = error
    return plan, needs_import
