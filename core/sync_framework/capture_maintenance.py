"""Explicit retention maintenance for Capture queue payloads and artifacts."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import stat
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from core.config import get_config
from core.ops.durable_io import (
    DurableIOError,
    secure_directory_tree_inventory,
    secure_remove_directory_tree,
)
from core.ops.exclusive_file_lock import exclusive_file_lock
from core.sync_framework.capture_schema import CaptureQueueSchema

TERMINAL_CAPTURE_STATUSES = ("done", "duplicate", "failed")
TERMINAL_HANDOFF_STATUSES = ("committed", "intentional_skip", "dead")
CAPTURE_RETENTION_PLAN_SCHEMA = "mnemos.capture_retention_plan.v3"


def _serialized_capture_apply(
    function: Callable[..., dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    """Give one process exclusive ownership of the filesystem saga."""

    @wraps(function)
    def wrapped(
        self: "CaptureRetentionMaintenance",
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        with exclusive_file_lock(
            self.db_path.with_name(".capture_retention_apply.lock"),
            unavailable_message="capture_retention_apply_in_progress",
        ):
            return function(self, *args, **kwargs)

    return wrapped


class CaptureRetentionMaintenance:
    """Plan first; delete only under an explicit maintenance invocation."""

    def __init__(self, config: Any | None = None):
        self.config = config or get_config()
        self.db_path = Path(self.config.database_dir) / "capture_queue.db"
        self.artifact_root = Path(self.config.database_dir) / "capture_artifacts"

    def plan(
        self,
        *,
        payload_retention_days: int,
        artifact_retention_days: int,
        artifact_max_total_bytes: int,
    ) -> dict[str, Any]:
        """Return eligible candidates without changing data or creating receipts."""
        policy = self._validate_policy(
            {
                "payload_retention_days": payload_retention_days,
                "artifact_retention_days": artifact_retention_days,
                "artifact_max_total_bytes": artifact_max_total_bytes,
            }
        )
        CaptureQueueSchema.require_current(self.db_path)
        created_at = datetime.now()
        cutoff = (created_at - timedelta(days=policy["payload_retention_days"])).isoformat()
        payload_candidates = self._payload_candidates(cutoff)
        artifact_inventory = self._artifact_inventory()
        artifact_candidates = self._select_artifact_candidates(
            artifact_inventory,
            plan_timestamp=created_at.timestamp(),
            artifact_retention_days=policy["artifact_retention_days"],
            artifact_max_total_bytes=policy["artifact_max_total_bytes"],
        )
        plan = {
            "schema": CAPTURE_RETENTION_PLAN_SCHEMA,
            "db_path": str(self.db_path),
            "artifact_root": str(self.artifact_root),
            "created_at": created_at.isoformat(),
            "policy": policy,
            "payload_cutoff": cutoff,
            "payload_candidates": payload_candidates,
            "artifact_inventory": artifact_inventory,
            "artifact_candidates": artifact_candidates,
        }
        plan["plan_hash"] = self._plan_hash(plan)
        return plan

    @_serialized_capture_apply
    def apply(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Delete only already-planned candidates and append one receipt."""
        if plan.get("schema") != CAPTURE_RETENTION_PLAN_SCHEMA:
            raise ValueError("capture retention plan schema is invalid")
        if Path(str(plan.get("db_path") or "")) != self.db_path:
            raise ValueError("retention plan belongs to another Capture database")
        if Path(str(plan.get("artifact_root") or "")) != self.artifact_root:
            raise ValueError("retention plan belongs to another artifact root")
        if plan.get("plan_hash") != self._plan_hash(plan):
            raise ValueError("capture retention plan hash is invalid")
        created_at = self._require_plan_datetime(plan.get("created_at"), "created_at")
        policy = self._validate_policy(plan.get("policy"))
        cutoff_datetime = self._require_plan_datetime(
            plan.get("payload_cutoff"),
            "payload_cutoff",
        )
        if cutoff_datetime != created_at - timedelta(days=policy["payload_retention_days"]):
            raise ValueError("capture retention payload cutoff is invalid")
        cutoff = cutoff_datetime.isoformat()
        payload_candidates = self._validate_payload_candidates(
            plan.get("payload_candidates"),
            cutoff=cutoff,
        )
        artifact_inventory = self._validate_artifact_candidates(
            plan.get("artifact_inventory"),
            label="inventory",
        )
        artifacts = self._validate_artifact_candidates(
            plan.get("artifact_candidates"),
            label="candidate",
        )
        expected_artifacts = self._select_artifact_candidates(
            artifact_inventory,
            plan_timestamp=created_at.timestamp(),
            artifact_retention_days=policy["artifact_retention_days"],
            artifact_max_total_bytes=policy["artifact_max_total_bytes"],
        )
        if artifacts != expected_artifacts:
            raise ValueError("capture artifact candidate selection is invalid")
        inventory_paths = {str(item["relative_path"]) for item in artifact_inventory}
        relative_paths: list[str] = []
        for item in artifacts:
            relative = str(item.get("relative_path") or "")
            if relative not in inventory_paths:
                raise ValueError("capture artifact candidate is outside inventory")
            relative_paths.append(relative)
        if len(relative_paths) != len(set(relative_paths)):
            raise ValueError("capture artifact candidate denominator is duplicated")
        now = datetime.now().isoformat()
        receipt_material = json.dumps(
            {
                "plan_hash": plan["plan_hash"],
                "payload_candidates": payload_candidates,
                "artifacts": artifacts,
                "cutoff": cutoff,
                "created_at": str(plan.get("created_at") or ""),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        receipt_id = (
            "capture-maint-" + hashlib.sha256(receipt_material.encode("utf-8")).hexdigest()[:32]
        )
        deleted_payloads = 0
        already_missing_payloads = 0
        stale_payloads = 0
        committed_replay_count: int | None = None
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_receipt = conn.execute(
                """
                SELECT candidates_json, applied_count, status
                FROM capture_maintenance_receipts
                WHERE receipt_id=?
                """,
                (receipt_id,),
            ).fetchone()
            if existing_receipt is not None:
                if str(existing_receipt[0]) != receipt_material:
                    raise ValueError("capture maintenance receipt collision")
                existing_status = str(existing_receipt[2])
                if existing_status == "committed":
                    committed_replay_count = int(existing_receipt[1])
                elif existing_status not in {"processing", "partial"}:
                    raise ValueError("capture maintenance receipt status is invalid")
            else:
                conn.execute(
                    """
                    INSERT INTO capture_maintenance_receipts (
                        receipt_id, operation, candidates_json, applied_count, status, created_at
                    ) VALUES (?, 'retention', ?, 0, 'processing', ?)
                    """,
                    (receipt_id, receipt_material, now),
                )
            if committed_replay_count is None:
                handoff_placeholders = ",".join("?" for _ in TERMINAL_HANDOFF_STATUSES)
                for item in payload_candidates:
                    cursor = conn.execute(
                        f"""
                        DELETE FROM capture_events AS event
                        WHERE event.id=?
                          AND event.source_agent=?
                          AND event.session_id=?
                          AND event.raw_revision_id IS ?
                          AND event.status=?
                          AND event.created_at=?
                          AND event.created_at < ?
                          AND NOT EXISTS (
                              SELECT 1 FROM capture_distillation_handoffs AS handoff
                              WHERE handoff.source_agent=event.source_agent
                                AND handoff.session_id=event.session_id
                                AND handoff.status NOT IN ({handoff_placeholders})
                          )
                        """,  # nosec B608 - placeholder count is fixed by terminal state contracts.
                        (
                            int(item["id"]),
                            str(item["source_agent"]),
                            str(item["session_id"]),
                            item.get("raw_revision_id"),
                            str(item["status"]),
                            str(item["created_at"]),
                            cutoff,
                            *TERMINAL_HANDOFF_STATUSES,
                        ),
                    )
                    if cursor.rowcount == 1:
                        deleted_payloads += 1
                    elif (
                        conn.execute(
                            "SELECT 1 FROM capture_events WHERE id=?",
                            (int(item["id"]),),
                        ).fetchone()
                        is None
                    ):
                        already_missing_payloads += 1
                    else:
                        stale_payloads += 1
                conn.execute(
                    """
                    UPDATE capture_maintenance_receipts
                    SET applied_count=?, status='processing'
                    WHERE receipt_id=?
                    """,
                    (deleted_payloads + already_missing_payloads, receipt_id),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

        if committed_replay_count is not None:
            return {
                "receipt_id": receipt_id,
                "status": "committed",
                "deleted_payloads": 0,
                "already_missing_payloads": 0,
                "stale_payloads": 0,
                "deleted_artifacts": 0,
                "already_missing_artifacts": 0,
                "deleted_artifact_bytes": 0,
                "stale_artifacts": 0,
                "already_applied_count": committed_replay_count,
                "replayed": True,
            }

        deleted_artifacts = 0
        already_missing_artifacts = 0
        deleted_artifact_bytes = 0
        stale_artifacts = 0
        for item in artifacts:
            candidate_path = self.artifact_root / str(item["relative_path"])
            try:
                candidate_path.lstat()
            except FileNotFoundError:
                already_missing_artifacts += 1
                continue
            except OSError:
                stale_artifacts += 1
                continue
            try:
                # trusted-scan: artifact owner=sync_framework target=capture_artifact_retention expires=never
                _file_count, size = secure_remove_directory_tree(
                    self.artifact_root,
                    str(item["relative_path"]),
                    expected_inventory_sha256=str(item["inventory_sha256"]),
                )
                deleted_artifacts += 1
                deleted_artifact_bytes += size
            except DurableIOError:
                stale_artifacts += 1
        status = "committed" if stale_payloads == 0 and stale_artifacts == 0 else "partial"
        applied_count = (
            deleted_payloads
            + already_missing_payloads
            + deleted_artifacts
            + already_missing_artifacts
        )
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE capture_maintenance_receipts
                SET applied_count=?, status=?
                WHERE receipt_id=? AND status IN ('processing', 'partial')
                """,
                (applied_count, status, receipt_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("capture maintenance receipt finalization lost ownership")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            "receipt_id": receipt_id,
            "status": status,
            "deleted_payloads": deleted_payloads,
            "already_missing_payloads": already_missing_payloads,
            "stale_payloads": stale_payloads,
            "deleted_artifacts": deleted_artifacts,
            "already_missing_artifacts": already_missing_artifacts,
            "deleted_artifact_bytes": deleted_artifact_bytes,
            "stale_artifacts": stale_artifacts,
            "already_applied_count": 0,
            "replayed": False,
        }

    @staticmethod
    def _plan_hash(plan: dict[str, Any]) -> str:
        material = {key: value for key, value in plan.items() if key != "plan_hash"}
        return hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _require_plan_datetime(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise ValueError(f"capture retention {field} is invalid")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"capture retention {field} is invalid") from None
        if parsed.isoformat() != value:
            raise ValueError(f"capture retention {field} is not canonical")
        return parsed

    @staticmethod
    def _validate_policy(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise ValueError("capture retention policy is invalid")
        expected = {
            "payload_retention_days",
            "artifact_retention_days",
            "artifact_max_total_bytes",
        }
        if set(value) != expected:
            raise ValueError("capture retention policy fields are invalid")
        result: dict[str, int] = {}
        for key in sorted(expected):
            item = value[key]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError("capture retention policy value is invalid")
            result[key] = item
        return result

    @staticmethod
    def _validate_payload_candidates(
        value: Any,
        *,
        cutoff: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError("capture payload candidate denominator is invalid")
        candidates: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        required = {
            "id",
            "source_agent",
            "session_id",
            "raw_revision_id",
            "status",
            "created_at",
        }
        for item in value:
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("capture payload candidate is invalid")
            candidate_id = item["id"]
            if (
                isinstance(candidate_id, bool)
                or not isinstance(candidate_id, int)
                or candidate_id <= 0
                or candidate_id in seen_ids
            ):
                raise ValueError("capture payload candidate id is invalid")
            source_agent = item["source_agent"]
            session_id = item["session_id"]
            raw_revision_id = item["raw_revision_id"]
            status_value = item["status"]
            created_at = item["created_at"]
            if (
                not isinstance(source_agent, str)
                or not source_agent
                or not isinstance(session_id, str)
                or not session_id
                or (raw_revision_id is not None and not isinstance(raw_revision_id, str))
                or status_value not in TERMINAL_CAPTURE_STATUSES
                or not isinstance(created_at, str)
                or not created_at
                or created_at >= cutoff
            ):
                raise ValueError("capture payload candidate fields are invalid")
            try:
                datetime.fromisoformat(created_at)
            except ValueError:
                raise ValueError("capture payload candidate timestamp is invalid") from None
            seen_ids.add(candidate_id)
            candidates.append(dict(item))
        return candidates

    def _validate_artifact_candidates(
        self,
        value: Any,
        *,
        label: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise ValueError(f"capture artifact {label} denominator is invalid")
        candidates: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        required = {
            "path",
            "relative_path",
            "mtime",
            "bytes",
            "inventory_sha256",
        }
        for item in value:
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError(f"capture artifact {label} is invalid")
            relative = item["relative_path"]
            digest = item["inventory_sha256"]
            byte_count = item["bytes"]
            modified = item["mtime"]
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or len(Path(relative).parts) != 1
                or Path(relative).parts[0] in {"", ".", ".."}
                or relative in seen_paths
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or isinstance(byte_count, bool)
                or not isinstance(byte_count, int)
                or byte_count < 0
                or isinstance(modified, bool)
                or not isinstance(modified, (int, float))
                or not math.isfinite(float(modified))
                or Path(str(item.get("path") or "")).absolute()
                != (self.artifact_root / relative).absolute()
            ):
                raise ValueError(f"capture artifact {label} scope is invalid")
            seen_paths.add(relative)
            candidates.append(dict(item))
        return candidates

    def _payload_candidates(self, cutoff: str) -> list[dict[str, Any]]:
        # Maintenance is an explicit operator action, not a diagnostic.  Open
        # the live database view so the plan includes committed WAL frames;
        # status/health reads use CaptureStatusReader's side-effect-free path.
        conn = sqlite3.connect(str(self.db_path))
        try:
            terminal_placeholders = ",".join("?" for _ in TERMINAL_CAPTURE_STATUSES)
            handoff_placeholders = ",".join("?" for _ in TERMINAL_HANDOFF_STATUSES)
            rows = conn.execute(
                f"""
                SELECT event.id, event.source_agent, event.session_id,
                       event.raw_revision_id, event.status, event.created_at
                FROM capture_events AS event
                WHERE event.created_at < ?
                  AND event.status IN ({terminal_placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM capture_distillation_handoffs AS handoff
                      WHERE handoff.source_agent=event.source_agent
                        AND handoff.session_id=event.session_id
                        AND handoff.status NOT IN ({handoff_placeholders})
                  )
                ORDER BY event.id
                """,  # nosec B608 - placeholder count is fixed by terminal state contracts.
                (cutoff, *TERMINAL_CAPTURE_STATUSES, *TERMINAL_HANDOFF_STATUSES),
            ).fetchall()
            keys = (
                "id",
                "source_agent",
                "session_id",
                "raw_revision_id",
                "status",
                "created_at",
            )
            return [dict(zip(keys, row)) for row in rows]
        finally:
            conn.close()

    def _artifact_inventory(self) -> list[dict[str, Any]]:
        try:
            root_metadata = self.artifact_root.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            raise DurableIOError("capture_artifact_root_unavailable") from None
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise DurableIOError("capture_artifact_root_unsafe")
        directories: list[dict[str, Any]] = []
        for path in sorted(self.artifact_root.iterdir(), key=lambda item: item.name):
            try:
                metadata = path.lstat()
            except OSError:
                raise DurableIOError("capture_artifact_inventory_unavailable") from None
            if stat.S_ISLNK(metadata.st_mode):
                raise DurableIOError("capture_artifact_inventory_unsafe")
            if not stat.S_ISDIR(metadata.st_mode):
                continue
            inventory = secure_directory_tree_inventory(
                self.artifact_root,
                path.name,
            )
            total_bytes = inventory.get("total_bytes")
            if isinstance(total_bytes, bool) or not isinstance(total_bytes, int):
                raise DurableIOError("capture_artifact_inventory_invalid")
            directories.append(
                {
                    "path": str(path),
                    "relative_path": path.name,
                    "mtime": metadata.st_mtime,
                    "bytes": total_bytes,
                    "inventory_sha256": str(inventory["inventory_sha256"]),
                }
            )
        return directories

    @staticmethod
    def _select_artifact_candidates(
        directories: list[dict[str, Any]],
        *,
        plan_timestamp: float,
        artifact_retention_days: int,
        artifact_max_total_bytes: int,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        selected_paths: set[str] = set()
        total = sum(item["bytes"] for item in directories)
        for item in directories:
            if plan_timestamp - item["mtime"] > artifact_retention_days * 86400:
                selected.append(item)
                selected_paths.add(item["path"])
                total -= item["bytes"]
        if total > artifact_max_total_bytes:
            for item in sorted(directories, key=lambda value: (value["mtime"], value["path"])):
                if total <= artifact_max_total_bytes:
                    break
                if item["path"] in selected_paths:
                    continue
                selected.append(item)
                selected_paths.add(item["path"])
                total -= item["bytes"]
        return selected
