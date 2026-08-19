"""Formal, evidence-bound replay for failed distillation occurrences."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.ops.operational_incident import (
    OperationalIncidentStore,
    validate_operational_incident_schema,
)
from core.ops.operational_incident_identity import canonical_replay_input_binding_hash
from core.sync_framework.raw_event_reader import (
    CanonicalRawReadError,
    read_admissible_raw_revisions_readonly,
)
from core.utils import read_bytes_value

REPLAY_PLAN_SCHEMA_VERSION = "mnemos.operational_incident_replay_plan.v1"
REPLAY_RESULT_SCHEMA_VERSION = "mnemos.operational_incident_replay_result.v1"


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _load_canonical_raw_input(
    raw_db: str | Path,
    *,
    revision_ids: list[str],
    session_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Load exact current Raw revisions and derive replay input from stored bytes."""

    try:
        turns = {
            turn.revision_id: turn
            for turn in read_admissible_raw_revisions_readonly(raw_db, revision_ids)
        }
    except CanonicalRawReadError as exc:
        raise ValueError(
            "canonical Raw store does not contain every bound revision"
        ) from exc
    if set(turns) != set(revision_ids):
        raise ValueError("canonical Raw store does not contain every bound revision")
    ordered = [turns[revision_id] for revision_id in revision_ids]
    if any(turn.session_id != session_id for turn in ordered):
        raise ValueError("canonical Raw revisions do not match the bound session")
    messages: list[dict[str, Any]] = []
    for turn in ordered:
        if turn.user_content:
            messages.append({"role": "user", "content": turn.user_content})
        if turn.assistant_content:
            messages.append({"role": "assistant", "content": turn.assistant_content})
    if not messages:
        raise ValueError("canonical Raw revisions contain no visible messages")
    from core.hephaestus.distillation_text import build_session_text

    visible_input = build_session_text(messages, lossless=True)
    meta = {
        "raw_event_refs": [
            {
                "event_id": turn.logical_event_id,
                "revision_id": turn.revision_id,
                "content_hash": turn.content_hash,
            }
            for turn in ordered
        ]
    }
    return messages, meta, visible_input


def plan_distillation_failure_replay(
    incident_db: str | Path,
    *,
    occurrence_id: str,
) -> dict[str, Any]:
    """Inspect one replay target without creating commands or receipts."""

    db_path = Path(incident_db).expanduser().resolve(strict=True)
    uri = f"{db_path.as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        validate_operational_incident_schema(conn)
        row = conn.execute(
            """
            SELECT occurrence.*, incident.status AS incident_status,
                   report.root_cause_status, report.report_id,
                   replay.command_id, replay.status AS replay_status
            FROM incident_occurrences AS occurrence
            JOIN operational_incidents AS incident
              ON incident.incident_id=occurrence.incident_id
            LEFT JOIN root_cause_reports AS report
              ON report.incident_id=occurrence.incident_id
            LEFT JOIN incident_replay_commands AS replay
              ON replay.occurrence_id=occurrence.occurrence_id
            WHERE occurrence.occurrence_id=?
            ORDER BY report.report_revision DESC
            LIMIT 1
            """,
            (occurrence_id,),
        ).fetchone()
    if row is None:
        raise ValueError("unknown operational incident occurrence")
    artifact_path = Path(str(row["artifact_path"])).resolve(strict=True)
    if not artifact_path.is_file():
        raise RuntimeError("replay artifact is missing")
    registered_hash = str(row["artifact_hash"])
    session_id = str(row["session_id"])
    source_event_refs = json.loads(str(row["source_event_refs_json"]))
    input_binding_hash = canonical_replay_input_binding_hash(
        session_id=session_id,
        prompt_hash=str(row["prompt_hash"]),
        visible_input_sha256=str(row["visible_input_sha256"]),
        response_hash=str(row["response_hash"]),
        source_event_refs=source_event_refs,
        artifact_hash=registered_hash,
    )
    plan_core = {
        "schema_version": REPLAY_PLAN_SCHEMA_VERSION,
        "incident_id": str(row["incident_id"]),
        "occurrence_id": str(row["occurrence_id"]),
        "artifact_hash": registered_hash,
        "artifact_size": artifact_path.stat().st_size,
        "incident_db_scope_hash": _sha256_text(str(db_path)),
        "session_id_hash": _sha256_json(session_id),
        "prompt_hash": str(row["prompt_hash"]),
        "visible_input_sha256": str(row["visible_input_sha256"]),
        "response_hash": str(row["response_hash"]),
        "source_event_refs": source_event_refs,
        "input_binding_hash": input_binding_hash,
        "artifact_acl": str(row["artifact_acl"]),
        "retention_class": str(row["retention_class"]),
        "root_cause_status": str(row["root_cause_status"] or ""),
        "report_id": str(row["report_id"] or ""),
        "incident_status": str(row["incident_status"]),
        "existing_command_id": str(row["command_id"] or ""),
        "existing_replay_status": str(row["replay_status"] or ""),
        "requires_canonical_raw_input": True,
        "writes_wiki": False,
    }
    return {**plan_core, "plan_hash": _sha256_json(plan_core), "read_only": True}


def execute_distillation_failure_replay(
    incident_db: str | Path,
    *,
    occurrence_id: str,
    expected_plan_hash: str,
    expected_artifact_hash: str,
    raw_db: str | Path,
    runner: Callable[[str, list[dict[str, Any]], dict[str, Any]], Any],
) -> dict[str, Any]:
    """Run extraction replay and append one terminal receipt.

    The runner is injected so the contract can be tested without a network.
    Production passes ``DistillationEngine.process``. Wiki writes are excluded.
    """

    plan = plan_distillation_failure_replay(
        incident_db,
        occurrence_id=occurrence_id,
    )
    if plan["plan_hash"] != expected_plan_hash:
        raise RuntimeError("replay plan changed; rerun dry-run")
    if plan["artifact_hash"] != expected_artifact_hash:
        raise RuntimeError("replay artifact binding changed")
    store = OperationalIncidentStore(incident_db)
    store.record_artifact_access(
        occurrence_id,
        principal="operational-incident-replay",
        purpose="formal-distillation-replay",
    )
    occurrence = store.get_occurrence(occurrence_id)
    artifact_path = Path(str(occurrence["artifact_path"])).resolve(strict=True)
    if _sha256_bytes(read_bytes_value(artifact_path)) != plan["artifact_hash"]:
        raise RuntimeError("replay artifact hash mismatch")
    session_id = str(occurrence["session_id"])
    messages, meta, visible_input = _load_canonical_raw_input(
        raw_db,
        revision_ids=list(plan["source_event_refs"]),
        session_id=session_id,
    )
    if plan["session_id_hash"] != _sha256_json(session_id):
        raise ValueError("canonical raw session does not match occurrence")
    if _sha256_text(visible_input) != plan["visible_input_sha256"]:
        raise ValueError("canonical visible input does not match occurrence")
    raw_refs = meta.get("raw_event_refs")
    if not isinstance(raw_refs, list):
        raise ValueError("formal replay requires canonical raw event refs")
    provided_refs = [
        str(item.get("revision_id") or "")
        for item in raw_refs
        if isinstance(item, dict) and str(item.get("revision_id") or "")
    ]
    if provided_refs != list(plan["source_event_refs"]):
        raise ValueError("canonical raw event refs do not match occurrence")
    if plan["root_cause_status"] != "confirmed":
        raise RuntimeError("formal replay requires a confirmed root cause")
    if plan["existing_replay_status"] in {"committed", "failed"}:
        raise RuntimeError("replay occurrence already has a terminal receipt")
    command = store.create_replay_command(
        str(plan["incident_id"]),
        occurrence_id=occurrence_id,
    )
    try:
        result = runner(session_id, messages, meta)
        contract_valid = getattr(result, "extraction_contract_valid", None) is True
        replay_input_spec = getattr(result, "input_spec", None)
        input_binding_valid = bool(
            replay_input_spec is not None
            and getattr(replay_input_spec, "visible_input_sha256", "")
            == plan["visible_input_sha256"]
            and list(getattr(replay_input_spec, "source_event_ids", ()))
            == list(plan["source_event_refs"])
        )
        judgment = str(getattr(result, "judgment", "") or "")
        error = str(getattr(result, "error", "") or "")
        status = (
            "committed"
            if contract_valid and input_binding_valid and judgment != "error" and not error
            else "failed"
        )
        output = {
            "judgment": judgment,
            "extraction_contract_valid": contract_valid,
            "input_binding_valid": input_binding_valid,
            "extraction_output_hash": str(getattr(result, "extraction_output_hash", "") or ""),
            "error_type": "" if not error else "distillation_result_error",
        }
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        status = "failed"
        output = {
            "judgment": "error",
            "extraction_contract_valid": False,
            "input_binding_valid": False,
            "extraction_output_hash": "",
            "error_type": type(exc).__name__,
        }
    output_hash = _sha256_json(output)
    receipt = store.record_replay_receipt(
        str(command["command_id"]),
        status=status,
        output_hash=output_hash,
        input_binding_hash=str(plan["input_binding_hash"]),
        executor="formal_distillation_replay.v1",
    )
    return {
        "schema_version": REPLAY_RESULT_SCHEMA_VERSION,
        "incident_id": str(plan["incident_id"]),
        "occurrence_id": occurrence_id,
        "command_id": str(command["command_id"]),
        "receipt_id": str(receipt["receipt_id"]),
        "status": status,
        "output_hash": output_hash,
        "writes_wiki": False,
    }


__all__ = [
    "REPLAY_PLAN_SCHEMA_VERSION",
    "REPLAY_RESULT_SCHEMA_VERSION",
    "execute_distillation_failure_replay",
    "plan_distillation_failure_replay",
]
