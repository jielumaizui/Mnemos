#!/usr/bin/env python3
"""Inventory legacy Raw-projection backups without reading or deleting Raw content.

The v1 projector could move an entire vault into directories named
``raw-vault-projection-*``.  This audit deliberately reports only filesystem
metadata and the optional v2 change-manifest hash.  It never opens Markdown
contents, mutates timestamps, or removes a backup; deletion remains an
explicit, separately approved lifecycle operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import get_config
from core.ops.durable_io import DurableIOError, secure_read_bytes


SCHEMA_VERSION = "mnemos.raw_projection_backup_audit.v1"
CHANGE_MANIFEST_NAME = "raw-projection-change-manifest.json"
CHANGE_MANIFEST_SCHEMA = "mnemos.raw_projection_change_set.v1"
CONTENT_ADDRESSED_MANIFEST_PATTERN = re.compile(
    r"^raw-projection-(plan|commit|abort|change|recovery)-([0-9a-f]{64})\.json$"
)
DEFAULT_MAX_TOTAL_MB = 1024


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _metadata_inventory(backup_dir: Path) -> dict[str, Any]:
    """Return a deterministic regular-file inventory without following links."""
    entries: list[tuple[str, int, int]] = []
    symlink_count = 0
    newest_mtime_ns = 0
    for root, dirs, files in os.walk(backup_dir, followlinks=False):
        dirs[:] = sorted(dirs)
        for name in sorted(files):
            path = Path(root) / name
            try:
                file_stat = path.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(file_stat.st_mode):
                symlink_count += 1
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            relative_path = path.relative_to(backup_dir).as_posix()
            entries.append((relative_path, int(file_stat.st_size), int(file_stat.st_mtime_ns)))
            newest_mtime_ns = max(newest_mtime_ns, int(file_stat.st_mtime_ns))
    inventory_hash = _sha256_bytes(
        json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return {
        "file_count": len(entries),
        "total_bytes": sum(size for _path, size, _mtime in entries),
        "inventory_hash": inventory_hash,
        "newest_mtime_ns": newest_mtime_ns,
        "symlink_count": symlink_count,
        "file_names": {path for path, _size, _mtime in entries},
    }


def _manifest_metadata(backup_dir: Path) -> dict[str, Any]:
    manifest_paths: list[Path] = []
    for path in sorted(backup_dir.iterdir()):
        if not (
            path.name == CHANGE_MANIFEST_NAME
            or CONTENT_ADDRESSED_MANIFEST_PATTERN.fullmatch(path.name)
        ):
            continue
        try:
            metadata = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            manifest_paths.append(path)
    if not manifest_paths:
        return {
            "present": False,
            "valid": False,
            "count": 0,
            "sha256": "",
            "set_hash": _sha256_bytes(b"[]"),
            "file_names": set(),
        }

    entries: list[tuple[str, str]] = []
    receipts: list[tuple[str, str, dict[str, Any]]] = []
    all_valid = True
    for manifest_path in manifest_paths:
        try:
            raw = secure_read_bytes(backup_dir, manifest_path.name)
            if raw is None:
                raise DurableIOError("raw_projection_manifest_missing")
        except (DurableIOError, OSError):
            all_valid = False
            continue
        file_digest = _sha256_bytes(raw)
        entries.append((manifest_path.name, file_digest))
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            all_valid = False
            continue
        match = CONTENT_ADDRESSED_MANIFEST_PATTERN.fullmatch(manifest_path.name)
        kind = match.group(1) if match else "change"
        identity = match.group(2) if match else ""
        expected_keys = {
            "plan": {
                "schema_version",
                "status",
                "plan_hash",
                "generation_hash",
                "backup_dir",
                "changed_paths",
                "stale_paths",
                "index_changed_paths",
                "index_deleted_paths",
            },
            "commit": {
                "schema_version",
                "status",
                "plan_hash",
                "generation_hash",
                "changed_paths",
                "stale_paths",
                "index_changed_paths",
                "index_deleted_paths",
                "bytes_written",
            },
            "abort": {
                "schema_version",
                "status",
                "plan_hash",
                "generation_hash",
                "changed_paths",
                "stale_paths",
                "index_changed_paths",
                "index_deleted_paths",
            },
            "recovery": {
                "schema_version",
                "status",
                "plan_hash",
                "generation_hash",
                "changed_paths",
                "stale_paths",
            },
            "change": {
                "schema_version",
                "generation_hash",
                "changed_paths",
                "unchanged_paths",
                "stale_paths",
                "bytes_written",
            },
        }[kind]
        valid = isinstance(parsed, dict) and parsed.get(
            "schema_version"
        ) == CHANGE_MANIFEST_SCHEMA
        valid = bool(valid and set(parsed) == expected_keys)
        plan_hash = str(parsed.get("plan_hash") or "")
        generation_hash = str(parsed.get("generation_hash") or "")
        sha256_pattern = re.compile(r"^[0-9a-f]{64}$")
        path_fields = ("changed_paths", "stale_paths")
        valid = bool(
            valid
            and sha256_pattern.fullmatch(generation_hash)
            and all(
                isinstance(parsed.get(field), list)
                and parsed[field] == sorted(set(parsed[field]))
                and all(isinstance(item, str) and item for item in parsed[field])
                for field in path_fields
            )
        )
        if match:
            expected_status = {
                "plan": "planned",
                "commit": "committed",
                "abort": "aborted_before_publish",
                "recovery": "recovered_for_replan",
            }.get(kind)
            valid = bool(
                valid
                and identity == (plan_hash or generation_hash)
                and (
                    kind == "change"
                    or (
                        sha256_pattern.fullmatch(plan_hash)
                        and parsed.get("status") == expected_status
                    )
                )
            )
            if kind in {"plan", "commit", "abort"}:
                valid = bool(
                    valid
                    and all(
                        isinstance(parsed.get(field), list)
                        and parsed[field] == sorted(set(parsed[field]))
                        for field in (
                            "index_changed_paths",
                            "index_deleted_paths",
                        )
                    )
                )
            if kind == "plan":
                backup_scope = parsed.get("backup_dir")
                valid = bool(
                    valid
                    and isinstance(backup_scope, str)
                    and Path(backup_scope).is_absolute()
                    and Path(backup_scope).resolve() == backup_dir.resolve()
                )
            if kind == "commit":
                valid = bool(
                    valid
                    and isinstance(parsed.get("bytes_written"), int)
                    and parsed["bytes_written"] >= 0
                )
            if kind == "change":
                valid = bool(
                    valid
                    and isinstance(parsed.get("unchanged_paths"), list)
                    and parsed["unchanged_paths"]
                    == sorted(set(parsed["unchanged_paths"]))
                    and isinstance(parsed.get("bytes_written"), int)
                    and parsed["bytes_written"] >= 0
                )
        else:
            valid = bool(
                valid
                and sha256_pattern.fullmatch(generation_hash)
                and isinstance(parsed.get("unchanged_paths"), list)
                and isinstance(parsed.get("bytes_written"), int)
                and parsed["bytes_written"] >= 0
            )
        all_valid = all_valid and valid
        if valid:
            receipts.append((kind, plan_hash or generation_hash, parsed))

    terminal_by_identity = {
        identity
        for kind, identity, _payload in receipts
        if kind in {"commit", "recovery", "abort"}
    }
    planned_identities = {
        identity for kind, identity, _payload in receipts if kind == "plan"
    }
    terminal_plan_identities = {
        identity
        for kind, identity, _payload in receipts
        if kind in {"commit", "recovery", "abort"}
    }
    if terminal_plan_identities - planned_identities:
        all_valid = False
    if planned_identities - terminal_by_identity:
        all_valid = False
    plans_by_identity = {
        identity: payload
        for kind, identity, payload in receipts
        if kind == "plan"
    }
    for kind, identity, payload in receipts:
        if kind not in {"commit", "recovery", "abort"} or identity not in plans_by_identity:
            continue
        plan = plans_by_identity[identity]
        if (
            payload.get("generation_hash") != plan.get("generation_hash")
            or payload.get("changed_paths") != plan.get("changed_paths")
            or payload.get("stale_paths") != plan.get("stale_paths")
            or (
                kind in {"commit", "abort"}
                and (
                    payload.get("index_changed_paths")
                    != plan.get("index_changed_paths")
                    or payload.get("index_deleted_paths")
                    != plan.get("index_deleted_paths")
                )
            )
        ):
            all_valid = False

    set_hash = _sha256_bytes(
        json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    return {
        "present": True,
        "valid": all_valid and len(entries) == len(manifest_paths),
        "count": len(manifest_paths),
        "sha256": entries[0][1] if len(entries) == 1 else "",
        "set_hash": set_hash,
        "file_names": {path.name for path in manifest_paths},
    }


def _backup_record(backup_dir: Path) -> dict[str, Any]:
    inventory = _metadata_inventory(backup_dir)
    manifest = _manifest_metadata(backup_dir)
    non_manifest_files = inventory["file_names"] - manifest["file_names"]
    if non_manifest_files:
        recovery_class = "legacy_full_copy"
    elif manifest["valid"]:
        recovery_class = "metadata_only"
    elif manifest["present"]:
        recovery_class = "metadata_incomplete"
    else:
        recovery_class = "unknown_legacy"
    return {
        "backup_id": backup_dir.name,
        "recovery_class": recovery_class,
        "manifest": manifest,
        "file_count": inventory["file_count"],
        "total_bytes": inventory["total_bytes"],
        "inventory_hash": inventory["inventory_hash"],
        "newest_mtime_ns": inventory["newest_mtime_ns"],
        "symlink_count": inventory["symlink_count"],
    }


def audit_raw_projection_backups(
    backup_root: Path,
    *,
    max_total_bytes: int,
) -> dict[str, Any]:
    """Inventory known projection backup directories; never clean them up."""
    directories = (
        sorted(path for path in backup_root.glob("raw-vault-projection-*") if path.is_dir())
        if backup_root.is_dir()
        else []
    )
    backups = [_backup_record(path) for path in directories]
    total_bytes = sum(int(item["total_bytes"]) for item in backups)
    over_budget = total_bytes > max_total_bytes
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not over_budget,
        "backup_root": str(backup_root),
        "backup_count": len(backups),
        "total_bytes": total_bytes,
        "budget_bytes": max_total_bytes,
        "over_budget": over_budget,
        "backups": backups,
        "destructive_actions": 0,
        "deletion_authorized": False,
        "repair_action": (
            "Review the metadata inventory and recovery class, then obtain explicit "
            "approval before deleting any legacy backup."
            if over_budget
            else "No deletion was performed; retain recovery evidence until an explicit decision."
        ),
    }


def _default_backup_root() -> Path:
    config = get_config()
    mnemos_dir = Path(
        getattr(config, "mnemos_dir", None)
        or getattr(config, "data_dir", None)
        or config.database_dir
    )
    return mnemos_dir / "backups"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=None,
        help="Directory containing raw-vault-projection-* backups (default: Mnemos backups)",
    )
    parser.add_argument(
        "--max-total-mb",
        type=float,
        default=DEFAULT_MAX_TOTAL_MB,
        help="Manual-retention budget; this command never deletes files",
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    budget_bytes = int(max(0.0, args.max_total_mb) * 1024 * 1024)
    report = audit_raw_projection_backups(
        args.backup_root.expanduser() if args.backup_root else _default_backup_root(),
        max_total_bytes=budget_bytes,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(report)
    return 0 if not args.strict or report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
