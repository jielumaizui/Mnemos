"""Durable forward-only transaction boundary for Raw projection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Dict, List, Tuple, cast
import uuid

from core.app.raw_search import raw_index_content_state
from core.ops.durable_io import (
    DurableIOError,
    inspect_path_kind,
    regular_file_sha256,
)
from scripts.raw_projection_contract import (
    PROJECTION_CONTRACT,
    PROJECTION_JOURNAL_NAME,
    PROJECTION_TRANSACTION_DIR,
    PROJECTION_TRANSACTION_SCHEMA,
    PROJECTION_TRANSACTION_STATE_SCHEMA,
    PROJECTION_TRANSACTION_TOMBSTONE_PREFIX,
    safe_projection_target as _safe_projection_target,
)
from scripts.raw_projection_secure_io import (
    _acquire_projection_transaction_lock,
    _ensure_safe_projection_root,
    _open_secure_directory_path,
    _release_projection_transaction_lock,
    _secure_atomic_write_bytes,
    _secure_delete_managed_file,
    _secure_read_file,
    _write_all,
)

ProjectionArtifact = Any
ProjectionChunk = Any


def _projection_host() -> Any:
    from scripts import project_raw_vault

    return project_raw_vault


def _sha256_text(value: str) -> str:
    return str(_projection_host()._sha256_text(value))


def _chunk_path(raw_dir: Path, chunk: Any) -> Path:
    return Path(_projection_host()._chunk_path(raw_dir, chunk))


def _journal_path(raw_dir: Path) -> Path:
    return Path(_projection_host()._journal_path(raw_dir))


def _managed_projection_paths(raw_dir: Path) -> set[str]:
    return set(_projection_host()._managed_projection_paths(raw_dir))


def render_chunk(*args: Any, **kwargs: Any) -> tuple[str, bool]:
    """Delegate deterministic chunk rendering to the projection owner."""
    rendered = _projection_host().render_chunk(*args, **kwargs)
    if (
        not isinstance(rendered, tuple)
        or len(rendered) != 2
        or not isinstance(rendered[0], str)
        or not isinstance(rendered[1], bool)
    ):
        raise RuntimeError(
            "Raw projection chunk renderer returned invalid output"
        )
    return rendered


def _render_chunk_parts(*args: Any, **kwargs: Any) -> List[Tuple[str, str]]:
    """Delegate paged chunk rendering to the projection owner."""
    rendered = _projection_host().render_chunk_parts(*args, **kwargs)
    if (
        not isinstance(rendered, list)
        or not rendered
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            for item in rendered
        )
        or rendered[0][0] != ""
    ):
        raise RuntimeError(
            "Raw projection chunk part renderer returned invalid output"
        )
    return [(str(suffix), str(text)) for suffix, text in rendered]


def _part_relative_path(base_relative_path: str, part_suffix: str) -> str:
    return str(_projection_host()._part_relative_path(base_relative_path, part_suffix))


def _transaction_path(raw_dir: Path) -> Path:
    return raw_dir / PROJECTION_TRANSACTION_DIR


def _cleanup_projection_transaction_tombstones(raw_dir: Path) -> None:
    try:
        root_fd = _open_secure_directory_path(raw_dir, create=False)
    except FileNotFoundError:
        return
    try:
        for name in os.listdir(root_fd):
            if re.fullmatch(
                re.escape(PROJECTION_TRANSACTION_TOMBSTONE_PREFIX) + r"[0-9a-f]{32}",
                name,
            ) is None:
                continue
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("Raw projection transaction tombstone is unsafe")
            cast(Any, shutil.rmtree)(name, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _remove_projection_transaction(transaction_dir: Path) -> None:
    raw_dir = transaction_dir.parent
    _cleanup_projection_transaction_tombstones(raw_dir)
    try:
        root_fd = _open_secure_directory_path(raw_dir, create=False)
    except FileNotFoundError:
        return
    try:
        try:
            metadata = os.stat(
                transaction_dir.name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("Raw projection transaction path is unsafe")
        tombstone = f"{PROJECTION_TRANSACTION_TOMBSTONE_PREFIX}{uuid.uuid4().hex}"
        os.rename(
            transaction_dir.name,
            tombstone,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        os.fsync(root_fd)
        cast(Any, shutil.rmtree)(tombstone, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def _create_projection_transaction(raw_dir: Path) -> Path:
    try:
        root_fd = _open_secure_directory_path(raw_dir, create=False)
    except OSError as exc:
        raise RuntimeError("Raw projection transaction root is unsafe") from exc
    try:
        try:
            os.mkdir(PROJECTION_TRANSACTION_DIR, dir_fd=root_fd)
        except FileExistsError as exc:
            raise RuntimeError(
                "Raw projection interrupted transaction requires recovery"
            ) from exc
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return _transaction_path(raw_dir)


def _write_projection_transaction_state(
    transaction_dir: Path,
    *,
    status: str,
    plan_hash: str,
    generation_hash: str,
    manifest_hash: str,
) -> None:
    if status not in {
        "preparing",
        "prepared",
        "publishing",
        "published",
        "aborting",
    }:
        raise ValueError("Raw projection transaction status is invalid")
    _projection_host()._secure_atomic_write_text(
        transaction_dir,
        "state.json",
        json.dumps(
            {
                "schema_version": PROJECTION_TRANSACTION_STATE_SCHEMA,
                "status": status,
                "plan_hash": plan_hash,
                "generation_hash": generation_hash,
                "manifest_hash": manifest_hash,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _projection_transaction_entries(transaction_dir: Path) -> Dict[str, int]:
    transaction_fd = _open_secure_directory_path(transaction_dir, create=False)
    try:
        return {
            name: os.stat(
                name,
                dir_fd=transaction_fd,
                follow_symlinks=False,
            ).st_mode
            for name in os.listdir(transaction_fd)
        }
    finally:
        os.close(transaction_fd)


def _promote_projection_transaction_state_temp(
    transaction_dir: Path,
    state_temp_names: List[str],
) -> None:
    transaction_fd = _open_secure_directory_path(transaction_dir, create=False)
    try:
        selected_name = sorted(state_temp_names)[0]
        os.replace(
            selected_name,
            "state.json",
            src_dir_fd=transaction_fd,
            dst_dir_fd=transaction_fd,
        )
        for duplicate_name in sorted(state_temp_names)[1:]:
            os.unlink(duplicate_name, dir_fd=transaction_fd)
        os.fsync(transaction_fd)
    finally:
        os.close(transaction_fd)


def _load_projection_transaction(
    raw_dir: Path,
    *,
    allow_cleanup: bool = False,
) -> Dict[str, Any]:
    if allow_cleanup:
        try:
            _cleanup_projection_transaction_tombstones(raw_dir)
        except OSError as exc:
            raise ValueError("Raw projection vault root is unsafe") from exc
    transaction_dir = _transaction_path(raw_dir)
    try:
        entries = _projection_transaction_entries(transaction_dir)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError("Raw projection vault root is unsafe") from exc
    if "state.json" not in entries:
        prestate_temp_pattern = re.compile(r"\.state\.json\.[0-9a-f]{32}\.tmp")
        if not entries or (
            len(entries) == 1
            and all(
                prestate_temp_pattern.fullmatch(name)
                and stat.S_ISREG(mode)
                for name, mode in entries.items()
            )
        ):
            if allow_cleanup:
                _remove_projection_transaction(transaction_dir)
                return {}
            raise RuntimeError(
                "Raw projection pre-state transaction requires explicit recovery"
            )
        raise RuntimeError("Raw projection transaction state is missing")
    try:
        state_bytes, _state_hash = _secure_read_file(transaction_dir, "state.json")
        if state_bytes is None:
            raise RuntimeError("Raw projection transaction state is missing")
        state = json.loads(state_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Raw projection transaction state is invalid") from exc

    def valid_transaction_state(candidate: Any) -> bool:
        return bool(
            isinstance(candidate, dict)
            and set(candidate)
            == {
                "schema_version",
                "status",
                "plan_hash",
                "generation_hash",
                "manifest_hash",
            }
            and candidate.get("schema_version")
            == PROJECTION_TRANSACTION_STATE_SCHEMA
            and candidate.get("status")
            in {
                "preparing",
                "prepared",
                "publishing",
                "published",
                "aborting",
            }
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(candidate.get("plan_hash") or ""),
            )
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(candidate.get("generation_hash") or ""),
            )
            and (
                (
                    candidate.get("status") == "preparing"
                    and candidate.get("manifest_hash") == ""
                )
                or (
                    candidate.get("status") != "preparing"
                    and re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(candidate.get("manifest_hash") or ""),
                    )
                )
            )
        )

    if not valid_transaction_state(state):
        raise RuntimeError("Raw projection transaction state is invalid")
    state_temp_names = [
        name
        for name, mode in entries.items()
        if re.fullmatch(r"\.state\.json\.[0-9a-f]{32}\.tmp", name)
        and stat.S_ISREG(mode)
    ]
    if state_temp_names:
        state_temp_payloads = []
        state_temp_bytes_values = []
        for state_temp_name in sorted(state_temp_names):
            state_temp_bytes, _state_temp_hash = _secure_read_file(
                transaction_dir,
                state_temp_name,
            )
            state_temp_bytes_values.append(state_temp_bytes)
            try:
                state_temp_payloads.append(
                    json.loads((state_temp_bytes or b"").decode("utf-8"))
                )
            except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "Raw projection transaction state transition is invalid"
                ) from exc
        if any(
            state_temp_bytes != state_temp_bytes_values[0]
            for state_temp_bytes in state_temp_bytes_values[1:]
        ):
            raise RuntimeError(
                "Raw projection transaction state transition is ambiguous"
            )
        pending_state = state_temp_payloads[0]
        try:
            allowed_transitions = {
                "preparing": {"prepared"},
                "prepared": {"publishing", "aborting"},
                "publishing": {"published"},
                "published": set(),
                "aborting": set(),
            }
            if (
                not valid_transaction_state(pending_state)
                or pending_state["status"]
                not in allowed_transitions[state["status"]]
                or pending_state["plan_hash"] != state["plan_hash"]
                or pending_state["generation_hash"] != state["generation_hash"]
                or (
                    state["manifest_hash"]
                    and pending_state["manifest_hash"] != state["manifest_hash"]
                )
            ):
                raise RuntimeError(
                    "Raw projection transaction state transition is invalid"
                )
        except KeyError as exc:
            raise RuntimeError(
                "Raw projection transaction state transition is invalid"
            ) from exc
        if allow_cleanup:
            _promote_projection_transaction_state_temp(
                transaction_dir,
                state_temp_names,
            )
        state = pending_state
    elif any(
        name.startswith(".state.json.") and name.endswith(".tmp")
        for name in entries
    ):
        raise RuntimeError("Raw projection transaction state transition is invalid")
    if state["status"] == "preparing":
        # Publication cannot begin until the durable state advances to
        # prepared.  An interrupted preparing directory therefore contains
        # staging bytes only and is safe to discard before a fresh replan.
        if allow_cleanup:
            _remove_projection_transaction(transaction_dir)
            return {}
        raise RuntimeError(
            "Raw projection preparing transaction requires explicit recovery"
        )
    manifest_bytes, _manifest_file_hash = _secure_read_file(
        transaction_dir,
        "manifest.json",
    )
    if manifest_bytes is None:
        raise RuntimeError("Raw projection prepared transaction manifest is missing")
    try:
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Raw projection transaction manifest is invalid") from exc
    expected_fields = {
        "schema_version",
        "plan_hash",
        "generation_hash",
        "source_epoch_hash",
        "backup_dir",
        "changed_files",
        "stale_files",
        "journal_text",
        "journal_hash",
        "journal_preimage_hash",
        "journal_write",
        "projection_plan",
        "manifest_hash",
    }
    changed_files = payload.get("changed_files")
    stale_files = payload.get("stale_files")
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("schema_version") != PROJECTION_TRANSACTION_SCHEMA
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("plan_hash") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("generation_hash") or ""))
        or not isinstance(payload.get("source_epoch_hash"), str)
        or len(payload["source_epoch_hash"]) not in {0, 64}
        or not isinstance(payload.get("backup_dir"), str)
        or not isinstance(changed_files, dict)
        or not isinstance(stale_files, dict)
        or not isinstance(payload.get("journal_text"), str)
        or _sha256_text(payload["journal_text"]) != payload.get("journal_hash")
        or not isinstance(payload.get("journal_preimage_hash"), str)
        or len(payload["journal_preimage_hash"]) not in {0, 64}
        or type(payload.get("journal_write")) is not bool
        or not isinstance(payload.get("projection_plan"), dict)
    ):
        raise RuntimeError("Raw projection transaction manifest is invalid")
    unsigned_manifest = {
        key: value for key, value in payload.items() if key != "manifest_hash"
    }
    if (
        _sha256_text(
            json.dumps(
                unsigned_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        != payload.get("manifest_hash")
        or payload["manifest_hash"] != state["manifest_hash"]
        or payload["plan_hash"] != state["plan_hash"]
        or payload["generation_hash"] != state["generation_hash"]
    ):
        raise RuntimeError("Raw projection transaction manifest hash is invalid")
    projection_plan = _projection_host()._validated_projection_plan(
        payload["projection_plan"]
    )
    if (
        projection_plan["plan_hash"] != payload["plan_hash"]
        or projection_plan["raw_dir"] != str(raw_dir.resolve())
        or projection_plan["generation_hash"] != payload["generation_hash"]
        or projection_plan["source_epoch_hash"] != payload["source_epoch_hash"]
        or projection_plan["backup_dir"] != payload["backup_dir"]
        or projection_plan["desired_journal_hash"] != payload["journal_hash"]
        or projection_plan["current_journal_hash"]
        != payload["journal_preimage_hash"]
        or projection_plan["journal_write"] is not payload["journal_write"]
        or set(projection_plan["changed_paths"]) != set(changed_files)
        or set(projection_plan["stale_paths"]) != set(stale_files)
    ):
        raise RuntimeError("Raw projection transaction does not match its plan")
    for collection, fields in (
        (changed_files, {"target_hash", "preimage_hash"}),
        (stale_files, {"preimage_hash"}),
    ):
        for relative_path, record in collection.items():
            if (
                not isinstance(relative_path, str)
                or not isinstance(record, dict)
                or set(record) != fields
                or any(
                    not isinstance(record[field], str)
                    or len(record[field]) not in {0, 64}
                    for field in fields
                )
            ):
                raise RuntimeError("Raw projection transaction file record is invalid")
            _safe_projection_target(raw_dir, relative_path)
    for relative_path, record in changed_files.items():
        if (
            projection_plan["desired_file_hashes"].get(relative_path)
            != record["target_hash"]
            or projection_plan["file_preimage_hashes"].get(relative_path)
            != record["preimage_hash"]
        ):
            raise RuntimeError("Raw projection transaction changed file is not plan-bound")
    for relative_path, record in stale_files.items():
        if (
            projection_plan["file_preimage_hashes"].get(relative_path)
            != record["preimage_hash"]
        ):
            raise RuntimeError("Raw projection transaction stale file is not plan-bound")
    payload["_transaction_status"] = state["status"]
    return payload


def _prepare_projection_transaction(
    raw_dir: Path,
    *,
    changed_texts: Dict[str, str],
    stale_paths: List[str],
    journal_text: str,
    plan_hash: str,
    generation_hash: str,
    source_epoch_hash: str,
    backup_dir: str,
    journal_write: bool,
    projection_plan: Dict[str, Any],
    expected_file_preimages: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    transaction_lock_fd = _acquire_projection_transaction_lock(raw_dir)
    transaction_dir = _transaction_path(raw_dir)
    transaction_created = False
    changed_files: Dict[str, Dict[str, str]] = {}
    stale_files: Dict[str, Dict[str, str]] = {}
    try:
        _cleanup_projection_transaction_tombstones(raw_dir)
        transaction_dir = _create_projection_transaction(raw_dir)
        transaction_created = True
        _write_projection_transaction_state(
            transaction_dir,
            status="preparing",
            plan_hash=plan_hash,
            generation_hash=generation_hash,
            manifest_hash="",
        )
        for relative_path, text in sorted(changed_texts.items()):
            _safe_projection_target(raw_dir, relative_path)
            preimage_bytes, observed_preimage_hash = _secure_read_file(
                raw_dir,
                relative_path,
            )
            preimage_hash = (
                expected_file_preimages.get(relative_path, "")
                if expected_file_preimages is not None
                else observed_preimage_hash
            )
            if observed_preimage_hash != preimage_hash:
                raise RuntimeError("Raw projection target preimage changed before staging")
            if preimage_hash:
                _secure_atomic_write_bytes(
                    transaction_dir,
                    (Path("old") / relative_path).as_posix(),
                    preimage_bytes or b"",
                )
            staged = transaction_dir / "new" / relative_path
            _projection_host()._secure_atomic_write_text(
                transaction_dir,
                (Path("new") / relative_path).as_posix(),
                text,
            )
            target_hash = _sha256_text(text)
            if _read_file_hash(staged) != target_hash:
                raise RuntimeError("Raw projection staged chunk hash mismatch")
            changed_files[relative_path] = {
                "target_hash": target_hash,
                "preimage_hash": preimage_hash,
            }
        for relative_path in sorted(stale_paths):
            _safe_projection_target(raw_dir, relative_path)
            preimage_bytes, observed_preimage_hash = _secure_read_file(
                raw_dir,
                relative_path,
            )
            preimage_hash = (
                expected_file_preimages.get(relative_path, "")
                if expected_file_preimages is not None
                else observed_preimage_hash
            )
            if observed_preimage_hash != preimage_hash:
                raise RuntimeError("Raw projection stale preimage changed before staging")
            if preimage_hash:
                _secure_atomic_write_bytes(
                    transaction_dir,
                    (Path("old") / relative_path).as_posix(),
                    preimage_bytes or b"",
                )
            stale_files[relative_path] = {"preimage_hash": preimage_hash}
        journal_preimage, journal_preimage_hash = _secure_read_file(
            raw_dir,
            PROJECTION_JOURNAL_NAME,
        )
        if journal_preimage_hash:
            _secure_atomic_write_bytes(
                transaction_dir,
                "old-journal.json",
                journal_preimage or b"",
            )
        manifest = {
            "schema_version": PROJECTION_TRANSACTION_SCHEMA,
            "plan_hash": plan_hash,
            "generation_hash": generation_hash,
            "source_epoch_hash": source_epoch_hash,
            "backup_dir": backup_dir,
            "changed_files": changed_files,
            "stale_files": stale_files,
            "journal_text": journal_text,
            "journal_hash": _sha256_text(journal_text),
            "journal_preimage_hash": journal_preimage_hash,
            "journal_write": journal_write,
            "projection_plan": projection_plan,
        }
        manifest["manifest_hash"] = _sha256_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        _projection_host()._secure_atomic_write_text(
            transaction_dir,
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        _projection_host()._secure_atomic_write_text(
            transaction_dir,
            "new-journal.json",
            journal_text,
        )
        manifest_hash = manifest.get("manifest_hash")
        if not isinstance(manifest_hash, str):
            raise RuntimeError(
                "Raw projection transaction manifest hash is invalid"
            )
        _write_projection_transaction_state(
            transaction_dir,
            status="prepared",
            plan_hash=plan_hash,
            generation_hash=generation_hash,
            manifest_hash=manifest_hash,
        )
        manifest["_transaction_status"] = "prepared"
        manifest["_transaction_lock_fd"] = transaction_lock_fd
        return manifest
    except BaseException:
        if transaction_created:
            _remove_projection_transaction(transaction_dir)
        _release_projection_transaction_lock(transaction_lock_fd)
        raise


def _publish_projection_transaction(raw_dir: Path, manifest: Dict[str, Any]) -> None:
    transaction_dir = _transaction_path(raw_dir)
    transaction_status = str(manifest.get("_transaction_status") or "")
    if transaction_status == "prepared":
        _write_projection_transaction_state(
            transaction_dir,
            status="publishing",
            plan_hash=manifest["plan_hash"],
            generation_hash=manifest["generation_hash"],
            manifest_hash=manifest["manifest_hash"],
        )
        manifest["_transaction_status"] = "publishing"
    elif transaction_status not in {"publishing", "published"}:
        raise RuntimeError("Raw projection transaction cannot enter publication")
    published_paths: List[str] = manifest.setdefault("_runtime_published_paths", [])
    for relative_path, record in sorted(manifest["changed_files"].items()):
        target = _safe_projection_target(raw_dir, relative_path)
        current_hash = _read_file_hash(target)
        if current_hash == record["target_hash"]:
            continue
        staged = transaction_dir / "new" / relative_path
        _projection_host()._secure_publish_staged_file(
            raw_dir,
            relative_path,
            staged,
            expected_preimage_hash=record["preimage_hash"],
            target_hash=record["target_hash"],
        )
        published_paths.append(relative_path)
    for relative_path, record in sorted(manifest["changed_files"].items()):
        _content, current_hash = _secure_read_file(raw_dir, relative_path)
        if current_hash != record["target_hash"]:
            raise RuntimeError("Raw projection publish hash verification failed")
    if manifest["journal_write"]:
        current_journal_hash = _read_file_hash(_journal_path(raw_dir))
        if current_journal_hash != manifest["journal_hash"]:
            _projection_host()._secure_publish_staged_file(
                raw_dir,
                PROJECTION_JOURNAL_NAME,
                transaction_dir / "new-journal.json",
                expected_preimage_hash=manifest["journal_preimage_hash"],
                target_hash=manifest["journal_hash"],
            )
    for relative_path, record in sorted(manifest["stale_files"].items()):
        _secure_delete_managed_file(
            raw_dir,
            relative_path,
            expected_hash=record["preimage_hash"],
        )
    if manifest["_transaction_status"] != "published":
        _write_projection_transaction_state(
            transaction_dir,
            status="published",
            plan_hash=manifest["plan_hash"],
            generation_hash=manifest["generation_hash"],
            manifest_hash=manifest["manifest_hash"],
        )
    manifest["_transaction_status"] = "published"


def _rollback_projection_transaction(raw_dir: Path, manifest: Dict[str, Any]) -> None:
    transaction_dir = _transaction_path(raw_dir)
    if manifest.get("_transaction_status") in {
        "publishing",
        "published",
        "aborting",
    }:
        # Never overwrite or delete a path after publication has begun.  The
        # durable, plan-bound transaction is the recovery mechanism; restart
        # rolls it forward and then replans any remaining index/source delta.
        return
    # Before publication begins, no vault or journal target can have changed.
    # Discarding staging is both sufficient and avoids any rollback write that
    # could race a concurrent owner or follow a swapped parent path.
    _remove_projection_transaction(transaction_dir)


def recover_interrupted_projection(
    raw_dir: Path,
    *,
    expected_plan_hash: str = "",
    expected_backup_dir: Path | None = None,
) -> Dict[str, Any]:
    """Resolve one interrupted transaction while excluding a live writer."""
    raw_dir_kind = inspect_path_kind(raw_dir)
    if raw_dir_kind == "missing":
        return {"recovered": False, "plan_hash": ""}
    if raw_dir_kind != "directory":
        raise DurableIOError("raw_projection_root_not_directory")
    transaction_lock_fd = _acquire_projection_transaction_lock(raw_dir)
    try:
        return _recover_interrupted_projection_locked(
            raw_dir,
            expected_plan_hash=expected_plan_hash,
            expected_backup_dir=expected_backup_dir,
        )
    finally:
        _release_projection_transaction_lock(transaction_lock_fd)


def _recover_interrupted_projection_locked(
    raw_dir: Path,
    *,
    expected_plan_hash: str = "",
    expected_backup_dir: Path | None = None,
) -> Dict[str, Any]:
    """Roll forward one durable staged generation after process termination."""
    manifest = _load_projection_transaction(raw_dir, allow_cleanup=True)
    if not manifest:
        return {"recovered": False, "plan_hash": ""}
    if expected_plan_hash and manifest["plan_hash"] != expected_plan_hash:
        raise RuntimeError("Raw projection recovery plan hash does not match")
    backup_dir_value = str(manifest.get("backup_dir") or "")
    if (
        expected_backup_dir is not None
        and backup_dir_value
        != str(expected_backup_dir.resolve())
    ):
        raise RuntimeError("Raw projection recovery backup scope does not match")
    if backup_dir_value:
        plan_receipt_name = f"raw-projection-plan-{manifest['plan_hash']}.json"
        try:
            plan_receipt_bytes, _receipt_hash = _secure_read_file(
                Path(backup_dir_value),
                plan_receipt_name,
            )
            if plan_receipt_bytes is None:
                projection_plan = manifest["projection_plan"]
                _write_change_manifest(
                    Path(backup_dir_value),
                    {
                        "schema_version": "mnemos.raw_projection_change_set.v1",
                        "status": "planned",
                        "plan_hash": manifest["plan_hash"],
                        "generation_hash": manifest["generation_hash"],
                        "backup_dir": backup_dir_value,
                        "changed_paths": projection_plan["changed_paths"],
                        "stale_paths": projection_plan["stale_paths"],
                        "index_changed_paths": projection_plan[
                            "index_changed_paths"
                        ],
                        "index_deleted_paths": projection_plan[
                            "index_deleted_paths"
                        ],
                    },
                    receipt_kind="plan",
                )
                plan_receipt_bytes, _receipt_hash = _secure_read_file(
                    Path(backup_dir_value),
                    plan_receipt_name,
                )
                if plan_receipt_bytes is None:
                    raise RuntimeError(
                        "Raw projection recovery planned receipt is missing"
                    )
            plan_receipt = json.loads(
                plan_receipt_bytes.decode("utf-8")
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Raw projection recovery planned receipt is invalid"
            ) from exc
        if (
            not isinstance(plan_receipt, dict)
            or set(plan_receipt)
            != {
                "schema_version",
                "status",
                "plan_hash",
                "generation_hash",
                "backup_dir",
                "changed_paths",
                "stale_paths",
                "index_changed_paths",
                "index_deleted_paths",
            }
            or plan_receipt.get("schema_version")
            != "mnemos.raw_projection_change_set.v1"
            or plan_receipt.get("status") != "planned"
            or plan_receipt.get("plan_hash") != manifest["plan_hash"]
            or plan_receipt.get("generation_hash") != manifest["generation_hash"]
            or Path(str(plan_receipt.get("backup_dir") or "")).resolve()
            != Path(backup_dir_value).resolve()
            or plan_receipt.get("changed_paths")
            != manifest["projection_plan"]["changed_paths"]
            or plan_receipt.get("stale_paths")
            != manifest["projection_plan"]["stale_paths"]
            or plan_receipt.get("index_changed_paths")
            != manifest["projection_plan"]["index_changed_paths"]
            or plan_receipt.get("index_deleted_paths")
            != manifest["projection_plan"]["index_deleted_paths"]
        ):
            raise RuntimeError("Raw projection recovery planned receipt is invalid")
    if manifest["_transaction_status"] == "aborting":
        if not backup_dir_value:
            raise RuntimeError("Raw projection abort recovery receipt scope is missing")
        abort_receipt_path = _write_change_manifest(
            Path(backup_dir_value),
            {
                "schema_version": "mnemos.raw_projection_change_set.v1",
                "status": "aborted_before_publish",
                "plan_hash": manifest["plan_hash"],
                "generation_hash": manifest["generation_hash"],
                "changed_paths": manifest["projection_plan"]["changed_paths"],
                "stale_paths": manifest["projection_plan"]["stale_paths"],
                "index_changed_paths": manifest["projection_plan"][
                    "index_changed_paths"
                ],
                "index_deleted_paths": manifest["projection_plan"][
                    "index_deleted_paths"
                ],
            },
            receipt_kind="abort",
        )
        _remove_projection_transaction(_transaction_path(raw_dir))
        return {
            "recovered": True,
            "plan_hash": manifest["plan_hash"],
            "generation_hash": manifest["generation_hash"],
            "recovery_action": "aborted_before_publish",
            "recovery_receipt_path": abort_receipt_path,
        }
    _projection_host()._publish_projection_transaction(raw_dir, manifest)
    receipt_path = ""
    if backup_dir_value:
        receipt_path = _write_change_manifest(
            Path(backup_dir_value),
            {
                "schema_version": "mnemos.raw_projection_change_set.v1",
                "status": "recovered_for_replan",
                "plan_hash": manifest["plan_hash"],
                "generation_hash": manifest["generation_hash"],
                "changed_paths": sorted(manifest["changed_files"]),
                "stale_paths": sorted(manifest["stale_files"]),
            },
            receipt_kind="recovery",
        )
    _remove_projection_transaction(_transaction_path(raw_dir))
    return {
        "recovered": True,
        "plan_hash": manifest["plan_hash"],
        "generation_hash": manifest["generation_hash"],
        "recovery_action": "rolled_forward_for_replan",
        "recovery_receipt_path": receipt_path,
    }


def _artifact_descriptors(
    raw_dir: Path,
    store: Any,
    chunks: List[ProjectionChunk],
    *,
    db_path: Path,
    max_turn_chars: int,
    max_file_bytes: int = 0,
) -> Tuple[Dict[str, ProjectionArtifact], Dict[str, ProjectionChunk]]:
    artifacts: Dict[str, ProjectionArtifact] = {}
    chunks_by_path: Dict[str, ProjectionChunk] = {}
    for chunk in chunks:
        path = _chunk_path(raw_dir, chunk)
        base_relative_path = path.relative_to(raw_dir).as_posix()
        rendered_parts = _render_chunk_parts(
            store,
            chunk,
            db_path=db_path,
            max_turn_chars=max_turn_chars,
            max_file_bytes=max_file_bytes,
        )
        revision_ids = tuple(chunk.event_ids)
        for suffix, text in rendered_parts:
            relative_path = _part_relative_path(base_relative_path, suffix)
            _safe_projection_target(raw_dir, relative_path)
            if relative_path in artifacts:
                raise ValueError(f"projection path collision: {relative_path}")
            artifacts[relative_path] = _projection_host().ProjectionArtifact(
                relative_path=relative_path,
                text="",
                sha256=_sha256_text(text),
                event_ids=revision_ids,
                logical_event_ids=tuple(chunk.logical_event_ids),
                revision_set_hash=_sha256_text(
                    json.dumps(revision_ids, ensure_ascii=False, separators=(",", ":"))
                ),
                source_agent=chunk.source_agent,
                session_id=chunk.session_id,
                tags=(
                    "raw-retention-projection",
                    f"source={chunk.source_agent}",
                    "canonical=raw_events",
                ),
                index_state=raw_index_content_state(
                    raw_dir,
                    relative_path,
                    text,
                ),
            )
            chunks_by_path[relative_path] = chunk
    return artifacts, chunks_by_path


def _read_file_hash(path: Path) -> str:
    try:
        return regular_file_sha256(path)
    except (DurableIOError, OSError):
        return ""


def _write_change_manifest(
    backup_dir: Path,
    change_set: Dict[str, Any],
    *,
    receipt_kind: str = "change",
) -> str:
    """Write one immutable metadata-only receipt, never a Raw vault copy."""
    identity = str(
        change_set.get("plan_hash")
        or change_set.get("generation_hash")
        or ""
    )
    if (
        not identity
        or len(identity) != 64
        or any(character not in "0123456789abcdef" for character in identity)
        or re.fullmatch(r"[a-z_]{1,32}", receipt_kind) is None
    ):
        raise ValueError("Raw projection receipt identity is invalid")
    target_name = f"raw-projection-{receipt_kind}-{identity}.json"
    content = json.dumps(
        change_set,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    content_bytes = content.encode("utf-8")
    try:
        backup_fd = _open_secure_directory_path(backup_dir, create=True)
    except OSError as exc:
        raise RuntimeError("Raw projection receipt directory is unsafe") from exc
    receipt_temp_pattern = re.compile(
        r"\.raw-projection-(?:plan|commit|abort|change|recovery)-"
        r"[0-9a-f]{64}\.json\.[0-9a-f]{32}\.tmp"
    )
    for entry_name in os.listdir(backup_fd):
        if receipt_temp_pattern.fullmatch(entry_name) is None:
            continue
        metadata = os.stat(
            entry_name,
            dir_fd=backup_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(metadata.st_mode):
            os.close(backup_fd)
            raise RuntimeError("Raw projection receipt staging debris is unsafe")
        os.unlink(entry_name, dir_fd=backup_fd)
    os.fsync(backup_fd)

    def read_existing() -> bytes | None:
        try:
            descriptor = os.open(
                target_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=backup_fd,
            )
        except FileNotFoundError:
            return None
        try:
            chunks: List[bytes] = []
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    return b"".join(chunks)
                chunks.append(block)
        finally:
            os.close(descriptor)

    temporary_name = f".{target_name}.{uuid.uuid4().hex}.tmp"
    temporary_fd = -1
    try:
        existing = read_existing()
        if existing is not None:
            if existing != content_bytes:
                raise RuntimeError("Raw projection receipt identity collision")
            return str(Path(os.path.abspath(backup_dir)) / target_name)
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=backup_fd,
        )
        _write_all(temporary_fd, content_bytes)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1
        try:
            os.link(
                temporary_name,
                target_name,
                src_dir_fd=backup_fd,
                dst_dir_fd=backup_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = read_existing()
            if existing != content_bytes:
                raise RuntimeError("Raw projection receipt identity collision")
        os.fsync(backup_fd)
        return str(Path(os.path.abspath(backup_dir)) / target_name)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=backup_fd)
        except FileNotFoundError:
            pass
        os.close(backup_fd)


def _json_text(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _projection_journal(artifacts: Dict[str, ProjectionArtifact]) -> Dict[str, Any]:
    """Build the deterministic, content-addressed ownership journal."""
    files = {
        relative_path: {
            "content_hash": artifact.sha256,
            "logical_event_ids": list(artifact.logical_event_ids),
            "revision_ids": list(artifact.event_ids),
            "revision_set_hash": artifact.revision_set_hash,
        }
        for relative_path, artifact in sorted(artifacts.items())
    }
    generation_hash = _sha256_text(
        json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return {
        "schema_version": "mnemos.raw_projection.v2",
        "projection_contract": PROJECTION_CONTRACT,
        "generation_hash": generation_hash,
        "files": files,
    }


def write_projection(
    raw_dir: Path,
    store: Any,
    chunks: List[ProjectionChunk],
    *,
    db_path: Path,
    max_turn_chars: int,
    max_file_bytes: int = 0,
    backup_dir: Path | None = None,
    transaction_backup_dir: Path | None = None,
    projection_plan: Dict[str, Any] | None = None,
    before_prepare: Any = None,
    before_publish: Any = None,
    retain_transaction: bool = False,
) -> Dict[str, Any]:
    """Publish only changed Raw chunks and keep unrelated vault files untouched."""
    if callable(before_prepare):
        before_prepare()
    if _load_projection_transaction(raw_dir):
        raise RuntimeError("Raw projection interrupted transaction requires recovery")
    artifacts, chunks_by_path = _artifact_descriptors(
        raw_dir,
        store,
        chunks,
        db_path=db_path,
        max_turn_chars=max_turn_chars,
        max_file_bytes=max_file_bytes,
    )
    previously_managed = _managed_projection_paths(raw_dir)
    changed_paths: List[str] = []
    unchanged_paths: List[str] = []
    bytes_written = 0
    stale_paths = sorted(previously_managed - set(artifacts))
    journal = _projection_journal(artifacts)
    journal_text = _json_text(journal)
    journal_written = _read_file_hash(_journal_path(raw_dir)) != _sha256_text(journal_text)
    changed_texts: Dict[str, str] = {}
    rendered_parts_by_chunk: Dict[int, Dict[str, str]] = {}
    for relative_path, artifact in sorted(artifacts.items()):
        target = _safe_projection_target(raw_dir, relative_path)
        if _read_file_hash(target) == artifact.sha256:
            unchanged_paths.append(relative_path)
            continue
        chunk = chunks_by_path[relative_path]
        rendered = rendered_parts_by_chunk.get(id(chunk))
        if rendered is None:
            base_relative_path = (
                _chunk_path(raw_dir, chunk).relative_to(raw_dir).as_posix()
            )
            rendered = {
                _part_relative_path(base_relative_path, suffix): text
                for suffix, text in _render_chunk_parts(
                    store,
                    chunk,
                    db_path=db_path,
                    max_turn_chars=max_turn_chars,
                    max_file_bytes=max_file_bytes,
                )
            }
            rendered_parts_by_chunk[id(chunk)] = rendered
        text = rendered.get(relative_path)
        if text is None:
            raise RuntimeError("raw projection part render is missing a planned path")
        if _sha256_text(text) != artifact.sha256:
            raise RuntimeError("raw projection plan/render hash mismatch")
        changed_paths.append(relative_path)
        changed_texts[relative_path] = text
        bytes_written += len(text.encode("utf-8"))

    change_set = {
        "schema_version": "mnemos.raw_projection_change_set.v1",
        "generation_hash": journal["generation_hash"],
        "changed_paths": changed_paths,
        "unchanged_paths": unchanged_paths,
        "stale_paths": stale_paths,
        "bytes_written": bytes_written,
    }
    deleted_stale_paths: List[str] = []
    manifest_path = ""
    transaction_manifest: Dict[str, Any] = {}
    transaction_lock_fd = -1
    receipt_backup_root: Path | None = None
    has_filesystem_effect = bool(changed_paths or stale_paths or journal_written)
    if has_filesystem_effect or retain_transaction:
        if has_filesystem_effect:
            _ensure_safe_projection_root(raw_dir)
        else:
            root_fd = _open_secure_directory_path(raw_dir, create=False)
            os.close(root_fd)
        effective_backup_dir = transaction_backup_dir or backup_dir
        validated_plan = _projection_host()._validated_projection_plan(
            projection_plan
            if projection_plan is not None
            else _projection_host().build_projection_plan(
                raw_dir,
                store,
                chunks,
                db_path=db_path,
                max_turn_chars=max_turn_chars,
                max_file_bytes=max_file_bytes,
                backup_dir=effective_backup_dir,
            )
        )
        receipt_backup_root = (
            Path(validated_plan["backup_dir"])
            if validated_plan["backup_dir"]
            else None
        )
        if (
            validated_plan["generation_hash"] != journal["generation_hash"]
            or list(validated_plan["changed_paths"]) != changed_paths
            or list(validated_plan["stale_paths"]) != stale_paths
            or validated_plan["journal_write"] is not journal_written
        ):
            raise RuntimeError(
                "Raw projection transaction effects diverged from the bound plan"
            )
        transaction_manifest = _prepare_projection_transaction(
            raw_dir,
            changed_texts=changed_texts,
            stale_paths=stale_paths,
            journal_text=journal_text,
            plan_hash=str(validated_plan["plan_hash"]),
            generation_hash=journal["generation_hash"],
            source_epoch_hash=str(validated_plan["source_epoch_hash"]),
            backup_dir=(
                str(transaction_backup_dir.resolve())
                if transaction_backup_dir is not None
                else (str(backup_dir.resolve()) if backup_dir is not None else "")
            ),
            journal_write=journal_written,
            projection_plan=validated_plan,
            expected_file_preimages=dict(validated_plan["file_preimage_hashes"]),
        )
        transaction_lock_fd = int(
            transaction_manifest.get("_transaction_lock_fd", -1)
        )
    try:
        if transaction_manifest and callable(before_publish):
            before_publish()
        if transaction_manifest and has_filesystem_effect:
            _projection_host()._publish_projection_transaction(
                raw_dir,
                transaction_manifest,
            )
            deleted_stale_paths = [
                relative_path
                for relative_path, record in transaction_manifest["stale_files"].items()
                if record["preimage_hash"]
            ]
        if backup_dir is not None and (changed_paths or stale_paths or journal_written):
            manifest_path = _write_change_manifest(
                receipt_backup_root or backup_dir,
                change_set,
            )
    except BaseException:
        if transaction_manifest and not retain_transaction:
            _rollback_projection_transaction(raw_dir, transaction_manifest)
        _release_projection_transaction_lock(transaction_lock_fd)
        raise
    if transaction_manifest and not retain_transaction:
        _remove_projection_transaction(_transaction_path(raw_dir))
        _release_projection_transaction_lock(transaction_lock_fd)
    return {
        "projected_files": len(artifacts),
        "truncated_chunks": 0,
        "written_files": len(changed_paths),
        "unchanged_files": len(unchanged_paths),
        "deleted_stale_files": len(deleted_stale_paths),
        "bytes_written": bytes_written,
        "journal_written": journal_written,
        "changed_paths": changed_paths,
        "deleted_stale_paths": deleted_stale_paths,
        "desired_file_hashes": {
            relative_path: artifact.sha256 for relative_path, artifact in sorted(artifacts.items())
        },
        "moved_old_files": 0,
        "moved_old_md_files": 0,
        "unrelated_files_moved": 0,
        "backup_manifest_path": manifest_path,
        "transaction_retained": bool(transaction_manifest and retain_transaction),
        "_transaction_lock_fd": (
            transaction_lock_fd
            if transaction_manifest and retain_transaction
            else -1
        ),
    }
