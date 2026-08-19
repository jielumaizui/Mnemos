"""Private restore and reverse-compensation implementation for ledger recovery.

The public recovery facade owns the stable operator-facing API.  This module
owns mutable restore mechanics: reverse snapshots, target replacement,
compensation, and read-only restore planning.
"""

from __future__ import annotations

import contextlib
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.migrations.model_call_ledger_recovery_evidence import (
    RECOVERY_SCHEMA_VERSION,
    ModelCallLedgerRecoveryError,
    _RECOVERABLE_ERRORS,
    _RecoveryLock,
    _SIDECAR_SUFFIXES,
    _append_progress,
    _clean_error,
    _create_private_directory,
    _runtime_writers_are_inactive,
    _exclusive_recovery_lock,
    _file_identity,
    _fsync_directory,
    _hash_payload,
    _load_manifest,
    _lstat_directory,
    _lstat_regular,
    _matches_preimage,
    _no_follow_open,
    _now_iso,
    _postimages_from_event,
    _read_progress,
    _safe_relative_child,
    _sidecar_identity,
    _sqlite_integrity,
    _state_matches,
    _target_entries,
    _target_identity,
    _target_paths,
    _verify_backup_binding,
    _verify_manifest_bindings,
    _write_all,
    acquire_model_call_ledger_migration_lock,
    reconciliation_semantic_hash,
)
from core.runtime_paths import RuntimePaths


def _copy_runtime_file_to_new(source: Path, destination: Path) -> dict[str, Any]:
    """Capture one exact runtime file, including SQLite sidecars, privately.

    A logical SQLite backup intentionally omits WAL/SHM bytes.  That is useful
    for migration preimages, but not for reverse compensation: after a failed
    restore we must be able to recreate the exact postimage which the sealed
    journal bound.  This helper therefore copies bytes component-by-component
    and rejects a source that changes during capture.
    """
    before = _file_identity(source, private=False)
    _lstat_directory(destination.parent, private=True)
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = _no_follow_open(source, os.O_RDONLY)
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (before["device"], before["inode"]):
            raise ModelCallLedgerRecoveryError("recovery_reverse_source_replaced")
        destination_fd = _no_follow_open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        os.fchmod(destination_fd, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            _write_all(destination_fd, chunk)
        os.fsync(destination_fd)
    except ModelCallLedgerRecoveryError:
        raise
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_reverse_backup_copy_failed") from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
    copied = _file_identity(destination, private=True)
    after = _file_identity(source, private=False)
    if (
        copied.get("sha256") != before.get("sha256")
        or copied.get("byte_size") != before.get("byte_size")
        or any(
            after.get(key) != before.get(key)
            for key in ("sha256", "byte_size", "device", "inode", "mtime_ns", "ctime_ns")
        )
    ):
        raise ModelCallLedgerRecoveryError("recovery_reverse_source_changed_during_backup")
    _fsync_directory(destination.parent)
    return copied


def _copy_private_backup_to_target(source: Path, target: Path, expected_sha256: str) -> None:
    _lstat_regular(source, private=True)
    parent = target.parent
    _lstat_directory(parent, private=False)
    stage = parent / f".{target.name}.restore-{uuid.uuid4().hex}.tmp"
    try:
        source_fd = _no_follow_open(source, os.O_RDONLY)
        target_fd = _no_follow_open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.close(locals().get("source_fd", -1))
        raise ModelCallLedgerRecoveryError("recovery_restore_stage_create_failed") from exc
    try:
        os.fchmod(target_fd, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            _write_all(target_fd, chunk)
        os.fsync(target_fd)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_restore_stage_write_failed") from exc
    finally:
        os.close(source_fd)
        os.close(target_fd)
    try:
        staged = _file_identity(stage, private=False)
        if staged.get("sha256") != expected_sha256:
            raise ModelCallLedgerRecoveryError("recovery_restore_stage_hash_mismatch")
        os.replace(stage, target)
        _fsync_directory(parent)
    except OSError as exc:
        raise ModelCallLedgerRecoveryError("recovery_restore_replace_failed") from exc
    finally:
        with contextlib.suppress(OSError):
            # trusted-scan: backup owner=model_call_ledger target=restore_stage expires=never
            stage.unlink(missing_ok=True)


def _remove_target_sidecars(target: Path) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = Path(str(target) + suffix)
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ModelCallLedgerRecoveryError("recovery_sidecar_uninspectable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ModelCallLedgerRecoveryError("recovery_sidecar_not_regular")
        try:
            # trusted-scan: manual_repair owner=model_call_ledger target=restore_runtime_sidecar expires=never
            sidecar.unlink()
        except OSError as exc:
            raise ModelCallLedgerRecoveryError("recovery_sidecar_remove_failed") from exc
    _fsync_directory(target.parent)


def _remove_runtime_target(target: Path) -> None:
    try:
        target.lstat()
    except FileNotFoundError:
        pass
    else:
        _lstat_regular(target, private=False)
        try:
            # trusted-scan: manual_repair owner=model_call_ledger target=restore_runtime_target expires=never
            target.unlink()
        except OSError as exc:
            raise ModelCallLedgerRecoveryError("recovery_target_remove_failed") from exc
        _fsync_directory(target.parent)
    _remove_target_sidecars(target)


def _restore_target(target: Path, entry: Mapping[str, Any], root: Path) -> None:
    preimage = entry["preimage"]
    if preimage.get("state") == "absent":
        _remove_runtime_target(target)
        return
    binding = preimage.get("backup")
    if not isinstance(binding, Mapping):
        raise ModelCallLedgerRecoveryError("recovery_manifest_backup_missing")
    complete_binding = dict(binding)
    complete_binding["target_id"] = str(entry.get("target_id") or "")
    source = _verify_backup_binding(root, complete_binding)
    _copy_private_backup_to_target(source, target, str(binding.get("sha256") or ""))
    _remove_target_sidecars(target)
    restored = _file_identity(target, private=False)
    if restored.get("sha256") != binding.get("sha256") or _sqlite_integrity(target) != "ok":
        raise ModelCallLedgerRecoveryError("recovery_restored_target_verification_failed")


def _reverse_backup_path(root: Path, binding: Mapping[str, Any]) -> Path:
    relative_name = str(binding.get("relative_name") or "")
    parts = Path(relative_name).parts
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        raise ModelCallLedgerRecoveryError("recovery_reverse_backup_path_invalid")
    directory = _safe_relative_child(root, parts[0], private=True)
    return _safe_relative_child(directory, parts[1], private=True)


def _verify_reverse_component(root: Path, binding: Mapping[str, Any]) -> Path:
    path = _reverse_backup_path(root, binding)
    identity = _file_identity(path, private=True)
    for key in ("sha256", "byte_size", "mode", "uid", "device", "inode", "mtime_ns", "ctime_ns"):
        if identity.get(key) != binding.get(key):
            raise ModelCallLedgerRecoveryError("recovery_reverse_backup_binding_mismatch")
    return path


def _capture_reverse_target(
    target_id: str, target: Path, reverse_dir: Path, reverse_backup_id: str
) -> dict[str, Any]:
    """Capture every byte component needed to compensate one restore intent."""
    current = _target_identity(target)
    if current.get("state") == "absent":
        if any(Path(str(target) + suffix).exists() for suffix in _SIDECAR_SUFFIXES):
            raise ModelCallLedgerRecoveryError("recovery_orphan_sidecar_present")
        return {
            "target_id": target_id,
            "state": "absent",
            "target_state": current,
            "sidecars": [],
        }
    main_name = f"{target_id}.post-restore.db"
    main = {
        "relative_name": f"{reverse_backup_id}/{main_name}",
        **_copy_runtime_file_to_new(target, _safe_relative_child(reverse_dir, main_name, private=True)),
    }
    sidecars: list[dict[str, Any]] = []
    for sidecar in current.get("sidecars", []):
        suffix = str(sidecar.get("suffix") or "")
        state = str(sidecar.get("state") or "")
        if suffix not in _SIDECAR_SUFFIXES or state not in {"present", "absent"}:
            raise ModelCallLedgerRecoveryError("recovery_reverse_sidecar_state_invalid")
        if state == "absent":
            sidecars.append({"suffix": suffix, "state": "absent"})
            continue
        component_name = f"{target_id}.post-restore{suffix}"
        sidecars.append(
            {
                "suffix": suffix,
                "state": "present",
                "relative_name": f"{reverse_backup_id}/{component_name}",
                **_copy_runtime_file_to_new(
                    Path(str(target) + suffix),
                    _safe_relative_child(reverse_dir, component_name, private=True),
                ),
            }
        )
    if not _state_matches(current, _target_identity(target)):
        raise ModelCallLedgerRecoveryError("recovery_reverse_source_changed_during_backup")
    return {
        "target_id": target_id,
        "state": "present",
        "target_state": current,
        "main": main,
        "sidecars": sidecars,
    }


def _verify_reverse_entry(root: Path, entry: Mapping[str, Any]) -> None:
    state = str(entry.get("state") or "")
    target_state = entry.get("target_state")
    if not isinstance(target_state, Mapping) or str(target_state.get("state") or "") != state:
        raise ModelCallLedgerRecoveryError("recovery_reverse_backup_state_invalid")
    if state == "absent":
        if entry.get("sidecars") not in ([], None):
            raise ModelCallLedgerRecoveryError("recovery_reverse_backup_state_invalid")
        return
    if state != "present":
        raise ModelCallLedgerRecoveryError("recovery_reverse_backup_state_invalid")
    main = entry.get("main")
    sidecars = entry.get("sidecars")
    if not isinstance(main, Mapping) or not isinstance(sidecars, list):
        raise ModelCallLedgerRecoveryError("recovery_reverse_backup_state_invalid")
    _verify_reverse_component(root, main)
    if [str(item.get("suffix") or "") for item in sidecars if isinstance(item, Mapping)] != list(
        _SIDECAR_SUFFIXES
    ) or len(sidecars) != len(_SIDECAR_SUFFIXES):
        raise ModelCallLedgerRecoveryError("recovery_reverse_sidecar_state_invalid")
    for sidecar in sidecars:
        if not isinstance(sidecar, Mapping):
            raise ModelCallLedgerRecoveryError("recovery_reverse_sidecar_state_invalid")
        state = str(sidecar.get("state") or "")
        if state == "present":
            _verify_reverse_component(root, sidecar)
        elif state != "absent":
            raise ModelCallLedgerRecoveryError("recovery_reverse_sidecar_state_invalid")


def _restore_reverse_target(target: Path, entry: Mapping[str, Any], root: Path) -> None:
    _verify_reverse_entry(root, entry)
    if str(entry.get("state") or "") == "absent":
        _remove_runtime_target(target)
    else:
        main = entry["main"]
        _remove_target_sidecars(target)
        source = _verify_reverse_component(root, main)
        _copy_private_backup_to_target(source, target, str(main.get("sha256") or ""))
        for sidecar in entry["sidecars"]:
            if str(sidecar.get("state") or "") != "present":
                continue
            sidecar_source = _verify_reverse_component(root, sidecar)
            _copy_private_backup_to_target(
                sidecar_source,
                Path(str(target) + str(sidecar["suffix"])),
                str(sidecar.get("sha256") or ""),
            )
    if not _state_matches(dict(entry["target_state"]), _target_identity(target)):
        raise ModelCallLedgerRecoveryError("recovery_reverse_restore_verification_failed")


def _compensate_reverse_targets(
    target_paths: Mapping[str, Path],
    reverse_entries: Mapping[str, Mapping[str, Any]],
    touched_target_ids: list[str],
    root: Path,
) -> None:
    for target_id in reversed(touched_target_ids):
        entry = reverse_entries.get(target_id)
        if entry is None:
            raise ModelCallLedgerRecoveryError("recovery_reverse_backup_missing")
        _restore_reverse_target(target_paths[target_id], entry, root)


def _verify_restored_preimages(manifest: Mapping[str, Any], config: Any, root: Path) -> None:
    target_paths = _target_paths(config)
    for entry in _target_entries(manifest):
        target = target_paths[str(entry["target_id"])]
        preimage = entry["preimage"]
        if preimage.get("state") == "absent":
            try:
                target.lstat()
            except FileNotFoundError:
                continue
            raise ModelCallLedgerRecoveryError("recovery_preimage_absence_not_restored")
        binding = preimage.get("backup")
        if not isinstance(binding, Mapping):
            raise ModelCallLedgerRecoveryError("recovery_manifest_backup_missing")
        actual = _file_identity(target, private=False)
        if actual.get("sha256") != binding.get("sha256") or _sqlite_integrity(target) != "ok":
            raise ModelCallLedgerRecoveryError("recovery_preimage_hash_not_restored")
        sidecars = _sidecar_identity(target)
        if any(value.get("state") == "present" for value in sidecars):
            raise ModelCallLedgerRecoveryError("recovery_preimage_sidecars_not_cleared")
    # The command script is intentionally a thin wrapper.  Restoration must
    # validate through the same production reconciliation facade as apply.
    from core.migrations.model_call_ledger_reconcile import build_reconciliation_plan

    restored_plan, _ = build_reconciliation_plan(config)
    if reconciliation_semantic_hash(restored_plan) != str(manifest.get("preimage_semantic_hash") or ""):
        raise ModelCallLedgerRecoveryError("recovery_preimage_semantic_proof_mismatch")


def _verify_compensated_restore_retry(
    events: Iterable[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    root: Path,
) -> None:
    """Prove that a previous failed restore is safely back at its postimage.

    ``restore_failed`` is retryable only after all three facts are durable:
    the failure says compensation succeeded, its matching ``restore_started``
    binds every reverse target, and each present reverse backup remains intact.
    The caller separately compares the live files to the sealed postimage
    before issuing a new restore intent.
    """
    event_list = list(events)
    if not event_list or str(event_list[-1].get("event") or "") != "restore_failed":
        raise ModelCallLedgerRecoveryError("recovery_restore_retry_state_invalid")
    failed = event_list[-1]
    if failed.get("compensation_ok") is not True:
        raise ModelCallLedgerRecoveryError("recovery_restore_compensation_incomplete")
    started = next(
        (
            event
            for event in reversed(event_list[:-1])
            if str(event.get("event") or "") == "restore_started"
        ),
        None,
    )
    if not isinstance(started, Mapping):
        raise ModelCallLedgerRecoveryError("recovery_restore_reverse_bindings_missing")
    entries = started.get("reverse_entries")
    target_ids = [str(entry["target_id"]) for entry in _target_entries(manifest)]
    if (
        not isinstance(entries, list)
        or [str(entry.get("target_id") or "") for entry in entries if isinstance(entry, Mapping)]
        != target_ids
        or len(entries) != len(target_ids)
        or str(started.get("reverse_backup_hash") or "") != _hash_payload(entries)
        or str(failed.get("reverse_backup_hash") or "") != _hash_payload(entries)
    ):
        raise ModelCallLedgerRecoveryError("recovery_restore_reverse_bindings_invalid")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ModelCallLedgerRecoveryError("recovery_restore_reverse_bindings_invalid")
        state = str(entry.get("state") or "")
        if state not in {"present", "absent"}:
            raise ModelCallLedgerRecoveryError("recovery_restore_reverse_bindings_invalid")
        _verify_reverse_entry(root, entry)


def _safe_restore_output(
    manifest: Mapping[str, Any],
    manifest_hash: str,
    *,
    status: str,
    ok: bool,
    error: str = "",
    chain_head: str = "",
    reverse_backup_id: str = "",
    partial_recovery: bool = False,
    interrupted_apply: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "status": status,
        "ok": bool(ok),
        "error": error,
        "recovery_id": str(manifest.get("recovery_id") or ""),
        "migration_id": str(manifest.get("migration_id") or ""),
        "expected_plan_hash": str(manifest.get("expected_plan_hash") or ""),
        "manifest_sha256": manifest_hash,
        "recovery_manifest": (
            "<MNEMOS_DIR>/backups/model-call-ledger/" + str(manifest.get("manifest_name") or "")
            if manifest.get("recovery_manifest_ref")
            else ""
        ),
        "target_count": len(_target_entries(manifest)),
        "chain_head": chain_head or str(manifest.get("prepared_chain_head") or ""),
        "reverse_backup_id": reverse_backup_id,
        "partial_recovery": bool(partial_recovery),
        "interrupted_apply": bool(interrupted_apply),
    }


def plan_model_call_ledger_restore(
    config: Any,
    *,
    recovery_manifest: Path,
    ledger_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Read-only restore plan.  No directories, lock files, or backups are made."""
    try:
        manifest, root, manifest_hash = _load_manifest(config, recovery_manifest)
        # Keep the caller-provided path out of the sealed payload itself while
        # still returning it to the operator who explicitly supplied it.
        manifest = dict(manifest)
        manifest["recovery_manifest_ref"] = str(Path(recovery_manifest).expanduser())
        events, head = _read_progress(root, manifest)
        terminal = str(events[-1].get("event") or "")
        apply_event = next(
            (event for event in reversed(events) if event.get("event") == "apply_committed"),
            None,
        )
        _verify_manifest_bindings(
            manifest,
            root,
            manifest_hash,
            ledger_binding,
            terminal_event="apply_committed" if apply_event else terminal,
            chain_head=str(apply_event.get("event_hash") or "") if apply_event else head,
        )
        if terminal == "restore_committed":
            return _safe_restore_output(
                manifest,
                manifest_hash,
                status="blocked",
                ok=False,
                error="recovery_already_restored",
                chain_head=head,
            )
        if terminal == "apply_prepared":
            return _safe_restore_output(
                manifest,
                manifest_hash,
                status="blocked",
                ok=False,
                error="recovery_apply_not_started_no_restore_required",
                chain_head=head,
            )
        if terminal in {"apply_started", "apply_failed"}:
            target_paths = _target_paths(config)
            changed_count = sum(
                not _matches_preimage(entry, _target_identity(target_paths[str(entry["target_id"])]))
                for entry in _target_entries(manifest)
            )
            if not _runtime_writers_are_inactive(Path(RuntimePaths.from_config(config).database_dir)):
                return _safe_restore_output(
                    manifest,
                    manifest_hash,
                    status="blocked",
                    ok=False,
                    error="recovery_daemon_not_inactive",
                    chain_head=head,
                    partial_recovery=bool(changed_count),
                    interrupted_apply=True,
                )
            result = _safe_restore_output(
                manifest,
                manifest_hash,
                status="planned",
                ok=True,
                chain_head=head,
                partial_recovery=bool(changed_count),
                interrupted_apply=True,
            )
            result["interrupted_target_count"] = changed_count
            return result
        retry_after_compensation = terminal == "restore_failed"
        if retry_after_compensation:
            _verify_compensated_restore_retry(events, manifest, root)
        if terminal not in {"apply_committed", "restore_failed"} or not isinstance(
            apply_event, Mapping
        ):
            return _safe_restore_output(
                manifest,
                manifest_hash,
                status="blocked",
                ok=False,
                error="recovery_incomplete_requires_manual_review",
                chain_head=head,
                partial_recovery=True,
            )
        postimages = _postimages_from_event(manifest, apply_event)
        target_paths = _target_paths(config)
        for entry in _target_entries(manifest):
            expected = postimages[str(entry["target_id"])]
            actual = _target_identity(target_paths[str(entry["target_id"])])
            if not _state_matches(expected, actual):
                return _safe_restore_output(
                    manifest,
                    manifest_hash,
                    status="blocked",
                    ok=False,
                    error="recovery_postimage_drift_detected",
                    chain_head=head,
                )
        if not _runtime_writers_are_inactive(Path(RuntimePaths.from_config(config).database_dir)):
            return _safe_restore_output(
                manifest,
                manifest_hash,
                status="blocked",
                ok=False,
                error="recovery_daemon_not_inactive",
                chain_head=head,
            )
        result = _safe_restore_output(
            manifest,
            manifest_hash,
            status="planned",
            ok=True,
            chain_head=head,
        )
        if retry_after_compensation:
            result["retry_after_compensation"] = True
        return result
    except _RECOVERABLE_ERRORS as exc:
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": _clean_error(exc),
        }


def restore_model_call_ledger(
    config: Any,
    *,
    recovery_manifest: Path,
    ledger_binding: Mapping[str, Any] | None,
    apply: bool,
) -> dict[str, Any]:
    """Restore a v3 preimage only after an explicit apply request.

    ``apply=False`` is exactly the read-only plan.  The mutating path takes a
    private non-blocking lock, revalidates every seal and postimage, captures a
    reverse backup before touching a target, and journals each target action.
    """
    reverse_backup_id = ""
    chain_head = ""
    mutation_intent = False
    compensation_ok = True
    manifest: dict[str, Any] | None = None
    root: Path | None = None
    manifest_hash = ""
    target_paths: dict[str, Path] = {}
    reverse_entries: dict[str, Mapping[str, Any]] = {}
    touched_target_ids: list[str] = []
    runtime_lock: _RecoveryLock | None = None
    try:
        if not apply:
            return plan_model_call_ledger_restore(
                config,
                recovery_manifest=recovery_manifest,
                ledger_binding=ledger_binding,
            )
        runtime_lock = acquire_model_call_ledger_migration_lock(config)
        planned = plan_model_call_ledger_restore(
            config,
            recovery_manifest=recovery_manifest,
            ledger_binding=ledger_binding,
        )
        if planned.get("status") != "planned" or not planned.get("ok"):
            return planned
        chain_head = str(planned.get("chain_head") or "")
        loaded_manifest, root, manifest_hash = _load_manifest(config, recovery_manifest)
        manifest = dict(loaded_manifest)
        manifest["recovery_manifest_ref"] = str(Path(recovery_manifest).expanduser())
        with _exclusive_recovery_lock(root):
            rechecked = plan_model_call_ledger_restore(
                config,
                recovery_manifest=recovery_manifest,
                ledger_binding=ledger_binding,
            )
            if rechecked.get("status") != "planned" or not rechecked.get("ok"):
                return rechecked
            events, _ = _read_progress(root, manifest)
            apply_event = next(
                (event for event in reversed(events) if event.get("event") == "apply_committed"),
                None,
            )
            _verify_manifest_bindings(
                manifest,
                root,
                manifest_hash,
                ledger_binding,
                terminal_event=(
                    "apply_committed" if apply_event else str(events[-1].get("event") or "")
                ),
                chain_head=(
                    str(apply_event.get("event_hash") or "")
                    if apply_event
                    else str(events[-1].get("event_hash") or "")
                ),
            )
            target_paths = _target_paths(config)
            entries = _target_entries(manifest)
            reverse_backup_id = "reverse-" + uuid.uuid4().hex
            reverse_dir = _create_private_directory(root, reverse_backup_id)
            reverse_list: list[dict[str, Any]] = []
            for entry in entries:
                target_id = str(entry["target_id"])
                reverse_list.append(
                    _capture_reverse_target(
                        target_id,
                        target_paths[target_id],
                        reverse_dir,
                        reverse_backup_id,
                    )
                )
            reverse_entries = {str(entry["target_id"]): entry for entry in reverse_list}
            reverse_hash = _hash_payload(reverse_list)
            chain_head = _append_progress(
                root,
                str(manifest["journal_file"]),
                {
                    "event": "restore_started",
                    "recovery_id": str(manifest["recovery_id"]),
                    "reverse_backup_id": reverse_backup_id,
                    "reverse_entries": reverse_list,
                    "reverse_backup_hash": reverse_hash,
                    "created_at": _now_iso(),
                    "prev_hash": chain_head,
                },
            )
            for entry in entries:
                target_id = str(entry["target_id"])
                reverse_entry = reverse_entries[target_id]
                chain_head = _append_progress(
                    root,
                    str(manifest["journal_file"]),
                    {
                        "event": "target_restore_intent",
                        "recovery_id": str(manifest["recovery_id"]),
                        "target_id": target_id,
                        "reverse_entry_hash": _hash_payload(reverse_entry),
                        "created_at": _now_iso(),
                        "prev_hash": chain_head,
                    },
                )
                touched_target_ids.append(target_id)
                mutation_intent = True
                _restore_target(target_paths[target_id], entry, root)
                chain_head = _append_progress(
                    root,
                    str(manifest["journal_file"]),
                    {
                        "event": "target_restored",
                        "recovery_id": str(manifest["recovery_id"]),
                        "target_id": target_id,
                        "preimage_state": str(entry["preimage"].get("state") or ""),
                        "created_at": _now_iso(),
                        "prev_hash": chain_head,
                    },
                )
            _verify_restored_preimages(manifest, config, root)
            chain_head = _append_progress(
                root,
                str(manifest["journal_file"]),
                {
                    "event": "restore_committed",
                    "recovery_id": str(manifest["recovery_id"]),
                    "reverse_backup_id": reverse_backup_id,
                    "created_at": _now_iso(),
                    "prev_hash": chain_head,
                },
            )
            return _safe_restore_output(
                manifest,
                manifest_hash,
                status="restored",
                ok=True,
                chain_head=chain_head,
                reverse_backup_id=reverse_backup_id,
            )
    except _RECOVERABLE_ERRORS as exc:
        error = _clean_error(exc)
        if mutation_intent and root is not None and target_paths:
            try:
                _compensate_reverse_targets(target_paths, reverse_entries, touched_target_ids, root)
            except _RECOVERABLE_ERRORS:
                compensation_ok = False
            if manifest is not None:
                try:
                    chain_head = _append_progress(
                        root,
                        str(manifest["journal_file"]),
                        {
                            "event": "restore_failed",
                            "recovery_id": str(manifest["recovery_id"]),
                            "reverse_backup_id": reverse_backup_id,
                            "touched_target_ids": touched_target_ids,
                            "reverse_backup_hash": _hash_payload(list(reverse_entries.values())),
                            "compensation_ok": compensation_ok,
                            "error": error,
                            "created_at": _now_iso(),
                            "prev_hash": chain_head,
                        },
                    )
                except _RECOVERABLE_ERRORS:
                    compensation_ok = False
        if manifest is not None:
            return _safe_restore_output(
                manifest,
                manifest_hash,
                status="blocked",
                ok=False,
                error=error,
                chain_head=chain_head,
                reverse_backup_id=reverse_backup_id,
                partial_recovery=mutation_intent and not compensation_ok,
            )
        return {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "status": "blocked",
            "ok": False,
            "error": error,
            "partial_recovery": mutation_intent and not compensation_ok,
        }
    finally:
        if runtime_lock is not None:
            runtime_lock.close()
