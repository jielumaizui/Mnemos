"""Public JSON-RPC constants and bounded MCP health projection."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Mapping


# JSON-RPC 2.0 标准错误码
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
MCP_TOOL_EXECUTION_ERROR = -32000
MCP_LAUNCH_CAPABILITY_REF_ENV = "MNEMOS_MCP_LAUNCH_CAPABILITY_REF"
MCP_RECOVERABLE_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
    sqlite3.Error,
)


def _compact_mcp_health_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the bounded public health view appropriate for an MCP turn.

    The complete health report can contain large audit inventories and nested
    recovery evidence.  Sending that object through a host tool call makes a
    simple runtime handshake exceed client output limits, while the probe only
    needs the canonical check-set hash.  The full diagnostic remains available
    through the CLI; this MCP surface intentionally exposes only statuses and
    stable identifiers.
    """
    raw_checks = report.get("checks")
    checks: Dict[str, str] = {}
    if isinstance(raw_checks, Mapping):
        for name, value in raw_checks.items():
            if isinstance(value, Mapping):
                status = value.get("status", "unknown")
            else:
                status = value
            checks[str(name)] = str(status or "unknown")

    counts: Dict[str, int] = {}
    for status in checks.values():
        counts[status] = counts.get(status, 0) + 1

    def _string_list(name: str) -> List[str]:
        value = report.get(name)
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item) for item in value[:64]]

    return {
        "schema_version": "mnemos.mcp_health.v1",
        "ok": bool(report.get("ok", False)),
        "usable": bool(report.get("usable", False)),
        "strict_ok": bool(report.get("strict_ok", False)),
        "status": str(report.get("status") or "unknown"),
        "health_check_ids": _string_list("health_check_ids"),
        "health_check_ids_hash": str(report.get("health_check_ids_hash") or ""),
        "checks": checks,
        "counts": counts,
        "strict_failures": _string_list("strict_failures"),
        "failed_checks": _string_list("failed_checks"),
        "degraded_checks": _string_list("degraded_checks"),
        "warning_checks": _string_list("warning_checks"),
        "detail_surface": "python3 mnemos_cli.py health --json",
    }
