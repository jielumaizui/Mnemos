"""Immutable synthetic-safe Agent Kit runtime probe contract."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from core.ops.health_contract import CANONICAL_HEALTH_CHECK_IDS_HASH

RUNTIME_PROBE_SCHEMA_VERSION = "mnemos.agent_runtime_probe.v1"
RUNTIME_PROBE_USER_CONTENT = "mnemos-runtime-probe-user"
RUNTIME_PROBE_ASSISTANT_CONTENT = "mnemos-runtime-probe-assistant"
RUNTIME_PROBE_CALL_ID = "mnemos-runtime-probe-call"
EXPECTED_RUNTIME_PROBE_COMPLETENESS = {
    "visible_text": "full",
    "tool_calls": "full",
    "tool_results": "full",
    "truncated": False,
}


def runtime_probe_contract() -> dict[str, Any]:
    """Return the exact public sample a host must echo through MCP."""
    return {
        "tool": "agent_runtime_probe",
        "prerequisite_tool": "health_check",
        "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
        "health_check_ids_hash": CANONICAL_HEALTH_CHECK_IDS_HASH,
        "sample": {
            "schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
            "user_content": RUNTIME_PROBE_USER_CONTENT,
            "assistant_content": RUNTIME_PROBE_ASSISTANT_CONTENT,
            "tool_calls": [
                {
                    "id": RUNTIME_PROBE_CALL_ID,
                    "name": "health_check",
                    "arguments": {},
                }
            ],
            "tool_results": [
                {"tool_call_id": RUNTIME_PROBE_CALL_ID, "status": "ok"}
            ],
            "completeness": dict(EXPECTED_RUNTIME_PROBE_COMPLETENESS),
        },
    }


def runtime_probe_canary_hash(
    *,
    health_check_ids_hash: str,
    sample: Mapping[str, Any],
) -> str:
    """Hash the exact synthetic-safe probe without persisting its content."""
    payload = {
        "schema_version": "mnemos.agent_runtime_canary.v1",
        "health_check_ids_hash": str(health_check_ids_hash),
        "sample": dict(sample),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
