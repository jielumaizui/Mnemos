"""Daemon-owned orchestration for canonical Raw vault projection."""

from __future__ import annotations

import hashlib
import json
import re
import time
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, Mapping

from core.ops.durable_io import DurableIOError, inspect_path_kind
from daemon.raw_projection_state import (
    hash_projection_paths,
    raw_projection_actual_paths,
    raw_projection_expected_paths,
    raw_projection_signature,
    raw_projection_state_path,
    write_raw_projection_state,
)
from scripts.raw_projection_secure_io import (
    _secure_atomic_write_text,
    _secure_read_file,
    _secure_unlink_file,
)

RECOVERY_INTENT_SCHEMA = "mnemos.raw_projection_recovery_intent.v1"
RECOVERY_INTENT_NAME = "raw_projection_recovery_intent.json"
_PLAN_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


def _recovery_intent_path(cfg: Any) -> Path:
    return Path(cfg.database_dir) / RECOVERY_INTENT_NAME


def _validated_recovery_intent(
    value: Any,
    *,
    raw_dir: Path,
    backup_dir: Path,
) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "plan_hash",
        "raw_dir",
        "backup_dir",
    }:
        raise RuntimeError("Raw projection recovery intent shape is invalid")
    if value.get("schema_version") != RECOVERY_INTENT_SCHEMA:
        raise RuntimeError("Raw projection recovery intent schema is invalid")
    plan_hash = value.get("plan_hash")
    if not isinstance(plan_hash, str) or _PLAN_HASH_PATTERN.fullmatch(plan_hash) is None:
        raise RuntimeError("Raw projection recovery intent plan hash is invalid")
    if value.get("raw_dir") != str(raw_dir.resolve()):
        raise RuntimeError("Raw projection recovery intent Raw scope does not match")
    if value.get("backup_dir") != str(backup_dir.resolve()):
        raise RuntimeError("Raw projection recovery intent backup scope does not match")
    return {
        "schema_version": RECOVERY_INTENT_SCHEMA,
        "plan_hash": plan_hash,
        "raw_dir": str(raw_dir.resolve()),
        "backup_dir": str(backup_dir.resolve()),
    }


def _load_recovery_intent(
    path: Path,
    *,
    raw_dir: Path,
    backup_dir: Path,
) -> Dict[str, str] | None:
    try:
        content, _digest = _secure_read_file(path.parent, path.name)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Raw projection recovery intent is unreadable") from exc
    if content is None:
        return None
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Raw projection recovery intent is unreadable") from exc
    return _validated_recovery_intent(
        value,
        raw_dir=raw_dir,
        backup_dir=backup_dir,
    )


def _write_recovery_intent(
    path: Path,
    *,
    plan_hash: str,
    raw_dir: Path,
    backup_dir: Path,
) -> None:
    value = _validated_recovery_intent(
        {
            "schema_version": RECOVERY_INTENT_SCHEMA,
            "plan_hash": plan_hash,
            "raw_dir": str(raw_dir.resolve()),
            "backup_dir": str(backup_dir.resolve()),
        },
        raw_dir=raw_dir,
        backup_dir=backup_dir,
    )
    _secure_atomic_write_text(
        path.parent,
        path.name,
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
    )


def _clear_recovery_intent(
    path: Path,
    *,
    expected_plan_hash: str,
    raw_dir: Path,
    backup_dir: Path,
) -> None:
    content, digest = _secure_read_file(path.parent, path.name)
    if content is None:
        raise RuntimeError("Raw projection recovery intent disappeared before cleanup")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Raw projection recovery intent is unreadable") from exc
    validated = _validated_recovery_intent(
        value,
        raw_dir=raw_dir,
        backup_dir=backup_dir,
    )
    if validated["plan_hash"] != expected_plan_hash:
        raise RuntimeError("Raw projection recovery intent changed before cleanup")
    if digest != hashlib.sha256(content).hexdigest():
        raise RuntimeError("Raw projection recovery intent hash is inconsistent")
    if not _secure_unlink_file(path.parent, path.name, expected_hash=digest):
        raise RuntimeError("Raw projection recovery intent disappeared before cleanup")


def _recover_daemon_owned_transaction(
    projection: Any,
    *,
    intent_path: Path,
    raw_dir: Path,
    backup_dir: Path,
) -> Dict[str, Any]:
    intent = _load_recovery_intent(
        intent_path,
        raw_dir=raw_dir,
        backup_dir=backup_dir,
    )
    if intent is None:
        return {"recovered": False, "plan_hash": ""}
    recovery = projection.recover_interrupted_projection(
        raw_dir,
        expected_plan_hash=intent["plan_hash"],
        expected_backup_dir=backup_dir,
    )
    _clear_recovery_intent(
        intent_path,
        expected_plan_hash=intent["plan_hash"],
        raw_dir=raw_dir,
        backup_dir=backup_dir,
    )
    return dict(recovery)


def service_raw_projection(host: Any) -> Dict[str, Any]:
    """Project canonical Raw using a daemon-owned exact recovery identity."""

    def mark_recovered(
        payload: Mapping[str, Any],
        config: Any,
    ) -> dict[str, Any]:
        result = host._mark_service_recovered(
            "raw_projection",
            dict(payload),
            config,
        )
        if not isinstance(result, Mapping):
            raise RuntimeError(
                "raw_projection_recovery_result_invalid"
            )
        return dict(result)

    cfg = None
    try:
        from core.config import get_config
        from scripts import project_raw_vault as projection

        cfg = get_config()
        if not host._service_enabled(cfg, "raw_projection"):
            return {"enabled": False, "status": "disabled"}

        db_path = Path(cfg.database_dir) / "raw_events.db"
        db_kind = inspect_path_kind(db_path)
        if db_kind == "missing":
            return mark_recovered(
                {"enabled": True, "status": "skipped", "reason": "raw_events_missing"},
                cfg,
            )
        if db_kind != "file":
            raise DurableIOError("raw_projection_database_not_regular")

        raw_dir = Path(cfg.obsidian_vault_path)
        max_files = int(cfg.get("raw_projection.max_files", 0))
        chunk_turns = int(cfg.get("raw_projection.chunk_turns", projection.DEFAULT_CHUNK_TURNS))
        max_turn_chars = int(
            cfg.get("raw_projection.max_turn_chars", projection.DEFAULT_MAX_TURN_CHARS)
        )
        max_file_bytes = int(
            cfg.get("raw_projection.max_file_bytes", projection.DEFAULT_MAX_FILE_BYTES)
        )
        include_eligible_delete = bool(cfg.get("raw_projection.include_eligible_delete", False))
        backup_dir = Path(cfg.database_dir) / "backups" / "raw-vault-projection-metadata"
        intent_path = _recovery_intent_path(cfg)
        args = Namespace(
            raw_dir="",
            db_path="",
            backup_dir=str(backup_dir),
            max_files=max_files,
            chunk_turns=chunk_turns,
            max_turn_chars=max_turn_chars,
            max_file_bytes=max_file_bytes,
            include_eligible_delete=include_eligible_delete,
            expected_plan_hash="",
        )

        _recover_daemon_owned_transaction(
            projection,
            intent_path=intent_path,
            raw_dir=raw_dir,
            backup_dir=backup_dir,
        )
        store, chunks, stats = projection.plan_projection(args)
        try:
            planned_raw_dir = Path(stats["raw_dir"])
            expected_paths = raw_projection_expected_paths(planned_raw_dir, chunks, projection)
            actual_paths = projection.managed_projection_paths(planned_raw_dir)
            if not actual_paths and "existing_managed_files" not in stats:
                actual_paths = raw_projection_actual_paths(planned_raw_dir)
            signature = raw_projection_signature(
                db_path=Path(stats["db_path"]),
                raw_dir=planned_raw_dir,
                stats=stats,
                expected_path_hash=hash_projection_paths(expected_paths),
                max_files=max_files,
                chunk_turns=chunk_turns,
                max_turn_chars=max_turn_chars,
                max_file_bytes=max_file_bytes,
                include_eligible_delete=include_eligible_delete,
            )
            projection_plan = stats.get("projection_plan")
            try:
                validated_plan = projection.validate_projection_plan(projection_plan)
            except RuntimeError:
                validated_plan = None
            if projection_plan is None or validated_plan is None:
                raise RuntimeError("Raw projection daemon requires a complete validated plan")
            plan_hash = str(validated_plan["plan_hash"])
            _write_recovery_intent(
                intent_path,
                plan_hash=plan_hash,
                raw_dir=planned_raw_dir,
                backup_dir=backup_dir,
            )
            args.expected_plan_hash = plan_hash
            applied = projection.apply_projection(args, store, chunks, stats)
            _clear_recovery_intent(
                intent_path,
                expected_plan_hash=plan_hash,
                raw_dir=planned_raw_dir,
                backup_dir=backup_dir,
            )
            if validated_plan.get("write_set_empty") is True:
                return mark_recovered(
                    {"enabled": True, "status": "skipped", "reason": "up_to_date", **applied},
                    cfg,
                )
            write_raw_projection_state(
                raw_projection_state_path(cfg),
                {
                    "signature": signature,
                    "last_applied_at": time.time(),
                    "last_result": applied,
                },
            )
            return mark_recovered(
                {"enabled": True, "status": "applied", **applied},
                cfg,
            )
        finally:
            store.close()
    except host.DAEMON_OPERATION_ERRORS as exc:
        host._log_service_error("raw_projection", exc)
        if cfg is not None:
            from daemon.runtime_flow_receipts import record_raw_projection_error

            record_raw_projection_error(cfg, host._service_error_state, exc)
        return {"enabled": True, "status": "error", "error": str(exc)}
