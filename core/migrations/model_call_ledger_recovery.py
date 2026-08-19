"""Public model-call-ledger recovery facade.

Only stable lifecycle operations, restore planning/apply, the recovery error
type, and the shared runtime lock are exposed here.  Evidence sealing,
journal/file handling, and reverse compensation live in focused internal
modules so callers cannot accidentally couple to mutable recovery mechanics.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

from core.migrations import model_call_ledger_recovery_evidence as _evidence

MODEL_CALL_LEDGER_MIGRATION_ID = _evidence.MODEL_CALL_LEDGER_MIGRATION_ID
RECOVERY_PROGRESS_SCHEMA_VERSION = _evidence.RECOVERY_PROGRESS_SCHEMA_VERSION
RECOVERY_SCHEMA_VERSION = _evidence.RECOVERY_SCHEMA_VERSION
ModelCallLedgerRecoveryError = _evidence.ModelCallLedgerRecoveryError
acquire_model_call_ledger_migration_lock = _evidence.acquire_model_call_ledger_migration_lock
reconciliation_semantic_hash = _evidence.reconciliation_semantic_hash

__all__ = [
    "MODEL_CALL_LEDGER_MIGRATION_ID",
    "RECOVERY_PROGRESS_SCHEMA_VERSION",
    "RECOVERY_SCHEMA_VERSION",
    "ModelCallLedgerRecoveryError",
    "acquire_model_call_ledger_migration_lock",
    "commit_sealed_recovery_bundle",
    "fail_sealed_recovery_apply",
    "plan_model_call_ledger_restore",
    "prepare_sealed_recovery_bundle",
    "reconciliation_semantic_hash",
    "restore_model_call_ledger",
    "safe_reconciliation_summary",
    "start_sealed_recovery_apply",
]


def _bundle_output(
    manifest: Mapping[str, Any],
    root: Path,
    manifest_hash: str,
    chain_head: str,
    *,
    status: str,
    verification: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "status": status,
        "ok": True,
        "recovery_id": str(manifest["recovery_id"]),
        "recovery_manifest": str(root / str(manifest["manifest_name"])),
        "manifest_sha256": manifest_hash,
        "recovery_chain_head": chain_head,
        "recovery_prepare_chain_head": str(manifest["prepared_chain_head"]),
        "backup_root": str(root),
        "backup_id": _evidence._hash_payload(
            {"recovery_id": manifest["recovery_id"], "backup_root_binding": manifest["backup_root_binding"]}
        ),
        "preimage_semantic_hash": str(manifest["preimage_semantic_hash"]),
        "target_count": len(_evidence._target_entries(manifest)),
        "verification": dict(verification),
    }


def prepare_sealed_recovery_bundle(
    config: Any,
    *,
    reconciliation_plan: Mapping[str, Any],
    backup_dir: Path,
    registry_ledger_id: str,
    expected_plan_hash: str,
) -> dict[str, Any]:
    """Durably seal verified preimages before the source reconciler can write."""
    try:
        plan_hash = str(reconciliation_plan.get("plan_fingerprint") or "")
        if not expected_plan_hash or expected_plan_hash != plan_hash:
            raise ModelCallLedgerRecoveryError("recovery_reviewed_plan_hash_mismatch")
        if not registry_ledger_id:
            raise ModelCallLedgerRecoveryError("recovery_registry_ledger_id_missing")
        root = Path(backup_dir).expanduser().absolute()
        _evidence._lstat_directory(root, private=True)
        root = root.resolve(strict=True)
        backup_receipts = _evidence._backup_map(root, reconciliation_plan)
        reports = _evidence._source_report_by_filename(reconciliation_plan)
        target_ids = ["canonical_ledger"]
        for target_id, filename in _evidence._TARGETS[1:]:
            if reports.get(filename, {}).get("retired_tables"):
                target_ids.append(target_id)
        target_entries: list[dict[str, Any]] = []
        for target_id in target_ids:
            filename = _evidence._TARGET_FILENAMES[target_id]
            report = reports.get(filename)
            receipt = backup_receipts.get(filename)
            if report is None or bool(report.get("exists")) != bool(receipt):
                raise ModelCallLedgerRecoveryError("recovery_preimage_backup_coverage_invalid")
            if receipt is None:
                preimage: dict[str, Any] = {"state": "absent"}
            else:
                preimage = {"state": "present", "backup": _evidence._backup_binding(root, target_id, receipt)}
            target_entries.append({"target_id": target_id, "preimage": preimage})
        recovery_id = "mcl-recovery-" + uuid.uuid4().hex
        token = recovery_id.rsplit("-", 1)[-1]
        journal_name = _evidence._RECOVERY_PREFIX + token + ".progress.jsonl"
        manifest_name = _evidence._RECOVERY_PREFIX + token + ".json"
        journal_anchor = _evidence._hash_payload(
            {
                "recovery_id": recovery_id,
                "registry_ledger_id": registry_ledger_id,
                "expected_plan_hash": expected_plan_hash,
                "target_ids": target_ids,
            }
        )
        prepared_event = {
            "event": "apply_prepared",
            "recovery_id": recovery_id,
            "registry_ledger_id": registry_ledger_id,
            "expected_plan_hash": expected_plan_hash,
            "preimage_hash": _evidence._hash_payload(target_entries),
            "created_at": _evidence._now_iso(),
            "prev_hash": journal_anchor,
        }
        prepared_head = _evidence._hash_payload(
            {**prepared_event, "schema_version": RECOVERY_PROGRESS_SCHEMA_VERSION}
        )
        manifest_without_hash: dict[str, Any] = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "recovery_id": recovery_id,
            "migration_id": MODEL_CALL_LEDGER_MIGRATION_ID,
            "registry_ledger_id": registry_ledger_id,
            "expected_plan_hash": expected_plan_hash,
            "reconcile_plan_hash": plan_hash,
            "preimage_semantic_hash": reconciliation_semantic_hash(reconciliation_plan),
            "backup_root_binding": _evidence._private_root_binding(root),
            "target_ids": target_ids,
            "targets": target_entries,
            "journal_file": journal_name,
            "journal_anchor_hash": journal_anchor,
            "prepared_chain_head": prepared_head,
            "manifest_name": manifest_name,
            "created_at": _evidence._now_iso(),
        }
        manifest_hash = _evidence._hash_payload(manifest_without_hash)
        manifest = {**manifest_without_hash, "manifest_sha256": manifest_hash}
        observed_head = _evidence._append_progress(root, journal_name, prepared_event)
        if observed_head != prepared_head:
            raise ModelCallLedgerRecoveryError("recovery_prepare_journal_hash_mismatch")
        manifest_path = _evidence._safe_relative_child(root, manifest_name, private=True)
        _evidence._write_new_private(manifest_path, _evidence._canonical_json(manifest))
        return _bundle_output(
            manifest,
            root,
            manifest_hash,
            prepared_head,
            status="prepared",
            verification={"backup_bindings_verified": True, "preimage_sealed_before_mutation": True},
        )
    except _evidence._RECOVERABLE_ERRORS as exc:
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": _evidence._clean_error(exc),
        }


def _load_prepared_bundle(
    config: Any, bundle: Mapping[str, Any]
) -> tuple[dict[str, Any], Path, str, list[dict[str, Any]], str]:
    manifest_ref = Path(str(bundle.get("recovery_manifest") or ""))
    manifest, root, manifest_hash = _evidence._load_manifest(config, manifest_ref)
    _evidence._verify_preimage_bindings(manifest, root)
    events, head = _evidence._read_progress(root, manifest)
    if str(bundle.get("manifest_sha256") or "") != manifest_hash:
        raise ModelCallLedgerRecoveryError("recovery_bundle_manifest_mismatch")
    return manifest, root, manifest_hash, events, head


def start_sealed_recovery_apply(config: Any, *, bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Append a durable mutation-start receipt immediately before source writes."""
    try:
        manifest, root, manifest_hash, events, head = _load_prepared_bundle(config, bundle)
        if str(events[-1].get("event") or "") != "apply_prepared":
            raise ModelCallLedgerRecoveryError("recovery_apply_start_state_invalid")
        started_head = _evidence._append_progress(
            root,
            str(manifest["journal_file"]),
            {
                "event": "apply_started",
                "recovery_id": str(manifest["recovery_id"]),
                "created_at": _evidence._now_iso(),
                "prev_hash": head,
            },
        )
        return _bundle_output(
            manifest, root, manifest_hash, started_head, status="started", verification={"mutation_start_durable": True}
        )
    except _evidence._RECOVERABLE_ERRORS as exc:
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": _evidence._clean_error(exc),
        }


def commit_sealed_recovery_bundle(
    config: Any,
    *,
    reconciliation_result: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Append postimage evidence after a successful source reconciliation."""
    try:
        manifest, root, manifest_hash, events, head = _load_prepared_bundle(config, bundle)
        if str(events[-1].get("event") or "") != "apply_started":
            raise ModelCallLedgerRecoveryError("recovery_apply_commit_state_invalid")
        if not reconciliation_result.get("ok") or str(reconciliation_result.get("status") or "") != "applied":
            raise ModelCallLedgerRecoveryError("recovery_requires_successful_reconciliation")
        if str(reconciliation_result.get("plan_fingerprint") or "") != str(manifest["expected_plan_hash"]):
            raise ModelCallLedgerRecoveryError("recovery_reviewed_plan_hash_mismatch")
        target_ids = [str(entry["target_id"]) for entry in _evidence._target_entries(manifest)]
        paths = _evidence._target_paths(config, target_ids)
        postimages = [
            {"target_id": target_id, "postimage": _evidence._target_identity(paths[target_id])}
            for target_id in target_ids
        ]
        committed_head = _evidence._append_progress(
            root,
            str(manifest["journal_file"]),
            {
                "event": "apply_committed",
                "recovery_id": str(manifest["recovery_id"]),
                "postimages": postimages,
                "postimage_hash": _evidence._hash_payload(postimages),
                "created_at": _evidence._now_iso(),
                "prev_hash": head,
            },
        )
        return _bundle_output(
            manifest,
            root,
            manifest_hash,
            committed_head,
            status="sealed",
            verification={"postimage_bound": True, "append_only_apply_receipt": True},
        )
    except _evidence._RECOVERABLE_ERRORS as exc:
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": _evidence._clean_error(exc),
        }


def fail_sealed_recovery_apply(config: Any, *, bundle: Mapping[str, Any], phase: str) -> dict[str, Any]:
    """Make a failed/interrupted apply visible without serializing its error text."""
    try:
        manifest, root, manifest_hash, events, head = _load_prepared_bundle(config, bundle)
        if str(events[-1].get("event") or "") not in {"apply_prepared", "apply_started"}:
            raise ModelCallLedgerRecoveryError("recovery_apply_failure_state_invalid")
        failed_head = _evidence._append_progress(
            root,
            str(manifest["journal_file"]),
            {
                "event": "apply_failed",
                "recovery_id": str(manifest["recovery_id"]),
                "phase": phase if phase in {"source_reconciliation", "recovery_commit"} else "lifecycle",
                "created_at": _evidence._now_iso(),
                "prev_hash": head,
            },
        )
        return _bundle_output(
            manifest,
            root,
            manifest_hash,
            failed_head,
            status="interrupted",
            verification={"interruption_recorded": True},
        )
    except _evidence._RECOVERABLE_ERRORS as exc:
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": _evidence._clean_error(exc),
        }


def safe_reconciliation_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Compact allowlisted reconcile evidence suitable for migration ledger JSON."""
    return {
        "schema_version": str(result.get("schema_version") or ""),
        "status": str(result.get("status") or ""),
        "ok": bool(result.get("ok")),
        "plan_fingerprint": str(result.get("plan_fingerprint") or ""),
        "imported_count": int(result.get("imported_count", 0) or 0),
        "backup_count": len(result.get("backup") or []),
        "cleanup_count": len(result.get("cleanup") or []),
        "discarded_unattributable_source_count": int(
            result.get("discarded_unattributable_source_count", 0) or 0
        ),
        "discarded_unrecoverable_run_tombstone_history": int(
            result.get("discarded_unrecoverable_run_tombstone_history", 0) or 0
        ),
    }


def plan_model_call_ledger_restore(
    config: Any,
    *,
    recovery_manifest: Path,
    ledger_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the read-only v3 restore plan through the stable facade."""
    from core.migrations.model_call_ledger_recovery_restore import (
        plan_model_call_ledger_restore as _plan_model_call_ledger_restore,
    )

    return _plan_model_call_ledger_restore(
        config,
        recovery_manifest=recovery_manifest,
        ledger_binding=ledger_binding,
    )


def restore_model_call_ledger(
    config: Any,
    *,
    recovery_manifest: Path,
    ledger_binding: Mapping[str, Any] | None,
    apply: bool,
) -> dict[str, Any]:
    """Run the v3 restore implementation through the stable facade."""
    from core.migrations.model_call_ledger_recovery_restore import (
        restore_model_call_ledger as _restore_model_call_ledger,
    )

    return _restore_model_call_ledger(
        config,
        recovery_manifest=recovery_manifest,
        ledger_binding=ledger_binding,
        apply=apply,
    )
