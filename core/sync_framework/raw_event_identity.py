# -*- coding: utf-8 -*-
"""Canonical Raw revision identity and completeness policy."""

from __future__ import annotations

import hashlib
import json
import zlib
from datetime import datetime
from typing import Any, Dict, Optional

from core.sync_framework.native_raw_contract_ledger import NativeRawContractLedger

DEFAULT_RECALC_DAYS = 7

DEFAULT_RETENTION_DAYS = 30

DEFAULT_SURVIVAL_PRUNE_THRESHOLD = 35.0

DEFAULT_FRESHNESS_HALF_LIFE_DAYS = 30

DEFAULT_CANONICAL_RAW_PAGE_SNAPSHOT_BYTES = 16 * 1024 * 1024

_NATIVE_RAW_CONTRACT_LEDGER = NativeRawContractLedger()


class RawEventIdentitySchemaMigrationRequired(RuntimeError):
    """Raised when historical turn-number uniqueness needs explicit migration."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _bind_source_support_metadata(metadata: Any, source_agent: str) -> Dict[str, Any]:
    """Load the agent contract only after this storage module is initialized."""
    from core.agent_kit.source_support_manifest import bind_source_support_metadata

    return bind_source_support_metadata(metadata, source_agent)


def _validate_native_raw_contract(
    metadata: Dict[str, Any],
    completeness: Dict[str, Any],
    source_agent: str,
) -> tuple[str, ...]:
    """Validate one native receipt without creating an import-time cycle."""
    from core.agent_kit.source_support_manifest import validate_native_raw_contract

    return validate_native_raw_contract(metadata, completeness, source_agent)


def _record_native_raw_contract_outcome(
    metadata: Dict[str, Any],
    errors: tuple[str, ...],
) -> None:
    """Persist contract conformance without discarding the visible Raw payload."""
    result_errors = list(dict.fromkeys(errors))
    expected_state = "conformant" if not result_errors else "nonconforming"
    supplied_state = metadata.get("support_raw_contract_state")
    if supplied_state not in (None, "", expected_state):
        result_errors.append("support_raw_contract_state_forged")
    supplied_errors = metadata.get("support_raw_contract_errors")
    if supplied_errors not in (None, "", [], tuple(result_errors)):
        result_errors.append("support_raw_contract_errors_forged")
    metadata["support_raw_contract_state"] = (
        "conformant" if not result_errors else "nonconforming"
    )
    metadata["support_raw_contract_errors"] = list(dict.fromkeys(result_errors))


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _compress_text(text: str) -> bytes:
    return zlib.compress((text or "").encode("utf-8"), level=6)


def _utcnow() -> str:
    return datetime.now().isoformat()


def _parse_datetime(value: Any, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _event_id(
    source_agent: str,
    session_id: str,
    turn_number: int,
    *,
    native_event_id: str = "",
    parser: str = "",
    parser_version: str = "",
    source_artifact_id: str = "",
    artifact_offset: str = "",
) -> str:
    """Derive one logical event alias without collapsing native events.

    Existing rows retain their historical source/session/turn key.  New
    native ingestion uses an explicit producer identity when available, or a
    complete parser/version/artifact/offset tuple when it is not.  Partial
    fallback metadata intentionally does not change the historical key.
    """
    if native_event_id:
        raw = f"native-event-v1:{source_agent}:{session_id}:{native_event_id}"
    elif parser and parser_version and source_artifact_id and artifact_offset:
        raw = (
            "parser-artifact-offset-v1:"
            f"{source_agent}:{session_id}:{parser}:{parser_version}:"
            f"{source_artifact_id}:{artifact_offset}"
        )
    else:
        raw = f"{source_agent}:{session_id}:{turn_number}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def compute_logical_event_id(
    source_agent: str,
    session_id: str,
    turn_number: int,
    *,
    native_event_id: str = "",
    parser: str = "",
    parser_version: str = "",
    source_artifact_id: str = "",
    artifact_offset: str = "",
) -> str:
    """Return the stable logical alias shared by all revisions of one turn."""
    return _event_id(
        source_agent,
        session_id,
        turn_number,
        native_event_id=native_event_id,
        parser=parser,
        parser_version=parser_version,
        source_artifact_id=source_artifact_id,
        artifact_offset=artifact_offset,
    )


def _revision_id(logical_event_id: str, content_hash: str) -> str:
    raw = f"{logical_event_id}\0{content_hash}"
    return "rawrev-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def compute_raw_content_hash(
    *,
    user_content: str,
    assistant_content: str,
    reasoning: str = "",
    tool_calls: Any = None,
    tool_results: Any = None,
    attachments: Any = None,
    raw_event_refs: Any = None,
    metadata: Any = None,
) -> str:
    """Compute a stable hash over the full raw turn payload."""
    payload = {
        "user_content": user_content or "",
        "assistant_content": assistant_content or "",
        "reasoning": reasoning or "",
        "tool_calls": tool_calls or [],
        "tool_results": tool_results or [],
        "attachments": attachments or [],
        "raw_event_refs": raw_event_refs or [],
        "metadata": metadata or {},
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def classify_completeness(
    completeness: Optional[Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None
) -> str:
    """Classify raw capture quality for retention/distillation gates."""
    comp = dict(completeness or {})
    meta = dict(metadata or {})
    if meta.get("support_raw_contract_state") == "nonconforming":
        return "partial"
    if meta.get("support_fidelity_contract_state") == "observed_mismatch":
        return "partial"
    if comp.get("truncated") or comp.get("loss_reasons"):
        return "partial"
    if meta.get("source_fidelity") == "unknown" or comp.get("source_fidelity") == "unknown":
        return "partial"
    if comp.get("visible_text") == "host_provided":
        return "derived"
    if meta.get("source_fidelity") in ("derived", "experimental") or comp.get(
        "source_fidelity"
    ) in ("derived", "experimental"):
        return "derived"
    return "complete"


def _quality_rank(status: str, origin: str) -> int:
    base = {"partial": 1, "derived": 2, "complete": 3}.get(status, 0)
    origin_bonus = {"sync_engine": 2, "capture_service": 1}.get(origin, 0)
    return base * 10 + origin_bonus


def _initial_confidence(status: str) -> float:
    if status == "complete":
        return 1.0
    if status == "derived":
        return 0.65
    return 0.4
