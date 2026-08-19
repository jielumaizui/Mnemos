"""Terminal receipt validation primitives for Amphora.

The queue module re-exports these helpers; this module keeps immutable payload,
anchor, and outbox validation separate from queue scheduling.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Mapping

from core.kia.amphora_types import MINUTES_SECONDS
from core.pipeline_receipts import (
    DistillationWriteReceipt,
    canonical_distillation_write_receipt_payload,
    distillation_failed_terminal_sha256,
    distillation_write_receipt_sha256,
)

_DB_PATH_PROVIDER: Callable[[], Path] | None = None
_ROW_TO_DICT_PROVIDER: Callable[[sqlite3.Row], dict] | None = None


def bind_terminal_contract_runtime(
    *,
    db_path: Callable[[], Path],
    row_to_dict: Callable[[sqlite3.Row], dict],
) -> None:
    """Bind the queue-owned runtime seams after ``amphora`` initializes."""
    global _DB_PATH_PROVIDER, _ROW_TO_DICT_PROVIDER
    _DB_PATH_PROVIDER = db_path
    _ROW_TO_DICT_PROVIDER = row_to_dict


def _db_path() -> Path:
    if _DB_PATH_PROVIDER is None:
        raise RuntimeError("amphora_terminal_contract_runtime_unbound")
    return _DB_PATH_PROVIDER()


def _row_to_dict(row: sqlite3.Row) -> dict:
    if _ROW_TO_DICT_PROVIDER is None:
        raise RuntimeError("amphora_terminal_contract_runtime_unbound")
    return _ROW_TO_DICT_PROVIDER(row)


def _validated_failed_terminal_outbox(
    value: object,
    *,
    task_id: str,
) -> dict | None:
    """Validate the Amphora-owned terminal outbox, or reject reserved-key drift."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("amphora_failed_terminal_outbox_invalid")
    required = {
        "schema_version",
        "task_id",
        "session_id",
        "input_revision",
        "status",
        "reason",
        "retry_count",
        "max_retries",
        "created_at",
        "cognitive_event_ids",
        "cognitive_event_count",
        "cognitive_event_ids_sha256",
        "payload_sha256",
    }
    proof_fields = {
        "runtime_receipt_id",
        "production_event_id",
        "generation_id",
    }
    if (
        set(value) - (required | {"committed_at"} | proof_fields)
        or not required.issubset(value)
        or value.get("schema_version")
        != "mnemos.amphora_failed_terminal_receipt_outbox.v2"
        or value.get("task_id") != str(task_id)
        or not str(value.get("session_id") or "").strip()
        or not str(value.get("input_revision") or "").strip()
        or value.get("status") not in {"pending", "committed"}
        or not str(value.get("reason") or "").strip()
        or not str(value.get("created_at") or "").strip()
        or not isinstance(value.get("retry_count"), int)
        or not isinstance(value.get("max_retries"), int)
        or not isinstance(value.get("cognitive_event_ids"), list)
        or any(
            not isinstance(event_id, str) or not event_id
            for event_id in value.get("cognitive_event_ids", [])
        )
        or len(set(value.get("cognitive_event_ids", [])))
        != len(value.get("cognitive_event_ids", []))
        or value.get("cognitive_event_count")
        != len(value.get("cognitive_event_ids", []))
        or value.get("cognitive_event_ids_sha256")
        != _cognitive_event_ids_sha256(value.get("cognitive_event_ids", []))
        or value.get("payload_sha256")
        != distillation_failed_terminal_sha256(
            task_id=str(value.get("task_id") or ""),
            session_id=str(value.get("session_id") or ""),
            input_revision=str(value.get("input_revision") or ""),
            reason=str(value.get("reason") or ""),
            retry_count=int(value.get("retry_count") or 0),
            max_retries=int(value.get("max_retries") or 0),
            cognitive_event_ids=list(value.get("cognitive_event_ids") or []),
        )
        or int(value["retry_count"]) < 1
        or int(value["max_retries"]) < 1
        or int(value["retry_count"]) > int(value["max_retries"])
        or (
            value.get("status") == "committed"
            and (
                not str(value.get("committed_at") or "").strip()
                or any(
                    not str(value.get(field) or "").strip()
                    for field in proof_fields
                )
            )
        )
        or (
            value.get("status") == "pending"
            and (
                "committed_at" in value
                or any(field in value for field in proof_fields)
            )
        )
    ):
        raise RuntimeError("amphora_failed_terminal_outbox_invalid")
    return dict(value)


def _failed_terminal_outbox_matches_row(
    row: sqlite3.Row,
    outbox: Mapping[str, object],
    *,
    allow_archived: bool = False,
) -> bool:
    expected_reason = f"retry_exhausted:{str(row['terminal_reason'] or '')}"
    retry_count = outbox.get("retry_count")
    max_retries = outbox.get("max_retries")
    if not isinstance(retry_count, int) or not isinstance(max_retries, int):
        return False
    return (
        (str(row["status"]) == "failed" or (allow_archived and str(row["status"]) == "archived"))
        and str(outbox.get("session_id") or "") == str(row["session_id"] or "")
        and str(outbox.get("input_revision") or "")
        == str(row["input_revision"] or "")
        and str(outbox.get("reason") or "") == expected_reason
        and retry_count == int(row["retry_count"] or 0)
        and max_retries == int(row["max_retries"] or 0)
    )


def _terminal_receipt_payload(receipt: DistillationWriteReceipt) -> dict:
    return canonical_distillation_write_receipt_payload(receipt)


def _terminal_receipt_payload_sha256(payload: Mapping[str, object]) -> str:
    receipt = _distillation_write_receipt_from_payload(dict(payload))
    return distillation_write_receipt_sha256(receipt)


def _terminal_receipt_matches_row(
    row: sqlite3.Row,
    receipt: DistillationWriteReceipt,
    *,
    allow_archived: bool = False,
) -> bool:
    try:
        written_paths = json.loads(str(row["written_paths"] or "[]"))
        proposal_ids = json.loads(str(row["proposal_ids"] or "[]"))
        required_receipts = json.loads(
            str(row["required_consumer_receipts"] or "[]")
        )
    except (json.JSONDecodeError, TypeError):
        return False
    return (
        (
            receipt.status == str(row["status"])
            or (allow_archived and str(row["status"]) == "archived")
        )
        and receipt.terminal_reason == str(row["terminal_reason"] or "")
        and list(receipt.written_pages) == written_paths
        and list(receipt.proposal_ids) == proposal_ids
        and receipt.written_count == int(row["written_count"] or 0)
        and list(receipt.required_consumer_receipts) == required_receipts
    )


def _distillation_write_receipt_from_payload(
    value: object,
) -> DistillationWriteReceipt:
    if not isinstance(value, dict):
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid")
    required = {
        "status",
        "terminal_reason",
        "written_pages",
        "proposal_ids",
        "expected_count",
        "written_count",
        "failed_count",
        "required_consumer_receipts",
        "schema_version",
    }
    if set(value) != required:
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid")
    sequence_fields = (
        "written_pages",
        "proposal_ids",
        "required_consumer_receipts",
    )
    count_fields = ("expected_count", "written_count", "failed_count")
    if (
        any(not isinstance(value[field], list) for field in sequence_fields)
        or any(
            not isinstance(item, str)
            for field in sequence_fields
            for item in value[field]
        )
        or any(
            not isinstance(value[field], int) or isinstance(value[field], bool)
            for field in count_fields
        )
        or value.get("schema_version")
        != "mnemos.distillation_write_receipt.v1"
    ):
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid")
    try:
        receipt = DistillationWriteReceipt(
            status=str(value["status"]),
            terminal_reason=str(value["terminal_reason"]),
            written_pages=tuple(value["written_pages"]),
            proposal_ids=tuple(value["proposal_ids"]),
            expected_count=int(value["expected_count"]),
            written_count=int(value["written_count"]),
            failed_count=int(value["failed_count"]),
            required_consumer_receipts=tuple(
                value["required_consumer_receipts"]
            ),
            schema_version=str(value["schema_version"]),
        )
    except (TypeError, ValueError):
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid") from None
    if (
        not receipt.terminal
        or receipt.status not in {"committed", "intentional_skip"}
        or _terminal_receipt_payload(receipt) != value
    ):
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid")
    return receipt


def _validated_terminal_receipt_outbox(
    value: object,
    *,
    task_id: str,
) -> dict | None:
    """Validate a success/skip terminal outbox frozen in the task transaction."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid")
    required = {
        "schema_version",
        "task_id",
        "session_id",
        "input_revision",
        "status",
        "created_at",
        "receipt",
        "receipt_sha256",
        "cognitive_event_ids",
        "cognitive_event_count",
        "cognitive_event_ids_sha256",
    }
    proof_fields = {
        "runtime_receipt_id",
        "production_event_id",
        "generation_id",
    }
    receipt_payload = value.get("receipt")
    if not isinstance(receipt_payload, Mapping):
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid")
    if (
        set(value) - (required | {"committed_at"} | proof_fields)
        or not required.issubset(value)
        or value.get("schema_version")
        != "mnemos.amphora_terminal_receipt_outbox.v1"
        or value.get("task_id") != str(task_id)
        or not str(value.get("session_id") or "").strip()
        or not str(value.get("input_revision") or "").strip()
        or value.get("status") not in {"pending", "committed"}
        or not str(value.get("created_at") or "").strip()
        or not isinstance(value.get("cognitive_event_ids"), list)
        or any(
            not isinstance(event_id, str) or not event_id
            for event_id in value.get("cognitive_event_ids", [])
        )
        or len(set(value.get("cognitive_event_ids", [])))
        != len(value.get("cognitive_event_ids", []))
        or value.get("cognitive_event_count")
        != len(value.get("cognitive_event_ids", []))
        or value.get("cognitive_event_ids_sha256")
        != _cognitive_event_ids_sha256(value.get("cognitive_event_ids", []))
        or value.get("receipt_sha256")
        != _terminal_receipt_payload_sha256(receipt_payload)
        or (
            value.get("status") == "committed"
            and (
                not str(value.get("committed_at") or "").strip()
                or any(
                    not str(value.get(field) or "").strip()
                    for field in proof_fields
                )
            )
        )
        or (
            value.get("status") == "pending"
            and (
                "committed_at" in value
                or any(field in value for field in proof_fields)
            )
        )
    ):
        raise RuntimeError("amphora_terminal_receipt_outbox_invalid")
    _distillation_write_receipt_from_payload(value.get("receipt"))
    return dict(value)


def _normalized_cognitive_event_ids(meta: Mapping[str, object]) -> list[str]:
    values = meta.get("cognitive_sync_event_ids")
    if not isinstance(values, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            event_id
            for value in values
            if (event_id := str(value or "").strip())
        )
    )


def _cognitive_event_ids_sha256(event_ids: object) -> str:
    canonical = json.dumps(
        list(event_ids) if isinstance(event_ids, (list, tuple)) else [],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _terminal_outbox_anchor_sha256(
    outbox: Mapping[str, object],
) -> str:
    ignored = {
        "status",
        "committed_at",
        "runtime_receipt_id",
        "production_event_id",
        "generation_id",
    }
    canonical = json.dumps(
        {
            key: value
            for key, value in outbox.items()
            if key not in ignored
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _terminal_outbox_anchor_matches_row(
    row: sqlite3.Row,
    outbox: Mapping[str, object],
) -> bool:
    try:
        anchor = str(row["terminal_outbox_anchor_sha256"] or "")
    except (IndexError, KeyError):
        return False
    return bool(anchor) and anchor == _terminal_outbox_anchor_sha256(outbox)


def _task_with_frozen_terminal_denominator(
    row: sqlite3.Row,
    outbox: Mapping[str, object],
) -> dict:
    if (
        str(row["session_id"] or "") != str(outbox.get("session_id") or "")
        or str(row["input_revision"] or "")
        != str(outbox.get("input_revision") or "")
    ):
        raise RuntimeError("amphora_terminal_outbox_identity_drift")
    task = _row_to_dict(row)
    task["session_id"] = str(outbox["session_id"])
    task["input_revision"] = str(outbox["input_revision"])
    meta = dict(task.get("meta") or {})
    cognitive_event_ids = outbox.get("cognitive_event_ids")
    if not isinstance(cognitive_event_ids, list):
        raise RuntimeError("amphora_terminal_outbox_cognitive_events_invalid")
    meta["cognitive_sync_event_ids"] = list(cognitive_event_ids)
    task["meta"] = meta
    return task


def _require_canonical_task_database_config(config: object) -> Path:
    configured = (
        config.get("database_dir")
        if isinstance(config, Mapping)
        else getattr(config, "database_dir", None)
    )
    if not isinstance(configured, (str, Path)):
        raise RuntimeError("failed_terminal_runtime_ledger_identity_mismatch")
    configured_path = Path(configured).expanduser().resolve()
    canonical_path = _db_path().parent.expanduser().resolve()
    if configured_path != canonical_path:
        raise RuntimeError("failed_terminal_runtime_ledger_identity_mismatch")
    return canonical_path


def _validated_message_cleanup_outbox(
    value: object,
    *,
    task_id: str,
    messages_path: str | None,
) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError("amphora_message_cleanup_outbox_invalid")
    required = {
        "schema_version",
        "task_id",
        "status",
        "messages_path_sha256",
        "created_at",
    }
    allowed = required | {"messages_path", "committed_at"}
    status = value.get("status")
    if (
        set(value) - allowed
        or not required.issubset(value)
        or value.get("schema_version")
        != "mnemos.amphora_message_cleanup_outbox.v1"
        or value.get("task_id") != str(task_id)
        or status not in {"pending", "committed"}
        or not str(value.get("created_at") or "").strip()
        or (
            status == "pending"
            and (
                not str(value.get("messages_path") or "").strip()
                or value.get("messages_path") != messages_path
                or "committed_at" in value
            )
        )
        or (
            status == "committed"
            and (
                "messages_path" in value
                or not str(value.get("committed_at") or "").strip()
            )
        )
        or (
            status == "pending"
            and value.get("messages_path_sha256")
            != hashlib.sha256(
                str(value.get("messages_path") or "").encode("utf-8")
            ).hexdigest()
        )
        or (
            status == "committed"
            and (
                len(str(value.get("messages_path_sha256") or "")) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in str(
                        value.get("messages_path_sha256") or ""
                    )
                )
            )
        )
    ):
        raise RuntimeError("amphora_message_cleanup_outbox_invalid")
    return dict(value)


def _identifier_filter(conn: sqlite3.Connection, identifier: str) -> tuple:
    if conn.execute("SELECT 1 FROM distillation_tasks WHERE task_id = ?", (identifier,)).fetchone():
        return "task_id", identifier
    row = conn.execute(
        """
        SELECT task_id FROM distillation_tasks WHERE session_id=?
        ORDER BY generation DESC, created_at DESC LIMIT 1
        """,
        (identifier,),
    ).fetchone()
    return "task_id", row["task_id"] if row else identifier


def _retry_time(retry_count: int) -> str:
    minutes = min(2**retry_count, MINUTES_SECONDS)  # 上限 24 小时
    return (datetime.now() + timedelta(minutes=minutes)).isoformat()
